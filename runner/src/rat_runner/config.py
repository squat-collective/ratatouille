"""Configuration management — S3, Nessie, and pipeline config parsing.

NOTE: S3Config, DuckDBConfig, and NessieConfig are intentionally aligned with
the query service's versions in query/src/rat_query/config.py. This file
(runner) is the canonical version. When modifying these classes, keep both
files in sync. A shared package was considered but deferred to avoid
pyproject.toml and Docker packaging complexity. See task P6-01.
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Iterable  # noqa: TCH003 — used at runtime by validate_pipeline_config
from dataclasses import dataclass

import boto3
import yaml
from botocore.client import BaseClient
from botocore.exceptions import ClientError

from rat_runner.models import (
    VALID_PARTITION_TRANSFORMS,
    MergeStrategy,
    PartitionByEntry,
    PipelineConfig,
)

# Type alias for the S3 client returned by boto3.client("s3").
# At runtime this is a botocore.client.S3 instance (a dynamic subclass of
# BaseClient), but there is no static type for it without the mypy-boto3-s3
# stub package.  Using BaseClient keeps things correct and avoids an extra
# dependency.
S3ClientType = BaseClient

logger = logging.getLogger(__name__)

# Known keys in pipeline config.yaml — anything else triggers a warning.
_KNOWN_CONFIG_KEYS: frozenset[str] = frozenset(
    {
        "description",
        "materialized",
        "unique_key",
        "merge_strategy",
        "watermark_column",
        "archive_landing_zones",
        "partition_column",
        "partition_by",
        "scd_valid_from",
        "scd_valid_to",
        "max_retries",
        "retry_delay_seconds",
    }
)

# Valid values for 'materialized' field.
_VALID_MATERIALIZED: frozenset[str] = frozenset({"table", "view"})


@dataclass(frozen=True)
class S3Config:
    """S3/MinIO connection configuration.

    Canonical version — query/src/rat_query/config.py must stay aligned.
    Runner adds with_overrides() and boto3 helpers that query doesn't need.
    """

    endpoint: str = "minio:9000"
    access_key: str = ""
    secret_key: str = ""
    bucket: str = "rat"
    use_ssl: bool = False
    session_token: str = ""
    region: str = "us-east-1"

    @classmethod
    def from_env(cls) -> S3Config:
        """Build S3Config from environment variables.

        Raises ValueError if S3_ACCESS_KEY or S3_SECRET_KEY are not set,
        since credentials must never be hardcoded as defaults.
        """
        access_key = os.environ.get("S3_ACCESS_KEY", "")
        secret_key = os.environ.get("S3_SECRET_KEY", "")
        if not access_key or not secret_key:
            raise ValueError(
                "S3_ACCESS_KEY and S3_SECRET_KEY environment variables are required. "
                "See infra/.env.example for reference."
            )
        return cls(
            endpoint=os.environ.get("S3_ENDPOINT", "minio:9000"),
            access_key=access_key,
            secret_key=secret_key,
            bucket=os.environ.get("S3_BUCKET", "rat"),
            use_ssl=os.environ.get("S3_USE_SSL", "false").lower() == "true",
            session_token=os.environ.get("S3_SESSION_TOKEN", ""),
            region=os.environ.get("S3_REGION", "us-east-1"),
        )

    @property
    def endpoint_url(self) -> str:
        scheme = "https" if self.use_ssl else "http"
        return f"{scheme}://{self.endpoint}"

    def with_overrides(self, overrides: dict[str, str]) -> S3Config:
        """Create a new S3Config with overridden fields from a proto map."""
        if not overrides:
            return self
        return S3Config(
            endpoint=overrides.get("endpoint", self.endpoint),
            access_key=overrides.get("access_key", self.access_key),
            secret_key=overrides.get("secret_key", self.secret_key),
            bucket=overrides.get("bucket", self.bucket),
            use_ssl=overrides.get("use_ssl", str(self.use_ssl)).lower() == "true",
            session_token=overrides.get("session_token", self.session_token),
            region=overrides.get("region", self.region),
        )


@dataclass(frozen=True)
class DuckDBConfig:
    """DuckDB resource limits.

    Canonical version — query/src/rat_query/config.py must stay aligned.

    `query_timeout_seconds` is consumed by ratq's engine (watchdog-driven
    conn.interrupt()) to bound user-submitted SQL. The runner currently
    ignores it — pipeline SQL is plugin-author-controlled rather than
    user-facing — but the field is mirrored here so the two configs do
    not drift.

    `quality_test_timeout_seconds` is runner-only. Quality test SQL is
    author-written but can still hang the run thread (infinite recursion,
    catastrophic cartesian join, etc.). The runner's engine.query_arrow()
    consumes this field as the per-test deadline via the same watchdog
    pattern. Tunable via QUALITY_TEST_TIMEOUT_SECS (default 60s).
    """

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
            raise ValueError(
                f"DUCKDB_THREADS must be a valid integer, got {raw_threads!r}"
            ) from None
        if threads < 1:
            raise ValueError(f"DUCKDB_THREADS must be a positive integer, got {threads}")

        raw_timeout = os.environ.get("QUERY_TIMEOUT_SECS", "60")
        try:
            query_timeout_seconds = int(raw_timeout)
        except ValueError:
            raise ValueError(
                f"QUERY_TIMEOUT_SECS must be a valid integer, got {raw_timeout!r}"
            ) from None
        if query_timeout_seconds < 1:
            raise ValueError(
                f"QUERY_TIMEOUT_SECS must be a positive integer, got {query_timeout_seconds}"
            )

        raw_qt_timeout = os.environ.get("QUALITY_TEST_TIMEOUT_SECS", "60")
        try:
            quality_test_timeout_seconds = int(raw_qt_timeout)
        except ValueError:
            raise ValueError(
                f"QUALITY_TEST_TIMEOUT_SECS must be a valid integer, got {raw_qt_timeout!r}"
            ) from None
        if quality_test_timeout_seconds < 1:
            raise ValueError(
                "QUALITY_TEST_TIMEOUT_SECS must be a positive integer, "
                f"got {quality_test_timeout_seconds}"
            )

        return cls(
            memory_limit=os.environ.get("DUCKDB_MEMORY_LIMIT", "2GB"),
            threads=threads,
            query_timeout_seconds=query_timeout_seconds,
            quality_test_timeout_seconds=quality_test_timeout_seconds,
        )


@dataclass(frozen=True)
class NessieConfig:
    """Nessie catalog connection configuration.

    Canonical version — query/src/rat_query/config.py must stay aligned.
    """

    url: str = "http://nessie:19120/api/v1"

    @classmethod
    def from_env(cls) -> NessieConfig:
        return cls(url=os.environ.get("NESSIE_URL", "http://nessie:19120/api/v1"))

    @property
    def _host_url(self) -> str:
        """Strip known API suffixes to get the bare Nessie host URL."""
        url = self.url.rstrip("/")
        for suffix in ("/api/v1", "/api/v2", "/iceberg"):
            if url.endswith(suffix):
                url = url[: -len(suffix)]
                break
        return url

    @property
    def base_url(self) -> str:
        """Nessie Iceberg REST catalog URI (e.g., http://nessie:19120/iceberg)."""
        return self._host_url + "/iceberg"

    @property
    def api_v2_url(self) -> str:
        """Nessie v2 REST API base URL for branch operations."""
        return self._host_url + "/api/v2"


@dataclass(frozen=True)
class EngineConfig:
    """Address of the compute engine service (engine/v1). See ADR-024."""

    addr: str = "engine:50081"

    @classmethod
    def from_env(cls) -> EngineConfig:
        return cls(addr=os.environ.get("ENGINE_ADDR", "engine:50081"))


@dataclass(frozen=True)
class CatalogConfig:
    """Address of the catalog service (catalog/v1). See ADR-024."""

    addr: str = "catalog:50082"

    @classmethod
    def from_env(cls) -> CatalogConfig:
        return cls(addr=os.environ.get("CATALOG_ADDR", "catalog:50082"))


@dataclass(frozen=True)
class StorageConfig:
    """Address of the storage service (storage/v1). See ADR-024."""

    addr: str = "storage:50083"

    @classmethod
    def from_env(cls) -> StorageConfig:
        return cls(addr=os.environ.get("STORAGE_ADDR", "storage:50083"))


def validate_pipeline_config(
    data: dict[str, object],
    known_strategies: Iterable[str] | None = None,
) -> None:
    """Validate pipeline config keys and values.

    Warns on unknown keys (backward-compatible — does not reject them) and
    raises ValueError for invalid values in fields that have a known set of
    valid options (merge_strategy, materialized).

    ``known_strategies`` extends the accepted merge strategies beyond the six
    built-ins — pass the names discovered by the runner plugin registry so
    custom strategy plugins are not rejected. When None, only built-ins pass.
    """
    # Warn on unknown keys so users catch typos early.
    unknown_keys = set(data.keys()) - _KNOWN_CONFIG_KEYS
    for key in sorted(unknown_keys):
        logger.warning("Unknown pipeline config key '%s' — will be ignored", key)

    # Validate merge_strategy — an invalid value would fail at execution time
    # so we reject it early with a clear error message.
    merge_strategy = data.get("merge_strategy")
    if merge_strategy is not None:
        strategy_str = str(merge_strategy)
        plugin_strategies = set(known_strategies or ())
        if not MergeStrategy.validate(strategy_str) and strategy_str not in plugin_strategies:
            valid = sorted({m.value for m in MergeStrategy} | plugin_strategies)
            raise ValueError(
                f"Invalid merge_strategy '{strategy_str}'. Must be one of: {', '.join(valid)}"
            )

    # Validate materialized — only "table" and "view" are supported.
    materialized = data.get("materialized")
    if materialized is not None:
        mat_str = str(materialized)
        if mat_str not in _VALID_MATERIALIZED:
            raise ValueError(
                f"Invalid materialized '{mat_str}'. "
                f"Must be one of: {', '.join(sorted(_VALID_MATERIALIZED))}"
            )

    # Validate partition_by — each entry must have a column and a valid transform.
    partition_by = data.get("partition_by")
    if partition_by is not None:
        if not isinstance(partition_by, list):
            raise ValueError(
                "partition_by must be a list of entries with 'column' and optional 'transform'"
            )
        for i, entry in enumerate(partition_by):
            if not isinstance(entry, dict):
                raise ValueError(
                    f"partition_by[{i}] must be a mapping with 'column' and optional 'transform'"
                )
            if "column" not in entry:
                raise ValueError(f"partition_by[{i}] is missing required 'column' field")
            transform = str(entry.get("transform", "identity"))
            if transform not in VALID_PARTITION_TRANSFORMS:
                raise ValueError(
                    f"Invalid partition transform '{transform}' in partition_by[{i}]. "
                    f"Must be one of: {', '.join(sorted(VALID_PARTITION_TRANSFORMS))}"
                )


def _parse_partition_by(raw: object) -> tuple[PartitionByEntry, ...]:
    """Parse partition_by from YAML into a tuple of PartitionByEntry.

    Accepts a list of dicts like:
      partition_by:
        - column: created_date
          transform: day
        - column: region
    """
    if raw is None or raw == []:
        return ()
    if not isinstance(raw, list):
        return ()
    entries: list[PartitionByEntry] = []
    for item in raw:
        if isinstance(item, dict) and "column" in item:
            entries.append(
                PartitionByEntry(
                    column=str(item["column"]),
                    transform=str(item.get("transform", "identity")),
                )
            )
    return tuple(entries)


def parse_pipeline_config(
    yaml_str: str,
    known_strategies: Iterable[str] | None = None,
) -> PipelineConfig:
    """Parse a pipeline config.yaml string into PipelineConfig.

    Validates the config after parsing: warns on unknown keys and raises
    ValueError for invalid merge_strategy or materialized values.

    ``known_strategies`` is forwarded to :func:`validate_pipeline_config` so
    plugin-provided merge strategies are accepted.
    """
    data = yaml.safe_load(yaml_str)
    if not isinstance(data, dict):
        return PipelineConfig()

    validate_pipeline_config(data, known_strategies)

    unique_key_raw = data.get("unique_key", [])
    if isinstance(unique_key_raw, str):
        unique_key = tuple(k.strip() for k in unique_key_raw.split(",") if k.strip())
    elif isinstance(unique_key_raw, list):
        unique_key = tuple(str(k) for k in unique_key_raw)
    else:
        unique_key = ()

    # Parse retry configuration with validation.
    max_retries = 0
    raw_retries = data.get("max_retries")
    if raw_retries is not None:
        try:
            max_retries = int(raw_retries)
        except (ValueError, TypeError) as e:
            raise ValueError(f"max_retries must be an integer, got {raw_retries!r}") from e
        if max_retries < 0:
            raise ValueError(f"max_retries must be non-negative, got {max_retries}")

    retry_delay_seconds = 30
    raw_delay = data.get("retry_delay_seconds")
    if raw_delay is not None:
        try:
            retry_delay_seconds = int(raw_delay)
        except (ValueError, TypeError) as e:
            raise ValueError(f"retry_delay_seconds must be an integer, got {raw_delay!r}") from e
        if retry_delay_seconds < 0:
            raise ValueError(f"retry_delay_seconds must be non-negative, got {retry_delay_seconds}")

    return PipelineConfig(
        description=str(data.get("description", "")),
        materialized=str(data.get("materialized", "table")),
        unique_key=unique_key,
        merge_strategy=str(data.get("merge_strategy", "full_refresh")),
        watermark_column=str(data.get("watermark_column", "")),
        archive_landing_zones=str(data.get("archive_landing_zones", "false")).lower() == "true",
        partition_column=str(data.get("partition_column", "")),
        partition_by=_parse_partition_by(data.get("partition_by")),
        scd_valid_from=str(data.get("scd_valid_from", "valid_from")),
        scd_valid_to=str(data.get("scd_valid_to", "valid_to")),
        max_retries=max_retries,
        retry_delay_seconds=retry_delay_seconds,
    )


def merge_configs(
    base: PipelineConfig | None,
    annotations: dict[str, str],
) -> PipelineConfig:
    """Merge config.yaml (base) with annotation overrides per-field.

    Annotations win for any field they specify. If both are None/empty,
    returns a default PipelineConfig.
    """
    if not annotations and base is not None:
        return base
    if not annotations and base is None:
        return PipelineConfig()

    # Start from base values (or defaults)
    b = base or PipelineConfig()

    # Parse annotation values, falling back to base for unset fields
    unique_key_raw = annotations.get("unique_key", "")
    unique_key = (
        tuple(k.strip() for k in unique_key_raw.split(",") if k.strip())
        if unique_key_raw
        else b.unique_key
    )

    return PipelineConfig(
        description=annotations.get("description", b.description),
        materialized=annotations.get("materialized", b.materialized),
        unique_key=unique_key,
        merge_strategy=annotations.get("merge_strategy", b.merge_strategy),
        watermark_column=annotations.get("watermark_column", b.watermark_column),
        archive_landing_zones=(
            annotations["archive_landing_zones"].lower() == "true"
            if "archive_landing_zones" in annotations
            else b.archive_landing_zones
        ),
        partition_column=annotations.get("partition_column", b.partition_column),
        partition_by=b.partition_by,  # partition_by is only set via config.yaml, not annotations
        scd_valid_from=annotations.get("scd_valid_from", b.scd_valid_from),
        scd_valid_to=annotations.get("scd_valid_to", b.scd_valid_to),
        max_retries=int(annotations["max_retries"])
        if "max_retries" in annotations
        else b.max_retries,
        retry_delay_seconds=int(annotations["retry_delay_seconds"])
        if "retry_delay_seconds" in annotations
        else b.retry_delay_seconds,
    )


# TTL cache for boto3 clients — 45 minutes (STS tokens typically last 1 hour).
_BOTO3_CLIENT_TTL_SECONDS = 45 * 60
_boto3_client_cache: dict[S3Config, tuple[S3ClientType, float]] = {}


def _boto3_client(s3_config: S3Config) -> S3ClientType:
    """Create a TTL-cached boto3 S3 client, including session token if present (STS).

    S3Config is frozen (hashable), so it works as a dict key.
    Different STS credentials produce different cache entries.
    Cached clients expire after 45 minutes to handle STS token rotation.
    """
    now = time.monotonic()
    cached = _boto3_client_cache.get(s3_config)
    if cached is not None:
        client, created_at = cached
        if now - created_at < _BOTO3_CLIENT_TTL_SECONDS:
            return client

    kwargs = {
        "endpoint_url": s3_config.endpoint_url,
        "aws_access_key_id": s3_config.access_key,
        "aws_secret_access_key": s3_config.secret_key,
    }
    if s3_config.session_token:
        kwargs["aws_session_token"] = s3_config.session_token
    client = boto3.client("s3", **kwargs)
    _boto3_client_cache[s3_config] = (client, now)
    return client


def _boto3_client_cache_clear() -> None:
    """Clear all cached boto3 clients."""
    _boto3_client_cache.clear()


# Attach cache_clear as an attribute for backward compatibility with test fixtures.
_boto3_client.cache_clear = _boto3_client_cache_clear  # type: ignore[attr-defined]


def read_s3_text(s3_config: S3Config, key: str) -> str | None:
    """Read a text file from S3. Returns None if the key doesn't exist."""
    client = _boto3_client(s3_config)
    try:
        resp = client.get_object(Bucket=s3_config.bucket, Key=key)
        return resp["Body"].read().decode("utf-8")
    except ClientError as e:
        if e.response["Error"]["Code"] == "NoSuchKey":
            return None
        raise


def read_s3_text_version(s3_config: S3Config, key: str, version_id: str) -> str | None:
    """Read a specific version of a text file from S3.

    Returns None if the key/version doesn't exist.
    """
    client = _boto3_client(s3_config)
    try:
        resp = client.get_object(Bucket=s3_config.bucket, Key=key, VersionId=version_id)
        return resp["Body"].read().decode("utf-8")
    except ClientError as e:
        code = e.response["Error"]["Code"]
        if code in ("NoSuchKey", "NoSuchVersion"):
            return None
        raise


def move_s3_keys(
    s3_config: S3Config, src_keys: list[str], src_prefix: str, dest_prefix: str
) -> None:
    """Move S3 keys from src_prefix to dest_prefix. Best-effort."""
    if not src_keys:
        return
    client = _boto3_client(s3_config)
    for key in src_keys:
        dest_key = key.replace(src_prefix, dest_prefix, 1)
        client.copy_object(
            Bucket=s3_config.bucket,
            CopySource={"Bucket": s3_config.bucket, "Key": key},
            Key=dest_key,
        )
    client.delete_objects(
        Bucket=s3_config.bucket,
        Delete={"Objects": [{"Key": k} for k in src_keys]},
    )


def list_s3_keys(s3_config: S3Config, prefix: str, suffix: str = "") -> list[str]:
    """List S3 keys matching a prefix (and optional suffix filter)."""
    client = _boto3_client(s3_config)
    keys: list[str] = []
    paginator = client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=s3_config.bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            if not suffix or key.endswith(suffix):
                keys.append(key)
    return keys
