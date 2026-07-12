"""tests/test_mcp_resources.py — Phase 2 Step 2: resources 单测。"""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from agent_core.mcp.materialize import _normalize_resource_result, materialize_resource_tools
from agent_core.mcp.manager import McpManager, _LiveServer


# ── _normalize_resource_result ─────────────────────────────────────
def test_normalize_resource_text():
    r = SimpleNamespace(contents=[SimpleNamespace(text="hello", uri="file:///x")])
    assert _normalize_resource_result(r) == "hello"


def test_normalize_resource_blob_placeholder():
    r = SimpleNamespace(contents=[SimpleNamespace(blob="abcd", mimeType="image/png", uri="file:///x")])
    assert _normalize_resource_result(r) == "[blob: image/png, 4 chars base64]"


def test_normalize_resource_multi_joined():
    r = SimpleNamespace(contents=[
        SimpleNamespace(text="a", uri="u1"),
        SimpleNamespace(text="b", uri="u2"),
    ])
    assert _normalize_resource_result(r) == "a\nb"


def test_normalize_resource_empty():
    assert _normalize_resource_result(SimpleNamespace(contents=[])) == "(empty resource)"


# ── materialize_resource_tools ─────────────────────────────────────
def test_materialize_resource_tools_two_globals():
    mgr = MagicMock()
    mgr.list_resources.return_value = "res list"
    mgr.read_resource.return_value = "res content"
    tools = materialize_resource_tools(mgr)
    assert len(tools) == 2
    assert tools[0].name == "list_mcp_resources"
    assert tools[1].name == "read_mcp_resource"
    assert all(t.category == "mcp" for t in tools)


def test_materialize_resource_handlers_call_manager():
    mgr = MagicMock()
    mgr.list_resources.return_value = "L"
    mgr.read_resource.return_value = "R"
    tools = materialize_resource_tools(mgr)
    # list: 无参 / 带 server 过滤
    assert tools[0].handler() == "L"
    assert tools[0].handler(server="fs") == "L"
    mgr.list_resources.assert_called_with("fs")
    # read: 必须 server + uri
    assert tools[1].handler(server="fs", uri="file:///x") == "R"
    mgr.read_resource.assert_called_with("fs", "file:///x")


def test_materialize_resource_handler_pops_cancel_event():
    mgr = MagicMock()
    mgr.list_resources.return_value = "ok"
    tools = materialize_resource_tools(mgr)
    tools[0].handler(_cancel_event="evt")   # 不抛
    mgr.list_resources.assert_called_once_with(None)


# ── manager.list_resources / read_resource ─────────────────────────
def test_manager_list_resources_aggregates_all_servers():
    mgr = McpManager([])
    mgr._servers["a"] = _LiveServer(session=None, tool_defs=[], resources=[
        SimpleNamespace(uri="file:///1", description="d1", name="r1"),
        SimpleNamespace(uri="file:///2", description=None, name="r2"),   # desc None → fallback name
    ])
    mgr._servers["b"] = _LiveServer(session=None, tool_defs=[], resources=[
        SimpleNamespace(uri="file:///3", description="d3", name="r3"),
    ])
    out = mgr.list_resources()
    assert "[a] file:///1 — d1" in out
    assert "[a] file:///2 — r2" in out
    assert "[b] file:///3 — d3" in out


def test_manager_list_resources_filter_by_server():
    mgr = McpManager([])
    mgr._servers["a"] = _LiveServer(session=None, tool_defs=[], resources=[
        SimpleNamespace(uri="file:///1", description="d1", name="r1")])
    mgr._servers["b"] = _LiveServer(session=None, tool_defs=[], resources=[
        SimpleNamespace(uri="file:///2", description="d2", name="r2")])
    out = mgr.list_resources(server="a")
    assert "file:///1" in out
    assert "file:///2" not in out


def test_manager_list_resources_empty():
    assert McpManager([]).list_resources() == "(no resources)"


def test_manager_read_resource_unknown_server_raises():
    mgr = McpManager([])
    with pytest.raises(RuntimeError, match="未连接"):
        mgr.read_resource("ghost", "file:///x")
