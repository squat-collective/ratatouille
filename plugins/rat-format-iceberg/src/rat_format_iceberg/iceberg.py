"""Iceberg writer — PyIceberg table creation, overwrites, and merges via Nessie catalog.

Idempotency Guarantees per Merge Strategy
==========================================

Each merge strategy provides different idempotency characteristics when a
pipeline run is retried or re-executed with the same input data:

full_refresh (write_iceberg):
    IDEMPOTENT. Overwrites the entire table with the new data. Running twice
    with the same input produces the same result. Safe to retry unconditionally.

incremental (merge_iceberg):
    IDEMPOTENT on unique_key. Deduplicates new_data on unique_key (last row wins),
    then merges into existing data using ANTI JOIN. Re-running with the same input
    and unique_key produces the same result because existing rows with matching keys
    are replaced. NOT idempotent if unique_key is missing (falls back to full_refresh).

append_only (append_iceberg):
    NOT IDEMPOTENT. Each execution appends all rows unconditionally. Re-running
    with the same input produces duplicate rows. Callers must implement their own
    deduplication if retries are needed (e.g., using a unique run_id column).

delete_insert (delete_insert_iceberg):
    IDEMPOTENT on unique_key. Deletes all rows matching new_data's key values,
    then inserts new_data without deduplication. Re-running produces the same
    result because the delete step removes any previously inserted rows.
    NOT idempotent if unique_key is missing (falls back to full_refresh).

scd2 (scd2_iceberg):
    PARTIALLY IDEMPOTENT. Re-running with the same input will close and re-open
    the same records, but valid_from timestamps will differ between runs (they use
    CURRENT_TIMESTAMP). The historical chain is consistent but timestamps of the
    latest open records will reflect the most recent execution time.

snapshot (snapshot_iceberg):
    IDEMPOTENT on partition_column. Replaces only the partitions present in
    new_data. Re-running with the same partition values and data produces the
    same result. Untouched partitions are always preserved.

Retry Safety Summary:
    - Safe to retry:     full_refresh, incremental, delete_insert, snapshot
    - Unsafe to retry:   append_only (duplicates data)
    - Partially safe:    scd2 (timestamps differ but data is consistent)
"""

from __future__ import annotations

import contextlib
import logging
import re
from typing import TYPE_CHECKING

import duckdb
import pyarrow as pa
from pyiceberg.catalog import load_catalog
from pyiceberg.catalog.rest import RestCatalog
from pyiceberg.exceptions import NamespaceAlreadyExistsError, NoSuchTableError
from pyiceberg.expressions import And, EqualTo, In, IsNull, Or
from pyiceberg.partitioning import PartitionField, PartitionSpec
from pyiceberg.transforms import (
    DayTransform,
    HourTransform,
    IdentityTransform,
    MonthTransform,
    YearTransform,
)
from rat_engine_duckdb.duckdb_engine import _to_arrow_table

if TYPE_CHECKING:
    from pyiceberg.table import Table as IcebergTable
    from rat_engine_duckdb.config import NessieConfig, PartitionByEntry, S3Config

logger = logging.getLogger(__name__)

# Strict pattern for SQL identifiers — prevents SQL injection via column names.
_SAFE_IDENTIFIER = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")

# Maximum number of composite key rows for the optimized delete+append path.
# Beyond this threshold, the Or(And(...)) filter expression becomes too large
# and we fall back to the full-rewrite approach.
_MAX_COMPOSITE_DELETE_ROWS = 500


def _escape_sql_string(value: str) -> str:
    """Escape a string value for safe inclusion in a SQL single-quoted literal.

    Doubles any embedded single quotes to prevent SQL injection
    (e.g. from catalog-provided metadata_location paths).
    """
    return value.replace("'", "''")


def _quote_identifier(name: str) -> str:
    """Validate and quote a SQL identifier to prevent injection.

    Raises ValueError if the name contains characters outside [a-zA-Z0-9_].
    """
    if not _SAFE_IDENTIFIER.match(name):
        raise ValueError(f"Invalid SQL identifier: {name!r}")
    return f'"{name}"'


def _validate_identifiers(names: tuple[str, ...] | list[str]) -> list[str]:
    """Validate and quote a sequence of SQL identifiers."""
    return [_quote_identifier(n) for n in names]


