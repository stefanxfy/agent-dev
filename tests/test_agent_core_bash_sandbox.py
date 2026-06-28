"""
agent_core.py 整合测试 — BashTool + sandbox wrap + audit logger

覆盖:
1. PermissionEngine Step 1c' BashTool 路由
2. audit_logger 在 decision 后被调
3. audit_logger 失败不阻断执行
4. BashTool 通过 agent run 执行
5. sandbox wrap 行为
6. system prompt sandbox section 注入
7. 回归:Read/calc 仍工作
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from agent_core.tools.base import ToolDef, ToolRegistry
from agent_core.tools.builtin import register_builtin_tools
from agent_core.tools.permission_engine import PermissionEngine
from agent_core.tools.permission_types import (
    OtherReason,
    PermissionBehavior,
    PermissionDecision,
    PermissionMode,
    ToolPermissionContext,
)
from agent_core.tools.sandbox_manager import SandboxManager


@pytest.fixture(autouse=True)
def reset_sandbox():
    mgr = SandboxManager()
    mgr._reset_for_testing()
    yield
    mgr._reset_for_testing()


def _ctx(**kwargs):
    defaults = {"always_deny_rules": {}, "always_ask_rules": {}, "always_allow_rules": {}}
    defaults.update(kwargs)
    return ToolPermissionContext(**defaults)


def _make_engine(**ctx_kwargs):
    ctx = _ctx(**ctx_kwargs)
    return PermissionEngine(context=ctx)


def _bash_tool_def():
    """duck-typed Bash tool(模拟 builtin BASH_TOOL)"""
    return SimpleNamespace(
        name="Bash",
        check_permissions=None,
        requires_user_interaction=False,
        category="shell",
    )


# ────────────────────────────────────────────────────────────────────
# Step 1c' BashTool 路由
# ────────────────────────────────────────────────────────────────────

class TestBashRouting:
    def test_bash_routes_to_bash_check_permissions(self):
        engine = _make_engine()
        tool = _bash_tool_def()
        # 无 rule + 非 sandbox → passthrough(bash_check_permissions 返 passthrough)
        decision = engine.check_permissions(tool, {"command": "ls -la"})
        # bash 返 passthrough → engine fall through → 最终 default ASK(step 7)
        # 或匹配 allow rule;这里无 rule → ASK
        assert decision.behavior in (
            PermissionBehavior.ASK.value, PermissionBehavior.PASSTHROUGH.value,
        )

    def test_bash_deny_rule_blocks(self):
        engine = _make_engine(
            always_deny_rules={"projectSettings": ["Bash(rm:*)"]},
        )
        tool = _bash_tool_def()
        decision = engine.check_permissions(tool, {"command": "rm -rf /"})
        assert decision.behavior == PermissionBehavior.DENY.value

    def test_bash_cd_git_blocks(self):
        engine = _make_engine()
        tool = _bash_tool_def()
        decision = engine.check_permissions(
            tool, {"command": "cd /tmp && git status"},
        )
        assert decision.behavior == PermissionBehavior.ASK.value
        assert decision.decision_reason.type == "safetyCheck"

    def test_bash_subcommand_deny_blocks(self):
        engine = _make_engine(
            always_deny_rules={"projectSettings": ["Bash(rm:*)"]},
        )
        tool = _bash_tool_def()
        decision = engine.check_permissions(
            tool, {"command": "echo a && rm -rf /"},
        )
        assert decision.behavior == PermissionBehavior.DENY.value

    def test_non_bash_tool_does_not_route_to_bash_check(self):
        # Read tool 不应走 bash_check_permissions
        engine = _make_engine()
        read_tool = SimpleNamespace(
            name="Read", check_permissions=None, requires_user_interaction=False,
        )
        with patch(
            "agent_core.tools.permission_engine.PermissionEngine._run_bash_check_permissions"
        ) as mock_bash:
            engine.check_permissions(read_tool, {"path": "/tmp/x"})
        mock_bash.assert_not_called()

    def test_bash_check_exception_does_not_break_pipeline(self):
        engine = _make_engine()
        tool = _bash_tool_def()
        with patch.object(
            engine, "_run_bash_check_permissions",
            side_effect=RuntimeError("boom"),
        ):
            # 不应抛,降级继续正常 pipeline
            decision = engine.check_permissions(tool, {"command": "ls"})
        # 降级后走默认 ASK(无 rule)
        assert decision.behavior == PermissionBehavior.ASK.value


# ────────────────────────────────────────────────────────────────────
# sandbox_enabled context → bash auto-allow
# ────────────────────────────────────────────────────────────────────

class TestSandboxAutoAllowViaEngine:
    def test_bash_auto_allowed_when_sandbox_enabled(self):
        # 需 sandbox_manager 真启用 → mock 它
        mgr = SandboxManager()
        mgr.load_config({"enabled": True, "autoAllowBashIfSandboxed": True})
        with patch.object(mgr, "_is_supported_platform", return_value=True), \
             patch.object(mgr, "_check_dependencies", return_value=True), \
             patch.object(mgr, "initialize", lambda: setattr(mgr, "_initialized", True)):
            engine = _make_engine(sandbox_enabled=True)
            tool = _bash_tool_def()
            decision = engine.check_permissions(tool, {"command": "npm install"})
        assert decision.behavior == PermissionBehavior.ALLOW.value


# ────────────────────────────────────────────────────────────────────
# audit_logger 集成(通过 _log_audit)
# ────────────────────────────────────────────────────────────────────

class TestAuditLoggerIntegration:
    def _make_agent_with_audit(self):
        """构造一个最小 ReactAgent-like 对象测试 _log_audit"""
        from agent_core.agent_core import ReactAgent
        # 不真实例化(需要 LLM 等),用 __new__ 绕过 __init__
        agent = ReactAgent.__new__(ReactAgent)
        engine = _make_engine()
        agent.permission_engine = engine
        agent.audit_logger = MagicMock()
        return agent

    def test_log_audit_called_with_decision(self):
        agent = self._make_agent_with_audit()
        decision = PermissionDecision(
            behavior=PermissionBehavior.ALLOW.value,
            decision_reason=OtherReason(reason="test"),
        )
        agent._log_audit("Bash", {"command": "ls"}, decision)
        agent.audit_logger.log.assert_called_once()
        call_kwargs = agent.audit_logger.log.call_args
        assert call_kwargs.kwargs["tool_name"] == "Bash"

    def test_log_audit_skipped_when_logger_none(self):
        agent = self._make_agent_with_audit()
        agent.audit_logger = None
        # 不应抛
        decision = PermissionDecision(
            behavior=PermissionBehavior.ALLOW.value,
            decision_reason=OtherReason(reason="test"),
        )
        agent._log_audit("Bash", {"command": "ls"}, decision)

    def test_log_audit_failure_does_not_raise(self):
        agent = self._make_agent_with_audit()
        agent.audit_logger.log.side_effect = RuntimeError("disk full")
        decision = PermissionDecision(
            behavior=PermissionBehavior.ALLOW.value,
            decision_reason=OtherReason(reason="test"),
        )
        # 不应抛
        agent._log_audit("Bash", {"command": "ls"}, decision)

    def test_log_audit_passes_engine_context(self):
        agent = self._make_agent_with_audit()
        decision = PermissionDecision(
            behavior=PermissionBehavior.DENY.value,
            decision_reason=OtherReason(reason="deny"),
        )
        agent._log_audit("Bash", {"command": "rm"}, decision)
        call_kwargs = agent.audit_logger.log.call_args.kwargs
        assert call_kwargs["context"] is agent.permission_engine.context


# ────────────────────────────────────────────────────────────────────
# system prompt sandbox section 注入
# ────────────────────────────────────────────────────────────────────

class TestSystemPromptSandboxSection:
    def _make_agent_for_prompt(self):
        from agent_core.agent_core import ReactAgent
        agent = ReactAgent.__new__(ReactAgent)
        agent.permission_engine = _make_engine(sandbox_enabled=False)
        agent.system_prompt = "base prompt"
        agent.memory_index = None  # _build_system_prompt_with_memory 需要
        return agent

    def test_sandbox_section_omitted_when_disabled(self):
        agent = self._make_agent_for_prompt()
        section = agent._get_sandbox_prompt_section()
        assert section == ""

    def test_sandbox_section_present_when_enabled(self):
        mgr = SandboxManager()
        mgr.load_config({"enabled": True})
        with patch.object(mgr, "_is_supported_platform", return_value=True), \
             patch.object(mgr, "_check_dependencies", return_value=True), \
             patch.object(mgr, "initialize", lambda: setattr(mgr, "_initialized", True)), \
             patch.object(mgr, "_get_sandbox_tmp_dir", return_value="/tmp/claude-1000"):
            agent = self._make_agent_for_prompt()
            section = agent._get_sandbox_prompt_section()
        assert "## Command sandbox" in section

    def test_sandbox_section_injected_into_full_prompt(self):
        mgr = SandboxManager()
        mgr.load_config({"enabled": True})
        with patch.object(mgr, "_is_supported_platform", return_value=True), \
             patch.object(mgr, "_check_dependencies", return_value=True), \
             patch.object(mgr, "initialize", lambda: setattr(mgr, "_initialized", True)), \
             patch.object(mgr, "_get_sandbox_tmp_dir", return_value="/tmp/claude-1000"):
            agent = self._make_agent_for_prompt()
            full = agent._build_system_prompt_with_memory()
        assert "base prompt" in full
        assert "## Command sandbox" in full

    def test_prompt_omits_sandbox_when_engine_none(self):
        from agent_core.agent_core import ReactAgent
        agent = ReactAgent.__new__(ReactAgent)
        agent.permission_engine = None
        assert agent._get_sandbox_prompt_section() == ""

    def test_sandbox_prompt_failure_returns_empty(self):
        agent = self._make_agent_for_prompt()
        with patch(
            "agent_core.tools.sandbox_prompt.get_sandbox_prompt_section",
            side_effect=RuntimeError("boom"),
        ):
            section = agent._get_sandbox_prompt_section()
        assert section == ""


# ────────────────────────────────────────────────────────────────────
# ToolRegistry 集成 — BashTool 真执行
# ────────────────────────────────────────────────────────────────────

class TestBashToolExecutionViaRegistry:
    def test_registry_executes_bash_command(self):
        registry = ToolRegistry()
        register_builtin_tools(registry)
        result = registry.execute("Bash", {"command": "echo integration_test"})
        assert result["status"] == "success"
        assert "integration_test" in result["output"]

    def test_registry_returns_error_on_missing_command(self):
        registry = ToolRegistry()
        register_builtin_tools(registry)
        result = registry.execute("Bash", {})
        assert result["status"] == "error"

    def test_bash_tool_in_schema_list(self):
        registry = ToolRegistry()
        register_builtin_tools(registry)
        schemas = registry.list_schemas()
        names = [s["name"] for s in schemas]
        assert "Bash" in names
        assert "calc" in names


# ────────────────────────────────────────────────────────────────────
# 回归:现有工具仍工作
# ────────────────────────────────────────────────────────────────────

class TestRegressionExistingTools:
    def test_calc_still_works(self):
        registry = ToolRegistry()
        register_builtin_tools(registry)
        result = registry.execute("calc", {"expression": "2 + 3"})
        assert result["status"] == "success"
        assert "5" in result["output"]

    def test_search_tool_registered(self):
        registry = ToolRegistry()
        register_builtin_tools(registry)
        assert registry.get("search") is not None

    def test_bash_in_engine_allows_when_passthrough_via_check(self):
        # 综合:engine + Bash + 无 rule → passthrough(经 _check_tool_permission 转成 ASK/allow)
        # 这里只验证 engine 层不挂
        engine = _make_engine()
        tool = _bash_tool_def()
        decision = engine.check_permissions(tool, {"command": "echo hi"})
        # 不抛即可
        assert decision.behavior is not None
