"""Entrypoint for the runner service: python -m rat_runner.

Modes:
  RUN_MODE=single  — Execute a single pipeline from env vars, print JSON result, exit.
  RUN_MODE=server   — Start the gRPC server (default).
"""

import logging
import os
import subprocess
import sys
from pathlib import Path

# Proto stubs come from the shared `rat-protos` package (ADR-024 cleanup C):
# `from common.v1 import common_pb2` is satisfied without any per-service gen/.
# Configure logging — JSON one-object-per-line so ratd's slog output and
# runner output are interoperable for cross-service grep'ing by request_id.
from rat_runner.json_log import configure_json_logging  # noqa: E402

configure_json_logging(level=logging.INFO)

_logger = logging.getLogger("rat_runner.startup")

_PLUGIN_DIR = Path(os.environ.get("RAT_PLUGIN_DIR", "/plugins"))
_INSTALLED_FLAG = "_RAT_PLUGINS_INSTALLED"


def _auto_install_plugins() -> None:
    """pip-install every package found in the plugin directory.

    Scans RAT_PLUGIN_DIR (default ``/plugins``) for subdirectories containing a
    ``pyproject.toml``. Each one is installed with ``pip install --user`` so
    entry-points are registered and discoverable by the PluginRegistry.

    After a successful install the process re-execs itself so the new
    packages are on the metadata search path from the start.
    """
    # Skip if we already installed on a previous exec (avoids infinite loop).
    if os.environ.get(_INSTALLED_FLAG):
        return

    if not _PLUGIN_DIR.is_dir():
        return

    packages = sorted(p.parent for p in _PLUGIN_DIR.glob("*/pyproject.toml"))
    if not packages:
        return

    _logger.info(
        "Auto-installing %d plugin(s) from %s: %s",
        len(packages),
        _PLUGIN_DIR,
        ", ".join(p.name for p in packages),
    )

    cmd = [
        sys.executable,
        "-m",
        "pip",
        "install",
        "--user",
        "--quiet",
        "--no-cache-dir",
        *[str(p) for p in packages],
    ]
    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True)
        _logger.info("Plugin auto-install complete — re-execing to pick up new packages")
    except subprocess.CalledProcessError as exc:
        _logger.warning(
            "Plugin auto-install failed (exit %d): %s",
            exc.returncode,
            exc.stderr.strip(),
        )
        return

    # Re-exec so importlib.metadata sees the newly installed packages.
    os.environ[_INSTALLED_FLAG] = "1"
    os.execv(sys.executable, [sys.executable, "-m", "rat_runner"])


if __name__ == "__main__":
    _auto_install_plugins()

    run_mode = os.environ.get("RUN_MODE", "server").lower()

    if run_mode == "single":
        from rat_runner.single_shot import run_single  # noqa: E402

        run_single()
    else:
        from rat_runner.server import serve  # noqa: E402

        port = int(os.environ.get("GRPC_PORT", "50052"))
        serve(port)
