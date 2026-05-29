"""Python pipeline executor — exec() with injected globals.

Security: The sandbox uses multiple layers of defense:
1. Blocked builtins (eval, exec, open, getattr, type, etc.)
2. Blocked module imports (os, sys, subprocess, etc.)
3. AST validation rejecting dunder attribute access (__class__, __subclasses__, etc.)
4. Restricted DuckDB connection wrapper blocking dangerous commands

These layers are defense-in-depth on top of the container-level trust boundary
(read-only fs, dropped capabilities, no-new-privileges); they MUST NOT be relied
upon for new threat models — see docs/adr/017-python-pipeline-trust-model.md for
the trust contract.
"""

from __future__ import annotations

import ast
import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import pyarrow as pa

from rat_engine_duckdb.refs import _resolve_landing_zone, _resolve_ref

if TYPE_CHECKING:
    from collections.abc import Callable

    import duckdb

    from rat_engine_duckdb.config import NessieConfig, PipelineConfig, S3Config
    from rat_engine_duckdb.duckdb_engine import DuckDBEngine

# Builtins blocked in pipeline exec() to reduce sandbox escape surface.
# Pipelines should use the injected globals (duckdb_conn, pa, ref) instead.
# NOTE: __import__ is NOT in this set — it's replaced with a restricted version
# because PyArrow's C++ code calls __import__ internally and crashes (SIGABRT)
# if it's missing.
_BLOCKED_BUILTINS = frozenset(
    {
        "eval",  # arbitrary code evaluation
        "exec",  # nested exec
        "compile",  # code compilation
        "open",  # filesystem access
        "breakpoint",  # debugger
        "exit",  # process exit
        "quit",  # process exit
        "globals",  # introspect global scope
        "locals",  # introspect local scope
        "vars",  # introspect object dict
        "dir",  # object introspection
        "getattr",  # attribute access (can reach __subclasses__)
        "setattr",  # attribute mutation
        "delattr",  # attribute deletion
        "memoryview",  # low-level memory access
        "input",  # stdin (would block the thread)
        "help",  # interactive help (would block)
        "type",  # metaclass introspection (sandbox escape via __subclasses__)
        "isinstance",  # can be used for type introspection chains
        "hasattr",  # attribute probing
        "classmethod",  # descriptor manipulation
        "staticmethod",  # descriptor manipulation
        "property",  # descriptor manipulation
        "super",  # MRO traversal
    }
)

# Modules that pipeline code is NOT allowed to import directly.
# Internal imports by PyArrow/DuckDB (e.g. decimal, json) are allowed
# because they happen in the library's own module scope.
_BLOCKED_MODULES = frozenset(
    {
        "os",
        "sys",
        "subprocess",
        "shutil",
        "signal",
        "socket",
        "http",
        "ftplib",
        "smtplib",
        "telnetlib",
        "xmlrpc",
        "ctypes",
        "importlib",
        "code",
        "codeop",
        "compileall",
        "py_compile",
        "runpy",
        "pathlib",
        "tempfile",
        "glob",
        "fnmatch",
        "multiprocessing",
        "threading",
        "concurrent",
        "webbrowser",
        "antigravity",
        "turtle",
        "pickle",
        "shelve",
        "marshal",
        "builtins",
        "inspect",
        "gc",
        "types",
        "abc",
    }
)

# Dunder attributes that enable sandbox escape via object introspection.
_BLOCKED_DUNDERS = frozenset(
    {
        "__class__",
        "__bases__",
        "__base__",
        "__mro__",
        "__subclasses__",
        "__globals__",
        "__code__",
        "__builtins__",
        "__import__",
        "__loader__",
        "__spec__",
        "__qualname__",
        "__func__",
        "__self__",
        "__wrapped__",
        "__closure__",
        "__dict__",
        "__init_subclass__",
        "__set_name__",
        "__reduce__",
        "__reduce_ex__",
        "__getstate__",
        "__setstate__",
    }
)

# DuckDB SQL commands that could be used for file system access or code execution.
_DANGEROUS_SQL_PATTERN = re.compile(
    r"\b(COPY|ATTACH|INSTALL|LOAD|CREATE\s+MACRO|IMPORT|EXPORT)\b",
    re.IGNORECASE,
)


class _SandboxViolationError(Exception):
    """Raised when pipeline code attempts a blocked operation."""


class _DunderAccessChecker(ast.NodeVisitor):
    """AST visitor that rejects access to dangerous dunder attributes.

    Catches patterns like:
    - obj.__class__
    - obj.__bases__
    - obj.__subclasses__()
    - x.__globals__
    """

    def visit_Attribute(self, node: ast.Attribute) -> None:  # noqa: N802 — ast.NodeVisitor API mandates the CamelCase node name
        if node.attr in _BLOCKED_DUNDERS:
            raise _SandboxViolationError(
                f"Access to '{node.attr}' is not allowed in pipelines (line {node.lineno})"
            )
        self.generic_visit(node)

    def visit_Subscript(self, node: ast.Subscript) -> None:  # noqa: N802 — ast.NodeVisitor API mandates the CamelCase node name
        # Block dict-style access like obj["__class__"]
        if (
            isinstance(node.slice, ast.Constant)
            and isinstance(node.slice.value, str)
            and node.slice.value in _BLOCKED_DUNDERS
        ):
            raise _SandboxViolationError(
                f"Access to '{node.slice.value}' via subscript is not allowed "
                f"in pipelines (line {node.lineno})"
            )
        self.generic_visit(node)


