"""tests/test_mcp_client.py — MCP 单 server 连接单测（Step 3）。

测能力协商分支（_negotiate_and_list）。transport + 真连接的冒烟测留 Step 7 E2E。
用 asyncio.run 包异步，无需 pytest-asyncio（项目未装，但有 anyio plugin）。
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from agent_core.mcp.client import _negotiate_and_list, McpServerHandle


def test_negotiate_no_tools_capability_does_not_list():
    """caps.tools=None → 不发 list_tools（省往返）。"""
    async def run():
        session = MagicMock()
        session.get_server_capabilities.return_value = SimpleNamespace(tools=None)
        caps, tools = await _negotiate_and_list(session, list_timeout=5)
        assert tools == []
        session.list_tools.assert_not_called()
    asyncio.run(run())


def test_negotiate_with_tools_lists():
    """caps.tools 非 None → list_tools 拿到工具。"""
    async def run():
        session = MagicMock()
        session.get_server_capabilities.return_value = SimpleNamespace(tools=SimpleNamespace())
        session.list_tools = AsyncMock(return_value=SimpleNamespace(tools=[
            SimpleNamespace(name="read_file", description="d", inputSchema={}),
            SimpleNamespace(name="write_file", description="d2", inputSchema={}),
        ]))
        caps, tools = await _negotiate_and_list(session, list_timeout=5)
        assert len(tools) == 2
        assert tools[0].name == "read_file"
    asyncio.run(run())


def test_negotiate_caps_none_does_not_list():
    """get_server_capabilities 返 None（未初始化）→ 不 list，不抛。"""
    async def run():
        session = MagicMock()
        session.get_server_capabilities.return_value = None
        caps, tools = await _negotiate_and_list(session, list_timeout=5)
        assert tools == []
        session.list_tools.assert_not_called()
    asyncio.run(run())


def test_negotiate_caps_missing_tools_attr_does_not_list():
    """caps 存在但没有 tools 属性 → 不 list。"""
    async def run():
        session = MagicMock()
        session.get_server_capabilities.return_value = SimpleNamespace()  # 无 tools
        caps, tools = await _negotiate_and_list(session, list_timeout=5)
        assert tools == []
        session.list_tools.assert_not_called()
    asyncio.run(run())


def test_server_handle_dataclass():
    """McpServerHandle 基本字段。"""
    h = McpServerHandle(name="fs", session="sess", capabilities=None, tools=[1, 2])
    assert h.name == "fs"
    assert h.tools == [1, 2]
