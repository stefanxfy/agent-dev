"""
Phase 2 (M2) 端到端集成测试

覆盖 doc §9.4 M2 测试矩阵关键不变量:
1. bare-git scrub attack 回归(CC #29316)
2. subcommand deny 在 sandbox 内仍生效
3. sandbox auto-allow 对安全命令
4. BashTool e2e(mock subprocess)
5. audit.jsonl 创建 + hash 不含原文
6. sandbox tmp dir mode 0o700
7. system prompt sandbox section
8. 回归:calc/search/Read safety check

对齐 docs/tool/tool-security-architecture.md §6.3 关键不变量:
- 即便 sandbox auto-allow,subcommand-level deny 仍触发整体 deny
- 即便 dangerouslyDisableSandbox,deny rule 仍生效
- 两层都失守才可能误放
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from agent_core.tools.base import ToolRegistry
from agent_core.tools.bash_permissions import bash_check_permissions, check_sandbox_auto_allow
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


def _enabled_sandbox_fixture(auto_allow=True):
    """返回一个 enabled sandbox 的 patch context manager stack(手动进/出)"""
    mgr = SandboxManager()
    mgr.load_config({"enabled": True, "autoAllowBashIfSandboxed": auto_allow})
    return mgr


@pytest.fixture
def enabled_sandbox():
    mgr = _enabled_sandbox_fixture()
    with patch.object(mgr, "_is_supported_platform", return_value=True), \
         patch.object(mgr, "_check_dependencies", return_value=True), \
         patch.object(mgr, "initialize", lambda: setattr(mgr, "_initialized", True)):
        yield mgr


# ────────────────────────────────────────────────────────────────────
# 1. bare-git scrub attack 回归(CC #29316)
# ────────────────────────────────────────────────────────────────────

class TestBareGitScrubRegression:
    """对齐 CC #29316 bare-git scrub attack"""

    def test_cd_into_repo_then_git_status_asks(self):
        # 经典攻击向量:cd 进恶意仓库 + git 命令(触发 .git/config alias/hook)
        decision = bash_check_permissions(
            {"command": "cd /tmp/evil_repo && git status"},
            _ctx(),
        )
        assert decision.behavior == PermissionBehavior.ASK.value
        assert decision.decision_reason.type == "safetyCheck"
        assert "29316" in (decision.decision_reason.reason or "") or \
               "bare-git" in (decision.decision_reason.reason or "")

    def test_cd_then_git_log_asks(self):
        decision = bash_check_permissions(
            {"command": "cd suspicious && git log --oneline"},
            _ctx(),
        )
        assert decision.behavior == PermissionBehavior.ASK.value

    def test_git_clone_then_cd_then_git_asks(self):
        # 三段式攻击
        decision = bash_check_permissions(
            {"command": "git clone evil.git /tmp/evil && cd /tmp/evil && git status"},
            _ctx(),
        )
        assert decision.behavior == PermissionBehavior.ASK.value

    def test_git_alone_without_cd_not_flagged(self):
        # git 无 cd → 不触发(正常 git 操作)
        decision = bash_check_permissions(
            {"command": "git status"}, _ctx(),
        )
        assert decision.decision_reason.type != "safetyCheck"

    def test_cd_alone_without_git_not_flagged(self):
        decision = bash_check_permissions(
            {"command": "cd /tmp"}, _ctx(),
        )
        assert decision.decision_reason.type != "safetyCheck"

    def test_sandbox_manager_scrubs_bare_git_after_command(self, enabled_sandbox, tmp_path):
        # sandbox cleanup 应删除 sandbox tmp 里的 .git 残留
        from agent_core.tools.sandbox_manager import sandbox_manager
        fake_tmp = tmp_path / "claude-test"
        fake_tmp.mkdir()
        evil_git = fake_tmp / ".git"
        evil_git.mkdir()
        (evil_git / "config").write_text("[alias] x = !rm -rf /")

        with patch.object(sandbox_manager, "_get_sandbox_tmp_dir", return_value=str(fake_tmp)):
            sandbox_manager.cleanup_after_command()
        assert not evil_git.exists()


