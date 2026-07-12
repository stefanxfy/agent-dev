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
from agent_core.tools.permission.engine import PermissionEngine
from agent_core.tools.permission.types import (
    OtherReason,
    PermissionBehavior,
    PermissionDecision,
    PermissionMode,
    ToolPermissionContext,
)
from agent_core.tools.sandbox.manager import SandboxManager


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
            "agent_core.tools.permission.engine.PermissionEngine._run_bash_check_permissions"
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
        with patch.object(mgr, "is_sandbox_enabled", return_value=True):
            engine = _make_engine(sandbox_enabled=True)
            tool = _bash_tool_def()
            decision = engine.check_permissions(tool, {"command": "npm install"})
        assert decision.behavior == PermissionBehavior.ALLOW.value


# ────────────────────────────────────────────────────────────────────
# audit_logger 集成(engine 是唯一审计点)
# ────────────────────────────────────────────────────────────────────

class TestAuditLoggerIntegration:
    def test_engine_logs_decision_to_audit_logger(self):
        # engine._log_and_return 是唯一审计点:每条 decision 都经此
        audit_mock = MagicMock()
        engine = PermissionEngine(context=_ctx(), audit_logger=audit_mock)
        tool = _bash_tool_def()
        engine.check_permissions(tool, {"command": "ls"})
        audit_mock.log.assert_called_once()
        call_kwargs = audit_mock.log.call_args.kwargs
        assert call_kwargs["tool_name"] == "Bash"
        assert call_kwargs["stage"] is not None
        assert call_kwargs["context"] is engine.context

    def test_engine_skips_audit_when_logger_none(self):
        # audit_logger=None → 不写,不抛
        engine = PermissionEngine(context=_ctx(), audit_logger=None)
        tool = _bash_tool_def()
        # 不应抛
        engine.check_permissions(tool, {"command": "ls"})

    def test_engine_audit_failure_does_not_break_pipeline(self):
        audit_mock = MagicMock()
        audit_mock.log.side_effect = RuntimeError("disk full")
        engine = PermissionEngine(context=_ctx(), audit_logger=audit_mock)
        tool = _bash_tool_def()
        # 不应抛,仍返 decision
        decision = engine.check_permissions(tool, {"command": "ls"})
        assert decision.behavior is not None

    def test_engine_logs_deny_with_stage(self):
        audit_mock = MagicMock()
        engine = PermissionEngine(
            context=_ctx(always_deny_rules={"projectSettings": ["Bash(rm:*)"]}),
            audit_logger=audit_mock,
        )
        tool = _bash_tool_def()
        engine.check_permissions(tool, {"command": "rm -rf /"})
        call_kwargs = audit_mock.log.call_args.kwargs
        assert call_kwargs["decision"].behavior == PermissionBehavior.DENY.value
        assert "deny" in (call_kwargs["stage"] or "")


# ────────────────────────────────────────────────────────────────────
# system prompt sandbox section 注入
# ────────────────────────────────────────────────────────────────────

class TestSystemPromptSandboxSection:
    """Phase 5 重构：SystemPromptAssembler 删了，装配逻辑内联到 SystemPromptHandler._build/_sandbox_section。
    base 从 agent.llm.config.system_prompt 读（不再有 agent.system_prompt 字段）。
    """
    def _make_agent_for_prompt(self, *, sandbox_enabled=False):
        """构造一个最小 agent(绕过 __init__)用于 prompt 注入测试。
        base 通过 llm.config.system_prompt 注入(不再有 agent.system_prompt 字段)。
        """
        from agent_core.agent_core import ReactAgent
        from agent_core.turn_chain import SystemPromptHandler
        agent = ReactAgent.__new__(ReactAgent)
        agent.permission_engine = _make_engine(sandbox_enabled=sandbox_enabled)
        agent.llm = SimpleNamespace(config=SimpleNamespace(system_prompt="base prompt"))
        agent.memory_index = None  # SystemPromptHandler._build 需要(None 跳过 MEMORY 段)
        self._handler = SystemPromptHandler(agent)
        return agent

    def test_sandbox_section_omitted_when_disabled(self):
        agent = self._make_agent_for_prompt(sandbox_enabled=False)
        section = self._handler._sandbox_section(agent)
        assert section == ""

    def test_sandbox_section_present_when_enabled(self):
        mgr = SandboxManager()
        mgr.load_config({"enabled": True})
        with patch.object(mgr, "is_sandbox_enabled", return_value=True), \
             patch("agent_core.tools.sandbox.manager.get_sandbox_tmp_dir", return_value="/tmp/claude-1000"):
            agent = self._make_agent_for_prompt()  # sandbox_enabled 无关(已 patch is_sandbox_enabled)
            section = self._handler._sandbox_section(agent)
        assert "## Command sandbox" in section

    def test_sandbox_section_injected_into_full_prompt(self):
        mgr = SandboxManager()
        mgr.load_config({"enabled": True})
        with patch.object(mgr, "is_sandbox_enabled", return_value=True), \
             patch("agent_core.tools.sandbox.manager.get_sandbox_tmp_dir", return_value="/tmp/claude-1000"):
            agent = self._make_agent_for_prompt()
            full = self._handler._build(agent)
        assert "base prompt" in full
        assert "## Command sandbox" in full

    def test_prompt_omits_sandbox_when_engine_none(self):
        from agent_core.agent_core import ReactAgent
        from agent_core.turn_chain import SystemPromptHandler
        agent = ReactAgent.__new__(ReactAgent)
        agent.permission_engine = None
        agent.llm = SimpleNamespace(config=SimpleNamespace(system_prompt="base"))
        handler = SystemPromptHandler(agent)
        assert handler._sandbox_section(agent) == ""

    def test_sandbox_prompt_failure_returns_empty(self):
        agent = self._make_agent_for_prompt()
        with patch(
            "agent_core.tools.sandbox.prompt.get_sandbox_prompt_section",
            side_effect=RuntimeError("boom"),
        ):
            section = self._handler._sandbox_section(agent)
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
