"""Entrypoint: ``python -m rat_catalog_nessie`` — start the catalog/v1 gRPC server."""

import logging
import os

# Proto stubs come from the shared `rat-protos` package (ADR-024 cleanup C).

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

if __name__ == "__main__":
    from rat_catalog_nessie.server import serve

    serve(int(os.environ.get("CATALOG_PORT", "50082")))