def get_catalog(
    s3_config: S3Config,
    nessie_config: NessieConfig,
    branch: str = "main",
) -> RestCatalog:
    """Create a PyIceberg REST catalog from the catalog config.

    Supports Nessie-style catalogs (branch via ``prefix``, warehouse = the S3 bucket)
    and Lakekeeper-style catalogs (a named warehouse + bearer token, no prefix),
    selected by ``nessie_config.protocol``.
    """
    catalog_props: dict[str, str] = {
        "type": "rest",
        "s3.endpoint": s3_config.endpoint_url,
        "s3.access-key-id": s3_config.access_key,
        "s3.secret-access-key": s3_config.secret_key,
        "s3.path-style-access": "true",
    }
    if s3_config.session_token:
        catalog_props["s3.session-token"] = s3_config.session_token
    if nessie_config.protocol == "lakekeeper":
        catalog_props["uri"] = nessie_config.url
        catalog_props["warehouse"] = nessie_config.warehouse
        catalog_props["token"] = nessie_config.token or "dummy"
    else:
        catalog_props["uri"] = nessie_config.base_url
        catalog_props["warehouse"] = f"s3://{s3_config.bucket}"
        catalog_props["prefix"] = branch
    catalog = load_catalog(nessie_config.protocol, **catalog_props)
    assert isinstance(catalog, RestCatalog)
    return catalog


def ensure_namespace(catalog: RestCatalog, namespace: str) -> None:
    """Create namespace hierarchy if it doesn't exist.

    For "ns.layer", creates both ("ns",) and ("ns", "layer").
    """
    parts = namespace.split(".")
    for i in range(1, len(parts) + 1):
        ns_tuple = tuple(parts[:i])
        with contextlib.suppress(NamespaceAlreadyExistsError):
            catalog.create_namespace(ns_tuple)


# Mapping from transform name to PyIceberg transform class.
_TRANSFORM_MAP: dict[str, type] = {
    "identity": IdentityTransform,
    "day": DayTransform,
    "month": MonthTransform,
    "year": YearTransform,
    "hour": HourTransform,
}


def build_partition_spec(
    arrow_schema: pa.Schema,
    partition_by: tuple[PartitionByEntry, ...] | list[PartitionByEntry],
) -> PartitionSpec:
    """Build a PyIceberg PartitionSpec from partition_by config entries and a PyArrow schema.

    Each entry maps a column name to a transform (identity, day, month, year, hour).
    The schema is needed to resolve column names to Iceberg field IDs.

    PyIceberg assigns Iceberg field IDs sequentially starting at 1 when converting
    from PyArrow schemas, so source_id = column_index + 1.

    Raises ValueError if a column is not found in the schema or the transform is unsupported.
    """
    if not partition_by:
        return PartitionSpec()

    column_names = arrow_schema.names
    fields: list[PartitionField] = []
    for i, entry in enumerate(partition_by):
        # Resolve column name to Iceberg field ID (1-indexed position in schema)
        if entry.column not in column_names:
            raise ValueError(
                f"Partition column '{entry.column}' not found in table schema. "
                f"Available columns: {column_names}"
            )
        source_id = column_names.index(entry.column) + 1

        transform_cls = _TRANSFORM_MAP.get(entry.transform)
        if transform_cls is None:
            raise ValueError(
                f"Unsupported partition transform '{entry.transform}'. "
                f"Must be one of: {', '.join(sorted(_TRANSFORM_MAP.keys()))}"
            )

        fields.append(
            PartitionField(
                source_id=source_id,
                field_id=1000 + i,  # partition field IDs start at 1000 by convention
                transform=transform_cls(),
                name=f"{entry.column}_{entry.transform}"
                if entry.transform != "identity"
                else entry.column,
            )
        )

    return PartitionSpec(*fields)


def write_iceberg(
    data: pa.Table,
    table_name: str,
    s3_config: S3Config,
    nessie_config: NessieConfig,
    location: str,
    branch: str = "main",
    partition_by: tuple[PartitionByEntry, ...] | None = None,
) -> int:
    """Write a PyArrow Table to an Iceberg table (create or overwrite).

    Idempotency: SAFE. Full table overwrite — re-running with the same input
    always produces the same result regardless of previous state.

    Args:
        data: The data to write.
        table_name: Fully qualified name like "namespace.layer.pipeline_name".
        s3_config: S3 connection config.
        nessie_config: Nessie catalog config.
        location: S3 location for the table data (e.g., "s3://bucket/ns/layer/name/").
        branch: Nessie branch to write to (default "main").
        partition_by: Optional partition spec entries for table creation.
            Only used when creating a new table (ignored for existing tables).

    Returns:
        Number of rows written.
    """
    catalog = get_catalog(s3_config, nessie_config, branch=branch)

    # Ensure namespace exists (e.g., "namespace.layer")
    ns_parts = table_name.rsplit(".", 1)
    if len(ns_parts) == 2:
        ensure_namespace(catalog, ns_parts[0])

    try:
        table = catalog.load_table(table_name)
        table.overwrite(data)
    except NoSuchTableError:
        create_kwargs: dict[str, object] = {
            "identifier": table_name,
            "schema": data.schema,
        }
        if location:
            # Catalogs like Lakekeeper assign locations under their own warehouse
            # prefix and reject an explicit one — pass it only when provided.
            create_kwargs["location"] = location
        if partition_by:
            create_kwargs["partition_spec"] = build_partition_spec(data.schema, partition_by)
        table = catalog.create_table(**create_kwargs)
        table.overwrite(data)

    return len(data)


