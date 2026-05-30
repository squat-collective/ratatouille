"""Preview executor — ephemeral pipeline execution without writes."""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from common.v1 import data_plane_pb2  # type: ignore[import-untyped]
from engine.v1 import engine_pb2  # type: ignore[import-untyped]

from rat_runner.bindings import BindingConfig
from rat_runner.catalog_client import CatalogClient
from rat_runner.config import (
    CatalogConfig,
    EngineConfig,
    NessieConfig,
    S3Config,
    StorageConfig,
    read_s3_text,
)
from rat_runner.engine_client import EngineClient
from rat_runner.storage_client import StorageClient

if TYPE_CHECKING:
    import pyarrow as pa
from rat_runner.engine import DuckDBEngine
from rat_runner.log import RunLogger
from rat_runner.models import LogRecord, PipelineConfig, RunState
from rat_runner.plugin_registry import PluginRegistry
from rat_runner.python_exec import execute_python_pipeline
from rat_runner.templating import (
    _resolve_landing_zone_preview,
    compile_sql,
    extract_dependencies,
    extract_metadata,
    metadata_to_config,
)

# 2-part/3-part medallion refs (default ns when 2-part). Mirrors executor._parse_ref.
_LAYER_ENUMS = {"bronze": 1, "silver": 2, "gold": 3}


def _engine_mode() -> bool:
    """Same transitional gate as executor — route reads through engine/v1 when set."""
    return os.environ.get("RAT_ENGINE_MODE", "").lower() in ("1", "true", "yes")


def _split_ref(ref: str, default_ns: str) -> tuple[str, str, str]:
    """`ns.layer.name` or `layer.name` → (namespace, layer, name)."""
    parts = ref.split(".", 2)
    if len(parts) == 2:
        return default_ns, parts[0], parts[1]
    if len(parts) == 3:
        return parts[0], parts[1], parts[2]
    raise ValueError(f"Invalid ref {ref!r}: expected 'layer.name' or 'ns.layer.name'")


PREVIEW_TIMEOUT_SECONDS = 30
DEFAULT_PREVIEW_LIMIT = 100


@dataclass
class ColumnInfo:
    """Column metadata for preview results."""

    name: str
    type: str


@dataclass
class PhaseProfile:
    """Timing for a single execution phase."""

    name: str
    duration_ms: int
    metadata: dict[str, str] = field(default_factory=dict)


@dataclass
class PreviewResult:
    """Result of a pipeline preview execution."""

    arrow_table: pa.Table | None = None
    columns: list[ColumnInfo] = field(default_factory=list)
    total_row_count: int = 0
    phases: list[PhaseProfile] = field(default_factory=list)
    explain_output: str = ""
    memory_peak_bytes: int = 0
    logs: list[LogRecord] = field(default_factory=list)
    error: str = ""
    warnings: list[str] = field(default_factory=list)


def _time_ms(start: float) -> int:
    return int((time.monotonic() - start) * 1000)


