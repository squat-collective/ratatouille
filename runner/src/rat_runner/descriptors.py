"""Assemble engine/v1 TableDescriptors for a pipeline's inputs + output (ADR-024).

For the default Iceberg+Nessie+S3 composition the runner builds descriptors from its
own NessieConfig/S3Config — it passes *coordinates* (catalog URI, bucket, creds), not
data; the engine does the actual I/O. When a data_plane points at custom catalog/
storage *services*, these descriptors are instead vended by catalog/v1 GetTable +
storage/v1 VendDescriptor (a follow-on once those services exist).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from common.v1 import (  # type: ignore[import-untyped]
    common_pb2,
    data_plane_pb2,
)

if TYPE_CHECKING:
    from rat_runner.bindings import DataPlane
    from rat_runner.config import NessieConfig, S3Config

# common.v1.Layer enum value -> medallion schema name.
_LAYER_NAMES = {1: "bronze", 2: "silver", 3: "gold"}


def _s3_credentials(s3_config: S3Config) -> Any:
    return common_pb2.S3Credentials(
        endpoint=s3_config.endpoint,
        access_key_id=s3_config.access_key,
        secret_access_key=s3_config.secret_key,
        region=s3_config.region,
        bucket=s3_config.bucket,
        use_ssl=s3_config.use_ssl,
        session_token=s3_config.session_token,
    )


def build_table_descriptor(
    namespace: str,
    layer: int,
    name: str,
    data_plane: DataPlane,
    nessie_config: NessieConfig,
    s3_config: S3Config,
    branch: str = "main",
) -> Any:
    """Build a TableDescriptor for the Iceberg+Nessie+S3 default composition."""
    ref = data_plane_pb2.TableRef(namespace=namespace, layer=layer, name=name)
    catalog = data_plane_pb2.CatalogDescriptor(
        protocol="iceberg-rest", uri=nessie_config.base_url, branch=branch
    )
    storage = data_plane_pb2.StorageDescriptor(scheme="s3", s3=_s3_credentials(s3_config))
    layer_str = _LAYER_NAMES.get(layer, "")
    return data_plane_pb2.TableDescriptor(
        ref=ref,
        format=data_plane.format or "iceberg",
        identifier=f"{namespace}.{layer_str}.{name}",
        catalog=catalog,
        storage=storage,
    )