def _configure_s3(conn: duckdb.DuckDBPyConnection, s3_config: S3Config) -> None:
    """Configure S3/Iceberg extensions on a DuckDB connection."""
    conn.execute("INSTALL httpfs; LOAD httpfs;")
    conn.execute("INSTALL iceberg; LOAD iceberg;")
    conn.execute("SET s3_endpoint = ?", [s3_config.endpoint])
    conn.execute("SET s3_access_key_id = ?", [s3_config.access_key])
    conn.execute("SET s3_secret_access_key = ?", [s3_config.secret_key])
    conn.execute("SET s3_url_style = 'path'")
    conn.execute("SET s3_use_ssl = ?", [s3_config.use_ssl])
    conn.execute("SET s3_region = ?", [s3_config.region])
    if s3_config.session_token:
        conn.execute("SET s3_session_token = ?", [s3_config.session_token])


def _build_delete_filter_single_key(
    key_column: str,
    values: pa.Array | list[object],
) -> In:
    """Build a PyIceberg In() filter for a single unique key column.

    This is the efficient path: a single IN(...) predicate that PyIceberg
    can push down to data file pruning.
    """
    # Convert PyArrow array to Python list for PyIceberg's In() expression.
    if isinstance(values, pa.Array | pa.ChunkedArray):
        py_values = values.to_pylist()
    else:
        py_values = list(values)
    # Remove duplicates while preserving type.
    unique_values = list(set(py_values))
    return In(key_column, unique_values)


def _extract_composite_key_rows(
    new_data: pa.Table,
    key_columns: list[str],
) -> list[tuple[object, ...]]:
    """Extract deduplicated key-value tuples from a PyArrow table.

    Returns a list of (v1, v2, ...) tuples, one per unique composite key
    combination, preserving first-seen order. NULL values are preserved
    as None (hashable in Python tuples).
    """
    if len(new_data) == 0:
        return []

    columns = [new_data.column(col).to_pylist() for col in key_columns]
    seen: set[tuple[object, ...]] = set()
    result: list[tuple[object, ...]] = []
    for i in range(len(new_data)):
        row = tuple(columns[j][i] for j in range(len(key_columns)))
        if row not in seen:
            seen.add(row)
            result.append(row)
    return result


def _build_delete_filter_composite_key(
    key_columns: list[str],
    key_value_rows: list[tuple[object, ...]],
) -> And | Or:
    """Build a PyIceberg OR-of-ANDs filter for composite unique keys.

    For N rows with M key columns, builds:
        Or(And(k1=v1a, k2=v2a), And(k1=v1b, k2=v2b), ...)

    NULL key values are handled with IsNull() instead of EqualTo().
    Single-row input returns And(...) without an Or wrapper.

    WARNING: This does NOT scale well for large numbers of rows (>500).
    Callers should fall back to the full-rewrite approach for large key sets.
    """
    if not key_value_rows:
        raise ValueError("key_value_rows must not be empty")

    def _row_predicate(row: tuple[object, ...]) -> And:
        predicates = []
        for col, val in zip(key_columns, row, strict=False):
            if val is None:
                predicates.append(IsNull(col))
            else:
                predicates.append(EqualTo(col, val))
        return And(*predicates)

    if len(key_value_rows) == 1:
        return _row_predicate(key_value_rows[0])

    return Or(*[_row_predicate(row) for row in key_value_rows])


def _get_row_count(table: IcebergTable) -> int:
    """Get the total row count from Iceberg snapshot metadata (O(1)).

    Uses table.current_snapshot().summary["total-records"] for an instant count
    without reading any data files. Falls back to a full scan if the snapshot
    metadata is unavailable or doesn't contain the record count.
    """
    try:
        snapshot = table.current_snapshot()
        if snapshot is not None and snapshot.summary is not None:
            total = snapshot.summary.get("total-records")
            if total is not None:
                return int(total)
    except Exception:
        pass
    # Fallback: full table scan (expensive but always correct).
    return len(table.scan().to_arrow())


