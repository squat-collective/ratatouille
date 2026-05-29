"""gRPC server for the rat-engine-duckdb reference engine plugin.

Implements engine/v1 EngineService — the DuckDB compute axis of RAT's decoupled
data architecture (ADR-024). The engine does its OWN catalog + storage I/O: it
reconstructs S3Config/NessieConfig from the descriptors on the request, resolves
each input's logical ref to a native `iceberg_scan` view, runs the transform, and
(for Execute) dispatches the `(duckdb, iceberg)` strategy recipe to write + commit.
The runner never touches table bytes.
"""

from __future__ import annotations

import logging
from concurrent import futures
from typing import TYPE_CHECKING, Any

import grpc
import pyarrow as pa

# Proto imports (gen/ must be on sys.path — see __main__.py).
from engine.v1 import (  # type: ignore[import-untyped]
    engine_pb2,
    engine_pb2_grpc,
)

from rat_engine_duckdb import __version__
from rat_engine_duckdb.adapters import (
    layer_name,
    nessie_config_from_catalog,
    pipeline_config_from_options,
    s3_config_from_storage,
    table_identifier,
    table_location,
)
from rat_engine_duckdb.config import S3Config
from rat_engine_duckdb.duckdb_engine import DuckDBEngine
from rat_engine_duckdb.formats import ducklake
from rat_engine_duckdb.formats.iceberg import (
    _configure_s3,
    _escape_sql_string,
    _quote_identifier,
    get_catalog,
)
from rat_engine_duckdb.python_exec import execute_python_pipeline
from rat_engine_duckdb.strategies import (
    AppendOnlyStrategy,
    DeleteInsertStrategy,
    FullRefreshStrategy,
    IncrementalStrategy,
    SCD2Strategy,
    SnapshotStrategy,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

_logger = logging.getLogger("rat_engine_duckdb.server")

GRPC_MAX_WORKERS = 10

# Engine capabilities reported via Describe — open-set strings per ADR-024.
_LANGUAGES = ["sql", "python"]
_FORMATS = ["iceberg", "ducklake"]
_CAPABILITIES = ["preview", "explain"]
_DIALECTS = ["duckdb"]

# Strategy NAME → recipe. Resolved inside the engine for the (duckdb, iceberg) pair.
_STRATEGIES = {
    "full_refresh": FullRefreshStrategy(),
    "incremental": IncrementalStrategy(),
    "append_only": AppendOnlyStrategy(),
    "delete_insert": DeleteInsertStrategy(),
    "scd2": SCD2Strategy(),
    "snapshot": SnapshotStrategy(),
}


def _schema_to_ipc(schema: pa.Schema) -> bytes:
    """Serialize an Arrow schema to IPC bytes (for ExecuteResult.output_schema)."""
    return schema.serialize().to_pybytes()


def _table_to_ipc(table: pa.Table) -> bytes:
    """Serialize an Arrow table to a single IPC stream blob."""
    sink = pa.BufferOutputStream()
    with pa.ipc.new_stream(sink, table.schema) as writer:
        writer.write_table(table)
    return sink.getvalue().to_pybytes()


def _register_inputs(conn: Any, inputs: Any) -> None:
    """Resolve each input descriptor → a DuckDB view backed by iceberg_scan.

    SQL references inputs by `"<layer>"."<name>"`; the engine maps that logical
    name to the table's current metadata location via the input's own catalog.
    """
    for inp in inputs:
        s3 = s3_config_from_storage(inp.storage)
        nessie = nessie_config_from_catalog(inp.catalog)
        branch = inp.catalog.branch or "main"
        _configure_s3(conn, s3)
        catalog = get_catalog(s3, nessie, branch)
        table = catalog.load_table(table_identifier(inp))
        metadata = table.metadata_location
        layer = _quote_identifier(layer_name(inp.ref.layer))
        name = _quote_identifier(inp.ref.name)
        conn.execute(f"CREATE SCHEMA IF NOT EXISTS {layer}")
        conn.execute(
            f"CREATE OR REPLACE VIEW {layer}.{name} AS "
            f"SELECT * FROM iceberg_scan('{_escape_sql_string(metadata)}')"
        )


def _engine_for_inputs(inputs: Any) -> DuckDBEngine:
    """Build a DuckDBEngine using the first input's storage (or defaults if none)."""
    s3 = s3_config_from_storage(inputs[0].storage) if len(inputs) else S3Config()
    return DuckDBEngine(s3)


def _execute_error(message: str) -> Any:
    return engine_pb2.ExecuteResponse(result=engine_pb2.ExecuteResult(error=message))


def _execute_iceberg(engine: DuckDBEngine, request: Any) -> tuple[int, Any]:
    """The (duckdb, iceberg) path: register input views, run the transform, apply the strategy."""
    out = request.output
    language = request.language or "sql"
    if language not in ("sql", "python"):
        raise RuntimeError(f"unsupported language {language!r}")
    strategy = _STRATEGIES.get(request.strategy or "full_refresh")
    if strategy is None:
        raise RuntimeError(f"unknown strategy {request.strategy!r}")
    s3 = s3_config_from_storage(out.storage)
    nessie = nessie_config_from_catalog(out.catalog)
    branch = out.catalog.branch or "main"
    cfg = pipeline_config_from_options(request.options, request.strategy or "full_refresh")
    if language == "sql":
        _register_inputs(engine.conn, request.inputs)
        result = engine.query_arrow(request.source)
    else:
        _configure_s3(engine.conn, s3)
        result = execute_python_pipeline(
            request.source,
            engine,
            out.ref.namespace,
            layer_name(out.ref.layer),
            out.ref.name,
            s3,
            nessie,
            config=cfg,
        )
    rows = strategy.execute(
        result,
        table_identifier(out),
        s3,
        nessie,
        table_location(out),
        cfg,
        branch=branch,
        conn=engine.conn,
    )
    return rows, result.schema


class EngineServicer(engine_pb2_grpc.EngineServiceServicer):
    """DuckDB implementation of engine/v1."""

    def Describe(self, request: Any, context: grpc.ServicerContext) -> Any:  # noqa: N802
        return engine_pb2.DescribeResponse(
            name="rat-engine-duckdb",
            version=__version__,
            languages=_LANGUAGES,
            formats=_FORMATS,
            capabilities=_CAPABILITIES,
            dialects=_DIALECTS,
        )

    def Execute(self, request: Any, context: grpc.ServicerContext) -> Iterator[Any]:  # noqa: N802
        fmt = request.output.format or "iceberg"
        try:
            engine = DuckDBEngine(s3_config_from_storage(request.output.storage))
            try:
                if fmt == "ducklake":
                    rows, schema = ducklake.execute_ducklake(engine.conn, request)
                elif fmt == "iceberg":
                    rows, schema = _execute_iceberg(engine, request)
                else:
                    yield _execute_error(f"rat-engine-duckdb has no adapter for format {fmt!r}")
                    return
                yield engine_pb2.ExecuteResponse(
                    result=engine_pb2.ExecuteResult(
                        rows_written=rows,
                        output_schema=_schema_to_ipc(schema),
                        error="",
                    )
                )
            finally:
                engine.close()
        except Exception as exc:
            _logger.exception("Execute failed for run %s", request.run_id)
            yield _execute_error(str(exc))

    def Query(self, request: Any, context: grpc.ServicerContext) -> Iterator[Any]:  # noqa: N802
        if request.language and request.language != "sql":
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, "Query supports only 'sql'")
        engine = _engine_for_inputs(request.inputs)
        try:
            _register_inputs(engine.conn, request.inputs)
            sql = request.source
            if request.limit:
                sql = f"SELECT * FROM ({sql}) AS _q LIMIT {int(request.limit)}"
            table = engine.query_arrow(sql)
            yield engine_pb2.QueryResponse(arrow_ipc=_table_to_ipc(table))
        finally:
            engine.close()

    def Preview(self, request: Any, context: grpc.ServicerContext) -> Any:  # noqa: N802
        if request.language and request.language != "sql":
            return engine_pb2.PreviewResponse(
                success=False, error="Preview supports only 'sql' (python lands next increment)"
            )
        try:
            engine = _engine_for_inputs(request.inputs)
            try:
                _register_inputs(engine.conn, request.inputs)
                limit = int(request.limit) or 100
                table = engine.query_arrow(f"SELECT * FROM ({request.source}) AS _p LIMIT {limit}")
                return engine_pb2.PreviewResponse(
                    success=True, arrow_ipc=_table_to_ipc(table), error=""
                )
            finally:
                engine.close()
        except Exception as exc:
            return engine_pb2.PreviewResponse(success=False, error=str(exc))


def serve(port: int = 50081) -> None:
    """Start the engine/v1 gRPC server and block until termination."""
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=GRPC_MAX_WORKERS))
    engine_pb2_grpc.add_EngineServiceServicer_to_server(EngineServicer(), server)
    server.add_insecure_port(f"[::]:{port}")
    _logger.info("rat-engine-duckdb (engine/v1) serving on :%d", port)
    server.start()
    server.wait_for_termination()
