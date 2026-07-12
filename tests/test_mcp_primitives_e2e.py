"""Phase 2 E2E: 连 FastMCP fixture server（暴露 tools+resources+prompts）。

验证 resources/prompts 消费端整条链：connect → list resources/prompts →
read_resource / get_prompt → 经 ToolRegistry.execute 调全局工具。
（list_changed 的真服务器触发需 server 主动发通知，FastMCP 默认不动态改，
逻辑由 test_mcp_list_changed.py 单测覆盖。）
"""

import os
import sys

from agent_core.mcp.config import McpServerConfig
from agent_core.mcp.manager import McpManager

_FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "primitives_mcp_server.py")
_CFG = [McpServerConfig(name="prim", kind="stdio", command=sys.executable, args=[_FIXTURE])]


def test_e2e_resources_listed_on_connect():
    mgr = McpManager(_CFG, connect_timeout=40)
    try:
        mgr.connect_all()
        out = mgr.list_resources()
        assert "[prim]" in out
        assert "config://app" in out
    finally:
        mgr.dispose()


def test_e2e_read_resource():
    mgr = McpManager(_CFG, connect_timeout=40)
    try:
        mgr.connect_all()
        out = mgr.read_resource("prim", "config://app")
        assert "key=value" in out
        assert "version=1.0" in out
    finally:
        mgr.dispose()


def test_e2e_prompts_listed_and_get():
    mgr = McpManager(_CFG, connect_timeout=40)
    try:
        mgr.connect_all()
        prompts = mgr.registered_prompts()
        assert "prim" in prompts
        names = [getattr(p, "name", "") for p in prompts["prim"]]
        assert "review" in names
        assert "greet" in names
        out = mgr.get_prompt("prim", "review", {"code": "print('x')"})
        assert "print('x')" in out
    finally:
        mgr.dispose()


def test_e2e_global_tools_via_registry():
    """组合根层面：3 个全局工具注册 + 经 ToolRegistry.execute 调通。"""
    from agent_core.mcp.materialize import materialize_prompt_tools, materialize_resource_tools
    from agent_core.tools.base import ToolRegistry

    mgr = McpManager(_CFG, connect_timeout=40)
    try:
        mgr.connect_all()
        registry = ToolRegistry()
        for td in materialize_resource_tools(mgr):
            registry.register(td)
        for td in materialize_prompt_tools(mgr):
            registry.register(td)

        assert "list_mcp_resources" in registry.list_names()
        assert "read_mcp_resource" in registry.list_names()
        assert "get_mcp_prompt" in registry.list_names()

        r = registry.execute("read_mcp_resource", {"server": "prim", "uri": "config://app"})
        assert r["status"] == "success"
        assert "key=value" in r["output"]

        r2 = registry.execute("get_mcp_prompt", {"server": "prim", "name": "greet",
                                                  "arguments": {"name": "world"}})
        assert r2["status"] == "success"
        assert "hello world" in r2["output"]
    finally:
        mgr.dispose()


def test_e2e_root_capability_declared():
    """roots capability 声明：server 连上即声明（roots callback 非空）。
    这里间接验证：连接成功（server 不因 roots 协商失败）。"""
    mgr = McpManager(_CFG, connect_timeout=40, roots=[{"uri": "file:///tmp", "name": "t"}])
    try:
        results = mgr.connect_all()
        assert "prim" in results   # 连接成功（roots 声明不影响）
    finally:
        mgr.dispose()
