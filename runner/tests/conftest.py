"""Shared test fixtures for the runner test suite."""

from __future__ import annotations

import pytest

from rat_runner.config import NessieConfig, S3Config, _boto3_client_cache_clear

# Proto stubs come from the shared `rat-protos` package (ADR-024 cleanup C).


@pytest.fixture(autouse=True)
def _clear_boto3_cache():
    """Clear cached boto3 clients between tests so mocks take effect."""
    _boto3_client_cache_clear()
    yield
    _boto3_client_cache_clear()


@pytest.fixture
def s3_config() -> S3Config:
    return S3Config(
        endpoint="localhost:9000",
        access_key="test-access-key",
        secret_key="test-secret-key",
        bucket="test-bucket",
        use_ssl=False,
    )


@pytest.fixture
def nessie_config() -> NessieConfig:
    return NessieConfig(url="http://localhost:19120/api/v1")
