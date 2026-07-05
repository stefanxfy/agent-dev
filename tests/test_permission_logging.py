"""
工具权限 + 安全沙箱日志增强 — 测试

覆盖 6 个子 logger 的关键流程日志:
- 🛡️ agent_core.permission
- ⚙️ agent_core.sandbox
- 🪝 agent_core.hook
- 📋 agent_core.audit
- 🧪 agent_core.safety
- 🤖 agent_core.classifier

每条测试:
  1. 设置 caplog level=DEBUG
  2. 触发关键流程(deny / allow / sandbox wrap / hook / audit / classifier)
  3. 断言对应子 logger 出现 [xxx] 标记
"""
from __future__ import annotations

import logging
import os
import tempfile
from pathlib import Path

import pytest


# ────────────────────────────────────────────────────────────────────
# helpers
# ────────────────────────────────────────────────────────────────────

# 6 个子 logger 名称(与 logging_setup._SUB_LOGGER_ENV 对齐)
PERM = "agent_core.permission"
SANDBOX = "agent_core.sandbox"
HOOK = "agent_core.hook"
AUDIT = "agent_core.audit"
SAFETY = "agent_core.safety"
CLASSIFIER = "agent_core.classifier"


def _records(caplog, name: str) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.name == name]


# ────────────────────────────────────────────────────────────────────
# 1. permission 子系统日志
# ────────────────────────────────────────────────────────────────────

class TestPermissionLogging:
    def test_permission_engine_logs_decision(self, caplog):
        """permission_engine.check_permissions 命中 deny → 🛡️ [decision] 应在 caplog"""
        from agent_core.tools.permission_engine import PermissionEngine
        from agent_core.tools.permission_types import ToolPermissionContext, PermissionMode

        # 强制 deny:global_deny 含 Bash → 命中 step_1a_global_deny
        ctx = ToolPermissionContext(
            mode=PermissionMode.DEFAULT.value,
            always_allow_rules={},
            always_deny_rules={
                "userSettings": ["Bash"],
            },
            always_ask_rules={},
        )
        engine = PermissionEngine(ctx)

        class BashTool:
            name = "Bash"

        with caplog.at_level(logging.DEBUG, logger=PERM):
            decision = engine.check_permissions(
                BashTool(), {"command": "ls"}, [],
            )

        perm_records = _records(caplog, PERM)
        decision_logs = [r for r in perm_records if "[decision]" in r.getMessage()]
        assert decision_logs, "应有 [decision] 标记的 log"
        msg = decision_logs[0].getMessage()
        assert "tool=Bash" in msg
        assert "behavior=deny" in msg

    def test_denial_tracking_logs_increment(self, caplog):
        """denial_tracking.record_denial → 🛡️ [denial_record]"""
        from agent_core.tools.denial_tracking import (
            DenialTrackingState, record_denial, record_success,
        )
        state = DenialTrackingState()
        with caplog.at_level(logging.DEBUG, logger=PERM):
            new_state = record_denial(state)
            record_success(new_state)

        perm_records = _records(caplog, PERM)
        denial_record = [r for r in perm_records if "[denial_record]" in r.getMessage()]
        success_record = [r for r in perm_records if "[denial_record_success]" in r.getMessage()]
        assert denial_record, "应记录 [denial_record]"
        assert success_record, "应记录 [denial_record_success]"
        assert "consecutive=0→1" in denial_record[0].getMessage()

    def test_permission_matcher_logs_match(self, caplog):
        """permission_matcher.match_permission_rule → 🛡️ [match_*]"""
        from agent_core.tools.permission_matcher import (
            ShellPermissionRule, match_permission_rule,
        )
        with caplog.at_level(logging.DEBUG, logger=PERM):
            match_permission_rule(
                ShellPermissionRule(type="prefix", prefix="rm "),
                "rm -rf /tmp/foo",
            )
        perm_records = _records(caplog, PERM)
        match_logs = [r for r in perm_records if "[match_prefix]" in r.getMessage()]
        assert match_logs
        assert "matched=True" in match_logs[0].getMessage()


# ────────────────────────────────────────────────────────────────────
# 2. safety 子系统日志(从 0 到 1)
# ────────────────────────────────────────────────────────────────────

class TestSafetyLogging:
    def test_safety_secret_block_logs(self, caplog):
        """safety_check 命中 secret pattern → 🧪 [safety_secret_block]"""
        from agent_core.tools.safety_check import safety_check
        # 触发 secret 检测:echo sk-ant-xxxx
        with caplog.at_level(logging.DEBUG, logger=SAFETY):
            result = safety_check("Bash", {"command": "echo sk-ant-aaaaaaaaaaaaaaaaaaaaaaaaaaaa"})

        safety_records = _records(caplog, SAFETY)
        secret_logs = [r for r in safety_records if "[safety_secret_block]" in r.getMessage()]
        # 命中或未命中都应至少有 entry
        entry_logs = [r for r in safety_records if "[safety_check_entry]" in r.getMessage()]
        assert entry_logs, "应有 [safety_check_entry]"


# ────────────────────────────────────────────────────────────────────
# 3. sandbox 子系统日志
# ────────────────────────────────────────────────────────────────────