# ────────────────────────────────────────────────────────────────────
# 2. subcommand deny 在 sandbox 内仍生效
# ────────────────────────────────────────────────────────────────────

class TestSubcommandDenyInSandbox:
    """对齐 doc §6.3 关键不变量:即便 sandbox auto-allow,deny 仍生效"""

    def test_deny_rule_blocks_in_sandbox_auto_allow(self, enabled_sandbox):
        ctx = _ctx(always_deny_rules={"projectSettings": ["Bash(rm:*)"]})
        decision = bash_check_permissions(
            {"command": "rm -rf /important"}, ctx,
        )
        assert decision.behavior == PermissionBehavior.DENY.value

    def test_deny_in_compound_blocks_in_sandbox(self, enabled_sandbox):
        ctx = _ctx(always_deny_rules={"projectSettings": ["Bash(rm:*)"]})
        decision = bash_check_permissions(
            {"command": "echo safe && rm -rf / && echo more"}, ctx,
        )
        assert decision.behavior == PermissionBehavior.DENY.value

    def test_dangerously_disable_does_not_bypass_deny(self, enabled_sandbox):
        # 关键不变量:dangerouslyDisableSandbox 仅 bypass sandbox,不 bypass permission
        ctx = _ctx(always_deny_rules={"projectSettings": ["Bash(rm:*)"]})
        decision = bash_check_permissions(
            {"command": "rm -rf /", "dangerously_disable_sandbox": True}, ctx,
        )
        assert decision.behavior == PermissionBehavior.DENY.value

    def test_check_sandbox_auto_allow_respects_deny(self, enabled_sandbox):
        ctx = _ctx(always_deny_rules={"projectSettings": ["Bash(rm:*)"]})
        decision = check_sandbox_auto_allow({"command": "rm -rf /"}, ctx)
        assert decision.behavior == PermissionBehavior.DENY.value


# ────────────────────────────────────────────────────────────────────
# 3. sandbox auto-allow 对安全命令
# ────────────────────────────────────────────────────────────────────

class TestSandboxAutoAllowSafe:
    def test_safe_command_auto_allowed_in_sandbox(self, enabled_sandbox):
        decision = bash_check_permissions({"command": "npm install express"}, _ctx())
        assert decision.behavior == PermissionBehavior.ALLOW.value

    def test_ls_auto_allowed_in_sandbox(self, enabled_sandbox):
        decision = bash_check_permissions({"command": "ls -la"}, _ctx())
        assert decision.behavior == PermissionBehavior.ALLOW.value

    def test_echo_auto_allowed_in_sandbox(self, enabled_sandbox):
        decision = bash_check_permissions({"command": "echo hello"}, _ctx())
        assert decision.behavior == PermissionBehavior.ALLOW.value

    def test_auto_allow_reason_mentions_sandbox(self, enabled_sandbox):
        decision = bash_check_permissions({"command": "npm install"}, _ctx())
        assert "Auto-allowed in sandbox" in (decision.decision_reason.reason or "")

    def test_sandbox_disabled_asks_for_unknown(self):
        # sandbox 禁用 + 无 rule → passthrough(非 auto-allow)
        decision = bash_check_permissions({"command": "npm install"}, _ctx())
        assert decision.behavior == PermissionBehavior.PASSTHROUGH.value


# ────────────────────────────────────────────────────────────────────
# 4. BashTool e2e(通过 ToolRegistry)
# ────────────────────────────────────────────────────────────────────

