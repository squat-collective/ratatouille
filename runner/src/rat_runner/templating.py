"""SQL templating — Jinja compilation with ref() resolution."""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import jinja2
from jinja2.sandbox import SandboxedEnvironment

from rat_runner.models import PipelineConfig

if TYPE_CHECKING:
    from collections.abc import Callable

    from rat_runner.config import NessieConfig, S3Config

logger = logging.getLogger(__name__)


def extract_metadata(source: str) -> dict[str, str]:
    """Parse @key: value metadata headers from SQL (--) or Python (#) comments.

    Example:
        -- @description: Clean orders table
        # @merge_strategy: incremental
    """
    metadata: dict[str, str] = {}
    for line in source.splitlines():
        stripped = line.strip()
        match = re.match(r"^(?:--|#)\s*@(\w+):\s*(.+)$", stripped)
        if match:
            metadata[match.group(1)] = match.group(2).strip()
        elif stripped and not stripped.startswith("--") and not stripped.startswith("#"):
            break  # stop at first non-comment, non-empty line
    return metadata


def metadata_to_config(metadata: dict[str, str]) -> PipelineConfig:
    """Convert extracted @key: value metadata into a PipelineConfig."""
    unique_key_raw = metadata.get("unique_key", "")
    unique_key = (
        tuple(k.strip() for k in unique_key_raw.split(",") if k.strip()) if unique_key_raw else ()
    )
    return PipelineConfig(
        description=metadata.get("description", ""),
        materialized=metadata.get("materialized", "table"),
        unique_key=unique_key,
        merge_strategy=metadata.get("merge_strategy", "full_refresh"),
        watermark_column=metadata.get("watermark_column", ""),
        archive_landing_zones=metadata.get("archive_landing_zones", "").lower() == "true",
        partition_column=metadata.get("partition_column", ""),
        scd_valid_from=metadata.get("scd_valid_from", "valid_from"),
        scd_valid_to=metadata.get("scd_valid_to", "valid_to"),
    )


def extract_dependencies(sql: str) -> list[str]:
    """Extract ref('...') table references from SQL."""
    return re.findall(r"""ref\(\s*['"]([^'"]+)['"]\s*\)""", sql)


def extract_landing_zones(sql: str) -> list[str]:
    """Extract landing_zone('...') references from SQL."""
    return re.findall(r"""landing_zone\(\s*['"]([^'"]+)['"]\s*\)""", sql)


def compile_sql(
    raw_sql: str,
    namespace: str,
    layer: str,
    pipeline_name: str,
    s3_config: S3Config,
    nessie_config: NessieConfig,
    config: PipelineConfig | None = None,
    watermark_value: str | None = None,
    landing_zone_fn: Callable[[str], str] | None = None,
    plugin_helpers: dict[str, Callable[..., object]] | None = None,
    logical_refs: bool = False,
) -> str:
    """Compile a Jinja SQL template with ref() resolution.

    With ``logical_refs=True``, ref()/this resolve to an engine-neutral
    ``"layer"."name"`` identifier with NO catalog I/O — the decoupled engine path
    (ADR-024), where the engine maps each logical name to a native scan via the
    input TableDescriptor. Otherwise they resolve to ``iceberg_scan()`` via the
    catalog (the legacy in-process path).

    Available in templates:
    - ref('layer.name') or ref('ns.layer.name') — resolves to iceberg_scan() via catalog
    - this — the current pipeline's target table identifier (also uses iceberg_scan)
    - run_started_at — ISO timestamp of the current run
    - is_incremental() — True when config.merge_strategy == "incremental"
    - watermark_value — max value of the watermark column (incremental pipelines)
    """
    run_started_at = datetime.now(UTC).isoformat()

    def ref_fn(table_ref: str) -> str:
        if logical_refs:
            return _logical_ref(table_ref)
        return _resolve_ref(table_ref, namespace, s3_config, nessie_config)

    if landing_zone_fn is None:

        def landing_zone_fn(zone_name: str) -> str:
            return _resolve_landing_zone(zone_name, namespace, s3_config)

    def is_incremental() -> bool:
        return config is not None and config.merge_strategy == "incremental"

    def is_scd2() -> bool:
        return config is not None and config.merge_strategy == "scd2"

    def is_snapshot() -> bool:
        return config is not None and config.merge_strategy == "snapshot"

    def is_append_only() -> bool:
        return config is not None and config.merge_strategy == "append_only"

    def is_delete_insert() -> bool:
        return config is not None and config.merge_strategy == "delete_insert"

    # Build the target "this" identifier — resolves to iceberg_scan() like ref()
    this = ref_fn(f"{layer}.{pipeline_name}")

    env = SandboxedEnvironment(undefined=jinja2.StrictUndefined)
    template = env.from_string(raw_sql)

    template_vars: dict[str, object] = {
        "ref": ref_fn,
        "landing_zone": landing_zone_fn,
        "this": this,
        "run_started_at": run_started_at,
        "is_incremental": is_incremental,
        "is_scd2": is_scd2,
        "is_snapshot": is_snapshot,
        "is_append_only": is_append_only,
        "is_delete_insert": is_delete_insert,
        "watermark_value": watermark_value,
    }

    # Register plugin Jinja helpers (won't override built-in vars)
    if plugin_helpers:
        for name, helper in plugin_helpers.items():
            if name not in template_vars:
                template_vars[name] = helper
            else:
                logger.warning("Plugin Jinja helper '%s' conflicts with built-in, skipping", name)

    rendered = template.render(**template_vars)

    # Strip metadata comment lines from output
    lines = rendered.splitlines()
    output_lines: list[str] = []
    for line in lines:
        if re.match(r"^\s*(?:--|#)\s*@\w+:", line):
            continue
        output_lines.append(line)

    return "\n".join(output_lines).strip()