def _preview_sql_via_engine(
    source: str,
    namespace: str,
    layer: str,
    pipeline_name: str,
    s3_config: S3Config,
    nessie_config: NessieConfig,
    config: PipelineConfig | None,
    log: RunLogger,
    result: PreviewResult,
    preview_limit: int,
) -> None:
    """SQL preview through engine.Preview (ADR-024 #10 stage 2).

    Runner does no local I/O: compiles logical-ref SQL, resolves each ref to a
    TableDescriptor via catalog/v1+storage/v1, and asks the engine to run a
    LIMIT-bounded preview. EXPLAIN ANALYZE is not part of the Preview contract
    (skipped with a warning); the exact total row count is only known when the
    sample didn't fill the limit.
    """
    binding = BindingConfig.load(
        os.environ.get("RAT_BINDINGS"),
        engine_addr=EngineConfig.from_env().addr,
        catalog_addr=CatalogConfig.from_env().addr,
        storage_addr=StorageConfig.from_env().addr,
    )
    plane = binding.resolve(namespace, layer, pipeline_name)

    t0 = time.monotonic()
    compiled = compile_sql(
        raw_sql=source,
        namespace=namespace,
        layer=layer,
        pipeline_name=pipeline_name,
        s3_config=s3_config,
        nessie_config=nessie_config,
        config=config,
        logical_refs=True,
    )
    result.phases.append(PhaseProfile(name="compile", duration_ms=_time_ms(t0)))
    log.info("SQL compiled (logical refs)")

    catalog_client = CatalogClient(plane.catalog_addr)
    storage_client = StorageClient(plane.storage_addr)
    try:
        storage = storage_client.vend_descriptor(location=s3_config.bucket)
        inputs = []
        for dep in extract_dependencies(source):
            d_ns, d_layer, d_name = _split_ref(dep, namespace)
            ref = data_plane_pb2.TableRef(namespace=d_ns, layer=_LAYER_ENUMS[d_layer], name=d_name)
            info = catalog_client.get_table(ref, "main")
            inputs.append(
                data_plane_pb2.TableDescriptor(
                    ref=ref,
                    format=info.format,
                    identifier=info.identifier,
                    catalog=info.catalog,
                    storage=storage,
                )
            )
    finally:
        catalog_client.close()
        storage_client.close()

    t0 = time.monotonic()
    client = EngineClient(plane.engine_addr)
    try:
        ok, table, err = client.preview(
            engine_pb2.PreviewRequest(
                language="sql", source=compiled, inputs=inputs, limit=preview_limit
            )
        )
    finally:
        client.close()
    if not ok:
        raise RuntimeError(f"engine.Preview failed: {err}")
    assert table is not None
    result.phases.append(
        PhaseProfile(
            name="execute", duration_ms=_time_ms(t0), metadata={"limit": str(preview_limit)}
        )
    )
    result.arrow_table = table
    result.columns = _extract_columns(table)
    log.info(f"Executed via engine.Preview: {table.num_rows} rows")

    result.warnings.append("EXPLAIN ANALYZE skipped: not supported by engine.Preview")
    if table.num_rows < preview_limit:
        result.total_row_count = table.num_rows
    else:
        result.total_row_count = -1
        result.warnings.append("Total row count unknown (preview returned full limit)")


