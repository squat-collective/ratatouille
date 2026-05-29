"""gRPC server — QueryService implementation."""

from __future__ import annotations

import logging
import os
import re
import threading
import time
from concurrent import futures

import grpc

# Proto imports (gen/ must be on sys.path — see __main__.py)
from common.v1 import common_pb2  # type: ignore[import-untyped]
from query.v1 import (
    query_pb2,  # type: ignore[import-untyped]
    query_pb2_grpc,  # type: ignore[import-untyped]
)

from rat_query import composition
from rat_query.arrow_ipc import columns_from_schema, table_to_ipc
from rat_query.catalog import NessieCatalog
from rat_query.config import (
    CompositionConfig,
    DuckDBConfig,
    NessieConfig,
    S3Config,
    UserDataPostgresConfig,
    engine_mode,
)
from rat_query.engine import QueryEngine, QueryTimeoutError, _validate_identifier

logger = logging.getLogger(__name__)


def _sanitize_error(error: str) -> str:
    """Sanitize error messages before returning to clients.

    Strips DuckDB internals (file paths, memory addresses, stack traces) to avoid
    leaking server-side details. The full error is always logged server-side before
    calling this function.
    """
    # Remove absolute file paths (Unix and Windows style)
    sanitized = re.sub(r"(/[^\s:]+\.(?:py|so|cpp|c|h|hpp|o|parquet|csv|json))", "<path>", error)
    # Remove memory addresses (0x7fff...)
    sanitized = re.sub(r"0x[0-9a-fA-F]{6,}", "<addr>", sanitized)
    # Remove DuckDB C++ source references (e.g., "src/something.cpp:123")
    sanitized = re.sub(r"src/[^\s]+\.[ch]pp:\d+", "<internal>", sanitized)
    # Remove stack trace lines
    sanitized = re.sub(r"(?m)^\s*File \".*\", line \d+.*$", "", sanitized)
    sanitized = re.sub(r"(?m)^\s*at .*$", "", sanitized)
    # Collapse multiple blank lines
    sanitized = re.sub(r"\n{3,}", "\n\n", sanitized)
    return sanitized.strip()


# Proto Layer enum → string
_LAYER_MAP: dict[int, str] = {
    common_pb2.LAYER_BRONZE: "bronze",
    common_pb2.LAYER_SILVER: "silver",
    common_pb2.LAYER_GOLD: "gold",
}


