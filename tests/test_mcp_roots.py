"""tests/test_mcp_roots.py — Phase 2 Step 1: roots + callback 基础设施单测。"""

import asyncio
from types import SimpleNamespace

from agent_core.mcp.config import parse_mcp_roots
from agent_core.mcp.manager import McpManager


# ── parse_mcp_roots ─────────────────────────────────────────────────
def test_parse_mcp_roots_dict_form():
    r = parse_mcp_roots({"mcp": {"roots": [{"uri": "file:///x", "name": "x"}]}})
    assert r == [{"uri": "file:///x", "name": "x"}]


def test_parse_mcp_roots_string_form_normalizes_to_file_uri():
    r = parse_mcp_roots({"mcp": {"roots": ["/y", "file:///z"]}})
    assert r[0]["uri"] == "file:///y"     # /y → file:///y
    assert r[1]["uri"] == "file:///z"


def test_parse_mcp_roots_default_cwd():
    r = parse_mcp_roots({})
    assert len(r) == 1
    assert r[0]["name"] == "cwd"
    assert r[0]["uri"].startswith("file://")


def test_parse_mcp_roots_empty_list_falls_back_to_cwd():
    r = parse_mcp_roots({"mcp": {"roots": []}})
    assert len(r) == 1 and r[0]["name"] == "cwd"


def test_parse_mcp_roots_non_dict_safe():
    assert len(parse_mcp_roots(None)) == 1      # 默认 cwd
    assert len(parse_mcp_roots({"mcp": "x"})) == 1


# ── _make_roots_callback（声明 roots capability 的回调）─────────────
def test_roots_callback_returns_configured_roots():
    mgr = McpManager([], roots=[
        {"uri": "file:///tmp", "name": "tmp"},
        {"uri": "file:///home", "name": "home"},
    ])
    cb = mgr._make_roots_callback()
    result = asyncio.run(cb(context=None))
    assert len(result.roots) == 2
    uris = [str(r.uri) for r in result.roots]
    assert any("tmp" in u for u in uris)
    assert any("home" in u for u in uris)


def test_roots_callback_empty_when_manager_has_no_roots():
    """manager 不传 roots 时 callback 返空（默认 cwd 由 config.load_mcp_roots 保证，
    组合根负责传入；manager 只透传 _roots，不自行兜底 cwd 以避免与 config 耦合）。"""
    mgr = McpManager([])   # 无 roots
    cb = mgr._make_roots_callback()
    result = asyncio.run(cb(context=None))
    assert result.roots == []   # 不 crash；capability 仍声明（callback 非空）


def test_roots_callback_passes_load_mcp_roots_default():
    """组合根传默认 roots（cwd）时 callback 透传 cwd。

    用 parse_mcp_roots({}) 模拟 load_mcp_roots() 的默认返回，不调真的
    load_mcp_roots（它读全局 settings.json，用户配了 roots 会让此测试 flaky）。"""
    from agent_core.mcp.config import parse_mcp_roots
    default_roots = parse_mcp_roots({})   # 默认 cwd（不读 settings）
    mgr = McpManager([], roots=default_roots)
    cb = mgr._make_roots_callback()
    result = asyncio.run(cb(context=None))
    assert len(result.roots) == 1
    assert result.roots[0].name == "cwd"


# ─_make_message_handler（Step 1 先 log，不抛即可）──────────────────
def test_message_handler_does_not_raise_on_notification():
    mgr = McpManager([])
    handler = mgr._make_message_handler("srv")
    # 任意输入都不抛（ServerNotification / Exception / 其它）
    asyncio.run(handler(SimpleNamespace(root=SimpleNamespace())))
    asyncio.run(handler(Exception("boom")))
    asyncio.run(handler("random"))
