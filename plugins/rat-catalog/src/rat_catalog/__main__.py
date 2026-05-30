"""Entrypoint: ``python -m rat_catalog`` — start the catalog/v1 gRPC server."""

import logging
import os

# Proto stubs come from the shared `rat-protos` package (ADR-024 cleanup C):
# `from catalog.v1 import catalog_pb2` is satisfied without a per-plugin gen/ copy.

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

if __name__ == "__main__":
    from rat_catalog.server import serve

    serve(int(os.environ.get("CATALOG_PORT", "50082")))
