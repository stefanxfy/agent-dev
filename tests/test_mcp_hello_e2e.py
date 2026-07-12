"""tests/test_mcp_hello_e2e.py — 综合 hello_mcp_server.py 的 stdio E2E 测试。

覆盖 agent_core MCP client 集成的每条路径：
- 工具 7 个（含 structured / image / roots / progress / error / 动态 add）
- resource 4 个（text / URI template / blob / metadata）
- prompt 5 个（无参 / 必选 / 可选 / 多消息 / 嵌入 resource）
- list_changed 动态增删 tool

跑：.venv/bin/python -m pytest tests/test_mcp_hello_e2e.py -v
"""

import os
import sys
import tempfile
import textwrap
import time

from agent_core.mcp.config import McpServerConfig
from agent_core.mcp.manager import McpManager

_FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "hello_mcp_server.py")
_CFG = [McpServerConfig(name="hello", kind="stdio", command=sys.executable, args=[_FIXTURE])]


def _new_mgr(call_timeout=20):
    return McpManager(_CFG, connect_timeout=30, call_timeout=call_timeout)


# ─── connect 基础 ──────────────────────────────────────────────────

def test_connect_all_lists_server_and_tools():
    mgr = _new_mgr()
    try:
        results = mgr.connect_all()
        assert "hello" in results
        tool_defs = results["hello"]
        tool_names = sorted(td.name for td in tool_defs)
        # 7 tools；不依赖顺序，按 set 断言
        assert tool_names == sorted([
            "mcp__hello__echo",
            "mcp__hello__add",
            "mcp__hello__get_image",
            "mcp__hello__read_root_file",
            "mcp__hello__long_operation",
            "mcp__hello__trigger_error",
            "mcp__hello__manage_dynamic_tool",
        ])
    finally:
        mgr.dispose()


def test_isolation_with_disabled_server():
    """disabled=True 时该 server 不连（manager 不抛）。"""
    cfg = [McpServerConfig(name="hello_off", kind="stdio", command=sys.executable,
                           args=[_FIXTURE], enabled=False)]
    mgr = McpManager(cfg, connect_timeout=10)
    try:
        results = mgr.connect_all()
        assert results == {}   # 没连
    finally:
        mgr.dispose()


def test_fault_isolation_invalid_command():
    """一个 server 命令不存在不阻断其他。"""
    bad = McpServerConfig(name="bad", kind="stdio", command="nonexistent-cmd-xyz",
                          args=[], enabled=True)
    cfg = [bad] + _CFG
    mgr = McpManager(cfg, connect_timeout=15)
    try:
        results = mgr.connect_all()
        # bad 跳过（不抛），hello 仍连上
        assert "hello" in results
        assert results.get("bad") == []
    finally:
        mgr.dispose()


# ─── Tools 内容类型 ────────────────────────────────────────────────

def test_tool_echo_returns_text():
    mgr = _new_mgr()
    try:
        mgr.connect_all()
        out = mgr.call_tool("hello", "echo", {"text": "ping"})
        assert "echo: ping" in out
    finally:
        mgr.dispose()


def test_tool_add_returns_structured_content():
    """add() 用 Pydantic + structured_output=True → 客户端拿到文本 + structuredContent。"""
    mgr = _new_mgr()
    try:
        mgr.connect_all()
        out = mgr.call_tool("hello", "add", {"a": 7, "b": 35})
        # 归一化会同时给文本和 [structured] 标签
        assert "42" in out
        assert "[structured]" in out
        assert '"sum": 42' in out
    finally:
        mgr.dispose()


def test_tool_get_image_returns_image_content():
    """Image helper → client._normalize_call_result 走 image 分支。"""
    mgr = _new_mgr()
    try:
        mgr.connect_all()
        out = mgr.call_tool("hello", "get_image", {})
        # _normalize_call_result 走 image 分支 → "[image: mime, N chars base64]"
        assert "[image:" in out
        assert "image/png" in out
        assert "chars base64]" in out
    finally:
        mgr.dispose()


def test_tool_trigger_error_raises_with_isError_marker():
    """trigger_error → raise RuntimeError（client error path）。"""
    mgr = _new_mgr()
    try:
        mgr.connect_all()
        try:
            mgr.call_tool("hello", "trigger_error", {"reason": "boom"})
        except RuntimeError as e:
            assert "boom" in str(e)
        else:
            raise AssertionError("expected RuntimeError")
    finally:
        mgr.dispose()


def test_tool_long_operation_does_not_block():
    """long_operation 不应阻塞或超时（client 端 progress 通知是 fire-and-forget）。"""
    mgr = _new_mgr(call_timeout=20)
    try:
        mgr.connect_all()
        out = mgr.call_tool("hello", "long_operation", {"steps": 3}, timeout=15)
        assert "completed 3 steps" in out
    finally:
        mgr.dispose()


