"""
permission_engine.py 测试

覆盖:
1. Step 1a: 全局 deny rule → DENY + RuleReason
2. Step 1b: 全局 ask rule → ASK + RuleReason
3. Step 1c: tool.check_permissions 返 DENY → DENY
4. Step 1d: requires_user_interaction → ASK
5. Step 1e: safety_check 命中 → ASK + SafetyCheckReason
6. Step 2a: bypass mode → ALLOW + ModeReason
7. Step 2b: tool 全局 allow rule → ALLOW + RuleReason
8. Step 3: classifier fast-path(acceptEdits + Read/auto)
9. Step 4: mode 后处理(dontAsk → DENY / should_avoid_permission_prompts → DENY)
10. Step 5: hook DENY/ASK 覆盖
11. Step 6: denial limit → ASK fallback
12. Step 7: 默认 ASK
13. 来源优先级:command source 优先于 project source
14. audit_logger 调用(用 mock)
15. deny state 自动更新
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, Callable, Optional

import pytest

from agent_core.tools.classifier import HaikuClassifier
from agent_core.tools.denial_tracking import DenialTrackingState, clear_all_denial_states
from agent_core.tools.permission_engine import PermissionEngine
from agent_core.tools.permission_hook import HookRegistry, PreToolUseResult, default_hooks
from agent_core.tools.permission_types import (
    AsyncAgentReason,
    ClassifierReason,
    ModeReason,
    OtherReason,
    PermissionBehavior,
    PermissionDecision,
    PermissionMode,
    PermissionRuleSource,
    RuleReason,
    SafetyCheckReason,
    ToolPermissionContext,
)
from agent_core.tools.safety_check import safety_check  # noqa


@pytest.fixture(autouse=True)
def _clear_global_store():
    """每个 test 清空 global deny state"""
    clear_all_denial_states()
    yield
    clear_all_denial_states()


# ────────────────────────────────────────────────────────────────────
# Test helpers
# ────────────────────────────────────────────────────────────────────

@dataclass
class FakeTool:
    """Step 10 前的 duck-typed ToolDef"""
    name: str
    requires_user_interaction: bool = False
    check_permissions: Optional[Callable] = None


def _ctx(
    mode: str = "default",
    always_allow: Optional[dict] = None,
    always_deny: Optional[dict] = None,
    always_ask: Optional[dict] = None,
    no_settings_match: bool = True,
    should_avoid_permission_prompts: bool = False,
) -> ToolPermissionContext:
    """构造 ToolPermissionContext 工厂"""
    return ToolPermissionContext(
        mode=mode,
        always_allow_rules=always_allow or {},
        always_deny_rules=always_deny or {},
        always_ask_rules=always_ask or {},
        no_settings_match=no_settings_match,
        should_avoid_permission_prompts=should_avoid_permission_prompts,
    )


# ────────────────────────────────────────────────────────────────────
# 1a. Step 1a: 全局 deny rule
# ────────────────────────────────────────────────────────────────────

class TestStep1aGlobalDeny:
    def test_deny_rule_matches_tool_returns_deny(self):
        """Bash(rm:*) 在 deny → Bash tool 返 DENY"""
        ctx = _ctx(
            always_deny={"projectSettings": ["Bash(rm:*)"]},
        )
        engine = PermissionEngine(context=ctx)
        decision = engine.check_permissions(FakeTool(name="Bash"), {"command": "rm foo"}, [])
        assert decision.behavior == PermissionBehavior.DENY.value
        assert isinstance(decision.decision_reason, RuleReason)

    def test_deny_rule_other_tool_no_match(self):
        """deny rule 不命中其他 tool"""
        ctx = _ctx(always_deny={"projectSettings": ["Bash(rm:*)"]})
        engine = PermissionEngine(context=ctx)
        decision = engine.check_permissions(FakeTool(name="Read"), {"path": "x.py"}, [])
        # 不应被 deny,继续 pipeline → 默认 ASK
        assert decision.behavior != PermissionBehavior.DENY.value

    def test_deny_rule_no_rule_content_matches_any(self):
        """无 rule_content 的 deny rule 命中任何 input"""
        ctx = _ctx(always_deny={"projectSettings": ["Read"]})
        engine = PermissionEngine(context=ctx)
        decision = engine.check_permissions(FakeTool(name="Read"), {"path": "x.py"}, [])
        assert decision.behavior == PermissionBehavior.DENY.value


# ────────────────────────────────────────────────────────────────────
# 1b. Step 1b: 全局 ask rule
# ────────────────────────────────────────────────────────────────────

class TestStep1bGlobalAsk:
    def test_ask_rule_returns_ask(self):
        """Bash(npm publish:*) → ASK"""
        ctx = _ctx(always_ask={"projectSettings": ["Bash(npm publish:*)"]})
        engine = PermissionEngine(context=ctx)
        decision = engine.check_permissions(
            FakeTool(name="Bash"),
            {"command": "npm publish foo"},
            [],
        )
        assert decision.behavior == PermissionBehavior.ASK.value
        assert isinstance(decision.decision_reason, RuleReason)

    def test_deny_priority_over_ask(self):
        """Step 1a 先于 1b:deny rule 优先"""
        ctx = _ctx(
            always_deny={"projectSettings": ["Bash(rm:*)"]},
            always_ask={"projectSettings": ["Bash(rm:*)"]},  # 也有 ask
        )
        engine = PermissionEngine(context=ctx)
        decision = engine.check_permissions(FakeTool(name="Bash"), {"command": "rm foo"}, [])
        assert decision.behavior == PermissionBehavior.DENY.value


# ────────────────────────────────────────────────────────────────────
# 1c. Step 1c: tool.check_permissions
# ────────────────────────────────────────────────────────────────────

class TestStep1cToolCheckPermissions:
    def test_tool_check_deny_returns_deny(self):
        """tool 自定义 check_permissions 返 DENY → DENY"""
        def check(input_dict, ctx):
            return PermissionDecision(
                behavior=PermissionBehavior.DENY.value,
                decision_reason=SafetyCheckReason(reason="custom deny"),
            )

        tool = FakeTool(name="Read", check_permissions=check)
        engine = PermissionEngine(context=_ctx())
        decision = engine.check_permissions(tool, {"path": "x.py"}, [])
        assert decision.behavior == PermissionBehavior.DENY.value

    def test_tool_check_allow_continues(self):
        """tool 自定义 check_permissions 返 ALLOW → 继续 pipeline"""
        def check(input_dict, ctx):
            return PermissionDecision(behavior=PermissionBehavior.ALLOW.value)

        tool = FakeTool(name="Read", check_permissions=check)
        engine = PermissionEngine(context=_ctx())
        decision = engine.check_permissions(tool, {"path": "x.py"}, [])
        # ALLOW 不在 1c 阻断 → 继续 → 默认 ASK
        assert decision.behavior == PermissionBehavior.ASK.value

    def test_tool_check_exception_falls_through(self):
        """check_permissions 抛异常 → 降级继续"""
        def check(input_dict, ctx):
            raise RuntimeError("boom")

        tool = FakeTool(name="Read", check_permissions=check)
        engine = PermissionEngine(context=_ctx())
        # 不抛异常
        decision = engine.check_permissions(tool, {"path": "x.py"}, [])
        # 继续 pipeline → 默认 ASK
        assert decision.behavior == PermissionBehavior.ASK.value


# ────────────────────────────────────────────────────────────────────
# 1d. Step 1d: requires_user_interaction
# ────────────────────────────────────────────────────────────────────

class TestStep1dRequiresUserInteraction:
    def test_agent_tool_returns_ask(self):
        """requires_user_interaction=True → ASK"""
        tool = FakeTool(name="Agent", requires_user_interaction=True)
        engine = PermissionEngine(context=_ctx())
        decision = engine.check_permissions(tool, {}, [])
        assert decision.behavior == PermissionBehavior.ASK.value
        assert "requires user interaction" in decision.decision_reason.reason


# ────────────────────────────────────────────────────────────────────
# 1e. Step 1e: safety_check
# ────────────────────────────────────────────────────────────────────

class TestStep1eSafetyCheck:
    def test_sensitive_path_returns_ask(self):
        """sensitive path → ASK + SafetyCheckReason"""
        engine = PermissionEngine(context=_ctx())
        decision = engine.check_permissions(
            FakeTool(name="Read"), {"path": ".agent_data/x"}, [],
        )
        assert decision.behavior == PermissionBehavior.ASK.value
        assert isinstance(decision.decision_reason, SafetyCheckReason)

    def test_secret_in_content_returns_ask(self):
        """secret in content → ASK"""
        engine = PermissionEngine(context=_ctx())
        decision = engine.check_permissions(
            FakeTool(name="Write"),
            {"path": "x.py", "content": "sk-ant-api03-abcdefghijklmnopqrstuvwxyz0123456789"},
            [],
        )
        assert decision.behavior == PermissionBehavior.ASK.value
        assert isinstance(decision.decision_reason, SafetyCheckReason)

    def test_safe_input_passes_safety_check(self):
        """safe input 通过 safety_check → 继续 pipeline"""
        engine = PermissionEngine(context=_ctx())
        decision = engine.check_permissions(
            FakeTool(name="Read"), {"path": "src/main.py"}, [],
        )
        # safety 通过 → 进入后续 step → 默认 ASK
        assert decision.behavior == PermissionBehavior.ASK.value
        assert not isinstance(decision.decision_reason, SafetyCheckReason)


# ────────────────────────────────────────────────────────────────────
# 2a. Step 2a: bypass mode
# ────────────────────────────────────────────────────────────────────

class TestStep2aBypassMode:
    def test_bypass_mode_returns_allow(self):
        """bypassPermissions mode → ALLOW + ModeReason"""
        engine = PermissionEngine(context=_ctx(mode="bypassPermissions"))
        decision = engine.check_permissions(FakeTool(name="Bash"), {"command": "rm -rf /"}, [])
        assert decision.behavior == PermissionBehavior.ALLOW.value
        assert isinstance(decision.decision_reason, ModeReason)
        assert decision.decision_reason.mode == "bypassPermissions"

    def test_bypass_mode_does_not_skip_safety_check(self):
        """bypass mode 跳过 safety_check 是 CC 实际行为(M1 简化:跳过)"""
        # 实际上 CC bypass 也跑 safety_check → ASK
        # 但 M1 简化:bypass 直接 ALLOW,跳过 safety_check
        engine = PermissionEngine(context=_ctx(mode="bypassPermissions"))
        decision = engine.check_permissions(
            FakeTool(name="Read"), {"path": ".ssh/id_rsa"}, [],
        )
        # M1: 1a→1b→1c→1d→1e→2a(此步 ALLOW)
        # 真实 CC:1e safety_check 在 2a 前 → ASK
        # M1 文档化差异:Step 1e 在 2a 之前已检查,所以安全路径在 1e 被截
        # 这里测试 .ssh 路径(会被 safety_check 命中 → ASK)
        # 实际期望:ASK(because 1e 在 2a 之前)
        # 让我重新检查代码顺序 ...
        # 代码中 1e 在 2a 之前,所以 .ssh 会被 safety 截到 ASK
        # 但 bypass 是 2a,所以即使 1e 没截到,2a 也会 ALLOW
        # 此 case 1e 应该先截到
        assert decision.behavior == PermissionBehavior.ASK.value


# ────────────────────────────────────────────────────────────────────
# 2b. Step 2b: tool 全局 allow rule
# ────────────────────────────────────────────────────────────────────

class TestStep2bGlobalAllow:
    def test_allow_rule_returns_allow(self):
        """tool 在 always_allow → ALLOW + RuleReason"""
        ctx = _ctx(always_allow={"projectSettings": ["Read"]})
        engine = PermissionEngine(context=ctx)
        decision = engine.check_permissions(FakeTool(name="Read"), {"path": "x.py"}, [])
        assert decision.behavior == PermissionBehavior.ALLOW.value
        assert isinstance(decision.decision_reason, RuleReason)


# ────────────────────────────────────────────────────────────────────
# Step 3: classifier fast-path
# ────────────────────────────────────────────────────────────────────

class TestStep3FastPath:
    def test_accept_edits_read_returns_allow(self):
        """acceptEdits + Read → fast-path ALLOW"""
        engine = PermissionEngine(context=_ctx(mode="acceptEdits"))
        decision = engine.check_permissions(FakeTool(name="Read"), {"path": "x.py"}, [])
        assert decision.behavior == PermissionBehavior.ALLOW.value

    def test_auto_mode_read_returns_allow(self):
        """auto mode + Read → fast-path ALLOW"""
        engine = PermissionEngine(context=_ctx(mode="auto"))
        decision = engine.check_permissions(FakeTool(name="Read"), {"path": "x.py"}, [])
        assert decision.behavior == PermissionBehavior.ALLOW.value

    def test_auto_mode_edit_returns_ask_fallthrough(self):
        """auto mode + Edit(非 allowlist)→ fallthrough → 默认 ASK"""
        engine = PermissionEngine(context=_ctx(mode="auto"))
        decision = engine.check_permissions(FakeTool(name="Edit"), {"path": "x.py"}, [])
        # Edit 不在 allowlist,fast-path miss → 默认 ASK
        assert decision.behavior == PermissionBehavior.ASK.value


# ────────────────────────────────────────────────────────────────────
# Step 4: mode 后处理
# ────────────────────────────────────────────────────────────────────

class TestStep4ModePostProcessing:
    def test_dontask_mode_converts_ask_to_deny(self):
        """dontAsk mode:ASK → DENY"""
        engine = PermissionEngine(context=_ctx(mode="dontAsk"))
        decision = engine.check_permissions(FakeTool(name="Edit"), {"path": "x.py"}, [])
        assert decision.behavior == PermissionBehavior.DENY.value
        assert isinstance(decision.decision_reason, ModeReason)
        assert decision.decision_reason.mode == "dontAsk"

    def test_should_avoid_permission_prompts_auto_denies(self):
        """async agent(should_avoid_permission_prompts=True)→ auto-deny"""
        ctx = _ctx(should_avoid_permission_prompts=True)
        engine = PermissionEngine(context=ctx)
        decision = engine.check_permissions(FakeTool(name="Read"), {"path": "x.py"}, [])
        assert decision.behavior == PermissionBehavior.DENY.value
        assert isinstance(decision.decision_reason, AsyncAgentReason)


# ────────────────────────────────────────────────────────────────────
# Step 5: hook chain
# ────────────────────────────────────────────────────────────────────

class TestStep5HookChain:
    def test_hook_deny_overrides_allow(self):
        """hook DENY 覆盖 global allow"""
        ctx = _ctx(always_allow={"projectSettings": ["Read"]})
        registry = HookRegistry()
        registry.register_hook(
            "PreToolUse", "block-secret",
            lambda n, i, c: PreToolUseResult.deny(reason="hook blocked"),
        )
        engine = PermissionEngine(context=ctx, hook_registry=registry)
        decision = engine.check_permissions(FakeTool(name="Read"), {"path": "x.py"}, [])
        assert decision.behavior == PermissionBehavior.DENY.value
        assert "hook" in decision.decision_reason.reason

    def test_hook_ask_overrides_allow(self):
        """hook ASK 覆盖 global allow"""
        ctx = _ctx(always_allow={"projectSettings": ["Read"]})
        registry = HookRegistry()
        registry.register_hook(
            "PreToolUse", "ask-extra",
            lambda n, i, c: PreToolUseResult.ask(reason="need confirm"),
        )
        engine = PermissionEngine(context=ctx, hook_registry=registry)
        decision = engine.check_permissions(FakeTool(name="Read"), {"path": "x.py"}, [])
        assert decision.behavior == PermissionBehavior.ASK.value

    def test_default_hooks_secret_path(self):
        """default hooks 集成:secret 命中 → ASK"""
        registry = default_hooks()
        engine = PermissionEngine(context=_ctx(), hook_registry=registry)
        decision = engine.check_permissions(
            FakeTool(name="Write"),
            {"path": "x.py", "content": "sk-ant-api03-abcdefghijklmnopqrstuvwxyz0123456789"},
            [],
        )
        assert decision.behavior == PermissionBehavior.ASK.value


# ────────────────────────────────────────────────────────────────────
# Step 6: denial limit
# ────────────────────────────────────────────────────────────────────

class TestStep6DenialLimit:
    def test_consecutive_denial_limit_triggers(self):
        """consecutive=3 → ASK fallback"""
        state = DenialTrackingState(consecutive_denials=3, total_denials=3)
        engine = PermissionEngine(context=_ctx(), denial_state=state)
        decision = engine.check_permissions(FakeTool(name="Read"), {"path": "x.py"}, [])
        assert decision.behavior == PermissionBehavior.ASK.value
        assert isinstance(decision.decision_reason, OtherReason)
        assert "denial_limit" in decision.decision_reason.reason


# ────────────────────────────────────────────────────────────────────
# Step 7: 默认 ASK(passthrough)
# ────────────────────────────────────────────────────────────────────

class TestStep7DefaultAsk:
    def test_no_match_returns_ask(self):
        """无匹配 → 默认 ASK + OtherReason"""
        engine = PermissionEngine(context=_ctx())
        decision = engine.check_permissions(FakeTool(name="Edit"), {"path": "x.py"}, [])
        assert decision.behavior == PermissionBehavior.ASK.value
        assert isinstance(decision.decision_reason, OtherReason)


# ────────────────────────────────────────────────────────────────────
# 来源优先级:高优先级 source 覆盖低优先级
# ────────────────────────────────────────────────────────────────────

class TestSourcePriority:
    def test_deny_in_user_overrides_allow_in_project(self):
        """userSettings deny 优先于 projectSettings allow"""
        ctx = _ctx(
            always_allow={"projectSettings": ["Read"]},
            always_deny={"userSettings": ["Read"]},
        )
        engine = PermissionEngine(context=ctx)
        decision = engine.check_permissions(FakeTool(name="Read"), {"path": "x.py"}, [])
        # Step 1a deny 先检查(高优先级 user 命中) → DENY
        assert decision.behavior == PermissionBehavior.DENY.value


# ────────────────────────────────────────────────────────────────────
# audit_logger mock
# ────────────────────────────────────────────────────────────────────

class TestAuditLogger:
    def test_audit_logger_called(self):
        """audit_logger.log 被调"""
        logged = []

        class MockAudit:
            def log(self, tool_name, tool_input, decision, stage):
                logged.append({
                    "tool_name": tool_name,
                    "behavior": decision.behavior,
                    "stage": stage,
                })

        engine = PermissionEngine(context=_ctx(), audit_logger=MockAudit())
        engine.check_permissions(FakeTool(name="Edit"), {"path": "x.py"}, [])
        assert len(logged) == 1
        assert logged[0]["tool_name"] == "Edit"
        assert logged[0]["stage"].startswith("step_")

    def test_audit_logger_exception_does_not_break(self):
        """audit_logger 抛异常不阻断 decision"""
        class BadAudit:
            def log(self, **kwargs):
                raise RuntimeError("audit fail")

        engine = PermissionEngine(context=_ctx(), audit_logger=BadAudit())
        decision = engine.check_permissions(FakeTool(name="Edit"), {"path": "x.py"}, [])
        # 不抛异常,decision 仍正常返
        assert decision.behavior == PermissionBehavior.ASK.value


# ────────────────────────────────────────────────────────────────────
# deny state 自动更新
# ────────────────────────────────────────────────────────────────────

class TestDenyStateUpdate:
    def test_deny_increments_counter(self):
        """DENY → consecutive +1"""
        engine = PermissionEngine(context=_ctx())
        before = engine.get_denial_state()
        engine.check_permissions(FakeTool(name="Bash"), {"command": "rm -rf /"}, [])
        # 注:Bash tool 不在 deny rule → 默认 ASK → 不增
        # 让我们用 sensitive path 来触发 DENY?
        # sensitive path 是 ASK(Step 1e)
        # 实际能触发 DENY 的:.ssh(但 Read 也只是 ASK)
        # 直接构造一个 deny rule
        ctx = _ctx(always_deny={"projectSettings": ["Edit"]})
        engine2 = PermissionEngine(context=ctx)
        before2 = engine2.get_denial_state()
        engine2.check_permissions(FakeTool(name="Edit"), {"path": "x.py"}, [])
        after2 = engine2.get_denial_state()
        assert after2.consecutive_denials == before2.consecutive_denials + 1
        assert after2.total_denials == before2.total_denials + 1

    def test_allow_resets_consecutive(self):
        """ALLOW → consecutive=0"""
        ctx = _ctx(always_allow={"projectSettings": ["Read"]})
        state = DenialTrackingState(consecutive_denials=2, total_denials=5)
        engine = PermissionEngine(context=ctx, denial_state=state)
        engine.check_permissions(FakeTool(name="Read"), {"path": "x.py"}, [])
        after = engine.get_denial_state()
        assert after.consecutive_denials == 0
        assert after.total_denials == 5  # total 不变


# ────────────────────────────────────────────────────────────────────
# Classifier 集成(auto mode)
# ────────────────────────────────────────────────────────────────────

class TestClassifierIntegration:
    def test_auto_mode_classifier_deny(self, monkeypatch):
        """auto mode + classifier 返 deny → DENY"""
        monkeypatch.setenv("TRANSCRIPT_CLASSIFIER_ENABLED", "true")

        def mock_llm(messages, model, **kwargs):
            import json
            return json.dumps({"should_block": True, "reason": "dangerous"})

        classifier = HaikuClassifier(llm_callable=mock_llm)
        ctx = _ctx(mode="auto", no_settings_match=True)
        engine = PermissionEngine(
            context=ctx, classifier=classifier, provider="anthropic",
        )
        decision = engine.check_permissions(
            FakeTool(name="Bash"), {"command": "rm -rf /"}, [{"role": "user", "content": "x"}],
        )
        assert decision.behavior == PermissionBehavior.DENY.value
        assert isinstance(decision.decision_reason, ClassifierReason)

    def test_auto_mode_classifier_allow(self, monkeypatch):
        """auto mode + classifier 返 allow → ALLOW"""
        monkeypatch.setenv("TRANSCRIPT_CLASSIFIER_ENABLED", "true")

        def mock_llm(messages, model, **kwargs):
            import json
            return json.dumps({"should_block": False, "reason": "ok"})

        classifier = HaikuClassifier(llm_callable=mock_llm)
        ctx = _ctx(mode="auto", no_settings_match=True)
        engine = PermissionEngine(
            context=ctx, classifier=classifier, provider="anthropic",
        )
        decision = engine.check_permissions(
            FakeTool(name="Bash"), {"command": "ls"}, [{"role": "user", "content": "x"}],
        )
        # classifier allow → ALLOW
        assert decision.behavior == PermissionBehavior.ALLOW.value
        assert isinstance(decision.decision_reason, ClassifierReason)

    def test_auto_mode_classifier_unavailable_falls_through(self, monkeypatch):
        """auto mode + classifier unavailable → 默认 ASK"""
        monkeypatch.setenv("TRANSCRIPT_CLASSIFIER_ENABLED", "true")
        # 无 llm_callable → classifier unavailable
        ctx = _ctx(mode="auto", no_settings_match=True)
        engine = PermissionEngine(
            context=ctx, classifier=HaikuClassifier(), provider="anthropic",
        )
        decision = engine.check_permissions(
            FakeTool(name="Bash"), {"command": "ls"}, [],
        )
        # unavailable → fallthrough → 默认 ASK
        assert decision.behavior == PermissionBehavior.ASK.value


# ────────────────────────────────────────────────────────────────────
# 集成:多 step 协同
# ────────────────────────────────────────────────────────────────────

class TestEndToEnd:
    def test_realistic_bash_deny(self):
        """真实场景:Bash rm → deny rule → DENY"""
        ctx = _ctx(always_deny={"projectSettings": ["Bash(rm:*)"]})
        engine = PermissionEngine(context=ctx)
        decision = engine.check_permissions(FakeTool(name="Bash"), {"command": "rm -rf /"}, [])
        assert decision.behavior == PermissionBehavior.DENY.value

    def test_realistic_read_safety_ask(self):
        """真实场景:Read .ssh → safety_check → ASK"""
        ctx = _ctx()
        engine = PermissionEngine(context=_ctx())
        decision = engine.check_permissions(FakeTool(name="Read"), {"path": ".ssh/id_rsa"}, [])
        assert decision.behavior == PermissionBehavior.ASK.value
        assert isinstance(decision.decision_reason, SafetyCheckReason)

    def test_realistic_bypass_mode(self):
        """真实场景:bypass mode → 所有 → ALLOW"""
        ctx = _ctx(mode="bypassPermissions")
        engine = PermissionEngine(context=ctx)
        decision = engine.check_permissions(FakeTool(name="Bash"), {"command": "rm -rf /"}, [])
        assert decision.behavior == PermissionBehavior.ALLOW.value

    def test_realistic_default_mode_edit(self):
        """真实场景:default mode + Edit → 默认 ASK"""
        engine = PermissionEngine(context=_ctx())
        decision = engine.check_permissions(FakeTool(name="Edit"), {"path": "x.py"}, [])
        assert decision.behavior == PermissionBehavior.ASK.value
