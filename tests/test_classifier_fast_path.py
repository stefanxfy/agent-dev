"""
classifier_fast_path.py 测试

覆盖:
1. is_auto_mode_allowlisted_tool / is_fast_path_disabled_tool 静态判定
2. FastPathResult dataclass 三种工厂方法(miss/allow/ask)
3. check_classifier_fast_path 三阶段逻辑
4. _swap_mode context 临时切换
5. duck-typed tool 接口(Step 10 前用 simple namespace 代替)
"""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
from typing import Callable, Optional

import pytest

from agent_core.tools.classifier_fast_path import (
    FastPathResult,
    _swap_mode,
    check_classifier_fast_path,
    is_auto_mode_allowlisted_tool,
    is_fast_path_disabled_tool,
)
from agent_core.tools.permission_types import (
    PermissionBehavior,
    PermissionDecision,
    PermissionMode,
    ToolPermissionContext,
)


# ────────────────────────────────────────────────────────────────────
# Test helper: 简易 Tool-like 对象
# ────────────────────────────────────────────────────────────────────

@dataclass
class FakeTool:
    """Step 10 前的 Tool-like duck-typed 测试用工具"""
    name: str
    requires_user_interaction: bool = False
    check_permissions: Optional[Callable] = None


def _ctx(mode: str = "default", **kwargs) -> ToolPermissionContext:
    """构造 ToolPermissionContext 工厂"""
    return ToolPermissionContext(mode=mode, **kwargs)


# ────────────────────────────────────────────────────────────────────
# is_auto_mode_allowlisted_tool
# ────────────────────────────────────────────────────────────────────

class TestIsAutoModeAllowlistedTool:
    @pytest.mark.parametrize("tool_name", ["Read", "Glob", "Grep", "ListFiles", "ReadImage"])
    def test_allowlist_tools(self, tool_name):
        """5 个只读工具在 allowlist"""
        assert is_auto_mode_allowlisted_tool(tool_name) is True

    @pytest.mark.parametrize("tool_name", ["Edit", "Write", "Bash", "MultiEdit", "NotebookEdit", "Agent"])
    def test_non_allowlist_tools(self, tool_name):
        """破坏性工具不在 allowlist"""
        assert is_auto_mode_allowlisted_tool(tool_name) is False


# ────────────────────────────────────────────────────────────────────
# is_fast_path_disabled_tool
# ────────────────────────────────────────────────────────────────────

class TestIsFastPathDisabledTool:
    @pytest.mark.parametrize("tool_name", ["Agent", "REPL"])
    def test_disabled_tools(self, tool_name):
        """Agent / REPL 禁用 fast-path"""
        assert is_fast_path_disabled_tool(tool_name) is True

    @pytest.mark.parametrize("tool_name", ["Read", "Edit", "Bash", "Glob"])
    def test_enabled_tools(self, tool_name):
        """其他工具不禁用"""
        assert is_fast_path_disabled_tool(tool_name) is False


# ────────────────────────────────────────────────────────────────────
# FastPathResult
# ────────────────────────────────────────────────────────────────────

class TestFastPathResult:
    def test_miss_factory(self):
        """miss() 返 hit=False"""
        r = FastPathResult.miss()
        assert r.hit is False
        assert r.behavior is None
        assert r.stage is None

    def test_allow_factory(self):
        """allow() 返 hit=True,behavior=allow"""
        r = FastPathResult.allow(reason="test", stage="stage_1")
        assert r.hit is True
        assert r.behavior == PermissionBehavior.ALLOW.value
        assert r.reason == "test"
        assert r.stage == "stage_1"

    def test_ask_factory(self):
        """ask() 返 hit=True,behavior=ask"""
        r = FastPathResult.ask(reason="need user", stage="stage_0")
        assert r.hit is True
        assert r.behavior == PermissionBehavior.ASK.value
        assert r.stage == "stage_0"

    def test_to_permission_decision_miss(self):
        """miss 转 PASSTHROUGH decision"""
        r = FastPathResult.miss()
        d = r.to_permission_decision()
        assert d.behavior == PermissionBehavior.PASSTHROUGH.value

    def test_to_permission_decision_allow(self):
        """allow 转 ALLOW decision"""
        r = FastPathResult.allow(reason="auto", stage="stage_2")
        d = r.to_permission_decision()
        assert d.behavior == PermissionBehavior.ALLOW.value
        assert d.message == "auto"

    def test_to_permission_decision_ask(self):
        """ask 转 ASK + OtherReason"""
        r = FastPathResult.ask(reason="agent", stage="stage_0")
        d = r.to_permission_decision()
        assert d.behavior == PermissionBehavior.ASK.value
        assert d.decision_reason is not None


# ────────────────────────────────────────────────────────────────────
# Stage 0: requires_user_interaction
# ────────────────────────────────────────────────────────────────────

