"""Unit tests for data-plane binding resolution (pure; no gen/infra needed)."""

import pytest

from rat_runner.bindings import BindingConfig, DataPlane


def _cfg() -> BindingConfig:
    planes = {
        "default": DataPlane("default", "e1", "c1", "s1"),
        "fast": DataPlane("fast", "e2", "c2", "s2", "native"),
    }
    return BindingConfig(
        data_planes=planes,
        default_plane="default",
        by_namespace={"analytics": "fast"},
        by_layer={"shop.bronze": "fast", "shop.silver": "fast"},
        by_pipeline={"shop.silver.orders": "default"},
    )


def test_resolve_falls_back_to_default():
    assert _cfg().resolve("misc", "silver", "thing").name == "default"


def test_resolve_by_namespace():
    assert _cfg().resolve("analytics", "gold", "kpis").name == "fast"


def test_resolve_by_layer():
    assert _cfg().resolve("shop", "bronze", "raw").name == "fast"


def test_resolve_pipeline_beats_layer():
    # shop.silver is bound to 'fast', but the orders pipeline overrides to 'default'.
    assert _cfg().resolve("shop", "silver", "orders").name == "default"
    assert _cfg().resolve("shop", "silver", "other").name == "fast"


def test_single_default_used_without_a_binding_file():
    cfg = BindingConfig.single_default(engine_addr="e", catalog_addr="c", storage_addr="s")
    plane = cfg.resolve("any", "bronze", "x")
    assert (plane.engine_addr, plane.format) == ("e", "iceberg")


def test_from_dict_parses_planes_and_bindings():
    cfg = BindingConfig.from_dict(
        {
            "data_planes": {"default": {"engine": "e", "catalog": "c", "storage": "s"}},
            "bindings": {"default": "default", "namespaces": {"shop": "default"}},
        }
    )
    assert cfg.resolve("shop", "silver", "orders").catalog_addr == "c"


def test_unknown_plane_raises():
    cfg = BindingConfig(
        {"default": DataPlane("default", "e", "c", "s")}, "default", {"x": "missing"}, {}, {}
    )
    with pytest.raises(KeyError):
        cfg.resolve("x", "layer", "pipe")
