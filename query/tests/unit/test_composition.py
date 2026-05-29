"""Unit tests for the engine-backed read path (ref extraction + security guards)."""

import pytest

from rat_query import composition


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