def _try_optimized_delete_append(
    table: IcebergTable,
    new_data: pa.Table,
    unique_key: tuple[str, ...] | list[str],
) -> int | None:
    """Try the optimized delete+append path instead of a full table rewrite.

    Returns the total row count (existing minus deleted plus appended) on success,
    or None if the optimization is not applicable or fails (caller should fall back).

    Supports both single-column and composite unique keys:

    Single-column key:
        Uses In() filter for efficient predicate pushdown.

    Composite key (2+ columns, <= _MAX_COMPOSITE_DELETE_ROWS unique rows):
        Uses Or(And(EqualTo(...))) filter for precise row-level deletes.
        Falls back to full rewrite if the number of unique key rows exceeds
        the threshold (filter expression becomes too large).
    """
    # --- Build delete filter ---
    if len(unique_key) == 1:
        key_col = unique_key[0]
        if key_col not in new_data.column_names:
            return None
        delete_filter = _build_delete_filter_single_key(key_col, new_data.column(key_col))
    else:
        # Composite key: check all columns exist
        for col in unique_key:
            if col not in new_data.column_names:
                return None
        key_rows = _extract_composite_key_rows(new_data, list(unique_key))
        if not key_rows:
            return None
        if len(key_rows) > _MAX_COMPOSITE_DELETE_ROWS:
            return None
        delete_filter = _build_delete_filter_composite_key(list(unique_key), key_rows)

    # --- Execute delete + append ---
    try:
        existing_count = _get_row_count(table)

        # Count rows that will be deleted (for accurate total calculation).
        deleted_data = table.scan(row_filter=delete_filter).to_arrow()
        deleted_count = len(deleted_data)

        # Delete rows matching the key values, then append new data.
        table.delete(delete_filter)
        table.append(new_data)

        total = existing_count - deleted_count + len(new_data)
        logger.info(
            "Optimized delete+append: deleted %d, appended %d, total %d",
            deleted_count,
            len(new_data),
            total,
        )
        return total
    except Exception as e:
        logger.warning(
            "Optimized delete+append failed (%s), falling back to full rewrite",
            e,
        )
        return None


def _dedup_new_data(
    new_data: pa.Table,
    unique_key: tuple[str, ...] | list[str],
    conn: duckdb.DuckDBPyConnection | None = None,
) -> pa.Table:
    """Deduplicate new_data on unique_key, keeping the last row (by position).

    Uses DuckDB for efficient deduplication with ROW_NUMBER() OVER().
    """
    own_conn = conn is None
    if own_conn:
        conn = duckdb.connect(":memory:")
    try:
        conn.register("new_data_dedup_raw", new_data)
        col_names = ", ".join(new_data.column_names)
        safe_keys = _validate_identifiers(unique_key)
        key_cols = ", ".join(safe_keys)

        conn.execute(
            "CREATE TABLE new_data_dedup AS "
            "SELECT *, ROW_NUMBER() OVER () AS _rn FROM new_data_dedup_raw"
        )

        dedup_sql = f"""
            SELECT {col_names} FROM new_data_dedup
            QUALIFY ROW_NUMBER() OVER (
                PARTITION BY {key_cols} ORDER BY _rn DESC
            ) = 1
        """
        return _to_arrow_table(conn.execute(dedup_sql).arrow())
    finally:
        if own_conn:
            conn.close()


