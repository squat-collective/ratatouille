"""Unit tests for logical-ref SQL compilation (decoupled engine path; no catalog I/O)."""

from types import SimpleNamespace

from rat_runner.templating import compile_sql

_S3 = SimpleNamespace(bucket="rat")
_NESSIE = SimpleNamespace(base_url="http://nessie:19120/iceberg")


def test_logical_refs_emit_qualified_view_names():
    sql = (
        "SELECT * FROM {{ ref('silver.orders') }} o JOIN {{ ref('shop.bronze.raw') }} r USING (id)"
    )
    out = compile_sql(sql, "shop", "gold", "report", _S3, _NESSIE, logical_refs=True)
    assert '"silver"."orders"' in out
    assert '"bronze"."raw"' in out
    assert "iceberg_scan" not in out  # logical mode does no catalog resolution


def test_logical_this_resolves_to_target_view():
    out = compile_sql(
        "SELECT * FROM {{ this }}", "shop", "gold", "report", _S3, _NESSIE, logical_refs=True
    )
    assert '"gold"."report"' in out