def preview_pipeline(
    namespace: str,
    layer: str,
    pipeline_name: str,
    s3_config: S3Config,
    nessie_config: NessieConfig,
    preview_limit: int = DEFAULT_PREVIEW_LIMIT,
    code: str | None = None,
    pipeline_type: str | None = None,
) -> PreviewResult:
    """Execute a pipeline in preview mode — no writes, no branches, no quality tests.

    Returns sample rows, column info, timing profile, EXPLAIN ANALYZE output,
    memory stats, and execution logs.
    """
    result = PreviewResult()

    # Create a lightweight RunState for the logger (not persisted)
    run_state = RunState(
        run_id="preview",
        namespace=namespace,
        layer=layer,
        pipeline_name=pipeline_name,
        trigger="preview",
    )
    log = RunLogger(run_state)
    engine: DuckDBEngine | None = None

    try:
        engine = DuckDBEngine(s3_config)
        log.info(f"Starting preview for {namespace}/{layer}/{pipeline_name}")

        # --- Phase 1: Detect pipeline type + read config ---
        t0 = time.monotonic()
        layer_str = layer
        registry = PluginRegistry()
        registry.discover()
        detected_type, source, config = _detect_pipeline(
            namespace,
            layer_str,
            pipeline_name,
            s3_config,
            log,
            registry,
            code=code,
            pipeline_type_hint=pipeline_type,
        )
        pipeline_type = detected_type
        result.phases.append(
            PhaseProfile(
                name="detect",
                duration_ms=_time_ms(t0),
                metadata={"pipeline_type": pipeline_type},
            )
        )

        if pipeline_type == "sql" and _engine_mode():
            # Engine mode: skip local DuckDB entirely; rows come from engine.Preview.
            _preview_sql_via_engine(
                source=source,
                namespace=namespace,
                layer=layer_str,
                pipeline_name=pipeline_name,
                s3_config=s3_config,
                nessie_config=nessie_config,
                config=config,
                log=log,
                result=result,
                preview_limit=preview_limit,
            )
        elif pipeline_type == "sql":
            _preview_sql(
                source=source,
                namespace=namespace,
                layer=layer_str,
                pipeline_name=pipeline_name,
                s3_config=s3_config,
                nessie_config=nessie_config,
                config=config,
                engine=engine,
                log=log,
                result=result,
                preview_limit=preview_limit,
            )
        elif pipeline_type == "python":
            _preview_python(
                source=source,
                namespace=namespace,
                layer=layer_str,
                pipeline_name=pipeline_name,
                s3_config=s3_config,
                nessie_config=nessie_config,
                config=config,
                engine=engine,
                log=log,
                result=result,
                preview_limit=preview_limit,
            )
        else:
            _preview_plugin_type(
                pipeline_type=pipeline_type,
                source=source,
                namespace=namespace,
                layer=layer_str,
                pipeline_name=pipeline_name,
                s3_config=s3_config,
                nessie_config=nessie_config,
                config=config,
                registry=registry,
                log=log,
                result=result,
                preview_limit=preview_limit,
            )

        # Collect memory stats
        try:
            mem_stats = engine.get_memory_stats()
            result.memory_peak_bytes = mem_stats.get("memory_usage", 0)
        except Exception:
            pass

        log.info("Preview completed successfully")

    except Exception as e:
        result.error = str(e)
        log.error(f"Preview failed: {e}")
    finally:
        if engine is not None:
            engine.close()
        # Collect logs from run state
        result.logs = list(run_state.logs)

    return result


def _detect_pipeline(
    namespace: str,
    layer: str,
    pipeline_name: str,
    s3_config: S3Config,
    log: RunLogger,
    registry: PluginRegistry,
    code: str | None = None,
    pipeline_type_hint: str | None = None,
) -> tuple[str, str, PipelineConfig | None]:
    """Detect pipeline type and read source + config.

    If ``code`` is provided, uses it directly instead of reading from S3.
    ``pipeline_type_hint`` ("sql" or "python") disambiguates the type when
    inline code is given; defaults to "sql".
    """
    prefix = f"{namespace}/pipelines/{layer}/{pipeline_name}"

    # Inline code path — skip S3 reads for the source file
    if code is not None:
        known = {"sql", "python", *registry.pipeline_type_names()}
        ptype = pipeline_type_hint if pipeline_type_hint in known else "sql"
        log.info(f"Using inline {ptype} code ({len(code)} chars)")
        config = _load_config(code, prefix, s3_config, registry)
        return ptype, code, config

    # Try Python first, then SQL (same order as executor.py)
    py_source = read_s3_text(s3_config, f"{prefix}/pipeline.py")
    if py_source is not None:
        log.info("Detected Python pipeline")
        config = _load_config(py_source, prefix, s3_config, registry)
        return "python", py_source, config

    sql_source = read_s3_text(s3_config, f"{prefix}/pipeline.sql")
    if sql_source is not None:
        log.info("Detected SQL pipeline")
        config = _load_config(sql_source, prefix, s3_config, registry)
        return "sql", sql_source, config

    # Plugin-provided pipeline types (e.g. pipeline.prql).
    for type_name in registry.pipeline_type_names():
        plugin_type = registry.get_pipeline_type(type_name)
        if plugin_type is None:
            continue
        ext_source = read_s3_text(s3_config, f"{prefix}/pipeline.{plugin_type.file_extension}")
        if ext_source is not None:
            log.info(f"Detected {type_name} pipeline")
            config = _load_config(ext_source, prefix, s3_config, registry)
            return type_name, ext_source, config

    raise FileNotFoundError(
        f"No pipeline.py, pipeline.sql, or plugin pipeline-type file found at {prefix}/"
    )


