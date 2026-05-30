"""FormatAdapter contract — what a format plugin gives the duckdb engine (ADR-024).

The engine itself knows nothing about iceberg or ducklake. At startup it discovers
FormatAdapter implementations (via the `RAT_FORMAT_ADAPTERS` env var for dev/test
or the `rat_engine_duckdb.format_adapters` entry-point group for installed
plugins), then dispatches per `descriptor.format`:

  * inputs → the adapter for `input.format` registers a view in DuckDB
  * outputs → the adapter for `output.format` runs the transform + writes

Adding a new format = a new plugin that implements this Protocol. No engine edit.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    import pyarrow as pa

    from rat_engine_duckdb.duckdb_engine import DuckDBEngine


@runtime_checkable
class FormatAdapter(Protocol):
    """A single-format extension for the duckdb engine.

    Adapters get the full ``DuckDBEngine`` (not just the bare conn) so they can
    use ``engine.query_arrow`` + invoke ``execute_python_pipeline`` for the SQL /
    python execution side of ``execute_write``. The format-specific reads/writes
    happen on ``engine.conn`` directly.
    """

    @property
    def name(self) -> str:
        """The format identifier — must match `TableDescriptor.format` on the wire."""

    def supported_strategies(self) -> set[str]:
        """Universal strategy NAMEs (e.g. ``{"full_refresh", "incremental"}``) this adapter implements."""  # noqa: E501

    def register_input(self, engine: DuckDBEngine, descriptor: Any) -> None:
        """Bind ``descriptor`` as a queryable view on ``engine.conn`` (2-part + 3-part names)."""

    def execute_write(self, engine: DuckDBEngine, request: Any) -> tuple[int, pa.Schema]:
        """Run the transform (request.source) and write to request.output.

        Returns (rows_written, output_schema). May raise on unsupported language /
        unknown strategy / write failure.
        """
