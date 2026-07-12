"""tests/test_mcp_list_changed.py — Phase 2 Step 5: list_changed 通知分发单测 + race fix。

R 修复（2026-07-10）: list_changed 在连接阶段（_servers 未就绪）到达时，
_refresh 记入 _pending_refresh，_keep_alive 设置 _servers 后补刷（notify=False）。
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from mcp import types

from agent_core.mcp.manager import McpManager, _LiveServer
from agent_core.tools.base import ToolDef


def test_handle_tool_list_changed_refreshes_and_calls_callback():
    cb = MagicMock()
    mgr = McpManager([], on_tools_changed=cb)
    session = MagicMock()
    session.list_tools = AsyncMock(return_value=SimpleNamespace(tools=[
        SimpleNamespace(name="new_tool", description="d", inputSchema={})]))
    mgr._servers["s"] = _LiveServer(session=session, tool_defs=[])
    asyncio.run(mgr._handle_notification("s", types.ToolListChangedNotification()))
    # tool_defs 刷新 + 命名
    assert len(mgr._servers["s"].tool_defs) == 1
    assert mgr._servers["s"].tool_defs[0].name == "mcp__s__new_tool"
    # on_tools_changed 被调
    cb.assert_called_once_with("s")


def test_handle_resource_list_changed_refreshes():
    mgr = McpManager([])
    session = MagicMock()
    session.list_resources = AsyncMock(return_value=SimpleNamespace(resources=[
        SimpleNamespace(uri="file:///x", description="d", name="r")]))
    mgr._servers["s"] = _LiveServer(session=session, tool_defs=[], resources=[])
    asyncio.run(mgr._handle_notification("s", types.ResourceListChangedNotification()))
    assert len(mgr._servers["s"].resources) == 1


def test_handle_prompt_list_changed_refreshes():
    mgr = McpManager([])
    session = MagicMock()
    session.list_prompts = AsyncMock(return_value=SimpleNamespace(prompts=[
        SimpleNamespace(name="p", description="d", arguments=[])]))
    mgr._servers["s"] = _LiveServer(session=session, tool_defs=[], prompts=[])
    asyncio.run(mgr._handle_notification("s", types.PromptListChangedNotification()))
    assert len(mgr._servers["s"].prompts) == 1


def test_handle_resource_updated_does_not_throw():
    mgr = McpManager([])
    # ResourceUpdatedNotification 的 params(uri) 是 required，用 model_construct 绕过验证
    notif = types.ResourceUpdatedNotification.model_construct()
    asyncio.run(mgr._handle_notification("s", notif))
    # 不抛即可（MVP 不做细粒度刷新）


def test_handle_unknown_notification_ignored():
    mgr = McpManager([])
    asyncio.run(mgr._handle_notification("s", SimpleNamespace()))   # 不抛


def test_refresh_unknown_server_records_to_pending():
    """_servers 未就绪时 _refresh 记 pending（连接阶段 race fix）"""
    mgr = McpManager([])
    # _servers 为空 → 记 pending，不抛
    asyncio.run(mgr._refresh("ghost", "tools"))
    assert mgr._pending_refresh.get("ghost") == {"tools"}


def test_refresh_resources_unknown_server_records_to_pending():
    """resources/list_changed 连接阶段 race：记 pending"""
    mgr = McpManager([])
    asyncio.run(mgr._refresh("ghost", "resources"))
    assert mgr._pending_refresh.get("ghost") == {"resources"}


def test_refresh_prompts_unknown_server_records_to_pending():
    """prompts/list_changed 连接阶段 race：记 pending"""
    mgr = McpManager([])
    asyncio.run(mgr._refresh("ghost", "prompts"))
    assert mgr._pending_refresh.get("ghost") == {"prompts"}


def test_refresh_tools_callback_exception_does_not_break():
    """on_tools_changed 抛异常时 _refresh 不应中断（warn 后继续）。"""
    cb = MagicMock(side_effect=RuntimeError("cb boom"))
    mgr = McpManager([], on_tools_changed=cb)
    session = MagicMock()
    session.list_tools = AsyncMock(return_value=SimpleNamespace(tools=[
        SimpleNamespace(name="t", description="d", inputSchema={})]))
    mgr._servers["s"] = _LiveServer(session=session, tool_defs=[])
    asyncio.run(mgr._handle_notification("s", types.ToolListChangedNotification()))
    # tool_defs 仍刷新了（回调异常不影响刷新本身）
    assert len(mgr._servers["s"].tool_defs) == 1
    cb.assert_called_once_with("s")


def test_refresh_tools_list_failure_is_isolated():
    """list_tools 抛异常时 _refresh warn 不扩散。"""
    mgr = McpManager([])
    session = MagicMock()
    session.list_tools = AsyncMock(side_effect=RuntimeError("server down"))
    mgr._servers["s"] = _LiveServer(session=session, tool_defs=[ToolDef(
        name="mcp__s__old", description="d", parameters={}, handler=lambda **k: "")])
    asyncio.run(mgr._refresh("s", "tools"))   # 不抛


def test_pending_consumed_in_keep_alive_with_notify_false():
    """连接就绪后 _keep_alive 消费 pending：notify=False → 不调 on_tools_changed。

    场景：list_changed 在 _servers 就绪前到达 → _pending_refresh 暂存 →
    _keep_alive 设置 _servers 后调 _do_refresh(notify=False) → 组合根随后
    用 _servers.tool_defs 注册（避免与 on_tools_changed 重复 register）。
    """
    cb = MagicMock()
    mgr = McpManager([], on_tools_changed=cb)
    session = MagicMock()
    session.list_tools = AsyncMock(return_value=SimpleNamespace(tools=[
        SimpleNamespace(name="t", description="d", inputSchema={})]))

    # 模拟"连接阶段收到 list_changed"——此时 _servers 还没设
    asyncio.run(mgr._refresh("s", "tools"))
    assert mgr._pending_refresh.get("s") == {"tools"}

    # 模拟 _keep_alive 行为：先设 _servers，再循环消费 pending（notify=False）
    mgr._servers["s"] = _LiveServer(session=session, tool_defs=[])
    pending_kinds = mgr._pending_refresh.pop("s", None)
    assert pending_kinds == {"tools"}
    for kind in pending_kinds:   # 1 kind 这里只跑一次
        asyncio.run(mgr._do_refresh("s", kind, mgr._servers["s"], notify=False))

    # tool_defs 已刷新
    assert len(mgr._servers["s"].tool_defs) == 1
    # notify=False → on_tools_changed 未被调（避免与组合根随后 register 重复）
    cb.assert_not_called()
    # pending 已消费
    assert "s" not in mgr._pending_refresh


def test_pending_notifies_on_post_connect_refresh():
    """连接就绪后的运行期 list_changed 仍正常通知（notify=True 默认）。"""
    cb = MagicMock()
    mgr = McpManager([], on_tools_changed=cb)
    session = MagicMock()
    session.list_tools = AsyncMock(return_value=SimpleNamespace(tools=[
        SimpleNamespace(name="t", description="d", inputSchema={})]))
    mgr._servers["s"] = _LiveServer(session=session, tool_defs=[])
    # _servers 已就绪 → _refresh 走 _do_refresh 默认 notify=True
    asyncio.run(mgr._refresh("s", "tools"))
    cb.assert_called_once_with("s")


def test_pending_dedup_same_kind():
    """同 kind 多次 pending → set 自动去重，不会刷多次。"""
    mgr = McpManager([])
    asyncio.run(mgr._refresh("s", "tools"))
    asyncio.run(mgr._refresh("s", "tools"))
    asyncio.run(mgr._refresh("s", "tools"))
    assert mgr._pending_refresh.get("s") == {"tools"}


def test_pending_multi_kinds_all_refreshed():
    """多种 kind 在连接阶段连发，全部 pending 并被 _keep_alive 补刷（边角情况修复）。

    之前 dict[str, str] 会覆盖丢 kind；现在 dict[str, set[str]] 保留所有 kind。
    """
    cb = MagicMock()
    mgr = McpManager([], on_tools_changed=cb)
    session = MagicMock()
    # 3 个 list_* mock 各自返不同数据，便于断言"每个 kind 都被刷过"
    session.list_tools = AsyncMock(return_value=SimpleNamespace(tools=[
        SimpleNamespace(name="t", description="d", inputSchema={})]))
    session.list_resources = AsyncMock(return_value=SimpleNamespace(resources=[
        SimpleNamespace(uri="file:///r", description="d", name="r")]))
    session.list_prompts = AsyncMock(return_value=SimpleNamespace(prompts=[
        SimpleNamespace(name="p", description="d", arguments=[])]))

    # 模拟连接阶段连发 3 种 list_changed
    asyncio.run(mgr._refresh("s", "prompts"))    # 先到（任意顺序）
    asyncio.run(mgr._refresh("s", "tools"))      # 覆盖式到达
    asyncio.run(mgr._refresh("s", "resources"))  # 又一个
    assert mgr._pending_refresh.get("s") == {"tools", "resources", "prompts"}

    # 模拟 _keep_alive：先设 _servers，再循环消费
    mgr._servers["s"] = _LiveServer(session=session, tool_defs=[], resources=[], prompts=[])
    pending_kinds = mgr._pending_refresh.pop("s", None)
    for kind in sorted(pending_kinds):
        asyncio.run(mgr._do_refresh("s", kind, mgr._servers["s"], notify=False))

    # 3 个 kind 各自刷新到 _servers（没丢）
    assert len(mgr._servers["s"].tool_defs) == 1
    assert len(mgr._servers["s"].resources) == 1
    assert len(mgr._servers["s"].prompts) == 1
    # notify=False 仍生效
    cb.assert_not_called()
    # pending 已清
    assert "s" not in mgr._pending_refresh