def _load_config(
    source: str,
    prefix: str,
    s3_config: S3Config,
    registry: PluginRegistry,
) -> PipelineConfig | None:
    """Load config from inline annotations or config.yaml."""
    metadata = extract_metadata(source)
    if metadata:
        return metadata_to_config(metadata)

    config_yaml = read_s3_text(s3_config, f"{prefix}/config.yaml")
    if config_yaml:
        from rat_runner.config import parse_pipeline_config

        # Pass plugin strategy names so preview accepts custom merge strategies.
        return parse_pipeline_config(config_yaml, registry.strategy_names())

    return None


def _extract_columns(table: pa.Table) -> list[ColumnInfo]:
    """Extract column names and types from a PyArrow table."""
    columns = []
    for schema_field in table.schema:
        columns.append(ColumnInfo(name=schema_field.name, type=str(schema_field.type)))
    return columns


def _preview_sql(
    source: str,
    namespace: str,
    layer: str,
    pipeline_name: str,
    s3_config: S3Config,
    nessie_config: NessieConfig,
    config: PipelineConfig | None,
    engine: DuckDBEngine,
    log: RunLogger,
    result: PreviewResult,
    preview_limit: int,
) -> None:
    """Run SQL pipeline preview — compile, execute with LIMIT, EXPLAIN ANALYZE, COUNT."""
    # Phase 2: Compile SQL
    t0 = time.monotonic()

    def preview_lz_fn(zone_name: str) -> str:
        return _resolve_landing_zone_preview(zone_name, namespace, s3_config, result.warnings)

    compiled_sql = compile_sql(
        raw_sql=source,
        namespace=namespace,
        layer=layer,
        pipeline_name=pipeline_name,
        s3_config=s3_config,
        nessie_config=nessie_config,
        config=config,
        landing_zone_fn=preview_lz_fn,
    )
    result.phases.append(PhaseProfile(name="compile", duration_ms=_time_ms(t0)))
    log.info("SQL compiled")

    # Phase 3: Execute with LIMIT
    t0 = time.monotonic()
    limited_sql = f"SELECT * FROM ({compiled_sql}) AS _preview LIMIT {preview_limit}"
    table = engine.query_arrow(limited_sql)
    result.phases.append(
        PhaseProfile(
            name="execute",
            duration_ms=_time_ms(t0),
            metadata={"limit": str(preview_limit)},
        )
    )
    result.arrow_table = table
    result.columns = _extract_columns(table)
    log.info(f"Executed with LIMIT {preview_limit}: {table.num_rows} rows")

    # Phase 4: EXPLAIN ANALYZE
    # Use the limited SQL to avoid re-executing the full query.  The query
    # plan for the LIMIT-wrapped version is representative enough for
    # preview purposes and avoids a second full-data scan.
    t0 = time.monotonic()
    try:
        explain_text = engine.explain_analyze(limited_sql)
        result.explain_output = explain_text
    except Exception as e:
        result.warnings.append(f"EXPLAIN ANALYZE failed: {e}")
        log.warn(f"EXPLAIN ANALYZE failed: {e}")
    result.phases.append(PhaseProfile(name="explain", duration_ms=_time_ms(t0)))

    # Phase 5: COUNT(*)
    # Skip the extra full-query execution when the LIMIT query already returned
    # fewer rows than requested — that means we have the exact total.
    t0 = time.monotonic()
    if table.num_rows < preview_limit:
        result.total_row_count = table.num_rows
    else:
        try:
            count_result = engine.conn.execute(
                f"SELECT COUNT(*) FROM ({compiled_sql}) AS _count"
            ).fetchone()
            result.total_row_count = count_result[0] if count_result else 0
        except Exception as e:
            result.warnings.append(f"COUNT(*) failed: {e}")
            result.total_row_count = table.num_rows
            log.warn(f"COUNT(*) failed: {e}")
    result.phases.append(PhaseProfile(name="count", duration_ms=_time_ms(t0)))
    log.info(f"Total row count: {result.total_row_count}")


