"""Entrypoint: ``python -m rat_engine_duckdb`` — start the engine/v1 gRPC server."""

import logging
import os

# Proto stubs come from the shared `rat-protos` package (ADR-024 cleanup C):
# `from engine.v1 import engine_pb2` is satisfied without a per-plugin gen/ copy.

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

if __name__ == "__main__":
    from rat_engine_duckdb.server import serve

    port = int(os.environ.get("ENGINE_PORT", "50081"))
    serve(port)
