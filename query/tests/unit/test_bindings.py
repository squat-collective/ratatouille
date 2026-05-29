"""Unit tests for ratq data-plane binding resolution (ADR-024)."""

import pytest

from rat_query.bindings import BindingConfig

_RAW = {
    "data_planes": {
        "default": {"engine": "e:1", "catalog": "cat:1", "storage": "s:1"},
        "lake": {
            "engine": "e:1",
            "catalog": "cat-dl:1",
            "storage": "s:1",
            "format": "ducklake",
            "catalog_protocol": "ducklake",
            "supports_branching": False,
        },
    },
    "bindings": {"default": "default", "namespaces": {"lakehouse": "lake"}},
}


def test_single_default_resolves_everything_to_default():
    b = BindingConfig.single_default(engine_addr="e:1", catalog_addr="c:1", storage_addr="s:1")
    assert b.resolve("anything").name == "default"
    assert b.resolve("x", "bronze", "y").catalog_addr == "c:1"


def test_namespace_binding_wins_over_default():
    b = BindingConfig.from_dict(_RAW)
    assert b.resolve("lakehouse").name == "lake"
    assert b.resolve("lakehouse").catalog_addr == "cat-dl:1"
    assert b.resolve("lakehouse").format == "ducklake"
    # Unbound namespace falls back to the default plane.
    assert b.resolve("default").name == "default"
    assert b.resolve("other_ns").name == "default"


def test_unknown_default_plane_rejected():
    with pytest.raises(ValueError, match="default binding"):
        BindingConfig.from_dict({"data_planes": {}, "bindings": {"default": "missing"}})


def test_resolve_unknown_plane_raises():
    b = BindingConfig.from_dict(
        {
            "data_planes": {"default": {"engine": "e", "catalog": "c", "storage": "s"}},
            "bindings": {"default": "default", "namespaces": {"ns": "ghost"}},
        }
    )
    with pytest.raises(KeyError, match="ghost"):
        b.resolve("ns")
