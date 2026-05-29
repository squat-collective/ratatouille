"""Quality test discovery and execution — post-write validation on ephemeral branches."""

from __future__ import annotations

import re
import time
from pathlib import PurePosixPath
from typing import TYPE_CHECKING

from rat_runner.config import (
    S3Config,
    list_s3_keys,
    read_s3_text,
    read_s3_text_version,
)
from rat_runner.engine import QueryTimeoutError
from rat_runner.models import QualityTestResult, RunState

if TYPE_CHECKING:
    from collections.abc import Callable

    import pyarrow as pa

    from rat_runner.log import RunLogger

    # Compiles one raw quality-test SQL, returning (compiled_sql, run) where run()
    # executes it (local DuckDB or engine.Query) and returns the violation rows.
    # Two-step so the compiled SQL is captured for the result even if execution
    # raises. The executor supplies the right impl per mode.
    RunTest = Callable[[str], tuple[str, Callable[[], pa.Table]]]


def discover_quality_tests(
    s3_config: S3Config,
    namespace: str,
    layer: str,
    name: str,
) -> list[str]:
    """Discover quality test SQL files from S3.

    Looks for: {namespace}/pipelines/{layer}/{name}/tests/quality/*.sql
    """
    prefix = f"{namespace}/pipelines/{layer}/{name}/tests/quality/"
    return list_s3_keys(s3_config, prefix, suffix=".sql")


def discover_quality_tests_versioned(
    published_versions: dict[str, str],
    namespace: str,
    layer: str,
    name: str,
) -> list[str]:
    """Discover quality test keys from published_versions map."""
    prefix = f"{namespace}/pipelines/{layer}/{name}/tests/quality/"
    return sorted(k for k in published_versions if k.startswith(prefix) and k.endswith(".sql"))


_MAX_SAMPLE_ROWS = 3
_MAX_CELL_LENGTH = 40


def _truncate_cell(value: str, max_len: int = _MAX_CELL_LENGTH) -> str:
    """Truncate a cell value to avoid leaking PII in logs."""
    if len(value) <= max_len:
        return value
    return value[: max_len - 3] + "..."


def _format_sample_rows(
    table: pa.Table,
    max_rows: int = _MAX_SAMPLE_ROWS,
    max_cell: int = _MAX_CELL_LENGTH,
) -> str:
    """Format the first N rows of a PyArrow table as a readable text table.

    Cell values are truncated to ``max_cell`` characters to reduce the risk
    of logging PII-prone data (names, emails, addresses, etc.).
    """

    sliced = table.slice(0, max_rows)
    columns = sliced.column_names
    # Convert to Python lists for formatting, truncating long values
    col_data: dict[str, list[str]] = {}
    for col_name in columns:
        col_data[col_name] = [
            _truncate_cell(str(v.as_py()), max_cell) for v in sliced.column(col_name)
        ]

    # Calculate column widths
    widths = {
        col: max(len(col), *(len(v) for v in vals)) if vals else len(col)
        for col, vals in col_data.items()
    }

    # Header
    header = " | ".join(col.ljust(widths[col]) for col in columns)
    separator = "-+-".join("-" * widths[col] for col in columns)

    # Rows
    row_lines = []
    for i in range(len(sliced)):
        row = " | ".join(col_data[col][i].ljust(widths[col]) for col in columns)
        row_lines.append(row)

    parts = [header, separator] + row_lines
    if len(table) > max_rows:
        parts.append(f"... and {len(table) - max_rows} more row(s)")

    return "\n".join(parts)


def run_quality_test(
    sql: str,
    key: str,
    run_test: RunTest,
    log: RunLogger,
) -> QualityTestResult:
    """Run a single quality test SQL.

    A quality test returns violation rows. 0 rows = pass. ``run_test`` compiles the
    raw SQL and returns (compiled_sql, run); ``run()`` executes it (local DuckDB or
    the engine service). Severity/description/tags are parsed from `-- @key: value`
    headers (default severity: error).
    """
    test_name = PurePosixPath(key).stem
    severity = _parse_severity(sql)
    description = _parse_description(sql)
    tags = _parse_tags(sql)
    remediation = _parse_remediation(sql)

    compiled = ""
    start = time.monotonic()
    try:
        compiled, run = run_test(sql)
        log.debug(f"Quality test '{test_name}' SQL:\n{compiled}")
        result = run()
        row_count = len(result)
        elapsed_ms = int((time.monotonic() - start) * 1000)

        status = "pass" if row_count == 0 else "fail"
        message = "" if status == "pass" else f"{row_count} violation(s) found"

        sample = ""
        if status == "fail":
            sample = _format_sample_rows(result)
            log.warn(f"Quality test '{test_name}': {status} ({row_count} rows, {elapsed_ms}ms)")
            log.warn(f"Sample violations for '{test_name}':\n{sample}")
        else:
            log.info(f"Quality test '{test_name}': {status} ({row_count} rows, {elapsed_ms}ms)")

        if description:
            log.info(f"Quality test '{test_name}' description: {description}")

        return QualityTestResult(
            test_name=test_name,
            test_file=key,
            severity=severity,
            status=status,
            row_count=row_count,
            message=message,
            duration_ms=elapsed_ms,
            description=description,
            compiled_sql=compiled,
            sample_rows=sample,
            tags=tags,
            remediation=remediation,
        )
    except QueryTimeoutError as e:
        # Watchdog fired (local DuckDB path) — record a deliberate failure (NOT an
        # error) so the rest of the quality suite keeps executing. The exception
        # carries the actual deadline that tripped.
        elapsed_ms = int((time.monotonic() - start) * 1000)
        msg = str(e) or "quality test timed out"
        log.error(f"Quality test '{test_name}': {msg}")
        return QualityTestResult(
            test_name=test_name,
            test_file=key,
            severity=severity,
            status="fail",
            row_count=0,
            message=msg,
            duration_ms=elapsed_ms,
            description=description,
            compiled_sql=compiled,
            tags=tags,
            remediation=remediation,
        )
    except Exception as e:
        elapsed_ms = int((time.monotonic() - start) * 1000)
        log.error(f"Quality test '{test_name}' errored: {e}")
        return QualityTestResult(
            test_name=test_name,
            test_file=key,
            severity=severity,
            status="error",
            row_count=0,
            message=str(e),
            duration_ms=elapsed_ms,
            description=description,
            compiled_sql=compiled,
            tags=tags,
            remediation=remediation,
        )


