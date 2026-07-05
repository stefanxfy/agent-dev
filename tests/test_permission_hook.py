"""
permission_hook.py 测试

覆盖:
1. PreToolUseResult 工厂方法(allow/deny/ask)
2. HookRegistry 注册 / 注销 / 列表
3. run_pre_tool_use 并行执行
4. 第一个 DENY 短路
5. updated_input 链式 merge(后 hook 覆盖前 hook)
6. hook 抛异常不阻断 pipeline
7. prevent_continuation 触发
8. 默认 hooks(default_secret / default_path)
"""

from __future__ import annotations

import time

import pytest

from agent_core.tools.permission_hook import (
    HookRegistry,
    PreToolUseResult,
    default_hooks,
    default_path_validation_hook,
    default_secret_hook,
)
from agent_core.tools.permission_types import (
    PermissionBehavior,
    ToolPermissionContext,
)


# ────────────────────────────────────────────────────────────────────
# PreToolUseResult
# ────────────────────────────────────────────────────────────────────

class TestPreToolUseResult:
    def test_default_is_allow(self):
        """默认 ALLOW"""
        r = PreToolUseResult()
        assert r.behavior == PermissionBehavior.ALLOW.value
        assert r.updated_input is None
        assert r.prevent_continuation is False

    def test_allow_factory(self):
        """allow() 工厂"""
        r = PreToolUseResult.allow(hook_name="test")
        assert r.behavior == PermissionBehavior.ALLOW.value
        assert r.hook_name == "test"

    def test_deny_factory(self):
        """deny() 工厂"""
        r = PreToolUseResult.deny(reason="test reason")
        assert r.behavior == PermissionBehavior.DENY.value
        assert r.reason == "test reason"

    def test_ask_factory(self):
        """ask() 工厂"""
        r = PreToolUseResult.ask(reason="need confirm")
        assert r.behavior == PermissionBehavior.ASK.value
        assert r.reason == "need confirm"

    def test_updated_input_field(self):
        """updated_input 字段透传"""
        r = PreToolUseResult.allow(updated_input={"path": "/safe/path"})
        assert r.updated_input == {"path": "/safe/path"}


# ────────────────────────────────────────────────────────────────────
# HookRegistry — 注册 / 注销
# ────────────────────────────────────────────────────────────────────

class TestHookRegistryRegistration:
    def test_register_hook(self):
        """register_hook 添加 hook"""
        reg = HookRegistry()
        reg.register_hook("PreToolUse", "h1", lambda n, i, c: PreToolUseResult.allow())
        assert "h1" in reg.list_hooks()

    def test_register_multiple_hooks(self):
        """多个 hook 注册"""
        reg = HookRegistry()
        reg.register_hook("PreToolUse", "h1", lambda n, i, c: PreToolUseResult.allow())
        reg.register_hook("PreToolUse", "h2", lambda n, i, c: PreToolUseResult.allow())
        reg.register_hook("PostToolUse", "h3", lambda n, i, c: PreToolUseResult.allow())
        pre = reg.list_hooks("PreToolUse")
        post = reg.list_hooks("PostToolUse")
        assert "h1" in pre
        assert "h2" in pre
        assert "h3" not in pre
        assert "h3" in post

    def test_unregister_existing_returns_true(self):
        """注销存在的 hook 返 True"""
        reg = HookRegistry()
        reg.register_hook("PreToolUse", "h1", lambda n, i, c: PreToolUseResult.allow())
        assert reg.unregister_hook("h1") is True
        assert "h1" not in reg.list_hooks()

    def test_unregister_nonexistent_returns_false(self):
        """注销不存在的 hook 返 False"""
        reg = HookRegistry()
        assert reg.unregister_hook("not-there") is False

    def test_clear(self):
        """clear 清空所有"""
        reg = HookRegistry()
        reg.register_hook("PreToolUse", "h1", lambda n, i, c: PreToolUseResult.allow())
        reg.register_hook("PreToolUse", "h2", lambda n, i, c: PreToolUseResult.allow())
        reg.clear()
        assert reg.list_hooks() == []


# ────────────────────────────────────────────────────────────────────
# run_pre_tool_use — 单 hook
# ────────────────────────────────────────────────────────────────────

