"""tests/test_permission_steps/ — 13 个 step handler 独立测试(D.3d)。

每个 step 测试覆盖:
  - hit case(命中 → 返对应 decision)
  - miss case(不命中 → 返 None,继续 pipeline)
  - edge case(边界条件)

与 tests/test_permission_engine.py 互补:
  - engine 测试是集成测试(端到端 check_permissions)
  - 本文件是单元测试(直接调 step(ctx),隔离 engine pipeline)

跑:python -m pytest tests/test_permission_steps/ -v
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional

import pytest

from agent_core.tools.permission.denial import DenialTrackingState, clear_all_denial_states
from agent_core.tools.permission.rule_checker import RuleChecker
from agent_core.tools.permission.steps import PermissionContext
from agent_core.tools.permission.steps.bash_check import BashCheckStep
from agent_core.tools.permission.steps.bypass_mode import BypassModeStep
from agent_core.tools.permission.steps.default_ask import DefaultAskStep
from agent_core.tools.permission.steps.denial_limit import DenialLimitStep
from agent_core.tools.permission.steps.fast_path import FastPathStep
from agent_core.tools.permission.steps.global_allow import GlobalAllowStep
from agent_core.tools.permission.steps.global_ask import GlobalAskStep
from agent_core.tools.permission.steps.global_deny import GlobalDenyStep
from agent_core.tools.permission.steps.hook import HookStep
from agent_core.tools.permission.steps.mode_postprocess import ModePostProcessStep
from agent_core.tools.permission.steps.requires_user import RequiresUserStep
from agent_core.tools.permission.steps.safety import SafetyStep
from agent_core.tools.permission.steps.tool_check import ToolCheckStep
from agent_core.tools.permission.types import (
    AsyncAgentReason,
    ClassifierReason,
    ModeReason,
    OtherReason,
    PermissionBehavior,
    PermissionDecision,
    PermissionMode,
    RuleReason,
    SafetyCheckReason,
    ToolPermissionContext,
)


@pytest.fixture(autouse=True)
def _clear_global_store():
    clear_all_denial_states()
    yield
    clear_all_denial_states()


# ────────────────────────────────────────────────────────────────────
# Helpers
# ────────────────────────────────────────────────────────────────────

@dataclass
class FakeTool:
    name: str
    requires_user_interaction: bool = False
    check_permissions: Optional[Callable] = None


def _tctx(
    mode: str = "default",
    always_allow: Optional[dict] = None,
    always_deny: Optional[dict] = None,
    always_ask: Optional[dict] = None,
    no_settings_match: bool = True,
    should_avoid_permission_prompts: bool = False,
) -> ToolPermissionContext:
    return ToolPermissionContext(
        mode=mode,
        always_allow_rules=always_allow or {},
        always_deny_rules=always_deny or {},
        always_ask_rules=always_ask or {},
        no_settings_match=no_settings_match,
        should_avoid_permission_prompts=should_avoid_permission_prompts,
    )


def _pctx(
    tool=None,
    tool_input=None,
    context=None,
    rule_checker=None,
    **kw,
) -> PermissionContext:
    """构造 PermissionContext(默认无 rule 命中、bypass off)。"""
    tool = tool or FakeTool(name="Read")
    return PermissionContext(
        tool=tool,
        tool_input=tool_input or {"path": "x.py"},
        context=context or _tctx(),
        rule_checker=rule_checker or RuleChecker(context or _tctx()),
        tool_name=getattr(tool, "name", "unknown"),
        **kw,
    )


# ────────────────────────────────────────────────────────────────────
# 1a. GlobalDenyStep
# ────────────────────────────────────────────────────────────────────

class TestGlobalDenyStep:
    def test_hit_returns_deny(self):
        ctx = _tctx(always_deny={"projectSettings": ["Read(/secret/*)"]})
        pctx = _pctx(
            tool=FakeTool(name="Read"),
            tool_input={"path": "/secret/x"},
            context=ctx,
            rule_checker=RuleChecker(ctx),
        )
        result = GlobalDenyStep()(pctx)
        assert result is not None
        d, stage = result
        assert stage == 'step_1a_global_deny'  # 细 stage 粒度(对齐原 monolith)
        assert d.behavior == PermissionBehavior.DENY.value
        assert isinstance(d.decision_reason, RuleReason)

    def test_miss_returns_none(self):
        """不命中 → None(继续 pipeline)。"""
        ctx = _tctx(always_deny={"projectSettings": ["Read(/secret/*)"]})
        pctx = _pctx(
            tool=FakeTool(name="Read"),
            tool_input={"path": "/public/x"},
            context=ctx,
            rule_checker=RuleChecker(ctx),
        )
        assert GlobalDenyStep()(pctx) is None

    def test_no_rule_content_matches_any_input(self):
        """无 content 的 deny rule(如 "Read")命中任何 input。"""
        ctx = _tctx(always_deny={"projectSettings": ["Read"]})
        pctx = _pctx(
            tool=FakeTool(name="Read"),
            tool_input={"path": "/anywhere/x"},
            context=ctx,
            rule_checker=RuleChecker(ctx),
        )
        result = GlobalDenyStep()(pctx)
        assert result is not None
        d, stage = result
        assert stage == 'step_1a_global_deny'
        assert d.behavior == PermissionBehavior.DENY.value


# ────────────────────────────────────────────────────────────────────
# 1b. GlobalAskStep
# ────────────────────────────────────────────────────────────────────

class TestGlobalAskStep:
    def test_hit_returns_ask(self):
        ctx = _tctx(always_ask={"projectSettings": ["Read(/ask/*)"]})
        pctx = _pctx(
            tool=FakeTool(name="Read"),
            tool_input={"path": "/ask/x"},
            context=ctx,
            rule_checker=RuleChecker(ctx),
        )
        result = GlobalAskStep()(pctx)
        assert result is not None
        d, stage = result
        assert stage == 'step_1b_global_ask'
        assert d.behavior == PermissionBehavior.ASK.value
        assert isinstance(d.decision_reason, RuleReason)

    def test_miss_returns_none(self):
        ctx = _tctx(always_ask={"projectSettings": ["Read(/ask/*)"]})
        pctx = _pctx(
            tool=FakeTool(name="Read"),
            tool_input={"path": "/other/x"},
            context=ctx,
            rule_checker=RuleChecker(ctx),
        )
        assert GlobalAskStep()(pctx) is None


# ────────────────────────────────────────────────────────────────────
# 1c. ToolCheckStep
# ────────────────────────────────────────────────────────────────────

class TestToolCheckStep:
    def test_tool_returns_deny_propagates(self):
        """tool.check_permissions 返 DENY → step 返 DENY。"""
        def check_fn(tool_input, context):
            return PermissionDecision(behavior=PermissionBehavior.DENY.value)
        pctx = _pctx(tool=FakeTool(name="X", check_permissions=check_fn))
        result = ToolCheckStep()(pctx)
        assert result is not None
        d, stage = result
        assert stage == 'step_1c_tool_check_deny'
        assert d.behavior == PermissionBehavior.DENY.value

    def test_tool_returns_ask_does_not_terminate(self):
        """tool.check_permissions 返 ASK → step 返 None(只有 DENY 终止)。"""
        def check_fn(tool_input, context):
            return PermissionDecision(behavior=PermissionBehavior.ASK.value)
        pctx = _pctx(tool=FakeTool(name="X", check_permissions=check_fn))
        assert ToolCheckStep()(pctx) is None

    def test_no_check_permissions_fn_returns_none(self):
        pctx = _pctx(tool=FakeTool(name="X", check_permissions=None))
        assert ToolCheckStep()(pctx) is None

    def test_check_permissions_exception_swallowed(self):
        """tool.check_permissions 抛异常 → 吞掉,返 None。"""
        def check_fn(tool_input, context):
            raise RuntimeError("boom")
        pctx = _pctx(tool=FakeTool(name="X", check_permissions=check_fn))
        assert ToolCheckStep()(pctx) is None


# ────────────────────────────────────────────────────────────────────
# 1c'. BashCheckStep
# ────────────────────────────────────────────────────────────────────

class TestBashCheckStep:
    def test_non_bash_tool_skipped(self):
        """非 Bash tool → 返 None(不调 bash_check_fn)。"""
        calls = []
        def bash_fn(tool_input):
            calls.append(tool_input)
            return None
        pctx = _pctx(tool=FakeTool(name="Read"), )
        step = BashCheckStep(bash_fn)
        assert step(pctx) is None
        assert calls == []   # 未调用

    def test_bash_deny_terminates(self):
        def bash_fn(tool_input):
            return PermissionDecision(behavior=PermissionBehavior.DENY.value)
        pctx = _pctx(tool=FakeTool(name="Bash"), tool_input={"command": "rm x"})
        result = BashCheckStep(bash_fn)(pctx)
        assert result is not None
        d, stage = result
        assert stage == 'step_1c_bash_deny'
        assert d.behavior == PermissionBehavior.DENY.value

    def test_bash_passthrough_returns_none(self):
        """bash_check 返 PASSTHROUGH → None(fall through 到 1d+)。"""
        def bash_fn(tool_input):
            return PermissionDecision(behavior=PermissionBehavior.PASSTHROUGH.value)
        pctx = _pctx(tool=FakeTool(name="Bash"), tool_input={"command": "ls"})
        assert BashCheckStep(bash_fn)(pctx) is None

    def test_bash_fn_exception_swallowed(self):
        def bash_fn(tool_input):
            raise RuntimeError("boom")
        pctx = _pctx(tool=FakeTool(name="Bash"), tool_input={"command": "ls"})
        assert BashCheckStep(bash_fn)(pctx) is None


# ────────────────────────────────────────────────────────────────────
# 1d. RequiresUserStep
# ────────────────────────────────────────────────────────────────────

class TestRequiresUserStep:
    def test_true_returns_ask(self):
        pctx = _pctx(tool=FakeTool(name="X", requires_user_interaction=True))
        result = RequiresUserStep()(pctx)
        assert result is not None
        d, stage = result
        assert stage == 'step_1d_requires_user'
        assert d.behavior == PermissionBehavior.ASK.value
        assert isinstance(d.decision_reason, OtherReason)

    def test_false_returns_none(self):
        pctx = _pctx(tool=FakeTool(name="X", requires_user_interaction=False))
        assert RequiresUserStep()(pctx) is None


# ────────────────────────────────────────────────────────────────────
# 1e. SafetyStep
# ────────────────────────────────────────────────────────────────────

class TestSafetyStep:
    def test_sensitive_path_returns_ask(self):
        """读 .agent_data/settings.json → safety 命中 → ASK。"""
        pctx = _pctx(
            tool=FakeTool(name="Read"),
            tool_input={"path": ".agent_data/settings.json"},
        )
        result = SafetyStep()(pctx)
        assert result is not None
        d, stage = result
        assert stage == 'step_1e_safety_check'
        assert d.behavior == PermissionBehavior.ASK.value
        assert isinstance(d.decision_reason, SafetyCheckReason)
        assert d.decision_reason.classifier_approvable is False

    def test_safe_path_returns_none(self):
        pctx = _pctx(
            tool=FakeTool(name="Read"),
            tool_input={"path": "docs/README.md"},
        )
        assert SafetyStep()(pctx) is None


# ────────────────────────────────────────────────────────────────────
# 1.5. HookStep
# ────────────────────────────────────────────────────────────────────

class TestHookStep:
    def _hook_registry(self, behavior, hook_name="test_hook", reason="r", updated_input=None):
        from agent_core.tools.permission.hook import HookRegistry, PreToolUseResult
        reg = HookRegistry()
        result = PreToolUseResult(
            behavior=behavior, hook_name=hook_name, reason=reason,
            updated_input=updated_input,
        )
        reg.run_pre_tool_use = lambda *a, **k: result   # stub
        return reg

    def test_deny_terminates(self):
        pctx = _pctx(hook_registry=self._hook_registry(PermissionBehavior.DENY.value))
        result = HookStep()(pctx)
        assert result is not None
        d, stage = result
        assert stage == 'step_1_5_hook_deny'
        assert d.behavior == PermissionBehavior.DENY.value
        assert "test_hook denied" in d.decision_reason.reason

    def test_ask_terminates_with_updated_input(self):
        pctx = _pctx(
            tool_input={"command": "ls"},
            hook_registry=self._hook_registry(
                PermissionBehavior.ASK.value,
                updated_input={"command": "ls -la"},
            ),
        )
        result = HookStep()(pctx)
        assert result is not None
        d, stage = result
        assert stage == 'step_1_5_hook_ask'
        assert d.behavior == PermissionBehavior.ASK.value
        assert d.updated_input == {"command": "ls -la"}

    def test_allow_returns_none(self):
        """hook 返 allow/passthrough → 继续 pipeline。"""
        pctx = _pctx(hook_registry=self._hook_registry(PermissionBehavior.ALLOW.value))
        assert HookStep()(pctx) is None

    def test_none_registry_returns_none(self):
        pctx = _pctx(hook_registry=None)
        assert HookStep()(pctx) is None


# ────────────────────────────────────────────────────────────────────
# 2a. BypassModeStep
# ────────────────────────────────────────────────────────────────────

class TestBypassModeStep:
    def test_bypass_returns_allow(self):
        pctx = _pctx(context=_tctx(mode=PermissionMode.BYPASS.value))
        result = BypassModeStep()(pctx)
        assert result is not None
        d, stage = result
        assert stage == 'step_2a_bypass_mode'
        assert d.behavior == PermissionBehavior.ALLOW.value
        assert isinstance(d.decision_reason, ModeReason)

    def test_non_bypass_returns_none(self):
        pctx = _pctx(context=_tctx(mode="default"))
        assert BypassModeStep()(pctx) is None


# ────────────────────────────────────────────────────────────────────
# 2b. GlobalAllowStep
# ────────────────────────────────────────────────────────────────────

class TestGlobalAllowStep:
    def test_hit_returns_allow(self):
        ctx = _tctx(always_allow={"projectSettings": ["Read(/ok/*)"]})
        pctx = _pctx(
            tool=FakeTool(name="Read"),
            tool_input={"path": "/ok/x"},
            context=ctx,
            rule_checker=RuleChecker(ctx),
        )
        result = GlobalAllowStep()(pctx)
        assert result is not None
        d, stage = result
        assert stage == 'step_2b_global_allow'
        assert d.behavior == PermissionBehavior.ALLOW.value
        assert isinstance(d.decision_reason, RuleReason)

    def test_miss_returns_none(self):
        ctx = _tctx(always_allow={"projectSettings": ["Read(/ok/*)"]})
        pctx = _pctx(
            tool=FakeTool(name="Read"),
            tool_input={"path": "/other/x"},
            context=ctx,
            rule_checker=RuleChecker(ctx),
        )
        assert GlobalAllowStep()(pctx) is None


# ────────────────────────────────────────────────────────────────────
# 3. FastPathStep
# ────────────────────────────────────────────────────────────────────

class TestFastPathStep:
    def test_hit_returns_decision(self):
        """acceptEdits mode + Edit → fast_path 命中 ALLOW。"""
        ctx = _tctx(mode=PermissionMode.ACCEPT_EDITS.value, no_settings_match=False)
        pctx = _pctx(
            tool=FakeTool(name="Edit"),
            tool_input={"file_path": "x.py"},
            context=ctx,
        )
        result = FastPathStep()(pctx)
        assert result is not None
        d, stage = result
        assert stage.startswith('step_3_fast_path_')  # 细 stage(stage_0_agent/1_accept_edits/2_allowlist)
        assert d.behavior == PermissionBehavior.ALLOW.value

    def test_miss_returns_none(self):
        """default mode 无 fast-path 命中。"""
        ctx = _tctx(mode="default")
        pctx = _pctx(
            tool=FakeTool(name="Read"),
            tool_input={"path": "x.py"},
            context=ctx,
        )
        assert FastPathStep()(pctx) is None


# ────────────────────────────────────────────────────────────────────
# 4. ModePostProcessStep
# ────────────────────────────────────────────────────────────────────

class TestModePostProcessStep:
    def test_dontask_returns_deny(self):
        pctx = _pctx(context=_tctx(mode=PermissionMode.DONT_ASK.value))
        result = ModePostProcessStep()(pctx)
        assert result is not None
        d, stage = result
        assert stage == 'step_4_dontask'
        assert d.behavior == PermissionBehavior.DENY.value
        assert isinstance(d.decision_reason, ModeReason)

    def test_async_agent_returns_deny(self):
        pctx = _pctx(context=_tctx(should_avoid_permission_prompts=True))
        result = ModePostProcessStep()(pctx)
        assert result is not None
        d, stage = result
        assert stage == 'step_4_async_agent'
        assert d.behavior == PermissionBehavior.DENY.value
        assert isinstance(d.decision_reason, AsyncAgentReason)

    def test_default_mode_returns_none(self):
        """default mode + 非 async → 继续到 Step 6。"""
        pctx = _pctx(context=_tctx(mode="default"))
        assert ModePostProcessStep()(pctx) is None


# ────────────────────────────────────────────────────────────────────
# 6. DenialLimitStep
# ────────────────────────────────────────────────────────────────────

class TestDenialLimitStep:
    def test_under_limit_returns_none(self):
        pctx = _pctx(denial_state=DenialTrackingState())
        assert DenialLimitStep()(pctx) is None

    def test_none_state_returns_none(self):
        pctx = _pctx(denial_state=None)
        assert DenialLimitStep()(pctx) is None


# ────────────────────────────────────────────────────────────────────
# 7. DefaultAskStep
# ────────────────────────────────────────────────────────────────────

class TestDefaultAskStep:
    def test_always_returns_ask(self):
        """兜底 step 总返 ASK(非 None)。"""
        pctx = _pctx()
        result = DefaultAskStep()(pctx)
        assert result is not None
        d, stage = result
        assert stage == 'step_7_default_ask'
        assert d.behavior == PermissionBehavior.ASK.value
        assert isinstance(d.decision_reason, OtherReason)