def _resolve_ref(
    table_ref: str,
    namespace: str,
    s3_config: S3Config,
    nessie_config: NessieConfig,
) -> str:
    """Resolve a ref('...') to an iceberg_scan() expression.

    Supports:
    - 2-part: "layer.name" — auto-prefixes current namespace
    - 3-part: "ns.layer.name" — cross-namespace reference

    Resolves the exact metadata file path from the Nessie catalog so DuckDB
    doesn't need version-hint.text or unsafe version guessing.
    Falls back to the table directory path if the catalog is unreachable.
    """
    parts = table_ref.split(".", 2)
    if len(parts) == 2:
        ref_ns = namespace
        ref_layer, ref_name = parts
    elif len(parts) == 3:
        ref_ns, ref_layer, ref_name = parts
    else:
        raise ValueError(f"Invalid ref: '{table_ref}'. Expected 'layer.name' or 'ns.layer.name'.")

    from rat_runner.iceberg import _escape_sql_string

    # Try to resolve the exact metadata file from the Nessie catalog.
    # Passing the .metadata.json path directly to iceberg_scan() avoids
    # the need for version-hint.text or unsafe version guessing.
    try:
        from rat_runner.iceberg import get_catalog

        catalog = get_catalog(s3_config, nessie_config)
        table_name = f"{ref_ns}.{ref_layer}.{ref_name}"
        table = catalog.load_table(table_name)
        metadata_location = table.metadata_location
        safe_location = _escape_sql_string(metadata_location)
        return f"iceberg_scan('{safe_location}')"
    except Exception as e:
        logger.warning("Failed to resolve ref '%s' via catalog, using fallback: %s", table_ref, e)
        # Fallback: use the table directory path (requires version-hint.text)
        table_path = f"s3://{s3_config.bucket}/{ref_ns}/{ref_layer}/{ref_name}/"
        safe_path = _escape_sql_string(table_path)
        return f"iceberg_scan('{safe_path}', allow_moved_paths = true)"


def _logical_ref(table_ref: str) -> str:
    """Resolve ref('layer.name' | 'ns.layer.name') to an engine-neutral "layer"."name".

    Used for the decoupled engine path (ADR-024): the runner emits logical names and
    the engine maps each to a native scan via the input TableDescriptor — no catalog
    I/O at compile time. (Namespace-qualified resolution is a follow-on; the engine
    currently registers input views by layer.name.)
    """
    parts = table_ref.split(".", 2)
    if len(parts) == 2:
        layer, name = parts
    elif len(parts) == 3:
        _, layer, name = parts
    else:
        raise ValueError(f"Invalid ref: '{table_ref}'. Expected 'layer.name' or 'ns.layer.name'.")
    return f'"{layer}"."{name}"'


