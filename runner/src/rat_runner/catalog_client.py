"""gRPC client for catalog/v1 — the runner's control-plane handle on a catalog.

Part of the decoupled data architecture (ADR-024). The runner uses this for
discovery + branch lifecycle (the executor's phase-0 create / phase-5 merge|delete).
Capability-gated RPCs (branching, history) must only be called when Describe
reports support; the caller checks first.
"""

from __future__ import annotations

from typing import Any

import grpc
from catalog.v1 import (  # type: ignore[import-untyped]
    catalog_pb2,
    catalog_pb2_grpc,
)


class CatalogClient:
    """Thin client over catalog/v1."""

    def __init__(self, addr: str) -> None:
        self._addr = addr
        self._channel = grpc.insecure_channel(addr)
        self._stub = catalog_pb2_grpc.CatalogServiceStub(self._channel)

    def describe(self) -> Any:
        """Return the catalog's capabilities (open-set strings) + managed formats."""
        return self._stub.Describe(catalog_pb2.DescribeRequest())

    def get_table(self, ref: Any, branch: str = "") -> Any:
        """Discovery: schema + CatalogDescriptor + native identifier for a table ref."""
        return self._stub.GetTable(catalog_pb2.GetTableRequest(ref=ref, branch=branch))

    def list_tables(self, namespace: str, layer: int = 0) -> Any:
        return self._stub.ListTables(
            catalog_pb2.ListTablesRequest(namespace=namespace, layer=layer)
        )

    def create_branch(self, name: str, from_branch: str = "main") -> Any:
        """Phase-0: create the ephemeral run branch. Requires capability 'branching'."""
        return self._stub.CreateBranch(
            catalog_pb2.CreateBranchRequest(name=name, from_branch=from_branch)
        )

    def merge_branch(self, source: str, target: str = "main") -> Any:
        """Phase-5: merge the run branch into the target on success."""
        return self._stub.MergeBranch(catalog_pb2.MergeBranchRequest(source=source, target=target))

    def delete_branch(self, name: str) -> Any:
        """Phase-5: drop the run branch (on failure, or after a successful merge)."""
        return self._stub.DeleteBranch(catalog_pb2.DeleteBranchRequest(name=name))

    def get_history(self, ref: Any, limit: int = 0) -> Any:
        """Commit history for a table. Requires capability 'history'."""
        return self._stub.GetHistory(catalog_pb2.GetHistoryRequest(ref=ref, limit=limit))

    def close(self) -> None:
        self._channel.close()
