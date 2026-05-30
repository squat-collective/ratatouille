"""gRPC server for catalog/v1 — the DuckLake catalog axis (ADR-024).

DuckLake's metadata lives in a SQL database (Postgres by default); data lives in
Parquet on object storage. The engine uses DuckDB's `ducklake` extension to
ATTACH the lake and read/write tables — this service just vends the descriptor
the engine needs (URI + data_path). No branch lifecycle (transactional commits
are the ducklake extension's job, not ours). Discovery would walk the postgres
metadata; not wired yet.
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

from rat_catalog_ducklake import __version__

_logger = logging.getLogger("rat_catalog_ducklake.server")

GRPC_MAX_WORKERS = 10

_CAPABILITIES = ["time_travel"]
_FORMAT = "ducklake"
_LAYER_NAMES = {1: "bronze", 2: "silver", 3: "gold"}


def _catalog_descriptor() -> Any:
    """Vend the ducklake CatalogDescriptor (postgres URI + s3 data_path)."""
    bucket = os.environ.get("S3_BUCKET", "rat")
    return data_plane_pb2.CatalogDescriptor(
        protocol="ducklake",
        uri=os.environ.get(
            "DUCKLAKE_URI", "postgres:dbname=ducklake host=postgres user=rat password=rat"
        ),
        options={"data_path": os.environ.get("DUCKLAKE_DATA_PATH", f"s3://{bucket}/ducklake/")},
    )


def _no_branching(context: grpc.ServicerContext) -> None:
    context.abort(
        grpc.StatusCode.FAILED_PRECONDITION,
        "ducklake catalog does not support branch lifecycle",
    )


class CatalogServicer(catalog_pb2_grpc.CatalogServiceServicer):
    """DuckLake implementation of catalog/v1."""

    def Describe(self, request: Any, context: grpc.ServicerContext) -> Any:  # noqa: N802
        return catalog_pb2.DescribeResponse(
            name="rat-catalog-ducklake",
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
        context.abort(grpc.StatusCode.UNIMPLEMENTED, "ducklake discovery lands in a follow-on")

    def ListTables(self, request: Any, context: grpc.ServicerContext) -> Any:  # noqa: N802
        context.abort(grpc.StatusCode.UNIMPLEMENTED, "ducklake discovery lands in a follow-on")

    def GetHistory(self, request: Any, context: grpc.ServicerContext) -> Any:  # noqa: N802
        context.abort(grpc.StatusCode.UNIMPLEMENTED, "history lands in a follow-on")


def serve(port: int = 50082) -> None:
    """Start the catalog/v1 gRPC server and block until termination."""
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=GRPC_MAX_WORKERS))
    catalog_pb2_grpc.add_CatalogServiceServicer_to_server(CatalogServicer(), server)
    server.add_insecure_port(f"[::]:{port}")
    _logger.info("rat-catalog-ducklake (catalog/v1) serving on :%d", port)
    server.start()
    server.wait_for_termination()
