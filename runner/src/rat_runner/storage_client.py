"""gRPC client for storage/v1 — the runner's handle on an object-storage axis.

Part of the decoupled data architecture (ADR-024). Vends StorageDescriptors (for
inclusion in TableDescriptors handed to the engine) and performs the control-plane
object ops the orchestrator needs (e.g. landing-zone archival in the post-run step).
"""

from __future__ import annotations

from typing import Any

import grpc
from storage.v1 import (  # type: ignore[import-untyped]
    storage_pb2,
    storage_pb2_grpc,
)


class StorageClient:
    """Thin client over storage/v1."""

    def __init__(self, addr: str) -> None:
        self._addr = addr
        self._channel = grpc.insecure_channel(addr)
        self._stub = storage_pb2_grpc.StorageServiceStub(self._channel)

    def describe(self) -> Any:
        """Return the storage schemes this service serves."""
        return self._stub.Describe(storage_pb2.DescribeRequest())

    def vend_descriptor(
        self, scheme: str = "s3", location: str = "", options: dict[str, str] | None = None
    ) -> Any:
        """Return a ready-to-use StorageDescriptor for inclusion in a TableDescriptor."""
        response = self._stub.VendDescriptor(
            storage_pb2.VendDescriptorRequest(
                scheme=scheme, location=location, options=options or {}
            )
        )
        return response.descriptor

    def list_objects(self, descriptor: Any, prefix: str) -> Any:
        return self._stub.ListObjects(
            storage_pb2.ListObjectsRequest(descriptor=descriptor, prefix=prefix)
        )

    def move_objects(self, descriptor: Any, source_keys: list[str], dest_prefix: str) -> Any:
        """Archive/move objects (e.g. processed landing-zone files)."""
        return self._stub.MoveObjects(
            storage_pb2.MoveObjectsRequest(
                descriptor=descriptor, source_keys=source_keys, dest_prefix=dest_prefix
            )
        )

    def close(self) -> None:
        self._channel.close()
