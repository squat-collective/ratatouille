"""Tests for server — QueryServiceImpl gRPC service."""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import grpc
import pyarrow as pa
import pytest

# Proto imports
from common.v1 import common_pb2  # type: ignore[import-untyped]
from query.v1 import query_pb2  # type: ignore[import-untyped]

from rat_query.catalog import TableEntry
from rat_query.server import QueryServiceImpl, _configure_server_port, _str_to_layer


def _make_servicer() -> QueryServiceImpl:
    """Create a QueryServiceImpl with mocked engine and catalog."""
    with (
        patch("rat_query.server.QueryEngine") as MockEngine,
        patch("rat_query.server.NessieCatalog") as MockCatalog,
    ):
        mock_engine = MagicMock()
        MockEngine.return_value = mock_engine
        mock_catalog = MagicMock()
        MockCatalog.return_value = mock_catalog

        from rat_query.config import NessieConfig, S3Config

        servicer = QueryServiceImpl(
            s3_config=S3Config(),
            nessie_config=NessieConfig(),
            namespace="default",
        )
    return servicer


def test_engine_mode_skips_local_stack(monkeypatch):
    """In RAT_ENGINE_MODE the local DuckDB engine + Nessie catalog aren't created."""
    monkeypatch.setenv("RAT_ENGINE_MODE", "1")
    from rat_query.config import NessieConfig, S3Config

    with (
        patch("rat_query.server.QueryEngine") as MockEngine,
        patch("rat_query.server.NessieCatalog") as MockCatalog,
    ):
        servicer = QueryServiceImpl(
            s3_config=S3Config(), nessie_config=NessieConfig(), namespace="default"
        )

    assert servicer._engine is None
    assert servicer._catalog is None
    assert servicer._refresh_thread is None
    MockEngine.assert_not_called()
    MockCatalog.assert_not_called()


class TestExecuteQuery:
    def test_success(self):
        servicer = _make_servicer()
        table = pa.table({"x": [1, 2, 3]})
        servicer._engine.query_arrow.return_value = table

        ctx = MagicMock()
        req = query_pb2.ExecuteQueryRequest(sql="SELECT 1", limit=100)
        resp = servicer.ExecuteQuery(req, ctx)

        assert len(resp.columns) == 1
        assert resp.columns[0].name == "x"
        assert resp.total_rows == 3
        assert resp.duration_ms >= 0
        assert len(resp.arrow_batch) > 0
        ctx.set_code.assert_not_called()

    def test_empty_sql_returns_error(self):
        servicer = _make_servicer()
        ctx = MagicMock()
        req = query_pb2.ExecuteQueryRequest(sql="", limit=100)
        servicer.ExecuteQuery(req, ctx)

        ctx.set_code.assert_called()

    def test_default_limit(self):
        servicer = _make_servicer()
        table = pa.table({"x": [1]})
        servicer._engine.query_arrow.return_value = table

        ctx = MagicMock()
        req = query_pb2.ExecuteQueryRequest(sql="SELECT 1", limit=0)
        servicer.ExecuteQuery(req, ctx)

        # Should have been called with limit=1000
        args = servicer._engine.query_arrow.call_args
        assert args[0][1] == 1000

    def test_query_error(self):
        servicer = _make_servicer()
        servicer._engine.query_arrow.side_effect = Exception("DuckDB OOM")

        ctx = MagicMock()
        req = query_pb2.ExecuteQueryRequest(sql="SELECT * FROM huge_table", limit=100)
        servicer.ExecuteQuery(req, ctx)

        ctx.set_code.assert_called()

    def test_query_timeout_returns_deadline_exceeded(self):
        """When the engine raises QueryTimeoutError the server must surface
        DEADLINE_EXCEEDED (NOT generic INTERNAL) so clients can tell the
        difference between "your query is too slow" and "we crashed"."""
        from rat_query.engine import QueryTimeoutError

        servicer = _make_servicer()
        servicer._engine.query_arrow.side_effect = QueryTimeoutError("query exceeded 2s timeout")

        ctx = MagicMock()
        req = query_pb2.ExecuteQueryRequest(sql="SELECT * FROM huge_table", limit=100)
        servicer.ExecuteQuery(req, ctx)

        ctx.set_code.assert_called_once_with(grpc.StatusCode.DEADLINE_EXCEEDED)
        details_arg = ctx.set_details.call_args.args[0]
        assert "timed out" in details_arg.lower()
        assert "2s" in details_arg


