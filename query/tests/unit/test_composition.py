"""Unit tests for the engine-backed read path (ref extraction + security guards)."""

from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from rat_query import composition
from rat_query.bindings import BindingConfig

_BINDING = BindingConfig.single_default(engine_addr="e:1", catalog_addr="c:2", storage_addr="s:3")


class TestExtractRefs:
    def test_simple_three_part(self):
        assert composition.extract_refs("SELECT * FROM default.bronze.orders") == [
            ("default", "bronze", "orders")
        ]

    def test_quoted_three_part(self):
        assert composition.extract_refs('SELECT * FROM "default"."silver"."orders_doubled"') == [
            ("default", "silver", "orders_doubled")
        ]

    def test_join_collects_distinct_refs(self):
        sql = (
            "SELECT * FROM default.bronze.orders o "
            "JOIN shop.silver.items i ON o.id = i.id "
            "JOIN default.bronze.orders o2 ON o2.id = o.id"
        )
        assert composition.extract_refs(sql) == [
            ("default", "bronze", "orders"),
            ("shop", "silver", "items"),
        ]

    def test_ignores_two_part_refs(self):
        # 2-part (no namespace) is not resolvable yet — extractor returns nothing.
        assert composition.extract_refs("SELECT * FROM bronze.orders") == []

    def test_ignores_non_medallion_middle(self):
        assert composition.extract_refs("SELECT * FROM a.b.c") == []


class TestCheckSecurity:
    def test_allows_select(self):
        composition._check_security("SELECT * FROM default.bronze.orders")  # no raise

    def test_blocks_mutation(self):
        with pytest.raises(ValueError, match="Only SELECT"):
            composition._check_security("DELETE FROM default.bronze.orders")

    def test_blocks_file_functions(self):
        with pytest.raises(ValueError, match="Direct file/URL access"):
            composition._check_security("SELECT * FROM read_parquet('s3://x/y.parquet')")

    def test_blocks_overlong_query(self):
        with pytest.raises(ValueError, match="too long"):
            composition._check_security("SELECT 1 -- " + "x" * 200_000)


class TestListTables:
    def _patch_catalog(self, monkeypatch, *, namespaces, tables_by_ns):
        class FakeStub:
            def __init__(self, _channel):
                pass

            def ListNamespaces(self, _req):  # noqa: N802
                return SimpleNamespace(namespaces=namespaces)

            def ListTables(self, req):  # noqa: N802
                refs = [
                    SimpleNamespace(namespace=ns, layer=layer, name=name)
                    for ns, layer, name in tables_by_ns.get(req.namespace, [])
                ]
                return SimpleNamespace(tables=refs)

        @contextmanager
        def fake_channel(_addr):
            yield object()

        monkeypatch.setattr(composition.catalog_pb2_grpc, "CatalogServiceStub", FakeStub)
        monkeypatch.setattr(composition.grpc, "insecure_channel", fake_channel)

    def test_enumerates_all_namespaces(self, monkeypatch):
        self._patch_catalog(
            monkeypatch,
            namespaces=["default", "shop"],
            tables_by_ns={
                "default": [("default", 1, "orders")],
                "shop": [("shop", 2, "items")],
            },
        )
        assert composition.list_tables(_BINDING) == [
            ("default", "bronze", "orders"),
            ("shop", "silver", "items"),
        ]

    def test_single_namespace_skips_enumeration(self, monkeypatch):
        self._patch_catalog(
            monkeypatch,
            namespaces=["default", "shop"],  # should be ignored
            tables_by_ns={"default": [("default", 3, "revenue")]},
        )
        assert composition.list_tables(_BINDING, namespace="default") == [
            ("default", "gold", "revenue")
        ]
