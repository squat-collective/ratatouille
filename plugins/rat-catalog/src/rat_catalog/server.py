"""gRPC server for catalog/v1 — the protocol-aware catalog axis (ADR-024).

For the Nessie protocol it provides the ephemeral-branch lifecycle (relocated from
the runner's nessie.py) and vends an iceberg-rest CatalogDescriptor. Lakekeeper and
DuckLake have no branch lifecycle (capability-gated in Describe); their GetTable
vends the right descriptor. Discovery RPCs (list namespaces/tables, history) are
stubbed for now — the runner doesn't need them for execution yet.
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

from rat_catalog import __version__, nessie
from rat_catalog.config import NessieConfig

_logger = logging.getLogger("rat_catalog.server")

GRPC_MAX_WORKERS = 10

# Which catalog this instance fronts (set per data-plane composition).
_PROTOCOL = os.environ.get("CATALOG_PROTOCOL", "iceberg-rest")
_CAPABILITIES = {
    "iceberg-rest": ["branching", "time_travel", "history"],  # Nessie
    "lakekeeper": ["time_travel"],
    "ducklake": ["time_travel"],
}
_FORMATS = {"iceberg-rest": "iceberg", "lakekeeper": "iceberg", "ducklake": "ducklake"}
_LAYER_NAMES = {1: "bronze", 2: "silver", 3: "gold"}


def _catalog_descriptor(branch: str) -> Any:
    """Vend the CatalogDescriptor the engine uses for native I/O (per protocol)."""
    if _PROTOCOL == "lakekeeper":
        return data_plane_pb2.CatalogDescriptor(
            protocol="lakekeeper",
            uri=os.environ.get("LAKEKEEPER_URI", "http://lakekeeper:8181/catalog"),
            options={
                "warehouse": os.environ.get("LAKEKEEPER_WAREHOUSE", "rat"),
                "token": os.environ.get("LAKEKEEPER_TOKEN", "dummy"),
            },
        )
    if _PROTOCOL == "ducklake":
        bucket = os.environ.get("S3_BUCKET", "rat")
        return data_plane_pb2.CatalogDescriptor(
            protocol="ducklake",
            uri=os.environ.get(
                "DUCKLAKE_URI", "postgres:dbname=ducklake host=postgres user=rat password=rat"
            ),
            options={"data_path": os.environ.get("DUCKLAKE_DATA_PATH", f"s3://{bucket}/ducklake/")},
        )
    return data_plane_pb2.CatalogDescriptor(
        protocol="iceberg-rest", uri=NessieConfig.from_env().base_url, branch=branch or "main"
    )


def _require_branching(context: grpc.ServicerContext) -> None:
    if _PROTOCOL != "iceberg-rest":
        context.abort(
            grpc.StatusCode.FAILED_PRECONDITION,
            f"catalog '{_PROTOCOL}' does not support branch lifecycle",
        )


class CatalogServicer(catalog_pb2_grpc.CatalogServiceServicer):
    """Protocol-aware catalog/v1 implementation."""

    def Describe(self, request: Any, context: grpc.ServicerContext) -> Any:  # noqa: N802
        return catalog_pb2.DescribeResponse(
            name=f"rat-catalog-{_PROTOCOL}",
            version=__version__,
            capabilities=_CAPABILITIES.get(_PROTOCOL, []),
            formats=[_FORMATS.get(_PROTOCOL, "iceberg")],
        )

    def GetTable(self, request: Any, context: grpc.ServicerContext) -> Any:  # noqa: N802
        ref = request.ref
        layer = _LAYER_NAMES.get(ref.layer, "")
        return catalog_pb2.GetTableResponse(
            exists=True,
            catalog=_catalog_descriptor(request.branch),
            identifier=f"{ref.namespace}.{layer}.{ref.name}",
            format=_FORMATS.get(_PROTOCOL, "iceberg"),
        )

    def CreateBranch(self, request: Any, context: grpc.ServicerContext) -> Any:  # noqa: N802
        _require_branching(context)
        commit_hash = nessie.create_branch(
            NessieConfig.from_env(), request.name, request.from_branch or "main"
        )
        return catalog_pb2.CreateBranchResponse(name=request.name, commit_hash=commit_hash)

    def MergeBranch(self, request: Any, context: grpc.ServicerContext) -> Any:  # noqa: N802
        _require_branching(context)
        nessie.merge_branch(NessieConfig.from_env(), request.source, request.target or "main")
        return catalog_pb2.MergeBranchResponse(merged=True, commit_hash="")

    def DeleteBranch(self, request: Any, context: grpc.ServicerContext) -> Any:  # noqa: N802
        _require_branching(context)
        nessie.delete_branch(NessieConfig.from_env(), request.name)
        return catalog_pb2.DeleteBranchResponse(deleted=True)

    def ListNamespaces(self, request: Any, context: grpc.ServicerContext) -> Any:  # noqa: N802
        context.abort(grpc.StatusCode.UNIMPLEMENTED, "discovery lands in a follow-on")

    def ListTables(self, request: Any, context: grpc.ServicerContext) -> Any:  # noqa: N802
        context.abort(grpc.StatusCode.UNIMPLEMENTED, "discovery lands in a follow-on")

    def GetHistory(self, request: Any, context: grpc.ServicerContext) -> Any:  # noqa: N802
        context.abort(grpc.StatusCode.UNIMPLEMENTED, "history lands in a follow-on")


def serve(port: int = 50082) -> None:
    """Start the catalog/v1 gRPC server and block until termination."""
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=GRPC_MAX_WORKERS))
    catalog_pb2_grpc.add_CatalogServiceServicer_to_server(CatalogServicer(), server)
    server.add_insecure_port(f"[::]:{port}")
    _logger.info("rat-catalog (%s, catalog/v1) serving on :%d", _PROTOCOL, port)
    server.start()
    server.wait_for_termination()