class TestStage0RequiresUserInteraction:
    def test_agent_tool_returns_ask(self):
        """requires_user_interaction=True → ASK stage_0"""
        tool = FakeTool(name="Agent", requires_user_interaction=True)
        ctx = _ctx(mode="default")
        result = check_classifier_fast_path(tool, {}, ctx)
        assert result.hit is True
        assert result.behavior == PermissionBehavior.ASK.value
        assert result.stage == "stage_0_agent"

    def test_sub_agent_returns_ask_even_in_auto_mode(self):
        """auto mode 下 sub-agent 仍 ASK(不 bypass)"""
        tool = FakeTool(name="Agent", requires_user_interaction=True)
        ctx = _ctx(mode="auto")
        result = check_classifier_fast_path(tool, {}, ctx)
        assert result.hit is True
        assert result.behavior == PermissionBehavior.ASK.value


# ────────────────────────────────────────────────────────────────────
# Stage 1: acceptEdits mode
# ────────────────────────────────────────────────────────────────────

class TestStage1AcceptEdits:
    def test_accept_edits_with_check_permissions_allow(self):
        """acceptEdits + check_permissions 返 ALLOW → ALLOW"""
        def check(input_dict, ctx):
            return PermissionDecision(behavior=PermissionBehavior.ALLOW.value)
        tool = FakeTool(name="Edit", check_permissions=check)
        ctx = _ctx(mode="acceptEdits")
        result = check_classifier_fast_path(tool, {"path": "x.py"}, ctx)
        assert result.hit is True
        assert result.behavior == PermissionBehavior.ALLOW.value
        assert result.stage == "stage_1_accept_edits"

    def test_accept_edits_with_check_permissions_deny_fallthrough(self):
        """acceptEdits + check_permissions 返 DENY → 不 fast-path(走完整 pipeline)"""
        def check(input_dict, ctx):
            return PermissionDecision(behavior=PermissionBehavior.DENY.value)
        tool = FakeTool(name="Edit", check_permissions=check)
        ctx = _ctx(mode="acceptEdits")
        result = check_classifier_fast_path(tool, {"path": ".ssh/x"}, ctx)
        assert result.hit is False

    def test_accept_edits_without_check_permissions_defaults_allow(self):
        """acceptEdits + 无 check_permissions → 默认 ALLOW(对齐 CC)"""
        tool = FakeTool(name="Edit")  # 无 check_permissions
        ctx = _ctx(mode="acceptEdits")
        result = check_classifier_fast_path(tool, {"path": "x.py"}, ctx)
        assert result.hit is True
        assert result.behavior == PermissionBehavior.ALLOW.value
        assert result.stage == "stage_1_accept_edits"

    def test_accept_edits_agent_tool_disabled_fallthrough(self):
        """acceptEdits + Agent tool 走 fast-path disabled → fallthrough"""
        tool = FakeTool(name="Agent", requires_user_interaction=False)
        ctx = _ctx(mode="acceptEdits")
        result = check_classifier_fast_path(tool, {}, ctx)
        assert result.hit is False  # fast-path disabled

    def test_accept_edits_repl_tool_disabled_fallthrough(self):
        """acceptEdits + REPL tool 走 fast-path disabled → fallthrough"""
        tool = FakeTool(name="REPL", requires_user_interaction=False)
        ctx = _ctx(mode="acceptEdits")
        result = check_classifier_fast_path(tool, {}, ctx)
        assert result.hit is False

    def test_accept_edits_check_permissions_exception_fallthrough(self):
        """check_permissions 抛异常 → 不阻断 fast-path,降级 fallthrough"""
        def check(input_dict, ctx):
            raise RuntimeError("boom")
        tool = FakeTool(name="Edit", check_permissions=check)
        ctx = _ctx(mode="acceptEdits")
        # 不抛异常,返 miss
        result = check_classifier_fast_path(tool, {"path": "x.py"}, ctx)
        assert result.hit is False

    def test_default_mode_skips_stage1(self):
        """default mode 不走 stage 1"""
        tool = FakeTool(name="Edit")
        ctx = _ctx(mode="default")
        result = check_classifier_fast_path(tool, {}, ctx)
        assert result.hit is False  # stage 1 不触发


# ────────────────────────────────────────────────────────────────────
# Stage 2: auto mode allowlist
# ────────────────────────────────────────────────────────────────────

