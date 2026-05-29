"""Unit tests for TableDescriptor assembly (needs the generated common.v1 stubs)."""

from types import SimpleNamespace

from common.v1 import common_pb2, data_plane_pb2  # type: ignore[import-untyped]

from rat_runner.descriptors import build_table_descriptor


def _nessie() -> SimpleNamespace:
    return SimpleNamespace(base_url="http://nessie:19120/iceberg")


def _plane(fmt: str = "iceberg", protocol: str = "iceberg-rest") -> SimpleNamespace:
    return SimpleNamespace(
        name="default",
        engine_addr="e",
        catalog_addr="c",
        storage_addr="s",
        format=fmt,
        catalog_protocol=protocol,
        supports_branching=True,
    )


def _storage() -> data_plane_pb2.StorageDescriptor:
    return data_plane_pb2.StorageDescriptor(
        scheme="s3",
        s3=common_pb2.S3Credentials(
            endpoint="minio:9000", access_key_id="ak", secret_access_key="sk", bucket="rat"
        ),
    )


def test_build_table_descriptor_default_composition():
    desc = build_table_descriptor(
        "shop", 2, "orders", _plane(), _nessie(), _storage(), branch="run-1"
    )
    assert (desc.ref.namespace, desc.ref.layer, desc.ref.name) == ("shop", 2, "orders")
    assert desc.format == "iceberg"
    assert desc.identifier == "shop.silver.orders"
    assert desc.catalog.protocol == "iceberg-rest"
    assert desc.catalog.uri == "http://nessie:19120/iceberg"
    assert desc.catalog.branch == "run-1"
    # The vended StorageDescriptor is passed through unchanged.
    assert desc.storage.scheme == "s3"
    assert desc.storage.s3.bucket == "rat"


def test_build_table_descriptor_lakekeeper_catalog(monkeypatch):
    monkeypatch.setenv("LAKEKEEPER_URI", "http://lk:8181/catalog")
    monkeypatch.setenv("LAKEKEEPER_WAREHOUSE", "wh")
    desc = build_table_descriptor(
        "lk", 1, "orders", _plane(protocol="lakekeeper"), _nessie(), _storage()
    )
    assert desc.catalog.protocol == "lakekeeper"
    assert desc.catalog.uri == "http://lk:8181/catalog"
    assert desc.catalog.options["warehouse"] == "wh"
