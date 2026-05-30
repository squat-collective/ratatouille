"""Executor tests — orchestration of the engine/v1 + catalog/v1 + storage/v1 composition.

ADR-024 end state: the runner is a pure orchestrator (no embedded DuckDB/PyIceberg).
These tests mock the three gRPC clients and the data-plane binding, then drive
execute_pipeline and assert on the orchestration: descriptor assembly, branch
lifecycle via catalog/v1, engine.Execute is called with the right request,
hooks dispatch, archive, versioned reads, error/cancel paths.
"""

from __future__ import annotations

from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from common.v1 import common_pb2, data_plane_pb2  # type: ignore[import-untyped]

from rat_runner.config import NessieConfig, S3Config
from rat_runner.executor import execute_pipeline
from rat_runner.models import QualityTestResult, RunState, RunStatus

_EXEC = "rat_runner.executor"


def _make_run(**kwargs) -> RunState:
    defaults = dict(
        run_id="r1", namespace="myns", layer="silver", pipeline_name="orders", trigger="manual"
    )
    defaults.update(kwargs)
    return RunState(**defaults)


def _make_plane(**kwargs):
    defaults = dict(
        name="default",
        engine_addr="engine:1",
        catalog_addr="catalog:2",
        storage_addr="storage:3",
        format="iceberg",
        catalog_protocol="iceberg-rest",
        supports_branching=True,
    )
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


@pytest.fixture(autouse=True)
def _empty_registry():
    """Stub out plugin discovery so tests exercise the built-in pipeline types."""
    mock_registry = MagicMock()
    mock_registry.get_strategy.return_value = None
    mock_registry.get_helpers.return_value = {}
    mock_registry.pipeline_type_names.return_value = []
    mock_registry.get_pipeline_type.return_value = None
    mock_registry.dispatch_hooks.return_value = None
    with patch(f"{_EXEC}.PluginRegistry", return_value=mock_registry):
        yield mock_registry


@pytest.fixture(autouse=True)
def _engine_mode(monkeypatch):
    """All executor tests run engine-mode; the local path is gone (ADR-024 #10)."""
    monkeypatch.setenv("RAT_ENGINE_MODE", "1")


def _ok_describe_engine():
    return SimpleNamespace(
        formats=["iceberg"], languages=["sql", "python"], capabilities=[], dialects=["duckdb"]
    )


def _ok_describe_catalog(branching=True):
    caps = ["branching", "history"] if branching else ["history"]
    return SimpleNamespace(capabilities=caps, formats=["iceberg"])


def _ok_describe_storage():
    return SimpleNamespace(schemes=["s3"])


def _ok_get_table(*args, **kwargs):
    """Return a real proto-shaped GetTableResponse (executor builds TableDescriptor from it)."""
    return SimpleNamespace(
        exists=True,
        format="iceberg",
        identifier="myns.silver.orders",
        catalog=data_plane_pb2.CatalogDescriptor(
            protocol="iceberg-rest", uri="http://nessie/iceberg", branch="main"
        ),
    )


def _storage_descriptor():
    return data_plane_pb2.StorageDescriptor(
        scheme="s3",
        s3=common_pb2.S3Credentials(
            endpoint="minio:9000",
            access_key_id="ak",
            secret_access_key="sk",
            bucket="rat",
        ),
    )