def run_quality_tests(
    run: RunState,
    run_test: RunTest,
    s3_config: S3Config,
    log: RunLogger,
    *,
    published_versions: dict[str, str] | None = None,
) -> list[QualityTestResult]:
    """Discover and run all quality tests for a pipeline.

    When published_versions is provided, only published quality tests run
    (read from their pinned S3 version). When None, the pipeline has never
    been published — skip quality tests entirely.

    Returns list of results. Empty list if no tests found.
    """
    if published_versions is not None:
        keys = discover_quality_tests_versioned(
            published_versions,
            run.namespace,
            run.layer,
            run.pipeline_name,
        )
    else:
        log.info("Pipeline not published — skipping quality tests")
        return []

    if not keys:
        log.info("No quality tests found — skipping")
        return []

    log.info(f"Found {len(keys)} quality test(s)")
    results: list[QualityTestResult] = []

    for key in keys:
        vid = published_versions.get(key) if published_versions else None
        sql = read_s3_text_version(s3_config, key, vid) if vid else read_s3_text(s3_config, key)
        if sql is None:
            continue
        result = run_quality_test(sql, key, run_test, log)
        results.append(result)

    if results:
        passed = sum(1 for r in results if r.status == "pass")
        failed = sum(1 for r in results if r.status == "fail")
        errored = sum(1 for r in results if r.status == "error")
        log.info(f"Quality results: {passed} passed, {failed} failed, {errored} errored")
        for r in results:
            if r.status != "pass":
                if r.severity == "error":
                    log.error(f"  [{r.severity}] {r.test_name}: {r.status} — {r.message}")
                else:
                    log.warn(f"  [{r.severity}] {r.test_name}: {r.status} — {r.message}")

    return results


def has_error_failures(results: list[QualityTestResult]) -> bool:
    """Check if any quality test with severity 'error' has failed."""
    return any(r.severity == "error" and r.status in ("fail", "error") for r in results)


def _parse_severity(sql: str) -> str:
    """Parse `-- @severity: error|warn` from SQL comments. Defaults to 'error'.

    Scans all comment lines (not just the header) so annotations placed
    after the SQL body are also found.  Accepts 'warning' as an alias for 'warn'.
    """
    for line in sql.splitlines():
        stripped = line.strip()
        match = re.match(r"^--\s*@severity:\s*(\w+)", stripped)
        if match:
            val = match.group(1).lower()
            if val in ("warn", "warning"):
                return "warn"
            return "error"
    return "error"


def _parse_description(sql: str) -> str:
    """Parse `-- @description: ...` from SQL comments. Returns empty string if absent."""
    for line in sql.splitlines():
        stripped = line.strip()
        match = re.match(r"^--\s*@description:\s*(.+)$", stripped)
        if match:
            return match.group(1).strip()
    return ""


def _parse_tags(sql: str) -> tuple[str, ...]:
    """Parse `-- @tags: completeness, accuracy` from SQL comments.

    Returns a tuple of lowercase, trimmed tag strings.
    Returns empty tuple if absent.
    """
    for line in sql.splitlines():
        stripped = line.strip()
        match = re.match(r"^--\s*@tags:\s*(.+)$", stripped)
        if match:
            raw = match.group(1)
            tags = [t.strip().lower() for t in raw.split(",") if t.strip()]
            return tuple(tags)
    return ()


def _parse_remediation(sql: str) -> str:
    """Parse `-- @remediation: ...` from SQL comments. Returns empty string if absent."""
    for line in sql.splitlines():
        stripped = line.strip()
        match = re.match(r"^--\s*@remediation:\s*(.+)$", stripped)
        if match:
            return match.group(1).strip()
    return ""