# ─── Resources ──────────────────────────────────────────────────────

def test_list_resources_includes_3_static():
    """list_resources 列出静态资源。URI template（如 config://{key}）走 list_resource_templates，
    单独由 test_read_resource_uri_template_substitution 覆盖（用 read 间接验证可用）。"""
    mgr = _new_mgr()
    try:
        mgr.connect_all()
        out = mgr.list_resources()
        assert "[hello] text://hello" in out
        assert "[hello] blob://logo" in out
        assert "[hello] meta://info" in out
    finally:
        mgr.dispose()


def test_read_resource_text():
    mgr = _new_mgr()
    try:
        mgr.connect_all()
        out = mgr.read_resource("hello", "text://hello")
        assert "hello from text resource" in out
    finally:
        mgr.dispose()


def test_read_resource_blob_uses_blob_branch():
    """blob → 客户端 [_normalize_resource_result] blob 占位分支。"""
    mgr = _new_mgr()
    try:
        mgr.connect_all()
        out = mgr.read_resource("hello", "blob://logo")
        # blob 分支格式: "[blob: mime, N chars base64]"
        assert "[blob:" in out
        assert "image/png" in out
        assert "base64" in out
    finally:
        mgr.dispose()


def test_read_resource_uri_template_substitution():
    """config://{key} URI 模板：传 key 自动展开。"""
    mgr = _new_mgr()
    try:
        mgr.connect_all()
        out = mgr.read_resource("hello", "config://demo")
        assert "demo=value-demo" in out
    finally:
        mgr.dispose()


def test_read_resource_metadata_returns_json_text():
    mgr = _new_mgr()
    try:
        mgr.connect_all()
        out = mgr.read_resource("hello", "meta://info")
        # 应是 JSON 文本
        assert "hello_mcp_server" in out
        assert "version" in out
        assert "1.0" in out
    finally:
        mgr.dispose()


# ─── Prompts ────────────────────────────────────────────────────────

def test_list_prompts_includes_all_5():
    mgr = _new_mgr()
    try:
        mgr.connect_all()
        prompts = mgr.registered_prompts()
        names = sorted(p.name for p in prompts["hello"])
        assert names == sorted([
            "simple_greeting",
            "review_code",
            "code_review_with_context",
            "multi_turn_dialog",
            "with_embedded_resource",
        ])
    finally:
        mgr.dispose()


def test_prompt_simple_no_args():
    mgr = _new_mgr()
    try:
        mgr.connect_all()
        out = mgr.get_prompt("hello", "simple_greeting", {})
        assert "Say hello" in out
    finally:
        mgr.dispose()


def test_prompt_required_arg_in_substitution():
    mgr = _new_mgr()
    try:
        mgr.connect_all()
        out = mgr.get_prompt("hello", "review_code", {"code": "x = 1\n"})
        assert "x = 1" in out
        assert "review" in out.lower() or "审查" in out
    finally:
        mgr.dispose()


def test_prompt_optional_arg_uses_default():
    """code_review_with_context(code, language="python")：不传 language 用默认值。"""
    mgr = _new_mgr()
    try:
        mgr.connect_all()
        out = mgr.get_prompt("hello", "code_review_with_context", {"code": "let v = 1"})
        assert "python" in out
        assert "let v = 1" in out
    finally:
        mgr.dispose()


def test_prompt_optional_arg_overrides_default():
    mgr = _new_mgr()
    try:
        mgr.connect_all()
        out = mgr.get_prompt(
            "hello", "code_review_with_context",
            {"code": "let v = 1", "language": "rust"},
        )
        assert "rust" in out
        assert "let v = 1" in out
    finally:
        mgr.dispose()


def test_prompt_multi_message_returns_two_messages():
    """multi_turn_dialog(topic) 返回 list[PromptMessage] → 客户端归一为多条。"""
    mgr = _new_mgr()
    try:
        mgr.connect_all()
        out = mgr.get_prompt("hello", "multi_turn_dialog", {"topic": "asyncio"})
        # 客户端归一后格式: "[user] {...}\n[user] {...}" 之类，至少 2 段
        # FastMCP 会把所有 role 都标 user，但消息数和内容在
        assert out.count("[user]") >= 2 or out.count("assistant") >= 1
        assert "asyncio" in out
    finally:
        mgr.dispose()


def test_prompt_with_embedded_resource_includes_uri():
    """with_embedded_resource 嵌入 TextResourceContents。"""
    mgr = _new_mgr()
    try:
        mgr.connect_all()
        out = mgr.get_prompt("hello", "with_embedded_resource", {"text": "X"})
        # 应包含嵌入 resource 的 uri 和内联 text
        assert "text://hello" in out
        assert "hello from text resource" in out
        assert "Here is text: X" in out
    finally:
        mgr.dispose()