class TestGetSchema:
    def test_success(self):
        servicer = _make_servicer()
        servicer._engine.describe_table.return_value = [("id", "INTEGER"), ("name", "VARCHAR")]
        servicer._engine.count_rows.return_value = 42

        ctx = MagicMock()
        req = query_pb2.GetSchemaRequest(
            namespace="default",
            layer=common_pb2.LAYER_SILVER,
            table_name="orders",
        )
        resp = servicer.GetSchema(req, ctx)

        assert len(resp.columns) == 2
        assert resp.columns[0].name == "id"
        assert resp.columns[0].type == "INTEGER"
        assert resp.row_count == 42
        ctx.set_code.assert_not_called()

    def test_not_found(self):
        servicer = _make_servicer()
        servicer._engine.describe_table.side_effect = Exception("Table not found")

        ctx = MagicMock()
        req = query_pb2.GetSchemaRequest(
            namespace="default",
            layer=common_pb2.LAYER_SILVER,
            table_name="missing",
        )
        servicer.GetSchema(req, ctx)

        ctx.set_code.assert_called()

    def test_invalid_layer(self):
        servicer = _make_servicer()
        ctx = MagicMock()
        req = query_pb2.GetSchemaRequest(
            namespace="default",
            layer=common_pb2.LAYER_UNSPECIFIED,
            table_name="orders",
        )
        servicer.GetSchema(req, ctx)

        ctx.set_code.assert_called()


class TestPreviewTable:
    def test_success(self):
        servicer = _make_servicer()
        table = pa.table({"id": [1, 2], "name": ["a", "b"]})
        servicer._engine.query_arrow.return_value = table

        ctx = MagicMock()
        req = query_pb2.PreviewTableRequest(
            namespace="default",
            layer=common_pb2.LAYER_SILVER,
            table_name="orders",
            limit=10,
        )
        resp = servicer.PreviewTable(req, ctx)

        assert len(resp.columns) == 2
        assert len(resp.arrow_batch) > 0
        ctx.set_code.assert_not_called()

    def test_default_limit(self):
        servicer = _make_servicer()
        table = pa.table({"x": [1]})
        servicer._engine.query_arrow.return_value = table

        ctx = MagicMock()
        req = query_pb2.PreviewTableRequest(
            namespace="default",
            layer=common_pb2.LAYER_SILVER,
            table_name="orders",
            limit=0,
        )
        servicer.PreviewTable(req, ctx)

        args = servicer._engine.query_arrow.call_args
        assert args[0][1] == 50

    def test_invalid_layer(self):
        servicer = _make_servicer()
        ctx = MagicMock()
        req = query_pb2.PreviewTableRequest(
            namespace="default",
            layer=common_pb2.LAYER_UNSPECIFIED,
            table_name="orders",
        )
        servicer.PreviewTable(req, ctx)

        ctx.set_code.assert_called()

    def test_table_not_found(self):
        servicer = _make_servicer()
        servicer._engine.query_arrow.side_effect = Exception("Table not found")

        ctx = MagicMock()
        req = query_pb2.PreviewTableRequest(
            namespace="default",
            layer=common_pb2.LAYER_SILVER,
            table_name="missing",
        )
        servicer.PreviewTable(req, ctx)

        ctx.set_code.assert_called()


class TestListTables:
    def test_success(self):
        servicer = _make_servicer()
        servicer._catalog.get_tables.return_value = [
            TableEntry(namespace="default", layer="silver", name="orders"),
            TableEntry(namespace="default", layer="gold", name="revenue"),
        ]

        ctx = MagicMock()
        req = query_pb2.ListTablesRequest(namespace="default")
        resp = servicer.ListTables(req, ctx)

        assert len(resp.tables) == 2
        assert resp.tables[0].name == "orders"
        assert resp.tables[0].layer == common_pb2.LAYER_SILVER
        # ListTables omits row counts to avoid N sequential COUNT(*) queries;
        # clients should use GetSchema for individual table row counts.
        assert resp.tables[0].row_count == 0

    def test_layer_filter(self):
        servicer = _make_servicer()
        servicer._catalog.get_tables.return_value = [
            TableEntry(namespace="default", layer="silver", name="orders"),
        ]

        ctx = MagicMock()
        req = query_pb2.ListTablesRequest(namespace="default", layer=common_pb2.LAYER_SILVER)
        resp = servicer.ListTables(req, ctx)

        assert len(resp.tables) == 1
        # Verify filter was passed to catalog
        servicer._catalog.get_tables.assert_called_with("default", "silver")

    def test_empty(self):
        servicer = _make_servicer()
        servicer._catalog.get_tables.return_value = []

        ctx = MagicMock()
        req = query_pb2.ListTablesRequest(namespace="default")
        resp = servicer.ListTables(req, ctx)

        assert len(resp.tables) == 0

    def test_row_count_always_zero(self):
        """ListTables omits per-table row counts to avoid N sequential COUNT(*) queries."""
        servicer = _make_servicer()
        servicer._catalog.get_tables.return_value = [
            TableEntry(namespace="default", layer="silver", name="orders"),
        ]

        ctx = MagicMock()
        req = query_pb2.ListTablesRequest(namespace="default")
        resp = servicer.ListTables(req, ctx)

        assert len(resp.tables) == 1
        assert resp.tables[0].row_count == 0
        # count_rows should NOT be called from ListTables
        servicer._engine.count_rows.assert_not_called()


