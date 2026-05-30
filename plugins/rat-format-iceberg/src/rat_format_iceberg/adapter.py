"""IcebergFormatAdapter — the (duckdb, iceberg) FormatAdapter implementation.

Bridges the engine's per-adapter contract to this plugin's iceberg internals:
  * register_input → iceberg_scan view via PyIceberg's RestCatalog
  * execute_write  → compile/execute the transform, then dispatch to the
                     strategy recipe (full_refresh/incremental/scd2/…)
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from rat_engine_duckdb.adapters import (
    layer_name,
    nessie_config_from_catalog,
    pipeline_config_from_options,
    s3_config_from_storage,
    table_identifier,
    table_location,
)
from rat_engine_duckdb.python_exec import execute_python_pipeline
from rat_engine_duckdb.sql_utils import _configure_s3, _escape_sql_string
from rat_engine_duckdb.views import register_views

from rat_format_iceberg import strategy_matrix
from rat_format_iceberg.iceberg import get_catalog
from rat_format_iceberg.recipes import iceberg_recipe

if TYPE_CHECKING:
    import pyarrow as pa
    from rat_engine_duckdb.duckdb_engine import DuckDBEngine


class IcebergFormatAdapter:
    """The Iceberg adapter — wraps PyIceberg + the runner-side recipes."""

    name: str = "iceberg"

    def supported_strategies(self) -> set[str]:
        return set(strategy_matrix.SUPPORTED)

    def register_input(self, engine: DuckDBEngine, descriptor: Any) -> None:
        """Resolve the input to its current Iceberg metadata + register iceberg_scan views."""
        s3 = s3_config_from_storage(descriptor.storage)
        nessie = nessie_config_from_catalog(descriptor.catalog)
        branch = descriptor.catalog.branch or "main"
        _configure_s3(engine.conn, s3)
        catalog = get_catalog(s3, nessie, branch)
        table = catalog.load_table(table_identifier(descriptor))
        scan = f"SELECT * FROM iceberg_scan('{_escape_sql_string(table.metadata_location)}')"
        register_views(
            engine.conn,
            descriptor.ref.namespace,
            layer_name(descriptor.ref.layer),
            descriptor.ref.name,
            scan,
        )

    def execute_write(self, engine: DuckDBEngine, request: Any) -> tuple[int, pa.Schema]:
        """Compile SQL or run python → Arrow table; then dispatch to the strategy recipe."""
        out = request.output
        language = request.language or "sql"
        if language not in ("sql", "python"):
            raise RuntimeError(f"unsupported language {language!r}")
        # Universal strategy NAME → (duckdb, iceberg) recipe; raises UnknownStrategyError
        # (listing supported names) for one iceberg doesn't implement.
        strategy = iceberg_recipe(request.strategy or "full_refresh")

        s3 = s3_config_from_storage(out.storage)
        nessie = nessie_config_from_catalog(out.catalog)
        branch = out.catalog.branch or "main"
        cfg = pipeline_config_from_options(request.options, request.strategy or "full_refresh")
        # Lakekeeper assigns table locations under its own warehouse prefix and rejects
        # an explicit one; Nessie expects the runner-provided location.
        location = "" if out.catalog.protocol == "lakekeeper" else table_location(out)

        if language == "sql":
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
            location,
            cfg,
            branch=branch,
            conn=engine.conn,
        )
        return rows, result.schema
