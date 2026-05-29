"""The (engine=duckdb, format) -> merge-strategy matrix (ADR-024 #11).

Strategies travel the wire as a universal NAME ("full_refresh", "incremental",
...); which names are valid depends on the *format* the engine is writing. This
module is the single, import-light source of truth for that matrix, so a new
format (or a new strategy on an existing format) is declared in exactly one place
and both the iceberg and ducklake adapters validate against the same table.

Kept free of DuckDB/PyIceberg imports on purpose — it's pure data + checks, so it
loads (and unit-tests) without the heavy engine runtime.
"""

from __future__ import annotations

# format -> the strategy NAMES the (duckdb, format) pair implements.
# iceberg has the full six; ducklake currently materializes (full_refresh/snapshot
# both = full overwrite) — incremental/scd2/delete_insert via DuckLake MERGE is a
# follow-on. A third-party format adds one entry here.
FORMAT_STRATEGIES: dict[str, frozenset[str]] = {
    "iceberg": frozenset(
        {"full_refresh", "incremental", "append_only", "delete_insert", "scd2", "snapshot"}
    ),
    "ducklake": frozenset({"full_refresh", "snapshot"}),
}


class UnknownStrategyError(Exception):
    """A strategy NAME is not implemented for the engine's output format."""

    def __init__(self, fmt: str, name: str, supported: list[str]) -> None:
        self.format = fmt
        self.strategy = name
        self.supported = supported
        super().__init__(
            f"engine has no {name!r} strategy for format {fmt!r} "
            f"(supported: {supported or ['<none>']})"
        )


def supported_strategies(fmt: str) -> list[str]:
    """Sorted strategy names the (duckdb, fmt) pair supports (empty for an unknown format)."""
    return sorted(FORMAT_STRATEGIES.get(fmt, frozenset()))


def format_supports(fmt: str, name: str) -> bool:
    """True if the (duckdb, fmt) pair implements strategy ``name``."""
    return name in FORMAT_STRATEGIES.get(fmt, frozenset())


def require_supported(fmt: str, name: str) -> None:
    """Raise ``UnknownStrategyError`` unless the (duckdb, fmt) pair supports ``name``."""
    if not format_supports(fmt, name):
        raise UnknownStrategyError(fmt, name, supported_strategies(fmt))