@pytest.fixture
def mocks(monkeypatch):
    """Common engine-mode mocks: EngineClient, CatalogClient, StorageClient, BindingConfig.

    Returns a SimpleNamespace with the instances + the class mocks so tests can
    customize per-call return values + assert on calls.
    """
    plane = _make_plane()

    eng_inst = MagicMock()
    eng_inst.describe.return_value = _ok_describe_engine()
    eng_inst.execute.return_value = SimpleNamespace(rows_written=5, error="", output_schema=b"")
    eng_inst.query.return_value = MagicMock(__len__=lambda self: 0)  # quality: 0 violations

    cat_inst = MagicMock()
    cat_inst.describe.return_value = _ok_describe_catalog(branching=True)
    cat_inst.get_table.side_effect = _ok_get_table
    cat_inst.create_branch.return_value = SimpleNamespace(commit_hash="abc")
    cat_inst.merge_branch.return_value = SimpleNamespace(merged=True)
    cat_inst.delete_branch.return_value = SimpleNamespace(deleted=True)

    st_inst = MagicMock()
    st_inst.describe.return_value = _ok_describe_storage()
    st_inst.vend_descriptor.return_value = _storage_descriptor()

    binding = MagicMock()
    binding.data_planes = {"default": plane}
    binding.resolve.return_value = plane

    with ExitStack() as stack:
        eng_cls = stack.enter_context(patch(f"{_EXEC}.EngineClient"))
        cat_cls = stack.enter_context(patch(f"{_EXEC}.CatalogClient"))
        st_cls = stack.enter_context(patch(f"{_EXEC}.StorageClient"))
        binding_cls = stack.enter_context(patch(f"{_EXEC}.BindingConfig"))
        # Quality runs by default returns no violations and writes nothing.
        rqt = stack.enter_context(patch(f"{_EXEC}.run_quality_tests", return_value=[]))
        # The executor reads pipeline source from S3; tests override per case.
        rs3 = stack.enter_context(patch(f"{_EXEC}.read_s3_text"))
        # Default: bronze SQL pipeline, no config.yaml.
        rs3.side_effect = lambda _cfg, key: "SELECT 1 AS x" if key.endswith(".sql") else None
        eng_cls.return_value = eng_inst
        cat_cls.return_value = cat_inst
        st_cls.return_value = st_inst
        binding_cls.load.return_value = binding
        yield SimpleNamespace(
            engine=eng_inst,
            catalog=cat_inst,
            storage=st_inst,
            binding=binding,
            plane=plane,
            engine_cls=eng_cls,
            catalog_cls=cat_cls,
            storage_cls=st_cls,
            binding_cls=binding_cls,
            run_quality_tests=rqt,
            read_s3_text=rs3,
        )


def _s3():
    return S3Config(
        endpoint="minio:9000", access_key="ak", secret_key="sk", bucket="rat", use_ssl=False
    )


def _nessie():
    return NessieConfig()


# ── SQL flow ─────────────────────────────────────────────────────────────


class TestSqlFlow:
    def test_compiles_and_executes_sql(self, mocks):
        mocks.read_s3_text.side_effect = lambda _c, key: (
            "SELECT 42 AS value" if key.endswith(".sql") else None
        )
        run = _make_run()
        execute_pipeline(run, _s3(), _nessie())

        assert run.status == RunStatus.SUCCESS
        mocks.engine.execute.assert_called_once()
        request = mocks.engine.execute.call_args[0][0]
        assert request.language == "sql"
        # The runner compiles to logical refs; the user's SELECT survives.
        assert "42" in request.source

    def test_engine_failure_marks_run_failed(self, mocks):
        mocks.engine.execute.side_effect = RuntimeError("engine Execute failed: boom")
        run = _make_run()
        execute_pipeline(run, _s3(), _nessie())
        assert run.status == RunStatus.FAILED
        assert "boom" in (run.error or "")

    def test_sets_success_on_completion(self, mocks):
        run = _make_run()
        execute_pipeline(run, _s3(), _nessie())
        assert run.status == RunStatus.SUCCESS

    def test_records_duration_ms(self, mocks):
        run = _make_run()
        execute_pipeline(run, _s3(), _nessie())
        assert run.duration_ms >= 0

    def test_zero_rows_is_success(self, mocks):
        mocks.engine.execute.return_value = SimpleNamespace(
            rows_written=0, error="", output_schema=b""
        )
        run = _make_run()
        execute_pipeline(run, _s3(), _nessie())
        assert run.status == RunStatus.SUCCESS
        assert run.rows_written == 0

    def test_respects_cancellation(self, mocks):
        run = _make_run()
        run.cancel_event.set()
        execute_pipeline(run, _s3(), _nessie())
        assert run.status == RunStatus.CANCELLED
        mocks.engine.execute.assert_not_called()

    def test_missing_pipeline_file_fails(self, mocks):
        mocks.read_s3_text.side_effect = lambda _c, _k: None
        run = _make_run()
        execute_pipeline(run, _s3(), _nessie())
        assert run.status == RunStatus.FAILED