class TestStrToLayer:
    def test_known_layers(self):
        assert _str_to_layer("bronze") == common_pb2.LAYER_BRONZE
        assert _str_to_layer("silver") == common_pb2.LAYER_SILVER
        assert _str_to_layer("gold") == common_pb2.LAYER_GOLD

    def test_unknown_returns_unspecified(self):
        assert _str_to_layer("unknown") == common_pb2.LAYER_UNSPECIFIED


class TestShutdown:
    def test_cleanup(self):
        servicer = _make_servicer()
        servicer.shutdown()

        servicer._engine.close.assert_called_once()
        assert servicer._refresh_stop.is_set()


class TestConfigureServerPort:
    def test_insecure_port_when_no_tls_env_vars(self):
        """serve() uses add_insecure_port when GRPC_TLS_CERT and GRPC_TLS_KEY are unset."""
        server = MagicMock(spec=grpc.Server)
        env = {k: v for k, v in os.environ.items() if k not in ("GRPC_TLS_CERT", "GRPC_TLS_KEY")}
        with patch.dict(os.environ, env, clear=True):
            _configure_server_port(server, 50051)
        server.add_insecure_port.assert_called_once_with("[::]:50051")
        server.add_secure_port.assert_not_called()

    def test_raises_when_only_cert_set(self):
        """serve() raises ValueError when GRPC_TLS_CERT is set but GRPC_TLS_KEY is not."""
        server = MagicMock(spec=grpc.Server)
        env = {k: v for k, v in os.environ.items() if k not in ("GRPC_TLS_CERT", "GRPC_TLS_KEY")}
        env["GRPC_TLS_CERT"] = "/path/to/cert.pem"
        with patch.dict(os.environ, env, clear=True):
            with pytest.raises(ValueError, match="Both GRPC_TLS_CERT and GRPC_TLS_KEY must be set"):
                _configure_server_port(server, 50051)

    def test_raises_when_only_key_set(self):
        """serve() raises ValueError when GRPC_TLS_KEY is set but GRPC_TLS_CERT is not."""
        server = MagicMock(spec=grpc.Server)
        env = {k: v for k, v in os.environ.items() if k not in ("GRPC_TLS_CERT", "GRPC_TLS_KEY")}
        env["GRPC_TLS_KEY"] = "/path/to/key.pem"
        with patch.dict(os.environ, env, clear=True):
            with pytest.raises(ValueError, match="Both GRPC_TLS_CERT and GRPC_TLS_KEY must be set"):
                _configure_server_port(server, 50051)

    def test_secure_port_when_both_tls_vars_set(self, tmp_path):
        """serve() uses add_secure_port with ssl_server_credentials when both TLS env vars are set."""
        cert_file = tmp_path / "cert.pem"
        key_file = tmp_path / "key.pem"
        cert_file.write_bytes(b"FAKE-CERT-DATA")
        key_file.write_bytes(b"FAKE-KEY-DATA")

        server = MagicMock(spec=grpc.Server)
        env = {k: v for k, v in os.environ.items() if k not in ("GRPC_TLS_CERT", "GRPC_TLS_KEY")}
        env["GRPC_TLS_CERT"] = str(cert_file)
        env["GRPC_TLS_KEY"] = str(key_file)
        with (
            patch.dict(os.environ, env, clear=True),
            patch("rat_query.server.grpc.ssl_server_credentials") as mock_ssl,
        ):
            mock_creds = MagicMock()
            mock_ssl.return_value = mock_creds
            _configure_server_port(server, 50051)

        mock_ssl.assert_called_once_with([(b"FAKE-KEY-DATA", b"FAKE-CERT-DATA")])
        server.add_secure_port.assert_called_once_with("[::]:50051", mock_creds)
        server.add_insecure_port.assert_not_called()