class TestBashToolE2E:
    def test_bash_executes_and_returns_output(self):
        registry = ToolRegistry()
        register_builtin_tools(registry)
        result = registry.execute("Bash", {"command": "echo e2e_test"})
        assert result["status"] == "success"
        assert "e2e_test" in result["output"]

    def test_bash_with_working_dir(self, tmp_path):
        registry = ToolRegistry()
        register_builtin_tools(registry)
        result = registry.execute("Bash", {
            "command": "pwd",
            "working_dir": str(tmp_path),
        })
        assert result["status"] == "success"
        assert str(tmp_path) in result["output"]

    def test_bash_sandbox_disabled_executes_directly(self):
        # sandbox 禁用 → 直接执行(不 wrap)
        registry = ToolRegistry()
        register_builtin_tools(registry)
        with patch("agent_core.tools.builtin.subprocess.run", wraps=subprocess.run) as spy:
            registry.execute("Bash", {"command": "echo direct"})
        # 应直接执行(命令不含 npx wrap)
        assert spy.called

    def test_bash_sandbox_enabled_wraps_command(self, enabled_sandbox):
        registry = ToolRegistry()
        register_builtin_tools(registry)
        captured_cmds = []

        class FakeResult:
            stdout = "wrapped ok"
            stderr = ""
            returncode = 0

        def spy_run(cmd, *args, **kwargs):
            captured_cmds.append(cmd)
            return FakeResult()

        with patch("agent_core.tools.builtin.subprocess.run", spy_run):
            registry.execute("Bash", {"command": "echo wrap_me"})
        # sandbox 启用 → 命令被 wrap(含 npx)
        assert any("npx" in str(c) for c in captured_cmds)

    def test_bash_dangerously_disable_skips_wrap(self, enabled_sandbox):
        registry = ToolRegistry()
        register_builtin_tools(registry)
        captured_cmds = []

        class FakeResult:
            stdout = "ok"
            stderr = ""
            returncode = 0

        def spy_run(cmd, *args, **kwargs):
            captured_cmds.append(cmd)
            return FakeResult()

        with patch("agent_core.tools.builtin.subprocess.run", spy_run):
            registry.execute("Bash", {
                "command": "echo bypass",
                "dangerously_disable_sandbox": True,
            })
        # disable → 不 wrap(不含 npx)
        assert not any("npx" in str(c) for c in captured_cmds)


# ────────────────────────────────────────────────────────────────────
# 5. PermissionEngine → BashTool 集成
# ────────────────────────────────────────────────────────────────────

class TestEngineBashIntegration:
    def _bash_tool(self):
        return SimpleNamespace(
            name="Bash", check_permissions=None, requires_user_interaction=False,
        )

    def test_engine_deny_rule_blocks_bash(self):
        engine = PermissionEngine(context=_ctx(
            always_deny_rules={"projectSettings": ["Bash(rm:*)"]},
        ))
        decision = engine.check_permissions(
            self._bash_tool(), {"command": "rm -rf /"},
        )
        assert decision.behavior == PermissionBehavior.DENY.value

    def test_engine_bash_auto_allow_in_sandbox(self, enabled_sandbox):
        engine = PermissionEngine(context=_ctx(sandbox_enabled=True))
        decision = engine.check_permissions(
            self._bash_tool(), {"command": "npm install"},
        )
        assert decision.behavior == PermissionBehavior.ALLOW.value

    def test_engine_bash_passthrough_without_rule(self):
        engine = PermissionEngine(context=_ctx())
        decision = engine.check_permissions(
            self._bash_tool(), {"command": "ls"},
        )
        # passthrough → engine 继续到 step 7 default ask
        assert decision.behavior == PermissionBehavior.ASK.value

    def test_engine_routes_bash_not_other_tools(self):
        engine = PermissionEngine(context=_ctx())
        with patch.object(
            engine, "_run_bash_check_permissions",
        ) as mock_bash:
            mock_bash.return_value = None
            # Read tool 不应触发 bash 路径
            read_tool = SimpleNamespace(
                name="Read", check_permissions=None, requires_user_interaction=False,
            )
            engine.check_permissions(read_tool, {"path": "/tmp/x"})
        mock_bash.assert_not_called()


# ────────────────────────────────────────────────────────────────────
# 6. audit.jsonl e2e
# ────────────────────────────────────────────────────────────────────

