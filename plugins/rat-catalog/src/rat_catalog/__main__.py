"""Entrypoint: ``python -m rat_catalog`` — start the catalog/v1 gRPC server."""

import logging
import os
import sys
from pathlib import Path

# gen/ on sys.path so generated stubs use bare imports (from catalog.v1 import ...).
_gen_dir = Path(__file__).parent / "gen"
if str(_gen_dir) not in sys.path:
    sys.path.insert(0, str(_gen_dir))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

if __name__ == "__main__":
    from rat_catalog.server import serve

    serve(int(os.environ.get("CATALOG_PORT", "50082")))