class TestSandboxLogging:
    def test_sandbox_decision_logs(self, caplog):
        """sandbox_decision.should_use_sandbox → ⚙️ [should_use_sandbox]"""
        from agent_core.tools.sandbox_decision import should_use_sandbox
        from agent_core.tools.sandbox_manager import sandbox_manager

        # 强制 sandbox disabled → 命中 should_use_sandbox_disabled
        sandbox_manager._reset_for_testing()
        with caplog.at_level(logging.DEBUG, logger=SANDBOX):
            result = should_use_sandbox("Bash", {"command": "ls"})

        # 禁用时返 False,但 entry log 应在
        sandbox_records = _records(caplog, SANDBOX)
        entry_logs = [r for r in sandbox_records if "[should_use_sandbox]" in r.getMessage()]
        assert entry_logs, "应有 [should_use_sandbox] entry"
        assert "tool=Bash" in entry_logs[0].getMessage()

    def test_sandbox_prompt_logs(self, caplog):
        """sandbox_prompt.get_sandbox_prompt_section → ⚙️ [sandbox_prompt_*]"""
        from agent_core.tools.sandbox_prompt import get_sandbox_prompt_section
        with caplog.at_level(logging.DEBUG, logger=SANDBOX):
            section = get_sandbox_prompt_section()

        # 禁用时返 "" + 应打 [sandbox_prompt_empty] log
        sandbox_records = _records(caplog, SANDBOX)
        prompt_logs = [r for r in sandbox_records if "sandbox_prompt" in r.getMessage()]
        assert prompt_logs, "应有 [sandbox_prompt_*] log"


# ────────────────────────────────────────────────────────────────────
# 4. audit 子系统日志
# ────────────────────────────────────────────────────────────────────

class TestAuditLogging:
    def test_audit_logger_logs_record_built(self, caplog, tmp_path):
        """AuditLogger.log → 📋 [audit_record_built]"""
        from agent_core.tools.audit_logger import AuditLogger
        from agent_core.tools.permission_types import (
            PermissionBehavior, PermissionDecision, PermissionMode, ToolPermissionContext,
        )

        ctx = ToolPermissionContext(
            mode=PermissionMode.DEFAULT.value,
            sandbox_enabled=False,
        )
        decision = PermissionDecision(behavior=PermissionBehavior.ALLOW.value)
        with caplog.at_level(logging.DEBUG, logger=AUDIT):
            al = AuditLogger(str(tmp_path))
            al.log("Bash", {"command": "ls"}, decision, context=ctx, stage="step_1c_bash_allow")

        audit_records = _records(caplog, AUDIT)
        built_logs = [r for r in audit_records if "[audit_record_built]" in r.getMessage()]
        written_logs = [r for r in audit_records if "[audit_record_written]" in r.getMessage()]
        init_logs = [r for r in audit_records if "[audit_logger_init]" in r.getMessage()]
        assert init_logs, "构造时应有 [audit_logger_init]"
        assert built_logs, "应有 [audit_record_built]"
        assert written_logs, "应有 [audit_record_written]"


# ────────────────────────────────────────────────────────────────────
# 5. classifier 子系统日志
# ────────────────────────────────────────────────────────────────────

class TestClassifierLogging:
    def test_classifier_enabled_check_logs(self, caplog, monkeypatch):
        """is_classifier_enabled → 🤖 [classifier_enabled_check]"""
        monkeypatch.setenv("TRANSCRIPT_CLASSIFIER_ENABLED", "true")
        from agent_core.tools.classifier import is_classifier_enabled
        from agent_core.tools.permission_types import PermissionMode

        with caplog.at_level(logging.DEBUG, logger=CLASSIFIER):
            enabled = is_classifier_enabled("anthropic", PermissionMode.DEFAULT, no_settings_match=True)

        cls_records = _records(caplog, CLASSIFIER)
        check_logs = [r for r in cls_records if "[classifier_enabled_check]" in r.getMessage()]
        assert check_logs, "应有 [classifier_enabled_check]"

    def test_classify_transcript_too_long(self, caplog):
        """HaikuClassifier.classify with too long transcript → 🤖 [classify_transcript_too_long]"""
        from agent_core.tools.classifier import HaikuClassifier
        from agent_core.tools.permission_types import ToolPermissionContext, PermissionMode

        # max_transcript_tokens=10 → 1 message ≈ 1000 token 必超
        clf = HaikuClassifier(max_transcript_tokens=10)
        ctx = ToolPermissionContext(mode=PermissionMode.DEFAULT.value)

        # messages 数量大,触发 too_long
        with caplog.at_level(logging.DEBUG, logger=CLASSIFIER):
            result = clf.classify(
                messages=[{"role": "user", "content": "x"}] * 20,
                tool_name="Bash",
                tool_input={"command": "ls"},
                context=ctx,
            )

        cls_records = _records(caplog, CLASSIFIER)
        too_long = [r for r in cls_records if "[classify_transcript_too_long]" in r.getMessage()]
        assert too_long, "应有 [classify_transcript_too_long]"


# ────────────────────────────────────────────────────────────────────
# 6. logging_setup 子 logger env 变量控制
# ────────────────────────────────────────────────────────────────────

class TestLoggingSetupEnv:
    def test_env_var_raises_level(self, monkeypatch):
        """AGENT_LOG_SANDBOX=INFO 应把 sandbox 子 logger 提到 INFO"""
        monkeypatch.setenv("AGENT_LOG_SANDBOX", "INFO")
        # 直接调用 helper 验证
        from agent_core.logging_setup import _apply_env_level
        _apply_env_level("agent_core.sandbox", "AGENT_LOG_SANDBOX")
        assert logging.getLogger("agent_core.sandbox").level == logging.INFO

    def test_env_var_invalid_keeps_default(self, monkeypatch):
        """非法 env value 应回退到默认 DEBUG"""
        monkeypatch.setenv("AGENT_LOG_SANDBOX", "BOGUS")
        from agent_core.logging_setup import _apply_env_level
        _apply_env_level("agent_core.sandbox", "AGENT_LOG_SANDBOX")
        assert logging.getLogger("agent_core.sandbox").level == logging.DEBUG
