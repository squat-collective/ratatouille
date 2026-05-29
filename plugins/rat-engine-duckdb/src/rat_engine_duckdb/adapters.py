"""Map engine/v1 descriptors → the relocated config types + table coordinates.

The engine receives `TableDescriptor`/`StorageDescriptor`/`CatalogDescriptor`
values over engine/v1 and reconstructs the same `S3Config`/`NessieConfig`/
`PipelineConfig` the relocated iceberg + strategy code expects — so that code runs
unchanged. Proto messages are untyped at the boundary, hence the explicit `Any`.
"""

from __future__ import annotations

from typing import Any

from rat_engine_duckdb.config import NessieConfig, PartitionByEntry, PipelineConfig, S3Config

# common.v1.Layer enum value → medallion schema name.
_LAYER_NAMES = {1: "bronze", 2: "silver", 3: "gold"}


def layer_name(layer: int) -> str:
    """common.v1.Layer enum → 'bronze'/'silver'/'gold' (''=unspecified)."""
    return _LAYER_NAMES.get(layer, "")


def s3_config_from_storage(storage: Any) -> S3Config:
    """StorageDescriptor (scheme=s3) → S3Config."""
    s3 = storage.s3
    return S3Config(
        endpoint=s3.endpoint,
        access_key=s3.access_key_id,
        secret_key=s3.secret_access_key,
        bucket=s3.bucket,
        use_ssl=s3.use_ssl,
        session_token=s3.session_token,
        region=s3.region or "us-east-1",
    )


def nessie_config_from_catalog(catalog: Any) -> NessieConfig:
    """CatalogDescriptor → NessieConfig (URL suffix normalization is internal)."""
    return NessieConfig(url=catalog.uri)


def table_identifier(desc: Any) -> str:
    """Catalog-native identifier: descriptor.identifier, else derived ns.layer.name."""
    if desc.identifier:
        return desc.identifier
    return f"{desc.ref.namespace}.{layer_name(desc.ref.layer)}.{desc.ref.name}"


def table_location(desc: Any) -> str:
    """S3 base location for a table: s3://{bucket}/{ns}/{layer}/{name}."""
    return (
        f"s3://{desc.storage.s3.bucket}/{desc.ref.namespace}"
        f"/{layer_name(desc.ref.layer)}/{desc.ref.name}"
    )


def pipeline_config_from_options(options: Any, strategy: str = "full_refresh") -> PipelineConfig:
    """ExecuteRequest.options + strategy name → the PipelineConfig fields the recipes read."""
    unique_raw = options.get("unique_key", "")
    unique_key = tuple(p.strip() for p in unique_raw.split(",") if p.strip())
    return PipelineConfig(
        unique_key=unique_key,
        merge_strategy=strategy or "full_refresh",
        partition_column=options.get("partition_column", ""),
        partition_by=_parse_partition_by(options.get("partition_by", "")),
        scd_valid_from=options.get("scd_valid_from", "") or "valid_from",
        scd_valid_to=options.get("scd_valid_to", "") or "valid_to",
    )


def _parse_partition_by(raw: str) -> tuple[PartitionByEntry, ...]:
    """Parse 'col:transform,col2' → (PartitionByEntry, ...). Bare col → identity."""
    entries: list[PartitionByEntry] = []
    for part in raw.split(","):
        token = part.strip()
        if not token:
            continue
        if ":" in token:
            col, transform = token.split(":", 1)
            entries.append(PartitionByEntry(column=col.strip(), transform=transform.strip()))
        else:
            entries.append(PartitionByEntry(column=token))
    return tuple(entries)
