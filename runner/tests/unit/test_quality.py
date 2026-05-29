"""Tests for quality — quality test discovery and execution."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pyarrow as pa

from rat_runner.config import DuckDBConfig, NessieConfig, S3Config
from rat_runner.engine import QueryTimeoutError
from rat_runner.log import RunLogger
from rat_runner.models import RunState
from rat_runner.quality import (
    _format_sample_rows,
    _parse_description,
    _parse_remediation,
    _parse_severity,
    _parse_tags,
    _truncate_cell,
    discover_quality_tests,
    discover_quality_tests_versioned,
    has_error_failures,
    run_quality_test,
    run_quality_tests,
)


def _make_run(**kwargs) -> RunState:
    defaults = {
        "run_id": "r1",
        "namespace": "myns",
        "layer": "silver",
        "pipeline_name": "orders",
        "trigger": "manual",
    }
    defaults.update(kwargs)
    return RunState(**defaults)


def _make_engine(quality_test_timeout_seconds: int = 60) -> MagicMock:
    engine = MagicMock()
    # Some tests still read engine._duckdb_config for legacy assertions.
    engine._duckdb_config = DuckDBConfig(quality_test_timeout_seconds=quality_test_timeout_seconds)
    return engine


def _rt_from(engine: MagicMock, compiled: str = "SELECT * FROM iceberg_scan('s3://t/meta.json')"):
    """Adapt an engine mock to the run_test(raw) -> (compiled_sql, result) callable.

    Preserves each test's engine.query_arrow return_value/side_effect; quality.py no
    longer compiles (the executor's per-mode closure does), so the compiled SQL is a
    fixed stand-in here.
    """

    def _fn(raw: str) -> tuple[str, object]:
        return compiled, lambda: engine.query_arrow(raw)

    return _fn


class TestParseSeverity:
    def test_parses_error(self):
        sql = "-- @severity: error\nSELECT 1"
        assert _parse_severity(sql) == "error"

    def test_parses_warn(self):
        sql = "-- @severity: warn\nSELECT 1"
        assert _parse_severity(sql) == "warn"

    def test_defaults_to_error(self):
        sql = "SELECT 1"
        assert _parse_severity(sql) == "error"

    def test_invalid_severity_defaults_to_error(self):
        sql = "-- @severity: critical\nSELECT 1"
        assert _parse_severity(sql) == "error"

    def test_warning_alias_for_warn(self):
        sql = "-- @severity: warning\nSELECT 1"
        assert _parse_severity(sql) == "warn"

    def test_annotation_after_sql(self):
        sql = "SELECT 1\n-- @severity: warn"
        assert _parse_severity(sql) == "warn"


class TestDiscoverQualityTests:
    @patch("rat_runner.quality.list_s3_keys")
    def test_discovers_sql_files(self, mock_list: MagicMock, s3_config: S3Config):
        mock_list.return_value = [
            "myns/pipelines/silver/orders/tests/quality/not_null_id.sql",
            "myns/pipelines/silver/orders/tests/quality/unique_key.sql",
        ]

        keys = discover_quality_tests(s3_config, "myns", "silver", "orders")

        assert len(keys) == 2
        mock_list.assert_called_once_with(
            s3_config, "myns/pipelines/silver/orders/tests/quality/", suffix=".sql"
        )

    @patch("rat_runner.quality.list_s3_keys")
    def test_empty_when_no_tests(self, mock_list: MagicMock, s3_config: S3Config):
        mock_list.return_value = []

        keys = discover_quality_tests(s3_config, "myns", "silver", "orders")

        assert keys == []


class TestRunQualityTest:
    def test_pass_on_zero_rows(self, s3_config: S3Config, nessie_config: NessieConfig):
        engine = _make_engine()
        engine.query_arrow.return_value = pa.table({"x": pa.array([], type=pa.int64())})
        run = _make_run()
        log = RunLogger(run)

        result = run_quality_test(
            "SELECT 1 WHERE false",
            "myns/pipelines/silver/orders/tests/quality/not_null.sql",
            _rt_from(engine),
            log,
        )

        assert result.status == "pass"
        assert result.row_count == 0
        assert result.test_name == "not_null"

    def test_fail_on_rows_found(self, s3_config: S3Config, nessie_config: NessieConfig):
        engine = _make_engine()
        engine.query_arrow.return_value = pa.table({"id": [1, 2, 3]})
        run = _make_run()
        log = RunLogger(run)

        result = run_quality_test(
            "SELECT id FROM orders WHERE id IS NULL",
            "myns/pipelines/silver/orders/tests/quality/not_null.sql",
            _rt_from(engine),
            log,
        )

        assert result.status == "fail"
        assert result.row_count == 3
        assert "violation" in result.message.lower()

    def test_error_on_exception(self, s3_config: S3Config, nessie_config: NessieConfig):
        engine = _make_engine()
        engine.query_arrow.side_effect = Exception("DuckDB crash")
        run = _make_run()
        log = RunLogger(run)

        result = run_quality_test(
            "INVALID SQL",
            "myns/pipelines/silver/orders/tests/quality/bad.sql",
            _rt_from(engine),
            log,
        )

        assert result.status == "error"
        assert "DuckDB crash" in result.message

    def test_jinja_compilation(self, s3_config: S3Config, nessie_config: NessieConfig):
        """Quality test SQL should go through Jinja compilation."""
        engine = _make_engine()
        engine.query_arrow.return_value = pa.table({"x": pa.array([], type=pa.int64())})
        run = _make_run()
        log = RunLogger(run)

        sql = "SELECT * FROM {{ ref('bronze.events') }} WHERE id IS NULL"
        result = run_quality_test(
            sql,
            "myns/pipelines/silver/orders/tests/quality/ref_test.sql",
            _rt_from(engine),
            log,
        )

        assert result.status == "pass"
        # quality.py delegates compile+execute to the injected run_test; here we
        # just confirm the raw SQL flows through to it. (ref()->iceberg_scan
        # compilation is the executor closure's job now, covered by templating tests.)
        assert engine.query_arrow.call_args[0][0] == sql


class TestDiscoverQualityTestsVersioned:
    def test_filters_quality_test_keys(self):
        published_versions = {
            "myns/pipelines/silver/orders/pipeline.sql": "v1",
            "myns/pipelines/silver/orders/config.yaml": "v2",
            "myns/pipelines/silver/orders/tests/quality/not_null.sql": "v3",
            "myns/pipelines/silver/orders/tests/quality/unique_key.sql": "v4",
        }
        keys = discover_quality_tests_versioned(published_versions, "myns", "silver", "orders")
        assert keys == [
            "myns/pipelines/silver/orders/tests/quality/not_null.sql",
            "myns/pipelines/silver/orders/tests/quality/unique_key.sql",
        ]

    def test_returns_empty_when_no_tests_in_versions(self):
        published_versions = {
            "myns/pipelines/silver/orders/pipeline.sql": "v1",
        }
        keys = discover_quality_tests_versioned(published_versions, "myns", "silver", "orders")
        assert keys == []

    def test_ignores_other_pipeline_tests(self):
        published_versions = {
            "myns/pipelines/silver/orders/tests/quality/test1.sql": "v1",
            "myns/pipelines/silver/users/tests/quality/test2.sql": "v2",
        }
        keys = discover_quality_tests_versioned(published_versions, "myns", "silver", "orders")
        assert keys == ["myns/pipelines/silver/orders/tests/quality/test1.sql"]

    def test_returns_sorted_keys(self):
        published_versions = {
            "myns/pipelines/silver/orders/tests/quality/z_test.sql": "v1",
            "myns/pipelines/silver/orders/tests/quality/a_test.sql": "v2",
            "myns/pipelines/silver/orders/tests/quality/m_test.sql": "v3",
        }
        keys = discover_quality_tests_versioned(published_versions, "myns", "silver", "orders")
        assert keys == [
            "myns/pipelines/silver/orders/tests/quality/a_test.sql",
            "myns/pipelines/silver/orders/tests/quality/m_test.sql",
            "myns/pipelines/silver/orders/tests/quality/z_test.sql",
        ]


class TestRunQualityTests:
    @patch("rat_runner.quality.read_s3_text_version")
    def test_runs_all_discovered_tests(
        self,
        mock_read_version: MagicMock,
        s3_config: S3Config,
        nessie_config: NessieConfig,
    ):
        published_versions = {
            "myns/pipelines/silver/orders/tests/quality/test1.sql": "vid1",
            "myns/pipelines/silver/orders/tests/quality/test2.sql": "vid2",
        }
        mock_read_version.return_value = "SELECT 1 WHERE false"

        engine = _make_engine()
        engine.query_arrow.return_value = pa.table({"x": pa.array([], type=pa.int64())})

        run = _make_run()
        log = RunLogger(run)

        results = run_quality_tests(
            run,
            _rt_from(engine),
            s3_config,
            log,
            published_versions=published_versions,
        )

        assert len(results) == 2
        assert all(r.status == "pass" for r in results)

    def test_empty_when_no_published_versions(
        self,
        s3_config: S3Config,
        nessie_config: NessieConfig,
    ):
        engine = _make_engine()
        run = _make_run()
        log = RunLogger(run)

        results = run_quality_tests(
            run,
            _rt_from(engine),
            s3_config,
            log,
            published_versions=None,
        )

        assert results == []


class TestRunQualityTestsVersioned:
    @patch("rat_runner.quality.read_s3_text_version")
    def test_reads_from_published_versions(
        self,
        mock_read_version: MagicMock,
        s3_config: S3Config,
        nessie_config: NessieConfig,
    ):
        published_versions = {
            "myns/pipelines/silver/orders/tests/quality/test1.sql": "vid-abc",
        }
        mock_read_version.return_value = "SELECT 1 WHERE false"

        engine = _make_engine()
        engine.query_arrow.return_value = pa.table({"x": pa.array([], type=pa.int64())})

        run = _make_run()
        log = RunLogger(run)

        results = run_quality_tests(
            run,
            _rt_from(engine),
            s3_config,
            log,
            published_versions=published_versions,
        )

        assert len(results) == 1
        mock_read_version.assert_called_once_with(
            s3_config,
            "myns/pipelines/silver/orders/tests/quality/test1.sql",
            "vid-abc",
        )

    def test_skips_when_no_published_versions(
        self,
        s3_config: S3Config,
        nessie_config: NessieConfig,
    ):
        engine = _make_engine()
        run = _make_run()
        log = RunLogger(run)

        results = run_quality_tests(
            run,
            _rt_from(engine),
            s3_config,
            log,
            published_versions=None,
        )

        assert results == []

    def test_empty_published_versions_runs_no_tests(
        self,
        s3_config: S3Config,
        nessie_config: NessieConfig,
    ):
        engine = _make_engine()
        run = _make_run()
        log = RunLogger(run)

        results = run_quality_tests(
            run,
            _rt_from(engine),
            s3_config,
            log,
            published_versions={},
        )

        assert results == []


class TestHasErrorFailures:
    def test_error_severity_fail_returns_true(self):
        from rat_runner.models import QualityTestResult

        results = [
            QualityTestResult(
                test_name="t1",
                test_file="f1",
                severity="error",
                status="fail",
                row_count=3,
            ),
        ]
        assert has_error_failures(results) is True

    def test_warn_severity_fail_returns_false(self):
        from rat_runner.models import QualityTestResult

        results = [
            QualityTestResult(
                test_name="t1",
                test_file="f1",
                severity="warn",
                status="fail",
                row_count=3,
            ),
        ]
        assert has_error_failures(results) is False

    def test_all_pass_returns_false(self):
        from rat_runner.models import QualityTestResult

        results = [
            QualityTestResult(
                test_name="t1",
                test_file="f1",
                severity="error",
                status="pass",
                row_count=0,
            ),
        ]
        assert has_error_failures(results) is False

    def test_empty_returns_false(self):
        assert has_error_failures([]) is False


class TestParseDescription:
    def test_parses_description(self):
        sql = "-- @description: Check for null primary keys\nSELECT 1"
        assert _parse_description(sql) == "Check for null primary keys"

    def test_returns_empty_when_absent(self):
        sql = "SELECT 1"
        assert _parse_description(sql) == ""

    def test_parses_with_severity(self):
        sql = "-- @severity: error\n-- @description: Must have valid IDs\nSELECT 1"
        assert _parse_description(sql) == "Must have valid IDs"

    def test_finds_annotation_after_sql(self):
        sql = "SELECT 1\n-- @description: found it"
        assert _parse_description(sql) == "found it"


class TestParseTags:
    def test_parses_comma_separated(self):
        sql = "-- @tags: completeness, accuracy\nSELECT 1"
        assert _parse_tags(sql) == ("completeness", "accuracy")

    def test_returns_empty_when_absent(self):
        sql = "SELECT 1"
        assert _parse_tags(sql) == ()

    def test_single_tag(self):
        sql = "-- @tags: uniqueness\nSELECT 1"
        assert _parse_tags(sql) == ("uniqueness",)

    def test_lowercase_normalization(self):
        sql = "-- @tags: Completeness, ACCURACY, Freshness\nSELECT 1"
        assert _parse_tags(sql) == ("completeness", "accuracy", "freshness")

    def test_trims_whitespace(self):
        sql = "-- @tags:   completeness ,  accuracy  , freshness  \nSELECT 1"
        assert _parse_tags(sql) == ("completeness", "accuracy", "freshness")

    def test_annotation_after_sql(self):
        sql = "SELECT 1\n-- @tags: validity"
        assert _parse_tags(sql) == ("validity",)


class TestParseRemediation:
    def test_parses_remediation(self):
        sql = "-- @remediation: Check source system for missing records\nSELECT 1"
        assert _parse_remediation(sql) == "Check source system for missing records"

    def test_returns_empty_when_absent(self):
        sql = "SELECT 1"
        assert _parse_remediation(sql) == ""

    def test_annotation_after_sql(self):
        sql = "SELECT 1\n-- @remediation: Re-run the pipeline"
        assert _parse_remediation(sql) == "Re-run the pipeline"

    def test_with_other_annotations(self):
        sql = "-- @severity: error\n-- @description: Check IDs\n-- @remediation: Fix upstream\nSELECT 1"
        assert _parse_remediation(sql) == "Fix upstream"


class TestTruncateCell:
    def test_short_value_unchanged(self):
        assert _truncate_cell("hello", max_len=40) == "hello"

    def test_exact_limit_unchanged(self):
        assert _truncate_cell("a" * 40, max_len=40) == "a" * 40

    def test_long_value_truncated(self):
        result = _truncate_cell("a" * 50, max_len=40)
        assert len(result) == 40
        assert result.endswith("...")

    def test_truncation_preserves_prefix(self):
        value = "john.doe@example.com is a very long email address"
        result = _truncate_cell(value, max_len=20)
        assert result == "john.doe@example...."
        assert len(result) == 20


class TestFormatSampleRows:
    def test_formats_simple_table(self):
        table = pa.table({"id": [1, 2], "name": ["alice", "bob"]})
        output = _format_sample_rows(table)
        assert "id" in output
        assert "name" in output
        assert "alice" in output
        assert "bob" in output

    def test_truncates_at_default_max_rows(self):
        table = pa.table({"x": list(range(10))})
        output = _format_sample_rows(table)
        # Default is 3 rows
        assert "... and 7 more row(s)" in output

    def test_truncates_at_custom_max_rows(self):
        table = pa.table({"x": list(range(10))})
        output = _format_sample_rows(table, max_rows=5)
        assert "... and 5 more row(s)" in output

    def test_no_truncation_message_when_within_limit(self):
        table = pa.table({"x": [1, 2]})
        output = _format_sample_rows(table, max_rows=5)
        assert "more row(s)" not in output

    def test_truncates_long_cell_values(self):
        long_email = "a" * 60
        table = pa.table({"email": [long_email]})
        output = _format_sample_rows(table, max_cell=40)
        # The full 60-char value should not appear
        assert long_email not in output
        assert "..." in output

    def test_cell_truncation_with_default_limit(self):
        long_value = "x" * 100
        table = pa.table({"val": [long_value]})
        output = _format_sample_rows(table)
        # Default max_cell is 40
        assert long_value not in output
        assert "..." in output


class TestRunQualityTestEnhanced:
    def test_fail_includes_sample_rows(self, s3_config: S3Config, nessie_config: NessieConfig):
        engine = _make_engine()
        engine.query_arrow.return_value = pa.table({"id": [1, 2], "reason": ["null", "dup"]})
        run = _make_run()
        log = RunLogger(run)

        result = run_quality_test(
            "SELECT id, reason FROM {{ this }} WHERE id IS NULL",
            "myns/pipelines/silver/orders/tests/quality/not_null.sql",
            _rt_from(engine),
            log,
        )

        assert result.status == "fail"
        assert result.sample_rows != ""
        assert "null" in result.sample_rows
        assert "dup" in result.sample_rows

    def test_pass_has_empty_sample(self, s3_config: S3Config, nessie_config: NessieConfig):
        engine = _make_engine()
        engine.query_arrow.return_value = pa.table({"x": pa.array([], type=pa.int64())})
        run = _make_run()
        log = RunLogger(run)

        result = run_quality_test(
            "SELECT 1 WHERE false",
            "myns/pipelines/silver/orders/tests/quality/ok.sql",
            _rt_from(engine),
            log,
        )

        assert result.status == "pass"
        assert result.sample_rows == ""

    def test_compiled_sql_stored(self, s3_config: S3Config, nessie_config: NessieConfig):
        engine = _make_engine()
        engine.query_arrow.return_value = pa.table({"x": pa.array([], type=pa.int64())})
        run = _make_run()
        log = RunLogger(run)

        result = run_quality_test(
            "SELECT * FROM {{ this }} WHERE id IS NULL",
            "myns/pipelines/silver/orders/tests/quality/not_null.sql",
            _rt_from(engine),
            log,
        )

        assert result.compiled_sql != ""
        assert "iceberg_scan" in result.compiled_sql

    def test_description_parsed(self, s3_config: S3Config, nessie_config: NessieConfig):
        engine = _make_engine()
        engine.query_arrow.return_value = pa.table({"x": pa.array([], type=pa.int64())})
        run = _make_run()
        log = RunLogger(run)

        sql = "-- @description: Ensure no null IDs\nSELECT 1 WHERE false"
        result = run_quality_test(
            sql,
            "myns/pipelines/silver/orders/tests/quality/not_null.sql",
            _rt_from(engine),
            log,
        )

        assert result.description == "Ensure no null IDs"

    def test_error_preserves_compiled_sql(self, s3_config: S3Config, nessie_config: NessieConfig):
        engine = _make_engine()
        engine.query_arrow.side_effect = Exception("DuckDB crash")
        run = _make_run()
        log = RunLogger(run)

        result = run_quality_test(
            "SELECT 1",
            "myns/pipelines/silver/orders/tests/quality/bad.sql",
            _rt_from(engine),
            log,
        )

        assert result.status == "error"
        assert result.compiled_sql != ""


class TestQualityTestTimeout:
    """Tests for the per-quality-test watchdog timeout.

    A runaway quality test (infinite loop, catastrophic cartesian join)
    must not hang the runner. The engine's watchdog fires conn.interrupt()
    once the QUALITY_TEST_TIMEOUT_SECS deadline passes; the resulting
    QueryTimeoutError is caught in quality.py and recorded as a failed
    test rather than propagating and crashing the whole quality suite.
    """

    def test_normal_test_passes_and_passes_timeout_through(
        self, s3_config: S3Config, nessie_config: NessieConfig
    ):
        """A quality test that completes inside its deadline passes.

        (Forwarding the per-test timeout to the executor's local DuckDB closure is
        now an executor concern, exercised in test_executor, not here.)
        """
        engine = _make_engine(quality_test_timeout_seconds=45)
        engine.query_arrow.return_value = pa.table({"x": pa.array([], type=pa.int64())})
        run = _make_run()
        log = RunLogger(run)

        result = run_quality_test(
            "SELECT 1 WHERE false",
            "myns/pipelines/silver/orders/tests/quality/not_null.sql",
            _rt_from(engine),
            log,
        )

        assert result.status == "pass"
        assert result.row_count == 0

    @patch("rat_runner.quality.read_s3_text_version")
    def test_timeout_records_failure_and_suite_continues(
        self,
        mock_read_version: MagicMock,
        s3_config: S3Config,
        nessie_config: NessieConfig,
    ):
        """When one test times out the runner must NOT crash — it must
        record the timeout as a failed result and keep executing the next
        quality test in the suite."""
        # discover_quality_tests_versioned() returns keys sorted
        # alphabetically, so prefix the runaway test with 'a_' to guarantee
        # it runs first regardless of dict iteration order.
        published_versions = {
            "myns/pipelines/silver/orders/tests/quality/a_runaway.sql": "vid1",
            "myns/pipelines/silver/orders/tests/quality/b_quick.sql": "vid2",
        }
        mock_read_version.return_value = "SELECT 1 WHERE false"

        engine = _make_engine(quality_test_timeout_seconds=30)
        # First call hits the watchdog timeout; second call completes
        # normally — proves the loop continues past a timed-out test.
        engine.query_arrow.side_effect = [
            QueryTimeoutError("query exceeded 30s timeout"),
            pa.table({"x": pa.array([], type=pa.int64())}),
        ]

        run = _make_run()
        log = RunLogger(run)

        results = run_quality_tests(
            run,
            _rt_from(engine),
            s3_config,
            log,
            published_versions=published_versions,
        )

        assert len(results) == 2

        timed_out = results[0]
        assert timed_out.test_name == "a_runaway"
        assert timed_out.status == "fail"
        assert "30s" in timed_out.message
        assert "timeout" in timed_out.message.lower()

        # The second test ran to completion despite the first timing out.
        survived = results[1]
        assert survived.test_name == "b_quick"
        assert survived.status == "pass"
