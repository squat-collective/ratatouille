"""Shared SQL / connection helpers for the engine and its format adapters.

Format-agnostic — anything iceberg- or ducklake-specific lives in its own
plugin (`rat-format-iceberg`, `rat-format-ducklake`). These three helpers are
the only engine internals format adapters need to share.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import duckdb

    from rat_engine_duckdb.config import S3Config


def escape_sql_string(value: str) -> str:
    """SQL-escape a single-quoted string literal."""
    return value.replace("'", "''")


def quote_identifier(name: str) -> str:
    """Quote a SQL identifier; reject anything that isn't a safe identifier."""
    if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", name):
        raise ValueError(f"unsafe SQL identifier: {name!r}")
    return f'"{name}"'


def configure_s3(conn: duckdb.DuckDBPyConnection, s3_config: S3Config) -> None:
    """Install httpfs + iceberg and configure S3 access on the connection."""
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


# Aliases preserving the underscore-prefixed names some callers still use.
_escape_sql_string = escape_sql_string
_quote_identifier = quote_identifier
_configure_s3 = configure_s3
