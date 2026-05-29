"""Unit tests for plan-time composition capability matching (ADR-024)."""

import pytest

from rat_runner.bindings import DataPlane
from rat_runner.capabilities import AxisCapabilities, CompositionError, check_composition


def _plane(
    fmt: str = "iceberg", protocol: str = "iceberg-rest", branching: bool = True
) -> DataPlane:
    return DataPlane(
        name="default",
        engine_addr="engine:50081",
        catalog_addr="catalog:50082",
        storage_addr="storage:50083",
        format=fmt,
        catalog_protocol=protocol,
        supports_branching=branching,
    )


def _caps(
    engine_formats: list[str] | None = None,
    engine_languages: list[str] | None = None,
    catalog_formats: list[str] | None = None,
    catalog_capabilities: list[str] | None = None,
    storage_schemes: list[str] | None = None,
) -> AxisCapabilities:
    return AxisCapabilities(
        engine_formats=engine_formats if engine_formats is not None else ["iceberg", "ducklake"],
        engine_languages=engine_languages if engine_languages is not None else ["sql", "python"],
        catalog_formats=catalog_formats if catalog_formats is not None else ["iceberg"],
        catalog_capabilities=(
            catalog_capabilities if catalog_capabilities is not None else ["branching", "history"]
        ),
        storage_schemes=storage_schemes if storage_schemes is not None else ["s3"],
    )


def test_valid_iceberg_nessie_branching_passes():
    check_composition(_plane(), _caps())  # no raise


def test_valid_ducklake_non_branching_passes():
    check_composition(
        _plane(fmt="ducklake", protocol="ducklake", branching=False),
        _caps(catalog_formats=["ducklake"], catalog_capabilities=["time_travel"]),
    )  # no raise


def test_engine_missing_format_raises():
    with pytest.raises(CompositionError, match="engine does not support format"):
        check_composition(_plane(fmt="delta"), _caps(catalog_formats=["delta"]))


def test_catalog_missing_format_raises():
    with pytest.raises(CompositionError, match="catalog does not manage format"):
        check_composition(_plane(), _caps(catalog_formats=["ducklake"]))


def test_declared_branching_but_catalog_cannot_raises():
    with pytest.raises(CompositionError, match="supports_branching=true but catalog"):
        check_composition(_plane(branching=True), _caps(catalog_capabilities=["history"]))


def test_non_branching_binding_skips_branching_check():
    # Catalog lacks 'branching' but the binding doesn't ask for it → fine.
    check_composition(_plane(branching=False), _caps(catalog_capabilities=["history"]))


def test_storage_scheme_missing_raises():
    with pytest.raises(CompositionError, match="storage does not serve scheme"):
        check_composition(_plane(), _caps(storage_schemes=["gcs"]))


def test_language_mismatch_raises_when_provided():
    with pytest.raises(CompositionError, match="cannot execute language 'python'"):
        check_composition(_plane(), _caps(engine_languages=["sql"]), language="python")


def test_language_none_skips_language_check():
    check_composition(_plane(), _caps(engine_languages=["sql"]), language=None)


def test_aggregates_multiple_errors():
    with pytest.raises(CompositionError) as exc:
        check_composition(
            _plane(fmt="delta", branching=True),
            _caps(
                engine_formats=["iceberg"],
                catalog_formats=["iceberg"],
                catalog_capabilities=["history"],
                storage_schemes=["gcs"],
            ),
            language="cypher",
        )
    msg = str(exc.value)
    assert "engine does not support format" in msg
    assert "catalog does not manage format" in msg
    assert "supports_branching=true" in msg
    assert "storage does not serve scheme" in msg
    assert "cannot execute language" in msg
