"""gRPC server for catalog/v1 — the Nessie (iceberg-rest) catalog axis (ADR-024).

One plugin = one backend: Nessie's Iceberg REST endpoint for table descriptors,
the Nessie v2 API for ephemeral branch lifecycle (create / merge / delete), and
the Nessie entries API for namespace + table discovery. Lakekeeper and DuckLake
live in their own sibling plugins (rat-catalog-lakekeeper, rat-catalog-ducklake).
"""

from __future__ import annotations

import logging
from concurrent import futures
from typing import Any

import grpc
from catalog.v1 import (  # type: ignore[import-untyped]
    catalog_pb2,
    catalog_pb2_grpc,
)
from common.v1 import data_plane_pb2  # type: ignore[import-untyped]

from rat_catalog_nessie import __version__, discovery, nessie
from rat_catalog_nessie.config import NessieConfig

_logger = logging.getLogger("rat_catalog_nessie.server")

GRPC_MAX_WORKERS = 10

_CAPABILITIES = ["branching", "time_travel", "history"]
_FORMAT = "iceberg"
_LAYER_NAMES = {1: "bronze", 2: "silver", 3: "gold"}
_LAYER_ENUMS = {name: num for num, name in _LAYER_NAMES.items()}


def _catalog_descriptor(branch: str) -> Any:
    """Vend the iceberg-rest CatalogDescriptor the engine uses for native I/O."""
    return data_plane_pb2.CatalogDescriptor(
        protocol="iceberg-rest",
        uri=NessieConfig.from_env().base_url,
        branch=branch or "main",
    )


class CatalogServicer(catalog_pb2_grpc.CatalogServiceServicer):
    """Nessie (iceberg-rest) implementation of catalog/v1."""

    def Describe(self, request: Any, context: grpc.ServicerContext) -> Any:  # noqa: N802
        return catalog_pb2.DescribeResponse(
            name="rat-catalog-nessie",
            version=__version__,
            capabilities=_CAPABILITIES,
            formats=[_FORMAT],
        )

    def GetTable(self, request: Any, context: grpc.ServicerContext) -> Any:  # noqa: N802
        ref = request.ref
        layer = _LAYER_NAMES.get(ref.layer, "")
        return catalog_pb2.GetTableResponse(
            exists=True,
            catalog=_catalog_descriptor(request.branch),
            identifier=f"{ref.namespace}.{layer}.{ref.name}",
            format=_FORMAT,
        )

    def CreateBranch(self, request: Any, context: grpc.ServicerContext) -> Any:  # noqa: N802
        commit_hash = nessie.create_branch(
            NessieConfig.from_env(), request.name, request.from_branch or "main"
        )
        return catalog_pb2.CreateBranchResponse(name=request.name, commit_hash=commit_hash)

    def MergeBranch(self, request: Any, context: grpc.ServicerContext) -> Any:  # noqa: N802
        nessie.merge_branch(NessieConfig.from_env(), request.source, request.target or "main")
        return catalog_pb2.MergeBranchResponse(merged=True, commit_hash="")

    def DeleteBranch(self, request: Any, context: grpc.ServicerContext) -> Any:  # noqa: N802
        nessie.delete_branch(NessieConfig.from_env(), request.name)
        return catalog_pb2.DeleteBranchResponse(deleted=True)

    def ListNamespaces(self, request: Any, context: grpc.ServicerContext) -> Any:  # noqa: N802
        namespaces = discovery.list_namespaces(NessieConfig.from_env(), request.parent or "")
        return catalog_pb2.ListNamespacesResponse(namespaces=namespaces)

    def ListTables(self, request: Any, context: grpc.ServicerContext) -> Any:  # noqa: N802
        layer_filter = _LAYER_NAMES.get(request.layer, "")
        rows = discovery.list_tables(NessieConfig.from_env(), request.namespace, layer_filter)
        return catalog_pb2.ListTablesResponse(
            tables=[
                data_plane_pb2.TableRef(namespace=ns, layer=_LAYER_ENUMS[layer], name=name)
                for ns, layer, name in rows
            ]
        )

    def GetHistory(self, request: Any, context: grpc.ServicerContext) -> Any:  # noqa: N802
        context.abort(grpc.StatusCode.UNIMPLEMENTED, "history lands in a follow-on")


def serve(port: int = 50082) -> None:
    """Start the catalog/v1 gRPC server and block until termination."""
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=GRPC_MAX_WORKERS))
    catalog_pb2_grpc.add_CatalogServiceServicer_to_server(CatalogServicer(), server)
    server.add_insecure_port(f"[::]:{port}")
    _logger.info("rat-catalog-nessie (iceberg-rest, catalog/v1) serving on :%d", port)
    server.start()
    server.wait_for_termination()
