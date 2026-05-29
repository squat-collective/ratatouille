"""Pipeline executor — orchestrates the full pipeline lifecycle.

Execution flow:
  Phase 0: Create ephemeral Nessie branch
  Phase 1: Detect pipeline type (.py first, then .sql), read config.yaml
  Phase 2: Build result table (SQL or Python path)
  Phase 3: Write to Iceberg (full_refresh → overwrite, incremental → merge)
  Phase 4: Quality tests on ephemeral branch
  Phase 5: Branch resolution (merge or delete based on quality results)
"""

from __future__ import annotations

import logging
import os
import time
import urllib.error
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import pyarrow as pa

from engine.v1 import engine_pb2  # type: ignore[import-untyped]

from rat_runner.bindings import BindingConfig, DataPlane
from rat_runner.config import (
    CatalogConfig,
    DuckDBConfig,
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
from rat_runner.descriptors import build_table_descriptor, layer_enum
from rat_runner.engine import DuckDBEngine
from rat_runner.engine_client import EngineClient
from rat_runner.failed_merge_audit import record_failed_merge
from rat_runner.iceberg import (
    append_iceberg,
    delete_insert_iceberg,
    merge_iceberg,
    read_watermark,
    scd2_iceberg,
    snapshot_iceberg,
    write_iceberg,
)
from rat_runner.json_log import clear_run_context, set_run_context
from rat_runner.log import RunLogger, run_log_extras
from rat_runner.maintenance import run_maintenance
from rat_runner.models import MergeStrategy, PipelineConfig, QualityTestResult, RunState, RunStatus
from rat_runner.nessie import (
    BRANCH_CREATE_MAX_RETRIES,
    MERGE_CONFLICT_MAX_RETRIES,
    _get_reference,
    create_branch,
    delete_branch,
    merge_branch,
)
from rat_runner.plugin_protocols import HookContext
from rat_runner.plugin_registry import PluginRegistry
from rat_runner.python_exec import execute_python_pipeline
from rat_runner.quality import has_error_failures, run_quality_tests
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

    # Set during Phase 2
    engine: DuckDBEngine | None = None
    table_name: str = ""
    location: str = ""
    result: pa.Table | None = None
    row_count: int = 0

    # Set in Phase 5 when a merge attempt fails terminally. When True the
    # finally block MUST NOT delete the ephemeral branch — it holds data
    # that Phase 3 wrote and Phase 4 quality-tested but couldn't merge
    # into main. An operator will recover it manually via the
    # `failed_merges` audit row.
    retain_branch: bool = False

    # Engine mode: the resolved data-plane composition (set before Phase 0).
    data_plane: DataPlane | None = None


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


def _phase0_create_branch(ctx: _PipelineContext) -> None:
    """Create an ephemeral Nessie branch for isolation.

    Branch creation is REQUIRED — failure here aborts the run. The Nessie
    client itself retries transient errors (5xx / network / timeout) up to
    BRANCH_CREATE_MAX_RETRIES times with exponential backoff; permanent
    errors (4xx, invalid name) fail immediately. If we reach the except
    clause, retries are already exhausted or the error was non-transient.

    Falling back to main was the previous behaviour but caused concurrent
    runs to race and produce duplicate rows on main, with no rollback
    possible when quality tests later failed.
    """
    _check_cancelled(ctx.run)
    ctx.branch_name = f"run-{ctx.run.run_id}"
    ctx.log.info(f"Creating ephemeral branch '{ctx.branch_name}'")
    try:
        create_branch(ctx.nessie_config, ctx.branch_name, from_branch="main")
    except Exception as e:
        # Re-raise with a clear, attributable error message. The Nessie
        # client has already exhausted retries for transient errors.
        attempts = BRANCH_CREATE_MAX_RETRIES + 1
        raise RuntimeError(f"branch creation failed after {attempts} attempts: {e}") from e
    ctx.run.branch = ctx.branch_name
    ctx.log.info(f"Branch '{ctx.branch_name}' created")


# ── Phase 1: Detect pipeline type + read config ─────────────────────


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


def _phase2_build_result(ctx: _PipelineContext) -> None:
    """Execute the pipeline (SQL or Python) and produce the result Arrow table."""
    _check_cancelled(ctx.run)
    ns, layer, name = ctx.run.namespace, ctx.run.layer, ctx.run.pipeline_name

    ctx.engine = DuckDBEngine(ctx.s3_config, DuckDBConfig.from_env())
    ctx.table_name = f"{ns}.{layer}.{name}"
    ctx.location = f"s3://{ctx.s3_config.bucket}/{ns}/{layer}/{name}/"

    if ctx.pipeline_type == "python":
        ctx.log.info("Executing Python pipeline")
        ctx.result = execute_python_pipeline(
            ctx.raw_py,  # type: ignore[arg-type]
            ctx.engine,
            ns,
            layer,
            name,
            ctx.s3_config,
            ctx.nessie_config,
            ctx.config,
        )
    elif ctx.pipeline_type == "sql":
        ctx.result = _execute_sql_path(ctx)
    else:
        ctx.result = _execute_plugin_type_path(ctx)

    ctx.row_count = len(ctx.result)
    ctx.log.info(f"Query returned {ctx.row_count} rows")


def _execute_plugin_type_path(ctx: _PipelineContext) -> pa.Table:
    """Execute a pipeline whose type is provided by a runner plugin."""
    plugin_type = ctx.registry.get_pipeline_type(ctx.pipeline_type)
    if plugin_type is None:
        raise RuntimeError(f"No plugin registered for pipeline type '{ctx.pipeline_type}'")
    ns, layer, name = ctx.run.namespace, ctx.run.layer, ctx.run.pipeline_name
    ctx.log.info(f"Executing '{ctx.pipeline_type}' pipeline via plugin")
    return plugin_type.execute(
        ctx.source,
        ns,
        layer,
        name,
        ctx.s3_config,
        ctx.nessie_config,
        ctx.config,
    )


def _execute_sql_path(ctx: _PipelineContext) -> pa.Table:
    """Handle the SQL pipeline path: watermark read, compile, execute."""
    watermark_value: str | None = None
    if (
        ctx.config is not None
        and ctx.config.merge_strategy in (MergeStrategy.INCREMENTAL, MergeStrategy.DELETE_INSERT)
        and ctx.config.watermark_column
    ):
        ctx.log.info(f"Reading watermark for column '{ctx.config.watermark_column}'")
        watermark_value = read_watermark(
            ctx.table_name,
            ctx.config.watermark_column,
            ctx.s3_config,
            ctx.nessie_config,
            branch="main",
            conn=ctx.engine.conn if ctx.engine else None,
        )
        if watermark_value:
            ctx.log.info(f"Watermark value: {watermark_value}")
        else:
            ctx.log.info("No watermark found (first run or empty table)")

    ns, layer, name = ctx.run.namespace, ctx.run.layer, ctx.run.pipeline_name

    # Collect plugin Jinja helpers from registry
    plugin_helpers: dict[str, object] = {}
    for helper_name, helper in ctx.registry.get_helpers().items():
        plugin_helpers[helper_name] = helper

    ctx.log.info("Compiling SQL template")
    compiled_sql = compile_sql(
        ctx.raw_sql,  # type: ignore[arg-type]
        ns,
        layer,
        name,
        ctx.s3_config,
        ctx.nessie_config,
        config=ctx.config,
        watermark_value=watermark_value,
        plugin_helpers=plugin_helpers or None,
    )
    ctx.log.debug(f"Compiled SQL:\n{compiled_sql}")

    ctx.log.info("Executing SQL via DuckDB")
    assert ctx.engine is not None
    return ctx.engine.query_arrow(compiled_sql)


# ── Phase 3: Write to Iceberg ────────────────────────────────────────


def _phase3_write_iceberg(ctx: _PipelineContext) -> None:
    """Write the result table to Iceberg using the configured merge strategy.

    Checks the plugin registry first for a matching strategy (including built-in
    strategies when installed as a package). Falls back to direct dispatch when
    the registry doesn't have the strategy (e.g. development/testing).
    """
    _check_cancelled(ctx.run)
    assert ctx.result is not None

    if ctx.row_count == 0:
        ctx.log.info("Zero rows — skipping Iceberg write")
        ctx.run.rows_written = 0
        return

    strategy = ctx.config.merge_strategy if ctx.config else MergeStrategy.FULL_REFRESH

    # Try plugin registry first (includes built-in strategies when installed as package)
    plugin_strategy = ctx.registry.get_strategy(str(strategy))
    if plugin_strategy:
        ctx.log.info(f"Using strategy '{strategy}' via plugin registry")
        _engine_conn = ctx.engine.conn if ctx.engine else None
        rows = plugin_strategy.execute(
            ctx.result,
            ctx.table_name,
            ctx.s3_config,
            ctx.nessie_config,
            ctx.location,
            ctx.config,
            branch=ctx.branch_name,
            conn=_engine_conn,
        )
        ctx.run.rows_written = rows
        ctx.log.info(f"Strategy '{strategy}' complete ({rows} rows)")
        return

    # Fall back to built-in dispatch (for development/testing without package install)
    _engine_conn = ctx.engine.conn if ctx.engine else None
    _partition_by = ctx.config.partition_by if ctx.config and ctx.config.partition_by else None

    if strategy == MergeStrategy.INCREMENTAL and ctx.config and ctx.config.unique_key:
        ctx.log.info(f"Merging {ctx.row_count} rows into Iceberg table {ctx.table_name}")
        merged_rows = merge_iceberg(
            ctx.result,
            ctx.table_name,
            ctx.config.unique_key,
            ctx.s3_config,
            ctx.nessie_config,
            ctx.location,
            branch=ctx.branch_name,
            conn=_engine_conn,
            partition_by=_partition_by,
        )
        ctx.run.rows_written = merged_rows
        ctx.log.info(f"Merge complete ({merged_rows} total rows)")

    elif strategy == MergeStrategy.APPEND_ONLY:
        ctx.log.info(f"Appending {ctx.row_count} rows to Iceberg table {ctx.table_name}")
        appended = append_iceberg(
            ctx.result,
            ctx.table_name,
            ctx.s3_config,
            ctx.nessie_config,
            ctx.location,
            branch=ctx.branch_name,
            partition_by=_partition_by,
        )
        ctx.run.rows_written = appended
        ctx.log.info(f"Append complete ({appended} rows)")

    elif strategy == MergeStrategy.DELETE_INSERT and ctx.config and ctx.config.unique_key:
        ctx.log.info(f"Delete-insert {ctx.row_count} rows into Iceberg table {ctx.table_name}")
        total = delete_insert_iceberg(
            ctx.result,
            ctx.table_name,
            ctx.config.unique_key,
            ctx.s3_config,
            ctx.nessie_config,
            ctx.location,
            branch=ctx.branch_name,
            conn=_engine_conn,
            partition_by=_partition_by,
        )
        ctx.run.rows_written = total
        ctx.log.info(f"Delete-insert complete ({total} total rows)")

    elif strategy == MergeStrategy.SCD2 and ctx.config and ctx.config.unique_key:
        ctx.log.info(f"SCD2 merge {ctx.row_count} rows into Iceberg table {ctx.table_name}")
        total = scd2_iceberg(
            ctx.result,
            ctx.table_name,
            ctx.config.unique_key,
            ctx.s3_config,
            ctx.nessie_config,
            ctx.location,
            valid_from_col=ctx.config.scd_valid_from,
            valid_to_col=ctx.config.scd_valid_to,
            branch=ctx.branch_name,
            conn=_engine_conn,
            partition_by=_partition_by,
        )
        ctx.run.rows_written = total
        ctx.log.info(f"SCD2 merge complete ({total} total rows)")

    elif strategy == MergeStrategy.SNAPSHOT and ctx.config and ctx.config.partition_column:
        ctx.log.info(f"Snapshot {ctx.row_count} rows into Iceberg table {ctx.table_name}")
        total = snapshot_iceberg(
            ctx.result,
            ctx.table_name,
            ctx.config.partition_column,
            ctx.s3_config,
            ctx.nessie_config,
            ctx.location,
            branch=ctx.branch_name,
            conn=_engine_conn,
            partition_by=_partition_by,
        )
        ctx.run.rows_written = total
        ctx.log.info(f"Snapshot complete ({total} total rows)")

    else:
        _write_full_refresh_fallback(ctx, strategy)


def _write_full_refresh_fallback(ctx: _PipelineContext, strategy: MergeStrategy) -> None:
    """Write using full_refresh, warning if the intended strategy lacked required config."""
    assert ctx.result is not None

    if strategy in (MergeStrategy.INCREMENTAL, MergeStrategy.DELETE_INSERT, MergeStrategy.SCD2):
        if not (ctx.config and ctx.config.unique_key):
            ctx.log.warn(
                f"Strategy '{strategy}' requires unique_key — falling back to full_refresh"
            )
    elif strategy == MergeStrategy.SNAPSHOT and not (ctx.config and ctx.config.partition_column):
        ctx.log.warn("Strategy 'snapshot' requires partition_column — falling back to full_refresh")

    ctx.log.info(f"Writing {ctx.row_count} rows to Iceberg table {ctx.table_name}")
    partition_by = ctx.config.partition_by if ctx.config else None
    write_iceberg(
        ctx.result,
        ctx.table_name,
        ctx.s3_config,
        ctx.nessie_config,
        ctx.location,
        branch=ctx.branch_name,
        partition_by=partition_by or None,
    )
    ctx.run.rows_written = ctx.row_count
    ctx.log.info("Iceberg write complete")


# ── Phase 4: Quality tests ───────────────────────────────────────────


def _phase4_quality_tests(ctx: _PipelineContext) -> list[QualityTestResult]:
    """Run quality tests against the result data and return results."""
    _check_cancelled(ctx.run)
    assert ctx.engine is not None
    quality_results = run_quality_tests(
        ctx.run,
        ctx.engine,
        ctx.s3_config,
        ctx.nessie_config,
        ctx.log,
        published_versions=ctx.published_versions or None,
    )
    ctx.run.quality_results = quality_results
    return quality_results


# ── Phase 5: Branch resolution ───────────────────────────────────────


def _classify_merge_error(exc: BaseException) -> tuple[str, str]:
    """Return (error_kind, human_message) for an exception raised by merge_branch.

    Categories:
      * "conflict_exhausted" — 409 CONFLICT, even after internal refetch
        retries. The target ref kept moving; another long-running pipeline
        likely has it pinned.
      * "transient_exhausted" — 5xx / network / timeout, after the outer
        retry_on_transient decorator gave up.
      * "permanent_4xx" — any other 4xx (400 bad request, 404 not found,
        403 forbidden) — request is malformed or the branch was already
        gone.
      * "unknown" — anything else.
    """
    if isinstance(exc, urllib.error.HTTPError):
        if exc.code == 409:
            return "conflict_exhausted", (
                f"target moved during merge window after "
                f"{MERGE_CONFLICT_MAX_RETRIES} refetch attempts (HTTP 409)"
            )
        if 400 <= exc.code < 500:
            return "permanent_4xx", f"HTTP {exc.code}: {exc.reason}"
        if exc.code >= 500:
            return "transient_exhausted", f"HTTP {exc.code}: {exc.reason}"
    if isinstance(exc, urllib.error.URLError | TimeoutError):
        return "transient_exhausted", f"{type(exc).__name__}: {exc}"
    return "unknown", f"{type(exc).__name__}: {exc}"


def _phase5_resolve_branch(ctx: _PipelineContext, quality_results: list[QualityTestResult]) -> None:
    """Merge or discard the ephemeral branch based on quality test results.

    Since Phase 0 guarantees branch creation succeeded (or aborted the run),
    there is always an ephemeral branch to resolve here: merge to main on
    quality pass, delete on quality failure.

    On terminal merge failure (transient retries exhausted, 409 refetches
    exhausted, or permanent 4xx) the branch is RETAINED — Phase 3 wrote
    data and Phase 4 quality-tested it, and silently dropping it would
    erase work an operator can recover. We POST an audit record to ratd
    so the failure shows up in the `failed_merges` table.
    """
    if has_error_failures(quality_results):
        ctx.log.error("Quality tests failed — discarding branch (no data on main)")
        try:
            delete_branch(ctx.nessie_config, ctx.branch_name)
        except Exception as e:
            ctx.log.warn(f"Failed to delete branch: {e}")
        ctx.run.status = RunStatus.FAILED
        ctx.run.error = _format_quality_error(quality_results)
        return

    ctx.log.info(f"Merging branch '{ctx.branch_name}' to main")

    # Best-effort: capture the source/target hashes BEFORE attempting the
    # merge so the audit row has something useful even if Nessie blows up
    # mid-call. Failures here are silenced — they're not the audit's job
    # to surface, and the merge call below will reproduce them.
    source_hash: str | None = None
    target_hash: str | None = None
    try:
        source_hash = _get_reference(ctx.nessie_config, ctx.branch_name).get("hash")
        target_hash = _get_reference(ctx.nessie_config, "main").get("hash")
    except Exception:
        pass

    try:
        merge_branch(ctx.nessie_config, ctx.branch_name, target="main")
        ctx.log.info("Branch merged to main")
    except Exception as e:
        ctx.retain_branch = True
        error_kind, human = _classify_merge_error(e)
        msg = f"branch merge failed: {human} — branch {ctx.branch_name} retained for recovery"
        # Structured ERROR log with the fields a human will grep for.
        logger.error(
            "Phase 5 merge failed — branch retained",
            extra={
                "branch": ctx.branch_name,
                "run": ctx.run.run_id,
                "error_kind": error_kind,
                "merge_lost_data": True,
            },
            exc_info=True,
        )
        ctx.log.error(msg)
        try:
            record_failed_merge(
                ctx.run,
                ctx.branch_name,
                source_hash,
                target_hash,
                error_kind=error_kind,
                error_message=str(e),
            )
        except Exception as audit_exc:  # noqa: BLE001 — never let audit kill the run
            logger.warning(
                "failed_merges audit POST raised: %s",
                audit_exc,
                extra={"branch": ctx.branch_name, "run": ctx.run.run_id},
            )
        ctx.run.status = RunStatus.FAILED
        ctx.run.error = msg
        return

    _post_success(ctx)


# ── Post-success: archive + maintenance ──────────────────────────────


def _post_success(ctx: _PipelineContext) -> None:
    """Run post-success tasks: mark success, archive landing zones, run Iceberg maintenance."""
    ctx.run.status = RunStatus.SUCCESS
    ctx.log.info("Pipeline completed successfully")

    # Archive landing zone files (opt-in)
    if ctx.config is not None and ctx.config.archive_landing_zones:
        ns = ctx.run.namespace
        ctx.run.archived_zones = _archive_landing_zones(
            ctx.source,
            ns,
            ctx.run.run_id,
            ctx.s3_config,
            ctx.log,
        )

    # Iceberg maintenance (best-effort)
    if ctx.row_count > 0:
        try:
            run_maintenance(ctx.table_name, ctx.s3_config, ctx.nessie_config, log=ctx.log)
        except Exception as e:
            ctx.log.warn(f"Iceberg maintenance failed (non-fatal): {e}")


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


def _engine_mode() -> bool:
    """Transitional opt-in for the decoupled engine path (ADR-024); removed in #10."""
    return os.environ.get("RAT_ENGINE_MODE", "").lower() in ("1", "true", "yes")


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

    # Phase 4 (quality) still runs locally for now and needs a DuckDB engine to read
    # the freshly written branch table. TODO(#10): route quality via engine.Query and
    # drop the runner's embedded DuckDB entirely.
    if ctx.engine is None:
        ctx.engine = DuckDBEngine(ctx.s3_config)

    plane = ctx.data_plane if ctx.data_plane is not None else _resolve_data_plane(ctx)
    ctx.log.info(
        f"Engine mode: data_plane '{plane.name}' engine={plane.engine_addr} format={plane.format}"
    )

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

    inputs: list[Any] = []
    for dep in extract_dependencies(raw_for_deps):
        dep_ns, dep_layer, dep_name = _parse_ref(dep, ns)
        inputs.append(
            build_table_descriptor(
                dep_ns,
                layer_enum(dep_layer),
                dep_name,
                plane,
                ctx.nessie_config,
                ctx.s3_config,
                branch="main",
            )
        )
    output = build_table_descriptor(
        ns,
        layer_enum(layer),
        name,
        plane,
        ctx.nessie_config,
        ctx.s3_config,
        branch=ctx.branch_name,
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
        branching = True
        if _engine_mode():
            ctx.data_plane = _resolve_data_plane(ctx)
            branching = ctx.data_plane.format == "iceberg"

        if branching:
            _phase0_create_branch(ctx)
        else:
            ctx.branch_name = "main"
            ctx.log.info(
                f"Catalog format '{ctx.data_plane.format}' has no branching — "
                "writing directly (no ephemeral branch)"
            )
        _phase1_detect_and_load(ctx)

        # Dispatch pre_execute hooks
        hook_ctx = _build_hook_context(ctx)
        registry.dispatch_hooks("pre_execute", hook_ctx)

        if _engine_mode():
            # Decoupled path (ADR-024): execute + write happen in one engine/v1 call.
            _phase_engine_execute(ctx)
        else:
            _phase2_build_result(ctx)

            # Dispatch pre_write hooks
            hook_ctx = _build_hook_context(ctx)
            registry.dispatch_hooks("pre_write", hook_ctx)

            _phase3_write_iceberg(ctx)

        # Dispatch post_write hooks
        hook_ctx = _build_hook_context(ctx)
        registry.dispatch_hooks("post_write", hook_ctx)

        # Dispatch pre_quality hooks
        hook_ctx = _build_hook_context(ctx)
        registry.dispatch_hooks("pre_quality", hook_ctx)

        quality_results = _phase4_quality_tests(ctx)

        # Dispatch post_quality hooks
        hook_ctx = _build_hook_context(ctx)
        registry.dispatch_hooks("post_quality", hook_ctx)

        if branching:
            _phase5_resolve_branch(ctx, quality_results)
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
                delete_branch(nessie_config, run.branch)
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

        if ctx.engine is not None:
            ctx.engine.close()
        elapsed_ms = int((time.monotonic() - start) * 1000)
        run.duration_ms = elapsed_ms
        log.info(f"Duration: {elapsed_ms}ms")

        # Restore the prior context binding — important when the same worker
        # thread is recycled for a different run by ThreadPoolExecutor, so the
        # next run doesn't inherit the previous run's extras until it binds
        # its own.
        clear_run_context(_context_token)
