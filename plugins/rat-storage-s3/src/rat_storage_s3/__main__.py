"""Entrypoint: ``python -m rat_storage_s3`` — start the storage/v1 gRPC server."""

import logging
import os

# Proto stubs come from the shared `rat-protos` package (ADR-024 cleanup C):
# `from storage.v1 import storage_pb2` is satisfied without a per-plugin gen/ copy.

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

if __name__ == "__main__":
    from rat_storage_s3.server import serve

    serve(int(os.environ.get("STORAGE_PORT", "50083")))