def validate_landing_zones(
    sql: str,
    namespace: str,
    s3_config: S3Config,
) -> list[str]:
    """Check referenced landing zones and return warnings for empty ones.

    When multiple landing zones are referenced, S3 LIST calls are issued
    concurrently to avoid sequential latency.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    from rat_runner.config import list_s3_keys

    zones = extract_landing_zones(sql)
    if not zones:
        return []

    warnings: list[str] = []

    def _check_zone(zone: str) -> str | None:
        prefix = f"{namespace}/landing/{zone}/"
        keys = list_s3_keys(s3_config, prefix)
        if not keys:
            return f"Landing zone '{zone}' has no files at s3://{s3_config.bucket}/{prefix}"
        return None

    if len(zones) == 1:
        result = _check_zone(zones[0])
        if result:
            warnings.append(result)
    else:
        with ThreadPoolExecutor(max_workers=min(len(zones), 4)) as pool:
            futures = {pool.submit(_check_zone, zone): zone for zone in zones}
            for future in as_completed(futures):
                result = future.result()
                if result:
                    warnings.append(result)

    return warnings


def validate_template(raw_sql: str) -> tuple[list[str], list[str]]:
    """Validate Jinja template syntax and common anti-patterns.

    Returns (errors, warnings):
    - errors: fatal issues that prevent safe execution
    - warnings: suspicious patterns that might indicate mistakes
    """
    errors: list[str] = []
    warnings: list[str] = []

    # 1. Check Jinja syntax (unclosed tags, etc.)
    env = SandboxedEnvironment(undefined=jinja2.StrictUndefined)
    try:
        env.parse(raw_sql)
    except jinja2.TemplateSyntaxError as e:
        errors.append(f"Jinja syntax error: {e}")
        return errors, warnings

    # 2. Detect nested Jinja inside function calls — e.g. ref('{{this}}')
    nested_pattern = re.compile(r"""(?:ref|landing_zone)\(\s*['"].*\{\{.*\}\}.*['"]\s*\)""")
    for match in nested_pattern.finditer(raw_sql):
        errors.append(f"Nested Jinja inside function call: {match.group()}")

    # 3. Bare ref() or landing_zone() outside {{ }} delimiters
    # Find all ref(...) and landing_zone(...) calls, then check if they're inside
    # {{ }}, {% %}, or SQL comments (-- or /* */).
    bare_pattern = re.compile(r"""(?:ref|landing_zone)\(\s*['"][^'"]+['"]\s*\)""")
    for match in bare_pattern.finditer(raw_sql):
        start = match.start()

        # Skip matches inside SQL line comments (-- ...)
        line_start = raw_sql.rfind("\n", 0, start) + 1  # start of current line
        line_prefix = raw_sql[line_start:start]
        if "--" in line_prefix:
            continue

        # Skip matches inside SQL block comments (/* ... */)
        last_block_open = raw_sql.rfind("/*", 0, start)
        last_block_close = raw_sql.rfind("*/", 0, start)
        if last_block_open != -1 and last_block_open > last_block_close:
            continue

        prefix = raw_sql[:start]

        # Check if this call is inside {{ ... }} by looking backwards for {{
        last_open = prefix.rfind("{{")
        last_close = prefix.rfind("}}")
        if last_open != -1 and last_close < last_open:
            continue  # inside {{ }}

        # Check if this call is inside {% ... %} by looking backwards for {%
        last_block_jinja_open = prefix.rfind("{%")
        last_block_jinja_close = prefix.rfind("%}")
        if last_block_jinja_open != -1 and last_block_jinja_close < last_block_jinja_open:
            continue  # inside {% %}

        warnings.append(f"Bare function call outside Jinja delimiters: {match.group()}")

    return errors, warnings


def _resolve_landing_zone(zone_name: str, namespace: str, s3_config: S3Config) -> str:
    """Resolve landing_zone('name') to S3 glob for raw files.

    DuckDB's read_csv_auto / read_parquet on S3 requires a glob pattern —
    a bare directory path (trailing slash) is treated as a single-file read
    and returns 404. We use /** to match all files recursively.
    """
    return f"s3://{s3_config.bucket}/{namespace}/landing/{zone_name}/**"


def _resolve_landing_zone_preview(
    zone_name: str,
    namespace: str,
    s3_config: S3Config,
    warnings: list[str],
) -> str:
    """Resolve landing_zone() for preview — prefers _samples/ subfolder."""
    from rat_runner.config import list_s3_keys

    samples_prefix = f"{namespace}/landing/{zone_name}/_samples/"
    if list_s3_keys(s3_config, samples_prefix):
        return f"s3://{s3_config.bucket}/{namespace}/landing/{zone_name}/_samples/**"
    warnings.append(
        f"No sample files for landing zone '{zone_name}' (looked in _samples/). Using all files."
    )
    return f"s3://{s3_config.bucket}/{namespace}/landing/{zone_name}/**"