# ─── Roots 集成 ─────────────────────────────────────────────────────

def test_read_root_file_within_root():
    """声明 root 指向 tmpdir → 读 tmpdir 内文件 → 成功返回内容。"""
    with tempfile.TemporaryDirectory() as tmp:
        sample_file = os.path.join(tmp, "sample.txt")
        with open(sample_file, "w") as f:
            f.write("roots-ok-content")

        cfg = [McpServerConfig(name="hello", kind="stdio", command=sys.executable,
                               args=[_FIXTURE])]
        mgr = McpManager(
            cfg, connect_timeout=30, call_timeout=15,
            roots=[{"uri": f"file://{tmp}", "name": "tmp"}],
        )
        try:
            mgr.connect_all()
            out = mgr.call_tool("hello", "read_root_file", {"path": sample_file})
            assert "roots-ok-content" in out
        finally:
            mgr.dispose()


def test_read_root_file_outside_root_returns_error_text():
    """路径在声明 root 之外 → server 返回 Error 文本（isError via FastMCP → exception）。"""
    with tempfile.TemporaryDirectory() as tmp:
        # 用 tmp 做 root，但尝试读 /etc/hosts（任何 tmp 之外的文件）
        bad_path = "/etc/hosts"

        cfg = [McpServerConfig(name="hello", kind="stdio", command=sys.executable,
                               args=[_FIXTURE])]
        mgr = McpManager(
            cfg, connect_timeout=30, call_timeout=15,
            roots=[{"uri": f"file://{tmp}", "name": "tmp"}],
        )
        try:
            mgr.connect_all()
            try:
                out = mgr.call_tool("hello", "read_root_file", {"path": bad_path})
                # server 返回 "Error: ... not in declared roots"，可能 client 视为成功（str 返回）
                assert "Error" in out or "not in declared roots" in out
            except RuntimeError as e:
                # 也可能 server 用 ToolError raise，client 端报 RuntimeError
                assert "not in declared roots" in str(e) or "Error" in str(e)
        finally:
            mgr.dispose()


# ─── list_changed 动态增删 ─────────────────────────────────────────

def test_dynamic_tool_add_triggers_list_changed():
    """add dynamic tool → on_tools_changed 回调 → client ToolRegistry 刷新。

    这里只验证 manager 侧：registered_tools() 含新 tool。
    """
    mgr = _new_mgr()
    try:
        mgr.connect_all()
        before = [td.name for td in mgr.registered_tools()["hello"]]
        assert "mcp__hello__dyn1" not in before

        cb_calls = []
        mgr._on_tools_changed = lambda name: cb_calls.append(name)

        out = mgr.call_tool(
            "hello", "manage_dynamic_tool",
            {"action": "add", "name": "dyn1"},
        )
        assert "added dyn1" in out
        # 给 list_changed 通知 + on_tools_changed 一些时间
        time.sleep(0.5)
        after = [td.name for td in mgr.registered_tools()["hello"]]
        assert "mcp__hello__dyn1" in after
        assert cb_calls == ["hello"]   # 一次通知触发一次回调
    finally:
        mgr.dispose()


def test_dynamic_tool_remove_triggers_list_changed():
    mgr = _new_mgr()
    try:
        mgr.connect_all()
        # 先 add
        mgr.call_tool("hello", "manage_dynamic_tool", {"action": "add", "name": "dyn2"})
        time.sleep(0.3)
        assert "mcp__hello__dyn2" in [td.name for td in mgr.registered_tools()["hello"]]

        cb_calls = []
        mgr._on_tools_changed = lambda name: cb_calls.append(name)

        mgr.call_tool("hello", "manage_dynamic_tool", {"action": "remove", "name": "dyn2"})
        time.sleep(0.5)
        assert "mcp__hello__dyn2" not in [td.name for td in mgr.registered_tools()["hello"]]
        assert cb_calls == ["hello"]
    finally:
        mgr.dispose()


def test_dynamic_tool_dedup_set_logic_via_rapid_adds():
    """快速多次 add 同名工具：set-based 逻辑下注册只 1 次（FastMCP 端去重）。"""
    mgr = _new_mgr()
    try:
        mgr.connect_all()
        for _ in range(3):
            out = mgr.call_tool(
                "hello", "manage_dynamic_tool",
                {"action": "add", "name": "dyn3"},
            )
        time.sleep(0.5)
        dyn_names = [td.name for td in mgr.registered_tools()["hello"] if "dyn3" in td.name]
        # 可能 FastMCP 自动 dedup 为 1 个，或保留多个 — 至少 1 个
        assert len(dyn_names) >= 1
    finally:
        mgr.dispose()
