"""Merge-strategy matrix for the (duckdb, ducklake) adapter."""

from __future__ import annotations

FORMAT = "ducklake"
SUPPORTED: frozenset[str] = frozenset({"full_refresh", "snapshot"})


class UnknownStrategyError(Exception):
    """A strategy NAME is not implemented for ducklake."""

    def __init__(self, name: str) -> None:
        self.strategy = name
        super().__init__(
            f"engine has no {name!r} strategy for format {FORMAT!r} "
            f"(supported: {sorted(SUPPORTED)})"
        )


def supported_strategies() -> list[str]:
    return sorted(SUPPORTED)


def require_supported(name: str) -> None:
    if name not in SUPPORTED:
        raise UnknownStrategyError(name)
