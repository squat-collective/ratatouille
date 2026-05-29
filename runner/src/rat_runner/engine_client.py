"""gRPC client for engine/v1 — the runner's handle on a compute engine service.

Part of the decoupled data architecture (ADR-024): the runner orchestrates and
calls out to a compute engine instead of running DuckDB in-process. This client
wraps the engine/v1 stub; the executor uses it in place of the embedded engine.
"""

from __future__ import annotations

import logging
from typing import Any

import grpc
import pyarrow as pa
from engine.v1 import (  # type: ignore[import-untyped]
    engine_pb2,
    engine_pb2_grpc,
)

logger = logging.getLogger("rat_runner.engine_client")


class EngineClient:
    """Thin client over engine/v1 (Describe / Execute / Query / Preview)."""

    def __init__(self, addr: str) -> None:
        self._addr = addr
        self._channel = grpc.insecure_channel(addr)
        self._stub = engine_pb2_grpc.EngineServiceStub(self._channel)

    def describe(self) -> Any:
        """Return the engine's advertised languages / formats / capabilities / dialects."""
        return self._stub.Describe(engine_pb2.DescribeRequest())

    def execute(self, request: Any) -> Any:
        """Run a transform + write; forward streamed logs; return the terminal ExecuteResult.

        Raises RuntimeError if the engine reports an error or returns no result.
        """
        result = None
        for response in self._stub.Execute(request):
            event = response.WhichOneof("event")
            if event == "log":
                logger.info("[engine] %s", response.log.message)
            elif event == "result":
                result = response.result
        if result is None:
            raise RuntimeError("engine Execute returned no terminal result")
        if result.error:
            raise RuntimeError(f"engine Execute failed: {result.error}")
        return result

    def query(self, request: Any) -> pa.Table:
        """Run a read-only Query; reassemble the streamed Arrow IPC into one table."""
        table: pa.Table | None = None
        for response in self._stub.Query(request):
            if response.arrow_ipc:
                with pa.ipc.open_stream(pa.BufferReader(response.arrow_ipc)) as reader:
                    table = reader.read_all()
        return table if table is not None else pa.table({})

    def close(self) -> None:
        self._channel.close()