class TestStage2AutoModeAllowlist:
    @pytest.mark.parametrize("tool_name", ["Read", "Glob", "Grep", "ListFiles", "ReadImage"])
    def test_auto_mode_allowlist_returns_allow(self, tool_name):
        """auto mode + allowlist tool → ALLOW"""
        tool = FakeTool(name=tool_name)
        ctx = _ctx(mode="auto")
        result = check_classifier_fast_path(tool, {}, ctx)
        assert result.hit is True
        assert result.behavior == PermissionBehavior.ALLOW.value
        assert result.stage == "stage_2_allowlist"

    @pytest.mark.parametrize("tool_name", ["Edit", "Write", "Bash"])
    def test_auto_mode_non_allowlist_fallthrough(self, tool_name):
        """auto mode + 非 allowlist tool → fallthrough"""
        tool = FakeTool(name=tool_name)
        ctx = _ctx(mode="auto")
        result = check_classifier_fast_path(tool, {}, ctx)
        assert result.hit is False

    def test_default_mode_skips_stage2(self):
        """default mode 不走 stage 2 即使工具在 allowlist"""
        tool = FakeTool(name="Read")
        ctx = _ctx(mode="default")
        result = check_classifier_fast_path(tool, {}, ctx)
        assert result.hit is False


# ────────────────────────────────────────────────────────────────────
# Stage 3: fallthrough(全部 default mode + Edit tool)
# ────────────────────────────────────────────────────────────────────

class TestStage3Fallthrough:
    def test_default_mode_edit_fallthrough(self):
        """default mode + Edit tool → fallthrough(走完整 pipeline)"""
        tool = FakeTool(name="Edit")
        ctx = _ctx(mode="default")
        result = check_classifier_fast_path(tool, {"path": "x.py"}, ctx)
        assert result.hit is False

    def test_bypass_mode_edit_fallthrough(self):
        """bypass mode 不在 fast-path 中处理(由 PermissionEngine 的 bypass 短路)"""
        tool = FakeTool(name="Edit")
        ctx = _ctx(mode="bypassPermissions")
        result = check_classifier_fast_path(tool, {}, ctx)
        assert result.hit is False  # fast-path 不管 bypass


# ────────────────────────────────────────────────────────────────────
# _swap_mode
# ────────────────────────────────────────────────────────────────────

class TestSwapMode:
    def test_swap_creates_new_instance(self):
        """swap_mode 返新 context(原 instance 不变)"""
        ctx = _ctx(mode="default")
        new = _swap_mode(ctx, "acceptEdits")
        assert ctx.mode == "default"  # 原不变
        assert new.mode == "acceptEdits"

    def test_swap_preserves_other_fields(self):
        """swap 保留其他字段"""
        ctx = ToolPermissionContext(
            mode="default",
            always_allow_rules={"projectSettings": ["Edit"]},
        )
        new = _swap_mode(ctx, "auto")
        assert new.always_allow_rules == {"projectSettings": ["Edit"]}


# ────────────────────────────────────────────────────────────────────
# 集成:auto mode 下 Read 走 fast-path,Edit 走 fallthrough
# ────────────────────────────────────────────────────────────────────

class TestIntegrationAutoMode:
    def test_read_in_auto_mode(self):
        """auto + Read → fast-path ALLOW"""
        tool = FakeTool(name="Read")
        ctx = _ctx(mode="auto")
        result = check_classifier_fast_path(tool, {"path": "src/main.py"}, ctx)
        assert result.hit is True
        assert result.behavior == PermissionBehavior.ALLOW.value

    def test_edit_in_auto_mode_fallthrough(self):
        """auto + Edit → fallthrough(可能命中 global allow 或 ask)"""
        tool = FakeTool(name="Edit")
        ctx = _ctx(mode="auto")
        result = check_classifier_fast_path(tool, {"path": "x.py"}, ctx)
        assert result.hit is False


# ────────────────────────────────────────────────────────────────────
# duck-typed tool(SimpleNamespace 也兼容)
# ────────────────────────────────────────────────────────────────────

class TestDuckTypedTool:
    def test_simple_namespace_with_name(self):
        """SimpleNamespace 只要有 name 即可"""
        tool = SimpleNamespace(name="Read")
        ctx = _ctx(mode="auto")
        result = check_classifier_fast_path(tool, {}, ctx)
        assert result.hit is True
        assert result.behavior == PermissionBehavior.ALLOW.value

    def test_simple_namespace_requires_user_interaction(self):
        """SimpleNamespace 有 requires_user_interaction 也识别"""
        tool = SimpleNamespace(name="SubAgent", requires_user_interaction=True)
        ctx = _ctx(mode="auto")
        result = check_classifier_fast_path(tool, {}, ctx)
        assert result.hit is True
        assert result.behavior == PermissionBehavior.ASK.value

    def test_non_tool_like_returns_miss(self):
        """非 tool-like 对象(str / int)→ miss"""
        ctx = _ctx(mode="auto")
        result = check_classifier_fast_path("not a tool", {}, ctx)
        assert result.hit is False

        result2 = check_classifier_fast_path(42, {}, ctx)
        assert result2.hit is False
