"""gRPC server for the rat-engine-duckdb reference engine plugin.

Implements engine/v1 EngineService — the DuckDB compute axis of RAT's decoupled
data architecture (ADR-024). The engine itself knows NOTHING about specific
formats: at startup it discovers FormatAdapter implementations (either via
`rat_engine_duckdb.format_adapters` entry-points or the comma-separated
``RAT_FORMAT_ADAPTERS`` env var for mount-based dev), then dispatches per
``descriptor.format``:

  * inputs  → adapter.register_input
  * outputs → adapter.execute_write
"""

from __future__ import annotations

import logging
import os
from concurrent import futures
from importlib import import_module
from importlib.metadata import entry_points
from typing import TYPE_CHECKING, Any

import grpc
import pyarrow as pa
from engine.v1 import (  # type: ignore[import-untyped]
    engine_pb2,
    engine_pb2_grpc,
)

from rat_engine_duckdb import __version__
from rat_engine_duckdb.adapters import s3_config_from_storage
from rat_engine_duckdb.config import S3Config
from rat_engine_duckdb.duckdb_engine import DuckDBEngine

if TYPE_CHECKING:
    from collections.abc import Iterator

    from rat_engine_duckdb.format_adapter import FormatAdapter

_logger = logging.getLogger("rat_engine_duckdb.server")

GRPC_MAX_WORKERS = 10

# Engine capabilities reported via Describe — open-set strings per ADR-024.
# `formats` is computed at startup from the discovered adapters.
_LANGUAGES = ["sql", "python"]
_CAPABILITIES = ["preview", "explain"]
_DIALECTS = ["duckdb"]

_ADAPTERS_ENTRY_POINT_GROUP = "rat_engine_duckdb.format_adapters"
_ADAPTERS_ENV = "RAT_FORMAT_ADAPTERS"


def _load_adapters() -> dict[str, FormatAdapter]:
    """Discover format adapters: env-var first (dev/test), entry_points after (prod).

    The env var is a comma-separated list of ``module:Class`` specs. Adapters from
    entry_points won't overwrite anything already loaded from the env var, so dev
    can pin a specific module path without needing to uninstall installed ones.
    """
    adapters: dict[str, FormatAdapter] = {}
    for spec in (os.environ.get(_ADAPTERS_ENV, "") or "").split(","):
        spec = spec.strip()
        if not spec:
            continue
        mod_name, _sep, cls_name = spec.partition(":")
        if not cls_name:
            _logger.warning("%s entry %r missing 'Class' part — skipping", _ADAPTERS_ENV, spec)
            continue
        try:
            adapter = getattr(import_module(mod_name), cls_name)()
        except Exception:
            _logger.exception("Failed to load format adapter %r from %s", spec, _ADAPTERS_ENV)
            continue
        adapters[adapter.name] = adapter
        _logger.info("Loaded format adapter %r from %s (env)", adapter.name, spec)
    for ep in entry_points(group=_ADAPTERS_ENTRY_POINT_GROUP):
        try:
            adapter = ep.load()()
        except Exception:
            _logger.exception("Failed to load format adapter from entry-point %s", ep.name)
            continue
        if adapter.name in adapters:
            continue  # env var wins
        adapters[adapter.name] = adapter
        _logger.info("Loaded format adapter %r from entry-point %r", adapter.name, ep.name)
    if not adapters:
        _logger.warning(
            "No format adapters discovered — set %s or install a rat-format-* plugin",
            _ADAPTERS_ENV,
        )
    return adapters


def _schema_to_ipc(schema: pa.Schema) -> bytes:
    """Serialize an Arrow schema to IPC bytes (for ExecuteResult.output_schema)."""
    return schema.serialize().to_pybytes()


def _table_to_ipc(table: pa.Table) -> bytes:
    """Serialize an Arrow table to a single IPC stream blob."""
    sink = pa.BufferOutputStream()
    with pa.ipc.new_stream(sink, table.schema) as writer:
        writer.write_table(table)
    return sink.getvalue().to_pybytes()


def _engine_for_inputs(inputs: Any) -> DuckDBEngine:
    """Build a DuckDBEngine using the first input's storage (or defaults if none)."""
    s3 = s3_config_from_storage(inputs[0].storage) if len(inputs) else S3Config()
    return DuckDBEngine(s3)


def _execute_error(message: str) -> Any:
    return engine_pb2.ExecuteResponse(result=engine_pb2.ExecuteResult(error=message))


class EngineServicer(engine_pb2_grpc.EngineServiceServicer):
    """DuckDB implementation of engine/v1 — pure orchestration, format-agnostic."""

    def __init__(self, adapters: dict[str, FormatAdapter]) -> None:
        self._adapters = adapters

    def _register_inputs(self, engine: DuckDBEngine, inputs: Any) -> None:
        """Register every input via its format adapter — raise on a missing one."""
        for inp in inputs:
            fmt = inp.format or "iceberg"
            adapter = self._adapters.get(fmt)
            if adapter is None:
                raise RuntimeError(
                    f"no format adapter installed for {fmt!r} (installed: {sorted(self._adapters)})"
                )
            adapter.register_input(engine, inp)

    def Describe(self, request: Any, context: grpc.ServicerContext) -> Any:  # noqa: N802
        return engine_pb2.DescribeResponse(
            name="rat-engine-duckdb",
            version=__version__,
            languages=_LANGUAGES,
            formats=sorted(self._adapters),
            capabilities=_CAPABILITIES,
            dialects=_DIALECTS,
        )

    def Execute(self, request: Any, context: grpc.ServicerContext) -> Iterator[Any]:  # noqa: N802
        fmt = request.output.format or "iceberg"
        adapter = self._adapters.get(fmt)
        if adapter is None:
            yield _execute_error(
                f"no format adapter installed for {fmt!r} (installed: {sorted(self._adapters)})"
            )
            return
        try:
            engine = DuckDBEngine(s3_config_from_storage(request.output.storage))
            try:
                # Register inputs FIRST so the output adapter sees them in `engine.conn`
                # (its execute_write may run SQL that references them).
                self._register_inputs(engine, request.inputs)
                rows, schema = adapter.execute_write(engine, request)
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
            self._register_inputs(engine, request.inputs)
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
                self._register_inputs(engine, request.inputs)
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
    adapters = _load_adapters()
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=GRPC_MAX_WORKERS))
    engine_pb2_grpc.add_EngineServiceServicer_to_server(EngineServicer(adapters), server)
    server.add_insecure_port(f"[::]:{port}")
    _logger.info(
        "rat-engine-duckdb (engine/v1) serving on :%d (formats: %s)", port, sorted(adapters)
    )
    server.start()
    server.wait_for_termination()