# ── Python flow ──────────────────────────────────────────────────────────


class TestPythonFlow:
    def test_detects_python_and_sends_to_engine(self, mocks):
        mocks.read_s3_text.side_effect = lambda _c, key: (
            "import pyarrow as pa\nresult = pa.table({'x':[1]})\n" if key.endswith(".py") else None
        )
        run = _make_run()
        execute_pipeline(run, _s3(), _nessie())
        assert run.status == RunStatus.SUCCESS
        request = mocks.engine.execute.call_args[0][0]
        assert request.language == "python"


# ── Branch lifecycle via catalog/v1 ──────────────────────────────────────


class TestBranchLifecycle:
    def test_create_merge_delete_on_success(self, mocks):
        run = _make_run()
        execute_pipeline(run, _s3(), _nessie())
        assert run.status == RunStatus.SUCCESS
        mocks.catalog.create_branch.assert_called_once()
        mocks.catalog.merge_branch.assert_called_once()
        mocks.catalog.delete_branch.assert_called_once()

    def test_non_branching_catalog_skips_branch_ops(self, mocks):
        mocks.plane.supports_branching = False  # type: ignore[misc]
        mocks.catalog.describe.return_value = _ok_describe_catalog(branching=False)
        run = _make_run()
        execute_pipeline(run, _s3(), _nessie())
        assert run.status == RunStatus.SUCCESS
        mocks.catalog.create_branch.assert_not_called()
        mocks.catalog.merge_branch.assert_not_called()
        mocks.catalog.delete_branch.assert_not_called()

    def test_quality_failure_discards_branch_no_merge(self, mocks):
        mocks.run_quality_tests.return_value = [
            QualityTestResult(
                test_name="t",
                test_file="t.sql",
                severity="error",
                status="fail",
                row_count=1,
                message="violation",
            ),
        ]
        run = _make_run()
        execute_pipeline(run, _s3(), _nessie())
        assert run.status == RunStatus.FAILED
        mocks.catalog.merge_branch.assert_not_called()
        mocks.catalog.delete_branch.assert_called_once()


# ── Branch-create failure aborts ─────────────────────────────────────────


class TestBranchCreateFailureAborts:
    def test_create_branch_failure_marks_failed(self, mocks):
        mocks.catalog.create_branch.side_effect = RuntimeError("create_branch HTTP 500")
        run = _make_run()
        execute_pipeline(run, _s3(), _nessie())
        assert run.status == RunStatus.FAILED
        mocks.engine.execute.assert_not_called()
        mocks.catalog.merge_branch.assert_not_called()

    def test_failure_skips_archive(self, mocks):
        mocks.catalog.create_branch.side_effect = RuntimeError("nope")
        mocks.read_s3_text.side_effect = lambda _c, key: (
            "-- @archive_landing_zones: true\nSELECT * FROM landing.foo"
            if key.endswith(".sql")
            else None
        )
        with patch(f"{_EXEC}._archive_landing_zones") as archive:
            run = _make_run()
            execute_pipeline(run, _s3(), _nessie())
        assert run.status == RunStatus.FAILED
        archive.assert_not_called()


# ── Phase 5: merge failure ───────────────────────────────────────────────


class TestPhase5MergeFailure:
    def test_merge_failure_retains_branch(self, mocks):
        mocks.catalog.merge_branch.side_effect = RuntimeError("merge HTTP 4xx")
        run = _make_run()
        execute_pipeline(run, _s3(), _nessie())
        assert run.status == RunStatus.FAILED
        # Branch should NOT be deleted when retained for recovery.
        mocks.catalog.delete_branch.assert_not_called()
        assert "merge" in (run.error or "").lower()


# ── Config merge ─────────────────────────────────────────────────────────


