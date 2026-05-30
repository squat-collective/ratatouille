"""Nessie catalog discovery — enumerate namespaces + Iceberg tables (catalog/v1).

Relocated from the query service's NessieCatalog so discovery lives on the catalog
axis (ADR-024): the runner/ratq ask catalog/v1 "what's here" instead of speaking
Nessie's REST API themselves. Uses the Nessie v2 entries API; urllib-only so it
stays import-light (no DuckDB/PyIceberg) and unit-tests without infra.

RAT's naming convention is a 3-element Iceberg identifier: [namespace, layer, name]
where layer is one of bronze/silver/gold (themselves nested Nessie namespaces).
"""

from __future__ import annotations

import json
import urllib.request
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from rat_catalog_nessie.config import NessieConfig

LAYERS = ("bronze", "silver", "gold")


def _entries(config: NessieConfig, *, with_content: bool) -> list[dict]:
    """GET the Nessie v2 entries for main, returning the raw entry dicts."""
    url = f"{config.api_v2_url}/trees/main/entries"
    if with_content:
        url += "?content=true"
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310
        data = json.loads(resp.read().decode())
    return data.get("entries", [])


def list_namespaces(config: NessieConfig, parent: str = "") -> list[str]:
    """List namespace identifiers under ``parent`` ("" = top-level tenants).

    Top-level namespaces are NAMESPACE entries with a single name element
    (e.g. ``default``, ``shop``); with a parent, returns its direct children
    (the second element of 2-element entries under that parent).
    """
    out: list[str] = []
    seen: set[str] = set()
    for entry in _entries(config, with_content=False):
        if entry.get("type") != "NAMESPACE":
            continue
        elements = entry.get("name", {}).get("elements", [])
        if parent:
            if len(elements) == 2 and elements[0] == parent:
                child = elements[1]
            else:
                continue
        elif len(elements) == 1:
            child = elements[0]
        else:
            continue
        if child not in seen:
            seen.add(child)
            out.append(child)
    return out


def list_tables(
    config: NessieConfig, namespace: str, layer_filter: str = ""
) -> list[tuple[str, str, str]]:
    """List ``(namespace, layer, name)`` for Iceberg tables in ``namespace``.

    ``layer_filter`` (bronze/silver/gold, or "" for all) narrows the result.
    """
    out: list[tuple[str, str, str]] = []
    for entry in _entries(config, with_content=True):
        if entry.get("type") != "ICEBERG_TABLE":
            continue
        elements = entry.get("name", {}).get("elements", [])
        if len(elements) < 3:
            continue
        ns, layer, name = elements[0], elements[1], elements[2]
        if ns != namespace or layer not in LAYERS:
            continue
        if layer_filter and layer != layer_filter:
            continue
        out.append((ns, layer, name))
    return out
