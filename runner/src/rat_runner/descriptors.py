"""Assemble engine/v1 TableDescriptors for a pipeline's inputs + output (ADR-024).

The StorageDescriptor is vended by the storage/v1 service (which owns the S3 creds).
The CatalogDescriptor is built from config for the default Iceberg+Nessie / DuckLake /
Lakekeeper compositions (catalog/v1 GetTable will vend it once that service's discovery
lands). The runner passes coordinates, not data — the engine does the I/O.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any

from common.v1 import data_plane_pb2  # type: ignore[import-untyped]

if TYPE_CHECKING:
    from rat_runner.bindings import DataPlane
    from rat_runner.config import NessieConfig

# common.v1.Layer enum value -> medallion schema name (and back).
_LAYER_NAMES = {1: "bronze", 2: "silver", 3: "gold"}
_LAYER_ENUMS = {"bronze": 1, "silver": 2, "gold": 3}


def layer_enum(name: str) -> int:
    """Medallion layer name -> common.v1.Layer enum value (0 = unspecified)."""
    return _LAYER_ENUMS.get(name, 0)


def build_table_descriptor(
    namespace: str,
    layer: int,
    name: str,
    data_plane: DataPlane,
    nessie_config: NessieConfig,
    storage_descriptor: Any,
    branch: str = "main",
) -> Any:
    """Build a TableDescriptor from a vended StorageDescriptor + the plane's catalog."""
    ref = data_plane_pb2.TableRef(namespace=namespace, layer=layer, name=name)
    bucket = storage_descriptor.s3.bucket
    fmt = data_plane.format or "iceberg"
    if fmt == "ducklake":
        # DuckLake: SQL-DB catalog + Parquet. Connection from env in config mode.
        catalog = data_plane_pb2.CatalogDescriptor(
            protocol="ducklake",
            uri=os.environ.get(
                "DUCKLAKE_URI", "postgres:dbname=ducklake host=postgres user=rat password=rat"
            ),
            options={"data_path": os.environ.get("DUCKLAKE_DATA_PATH", f"s3://{bucket}/ducklake/")},
        )
    elif data_plane.catalog_protocol == "lakekeeper":
        # Lakekeeper: Iceberg REST catalog with a named warehouse + bearer token.
        catalog = data_plane_pb2.CatalogDescriptor(
            protocol="lakekeeper",
            uri=os.environ.get("LAKEKEEPER_URI", "http://lakekeeper:8181/catalog"),
            options={
                "warehouse": os.environ.get("LAKEKEEPER_WAREHOUSE", "rat"),
                "token": os.environ.get("LAKEKEEPER_TOKEN", "dummy"),
            },
        )
    else:
        catalog = data_plane_pb2.CatalogDescriptor(
            protocol="iceberg-rest", uri=nessie_config.base_url, branch=branch
        )
    layer_str = _LAYER_NAMES.get(layer, "")
    return data_plane_pb2.TableDescriptor(
        ref=ref,
        format=fmt,
        identifier=f"{namespace}.{layer_str}.{name}",
        catalog=catalog,
        storage=storage_descriptor,
    )
