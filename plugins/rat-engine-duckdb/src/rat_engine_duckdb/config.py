"""Connection + pipeline config types for the DuckDB engine.

Relocated from the runner (config.py / models.py) so the engine is self-contained.
The engine builds S3Config / NessieConfig from the descriptors it receives over
engine/v1 (see adapters.py); from_env() is retained for the engine's own resource
defaults and for parity with the runner's canonical definitions.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class S3Config:
    """S3/MinIO connection configuration. Mirrors the runner's canonical S3Config."""

    endpoint: str = "minio:9000"
    access_key: str = ""
    secret_key: str = ""
    bucket: str = "rat"
    use_ssl: bool = False
    session_token: str = ""
    region: str = "us-east-1"

    @property
    def endpoint_url(self) -> str:
        scheme = "https" if self.use_ssl else "http"
        return f"{scheme}://{self.endpoint}"


@dataclass(frozen=True)
class DuckDBConfig:
    """DuckDB resource limits. Mirrors the runner's canonical DuckDBConfig."""

    memory_limit: str = "2GB"
    threads: int = 4
    query_timeout_seconds: int = 60
    quality_test_timeout_seconds: int = 60

    @classmethod
    def from_env(cls) -> DuckDBConfig:
        raw_threads = os.environ.get("DUCKDB_THREADS", "4")
        try:
            threads = int(raw_threads)
        except ValueError:
            raise ValueError(f"DUCKDB_THREADS must be an integer, got {raw_threads!r}") from None
        if threads < 1:
            raise ValueError(f"DUCKDB_THREADS must be positive, got {threads}")
        return cls(
            memory_limit=os.environ.get("DUCKDB_MEMORY_LIMIT", "2GB"),
            threads=threads,
            query_timeout_seconds=int(os.environ.get("QUERY_TIMEOUT_SECS", "60")),
            quality_test_timeout_seconds=int(os.environ.get("QUALITY_TEST_TIMEOUT_SECS", "60")),
        )


@dataclass(frozen=True)
class NessieConfig:
    """Nessie catalog connection. Mirrors the runner's canonical NessieConfig."""

    url: str = "http://nessie:19120/api/v1"
    # Catalog protocol + Lakekeeper-style options. Defaults preserve Nessie behavior;
    # a lakekeeper plane sets protocol="lakekeeper" + a named warehouse + bearer token.
    protocol: str = "iceberg-rest"
    warehouse: str = ""
    token: str = ""

    @property
    def _host_url(self) -> str:
        url = self.url.rstrip("/")
        for suffix in ("/api/v1", "/api/v2", "/iceberg"):
            if url.endswith(suffix):
                url = url[: -len(suffix)]
                break
        return url

    @property
    def base_url(self) -> str:
        """Nessie Iceberg REST catalog URI (e.g. http://nessie:19120/iceberg)."""
        return self._host_url + "/iceberg"

    @property
    def api_v2_url(self) -> str:
        """Nessie v2 REST API base URL for branch operations."""
        return self._host_url + "/api/v2"


@dataclass(frozen=True)
class PartitionByEntry:
    """A single partition field: column name + transform (identity/day/month/year/hour)."""

    column: str
    transform: str = "identity"


@dataclass(frozen=True)
class PipelineConfig:
    """Subset of the runner's PipelineConfig the strategy recipes read.

    Built from an ExecuteRequest's strategy `options` map (see adapters.py).
    """

    unique_key: tuple[str, ...] = ()
    merge_strategy: str = "full_refresh"
    partition_column: str = ""
    partition_by: tuple[PartitionByEntry, ...] = ()
    scd_valid_from: str = "valid_from"
    scd_valid_to: str = "valid_to"