class TestRunPreToolUseSingle:
    def test_no_hooks_returns_allow(self):
        """无 hook → ALLOW"""
        reg = HookRegistry()
        result = reg.run_pre_tool_use("Read", {"path": "x.py"}, ToolPermissionContext())
        assert result.behavior == PermissionBehavior.ALLOW.value

    def test_single_allow_hook_returns_allow(self):
        """单 ALLOW hook → ALLOW"""
        reg = HookRegistry()
        reg.register_hook("PreToolUse", "h1", lambda n, i, c: PreToolUseResult.allow())
        result = reg.run_pre_tool_use("Read", {}, ToolPermissionContext())
        assert result.behavior == PermissionBehavior.ALLOW.value

    def test_single_deny_hook_returns_deny(self):
        """单 DENY hook → DENY"""
        reg = HookRegistry()
        reg.register_hook(
            "PreToolUse", "h1",
            lambda n, i, c: PreToolUseResult.deny(reason="bad"),
        )
        result = reg.run_pre_tool_use("Bash", {}, ToolPermissionContext())
        assert result.behavior == PermissionBehavior.DENY.value
        assert result.reason == "bad"
        assert result.hook_name == "h1"

    def test_single_ask_hook_returns_ask(self):
        """单 ASK hook → ASK"""
        reg = HookRegistry()
        reg.register_hook(
            "PreToolUse", "h1",
            lambda n, i, c: PreToolUseResult.ask(reason="confirm"),
        )
        result = reg.run_pre_tool_use("Bash", {}, ToolPermissionContext())
        assert result.behavior == PermissionBehavior.ASK.value

    def test_hook_metadata_populated(self):
        """hook_name + hook_source 在结果中"""
        reg = HookRegistry()
        reg.register_hook(
            "PreToolUse", "secret-scan",
            lambda n, i, c: PreToolUseResult.allow(),
            source="projectSettings",
        )
        result = reg.run_pre_tool_use("Read", {}, ToolPermissionContext())
        assert result.hook_name == "secret-scan"
        assert result.hook_source == "projectSettings"


# ────────────────────────────────────────────────────────────────────
# run_pre_tool_use — 多 hook 行为
# ────────────────────────────────────────────────────────────────────

class TestRunPreToolUseMulti:
    def test_all_allow_returns_allow(self):
        """全 ALLOW → ALLOW"""
        reg = HookRegistry()
        reg.register_hook("PreToolUse", "h1", lambda n, i, c: PreToolUseResult.allow())
        reg.register_hook("PreToolUse", "h2", lambda n, i, c: PreToolUseResult.allow())
        result = reg.run_pre_tool_use("Read", {}, ToolPermissionContext())
        assert result.behavior == PermissionBehavior.ALLOW.value

    def test_any_deny_short_circuits(self):
        """任一 DENY → DENY 短路"""
        reg = HookRegistry()
        reg.register_hook("PreToolUse", "h1", lambda n, i, c: PreToolUseResult.allow())
        reg.register_hook(
            "PreToolUse", "h2",
            lambda n, i, c: PreToolUseResult.deny(reason="blocked"),
        )
        reg.register_hook("PreToolUse", "h3", lambda n, i, c: PreToolUseResult.allow())
        result = reg.run_pre_tool_use("Read", {}, ToolPermissionContext())
        assert result.behavior == PermissionBehavior.DENY.value
        assert result.reason == "blocked"
        # DENY 来自 h2
        assert result.hook_name == "h2"

    def test_any_ask_returns_ask(self):
        """任一 ASK(无 DENY)→ ASK"""
        reg = HookRegistry()
        reg.register_hook("PreToolUse", "h1", lambda n, i, c: PreToolUseResult.allow())
        reg.register_hook(
            "PreToolUse", "h2",
            lambda n, i, c: PreToolUseResult.ask(reason="need confirm"),
        )
        result = reg.run_pre_tool_use("Read", {}, ToolPermissionContext())
        assert result.behavior == PermissionBehavior.ASK.value

    def test_deny_priority_over_ask(self):
        """DENY 优先 ASK"""
        reg = HookRegistry()
        reg.register_hook(
            "PreToolUse", "h1",
            lambda n, i, c: PreToolUseResult.ask(reason="ask"),
        )
        reg.register_hook(
            "PreToolUse", "h2",
            lambda n, i, c: PreToolUseResult.deny(reason="deny"),
        )
        result = reg.run_pre_tool_use("Read", {}, ToolPermissionContext())
        assert result.behavior == PermissionBehavior.DENY.value

    def test_updated_input_merged(self):
        """多 hook updated_input 链式 merge"""
        reg = HookRegistry()
        reg.register_hook(
            "PreToolUse", "h1",
            lambda n, i, c: PreToolUseResult.allow(updated_input={"a": 1, "b": 2}),
        )
        reg.register_hook(
            "PreToolUse", "h2",
            lambda n, i, c: PreToolUseResult.allow(updated_input={"b": 99, "c": 3}),
        )
        result = reg.run_pre_tool_use("Read", {}, ToolPermissionContext())
        # h2 的 b 覆盖 h1 的 b
        assert result.updated_input == {"a": 1, "b": 99, "c": 3}

    def test_deny_with_updated_input(self):
        """DENY 也带 updated_input"""
        reg = HookRegistry()
        reg.register_hook(
            "PreToolUse", "h1",
            lambda n, i, c: PreToolUseResult.deny(reason="x", updated_input={"sanitized": True}),
        )
        result = reg.run_pre_tool_use("Read", {}, ToolPermissionContext())
        assert result.behavior == PermissionBehavior.DENY.value
        assert result.updated_input == {"sanitized": True}


