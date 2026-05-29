"""Entrypoint: ``python -m rat_engine_duckdb`` — start the engine/v1 gRPC server."""

import logging
import os
import sys
from pathlib import Path

# Add gen/ to sys.path so generated proto stubs use bare imports
# (e.g. `from engine.v1 import engine_pb2`). Mirrors the runner's __main__.
_gen_dir = Path(__file__).parent / "gen"
if str(_gen_dir) not in sys.path:
    sys.path.insert(0, str(_gen_dir))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

if __name__ == "__main__":
    from rat_engine_duckdb.server import serve

    port = int(os.environ.get("ENGINE_PORT", "50081"))
    serve(port)
