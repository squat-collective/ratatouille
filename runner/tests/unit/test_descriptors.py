"""Unit tests for TableDescriptor assembly (needs the generated common.v1 stubs)."""

from types import SimpleNamespace

from rat_runner.descriptors import build_table_descriptor


def _nessie() -> SimpleNamespace:
    return SimpleNamespace(base_url="http://nessie:19120/iceberg")


def _s3() -> SimpleNamespace:
    return SimpleNamespace(
        endpoint="minio:9000",
        access_key="ak",
        secret_key="sk",
        region="us-east-1",
        bucket="rat",
        use_ssl=False,
        session_token="",
    )


def _plane(fmt: str = "iceberg") -> SimpleNamespace:
    return SimpleNamespace(
        name="default", engine_addr="e", catalog_addr="c", storage_addr="s", format=fmt
    )


def test_build_table_descriptor_default_composition():
    desc = build_table_descriptor("shop", 2, "orders", _plane(), _nessie(), _s3(), branch="run-123")
    assert (desc.ref.namespace, desc.ref.layer, desc.ref.name) == ("shop", 2, "orders")
    assert desc.format == "iceberg"
    assert desc.identifier == "shop.silver.orders"
    assert desc.catalog.protocol == "iceberg-rest"
    assert desc.catalog.uri == "http://nessie:19120/iceberg"
    assert desc.catalog.branch == "run-123"
    assert desc.storage.scheme == "s3"
    assert desc.storage.s3.bucket == "rat"
    assert desc.storage.s3.access_key_id == "ak"