def merge_iceberg(
    new_data: pa.Table,
    table_name: str,
    unique_key: tuple[str, ...] | list[str],
    s3_config: S3Config,
    nessie_config: NessieConfig,
    location: str,
    branch: str = "main",
    conn: duckdb.DuckDBPyConnection | None = None,
    partition_by: tuple[PartitionByEntry, ...] | None = None,
) -> int:
    """Merge new data into an existing Iceberg table.

    Idempotency: SAFE when unique_key is set. Existing rows with matching keys
    are replaced by new rows, so re-running with the same input produces
    identical results. Falls back to full_refresh (also safe) when table is new.

    Optimized path (single-column unique key):
        Deduplicates new_data, then uses PyIceberg delete(In(...)) + append()
        to write only the changed rows instead of rewriting the entire table.

    Fallback path (composite keys or if optimized path fails):
        Uses DuckDB ANTI JOIN + UNION ALL with iceberg_scan() for lazy S3 reads,
        then overwrites the full table.

    Deduplicates new_data on unique_key (last row wins by rowid).
    Falls back to write_iceberg if the table doesn't exist yet.

    Returns:
        Number of rows in the merged table.
    """
    catalog = get_catalog(s3_config, nessie_config, branch=branch)

    ns_parts = table_name.rsplit(".", 1)
    if len(ns_parts) == 2:
        ensure_namespace(catalog, ns_parts[0])

    try:
        table = catalog.load_table(table_name)
    except NoSuchTableError:
        # First run — no existing data, just write
        return write_iceberg(
            new_data,
            table_name,
            s3_config,
            nessie_config,
            location,
            branch,
            partition_by=partition_by,
        )

    # Optimized path: dedup first, then delete+append (avoids full table rewrite).
    # Only works for single-column unique keys where PyIceberg In() is efficient.
    deduped = _dedup_new_data(new_data, unique_key, conn)
    optimized_result = _try_optimized_delete_append(table, deduped, unique_key)
    if optimized_result is not None:
        return optimized_result

    # Fallback: full ANTI JOIN merge in DuckDB + overwrite.
    # Used for composite keys or when the optimized path fails.
    return _merge_iceberg_full_rewrite(table, new_data, unique_key, s3_config, conn)


def _merge_iceberg_full_rewrite(
    table: IcebergTable,
    new_data: pa.Table,
    unique_key: tuple[str, ...] | list[str],
    s3_config: S3Config,
    conn: duckdb.DuckDBPyConnection | None = None,
) -> int:
    """Full-rewrite merge: read entire table, ANTI JOIN in DuckDB, overwrite.

    This is the original approach that rewrites all rows. Used as a fallback
    when the optimized delete+append path is not applicable (composite keys)
    or when it fails.
    """
    own_conn = conn is None
    if own_conn:
        conn = duckdb.connect(":memory:")
    try:
        conn.register("new_data_raw", new_data)

        metadata_location = table.metadata_location
        try:
            if own_conn:
                _configure_s3(conn, s3_config)
            safe_location = _escape_sql_string(metadata_location)
            conn.execute(f"CREATE VIEW existing AS SELECT * FROM iceberg_scan('{safe_location}')")
        except (duckdb.Error, OSError) as exc:
            logger.warning(
                "DuckDB iceberg_scan failed (%s), falling back to PyIceberg full table scan",
                exc,
            )
            existing_arrow = table.scan().to_arrow()
            conn.register("existing", existing_arrow)

        col_names = ", ".join(new_data.column_names)
        conn.execute(
            "CREATE TABLE new_data AS SELECT *, ROW_NUMBER() OVER () AS _rn FROM new_data_raw"
        )

        safe_keys = _validate_identifiers(unique_key)
        key_cols = ", ".join(safe_keys)

        dedup_sql = f"""
            SELECT {col_names} FROM new_data
            QUALIFY ROW_NUMBER() OVER (
                PARTITION BY {key_cols} ORDER BY _rn DESC
            ) = 1
        """

        merge_sql = f"""
            WITH deduped AS ({dedup_sql})
            SELECT e.* FROM existing e
            WHERE NOT EXISTS (
                SELECT 1 FROM deduped d
                WHERE {" AND ".join(f"d.{k} = e.{k}" for k in safe_keys)}
            )
            UNION ALL
            SELECT * FROM deduped
        """

        merged = _to_arrow_table(conn.execute(merge_sql).arrow())
    finally:
        if own_conn:
            conn.close()

    table.overwrite(merged)
    return len(merged)


def append_iceberg(
    data: pa.Table,
    table_name: str,
    s3_config: S3Config,
    nessie_config: NessieConfig,
    location: str,
    branch: str = "main",
    partition_by: tuple[PartitionByEntry, ...] | None = None,
) -> int:
    """Append data to an existing Iceberg table (no overwrite).

    Idempotency: NOT SAFE. Each call appends all rows unconditionally.
    Re-running produces duplicate rows. If retries are needed, the caller
    should include a unique run_id or batch_id column to deduplicate later.

    For event/log tables where rows should always accumulate.
    Falls back to write_iceberg (create) if the table doesn't exist yet.

    Returns:
        Number of rows appended.
    """
    catalog = get_catalog(s3_config, nessie_config, branch=branch)

    ns_parts = table_name.rsplit(".", 1)
    if len(ns_parts) == 2:
        ensure_namespace(catalog, ns_parts[0])

    try:
        table = catalog.load_table(table_name)
        table.append(data)
        return len(data)
    except NoSuchTableError:
        return write_iceberg(
            data,
            table_name,
            s3_config,
            nessie_config,
            location,
            branch,
            partition_by=partition_by,
        )