class QueryServiceImpl(query_pb2_grpc.QueryServiceServicer):
    """gRPC QueryService implementation.

    - ExecuteQuery: run SQL, return Arrow IPC
    - GetSchema: describe table columns + row count
    - PreviewTable: first N rows as Arrow IPC
    - ListTables: all tables from Nessie catalog
    """

    def __init__(
        self,
        s3_config: S3Config,
        nessie_config: NessieConfig,
        namespace: str = "default",
    ) -> None:
        self._s3_config = s3_config
        self._engine = QueryEngine(
            s3_config,
            DuckDBConfig.from_env(),
            UserDataPostgresConfig.from_env(),
        )
        self._catalog = NessieCatalog(nessie_config, s3_config, self._engine)
        self._namespace = namespace
        self._refresh_stop = threading.Event()
        # ADR-024 #12: when enabled, ExecuteQuery routes through engine/v1
        # (format-agnostic reads). GetSchema/PreviewTable/ListTables stay on the
        # local engine + Nessie catalog for now (follow-on).
        self._engine_mode = engine_mode()
        self._composition = CompositionConfig.from_env()
        if self._engine_mode:
            logger.info("ratq engine mode ON — ExecuteQuery routes through engine/v1")

        # Initial discovery + registration. register_all_tables enumerates
        # every namespace Nessie knows about; passing the constructor's
        # `namespace` as an extra ensures the default tenant is still
        # registered even on a fresh database where it hasn't shown up in
        # Nessie yet.
        try:
            self._catalog.register_all_tables(extra_namespaces=[namespace])
        except Exception:
            logger.exception("Initial catalog registration failed (will retry in background)")

        self._refresh_thread = threading.Thread(
            target=self._catalog.refresh_loop,
            args=(self._refresh_stop,),
            kwargs={"extra_namespaces": [namespace]},
            daemon=True,
            name="catalog-refresh",
        )
        self._refresh_thread.start()

    def ExecuteQuery(  # noqa: N802
        self,
        request: query_pb2.ExecuteQueryRequest,
        context: grpc.ServicerContext,
    ) -> query_pb2.ExecuteQueryResponse:
        sql = request.sql
        if not sql:
            context.set_code(grpc.StatusCode.INVALID_ARGUMENT)
            context.set_details("sql is required")
            return query_pb2.ExecuteQueryResponse()

        limit = request.limit if request.limit > 0 else 1000

        try:
            start = time.monotonic()
            if self._engine_mode:
                table = composition.run_query(sql, limit, self._composition, self._s3_config.bucket)
            else:
                table = self._engine.query_arrow(sql, limit)
            duration_ms = int((time.monotonic() - start) * 1000)
        except QueryTimeoutError as e:
            # Surface as DEADLINE_EXCEEDED so callers can distinguish a
            # timeout from a generic INTERNAL failure. The detail message
            # already includes the configured timeout — safe to surface
            # verbatim (no path/internal leakage).
            logger.warning("Query timed out: %s", e)
            context.set_code(grpc.StatusCode.DEADLINE_EXCEEDED)
            context.set_details(f"Query timed out: {e}")
            return query_pb2.ExecuteQueryResponse()
        except Exception as e:
            logger.error("Query execution failed: %s", e)
            context.set_code(grpc.StatusCode.INTERNAL)
            context.set_details(f"Query execution failed: {_sanitize_error(str(e))}")
            return query_pb2.ExecuteQueryResponse()

        columns = [
            query_pb2.ColumnMeta(name=name, type=type_str)
            for name, type_str in columns_from_schema(table.schema)
        ]

        return query_pb2.ExecuteQueryResponse(
            columns=columns,
            arrow_batch=table_to_ipc(table),
            total_rows=len(table),
            duration_ms=duration_ms,
        )

    def GetSchema(  # noqa: N802
        self,
        request: query_pb2.GetSchemaRequest,
        context: grpc.ServicerContext,
    ) -> query_pb2.GetSchemaResponse:
        layer = _LAYER_MAP.get(request.layer)
        if layer is None:
            context.set_code(grpc.StatusCode.INVALID_ARGUMENT)
            context.set_details(f"Invalid layer: {request.layer}")
            return query_pb2.GetSchemaResponse()

        name = request.table_name
        if not name:
            context.set_code(grpc.StatusCode.INVALID_ARGUMENT)
            context.set_details("table_name is required")
            return query_pb2.GetSchemaResponse()

        try:
            _validate_identifier(name, "table name")
        except ValueError as e:
            context.set_code(grpc.StatusCode.INVALID_ARGUMENT)
            context.set_details(str(e))
            return query_pb2.GetSchemaResponse()

        try:
            col_info = self._engine.describe_table(layer, name)
        except Exception as e:
            logger.error("describe_table failed for %s.%s: %s", layer, name, e)
            context.set_code(grpc.StatusCode.NOT_FOUND)
            context.set_details(f"Table not found: {layer}.{name}: {_sanitize_error(str(e))}")
            return query_pb2.GetSchemaResponse()

        try:
            row_count = self._engine.count_rows(layer, name)
        except Exception as e:
            logger.warning("count_rows failed for %s.%s: %s", layer, name, e)
            row_count = -1

        columns = [
            query_pb2.ColumnMeta(name=col_name, type=col_type) for col_name, col_type in col_info
        ]

        return query_pb2.GetSchemaResponse(
            columns=columns,
            row_count=row_count,
            size_bytes=0,
        )

    def PreviewTable(  # noqa: N802
        self,
        request: query_pb2.PreviewTableRequest,
        context: grpc.ServicerContext,
    ) -> query_pb2.PreviewTableResponse:
        layer = _LAYER_MAP.get(request.layer)
        if layer is None:
            context.set_code(grpc.StatusCode.INVALID_ARGUMENT)
            context.set_details(f"Invalid layer: {request.layer}")
            return query_pb2.PreviewTableResponse()

        name = request.table_name
        if not name:
            context.set_code(grpc.StatusCode.INVALID_ARGUMENT)
            context.set_details("table_name is required")
            return query_pb2.PreviewTableResponse()

        try:
            _validate_identifier(name, "table name")
        except ValueError as e:
            context.set_code(grpc.StatusCode.INVALID_ARGUMENT)
            context.set_details(str(e))
            return query_pb2.PreviewTableResponse()

        limit = request.limit if request.limit > 0 else 50

        try:
            sql = f'SELECT * FROM "{layer}"."{name}"'
            table = self._engine.query_arrow(sql, limit)
        except QueryTimeoutError as e:
            logger.warning("PreviewTable timed out for %s.%s: %s", layer, name, e)
            context.set_code(grpc.StatusCode.DEADLINE_EXCEEDED)
            context.set_details(f"Preview timed out: {e}")
            return query_pb2.PreviewTableResponse()
        except Exception as e:
            logger.error("PreviewTable failed for %s.%s: %s", layer, name, e)
            context.set_code(grpc.StatusCode.NOT_FOUND)
            context.set_details(f"Table not found: {layer}.{name}: {_sanitize_error(str(e))}")
            return query_pb2.PreviewTableResponse()

        columns = [
            query_pb2.ColumnMeta(name=col_name, type=col_type)
            for col_name, col_type in columns_from_schema(table.schema)
        ]

        return query_pb2.PreviewTableResponse(
            columns=columns,
            arrow_batch=table_to_ipc(table),
        )

    def ListTables(  # noqa: N802
        self,
        request: query_pb2.ListTablesRequest,
        context: grpc.ServicerContext,
    ) -> query_pb2.ListTablesResponse:
        # Empty namespace means "no filter — list every namespace". The old
        # code defaulted to self._namespace ("default"), which silently
        # hid every other namespace's tables from /api/v1/tables.
        namespace = request.namespace
        layer_filter = _LAYER_MAP.get(request.layer, "")

        tables = self._catalog.get_tables(namespace, layer_filter)

        # Row counts are omitted from ListTables to avoid N sequential
        # SELECT COUNT(*) queries (one per table). Clients should use
        # GetSchema for individual table row counts when needed.
        table_infos = []
        for t in tables:
            table_infos.append(
                query_pb2.TableInfo(
                    namespace=t.namespace,
                    layer=_str_to_layer(t.layer),
                    name=t.name,
                    row_count=0,
                    size_bytes=0,
                )
            )

        return query_pb2.ListTablesResponse(tables=table_infos)

    def shutdown(self) -> None:
        """Stop background threads and release resources."""
        self._refresh_stop.set()
        self._refresh_thread.join(timeout=5.0)
        self._engine.close()


