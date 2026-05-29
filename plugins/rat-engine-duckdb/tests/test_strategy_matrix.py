"""Unit tests for the (engine=duckdb, format) -> strategy matrix (ADR-024 #11). Pure."""

import pytest

from rat_engine_duckdb import strategy_matrix


def test_iceberg_supports_all_six():
    assert strategy_matrix.supported_strategies("iceberg") == [
        "append_only",
        "delete_insert",
        "full_refresh",
        "incremental",
        "scd2",
        "snapshot",
    ]


def test_ducklake_supports_full_materialize_only():
    assert strategy_matrix.supported_strategies("ducklake") == ["full_refresh", "snapshot"]


def test_unknown_format_supports_nothing():
    assert strategy_matrix.supported_strategies("delta") == []


@pytest.mark.parametrize(
    ("fmt", "name", "expected"),
    [
        ("iceberg", "scd2", True),
        ("iceberg", "nope", False),
        ("ducklake", "full_refresh", True),
        ("ducklake", "scd2", False),
        ("delta", "full_refresh", False),
    ],
)
def test_format_supports(fmt: str, name: str, expected: bool):
    assert strategy_matrix.format_supports(fmt, name) is expected


def test_require_supported_passes_for_known():
    strategy_matrix.require_supported("iceberg", "incremental")  # no raise
    strategy_matrix.require_supported("ducklake", "snapshot")  # no raise


def test_require_supported_raises_with_supported_list():
    with pytest.raises(strategy_matrix.UnknownStrategyError) as exc:
        strategy_matrix.require_supported("ducklake", "scd2")
    err = exc.value
    assert err.format == "ducklake"
    assert err.strategy == "scd2"
    assert err.supported == ["full_refresh", "snapshot"]
    assert "ducklake" in str(err) and "scd2" in str(err)


def test_require_supported_unknown_format_raises():
    with pytest.raises(strategy_matrix.UnknownStrategyError):
        strategy_matrix.require_supported("delta", "full_refresh")
