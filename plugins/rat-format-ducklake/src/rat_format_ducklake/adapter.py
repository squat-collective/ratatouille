"""DuckLakeFormatAdapter — the (duckdb, ducklake) FormatAdapter implementation.

DuckLake's metadata is a SQL database (Postgres by default) accessed via the
DuckDB `ducklake` extension; data is Parquet on object storage. Reads and writes
both happen through ATTACH + plain SQL — there's no separate Arrow pipeline.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from rat_engine_duckdb.adapters import layer_name
from rat_engine_duckdb.sql_utils import _quote_identifier
from rat_engine_duckdb.views import register_views

from rat_format_ducklake import strategy_matrix
from rat_format_ducklake.ducklake import attach_lake, execute_ducklake

if TYPE_CHECKING:
    import pyarrow as pa
    from rat_engine_duckdb.duckdb_engine import DuckDBEngine


class DuckLakeFormatAdapter:
    """The DuckLake adapter — ATTACH lake + register views over `lake.layer.name`."""

    name: str = "ducklake"

    def supported_strategies(self) -> set[str]:
        return set(strategy_matrix.SUPPORTED)

    def register_input(self, engine: DuckDBEngine, descriptor: Any) -> None:
        """ATTACH the lake once + register the view sourcing `lake.<layer>.<name>`."""
        attach_lake(engine.conn, descriptor.catalog, descriptor.storage, use=False)
        layer = _quote_identifier(layer_name(descriptor.ref.layer))
        name = _quote_identifier(descriptor.ref.name)
        register_views(
            engine.conn,
            descriptor.ref.namespace,
            layer_name(descriptor.ref.layer),
            descriptor.ref.name,
            f"SELECT * FROM lake.{layer}.{name}",
        )

    def execute_write(self, engine: DuckDBEngine, request: Any) -> tuple[int, pa.Schema]:
        """Delegate to execute_ducklake (ATTACH + CREATE OR REPLACE TABLE AS source)."""
        return execute_ducklake(engine.conn, request)
