"""Data-plane bindings: which (storage, catalog, engine) composition a pipeline uses.

Decoupled data architecture (ADR-024). `rat.yaml` declares named `data_planes` and
`bindings`; resolution is most-specific-wins: pipeline -> layer -> namespace -> default.
With no binding file, a single `default` plane is built from the ENGINE/CATALOG/
STORAGE_ADDR env, preserving the one-line-deploy default bundle.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


@dataclass(frozen=True)
class DataPlane:
    """A named composition: engine + catalog + storage services + table format."""

    name: str
    engine_addr: str
    catalog_addr: str
    storage_addr: str
    format: str = "iceberg"
    catalog_protocol: str = "iceberg-rest"  # iceberg-rest (Nessie) | lakekeeper | ducklake
    supports_branching: bool = True  # Nessie branches; lakekeeper/ducklake do not


@dataclass(frozen=True)
class BindingConfig:
    """Resolves (namespace, layer, pipeline) -> DataPlane, most-specific-wins."""

    data_planes: dict[str, DataPlane]
    default_plane: str
    by_namespace: dict[str, str]
    by_layer: dict[str, str]  # key: "namespace.layer"
    by_pipeline: dict[str, str]  # key: "namespace.layer.name"

    def resolve(self, namespace: str, layer: str, pipeline: str) -> DataPlane:
        """Return the bound DataPlane (pipeline > layer > namespace > default)."""
        name = (
            self.by_pipeline.get(f"{namespace}.{layer}.{pipeline}")
            or self.by_layer.get(f"{namespace}.{layer}")
            or self.by_namespace.get(namespace)
            or self.default_plane
        )
        plane = self.data_planes.get(name)
        if plane is None:
            raise KeyError(f"binding refers to unknown data_plane '{name}'")
        return plane

    @classmethod
    def single_default(
        cls, *, engine_addr: str, catalog_addr: str, storage_addr: str, fmt: str = "iceberg"
    ) -> BindingConfig:
        """The implicit one-plane config used when no binding file is present."""
        plane = DataPlane("default", engine_addr, catalog_addr, storage_addr, fmt)
        return cls({"default": plane}, "default", {}, {}, {})

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> BindingConfig:
        """Parse the `data_planes` + `bindings` sections of a rat.yaml-shaped dict."""
        planes: dict[str, DataPlane] = {}
        for name, spec in (raw.get("data_planes") or {}).items():
            planes[name] = DataPlane(
                name=name,
                engine_addr=spec["engine"],
                catalog_addr=spec["catalog"],
                storage_addr=spec["storage"],
                format=spec.get("format", "iceberg"),
                catalog_protocol=spec.get("catalog_protocol", "iceberg-rest"),
                supports_branching=spec.get("supports_branching", True),
            )
        bindings = raw.get("bindings") or {}
        default_plane = bindings.get("default", "default")
        if default_plane not in planes:
            raise ValueError(f"default binding '{default_plane}' is not a declared data_plane")
        return cls(
            data_planes=planes,
            default_plane=default_plane,
            by_namespace=dict(bindings.get("namespaces") or {}),
            by_layer=dict(bindings.get("layers") or {}),
            by_pipeline=dict(bindings.get("pipelines") or {}),
        )

    @classmethod
    def load(
        cls, path: str | None, *, engine_addr: str, catalog_addr: str, storage_addr: str
    ) -> BindingConfig:
        """Load from a YAML file if it declares data_planes; else build a single default."""
        if path:
            file = Path(path)
            if file.is_file():
                raw = yaml.safe_load(file.read_text()) or {}
                if raw.get("data_planes"):
                    return cls.from_dict(raw)
        return cls.single_default(
            engine_addr=engine_addr, catalog_addr=catalog_addr, storage_addr=storage_addr
        )
