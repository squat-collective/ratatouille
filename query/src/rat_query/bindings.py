"""Data-plane bindings for ratq reads (ADR-024).

Mirrors runner/src/rat_runner/bindings.py — the SAME `rat.yaml` `data_planes` +
`bindings` shape, so one binding file configures both services. ratq resolves a
table ref's namespace to its composition (engine + catalog + storage) so a query
can read across compositions, not just a single default. Keep this aligned with
the runner's version (a shared package was deferred — see config.py's note).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class DataPlane:
    """A named composition: engine + catalog + storage services + table format."""

    name: str
    engine_addr: str
    catalog_addr: str
    storage_addr: str
    format: str = "iceberg"
    catalog_protocol: str = "iceberg-rest"  # iceberg-rest (Nessie) | lakekeeper | ducklake
    supports_branching: bool = True


@dataclass(frozen=True)
class BindingConfig:
    """Resolves (namespace, layer, name) -> DataPlane, most-specific-wins."""

    data_planes: dict[str, DataPlane]
    default_plane: str
    by_namespace: dict[str, str]
    by_layer: dict[str, str]  # key: "namespace.layer"
    by_pipeline: dict[str, str]  # key: "namespace.layer.name"

    def resolve(self, namespace: str, layer: str = "", name: str = "") -> DataPlane:
        """Return the bound DataPlane (full ref > layer > namespace > default)."""
        plane_name = (
            self.by_pipeline.get(f"{namespace}.{layer}.{name}")
            or self.by_layer.get(f"{namespace}.{layer}")
            or self.by_namespace.get(namespace)
            or self.default_plane
        )
        plane = self.data_planes.get(plane_name)
        if plane is None:
            raise KeyError(f"binding refers to unknown data_plane '{plane_name}'")
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
        for plane_name, spec in (raw.get("data_planes") or {}).items():
            planes[plane_name] = DataPlane(
                name=plane_name,
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
        """Load from a YAML file if it declares data_planes; else build a single default.

        ``yaml`` is imported lazily so the common single-default path (and module
        import) needs no PyYAML — only an actual binding file pulls it in.
        """
        if path:
            file = Path(path)
            if file.is_file():
                import yaml

                raw = yaml.safe_load(file.read_text()) or {}
                if raw.get("data_planes"):
                    return cls.from_dict(raw)
        return cls.single_default(
            engine_addr=engine_addr, catalog_addr=catalog_addr, storage_addr=storage_addr
        )