class TestAuditJsonlE2E:
    def test_audit_jsonl_created_after_decision(self, tmp_path):
        from agent_core.tools.audit_logger import AuditLogger
        al = AuditLogger(str(tmp_path / "e2e-session"))
        engine = PermissionEngine(context=_ctx(), audit_logger=al)
        tool = SimpleNamespace(
            name="Bash", check_permissions=None, requires_user_interaction=False,
        )
        engine.check_permissions(tool, {"command": "ls"})
        assert (tmp_path / "e2e-session" / "audit.jsonl").exists()

    def test_audit_contains_hash_not_plaintext(self, tmp_path):
        from agent_core.tools.audit_logger import AuditLogger
        from tests.test_safety_check import _S, _SUFFIX
        al = AuditLogger(str(tmp_path / "audit-session"))
        engine = PermissionEngine(context=_ctx(), audit_logger=al)
        tool = SimpleNamespace(
            name="Bash", check_permissions=None, requires_user_interaction=False,
        )
        secret_cmd = _S("sk-ant-api03-", _SUFFIX)
        engine.check_permissions(tool, {"command": secret_cmd})
        content = (tmp_path / "audit-session" / "audit.jsonl").read_text()
        # secret 字面量不在 audit,hash 在
        assert secret_cmd not in content
        from agent_core.tools.audit_logger import compute_tool_input_hash
        assert compute_tool_input_hash({"command": secret_cmd}) in content

    def test_audit_failure_does_not_block_engine(self, tmp_path):
        # audit_logger.log 抛 → engine 仍返 decision
        bad_logger = MagicMock()
        bad_logger.log.side_effect = RuntimeError("disk full")
        engine = PermissionEngine(context=_ctx(), audit_logger=bad_logger)
        tool = SimpleNamespace(
            name="Bash", check_permissions=None, requires_user_interaction=False,
        )
        # 不应抛
        decision = engine.check_permissions(tool, {"command": "ls"})
        assert decision.behavior is not None

    def test_audit_records_multiple_decisions(self, tmp_path):
        from agent_core.tools.audit_logger import AuditLogger
        al = AuditLogger(str(tmp_path / "multi-session"))
        engine = PermissionEngine(context=_ctx(), audit_logger=al)
        tool = SimpleNamespace(
            name="Bash", check_permissions=None, requires_user_interaction=False,
        )
        for i in range(5):
            engine.check_permissions(tool, {"command": f"echo {i}"})
        lines = (tmp_path / "multi-session" / "audit.jsonl").read_text().strip().split("\n")
        assert len(lines) == 5


# ────────────────────────────────────────────────────────────────────
# 7. sandbox tmp dir + cleanup
# ────────────────────────────────────────────────────────────────────

class TestSandboxTmpAndCleanup:
    def test_sandbox_tmp_dir_mode_0700(self, enabled_sandbox, tmp_path, monkeypatch):
        import tempfile
        monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
        monkeypatch.setattr(os, "getuid", lambda: 54321, raising=False)
        from agent_core.tools.sandbox_manager import sandbox_manager
        result = sandbox_manager._get_sandbox_tmp_dir()
        result_path = Path(result)
        assert result_path.exists()
        assert (result_path.stat().st_mode & 0o777) == 0o700

    def test_cleanup_removes_old_tmp_subdirs(self, enabled_sandbox, tmp_path):
        old = tmp_path / "run-old"
        old.mkdir()
        old_time = time.time() - 25 * 3600
        os.utime(old, (old_time, old_time))

        from agent_core.tools.sandbox_manager import sandbox_manager
        with patch.object(sandbox_manager, "_get_sandbox_tmp_dir", return_value=str(tmp_path)):
            sandbox_manager._cleanup_sandbox_tmp_dir(max_age_hours=24.0)
        assert not old.exists()


# ────────────────────────────────────────────────────────────────────
# 8. system prompt sandbox section e2e
# ────────────────────────────────────────────────────────────────────

