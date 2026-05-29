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
    from rat_query.bindings import BindingConfig

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


def _list_tables_in(
    catalog_addr: str, namespace: str, layer_enum: int
) -> list[tuple[str, str, str]]:
    """ListTables on one catalog; returns [] if that catalog can't do discovery."""
    try:
        with grpc.insecure_channel(catalog_addr) as ch:
            resp = catalog_pb2_grpc.CatalogServiceStub(ch).ListTables(
                catalog_pb2.ListTablesRequest(namespace=namespace, layer=layer_enum)
            )
        return [(r.namespace, _LAYER_NAMES.get(r.layer, ""), r.name) for r in resp.tables]
    except grpc.RpcError as e:
        if e.code() == grpc.StatusCode.UNIMPLEMENTED:  # e.g. ducklake/lakekeeper discovery
            return []
        raise


def list_tables(
    binding: BindingConfig, namespace: str = "", layer_filter: str = ""
) -> list[tuple[str, str, str]]:
    """List ``(namespace, layer, name)`` via catalog/v1, resolving each namespace's plane.

    With ``namespace=""`` it enumerates candidate namespaces (the default plane's
    ListNamespaces plus any explicitly bound) and lists tables on each namespace's
    own catalog — so listing spans compositions. Catalogs whose discovery is
    unimplemented (ducklake/lakekeeper) are skipped rather than failing the call.
    """
    layer_enum = _LAYER_ENUMS.get(layer_filter, 0)
    if namespace:
        plane = binding.resolve(namespace)
        return _list_tables_in(plane.catalog_addr, namespace, layer_enum)

    default = binding.resolve("")
    candidates: list[str] = list(binding.by_namespace.keys())
    try:
        with grpc.insecure_channel(default.catalog_addr) as ch:
            resp = catalog_pb2_grpc.CatalogServiceStub(ch).ListNamespaces(
                catalog_pb2.ListNamespacesRequest(parent="")
            )
        candidates += list(resp.namespaces)
    except grpc.RpcError as e:
        if e.code() != grpc.StatusCode.UNIMPLEMENTED:
            raise

    out: list[tuple[str, str, str]] = []
    seen: set[str] = set()
    for ns in candidates:
        if ns in seen:
            continue
        seen.add(ns)
        plane = binding.resolve(ns)
        out.extend(_list_tables_in(plane.catalog_addr, ns, layer_enum))
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


def _vend_storage(cache: dict[str, object], addr: str, bucket: str) -> object:
    """Vend (and cache) a StorageDescriptor from one storage/v1 service."""
    if addr not in cache:
        with grpc.insecure_channel(addr) as ch:
            cache[addr] = (
                storage_pb2_grpc.StorageServiceStub(ch)
                .VendDescriptor(storage_pb2.VendDescriptorRequest(scheme="s3", location=bucket))
                .descriptor
            )
    return cache[addr]


def run_query(sql: str, limit: int, binding: BindingConfig, bucket: str) -> pa.Table:
    """Resolve each ref's plane → descriptor, run via that composition's engine.

    Each input's catalog + storage come from the plane its namespace binds to, so
    a query can span multiple iceberg catalogs (e.g. Nessie + Lakekeeper). The
    engine is taken from the first ref's plane (one engine per query — all planes
    share an engine today). Reading non-iceberg formats via engine.Query needs
    format-aware input registration in the engine (a follow-on).
    """
    _check_security(sql)
    refs = extract_refs(sql)
    primary = binding.resolve(*refs[0]) if refs else binding.resolve("")

    storage_cache: dict[str, object] = {}
    inputs: list[data_plane_pb2.TableDescriptor] = []
    for ns, layer, name in refs:
        plane = binding.resolve(ns, layer, name)
        storage = _vend_storage(storage_cache, plane.storage_addr, bucket)
        ref = data_plane_pb2.TableRef(namespace=ns, layer=_LAYER_ENUMS[layer], name=name)
        with grpc.insecure_channel(plane.catalog_addr) as cat_ch:
            info = catalog_pb2_grpc.CatalogServiceStub(cat_ch).GetTable(
                catalog_pb2.GetTableRequest(ref=ref, branch="main")
            )
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
    with grpc.insecure_channel(primary.engine_addr) as eng_ch:
        engine = engine_pb2_grpc.EngineServiceStub(eng_ch)
        for resp in engine.Query(request):
            if resp.arrow_ipc:
                table = _ipc_to_table(resp.arrow_ipc)
    return table if table is not None else pa.table({})