def delete_insert_iceberg(
    new_data: pa.Table,
    table_name: str,
    unique_key: tuple[str, ...] | list[str],
    s3_config: S3Config,
    nessie_config: NessieConfig,
    location: str,
    branch: str = "main",
    conn: duckdb.DuckDBPyConnection | None = None,
    partition_by: tuple[PartitionByEntry, ...] | None = None,
) -> int:
    """Delete matching keys and insert all new rows (no dedup).

    Idempotency: SAFE when unique_key is set. The delete step removes any
    previously inserted rows for the same keys before re-inserting, so
    re-running with the same input produces identical results.

    Optimized path (single-column unique key):
        Uses PyIceberg delete(In(...)) + append() to write only the changed
        rows instead of rewriting the entire table.

    Fallback path (composite keys or if optimized path fails):
        Uses DuckDB ANTI JOIN + UNION ALL, then overwrites the full table.

    Unlike merge_iceberg, this does NOT deduplicate new_data.
    Falls back to write_iceberg if the table doesn't exist yet.

    Returns:
        Number of rows in the final table.
    """
    catalog = get_catalog(s3_config, nessie_config, branch=branch)

    ns_parts = table_name.rsplit(".", 1)
    if len(ns_parts) == 2:
        ensure_namespace(catalog, ns_parts[0])

    try:
        table = catalog.load_table(table_name)
    except NoSuchTableError:
        return write_iceberg(
            new_data,
            table_name,
            s3_config,
            nessie_config,
            location,
            branch,
            partition_by=partition_by,
        )

    # Optimized path: delete matching keys + append (no dedup, no full rewrite).
    optimized_result = _try_optimized_delete_append(table, new_data, unique_key)
    if optimized_result is not None:
        return optimized_result

    # Fallback: full ANTI JOIN + overwrite.
    return _delete_insert_full_rewrite(table, new_data, unique_key, s3_config, conn)


def _delete_insert_full_rewrite(
    table: IcebergTable,
    new_data: pa.Table,
    unique_key: tuple[str, ...] | list[str],
    s3_config: S3Config,
    conn: duckdb.DuckDBPyConnection | None = None,
) -> int:
    """Full-rewrite delete-insert: ANTI JOIN in DuckDB, overwrite (no dedup).

    Used as a fallback when the optimized delete+append path is not applicable.
    """
    own_conn = conn is None
    if own_conn:
        conn = duckdb.connect(":memory:")
    try:
        conn.register("new_data", new_data)

        metadata_location = table.metadata_location
        try:
            if own_conn:
                _configure_s3(conn, s3_config)
            safe_location = _escape_sql_string(metadata_location)
            conn.execute(f"CREATE VIEW existing AS SELECT * FROM iceberg_scan('{safe_location}')")
        except (duckdb.Error, OSError) as exc:
            logger.warning(
                "DuckDB iceberg_scan failed (%s), falling back to PyIceberg full table scan",
                exc,
            )
            existing_arrow = table.scan().to_arrow()
            conn.register("existing", existing_arrow)

        safe_keys = _validate_identifiers(unique_key)
        delete_insert_sql = f"""
            SELECT e.* FROM existing e
            WHERE NOT EXISTS (
                SELECT 1 FROM new_data d
                WHERE {" AND ".join(f"d.{k} = e.{k}" for k in safe_keys)}
            )
            UNION ALL
            SELECT * FROM new_data
        """

        merged = _to_arrow_table(conn.execute(delete_insert_sql).arrow())
    finally:
        if own_conn:
            conn.close()

    table.overwrite(merged)
    return len(merged)