def _str_to_layer(layer: str) -> int:
    """Convert layer string to proto Layer enum value."""
    return {
        "bronze": common_pb2.LAYER_BRONZE,
        "silver": common_pb2.LAYER_SILVER,
        "gold": common_pb2.LAYER_GOLD,
    }.get(layer, common_pb2.LAYER_UNSPECIFIED)


def _configure_server_port(server: grpc.Server, port: int) -> None:
    """Configure gRPC server port with optional TLS.

    Reads GRPC_TLS_CERT and GRPC_TLS_KEY env vars (file paths to PEM cert/key).
    If both are set, enables TLS via ssl_server_credentials.
    If neither is set, falls back to insecure port (backward compatible).
    Raises ValueError if only one of the two is set.
    """
    cert_path = os.environ.get("GRPC_TLS_CERT", "")
    key_path = os.environ.get("GRPC_TLS_KEY", "")

    if cert_path and key_path:
        with open(cert_path, "rb") as f:
            cert = f.read()
        with open(key_path, "rb") as f:
            key = f.read()
        creds = grpc.ssl_server_credentials([(key, cert)])
        server.add_secure_port(f"[::]:{port}", creds)
        logger.info("gRPC server listening on port %d (TLS enabled)", port)
    elif cert_path or key_path:
        raise ValueError("Both GRPC_TLS_CERT and GRPC_TLS_KEY must be set for TLS")
    else:
        server.add_insecure_port(f"[::]:{port}")
        logger.info("gRPC server listening on port %d (insecure)", port)


def serve(port: int = 50051) -> None:
    """Start the gRPC server."""
    s3_config = S3Config.from_env()
    nessie_config = NessieConfig.from_env()

    servicer = QueryServiceImpl(s3_config, nessie_config)
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=10))
    query_pb2_grpc.add_QueryServiceServicer_to_server(servicer, server)

    _configure_server_port(server, port)
    server.start()

    try:
        server.wait_for_termination()
    except KeyboardInterrupt:
        logger.info("Shutting down...")
    finally:
        servicer.shutdown()
        server.stop(grace=5)