class TestSystemPromptE2E:
    def _make_agent(self, sandbox_enabled):
        from agent_core.agent_core import ReactAgent
        agent = ReactAgent.__new__(ReactAgent)
        agent.permission_engine = PermissionEngine(context=_ctx(sandbox_enabled=sandbox_enabled))
        agent.system_prompt = "You are a helpful agent."
        agent.memory_index = None
        return agent

    def test_prompt_includes_sandbox_section_when_enabled(self, enabled_sandbox):
        agent = self._make_agent(sandbox_enabled=True)
        prompt = agent._build_system_prompt_with_memory()
        assert "## Command sandbox" in prompt
        assert "You are a helpful agent." in prompt

    def test_prompt_omits_sandbox_section_when_disabled(self):
        agent = self._make_agent(sandbox_enabled=False)
        prompt = agent._build_system_prompt_with_memory()
        assert "## Command sandbox" not in prompt


# ────────────────────────────────────────────────────────────────────
# 9. 回归:现有工具 + safety check
# ────────────────────────────────────────────────────────────────────

class TestRegression:
    def test_calc_still_works(self):
        registry = ToolRegistry()
        register_builtin_tools(registry)
        result = registry.execute("calc", {"expression": "2 + 3"})
        assert result["status"] == "success"
        assert "5" in result["output"]

    def test_calc_complex_expression(self):
        registry = ToolRegistry()
        register_builtin_tools(registry)
        result = registry.execute("calc", {"expression": "10 * (3 + 2)"})
        assert result["status"] == "success"
        assert "50" in result["output"]

    def test_search_tool_registered(self):
        registry = ToolRegistry()
        register_builtin_tools(registry)
        assert registry.get("search") is not None

    def test_read_safety_check_still_blocks_sensitive(self):
        # safety_check 对 Read .ssh 仍生效(Phase 1 hook)
        from agent_core.tools.safety_check import safety_check
        assert safety_check("Read", {"path": ".ssh/id_rsa"}) is True
        assert safety_check("Read", {"path": "./docs/README.md"}) is False

    def test_three_tools_registered(self):
        registry = ToolRegistry()
        register_builtin_tools(registry)
        names = registry.list_names()
        assert "calc" in names
        assert "search" in names
        assert "Bash" in names

    def test_engine_default_mode_works_for_calc(self):
        # calc 不走 bash 路径,engine 正常处理
        engine = PermissionEngine(context=_ctx(mode=PermissionMode.DEFAULT.value))
        calc_tool = SimpleNamespace(
            name="calc", check_permissions=None, requires_user_interaction=False,
        )
        decision = engine.check_permissions(calc_tool, {"expression": "1+1"})
        # calc 无 rule → default ASK(step 7)
        assert decision.behavior == PermissionBehavior.ASK.value


# ────────────────────────────────────────────────────────────────────
# 10. doc §6.3 关键不变量总结
# ────────────────────────────────────────────────────────────────────

class TestCriticalInvariants:
    """对齐 doc §6.3 '安全保证':两层都失守才可能误放"""

    def test_invariant_1_auto_allow_respects_deny(self, enabled_sandbox):
        """即便 auto-allow,deny 规则仍生效"""
        ctx = _ctx(always_deny_rules={"projectSettings": ["Bash(rm:*)"]})
        decision = bash_check_permissions({"command": "rm -rf /"}, ctx)
        assert decision.behavior == PermissionBehavior.DENY.value

    def test_invariant_2_disable_respects_deny(self, enabled_sandbox):
        """即便沙箱被绕过,deny 规则仍生效"""
        ctx = _ctx(always_deny_rules={"projectSettings": ["Bash(rm:*)"]})
        decision = bash_check_permissions(
            {"command": "rm -rf /", "dangerously_disable_sandbox": True}, ctx,
        )
        assert decision.behavior == PermissionBehavior.DENY.value

    def test_invariant_3_cd_git_always_asks(self, enabled_sandbox):
        """cd + git 组合永远 ASK(防 #29316),即便在 sandbox 内"""
        decision = bash_check_permissions(
            {"command": "cd /tmp && git status"}, _ctx(),
        )
        assert decision.behavior == PermissionBehavior.ASK.value

    def test_invariant_4_safe_command_allowed_in_sandbox(self, enabled_sandbox):
        """安全命令在 sandbox 内 auto-allow(无弹窗)"""
        decision = bash_check_permissions({"command": "ls -la"}, _ctx())
        assert decision.behavior == PermissionBehavior.ALLOW.value
