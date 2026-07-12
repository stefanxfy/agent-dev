"""E2E: McpManager 连本地 stdio echo MCP server（FastMCP 夹具）。

验证整条链: config → manager.connect_all（真 stdio spawn + initialize + 协商 + list）
→ materialize → call_tool → registry.execute（模拟 tool_chain）→ dispose。
不依赖 npm/npx，纯 Python（mcp.server.fastmcp）。
"""

import os
import sys

import pytest

from agent_core.mcp.config import McpServerConfig
from agent_core.mcp.manager import McpManager

_FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "echo_mcp_server.py")
# 用当前 venv 的 python 拉起 echo server 子进程
_CFG = [McpServerConfig(name="echo", kind="stdio", command=sys.executable, args=[_FIXTURE])]


def test_e2e_connect_list_call():
    """connect → 看到 mcp__echo__echo → call_tool 拿到回显。"""
    mgr = McpManager(_CFG, connect_timeout=40)
    try:
        results = mgr.connect_all()
        assert "echo" in results
        defs = results["echo"]
        assert len(defs) == 1
        assert defs[0].name == "mcp__echo__echo"
        assert defs[0].category == "mcp"

        out = mgr.call_tool("echo", "echo", {"text": "hello"})
        assert "echo: hello" in out
    finally:
        mgr.dispose()


def test_e2e_dispose_then_call_fails():
    """dispose 后连接关闭，call_tool 失败。"""
    mgr = McpManager(_CFG, connect_timeout=40)
    mgr.connect_all()
    mgr.dispose()
    with pytest.raises(Exception):
        mgr.call_tool("echo", "echo", {"text": "x"})


def test_e2e_registry_execute_simulates_tool_chain():
    """模拟组合根: manager + ToolRegistry，验证 execute 走通（最接近真实 tool_chain 路径）。

    覆盖: materialize 的 handler 闭包 → manager.call_tool 同步门面 →
    run_coroutine_threadsafe 到常驻 loop → session.call_tool → _normalize → str。
    以及 ToolRegistry.execute 的 jsonschema 校验 + ThreadPoolExecutor 超时壳。
    """
    from agent_core.tools.base import ToolRegistry

    mgr = McpManager(_CFG, connect_timeout=40)
    try:
        mgr.connect_all()
        registry = ToolRegistry()
        for _name, defs in mgr.registered_tools().items():
            for td in defs:
                registry.register(td)

        assert "mcp__echo__echo" in registry.list_names()

        result = registry.execute("mcp__echo__echo", {"text": "via_registry"})
        assert result["status"] == "success"
        assert "via_registry" in result["output"]
    finally:
        mgr.dispose()


def test_e2e_disconnect_server_isolated():
    """配一个 echo + 一个不存在的 server：后者失败不阻断前者。"""
    cfgs = [
        McpServerConfig(name="echo", kind="stdio", command=sys.executable, args=[_FIXTURE]),
        McpServerConfig(name="ghost", kind="stdio", command="/no/such/binary", args=[]),
    ]
    mgr = McpManager(cfgs, connect_timeout=10)
    try:
        results = mgr.connect_all()
        assert len(results["echo"]) == 1     # echo 成功
        assert results["ghost"] == []        # ghost 失败，返空
        out = mgr.call_tool("echo", "echo", {"text": "still works"})
        assert "still works" in out
    finally:
        mgr.dispose()
