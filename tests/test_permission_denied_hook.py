"""
PermissionDenied hook 测试(M3 Task 3)

覆盖:
1. PermissionDeniedResult dataclass
2. HookRegistry.run_permission_denied(聚合 / 异常隔离 / 兼容签名)
3. make_retry_hint_denied_hook factory
4. ReactAgent._check_tool_permission deny 集成(retry hint 追加到 err)
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from agent_core.tools.permission_hook import (
    HookRegistry,
    PermissionDeniedResult,
    PreToolUseResult,
    make_retry_hint_denied_hook,
)
from agent_core.tools.permission_types import (
    OtherReason,
    PermissionBehavior,
    PermissionDecision,
    ToolPermissionContext,
)


def _ctx():
    return ToolPermissionContext()


def _make_deny_decision(reason="test deny"):
    return PermissionDecision(
        behavior=PermissionBehavior.DENY.value,
        decision_reason=OtherReason(reason=reason),
    )


# ────────────────────────────────────────────────────────────────────
# PermissionDeniedResult
# ────────────────────────────────────────────────────────────────────

class TestPermissionDeniedResult:
    def test_default_empty(self):
        r = PermissionDeniedResult()
        assert r.retry_prompt is None
        assert r.has_content is False

    def test_has_content_with_retry_prompt(self):
        r = PermissionDeniedResult(retry_prompt="try safer way")
        assert r.has_content is True

    def test_has_content_with_notify(self):
        r = PermissionDeniedResult(notify_message="notified")
        assert r.has_content is True

    def test_has_content_with_additional_context(self):
        r = PermissionDeniedResult(additional_context="ctx")
        assert r.has_content is True

    def test_all_fields(self):
        r = PermissionDeniedResult(
            retry_prompt="retry", notify_message="notify", additional_context="ctx",
        )
        assert r.retry_prompt == "retry"
        assert r.notify_message == "notify"
        assert r.additional_context == "ctx"


# ────────────────────────────────────────────────────────────────────
# HookRegistry.run_permission_denied
# ────────────────────────────────────────────────────────────────────

class TestRunPermissionDenied:
    def test_no_hooks_returns_empty(self):
        reg = HookRegistry()
        result = reg.run_permission_denied(
            "Bash", {"command": "rm"}, _ctx(), _make_deny_decision(),
        )
        assert result.has_content is False

    def test_hook_retry_prompt_returned(self):
        reg = HookRegistry()
        reg.register_hook(
            "PermissionDenied", "h",
            lambda n, i, c, d: PermissionDeniedResult(retry_prompt="safer way"),
        )
        result = reg.run_permission_denied(
            "Bash", {"command": "rm"}, _ctx(), _make_deny_decision(),
        )
        assert result.retry_prompt == "safer way"

    def test_hook_retry_prompt_aggregated(self):
        # 两个 hook 各返 retry_prompt → 拼接
        reg = HookRegistry()
        reg.register_hook(
            "PermissionDenied", "h1",
            lambda n, i, c, d: PermissionDeniedResult(retry_prompt="hint1"),
        )
        reg.register_hook(
            "PermissionDenied", "h2",
            lambda n, i, c, d: PermissionDeniedResult(retry_prompt="hint2"),
        )
        result = reg.run_permission_denied(
            "Bash", {"command": "rm"}, _ctx(), _make_deny_decision(),
        )
        assert "hint1" in result.retry_prompt
        assert "hint2" in result.retry_prompt

    def test_hook_notify_message_aggregated(self):
        reg = HookRegistry()
        reg.register_hook(
            "PermissionDenied", "h1",
            lambda n, i, c, d: PermissionDeniedResult(notify_message="notify1"),
        )
        reg.register_hook(
            "PermissionDenied", "h2",
            lambda n, i, c, d: PermissionDeniedResult(notify_message="notify2"),
        )
        result = reg.run_permission_denied(
            "Bash", {"command": "rm"}, _ctx(), _make_deny_decision(),
        )
        assert "notify1" in result.notify_message
        assert "notify2" in result.notify_message

    def test_hook_additional_context_aggregated(self):
        reg = HookRegistry()
        reg.register_hook(
            "PermissionDenied", "h",
            lambda n, i, c, d: PermissionDeniedResult(additional_context="extra ctx"),
        )
        result = reg.run_permission_denied(
            "Bash", {"command": "rm"}, _ctx(), _make_deny_decision(),
        )
        assert "extra ctx" in result.additional_context

    def test_hook_exception_skipped(self):
        reg = HookRegistry()

        def bad_hook(n, i, c, d):
            raise RuntimeError("boom")

        reg.register_hook("PermissionDenied", "bad", bad_hook)
        # 不应抛
        result = reg.run_permission_denied(
            "Bash", {"command": "rm"}, _ctx(), _make_deny_decision(),
        )
        assert result.has_content is False

    def test_aggregated_empty_when_all_hooks_silent(self):
        reg = HookRegistry()
        reg.register_hook(
            "PermissionDenied", "silent",
            lambda n, i, c, d: PermissionDeniedResult(),
        )
        result = reg.run_permission_denied(
            "Bash", {"command": "rm"}, _ctx(), _make_deny_decision(),
        )
        assert result.has_content is False
        assert result.retry_prompt is None

    def test_compatible_with_3_arg_signature(self):
        # 旧签名(3 参,无 decision)也应兼容
        reg = HookRegistry()
        reg.register_hook(
            "PermissionDenied", "legacy",
            lambda n, i, c: PermissionDeniedResult(retry_prompt="legacy hint"),
        )
        result = reg.run_permission_denied(
            "Bash", {"command": "rm"}, _ctx(), _make_deny_decision(),
        )
        assert result.retry_prompt == "legacy hint"

    def test_compatible_with_pretooluse_result(self):
        # PreToolUseResult 也有 additional_context/reason,应被兼容读取
        reg = HookRegistry()
        reg.register_hook(
            "PermissionDenied", "ptu",
            lambda n, i, c, d: PreToolUseResult.deny(reason="ptu reason"),
        )
        result = reg.run_permission_denied(
            "Bash", {"command": "rm"}, _ctx(), _make_deny_decision(),
        )
        # PreToolUseResult.reason 被当作 notify_message
        assert "ptu reason" in (result.notify_message or "")

    def test_only_permission_denied_event_hooks_run(self):
        # PreToolUse / PermissionRequest hook 不应被调到
        reg = HookRegistry()
        called = {"v": False}
        reg.register_hook(
            "PreToolUse", "pretool",
            lambda n, i, c: called.__setitem__("v", True) or PreToolUseResult.allow(),
        )
        reg.run_permission_denied(
            "Bash", {"command": "rm"}, _ctx(), _make_deny_decision(),
        )
        assert called["v"] is False


# ────────────────────────────────────────────────────────────────────
# make_retry_hint_denied_hook
# ────────────────────────────────────────────────────────────────────

class TestRetryHintFactory:
    def test_factory_returns_callable(self):
        hook = make_retry_hint_denied_hook()
        assert callable(hook)

    def test_hook_returns_retry_prompt(self):
        hook = make_retry_hint_denied_hook()
        result = hook("Bash", {"command": "rm"}, _ctx(), _make_deny_decision())
        assert result.retry_prompt is not None
        assert "Bash" in result.retry_prompt

    def test_hook_returns_notify_message(self):
        hook = make_retry_hint_denied_hook()
        result = hook("Bash", {"command": "rm"}, _ctx(), _make_deny_decision())
        assert result.notify_message is not None
        assert "Bash" in result.notify_message

    def test_hook_via_registry(self):
        reg = HookRegistry()
        reg.register_hook(
            "PermissionDenied", "retry", make_retry_hint_denied_hook(),
        )
        result = reg.run_permission_denied(
            "Bash", {"command": "rm"}, _ctx(), _make_deny_decision(),
        )
        assert result.has_content is True
        assert "更安全" in result.retry_prompt or "放行" in result.retry_prompt


# ────────────────────────────────────────────────────────────────────
# ReactAgent._check_tool_permission deny 集成
# ────────────────────────────────────────────────────────────────────

class TestCheckPermissionDenyIntegration:
    def _make_agent(self, hook_registry=None):
        from agent_core.agent_core import ReactAgent
        agent = ReactAgent.__new__(ReactAgent)
        engine = MagicMock()
        engine.context = _ctx()
        engine.hook_registry = hook_registry
        agent.permission_engine = engine
        agent.tools = MagicMock()
        agent.messages = []
        agent.auto_allow_ask = True
        return agent

    def _bash_tool(self):
        return SimpleNamespace(
            name="Bash", check_permissions=None, requires_user_interaction=False,
        )

    def test_deny_with_hook_appends_retry_hint(self):
        reg = HookRegistry()
        reg.register_hook(
            "PermissionDenied", "retry", make_retry_hint_denied_hook(),
        )
        # mock engine 返 DENY
        agent = self._make_agent(reg)
        agent.permission_engine.check_permissions.return_value = _make_deny_decision()
        allowed, err, _ = agent._check_tool_permission("Bash", {"command": "rm"})
        assert allowed is False
        assert "Retry hint" in err
        assert "更安全" in err or "放行" in err

    def test_deny_without_hook_no_retry_hint(self):
        # 无 hook → err 不含 "Retry hint"
        agent = self._make_agent(HookRegistry())
        agent.permission_engine.check_permissions.return_value = _make_deny_decision()
        allowed, err, _ = agent._check_tool_permission("Bash", {"command": "rm"})
        assert allowed is False
        assert "Retry hint" not in err
        assert "Permission denied" in err

    def test_deny_hook_exception_no_break(self):
        # hook 异常 → err 仍返(不含 hint)
        agent = self._make_agent(None)  # 无 hook_registry
        agent.permission_engine.check_permissions.return_value = _make_deny_decision()
        allowed, err, _ = agent._check_tool_permission("Bash", {"command": "rm"})
        assert allowed is False
        assert "Permission denied" in err

    def test_deny_reason_still_present_with_hook(self):
        # hook 追加 hint,但原 deny reason 仍在
        reg = HookRegistry()
        reg.register_hook(
            "PermissionDenied", "retry", make_retry_hint_denied_hook(),
        )
        agent = self._make_agent(reg)
        agent.permission_engine.check_permissions.return_value = _make_deny_decision(
            reason="deny rule: Bash(rm:*)",
        )
        _, err, _ = agent._check_tool_permission("Bash", {"command": "rm"})
        assert "deny rule: Bash(rm:*)" in err
        assert "Retry hint" in err
