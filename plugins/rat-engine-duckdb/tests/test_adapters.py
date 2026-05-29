"""Unit tests for the descriptor → config adapters (pure; no DuckDB/Iceberg needed)."""

from types import SimpleNamespace

from rat_engine_duckdb.adapters import (
    _parse_partition_by,
    layer_name,
    pipeline_config_from_options,
    s3_config_from_storage,
    table_identifier,
    table_location,
)


def _storage(bucket: str = "rat") -> SimpleNamespace:
    s3 = SimpleNamespace(
        endpoint="minio:9000",
        access_key_id="ak",
        secret_access_key="sk",
        bucket=bucket,
        use_ssl=False,
        session_token="",
        region="us-east-1",
    )
    return SimpleNamespace(scheme="s3", s3=s3)


def _desc(identifier: str = "", namespace: str = "default", layer: int = 2, name: str = "orders"):
    ref = SimpleNamespace(namespace=namespace, layer=layer, name=name)
    return SimpleNamespace(ref=ref, identifier=identifier, storage=_storage(), format="iceberg")


def test_layer_name_maps_medallion_tiers():
    assert layer_name(1) == "bronze"
    assert layer_name(2) == "silver"
    assert layer_name(3) == "gold"
    assert layer_name(0) == ""


def test_s3_config_from_storage_maps_credential_fields():
    cfg = s3_config_from_storage(_storage(bucket="lake"))
    assert cfg.bucket == "lake"
    assert cfg.access_key == "ak"
    assert cfg.secret_key == "sk"
    assert cfg.endpoint_url == "http://minio:9000"


def test_table_identifier_prefers_explicit_then_derives():
    assert table_identifier(_desc()) == "default.silver.orders"
    assert table_identifier(_desc(identifier="custom.id")) == "custom.id"


def test_table_location_builds_s3_path():
    assert table_location(_desc(namespace="shop", layer=1, name="raw")) == "s3://rat/shop/bronze/raw"


def test_pipeline_config_from_options_threads_strategy_and_keys():
    cfg = pipeline_config_from_options(
        {"unique_key": "id, region", "partition_column": "dt"}, "scd2"
    )
    assert cfg.unique_key == ("id", "region")
    assert cfg.merge_strategy == "scd2"
    assert cfg.partition_column == "dt"
    assert cfg.scd_valid_from == "valid_from"


def test_parse_partition_by_handles_transform_and_bare_columns():
    entries = _parse_partition_by("event_date:day, region")
    assert len(entries) == 2
    assert (entries[0].column, entries[0].transform) == ("event_date", "day")
    assert (entries[1].column, entries[1].transform) == ("region", "identity")