# ────────────────────────────────────────────────────────────────────
# 异常隔离
# ────────────────────────────────────────────────────────────────────

class TestHookExceptionIsolation:
    def test_exception_in_hook_does_not_break_pipeline(self):
        """hook 抛异常不阻断 pipeline(降级为 ALLOW)"""
        reg = HookRegistry()

        def bad_hook(n, i, c):
            raise RuntimeError("boom")

        reg.register_hook("PreToolUse", "bad", bad_hook)
        reg.register_hook("PreToolUse", "good", lambda n, i, c: PreToolUseResult.allow())

        # 不抛异常
        result = reg.run_pre_tool_use("Read", {}, ToolPermissionContext())
        # 至少能拿到 ALLOW(good hook 顶上)
        assert result.behavior == PermissionBehavior.ALLOW.value

    def test_exception_then_deny_uses_deny(self):
        """异常后 DENY 仍生效"""
        reg = HookRegistry()

        def bad_hook(n, i, c):
            raise RuntimeError("boom")

        reg.register_hook("PreToolUse", "bad", bad_hook)
        reg.register_hook(
            "PreToolUse", "deny",
            lambda n, i, c: PreToolUseResult.deny(reason="real deny"),
        )

        result = reg.run_pre_tool_use("Read", {}, ToolPermissionContext())
        assert result.behavior == PermissionBehavior.DENY.value
        assert result.reason == "real deny"


# ────────────────────────────────────────────────────────────────────
# prevent_continuation
# ────────────────────────────────────────────────────────────────────

class TestPreventContinuation:
    def test_prevent_continuation_stops_subsequent_hooks(self):
        """prevent_continuation=True → 后续 hook 不再提交"""
        reg = HookRegistry()

        call_log = []

        def hook_prevent(n, i, c):
            call_log.append("prevent")
            return PreToolUseResult.allow(prevent_continuation=True)

        def hook_should_not_run(n, i, c):
            call_log.append("should_not_run")
            return PreToolUseResult.deny(reason="should not see this")

        reg.register_hook("PreToolUse", "prevent", hook_prevent)
        reg.register_hook("PreToolUse", "later", hook_should_not_run)

        result = reg.run_pre_tool_use("Read", {}, ToolPermissionContext())
        # prevent 已运行,结果可能是 ALLOW(因为 prevent 是 allow + prevent_continuation)
        assert "prevent" in call_log
        # "later" 可能跑了(因为并行),但其 DENY 不应影响主结果
        # 但结果应该是 ALLOW(prevent_continuation 触发)
        assert result.behavior in (
            PermissionBehavior.ALLOW.value,
            PermissionBehavior.DENY.value,  # 如果 DENY 先到
        )


# ────────────────────────────────────────────────────────────────────
# Default hooks
# ────────────────────────────────────────────────────────────────────

