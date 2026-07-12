"""tests/test_mcp_manager.py — McpManager 单测（Step 4，R1 核心）。

用 monkeypatch 替换 connect_server 为 fake async cm，避免依赖真 MCP server。
真连接冒烟测留 Step 7 E2E。
"""

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from agent_core.mcp.config import McpServerConfig
from agent_core.mcp.manager import McpManager


# ── fake connect_server 工厂（供 monkeypatch）────────────────────────
def _make_fake_connect(*, tools=None, fail=False):
    @asynccontextmanager
    async def _fake(name, cfg, **kw):
        if fail:
            raise RuntimeError("connect failed")
        session = MagicMock()
        session.call_tool = AsyncMock()
        yield SimpleNamespace(
            name=name, session=session,
            capabilities=SimpleNamespace(tools=SimpleNamespace()) if tools else None,
            tools=tools or [],
        )
    return _fake


# ── _run 基本取值 ───────────────────────────────────────────────────
def test_run_returns_coroutine_result():
    mgr = McpManager([])
    async def coro():
        return 42
    assert mgr._run(coro(), timeout=5) == 42
    mgr.dispose()


def test_run_timeout_raises():
    mgr = McpManager([])
    async def slow():
        await asyncio.sleep(10)
        return 1
    with pytest.raises(TimeoutError):
        mgr._run(slow(), timeout=0.5)
    mgr.dispose()


def test_run_concurrent_submissions():
    mgr = McpManager([])
    async def coro(i):
        await asyncio.sleep(0.01)
        return i
    for i in range(5):
        assert mgr._run(coro(i), timeout=5) == i
    mgr.dispose()


# ── dispose ─────────────────────────────────────────────────────────
def test_dispose_idempotent():
    mgr = McpManager([])
    mgr.dispose()
    mgr.dispose()   # 不抛


def test_dispose_stops_loop_thread():
    mgr = McpManager([])
    mgr._ensure_loop()
    assert mgr._loop_thread.is_alive()
    mgr.dispose()
    assert not mgr._loop_thread.is_alive()


def test_call_tool_after_dispose_raises():
    mgr = McpManager([])
    mgr.dispose()
    with pytest.raises(RuntimeError):
        mgr.call_tool("s", "t", {})


# ── connect_all 故障隔离 + deny strip + disabled ────────────────────
def test_connect_all_isolation_failed_server_skipped(monkeypatch):
    monkeypatch.setattr("agent_core.mcp.manager.connect_server", _make_fake_connect(fail=True))
    cfg = McpServerConfig(name="bad", kind="stdio", command="x")
    mgr = McpManager([cfg])
    results = mgr.connect_all()
    assert results == {"bad": []}   # 失败 server 返空 list，不抛
    mgr.dispose()


def test_connect_all_success(monkeypatch):
    tools = [SimpleNamespace(name="t1", description="d", inputSchema={})]
    monkeypatch.setattr("agent_core.mcp.manager.connect_server", _make_fake_connect(tools=tools))
    cfg = McpServerConfig(name="s", kind="stdio", command="x")
    mgr = McpManager([cfg])
    results = mgr.connect_all()
    assert "s" in results
    assert len(results["s"]) == 1
    assert results["s"][0].name == "mcp__s__t1"
    assert results["s"][0].category == "mcp"
    mgr.dispose()


def test_connect_all_mixed_success_and_failure(monkeypatch):
    """一个成功一个失败：失败不阻断成功。"""
    good_tools = [SimpleNamespace(name="g", description="d", inputSchema={})]

    @asynccontextmanager
    async def _mixed(name, cfg, **kw):
        if name == "bad":
            raise RuntimeError("nope")
        session = MagicMock()
        session.call_tool = AsyncMock()
        yield SimpleNamespace(name=name, session=session,
                              capabilities=SimpleNamespace(tools=SimpleNamespace()),
                              tools=good_tools)

    monkeypatch.setattr("agent_core.mcp.manager.connect_server", _mixed)
    mgr = McpManager([
        McpServerConfig(name="good", kind="stdio", command="x"),
        McpServerConfig(name="bad", kind="stdio", command="y"),
    ])
    results = mgr.connect_all()
    assert len(results["good"]) == 1
    assert results["bad"] == []
    mgr.dispose()


def test_connect_all_deny_strip_does_not_connect(monkeypatch):
    called = []
    monkeypatch.setattr("agent_core.mcp.manager.connect_server",
                        _make_fake_connect(tools=[], ))  # 不会真连

    @asynccontextmanager
    async def _spy(name, cfg, **kw):
        called.append(name)
        yield SimpleNamespace(name=name, session=MagicMock(), capabilities=None, tools=[])

    monkeypatch.setattr("agent_core.mcp.manager.connect_server", _spy)
    cfg = McpServerConfig(name="fs", kind="stdio", command="x")
    mgr = McpManager([cfg], deny_rules=["mcp__fs"])
    results = mgr.connect_all()
    assert called == []            # deny 命中 → 根本没连
    assert results == {}
    mgr.dispose()


def test_connect_all_disabled_skipped(monkeypatch):
    called = []

    @asynccontextmanager
    async def _spy(name, cfg, **kw):
        called.append(name)
        yield SimpleNamespace(name=name, session=MagicMock(), capabilities=None, tools=[])

    monkeypatch.setattr("agent_core.mcp.manager.connect_server", _spy)
    cfg = McpServerConfig(name="s", kind="stdio", command="x", enabled=False)
    mgr = McpManager([cfg])
    mgr.connect_all()
    assert called == []
    mgr.dispose()


# ── call_tool 端到端（mock session.call_tool）────────────────────────
def test_call_tool_returns_normalized_text(monkeypatch):
    tools = [SimpleNamespace(name="t1", description="d", inputSchema={})]
    monkeypatch.setattr("agent_core.mcp.manager.connect_server", _make_fake_connect(tools=tools))
    cfg = McpServerConfig(name="s", kind="stdio", command="x")
    mgr = McpManager([cfg])
    mgr.connect_all()

    # 让 session.call_tool 返回一个成功 result
    mgr._servers["s"].session.call_tool = AsyncMock(return_value=SimpleNamespace(
        isError=False,
        content=[SimpleNamespace(type="text", text="hello")],
        structuredContent=None,
    ))
    out = mgr.call_tool("s", "t1", {"a": 1})
    assert out == "hello"
    mgr.dispose()


def test_call_tool_unknown_server_raises(monkeypatch):
    monkeypatch.setattr("agent_core.mcp.manager.connect_server", _make_fake_connect(tools=[]))
    mgr = McpManager([])
    with pytest.raises(RuntimeError, match="未连接"):
        mgr.call_tool("ghost", "t", {})
    mgr.dispose()
