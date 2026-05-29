"""Unit tests for Nessie catalog discovery (pure; urllib mocked)."""

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from rat_catalog import discovery


def _nessie() -> SimpleNamespace:
    return SimpleNamespace(api_v2_url="http://nessie:19120/api/v2")


def _resp(entries: list[dict]) -> MagicMock:
    resp = MagicMock()
    resp.read.return_value = json.dumps({"entries": entries}).encode()
    resp.__enter__ = lambda s: s
    resp.__exit__ = MagicMock(return_value=False)
    return resp


_ENTRIES = [
    {"type": "NAMESPACE", "name": {"elements": ["default"]}},
    {"type": "NAMESPACE", "name": {"elements": ["shop"]}},
    {"type": "NAMESPACE", "name": {"elements": ["default", "bronze"]}},
    {"type": "ICEBERG_TABLE", "name": {"elements": ["default", "bronze", "orders"]}},
    {"type": "ICEBERG_TABLE", "name": {"elements": ["default", "silver", "orders_doubled"]}},
    {"type": "ICEBERG_TABLE", "name": {"elements": ["shop", "bronze", "items"]}},
    {"type": "ICEBERG_TABLE", "name": {"elements": ["default", "weird", "skipme"]}},  # bad layer
]


@patch("rat_catalog.discovery.urllib.request.urlopen")
def test_list_namespaces_top_level(mock_urlopen: MagicMock):
    mock_urlopen.return_value = _resp(_ENTRIES)
    assert discovery.list_namespaces(_nessie()) == ["default", "shop"]


@patch("rat_catalog.discovery.urllib.request.urlopen")
def test_list_namespaces_with_parent_returns_children(mock_urlopen: MagicMock):
    mock_urlopen.return_value = _resp(_ENTRIES)
    assert discovery.list_namespaces(_nessie(), parent="default") == ["bronze"]


@patch("rat_catalog.discovery.urllib.request.urlopen")
def test_list_tables_for_namespace(mock_urlopen: MagicMock):
    mock_urlopen.return_value = _resp(_ENTRIES)
    assert discovery.list_tables(_nessie(), "default") == [
        ("default", "bronze", "orders"),
        ("default", "silver", "orders_doubled"),
    ]


@patch("rat_catalog.discovery.urllib.request.urlopen")
def test_list_tables_filters_by_namespace(mock_urlopen: MagicMock):
    mock_urlopen.return_value = _resp(_ENTRIES)
    assert discovery.list_tables(_nessie(), "shop") == [("shop", "bronze", "items")]


@patch("rat_catalog.discovery.urllib.request.urlopen")
def test_list_tables_filters_by_layer(mock_urlopen: MagicMock):
    mock_urlopen.return_value = _resp(_ENTRIES)
    assert discovery.list_tables(_nessie(), "default", layer_filter="silver") == [
        ("default", "silver", "orders_doubled")
    ]