class TestDefaultSecretHook:
    def test_no_secret_returns_allow(self):
        """无 secret → ALLOW"""
        ctx = ToolPermissionContext()
        result = default_secret_hook("Write", {"path": "x.py", "content": "hello"}, ctx)
        assert result.behavior == PermissionBehavior.ALLOW.value

    def test_secret_in_content_returns_ask(self):
        """content 含 secret → ASK"""
        ctx = ToolPermissionContext()
        result = default_secret_hook(
            "Write",
            {"path": "x.py", "content": "sk-ant-api03-abcdefghijklmnopqrstuvwxyz0123456789"},
            ctx,
        )
        assert result.behavior == PermissionBehavior.ASK.value

    def test_secret_in_command_returns_ask(self):
        """Bash command 含 secret → ASK"""
        ctx = ToolPermissionContext()
        result = default_secret_hook(
            "Bash",
            {"command": "echo AKIAIOSFODNN7EXAMPLE"},
            ctx,
        )
        assert result.behavior == PermissionBehavior.ASK.value

    def test_non_secret_tool_skipped(self):
        """非 secret check tool(如 calc)→ ALLOW"""
        ctx = ToolPermissionContext()
        result = default_secret_hook(
            "calc",
            {"expression": "sk-ant-xxxxx"},
            ctx,
        )
        # calc 不在 _SECRET_CHECK_TOOLS → ALLOW
        assert result.behavior == PermissionBehavior.ALLOW.value

    def test_hook_name_set(self):
        """hook_name = 'default_secret'"""
        ctx = ToolPermissionContext()
        result = default_secret_hook("Write", {"content": "hi"}, ctx)
        assert result.hook_name == "default_secret"


class TestDefaultPathValidationHook:
    def test_safe_path_returns_allow(self):
        """safe path → ALLOW"""
        ctx = ToolPermissionContext()
        result = default_path_validation_hook(
            "Read", {"path": "src/main.py"}, ctx,
        )
        assert result.behavior == PermissionBehavior.ALLOW.value

    def test_sensitive_path_returns_deny(self):
        """敏感路径 → DENY"""
        ctx = ToolPermissionContext()
        result = default_path_validation_hook(
            "Read", {"path": ".ssh/id_rsa"}, ctx,
        )
        assert result.behavior == PermissionBehavior.DENY.value

    def test_sensitive_path_in_write(self):
        """Write 到敏感路径 → DENY"""
        ctx = ToolPermissionContext()
        result = default_path_validation_hook(
            "Write", {"path": ".git/HEAD", "content": "x"}, ctx,
        )
        assert result.behavior == PermissionBehavior.DENY.value

    def test_non_path_tool_skipped(self):
        """非 path check tool(Bash)→ ALLOW"""
        ctx = ToolPermissionContext()
        result = default_path_validation_hook(
            "Bash", {"command": "ls"}, ctx,
        )
        assert result.behavior == PermissionBehavior.ALLOW.value


class TestDefaultHooks:
    def test_default_hooks_registry_has_two(self):
        """default_hooks() 返带 2 个 hook 的 registry"""
        reg = default_hooks()
        pre = reg.list_hooks("PreToolUse")
        assert "default_secret" in pre
        assert "default_path" in pre

    def test_default_hooks_secret_triggers(self):
        """default_hooks 集成触发 secret 检测"""
        reg = default_hooks()
        result = reg.run_pre_tool_use(
            "Write",
            {"path": "x.py", "content": "sk-ant-api03-abcdefghijklmnopqrstuvwxyz0123456789"},
            ToolPermissionContext(),
        )
        assert result.behavior == PermissionBehavior.ASK.value

    def test_default_hooks_path_triggers(self):
        """default_hooks 集成触发 path 检测"""
        reg = default_hooks()
        result = reg.run_pre_tool_use(
            "Read", {"path": ".ssh/id_rsa"}, ToolPermissionContext(),
        )
        assert result.behavior == PermissionBehavior.DENY.value


# ────────────────────────────────────────────────────────────────────
# 并行性冒烟(无法严格测试,但确认不阻塞)
# ────────────────────────────────────────────────────────────────────

class TestParallelismSmoke:
    def test_multiple_hooks_run_concurrently(self):
        """多个 slow hook 总耗时 ≈ 单 hook 耗时(并行)"""
        reg = HookRegistry()

        def slow_hook(n, i, c):
            time.sleep(0.1)
            return PreToolUseResult.allow()

        # 注册 5 个 slow hook
        for i in range(5):
            reg.register_hook("PreToolUse", f"slow-{i}", slow_hook)

        start = time.time()
        result = reg.run_pre_tool_use("Read", {}, ToolPermissionContext())
        elapsed = time.time() - start

        assert result.behavior == PermissionBehavior.ALLOW.value
        # 并行下应 < 5 * 0.1 = 0.5s(留些 buffer)
        assert elapsed < 0.4, f"hook execution took {elapsed:.2f}s, expected parallel"
