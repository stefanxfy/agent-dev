"""
PermissionRequest hook 测试(M3 Task 2)

覆盖:
1. HookRegistry.run_permission_request(短路 / 异常隔离 / updated_input merge)
2. make_webhook_permission_request_hook(requests mock)
3. ReactAgent._ask_user_permission 集成(hook allow/deny 跳过 UI)
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from agent_core.tools.permission_hook import (
    HookRegistry,
    PermissionRequestResult,
    PreToolUseResult,
    make_webhook_permission_request_hook,
)
from agent_core.tools.permission_types import ToolPermissionContext


def _ctx():
    return ToolPermissionContext()


# ────────────────────────────────────────────────────────────────────
# PermissionRequestResult
# ────────────────────────────────────────────────────────────────────

class TestPermissionRequestResult:
    def test_default_no_decision(self):
        r = PermissionRequestResult()
        assert r.decision is None
        assert r.has_decision is False

    def test_has_decision_true_when_set(self):
        r = PermissionRequestResult(decision="allow")
        assert r.has_decision is True

    def test_deny_has_decision(self):
        r = PermissionRequestResult(decision="deny", reason="unsafe")
        assert r.has_decision is True
        assert r.reason == "unsafe"


# ────────────────────────────────────────────────────────────────────
# HookRegistry.run_permission_request
# ────────────────────────────────────────────────────────────────────

class TestRunPermissionRequest:
    def test_no_hooks_returns_no_decision(self):
        reg = HookRegistry()
        result = reg.run_permission_request("Bash", {"command": "ls"}, _ctx())
        assert result.has_decision is False

    def test_hook_returns_allow_short_circuits(self):
        reg = HookRegistry()
        reg.register_hook(
            "PermissionRequest", "hook1",
            lambda name, inp, ctx: PermissionRequestResult(decision="allow", reason="ok"),
        )
        result = reg.run_permission_request("Bash", {"command": "ls"}, _ctx())
        assert result.decision == "allow"
        assert result.reason == "ok"

    def test_hook_returns_deny_short_circuits(self):
        reg = HookRegistry()
        reg.register_hook(
            "PermissionRequest", "hook1",
            lambda name, inp, ctx: PermissionRequestResult(decision="deny", reason="unsafe"),
        )
        result = reg.run_permission_request("Bash", {"command": "rm"}, _ctx())
        assert result.decision == "deny"

    def test_first_deciding_hook_wins(self):
        # 两个 hook,第一个 allow,第二个 deny → allow 胜(短路)
        reg = HookRegistry()
        reg.register_hook(
            "PermissionRequest", "first",
            lambda name, inp, ctx: PermissionRequestResult(decision="allow"),
        )
        reg.register_hook(
            "PermissionRequest", "second",
            lambda name, inp, ctx: PermissionRequestResult(decision="deny"),
        )
        result = reg.run_permission_request("Bash", {"command": "ls"}, _ctx())
        assert result.decision == "allow"

    def test_no_decision_hook_falls_through(self):
        # hook 返 None(decision=None)→ 走默认
        reg = HookRegistry()
        reg.register_hook(
            "PermissionRequest", "silent",
            lambda name, inp, ctx: PermissionRequestResult(),
        )
        result = reg.run_permission_request("Bash", {"command": "ls"}, _ctx())
        assert result.has_decision is False

    def test_hook_exception_skipped(self):
        reg = HookRegistry()

        def bad_hook(name, inp, ctx):
            raise RuntimeError("boom")

        reg.register_hook("PermissionRequest", "bad", bad_hook)
        # 不应抛,返无决策
        result = reg.run_permission_request("Bash", {"command": "ls"}, _ctx())
        assert result.has_decision is False

    def test_updated_input_merged(self):
        reg = HookRegistry()
        reg.register_hook(
            "PermissionRequest", "modifier",
            lambda name, inp, ctx: PermissionRequestResult(
                decision="allow", updated_input={"modified": True},
            ),
        )
        result = reg.run_permission_request(
            "Bash", {"command": "ls"}, _ctx(),
        )
        assert result.decision == "allow"
        assert result.updated_input is not None
        assert result.updated_input.get("modified") is True
        # 原 input 保留
        assert result.updated_input.get("command") == "ls"

    def test_compatible_with_pretooluse_result(self):
        # 旧签名 PreToolUseResult 也应兼容(decision=None 时)
        reg = HookRegistry()
        reg.register_hook(
            "PermissionRequest", "legacy",
            lambda name, inp, ctx: PreToolUseResult.allow(),
        )
        result = reg.run_permission_request("Bash", {"command": "ls"}, _ctx())
        # PreToolUseResult.allow 的 behavior="allow" → 应被识别
        assert result.decision == "allow"

    def test_only_permission_request_event_hooks_run(self):
        # PreToolUse hook 不应被 run_permission_request 调到
        reg = HookRegistry()
        called = {"v": False}
        reg.register_hook(
            "PreToolUse", "pretool",
            lambda name, inp, ctx: called.__setitem__("v", True) or PreToolUseResult.allow(),
        )
        reg.run_permission_request("Bash", {"command": "ls"}, _ctx())
        assert called["v"] is False


# ────────────────────────────────────────────────────────────────────
# make_webhook_permission_request_hook
# ────────────────────────────────────────────────────────────────────

class TestWebhookHook:
    def test_webhook_success_allow(self):
        hook = make_webhook_permission_request_hook("http://localhost:9999/decide")
        mock_resp = MagicMock()
        mock_resp.raise_for_status.return_value = None
        mock_resp.json.return_value = {"decision": "allow", "reason": "safe"}
        with patch("requests.post", return_value=mock_resp):
            result = hook("Bash", {"command": "ls"}, _ctx())
        assert result.decision == "allow"
        assert result.reason == "safe"

    def test_webhook_success_deny(self):
        hook = make_webhook_permission_request_hook("http://localhost:9999/decide")
        mock_resp = MagicMock()
        mock_resp.raise_for_status.return_value = None
        mock_resp.json.return_value = {"decision": "deny", "reason": "dangerous"}
        with patch("requests.post", return_value=mock_resp):
            result = hook("Bash", {"command": "rm"}, _ctx())
        assert result.decision == "deny"

    def test_webhook_failure_returns_none(self):
        # requests 抛异常 → decision=None(走默认 UI)
        hook = make_webhook_permission_request_hook("http://localhost:9999/decide")
        with patch("requests.post", side_effect=ConnectionError("refused")):
            result = hook("Bash", {"command": "ls"}, _ctx())
        assert result.has_decision is False

    def test_webhook_invalid_response_returns_none(self):
        # decision 非 allow/deny/ask → None
        hook = make_webhook_permission_request_hook("http://localhost:9999/decide")
        mock_resp = MagicMock()
        mock_resp.raise_for_status.return_value = None
        mock_resp.json.return_value = {"decision": "bogus"}
        with patch("requests.post", return_value=mock_resp):
            result = hook("Bash", {"command": "ls"}, _ctx())
        assert result.has_decision is False

    def test_webhook_timeout_returns_none(self):
        import requests as _requests
        hook = make_webhook_permission_request_hook("http://localhost:9999/decide")
        with patch("requests.post", side_effect=_requests.exceptions.Timeout("timed out")):
            result = hook("Bash", {"command": "ls"}, _ctx())
        assert result.has_decision is False

    def test_webhook_posts_tool_info(self):
        # 验证 webhook 收到 tool_name + tool_input
        hook = make_webhook_permission_request_hook("http://example.com/hook")
        mock_resp = MagicMock()
        mock_resp.raise_for_status.return_value = None
        mock_resp.json.return_value = {"decision": "allow"}
        with patch("requests.post", return_value=mock_resp) as mock_post:
            hook("Bash", {"command": "ls -la"}, _ctx())
        mock_post.assert_called_once()
        call_kwargs = mock_post.call_args
        # json 参数含 tool_name + tool_input
        payload = call_kwargs.kwargs.get("json") or call_kwargs[1].get("json")
        assert payload["tool_name"] == "Bash"
        assert payload["tool_input"]["command"] == "ls -la"


# ────────────────────────────────────────────────────────────────────
# ReactAgent._ask_user_permission 集成
# ────────────────────────────────────────────────────────────────────

class TestAskUserPermissionIntegration:
    def _make_agent(self, hook_registry=None):
        from agent_core.agent_core import ReactAgent
        agent = ReactAgent.__new__(ReactAgent)
        engine = MagicMock()
        engine.context = _ctx()
        engine.hook_registry = hook_registry
        agent.permission_engine = engine
        agent.auto_allow_ask = False
        agent._pending_permission_request = None
        agent._permission_resolved = None
        return agent

    def test_hook_allow_skips_ui(self):
        # hook allow → 直接 allow,不等 Event.wait
        reg = HookRegistry()
        reg.register_hook(
            "PermissionRequest", "h",
            lambda n, i, c: PermissionRequestResult(decision="allow"),
        )
        agent = self._make_agent(reg)
        decision = SimpleNamespace(
            decision_reason=SimpleNamespace(reason="ask"),
            message="ask",
        )
        allowed, err, _ = agent._ask_user_permission("Bash", {"command": "ls"}, decision)
        assert allowed is True
        assert err is None

    def test_hook_deny_skips_ui(self):
        reg = HookRegistry()
        reg.register_hook(
            "PermissionRequest", "h",
            lambda n, i, c: PermissionRequestResult(decision="deny"),
        )
        agent = self._make_agent(reg)
        decision = SimpleNamespace(
            decision_reason=SimpleNamespace(reason="ask"),
            message="ask",
        )
        allowed, err, _ = agent._ask_user_permission("Bash", {"command": "ls"}, decision)
        assert allowed is False
        assert "PermissionRequest hook" in err

    def test_no_decision_falls_through_to_ui(self):
        # hook 未决策 → 走 UI(Event.wait 0.1s 超时 → deny)
        reg = HookRegistry()
        reg.register_hook(
            "PermissionRequest", "silent",
            lambda n, i, c: PermissionRequestResult(),
        )
        agent = self._make_agent(reg)
        decision = SimpleNamespace(
            decision_reason=SimpleNamespace(reason="ask"),
            message="ask",
        )
        allowed, err, _ = agent._ask_user_permission("Bash", {"command": "ls"}, decision)
        # UI 超时 → deny
        assert allowed is False
        assert "timed out" in err or "user" in err.lower()

    def test_hook_exception_falls_through(self):
        # hook 异常 → 走 UI
        reg = HookRegistry()
        reg.register_hook(
            "PermissionRequest", "bad",
            lambda n, i, c: (_ for _ in ()).throw(RuntimeError("boom")),
        )
        agent = self._make_agent(reg)
        decision = SimpleNamespace(
            decision_reason=SimpleNamespace(reason="ask"),
            message="ask",
        )
        # 不应抛,走 UI 超时 deny
        allowed, err, _ = agent._ask_user_permission("Bash", {"command": "ls"}, decision)
        assert allowed is False

    def test_no_hook_registry_falls_through(self):
        # permission_engine 无 hook_registry → 走 UI
        agent = self._make_agent(hook_registry=None)
        decision = SimpleNamespace(
            decision_reason=SimpleNamespace(reason="ask"),
            message="ask",
        )
        allowed, _, _ = agent._ask_user_permission("Bash", {"command": "ls"}, decision)
        assert allowed is False  # UI 超时 deny

    def test_no_engine_falls_through(self):
        from agent_core.agent_core import ReactAgent
        agent = ReactAgent.__new__(ReactAgent)
        agent.permission_engine = None
        agent._pending_permission_request = None
        agent._permission_resolved = None
        decision = SimpleNamespace(
            decision_reason=SimpleNamespace(reason="ask"),
            message="ask",
        )
        # 无 engine → hook 返 None → 走 UI(Event.wait 超时 deny)
        allowed, _, _ = agent._ask_user_permission("Bash", {"command": "ls"}, decision)
        assert allowed is False
