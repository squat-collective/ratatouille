"""Built-in merge strategy implementations.

Wraps the existing iceberg.py functions into MergeStrategyProtocol classes
so they can be discovered via entry points alongside third-party plugins.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from rat_engine_duckdb.formats.iceberg import (
    append_iceberg,
    delete_insert_iceberg,
    merge_iceberg,
    scd2_iceberg,
    snapshot_iceberg,
    write_iceberg,
)

if TYPE_CHECKING:
    import duckdb
    import pyarrow as pa

    from rat_engine_duckdb.config import NessieConfig, S3Config
    from rat_engine_duckdb.config import PipelineConfig


class FullRefreshStrategy:
    """Full table overwrite — idempotent, safe to retry."""

    @property
    def name(self) -> str:
        return "full_refresh"

    def execute(
        self,
        data: pa.Table,
        table_name: str,
        s3_config: S3Config,
        nessie_config: NessieConfig,
        location: str,
        config: PipelineConfig | None,
        branch: str = "main",
        conn: duckdb.DuckDBPyConnection | None = None,
    ) -> int:
        partition_by = config.partition_by if config and config.partition_by else None
        return write_iceberg(
            data,
            table_name,
            s3_config,
            nessie_config,
            location,
            branch=branch,
            partition_by=partition_by,
        )


class IncrementalStrategy:
    """Upsert merge on unique_key — idempotent on unique_key."""

    @property
    def name(self) -> str:
        return "incremental"

    def execute(
        self,
        data: pa.Table,
        table_name: str,
        s3_config: S3Config,
        nessie_config: NessieConfig,
        location: str,
        config: PipelineConfig | None,
        branch: str = "main",
        conn: duckdb.DuckDBPyConnection | None = None,
    ) -> int:
        if not config or not config.unique_key:
            # Fall back to full refresh if no unique_key
            return FullRefreshStrategy().execute(
                data, table_name, s3_config, nessie_config, location, config, branch, conn
            )
        partition_by = config.partition_by if config.partition_by else None
        return merge_iceberg(
            data,
            table_name,
            config.unique_key,
            s3_config,
            nessie_config,
            location,
            branch=branch,
            conn=conn,
            partition_by=partition_by,
        )


class AppendOnlyStrategy:
    """Pure append — NOT idempotent, duplicates on retry."""

    @property
    def name(self) -> str:
        return "append_only"

    def execute(
        self,
        data: pa.Table,
        table_name: str,
        s3_config: S3Config,
        nessie_config: NessieConfig,
        location: str,
        config: PipelineConfig | None,
        branch: str = "main",
        conn: duckdb.DuckDBPyConnection | None = None,
    ) -> int:
        partition_by = config.partition_by if config and config.partition_by else None
        return append_iceberg(
            data,
            table_name,
            s3_config,
            nessie_config,
            location,
            branch=branch,
            partition_by=partition_by,
        )


class DeleteInsertStrategy:
    """Delete matching keys, then insert — idempotent on unique_key."""

    @property
    def name(self) -> str:
        return "delete_insert"

    def execute(
        self,
        data: pa.Table,
        table_name: str,
        s3_config: S3Config,
        nessie_config: NessieConfig,
        location: str,
        config: PipelineConfig | None,
        branch: str = "main",
        conn: duckdb.DuckDBPyConnection | None = None,
    ) -> int:
        if not config or not config.unique_key:
            return FullRefreshStrategy().execute(
                data, table_name, s3_config, nessie_config, location, config, branch, conn
            )
        partition_by = config.partition_by if config.partition_by else None
        return delete_insert_iceberg(
            data,
            table_name,
            config.unique_key,
            s3_config,
            nessie_config,
            location,
            branch=branch,
            conn=conn,
            partition_by=partition_by,
        )


class SCD2Strategy:
    """Slowly Changing Dimension Type 2 — partially idempotent."""

    @property
    def name(self) -> str:
        return "scd2"

    def execute(
        self,
        data: pa.Table,
        table_name: str,
        s3_config: S3Config,
        nessie_config: NessieConfig,
        location: str,
        config: PipelineConfig | None,
        branch: str = "main",
        conn: duckdb.DuckDBPyConnection | None = None,
    ) -> int:
        if not config or not config.unique_key:
            return FullRefreshStrategy().execute(
                data, table_name, s3_config, nessie_config, location, config, branch, conn
            )
        partition_by = config.partition_by if config.partition_by else None
        return scd2_iceberg(
            data,
            table_name,
            config.unique_key,
            s3_config,
            nessie_config,
            location,
            valid_from_col=config.scd_valid_from,
            valid_to_col=config.scd_valid_to,
            branch=branch,
            conn=conn,
            partition_by=partition_by,
        )


class SnapshotStrategy:
    """Partition-aware overwrite — idempotent on partition_column."""

    @property
    def name(self) -> str:
        return "snapshot"

    def execute(
        self,
        data: pa.Table,
        table_name: str,
        s3_config: S3Config,
        nessie_config: NessieConfig,
        location: str,
        config: PipelineConfig | None,
        branch: str = "main",
        conn: duckdb.DuckDBPyConnection | None = None,
    ) -> int:
        if not config or not config.partition_column:
            return FullRefreshStrategy().execute(
                data, table_name, s3_config, nessie_config, location, config, branch, conn
            )
        partition_by = config.partition_by if config.partition_by else None
        return snapshot_iceberg(
            data,
            table_name,
            config.partition_column,
            s3_config,
            nessie_config,
            location,
            branch=branch,
            conn=conn,
            partition_by=partition_by,
        )
