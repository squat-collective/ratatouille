"""gRPC server for storage/v1 — the S3 object-storage axis (ADR-024).

Owns the storage credentials and vends StorageDescriptors the engine uses for
native I/O; also performs the control-plane object ops the orchestrator needs
(list, move — e.g. landing-zone archival). The runner asks this service to vend a
descriptor instead of holding S3 credentials itself.
"""

from __future__ import annotations

import logging
import os
from concurrent import futures
from typing import Any

import boto3
import grpc
from common.v1 import (  # type: ignore[import-untyped]
    common_pb2,
    data_plane_pb2,
)
from storage.v1 import (  # type: ignore[import-untyped]
    storage_pb2,
    storage_pb2_grpc,
)

from rat_storage_s3 import __version__

_logger = logging.getLogger("rat_storage_s3.server")

GRPC_MAX_WORKERS = 10


def _s3_credentials() -> Any:
    """Build S3Credentials from this service's own environment."""
    return common_pb2.S3Credentials(
        endpoint=os.environ.get("S3_ENDPOINT", "minio:9000"),
        access_key_id=os.environ.get("S3_ACCESS_KEY", ""),
        secret_access_key=os.environ.get("S3_SECRET_KEY", ""),
        region=os.environ.get("S3_REGION", "us-east-1"),
        bucket=os.environ.get("S3_BUCKET", "rat"),
        use_ssl=os.environ.get("S3_USE_SSL", "false").lower() == "true",
        session_token=os.environ.get("S3_SESSION_TOKEN", ""),
    )


def _boto_client(creds: Any) -> Any:
    scheme = "https" if creds.use_ssl else "http"
    return boto3.client(
        "s3",
        endpoint_url=f"{scheme}://{creds.endpoint}",
        aws_access_key_id=creds.access_key_id,
        aws_secret_access_key=creds.secret_access_key,
        aws_session_token=creds.session_token or None,
        region_name=creds.region or "us-east-1",
    )


class StorageServicer(storage_pb2_grpc.StorageServiceServicer):
    """S3/MinIO implementation of storage/v1."""

    def Describe(self, request: Any, context: grpc.ServicerContext) -> Any:  # noqa: N802
        return storage_pb2.DescribeResponse(
            name="rat-storage-s3", version=__version__, schemes=["s3"]
        )

    def VendDescriptor(self, request: Any, context: grpc.ServicerContext) -> Any:  # noqa: N802
        creds = _s3_credentials()
        if request.location:
            creds.bucket = request.location
        return storage_pb2.VendDescriptorResponse(
            descriptor=data_plane_pb2.StorageDescriptor(scheme="s3", s3=creds)
        )

    def ListObjects(self, request: Any, context: grpc.ServicerContext) -> Any:  # noqa: N802
        client = _boto_client(request.descriptor.s3)
        keys: list[str] = []
        paginator = client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=request.descriptor.s3.bucket, Prefix=request.prefix):
            keys.extend(obj["Key"] for obj in page.get("Contents", []))
        return storage_pb2.ListObjectsResponse(keys=keys)

    def MoveObjects(self, request: Any, context: grpc.ServicerContext) -> Any:  # noqa: N802
        client = _boto_client(request.descriptor.s3)
        bucket = request.descriptor.s3.bucket
        moved = 0
        for key in request.source_keys:
            dest = f"{request.dest_prefix.rstrip('/')}/{key.rsplit('/', 1)[-1]}"
            client.copy_object(Bucket=bucket, CopySource={"Bucket": bucket, "Key": key}, Key=dest)
            client.delete_object(Bucket=bucket, Key=key)
            moved += 1
        return storage_pb2.MoveObjectsResponse(moved=moved)


def serve(port: int = 50083) -> None:
    """Start the storage/v1 gRPC server and block until termination."""
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=GRPC_MAX_WORKERS))
    storage_pb2_grpc.add_StorageServiceServicer_to_server(StorageServicer(), server)
    server.add_insecure_port(f"[::]:{port}")
    _logger.info("rat-storage-s3 (storage/v1) serving on :%d", port)
    server.start()
    server.wait_for_termination()