def _validate_source(source: str) -> None:
    """Parse and validate pipeline source code before execution.

    Raises _SandboxViolationError if the code attempts to access blocked attributes.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError as e:
        raise _SandboxViolationError(f"Pipeline syntax error: {e}") from e

    _DunderAccessChecker().visit(tree)

    # Also check for string-based dunder access attempts in string literals
    # (e.g., getattr(obj, "__class__") — getattr is already blocked, but defense in depth)
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and node.value in _BLOCKED_DUNDERS
        ):
            raise _SandboxViolationError(
                f"String literal '{node.value}' references a blocked attribute (line {node.lineno})"
            )


class _SafeDuckDBConnection:
    """Wrapper around DuckDB connection that blocks dangerous SQL commands.

    Prevents pipeline code from using COPY (file write), ATTACH (file access),
    INSTALL/LOAD (extension loading), and other dangerous commands.
    Uses __getattribute__ to intercept ALL attribute access including private ones.
    """

    def __init__(self, conn: duckdb.DuckDBPyConnection) -> None:
        # Use object.__setattr__ to bypass our __getattribute__
        object.__setattr__(self, "_conn", conn)

    def execute(self, query: str, parameters: Any = None) -> Any:
        """Execute SQL after validating it doesn't contain dangerous commands."""
        if _DANGEROUS_SQL_PATTERN.search(query):
            raise _SandboxViolationError(
                f"SQL command not allowed in pipelines. Blocked pattern found in: {query[:100]}..."
            )
        conn = object.__getattribute__(self, "_conn")
        if parameters is not None:
            return conn.execute(query, parameters)
        return conn.execute(query)

    def sql(self, query: str) -> Any:
        """Execute SQL via .sql() after validation."""
        if _DANGEROUS_SQL_PATTERN.search(query):
            raise _SandboxViolationError(
                f"SQL command not allowed in pipelines. Blocked pattern found in: {query[:100]}..."
            )
        conn = object.__getattribute__(self, "_conn")
        return conn.sql(query)

    def __getattribute__(self, name: str) -> Any:
        """Intercept all attribute access — block private/internal attributes."""
        # Allow our own public methods
        if name in ("execute", "sql"):
            return object.__getattribute__(self, name)
        # Block access to private attributes (prevents reaching the real connection)
        if name.startswith("_"):
            raise _SandboxViolationError(
                f"Access to private attribute '{name}' on duckdb_conn is not allowed"
            )
        # Proxy safe public attributes to the underlying connection
        conn = object.__getattribute__(self, "_conn")
        return getattr(conn, name)


def _restricted_import(name: str, *args: object, **kwargs: object) -> object:
    """Import function that blocks dangerous top-level modules."""
    top_level = name.split(".")[0]
    if top_level in _BLOCKED_MODULES:
        raise ImportError(f"Import of '{name}' is not allowed in pipelines")
    return (
        __builtins__["__import__"](name, *args, **kwargs)
        if isinstance(__builtins__, dict)
        else __builtins__.__import__(name, *args, **kwargs)
    )  # type: ignore[union-attr]


def execute_python_pipeline(
    source: str,
    engine: DuckDBEngine,
    namespace: str,
    layer: str,
    name: str,
    s3_config: S3Config,
    nessie_config: NessieConfig,
    config: PipelineConfig | None = None,
    run_started_at: str | None = None,
    logger: Any | None = None,
    landing_zone_fn: Callable[[str], str] | None = None,
) -> pa.Table:
    """Execute a Python pipeline via exec() and extract the `result` variable.

    The script runs with injected globals:
    - duckdb_conn: the DuckDB connection (with S3 configured, dangerous commands blocked)
    - pa: PyArrow module
    - ref(table_ref): resolves to a parquet glob path string
    - this: current table identifier (e.g., "ns.silver.orders")
    - run_started_at: ISO timestamp
    - is_incremental: bool
    - config: PipelineConfig (or None)

    The script MUST set `result` to a PyArrow Table.

    Security: Source code is validated via AST analysis before execution.
    Dunder attribute access (__class__, __subclasses__, etc.) is blocked.
    """
    # Validate source code BEFORE execution — reject sandbox escape attempts
    _validate_source(source)

    if run_started_at is None:
        run_started_at = datetime.now(UTC).isoformat()

    def ref_fn(table_ref: str) -> str:
        return _resolve_ref(table_ref, namespace, s3_config, nessie_config)

    if landing_zone_fn is None:

        def landing_zone_fn(zone_name: str) -> str:
            return _resolve_landing_zone(zone_name, namespace, s3_config)

    is_incremental = config is not None and config.merge_strategy == "incremental"
    this = f"{namespace}.{layer}.{name}"

    # Restricted builtins — remove dangerous functions, replace __import__
    # __builtins__ is a dict in non-__main__ modules, a module in __main__
    builtins_dict = (
        __builtins__ if isinstance(__builtins__, dict) else __builtins__.__dict__  # type: ignore[union-attr]
    )
    safe_builtins = {k: v for k, v in builtins_dict.items() if k not in _BLOCKED_BUILTINS}
    safe_builtins["__import__"] = _restricted_import

    # Wrap DuckDB connection to block dangerous SQL commands
    safe_conn = _SafeDuckDBConnection(engine.conn)

    globals_dict: dict = {
        "__builtins__": safe_builtins,
        "duckdb_conn": safe_conn,
        "pa": pa,
        "ref": ref_fn,
        "landing_zone": landing_zone_fn,
        "this": this,
        "run_started_at": run_started_at,
        "is_incremental": is_incremental,
        "config": config,
        "result": None,
    }
    if logger is not None:
        globals_dict["log"] = logger

    exec(source, globals_dict)  # noqa: S102

    result = globals_dict.get("result")
    if result is None or not isinstance(result, pa.Table):
        raise ValueError(
            "Python pipeline must set `result` to a PyArrow Table. "
            f"Got: {type(result).__name__ if result is not None else 'None'}"
        )

    return result