def scd2_iceberg(
    new_data: pa.Table,
    table_name: str,
    unique_key: tuple[str, ...] | list[str],
    s3_config: S3Config,
    nessie_config: NessieConfig,
    location: str,
    valid_from_col: str = "valid_from",
    valid_to_col: str = "valid_to",
    branch: str = "main",
    conn: duckdb.DuckDBPyConnection | None = None,
    partition_by: tuple[PartitionByEntry, ...] | None = None,
) -> int:
    """SCD Type 2 merge — track history with valid_from/valid_to columns.

    Idempotency: PARTIALLY SAFE. Re-running closes and re-opens the same records,
    maintaining a consistent history chain. However, valid_from timestamps on
    the latest open records will reflect the most recent execution time
    (CURRENT_TIMESTAMP), so timestamps differ between runs.

    - Open records matching new keys → close them (set valid_to = CURRENT_TIMESTAMP)
    - Open records NOT matching → keep as-is
    - Already-closed historical records → keep unchanged
    - New records → add valid_from = CURRENT_TIMESTAMP, valid_to = NULL

    First run: adds SCD columns to new_data before writing.
    Falls back to write_iceberg if the table doesn't exist yet.

    Returns:
        Number of rows in the final table.
    """
    catalog = get_catalog(s3_config, nessie_config, branch=branch)

    ns_parts = table_name.rsplit(".", 1)
    if len(ns_parts) == 2:
        ensure_namespace(catalog, ns_parts[0])

    # Validate SCD column names and unique key up front
    safe_valid_from = _quote_identifier(valid_from_col)
    safe_valid_to = _quote_identifier(valid_to_col)
    safe_keys = _validate_identifiers(unique_key)

    try:
        table = catalog.load_table(table_name)
    except NoSuchTableError:
        # First run: add SCD columns to new_data
        own_conn_init = conn is None
        if own_conn_init:
            conn = duckdb.connect(":memory:")
        try:
            conn.register("new_data", new_data)
            col_names = ", ".join(new_data.column_names)
            init_sql = f"""
                SELECT {col_names},
                    CURRENT_TIMESTAMP AS {safe_valid_from},
                    NULL::TIMESTAMP AS {safe_valid_to}
                FROM new_data
            """
            init_data = _to_arrow_table(conn.execute(init_sql).arrow())
        finally:
            if own_conn_init:
                conn.close()
        return write_iceberg(
            init_data,
            table_name,
            s3_config,
            nessie_config,
            location,
            branch,
            partition_by=partition_by,
        )

    # Deduplicate new_data on unique_key (last row wins by position) to
    # prevent duplicate SCD2 inserts when new_data contains repeated keys.
    new_data = _dedup_new_data(new_data, unique_key, conn)

    own_conn = conn is None
    if own_conn:
        conn = duckdb.connect(":memory:")
    try:
        conn.register("new_data", new_data)

        metadata_location = table.metadata_location
        try:
            if own_conn:
                _configure_s3(conn, s3_config)
            safe_location = _escape_sql_string(metadata_location)
            conn.execute(f"CREATE VIEW existing AS SELECT * FROM iceberg_scan('{safe_location}')")
        except (duckdb.Error, OSError) as exc:
            logger.warning(
                "DuckDB iceberg_scan failed (%s), falling back to PyIceberg full table scan",
                exc,
            )
            existing_arrow = table.scan().to_arrow()
            conn.register("existing", existing_arrow)

        key_join = " AND ".join(f"n.{k} = e.{k}" for k in safe_keys)

        # Get all column names from existing table (includes SCD columns)
        existing_cols = [col for col in table.schema().names()]
        base_cols = [c for c in existing_cols if c not in (valid_from_col, valid_to_col)]
        safe_base_cols = [_quote_identifier(c) for c in base_cols]
        safe_all_cols = [_quote_identifier(c) for c in existing_cols]
        all_cols_str = ", ".join(safe_all_cols)

        scd2_sql = f"""
            -- Already-closed historical records: keep unchanged
            SELECT {all_cols_str} FROM existing
            WHERE {safe_valid_to} IS NOT NULL

            UNION ALL

            -- Open records matching new keys: close them
            SELECT {", ".join(f"e.{c}" for c in safe_base_cols)},
                e.{safe_valid_from},
                CURRENT_TIMESTAMP AS {safe_valid_to}
            FROM existing e
            WHERE e.{safe_valid_to} IS NULL
            AND EXISTS (
                SELECT 1 FROM new_data n WHERE {key_join}
            )

            UNION ALL

            -- Open records NOT matching new keys: keep as-is
            SELECT {all_cols_str} FROM existing e
            WHERE e.{safe_valid_to} IS NULL
            AND NOT EXISTS (
                SELECT 1 FROM new_data n WHERE {key_join}
            )

            UNION ALL

            -- New records: add SCD columns
            SELECT {", ".join(f"n.{c}" for c in safe_base_cols)},
                CURRENT_TIMESTAMP AS {safe_valid_from},
                NULL::TIMESTAMP AS {safe_valid_to}
            FROM new_data n
        """

        merged = _to_arrow_table(conn.execute(scd2_sql).arrow())
    finally:
        if own_conn:
            conn.close()

    table.overwrite(merged)
    return len(merged)


