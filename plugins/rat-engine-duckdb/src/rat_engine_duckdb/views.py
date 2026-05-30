"""Shared view registration — bind an input under both 2-part and 3-part names.

Every format adapter calls this once per input so the executor's compiled SQL
binds whether it references the input as ``"layer"."name"`` (runner's logical
refs) or ``"ns"."layer"."name"`` (ratq's full refs).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from rat_engine_duckdb.sql_utils import quote_identifier

if TYPE_CHECKING:
    import duckdb


def register_views(
    conn: duckdb.DuckDBPyConnection,
    namespace: str,
    layer_name: str,
    table_name: str,
    source_expr: str,
) -> None:
    """Bind ``source_expr`` under both 2-part and 3-part view names on ``conn``.

    The :memory: catalog mirrors how the query service registers its own views.
    """
    layer = quote_identifier(layer_name)
    name = quote_identifier(table_name)
    conn.execute(f"CREATE SCHEMA IF NOT EXISTS {layer}")
    conn.execute(f"CREATE OR REPLACE VIEW {layer}.{name} AS {source_expr}")
    if namespace:
        nsq = quote_identifier(namespace)
        conn.execute(f"ATTACH IF NOT EXISTS ':memory:' AS {nsq}")
        conn.execute(f"CREATE SCHEMA IF NOT EXISTS {nsq}.{layer}")
        conn.execute(f"CREATE OR REPLACE VIEW {nsq}.{layer}.{name} AS {source_expr}")
