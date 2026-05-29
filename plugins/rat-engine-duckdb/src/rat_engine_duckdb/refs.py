"""Runtime ref / landing-zone resolution for Python pipelines.

Relocated from the runner's templating module. Resolves a logical table ref to a
native ``iceberg_scan(...)`` fragment via the catalog. Runner-side Jinja keeps its
own compile-time copy for SQL; this engine-side copy backs the runtime ``ref()``
injected into Python pipelines (the part that MUST run where the code runs).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from rat_engine_duckdb.formats.iceberg import _escape_sql_string, get_catalog

if TYPE_CHECKING:
    from rat_engine_duckdb.config import NessieConfig, S3Config

logger = logging.getLogger("rat_engine_duckdb.refs")


def _resolve_ref(
    table_ref: str,
    namespace: str,
    s3_config: S3Config,
    nessie_config: NessieConfig,
) -> str:
    """Resolve a ref('...') to an iceberg_scan() expression.

    Supports 2-part 'layer.name' (auto-prefixes the namespace) and 3-part
    'ns.layer.name'. Resolves the exact metadata file from the catalog; falls back
    to the table directory path if the catalog is unreachable.
    """
    parts = table_ref.split(".", 2)
    if len(parts) == 2:
        ref_ns = namespace
        ref_layer, ref_name = parts
    elif len(parts) == 3:
        ref_ns, ref_layer, ref_name = parts
    else:
        raise ValueError(f"Invalid ref: '{table_ref}'. Expected 'layer.name' or 'ns.layer.name'.")

    try:
        catalog = get_catalog(s3_config, nessie_config)
        table = catalog.load_table(f"{ref_ns}.{ref_layer}.{ref_name}")
        return f"iceberg_scan('{_escape_sql_string(table.metadata_location)}')"
    except Exception as e:
        logger.warning("Failed to resolve ref '%s' via catalog, using fallback: %s", table_ref, e)
        table_path = f"s3://{s3_config.bucket}/{ref_ns}/{ref_layer}/{ref_name}/"
        return f"iceberg_scan('{_escape_sql_string(table_path)}', allow_moved_paths = true)"


def _resolve_landing_zone(zone_name: str, namespace: str, s3_config: S3Config) -> str:
    """Resolve landing_zone('name') to an S3 glob for raw files."""
    return f"s3://{s3_config.bucket}/{namespace}/landing/{zone_name}/**"
