"""Engine-backed read path (ADR-024 #12): route ratq queries through engine/v1.

Gated by RAT_ENGINE_MODE. Instead of reading Iceberg directly via iceberg_scan,
ratq resolves each 3-part table ref (ns.layer.name) to a TableDescriptor — its
catalog part vended by catalog/v1 GetTable, its storage part by storage/v1
VendDescriptor — and asks the bound engine to run the SQL. ratq thus becomes
format-agnostic (the engine handles iceberg/ducklake/…); it never touches bytes.

First cut: a single env-configured composition + 3-part refs. Per-namespace
binding resolution and 2-part refs are follow-ons.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

import grpc
import pyarrow as pa
from catalog.v1 import (  # type: ignore[import-untyped]
    catalog_pb2,
    catalog_pb2_grpc,
)
from common.v1 import data_plane_pb2  # type: ignore[import-untyped]
from engine.v1 import (  # type: ignore[import-untyped]
    engine_pb2,
    engine_pb2_grpc,
)
from storage.v1 import (  # type: ignore[import-untyped]
    storage_pb2,
    storage_pb2_grpc,
)

from rat_query.engine import (
    _BLOCKED_FUNCTIONS,
    _BLOCKED_STATEMENTS,
    _MAX_QUERY_LENGTH,
    _quote_ns_table_refs,
    _strip_sql_comments,
)

if TYPE_CHECKING:
    from rat_query.config import CompositionConfig

_LAYER_ENUMS = {"bronze": 1, "silver": 2, "gold": 3}
_LAYER_NAMES = {num: name for name, num in _LAYER_ENUMS.items()}

# 3-part ref ns.layer.name, tolerant of double-quoted parts (default.bronze.orders
# or "default"."bronze"."orders"). The middle part must be a medallion layer.
_REF_RE = re.compile(r'"?(\w+)"?\.\s*"?(bronze|silver|gold)"?\.\s*"?(\w+)"?', re.IGNORECASE)


def extract_refs(sql: str) -> list[tuple[str, str, str]]:
    """Distinct (namespace, layer, name) triples referenced in the SQL (3-part refs)."""
    seen: set[tuple[str, str, str]] = set()
    out: list[tuple[str, str, str]] = []
    for ns, layer, name in _REF_RE.findall(sql):
        key = (ns, layer.lower(), name)
        if key not in seen:
            seen.add(key)
            out.append(key)
    return out


def list_tables(
    cfg: CompositionConfig, namespace: str = "", layer_filter: str = ""
) -> list[tuple[str, str, str]]:
    """List ``(namespace, layer, name)`` via catalog/v1 discovery.

    With ``namespace=""`` it first enumerates namespaces (ListNamespaces), then
    lists tables in each — so ratq's listing comes from the catalog axis instead
    of ratq speaking Nessie's REST API itself.
    """
    layer_enum = _LAYER_ENUMS.get(layer_filter, 0)
    out: list[tuple[str, str, str]] = []
    with grpc.insecure_channel(cfg.catalog_addr) as cat_ch:
        catalog = catalog_pb2_grpc.CatalogServiceStub(cat_ch)
        if namespace:
            namespaces = [namespace]
        else:
            resp = catalog.ListNamespaces(catalog_pb2.ListNamespacesRequest(parent=""))
            namespaces = list(resp.namespaces)
        for ns in namespaces:
            resp = catalog.ListTables(catalog_pb2.ListTablesRequest(namespace=ns, layer=layer_enum))
            for ref in resp.tables:
                out.append((ref.namespace, _LAYER_NAMES.get(ref.layer, ""), ref.name))
    return out


def _check_security(sql: str) -> None:
    """Reuse ratq's read-only guards before handing SQL to the engine."""
    if len(sql) > _MAX_QUERY_LENGTH:
        raise ValueError(f"Query too long ({len(sql)} chars, max {_MAX_QUERY_LENGTH})")
    stripped = _strip_sql_comments(sql)
    if _BLOCKED_STATEMENTS.match(stripped):
        raise ValueError("Only SELECT queries are allowed")
    if _BLOCKED_FUNCTIONS.search(stripped):
        raise ValueError("Direct file/URL access functions are not allowed in queries")


def _ipc_to_table(blob: bytes) -> pa.Table:
    with pa.ipc.open_stream(pa.BufferReader(blob)) as reader:
        return reader.read_all()


def run_query(sql: str, limit: int, cfg: CompositionConfig, bucket: str) -> pa.Table:
    """Resolve refs → descriptors, run via engine.Query, return the Arrow result."""
    _check_security(sql)

    with grpc.insecure_channel(cfg.storage_addr) as storage_ch:
        storage = (
            storage_pb2_grpc.StorageServiceStub(storage_ch)
            .VendDescriptor(storage_pb2.VendDescriptorRequest(scheme="s3", location=bucket))
            .descriptor
        )

    inputs: list[data_plane_pb2.TableDescriptor] = []
    with grpc.insecure_channel(cfg.catalog_addr) as cat_ch:
        catalog = catalog_pb2_grpc.CatalogServiceStub(cat_ch)
        for ns, layer, name in extract_refs(sql):
            ref = data_plane_pb2.TableRef(namespace=ns, layer=_LAYER_ENUMS[layer], name=name)
            info = catalog.GetTable(catalog_pb2.GetTableRequest(ref=ref, branch="main"))
            inputs.append(
                data_plane_pb2.TableDescriptor(
                    ref=ref,
                    format=info.format,
                    identifier=info.identifier,
                    catalog=info.catalog,
                    storage=storage,
                )
            )

    wrapped = _quote_ns_table_refs(sql.rstrip().rstrip(";"))
    request = engine_pb2.QueryRequest(language="sql", source=wrapped, inputs=inputs, limit=limit)
    table: pa.Table | None = None
    with grpc.insecure_channel(cfg.engine_addr) as eng_ch:
        engine = engine_pb2_grpc.EngineServiceStub(eng_ch)
        for resp in engine.Query(request):
            if resp.arrow_ipc:
                table = _ipc_to_table(resp.arrow_ipc)
    return table if table is not None else pa.table({})
