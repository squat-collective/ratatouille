"""DuckLake format-adapter for the DuckDB engine (ADR-024).

DuckLake (https://ducklake.select) is a lakehouse format with a SQL-database
catalog + Parquet storage, accessed entirely through DuckDB's `ducklake`
extension. This adapter ATTACHes the lake described by the CatalogDescriptor and
runs the transform as plain SQL — DuckLake itself handles the Parquet writes and
the transactional metadata commit. It proves the (engine=duckdb, format=ducklake)
composition runs on the SAME engine that serves iceberg, selected purely by the
descriptor's `format`.

The connection is expected to already have httpfs + S3 configured (DuckDBEngine
does this); we add the ducklake + postgres extensions and ATTACH on top.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from rat_engine_duckdb.formats.iceberg import _escape_sql_string, _quote_identifier

if TYPE_CHECKING:
    import pyarrow as pa

_LAYER_NAMES = {1: "bronze", 2: "silver", 3: "gold"}

# Strategies the initial adapter supports (both = full materialization). Real
# incremental/scd2/delete_insert via DuckLake MERGE/UPDATE/DELETE is a follow-on.
_FULL_MATERIALIZE = frozenset({"full_refresh", "snapshot"})


def _attach_lake(conn: Any, catalog: Any, storage: Any) -> None:
    """Load the ducklake + postgres extensions and ATTACH the lake as `lake`."""
    data_path = catalog.options.get("data_path") or f"s3://{storage.s3.bucket}/ducklake/"
    conn.execute("INSTALL ducklake; LOAD ducklake;")
    conn.execute("INSTALL postgres; LOAD postgres;")
    target = _escape_sql_string(f"ducklake:{catalog.uri}")
    conn.execute(f"ATTACH '{target}' AS lake (DATA_PATH '{_escape_sql_string(data_path)}')")
    conn.execute("USE lake")


def execute_ducklake(conn: Any, request: Any) -> tuple[int, pa.Schema]:
    """Materialize the output table in the attached DuckLake; return (rows, schema).

    Inputs are not separately registered: they already live in the attached lake
    (resolved by `USE lake`), so the compiled SQL's "layer"."name" refs bind there.
    """
    out = request.output
    if request.language != "sql":
        raise RuntimeError(f"ducklake adapter supports 'sql' only; got {request.language!r}")
    strategy = request.strategy or "full_refresh"
    if strategy not in _FULL_MATERIALIZE:
        raise RuntimeError(
            f"ducklake adapter: strategy {strategy!r} not implemented "
            "(full_refresh/snapshot only for now)"
        )

    _attach_lake(conn, out.catalog, out.storage)
    layer = _quote_identifier(_LAYER_NAMES.get(out.ref.layer, "main"))
    name = _quote_identifier(out.ref.name)
    conn.execute(f"CREATE SCHEMA IF NOT EXISTS {layer}")
    conn.execute(f"CREATE OR REPLACE TABLE {layer}.{name} AS {request.source}")

    rows = conn.execute(f"SELECT count(*) FROM {layer}.{name}").fetchone()[0]
    schema = conn.execute(f"SELECT * FROM {layer}.{name} LIMIT 0").arrow().schema
    return int(rows), schema