def snapshot_iceberg(
    new_data: pa.Table,
    table_name: str,
    partition_column: str,
    s3_config: S3Config,
    nessie_config: NessieConfig,
    location: str,
    branch: str = "main",
    conn: duckdb.DuckDBPyConnection | None = None,
    partition_by: tuple[PartitionByEntry, ...] | None = None,
) -> int:
    """Partition-aware overwrite — replace only partitions present in new_data.

    Idempotency: SAFE on partition_column. Only partitions present in new_data
    are replaced; untouched partitions are preserved. Re-running with the same
    partition values and data produces identical results.

    Optimized path: Uses PyIceberg delete(In(...)) on the partition column
    to delete only the touched partitions, then append() the new data.
    This avoids reading and rewriting the entire table.

    Fallback path: Reads entire table, filters partitions in DuckDB, overwrites.

    Untouched partitions are preserved as-is.
    Falls back to write_iceberg if the table doesn't exist yet.

    Returns:
        Number of rows in the final table.
    """
    catalog = get_catalog(s3_config, nessie_config, branch=branch)

    ns_parts = table_name.rsplit(".", 1)
    if len(ns_parts) == 2:
        ensure_namespace(catalog, ns_parts[0])

    try:
        table = catalog.load_table(table_name)
    except NoSuchTableError:
        return write_iceberg(
            new_data,
            table_name,
            s3_config,
            nessie_config,
            location,
            branch,
            partition_by=partition_by,
        )

    # Optimized path: delete touched partitions + append new data.
    # partition_column is always a single column, so In() works well.
    optimized_result = _try_optimized_delete_append(table, new_data, [partition_column])
    if optimized_result is not None:
        return optimized_result

    # Fallback: full rewrite.
    return _snapshot_full_rewrite(table, new_data, partition_column, s3_config, conn)


def _snapshot_full_rewrite(
    table: IcebergTable,
    new_data: pa.Table,
    partition_column: str,
    s3_config: S3Config,
    conn: duckdb.DuckDBPyConnection | None = None,
) -> int:
    """Full-rewrite snapshot: filter partitions in DuckDB, overwrite.

    Used as a fallback when the optimized delete+append path fails.
    """
    own_conn = conn is None
    if own_conn:
        conn = duckdb.connect(":memory:")
    try:
        conn.register("new_data", new_data)

        metadata_location = table.metadata_location
        try:
            if own_conn:
                _configure_s3(conn, s3_config)
            safe_location = _escape_sql_string(metadata_location)
            conn.execute(f"CREATE VIEW existing AS SELECT * FROM iceberg_scan('{safe_location}')")
        except (duckdb.Error, OSError) as exc:
            logger.warning(
                "DuckDB iceberg_scan failed (%s), falling back to PyIceberg full table scan",
                exc,
            )
            existing_arrow = table.scan().to_arrow()
            conn.register("existing", existing_arrow)

        safe_partition_col = _quote_identifier(partition_column)
        snapshot_sql = f"""
            SELECT * FROM existing
            WHERE {safe_partition_col} NOT IN (
                SELECT DISTINCT {safe_partition_col} FROM new_data
            )
            UNION ALL
            SELECT * FROM new_data
        """

        merged = _to_arrow_table(conn.execute(snapshot_sql).arrow())
    finally:
        if own_conn:
            conn.close()

    table.overwrite(merged)
    return len(merged)


def read_watermark(
    table_name: str,
    watermark_column: str,
    s3_config: S3Config,
    nessie_config: NessieConfig,
    branch: str = "main",
    conn: duckdb.DuckDBPyConnection | None = None,
) -> str | None:
    """Read the max watermark value from an Iceberg table.

    Returns None if the table doesn't exist or is empty.
    """
    catalog = get_catalog(s3_config, nessie_config, branch=branch)

    try:
        table = catalog.load_table(table_name)
    except NoSuchTableError:
        return None

    # Only read the watermark column — avoids a full table scan of all columns.
    existing = table.scan(selected_fields=(watermark_column,)).to_arrow()
    if len(existing) == 0:
        return None

    own_conn = conn is None
    if own_conn:
        conn = duckdb.connect(":memory:")
    try:
        conn.register("tbl", existing)
        safe_wm_col = _quote_identifier(watermark_column)
        result = conn.execute(f"SELECT MAX({safe_wm_col})::VARCHAR FROM tbl").fetchone()
        if result and result[0] is not None:
            return str(result[0])
        return None
    finally:
        if own_conn:
            conn.close()