def _preview_python(
    source: str,
    namespace: str,
    layer: str,
    pipeline_name: str,
    s3_config: S3Config,
    nessie_config: NessieConfig,
    config: PipelineConfig | None,
    engine: DuckDBEngine,
    log: RunLogger,
    result: PreviewResult,
    preview_limit: int,
) -> None:
    """Run Python pipeline preview — execute with logger, slice result."""
    # Phase 2: Compile (no-op for Python)
    result.phases.append(
        PhaseProfile(
            name="compile",
            duration_ms=0,
            metadata={"skipped": "python"},
        )
    )

    # Phase 3: Execute
    t0 = time.monotonic()

    def preview_lz_fn(zone_name: str) -> str:
        return _resolve_landing_zone_preview(zone_name, namespace, s3_config, result.warnings)

    table = execute_python_pipeline(
        source=source,
        engine=engine,
        namespace=namespace,
        layer=layer,
        name=pipeline_name,
        s3_config=s3_config,
        nessie_config=nessie_config,
        config=config,
        logger=log,
        landing_zone_fn=preview_lz_fn,
    )
    result.phases.append(
        PhaseProfile(
            name="execute",
            duration_ms=_time_ms(t0),
            metadata={"limit": str(preview_limit)},
        )
    )

    # Slice to limit
    total = table.num_rows
    if total > preview_limit:
        table = table.slice(0, preview_limit)

    result.arrow_table = table
    result.columns = _extract_columns(table)
    result.total_row_count = total
    log.info(f"Executed Python pipeline: {table.num_rows} rows (total: {total})")

    # Phase 4: EXPLAIN (N/A for Python)
    result.phases.append(
        PhaseProfile(
            name="explain",
            duration_ms=0,
            metadata={"skipped": "python"},
        )
    )

    # Phase 5: COUNT (already have total)
    result.phases.append(PhaseProfile(name="count", duration_ms=0))


def _preview_plugin_type(
    pipeline_type: str,
    source: str,
    namespace: str,
    layer: str,
    pipeline_name: str,
    s3_config: S3Config,
    nessie_config: NessieConfig,
    config: PipelineConfig | None,
    registry: PluginRegistry,
    log: RunLogger,
    result: PreviewResult,
    preview_limit: int,
) -> None:
    """Preview a pipeline whose type is provided by a runner plugin.

    Runs the plugin's execute() and slices the result — there are no writes
    in preview mode, and the plugin owns its own execution.
    """
    plugin_type = registry.get_pipeline_type(pipeline_type)
    if plugin_type is None:
        raise RuntimeError(f"No plugin registered for pipeline type '{pipeline_type}'")

    result.phases.append(
        PhaseProfile(name="compile", duration_ms=0, metadata={"skipped": pipeline_type})
    )

    t0 = time.monotonic()
    table = plugin_type.execute(
        source,
        namespace,
        layer,
        pipeline_name,
        s3_config,
        nessie_config,
        config,
    )
    result.phases.append(
        PhaseProfile(
            name="execute",
            duration_ms=_time_ms(t0),
            metadata={"limit": str(preview_limit)},
        )
    )

    total = table.num_rows
    if total > preview_limit:
        table = table.slice(0, preview_limit)
    result.arrow_table = table
    result.columns = _extract_columns(table)
    result.total_row_count = total
    log.info(f"Executed '{pipeline_type}' pipeline: {table.num_rows} rows (total: {total})")

    # EXPLAIN / COUNT are N/A — the plugin owns execution.
    result.phases.append(
        PhaseProfile(name="explain", duration_ms=0, metadata={"skipped": pipeline_type})
    )
    result.phases.append(PhaseProfile(name="count", duration_ms=0))