class TestConfigMerge:
    def test_annotation_overrides_config_yaml(self, mocks):
        mocks.read_s3_text.side_effect = lambda _c, key: (
            "-- @merge_strategy: incremental\n-- @unique_key: [id]\nSELECT 1 AS id"
            if key.endswith(".sql")
            else ("merge_strategy: full_refresh\n" if key.endswith("config.yaml") else None)
        )
        run = _make_run()
        execute_pipeline(run, _s3(), _nessie())
        assert run.status == RunStatus.SUCCESS
        request = mocks.engine.execute.call_args[0][0]
        assert request.strategy == "incremental"


# ── Strategy dispatch — passed through to engine ─────────────────────────


class TestStrategyDispatch:
    @pytest.mark.parametrize(
        "strategy", ["full_refresh", "incremental", "append_only", "delete_insert", "snapshot"]
    )
    def test_strategy_passed_through(self, mocks, strategy):
        mocks.read_s3_text.side_effect = lambda _c, key: (
            f"-- @merge_strategy: {strategy}\n-- @unique_key: [id]\nSELECT 1 AS id"
            if key.endswith(".sql")
            else None
        )
        run = _make_run()
        execute_pipeline(run, _s3(), _nessie())
        assert run.status == RunStatus.SUCCESS
        request = mocks.engine.execute.call_args[0][0]
        assert request.strategy == strategy


# ── Archive landing zones ────────────────────────────────────────────────


class TestArchiveLandingZones:
    def test_archives_after_success(self, mocks):
        mocks.read_s3_text.side_effect = lambda _c, key: (
            "-- @archive_landing_zones: true\nSELECT * FROM landing.events"
            if key.endswith(".sql")
            else None
        )
        with patch(f"{_EXEC}._archive_landing_zones", return_value=["myns/events"]) as archive:
            run = _make_run()
            execute_pipeline(run, _s3(), _nessie())
        assert run.status == RunStatus.SUCCESS
        archive.assert_called_once()

    def test_skips_archive_when_annotation_absent(self, mocks):
        mocks.read_s3_text.side_effect = lambda _c, key: (
            "SELECT 1" if key.endswith(".sql") else None
        )
        with patch(f"{_EXEC}._archive_landing_zones") as archive:
            run = _make_run()
            execute_pipeline(run, _s3(), _nessie())
        assert run.status == RunStatus.SUCCESS
        archive.assert_not_called()


# ── Versioned reads (published_versions) ─────────────────────────────────


class TestVersionedReads:
    def test_executor_reads_published_version(self, mocks):
        with patch(f"{_EXEC}.read_s3_text_version", return_value="SELECT 1") as rsv:
            run = _make_run()
            published = {"myns/pipelines/silver/orders/pipeline.sql": "vid-abc"}
            execute_pipeline(run, _s3(), _nessie(), published_versions=published)
        assert run.status == RunStatus.SUCCESS
        rsv.assert_any_call(_s3(), "myns/pipelines/silver/orders/pipeline.sql", "vid-abc")

    def test_executor_falls_back_to_head_when_no_pin(self, mocks):
        # No pin for pipeline.sql -> falls through to read_s3_text.
        run = _make_run()
        execute_pipeline(run, _s3(), _nessie(), published_versions={})
        assert run.status == RunStatus.SUCCESS
        mocks.read_s3_text.assert_any_call(_s3(), "myns/pipelines/silver/orders/pipeline.sql")


# ── Composition validation (capability mismatch) ─────────────────────────


class TestCompositionValidation:
    def test_invalid_binding_fails_fast_no_branch(self, mocks):
        # Engine doesn't manage the plane's format -> CompositionError, no side effects.
        mocks.engine.describe.return_value = SimpleNamespace(
            formats=["delta"], languages=["sql"], capabilities=[], dialects=["duckdb"]
        )
        run = _make_run()
        execute_pipeline(run, _s3(), _nessie())
        assert run.status == RunStatus.FAILED
        assert "not a valid composition" in (run.error or "")
        mocks.catalog.create_branch.assert_not_called()
        mocks.engine.execute.assert_not_called()
