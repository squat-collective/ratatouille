"""gRPC server for catalog/v1 — the Lakekeeper catalog axis (ADR-024).

Lakekeeper is an Iceberg REST catalog with a named-warehouse + bearer-token model.
No branch lifecycle (so CreateBranch/MergeBranch/DeleteBranch return
FAILED_PRECONDITION). Discovery is the catalog's own REST API; here it returns
UNIMPLEMENTED until the lakekeeper /v1/namespaces walk is wired up.
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
from common.v1 import data_plane_pb2  # type: ignore[import-untyped]

from rat_catalog_lakekeeper import __version__

_logger = logging.getLogger("rat_catalog_lakekeeper.server")

GRPC_MAX_WORKERS = 10

_CAPABILITIES = ["time_travel"]
_FORMAT = "iceberg"
_LAYER_NAMES = {1: "bronze", 2: "silver", 3: "gold"}


def _catalog_descriptor() -> Any:
    """Vend the lakekeeper-style CatalogDescriptor the engine uses (named warehouse + token)."""
    return data_plane_pb2.CatalogDescriptor(
        protocol="lakekeeper",
        uri=os.environ.get("LAKEKEEPER_URI", "http://lakekeeper:8181/catalog"),
        options={
            "warehouse": os.environ.get("LAKEKEEPER_WAREHOUSE", "rat"),
            "token": os.environ.get("LAKEKEEPER_TOKEN", "dummy"),
        },
    )


def _no_branching(context: grpc.ServicerContext) -> None:
    context.abort(
        grpc.StatusCode.FAILED_PRECONDITION,
        "lakekeeper catalog does not support branch lifecycle",
    )


class CatalogServicer(catalog_pb2_grpc.CatalogServiceServicer):
    """Lakekeeper implementation of catalog/v1."""

    def Describe(self, request: Any, context: grpc.ServicerContext) -> Any:  # noqa: N802
        return catalog_pb2.DescribeResponse(
            name="rat-catalog-lakekeeper",
            version=__version__,
            capabilities=_CAPABILITIES,
            formats=[_FORMAT],
        )

    def GetTable(self, request: Any, context: grpc.ServicerContext) -> Any:  # noqa: N802
        ref = request.ref
        layer = _LAYER_NAMES.get(ref.layer, "")
        return catalog_pb2.GetTableResponse(
            exists=True,
            catalog=_catalog_descriptor(),
            identifier=f"{ref.namespace}.{layer}.{ref.name}",
            format=_FORMAT,
        )

    def CreateBranch(self, request: Any, context: grpc.ServicerContext) -> Any:  # noqa: N802
        _no_branching(context)

    def MergeBranch(self, request: Any, context: grpc.ServicerContext) -> Any:  # noqa: N802
        _no_branching(context)

    def DeleteBranch(self, request: Any, context: grpc.ServicerContext) -> Any:  # noqa: N802
        _no_branching(context)

    def ListNamespaces(self, request: Any, context: grpc.ServicerContext) -> Any:  # noqa: N802
        context.abort(grpc.StatusCode.UNIMPLEMENTED, "lakekeeper discovery lands in a follow-on")

    def ListTables(self, request: Any, context: grpc.ServicerContext) -> Any:  # noqa: N802
        context.abort(grpc.StatusCode.UNIMPLEMENTED, "lakekeeper discovery lands in a follow-on")

    def GetHistory(self, request: Any, context: grpc.ServicerContext) -> Any:  # noqa: N802
        context.abort(grpc.StatusCode.UNIMPLEMENTED, "history lands in a follow-on")


def serve(port: int = 50082) -> None:
    """Start the catalog/v1 gRPC server and block until termination."""
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=GRPC_MAX_WORKERS))
    catalog_pb2_grpc.add_CatalogServiceServicer_to_server(CatalogServicer(), server)
    server.add_insecure_port(f"[::]:{port}")
    _logger.info("rat-catalog-lakekeeper (catalog/v1) serving on :%d", port)
    server.start()
    server.wait_for_termination()
