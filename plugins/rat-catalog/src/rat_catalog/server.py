"""gRPC server for catalog/v1 — the protocol-aware catalog axis (ADR-024).

Describe is live (capabilities depend on CATALOG_PROTOCOL). Discovery + branch
lifecycle (relocating nessie.py branch ops + PyIceberg discovery, with ducklake /
lakekeeper variants) land in the next increment — stubbed UNIMPLEMENTED for now so
the runner can negotiate capabilities before wiring through.
"""

from __future__ import annotations

import logging
import os
from concurrent import futures
from typing import Any

import grpc
from catalog.v1 import (  # type: ignore[import-untyped]
    catalog_pb2,
    catalog_pb2_grpc,
)

from rat_catalog import __version__

_logger = logging.getLogger("rat_catalog.server")

GRPC_MAX_WORKERS = 10

# Which catalog this instance fronts (set per data-plane composition).
_PROTOCOL = os.environ.get("CATALOG_PROTOCOL", "iceberg-rest")
_CAPABILITIES = {
    "iceberg-rest": ["branching", "time_travel", "history"],  # Nessie
    "lakekeeper": ["time_travel"],
    "ducklake": ["time_travel"],
}
_FORMATS = {"iceberg-rest": ["iceberg"], "lakekeeper": ["iceberg"], "ducklake": ["ducklake"]}


class CatalogServicer(catalog_pb2_grpc.CatalogServiceServicer):
    """Protocol-aware catalog/v1 implementation."""

    def Describe(self, request: Any, context: grpc.ServicerContext) -> Any:  # noqa: N802
        return catalog_pb2.DescribeResponse(
            name=f"rat-catalog-{_PROTOCOL}",
            version=__version__,
            capabilities=_CAPABILITIES.get(_PROTOCOL, []),
            formats=_FORMATS.get(_PROTOCOL, ["iceberg"]),
        )

    def ListNamespaces(self, request: Any, context: grpc.ServicerContext) -> Any:  # noqa: N802
        context.abort(grpc.StatusCode.UNIMPLEMENTED, "discovery lands in the next increment")

    def ListTables(self, request: Any, context: grpc.ServicerContext) -> Any:  # noqa: N802
        context.abort(grpc.StatusCode.UNIMPLEMENTED, "discovery lands in the next increment")

    def GetTable(self, request: Any, context: grpc.ServicerContext) -> Any:  # noqa: N802
        context.abort(grpc.StatusCode.UNIMPLEMENTED, "GetTable lands in the next increment")

    def CreateBranch(self, request: Any, context: grpc.ServicerContext) -> Any:  # noqa: N802
        context.abort(grpc.StatusCode.UNIMPLEMENTED, "branch lifecycle lands in the next increment")

    def MergeBranch(self, request: Any, context: grpc.ServicerContext) -> Any:  # noqa: N802
        context.abort(grpc.StatusCode.UNIMPLEMENTED, "branch lifecycle lands in the next increment")

    def DeleteBranch(self, request: Any, context: grpc.ServicerContext) -> Any:  # noqa: N802
        context.abort(grpc.StatusCode.UNIMPLEMENTED, "branch lifecycle lands in the next increment")

    def GetHistory(self, request: Any, context: grpc.ServicerContext) -> Any:  # noqa: N802
        context.abort(grpc.StatusCode.UNIMPLEMENTED, "history lands in the next increment")


def serve(port: int = 50082) -> None:
    """Start the catalog/v1 gRPC server and block until termination."""
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=GRPC_MAX_WORKERS))
    catalog_pb2_grpc.add_CatalogServiceServicer_to_server(CatalogServicer(), server)
    server.add_insecure_port(f"[::]:{port}")
    _logger.info("rat-catalog (%s, catalog/v1) serving on :%d", _PROTOCOL, port)
    server.start()
    server.wait_for_termination()
