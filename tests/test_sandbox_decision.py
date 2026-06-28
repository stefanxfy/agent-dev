"""
sandbox_decision.py 测试

覆盖:
1. should_use_sandbox 四段短路
2. SANDBOXED_TOOLS 名单
3. _is_excluded_command substring match + 只对 Bash
4. dangerously_disable_sandbox 与 allow_unsandboxed_commands 协同
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from agent_core.tools.sandbox_decision import (
    SANDBOXED_TOOLS,
    _is_excluded_command,
    should_use_sandbox,
)
from agent_core.tools.sandbox_manager import SandboxManager


@pytest.fixture(autouse=True)
def reset_sandbox_singleton():
    """每个测试重置单例状态,避免状态泄漏"""
    mgr = SandboxManager()
    mgr._reset_for_testing()
    yield
    mgr._reset_for_testing()


# ────────────────────────────────────────────────────────────────────
# SANDBOXED_TOOLS
# ────────────────────────────────────────────────────────────────────

class TestSandboxedTools:
    def test_includes_bash_read_write_edit(self):
        assert "Bash" in SANDBOXED_TOOLS
        assert "Read" in SANDBOXED_TOOLS
        assert "Write" in SANDBOXED_TOOLS
        assert "Edit" in SANDBOXED_TOOLS

    def test_excludes_calc_search(self):
        assert "calc" not in SANDBOXED_TOOLS
        assert "search" not in SANDBOXED_TOOLS

    def test_is_frozen(self):
        # frozenset 不可变
        with pytest.raises(AttributeError):
            SANDBOXED_TOOLS.add("x")  # type: ignore


# ────────────────────────────────────────────────────────────────────
# should_use_sandbox — sandbox 禁用路径
# ────────────────────────────────────────────────────────────────────

class TestSandboxDisabled:
    def test_disabled_returns_false_for_bash(self):
        # sandbox 默认禁用
        assert should_use_sandbox("Bash", {"command": "ls"}) is False

    def test_disabled_returns_false_for_all_tools(self):
        for tool in ["Bash", "Read", "Write", "Edit", "calc", "search"]:
            assert should_use_sandbox(tool, {}) is False


# ────────────────────────────────────────────────────────────────────
# should_use_sandbox — sandbox 启用路径
# ────────────────────────────────────────────────────────────────────

class TestSandboxEnabled:
    @pytest.fixture(autouse=True)
    def enable_sandbox(self):
        mgr = SandboxManager()
        mgr.load_config({"enabled": True})
        # mock 平台 + 依赖 + 初始化
        with patch.object(mgr, "_is_supported_platform", return_value=True), \
             patch.object(mgr, "_check_dependencies", return_value=True), \
             patch.object(mgr, "initialize", lambda: setattr(mgr, "_initialized", True)):
            yield

    def test_bash_uses_sandbox_when_enabled(self):
        assert should_use_sandbox("Bash", {"command": "ls"}) is True

    def test_read_uses_sandbox(self):
        assert should_use_sandbox("Read", {"path": "/tmp/x"}) is True

    def test_write_uses_sandbox(self):
        assert should_use_sandbox("Write", {"path": "/tmp/x", "content": "y"}) is True

    def test_edit_uses_sandbox(self):
        assert should_use_sandbox("Edit", {"path": "/tmp/x"}) is True

    def test_calc_never_uses_sandbox(self):
        assert should_use_sandbox("calc", {"expression": "1+1"}) is False

    def test_search_never_uses_sandbox(self):
        assert should_use_sandbox("search", {"query": "hello"}) is False

    def test_unknown_tool_returns_false(self):
        assert should_use_sandbox("CustomTool", {}) is False


# ────────────────────────────────────────────────────────────────────
# dangerously_disable_sandbox
# ────────────────────────────────────────────────────────────────────

class TestDangerouslyDisableSandbox:
    @pytest.fixture(autouse=True)
    def enable_sandbox(self):
        mgr = SandboxManager()
        mgr.load_config({"enabled": True})
        with patch.object(mgr, "_is_supported_platform", return_value=True), \
             patch.object(mgr, "_check_dependencies", return_value=True), \
             patch.object(mgr, "initialize", lambda: setattr(mgr, "_initialized", True)):
            yield

    def test_disable_skips_sandbox_when_allowed(self):
        # allow_unsandboxed_commands=True(默认)→ disable 生效
        assert should_use_sandbox("Bash", {
            "command": "ls",
            "dangerously_disable_sandbox": True,
        }) is False

    def test_disable_still_sandboxed_when_strict_mode(self):
        # allow_unsandboxed_commands=False(strict)→ disable 不生效,仍沙箱化
        mgr = SandboxManager()
        mgr.load_config({
            "enabled": True,
            "allowUnsandboxedCommands": False,
        })
        with patch.object(mgr, "_is_supported_platform", return_value=True), \
             patch.object(mgr, "_check_dependencies", return_value=True), \
             patch.object(mgr, "initialize", lambda: setattr(mgr, "_initialized", True)):
            assert should_use_sandbox("Bash", {
                "command": "ls",
                "dangerously_disable_sandbox": True,
            }) is True

    def test_disable_false_uses_sandbox(self):
        assert should_use_sandbox("Bash", {
            "command": "ls",
            "dangerously_disable_sandbox": False,
        }) is True

    def test_disable_missing_uses_sandbox(self):
        # 没传 dangerously_disable_sandbox 字段 → 默认沙箱化
        assert should_use_sandbox("Bash", {"command": "ls"}) is True

    def test_disable_with_non_bash_tool(self):
        # 非 Bash 工具 disable 不影响(calc 本来就不沙箱)
        assert should_use_sandbox("calc", {
            "expression": "1+1",
            "dangerously_disable_sandbox": True,
        }) is False


# ────────────────────────────────────────────────────────────────────
# _is_excluded_command
# ────────────────────────────────────────────────────────────────────

class TestIsExcludedCommand:
    @pytest.fixture(autouse=True)
    def enable_sandbox_with_excludes(self):
        mgr = SandboxManager()
        mgr.load_config({
            "enabled": True,
            "excludedCommands": ["git commit", "npm"],
        })
        with patch.object(mgr, "_is_supported_platform", return_value=True), \
             patch.object(mgr, "_check_dependencies", return_value=True), \
             patch.object(mgr, "initialize", lambda: setattr(mgr, "_initialized", True)):
            yield

    def test_exact_match_excludes(self):
        assert _is_excluded_command("Bash", {"command": "git commit"}) is True

    def test_substring_match_excludes(self):
        # "git commit" 是 "git commit -m 'msg'" 的 substring
        assert _is_excluded_command("Bash", {"command": "git commit -m 'msg'"}) is True

    def test_npm_pattern_matches(self):
        assert _is_excluded_command("Bash", {"command": "npm install express"}) is True

    def test_no_match_returns_false(self):
        assert _is_excluded_command("Bash", {"command": "ls -la"}) is False

    def test_empty_command_returns_false(self):
        assert _is_excluded_command("Bash", {"command": ""}) is False

    def test_missing_command_returns_false(self):
        assert _is_excluded_command("Bash", {}) is False

    def test_case_sensitive_match(self):
        # substring match 大小写敏感
        assert _is_excluded_command("Bash", {"command": "NPM install"}) is False

    def test_non_bash_tool_returns_false(self):
        # 只对 Bash 检查 excluded_commands
        assert _is_excluded_command("Read", {"path": "git commit"}) is False

    def test_excluded_skips_sandbox_via_should_use(self):
        # 通过 should_use_sandbox 验证 excluded 命令不走沙箱
        assert should_use_sandbox("Bash", {"command": "git commit -m 'x'"}) is False


# ────────────────────────────────────────────────────────────────────
# 边界 case
# ────────────────────────────────────────────────────────────────────

class TestEdgeCases:
    def test_empty_tool_name_returns_false(self):
        assert should_use_sandbox("", {}) is False

    def test_none_tool_input(self):
        # tool_input 可能是 None(防御性)
        # sandbox 默认禁用 → False
        assert should_use_sandbox("Bash", {}) is False

    def test_command_with_none_value(self):
        # command 字段值是 None
        mgr = SandboxManager()
        mgr.load_config({"enabled": True, "excludedCommands": ["x"]})
        with patch.object(mgr, "_is_supported_platform", return_value=True), \
             patch.object(mgr, "_check_dependencies", return_value=True), \
             patch.object(mgr, "initialize", lambda: setattr(mgr, "_initialized", True)):
            # command=None → 当空串处理,不匹配 excluded
            assert _is_excluded_command("Bash", {"command": None}) is False
