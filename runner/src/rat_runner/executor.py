"""Pipeline executor — orchestrates the engine/v1 + catalog/v1 + storage/v1 composition.

The runner is a pure orchestrator (ADR-024): it never touches table bytes. Per run:
  Phase 0: catalog/v1 create_branch (when the catalog supports branching)
  Phase 1: detect pipeline type + load merged config from S3
  Phase E: engine.Execute does the transform + write
  Phase 4: quality tests via engine.Query against the run branch
  Phase 5: catalog/v1 merge_branch (success) or delete (failure / quality fail)
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any

from common.v1 import data_plane_pb2  # type: ignore[import-untyped]
from engine.v1 import engine_pb2  # type: ignore[import-untyped]

from rat_runner.bindings import BindingConfig, DataPlane
from rat_runner.capabilities import AxisCapabilities, check_composition
from rat_runner.catalog_client import CatalogClient
from rat_runner.config import (
    CatalogConfig,
    EngineConfig,
    NessieConfig,
    S3Config,
    StorageConfig,
    list_s3_keys,
    merge_configs,
    move_s3_keys,
    parse_pipeline_config,
    read_s3_text,
    read_s3_text_version,
)
from rat_runner.descriptors import layer_enum
from rat_runner.engine_client import EngineClient
from rat_runner.json_log import clear_run_context, set_run_context
from rat_runner.log import RunLogger, run_log_extras
from rat_runner.models import PipelineConfig, QualityTestResult, RunState, RunStatus
from rat_runner.plugin_protocols import HookContext
from rat_runner.plugin_registry import PluginRegistry
from rat_runner.quality import has_error_failures, run_quality_tests
from rat_runner.storage_client import StorageClient
from rat_runner.templating import (
    compile_sql,
    extract_dependencies,
    extract_landing_zones,
    extract_metadata,
    validate_landing_zones,
)

logger = logging.getLogger(__name__)


class CancelledError(Exception):
    """Raised when a run is cancelled between phases."""


def _check_cancelled(run: RunState) -> None:
    if run.cancel_event.is_set():
        raise CancelledError("Run cancelled")


@dataclass
class _PipelineContext:
    """Carries mutable state between pipeline execution phases.

    This replaces the many local variables that were threaded through the old
    monolith function, making inter-phase data flow explicit.
    """

    run: RunState
    s3_config: S3Config
    nessie_config: NessieConfig
    log: RunLogger
    registry: PluginRegistry = field(default_factory=PluginRegistry)
    published_versions: dict[str, str] = field(default_factory=dict)

    # Set during Phase 0. The branch is created OR the run fails — there is
    # no path where a run proceeds with branch_name still empty/main.
    branch_name: str = ""

    # Set during Phase 1
    # "python", "sql", or the name of a plugin-provided pipeline type.
    pipeline_type: str = "sql"
    source: str = ""  # raw pipeline source code (.py, .sql, or plugin type)
    raw_py: str | None = None
    raw_sql: str | None = None
    config: PipelineConfig | None = None

    # Set during the engine execute phase (kept for hook payload metadata).
    table_name: str = ""
    location: str = ""
    row_count: int = 0

    # Set in Phase 5 when a merge attempt fails terminally. When True the finally
    # block MUST NOT delete the ephemeral branch — it holds data the engine wrote
    # and Phase 4 quality-tested but couldn't merge into main. An operator
    # recovers it from the retained branch + the structured ERROR log.
    retain_branch: bool = False

    # The resolved data-plane composition + the catalog/v1 client + the vended
    # StorageDescriptor, all set early in execute_pipeline.
    data_plane: DataPlane | None = None
    catalog_client: CatalogClient | None = None
    storage_descriptor: Any = None


def _archive_landing_zones(
    source: str, namespace: str, run_id: str, s3_config: S3Config, log: RunLogger
) -> list[str]:
    """Move landing zone files to _processed/{run_id}/ subfolder. Best-effort.

    Returns list of archived zone identifiers as "{namespace}/{zone}".
    """
    zones = extract_landing_zones(source)
    archived: list[str] = []
    for zone in zones:
        prefix = f"{namespace}/landing/{zone}/"
        dest_prefix = f"{namespace}/landing/{zone}/_processed/{run_id}/"
        try:
            keys = list_s3_keys(s3_config, prefix)
            # Filter out already-processed files
            keys = [k for k in keys if "/_processed/" not in k]
            if keys:
                move_s3_keys(s3_config, keys, prefix, dest_prefix)
                log.info(f"Archived {len(keys)} file(s) from landing zone '{zone}'")
                archived.append(f"{namespace}/{zone}")
            else:
                log.info(f"No files to archive in landing zone '{zone}'")
        except Exception as e:
            log.warn(f"Failed to archive landing zone '{zone}': {e}")
    return archived


def _format_quality_error(results: list[QualityTestResult]) -> str:
    """Build a descriptive error string from failed quality test results."""
    failed = [r for r in results if r.severity == "error" and r.status in ("fail", "error")]
    lines = ["Quality tests failed:"]
    for r in failed:
        label = f"  {r.test_name}"
        if r.description:
            label += f" ({r.description})"
        if r.status == "error":
            lines.append(f"{label}: errored — {r.message}")
        else:
            lines.append(f"{label}: {r.row_count} violation(s)")
        if r.sample_rows:
            for row_line in r.sample_rows.splitlines():
                lines.append(f"    {row_line}")
    return "\n".join(lines)


def _read_versioned(
    s3_config: S3Config, key: str, published_versions: dict[str, str]
) -> str | None:
    """Read from pinned version if available, else HEAD."""
    vid = published_versions.get(key)
    if vid:
        return read_s3_text_version(s3_config, key, vid)
    return read_s3_text(s3_config, key)


# ── Phase 0: Create ephemeral Nessie branch ──────────────────────────


def _phase1_detect_and_load(ctx: _PipelineContext) -> None:
    """Detect pipeline type (.py or .sql) and load merged config.

    Reads the pipeline source from S3, detects type by file extension priority
    (.py first, then .sql), merges config.yaml with source annotations, and
    validates landing zones.
    """
    _check_cancelled(ctx.run)
    ns, layer, name = ctx.run.namespace, ctx.run.layer, ctx.run.pipeline_name
    base_prefix = f"{ns}/pipelines/{layer}/{name}"

    py_key = f"{base_prefix}/pipeline.py"
    sql_key = f"{base_prefix}/pipeline.sql"
    config_key = f"{base_prefix}/config.yaml"

    pv = ctx.published_versions

    ctx.raw_py = _read_versioned(ctx.s3_config, py_key, pv)
    ctx.raw_sql = _read_versioned(ctx.s3_config, sql_key, pv) if ctx.raw_py is None else None

    if ctx.raw_py is not None:
        ctx.pipeline_type = "python"
        source: str | None = ctx.raw_py
    elif ctx.raw_sql is not None:
        ctx.pipeline_type = "sql"
        source = ctx.raw_sql
    else:
        # No core pipeline file — try plugin-provided pipeline types.
        # Each plugin type owns a file extension (e.g. pipeline.prql).
        source = None
        for type_name in ctx.registry.pipeline_type_names():
            plugin_type = ctx.registry.get_pipeline_type(type_name)
            if plugin_type is None:
                continue
            ext_key = f"{base_prefix}/pipeline.{plugin_type.file_extension}"
            plugin_src = _read_versioned(ctx.s3_config, ext_key, pv)
            if plugin_src is not None:
                ctx.pipeline_type = type_name
                source = plugin_src
                break
        if source is None:
            raise FileNotFoundError(
                f"Pipeline not found: no pipeline.py/.sql and no registered "
                f"plugin pipeline-type file under {base_prefix}/"
            )

    ctx.log.info(f"Detected {ctx.pipeline_type} pipeline")

    # Load config: merge config.yaml base with annotation overrides
    assert source is not None
    ctx.source = source

    annotation_meta = extract_metadata(source)
    config_yaml = _read_versioned(ctx.s3_config, config_key, pv)
    # Pass plugin-discovered strategy names so config validation accepts
    # custom merge strategies registered via runner plugins, not just built-ins.
    base_config = (
        parse_pipeline_config(config_yaml, ctx.registry.strategy_names()) if config_yaml else None
    )
    if annotation_meta or base_config:
        ctx.config = merge_configs(base_config, annotation_meta)
        if annotation_meta and base_config:
            ctx.log.info(f"Merged config.yaml + annotations: {list(annotation_meta.keys())}")
        elif annotation_meta:
            ctx.log.info(f"Loaded config from source annotations: {list(annotation_meta.keys())}")
        else:
            ctx.log.info("Loaded pipeline config from config.yaml")

    lz_warnings = validate_landing_zones(source, ns, ctx.s3_config)
    for warn in lz_warnings:
        ctx.log.warn(warn)


# ── Phase 2: Build result table ──────────────────────────────────────


def _engine_quality_runner(ctx: _PipelineContext, client: EngineClient) -> Any:
    """Quality runner for engine mode: logical-ref SQL + engine.Query on the run branch.

    The test's ref()s resolve against the run branch (which holds the freshly written
    output on top of main), so input descriptors are assembled at ctx.branch_name.
    """
    ns, layer, name = ctx.run.namespace, ctx.run.layer, ctx.run.pipeline_name

    def run_test(raw: str) -> tuple[str, Any]:
        compiled = compile_sql(
            raw, ns, layer, name, ctx.s3_config, ctx.nessie_config, logical_refs=True
        )
        inputs = []
        for dep in extract_dependencies(raw):
            d_ns, d_layer, d_name = _parse_ref(dep, ns)
            inputs.append(
                _assemble_table_descriptor(
                    ctx.catalog_client,
                    ctx.storage_descriptor,
                    d_ns,
                    layer_enum(d_layer),
                    d_name,
                    ctx.branch_name,
                )
            )
        request = engine_pb2.QueryRequest(language="sql", source=compiled, inputs=inputs, limit=0)
        return compiled, lambda: client.query(request)

    return run_test


def _phase4_quality_tests(ctx: _PipelineContext) -> list[QualityTestResult]:
    """Run quality tests against the freshly written table via engine.Query."""
    _check_cancelled(ctx.run)
    assert ctx.data_plane is not None
    client = EngineClient(ctx.data_plane.engine_addr)
    try:
        quality_results = run_quality_tests(
            ctx.run,
            _engine_quality_runner(ctx, client),
            ctx.s3_config,
            ctx.log,
            published_versions=ctx.published_versions or None,
        )
    finally:
        client.close()
    ctx.run.quality_results = quality_results
    return quality_results


# ── Phase 5: Branch resolution ───────────────────────────────────────


def _post_success(ctx: _PipelineContext) -> None:
    """Mark success, archive landing zones (if requested).

    Iceberg maintenance (snapshot expiry, orphan removal) used to run here; it now
    belongs in the engine plugin or a dedicated compaction plugin, not the runner.
    """
    ctx.run.status = RunStatus.SUCCESS
    ctx.log.info("Pipeline completed successfully")

    if ctx.config is not None and ctx.config.archive_landing_zones:
        ns = ctx.run.namespace
        ctx.run.archived_zones = _archive_landing_zones(
            ctx.source, ns, ctx.run.run_id, ctx.s3_config, ctx.log
        )


# ── Public entry point ───────────────────────────────────────────────


def _build_hook_context(ctx: _PipelineContext) -> HookContext:
    """Build a HookContext from the current pipeline context."""
    return HookContext(
        namespace=ctx.run.namespace,
        layer=ctx.run.layer,
        name=ctx.run.pipeline_name,
        run_id=ctx.run.run_id,
        config=ctx.config,
        logger=ctx.log,
        branch=ctx.branch_name,
        extra={
            "rows_written": ctx.run.rows_written,
            "row_count": ctx.row_count,
        },
    )


def _assemble_table_descriptor(
    catalog_client: CatalogClient,
    storage_descriptor: Any,
    namespace: str,
    layer: int,
    name: str,
    branch: str,
) -> Any:
    """Build a TableDescriptor: catalog part vended by catalog/v1 GetTable, storage by storage/v1.

    This replaces the runner-local build_table_descriptor — the catalog axis now
    owns identifier + protocol + connection details (ADR-024 control plane).
    """
    ref = data_plane_pb2.TableRef(namespace=namespace, layer=layer, name=name)
    info = catalog_client.get_table(ref, branch)
    return data_plane_pb2.TableDescriptor(
        ref=ref,
        format=info.format,
        identifier=info.identifier,
        catalog=info.catalog,
        storage=storage_descriptor,
    )


def _resolve_branch_via_catalog(
    ctx: _PipelineContext, quality_results: list[QualityTestResult]
) -> None:
    """Engine-mode Phase 5: merge (or discard) the run branch via catalog/v1.

    The branch is deleted in execute_pipeline's finally on quality failure; on
    merge failure we retain it (the catalog service has no audit hook yet, so
    the retained branch + the ERROR log are the recovery signal).
    """
    assert ctx.catalog_client is not None
    if has_error_failures(quality_results):
        ctx.log.error("Quality tests failed — discarding branch (no data on main)")
        ctx.run.status = RunStatus.FAILED
        ctx.run.error = _format_quality_error(quality_results)
        return

    ctx.log.info(f"Merging branch '{ctx.branch_name}' to main via catalog/v1")
    try:
        ctx.catalog_client.merge_branch(ctx.branch_name, "main")
        ctx.log.info("Branch merged to main")
    except Exception as e:
        ctx.retain_branch = True
        msg = f"branch merge failed via catalog/v1: {e} — branch {ctx.branch_name} retained"
        logger.error(
            "Phase 5 merge failed (catalog/v1) — branch retained",
            extra={"branch": ctx.branch_name, "run": ctx.run.run_id, "merge_lost_data": True},
            exc_info=True,
        )
        ctx.log.error(msg)
        ctx.run.status = RunStatus.FAILED
        ctx.run.error = msg
        return

    _post_success(ctx)


def _parse_ref(table_ref: str, default_ns: str) -> tuple[str, str, str]:
    """Split a ref into (namespace, layer, name); 2-part refs take the default namespace."""
    parts = table_ref.split(".", 2)
    if len(parts) == 2:
        return default_ns, parts[0], parts[1]
    if len(parts) == 3:
        return parts[0], parts[1], parts[2]
    raise ValueError(f"Invalid ref '{table_ref}': expected 'layer.name' or 'ns.layer.name'")


def _build_engine_options(config: PipelineConfig | None) -> dict[str, str]:
    """Project the strategy-relevant PipelineConfig fields into the engine options map."""
    opts: dict[str, str] = {}
    if config is None:
        return opts
    if config.unique_key:
        opts["unique_key"] = ",".join(config.unique_key)
    if config.partition_column:
        opts["partition_column"] = config.partition_column
    if config.scd_valid_from:
        opts["scd_valid_from"] = config.scd_valid_from
    if config.scd_valid_to:
        opts["scd_valid_to"] = config.scd_valid_to
    if config.partition_by:
        opts["partition_by"] = ",".join(f"{e.column}:{e.transform}" for e in config.partition_by)
    return opts


def _resolve_data_plane(ctx: _PipelineContext) -> DataPlane:
    """Resolve the data-plane binding for this run (pipeline > layer > namespace > default)."""
    binding = BindingConfig.load(
        os.environ.get("RAT_BINDINGS"),
        engine_addr=EngineConfig.from_env().addr,
        catalog_addr=CatalogConfig.from_env().addr,
        storage_addr=StorageConfig.from_env().addr,
    )
    return binding.resolve(ctx.run.namespace, ctx.run.layer, ctx.run.pipeline_name)


def _validate_composition(ctx: _PipelineContext) -> None:
    """Fail-fast (ADR-024 #9): verify the resolved data plane is a valid composition.

    Queries each axis's Describe and matches the binding against the advertised
    capabilities BEFORE any side effect (branch creation, execute). A bad binding
    raises CompositionError up-front instead of failing mid-run.
    """
    plane = ctx.data_plane
    assert plane is not None and ctx.catalog_client is not None
    engine_client = EngineClient(plane.engine_addr)
    storage_client = StorageClient(plane.storage_addr)
    try:
        engine_d = engine_client.describe()
        storage_d = storage_client.describe()
    finally:
        engine_client.close()
        storage_client.close()
    catalog_d = ctx.catalog_client.describe()
    caps = AxisCapabilities(
        engine_formats=list(engine_d.formats),
        engine_languages=list(engine_d.languages),
        catalog_formats=list(catalog_d.formats),
        catalog_capabilities=list(catalog_d.capabilities),
        storage_schemes=list(storage_d.schemes),
    )
    check_composition(plane, caps)
    ctx.log.info(
        f"Composition '{plane.name}' validated "
        f"(format={plane.format}, branching={plane.supports_branching})"
    )


def _phase_engine_execute(ctx: _PipelineContext) -> None:
    """Decoupled path (ADR-024): execute + write via engine/v1, replacing local phases 2+3.

    The runner compiles logical-ref SQL (or passes Python through) and assembles input/
    output TableDescriptors, then calls the bound engine, which reads inputs, transforms,
    applies the strategy recipe, and commits to the run branch — the runner never touches
    table bytes. Branch ops (phase 0/5) + quality (phase 4) still run locally for now.
    """
    _check_cancelled(ctx.run)
    ns, layer, name = ctx.run.namespace, ctx.run.layer, ctx.run.pipeline_name
    ctx.table_name = f"{ns}.{layer}.{name}"
    ctx.location = f"s3://{ctx.s3_config.bucket}/{ns}/{layer}/{name}/"

    plane = ctx.data_plane if ctx.data_plane is not None else _resolve_data_plane(ctx)
    ctx.log.info(
        f"Engine mode: data_plane '{plane.name}' engine={plane.engine_addr} format={plane.format}"
    )

    # Vend the StorageDescriptor from the storage/v1 service (it owns the S3 creds).
    # Stash it so phase-4 quality can assemble its own input descriptors without
    # a second vend.
    storage_client = StorageClient(plane.storage_addr)
    try:
        storage_descriptor = storage_client.vend_descriptor(location=ctx.s3_config.bucket)
    finally:
        storage_client.close()
    ctx.storage_descriptor = storage_descriptor

    if ctx.pipeline_type == "sql":
        language = "sql"
        helpers = dict(ctx.registry.get_helpers())
        source = compile_sql(
            ctx.raw_sql,  # type: ignore[arg-type]
            ns,
            layer,
            name,
            ctx.s3_config,
            ctx.nessie_config,
            config=ctx.config,
            plugin_helpers=helpers or None,
            logical_refs=True,
        )
        raw_for_deps = ctx.raw_sql or ""
    elif ctx.pipeline_type == "python":
        language = "python"
        source = ctx.raw_py or ""
        raw_for_deps = source
    else:
        raise RuntimeError(
            f"Engine mode supports 'sql'/'python' pipelines; got '{ctx.pipeline_type}'"
        )

    assert ctx.catalog_client is not None, "engine mode must set ctx.catalog_client before execute"
    inputs: list[Any] = []
    for dep in extract_dependencies(raw_for_deps):
        dep_ns, dep_layer, dep_name = _parse_ref(dep, ns)
        inputs.append(
            _assemble_table_descriptor(
                ctx.catalog_client,
                storage_descriptor,
                dep_ns,
                layer_enum(dep_layer),
                dep_name,
                "main",
            )
        )
    output = _assemble_table_descriptor(
        ctx.catalog_client,
        storage_descriptor,
        ns,
        layer_enum(layer),
        name,
        ctx.branch_name,
    )

    strategy = str(ctx.config.merge_strategy) if ctx.config else "full_refresh"
    request = engine_pb2.ExecuteRequest(
        run_id=ctx.run.run_id,
        language=language,
        dialect="duckdb",
        source=source,
        inputs=inputs,
        output=output,
        strategy=strategy,
        options=_build_engine_options(ctx.config),
    )

    ctx.log.info(f"Executing via engine ({language}, strategy={strategy}, {len(inputs)} inputs)")
    client = EngineClient(plane.engine_addr)
    try:
        result = client.execute(request)
    finally:
        client.close()

    ctx.run.rows_written = result.rows_written
    ctx.row_count = result.rows_written
    ctx.log.info(f"Engine wrote {result.rows_written} rows")


def execute_pipeline(
    run: RunState,
    s3_config: S3Config,
    nessie_config: NessieConfig,
    published_versions: dict[str, str] | None = None,
) -> None:
    """Execute a pipeline run. Intended to run in a worker thread.

    Updates RunState in-place with status, rows_written, duration_ms, and error.

    Pipeline paths (S3):
        SQL:    {namespace}/pipelines/{layer}/{name}/pipeline.sql
        Python: {namespace}/pipelines/{layer}/{name}/pipeline.py
        Config: {namespace}/pipelines/{layer}/{name}/config.yaml
    Iceberg table: {namespace}.{layer}.{name}
    Iceberg loc:   s3://{bucket}/{namespace}/{layer}/{name}/
    """
    log = RunLogger(run)
    start = time.monotonic()
    run.status = RunStatus.RUNNING

    # Bind the run extras into the thread-local context so subsystem modules
    # (iceberg, nessie, maintenance, plugin_registry, state_dir) whose
    # module-level loggers don't have a RunState in scope still emit lines
    # tagged with run_id/request_id/namespace/layer/pipeline_name. We set
    # the context INSIDE the worker thread (not at submit time) because
    # ThreadPoolExecutor.submit does not copy the dispatcher's contextvars
    # to the new thread unless wrapped with copy_context().run, and doing
    # it here keeps every code path consistent.
    _context_token = set_run_context(run_log_extras(run))

    # Per-run env overrides: apply to S3Config instead of os.environ
    # (os.environ is process-global and thread-unsafe for concurrent runs)
    if run.env:
        s3_config = s3_config.with_overrides(run.env)

    # Discover plugins for this run (fresh scan each run).
    registry = PluginRegistry()
    registry.discover()

    ctx = _PipelineContext(
        run=run,
        s3_config=s3_config,
        nessie_config=nessie_config,
        log=log,
        registry=registry,
        published_versions=published_versions or {},
    )

    try:
        # ADR-024 end state: the runner is a pure orchestrator. Resolve the data
        # plane + catalog client + validate the composition unconditionally.
        ctx.data_plane = _resolve_data_plane(ctx)
        branching = ctx.data_plane.supports_branching
        ctx.catalog_client = CatalogClient(ctx.data_plane.catalog_addr)
        _validate_composition(ctx)

        if branching:
            ctx.branch_name = f"run-{ctx.run.run_id}"
            ctx.log.info(f"Creating ephemeral branch '{ctx.branch_name}' via catalog/v1")
            ctx.catalog_client.create_branch(ctx.branch_name, "main")
            ctx.run.branch = ctx.branch_name
            ctx.log.info(f"Branch '{ctx.branch_name}' created")
        else:
            ctx.branch_name = "main"
            ctx.log.info(
                f"Catalog format '{ctx.data_plane.format}' has no branching — "
                "writing directly (no ephemeral branch)"
            )
        _phase1_detect_and_load(ctx)

        hook_ctx = _build_hook_context(ctx)
        registry.dispatch_hooks("pre_execute", hook_ctx)

        _phase_engine_execute(ctx)

        hook_ctx = _build_hook_context(ctx)
        registry.dispatch_hooks("post_write", hook_ctx)

        hook_ctx = _build_hook_context(ctx)
        registry.dispatch_hooks("pre_quality", hook_ctx)

        quality_results = _phase4_quality_tests(ctx)

        hook_ctx = _build_hook_context(ctx)
        registry.dispatch_hooks("post_quality", hook_ctx)

        if branching:
            _resolve_branch_via_catalog(ctx, quality_results)
        elif has_error_failures(quality_results):
            ctx.run.status = RunStatus.FAILED
            ctx.run.error = _format_quality_error(quality_results)
        else:
            ctx.run.status = RunStatus.SUCCESS
            ctx.log.info("Pipeline completed successfully")

        # Dispatch post_execute hooks
        hook_ctx = _build_hook_context(ctx)
        registry.dispatch_hooks("post_execute", hook_ctx)

    except CancelledError:
        run.status = RunStatus.CANCELLED
        run.error = "Run cancelled by user"
        log.warn("Pipeline cancelled")

    except Exception as e:
        run.status = RunStatus.FAILED
        run.error = str(e)
        log.error(f"Pipeline failed: {e}")

    finally:
        # Cleanup: delete ephemeral branch if Phase 0 created one.
        # We use run.branch (set after create_branch succeeds) as the
        # signal — guarantees we never try to delete "main" or an
        # uninitialised branch name.
        #
        # EXCEPTION: when Phase 5 set retain_branch=True the branch holds
        # data that Phase 3 wrote and Phase 4 quality-tested but couldn't
        # merge into main. Deleting it would erase recoverable work, so we
        # leave it for the operator (see `failed_merges` audit row).
        if run.branch and run.branch != "main" and not ctx.retain_branch:
            try:
                assert ctx.catalog_client is not None
                ctx.catalog_client.delete_branch(run.branch)
            except Exception:
                logger.warning(
                    "Failed to delete ephemeral branch '%s'",
                    run.branch,
                    exc_info=True,
                    extra=run_log_extras(run),
                )
        elif ctx.retain_branch:
            logger.error(
                "Branch '%s' retained — Phase 5 merge failed; recover via failed_merges audit",
                run.branch,
                extra={**run_log_extras(run), "merge_lost_data": True},
            )

        if ctx.catalog_client is not None:
            ctx.catalog_client.close()
        elapsed_ms = int((time.monotonic() - start) * 1000)
        run.duration_ms = elapsed_ms
        log.info(f"Duration: {elapsed_ms}ms")

        # Restore the prior context binding — important when the same worker
        # thread is recycled for a different run by ThreadPoolExecutor, so the
        # next run doesn't inherit the previous run's extras until it binds
        # its own.
        clear_run_context(_context_token)
