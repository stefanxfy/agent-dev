"""
audit_logger.py 测试

覆盖:
1. AuditRecord 字段 + 序列化
2. compute_tool_input_hash 稳定性 + 不含原文
3. AuditLogger.log 写文件 + append + atomic
4. failure graceful(不阻断主流程)
5. query filter(since_ts / tool_name / decision)
6. 全局单例(init / get / reset)
7. mode 0o700 目录创建
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from agent_core.tools.audit_logger import (
    AuditLogger,
    AuditRecord,
    compute_tool_input_hash,
    get_audit_logger,
    init_audit_logger,
    reset_audit_logger_for_testing,
)
from agent_core.tools.permission_types import (
    OtherReason,
    PermissionBehavior,
    PermissionDecision,
    RuleReason,
    PermissionRule,
    PermissionRuleData,
    PermissionRuleSource,
    PermissionRuleValue,
    ToolPermissionContext,
)


@pytest.fixture(autouse=True)
def reset_global():
    reset_audit_logger_for_testing()
    yield
    reset_audit_logger_for_testing()


@pytest.fixture
def logger(tmp_path):
    session_dir = tmp_path / "test-session"
    return AuditLogger(str(session_dir))


def _make_decision(behavior=PermissionBehavior.ALLOW, reason=None):
    return PermissionDecision(
        behavior=behavior.value if hasattr(behavior, "value") else behavior,
        decision_reason=reason or OtherReason(reason="test"),
    )


def _make_ctx(**kwargs):
    return ToolPermissionContext(**kwargs)


# ────────────────────────────────────────────────────────────────────
# compute_tool_input_hash
# ────────────────────────────────────────────────────────────────────

class TestComputeHash:
    def test_stable_for_same_input(self):
        h1 = compute_tool_input_hash({"command": "ls"})
        h2 = compute_tool_input_hash({"command": "ls"})
        assert h1 == h2

    def test_different_for_different_input(self):
        h1 = compute_tool_input_hash({"command": "ls"})
        h2 = compute_tool_input_hash({"command": "rm"})
        assert h1 != h2

    def test_length_16(self):
        h = compute_tool_input_hash({"x": 1})
        assert len(h) == 16

    def test_empty_dict(self):
        h = compute_tool_input_hash({})
        assert isinstance(h, str)
        assert len(h) == 16

    def test_key_order_independent(self):
        # sort_keys=True → 顺序无关
        h1 = compute_tool_input_hash({"a": 1, "b": 2})
        h2 = compute_tool_input_hash({"b": 2, "a": 1})
        assert h1 == h2

    def test_does_not_return_input_plaintext(self):
        # hash 不应等于原 input 字符串
        secret = "super-secret-key-value"
        h = compute_tool_input_hash({"key": secret})
        assert secret not in h


# ────────────────────────────────────────────────────────────────────
# AuditLogger.log — 基础写入
# ────────────────────────────────────────────────────────────────────

class TestLogWrite:
    def test_creates_file(self, logger):
        logger.log(
            "Bash", {"command": "ls"},
            _make_decision(), _make_ctx(),
        )
        assert logger.path.exists()

    def test_writes_valid_jsonl(self, logger):
        logger.log(
            "Bash", {"command": "ls"},
            _make_decision(), _make_ctx(),
        )
        content = logger.path.read_text(encoding="utf-8").strip()
        data = json.loads(content)
        assert data["tool_name"] == "Bash"
        assert data["decision"] == "allow"

    def test_appends_multiple_records(self, logger):
        for i in range(3):
            logger.log(
                f"Bash{i}", {"command": f"cmd{i}"},
                _make_decision(), _make_ctx(),
            )
        lines = logger.path.read_text(encoding="utf-8").strip().split("\n")
        assert len(lines) == 3

    def test_includes_all_fields(self, logger):
        logger.log(
            "Bash", {"command": "ls"},
            _make_decision(), _make_ctx(),
        )
        data = json.loads(logger.path.read_text(encoding="utf-8"))
        for field in ["timestamp", "session_id", "tool_name", "tool_input_hash",
                      "decision", "reason_type", "sandbox_used", "hook_chain",
                      "classifier_used"]:
            assert field in data

    def test_hash_not_plaintext(self, logger):
        # 写入 Bash command 含 fake secret → file 里只有 hash
        from tests.test_safety_check import _S, _SUFFIX
        secret_cmd = _S("sk-ant-api03-", _SUFFIX)
        logger.log(
            "Bash", {"command": secret_cmd},
            _make_decision(), _make_ctx(),
        )
        content = logger.path.read_text(encoding="utf-8")
        # secret 字面量不应出现在 audit.jsonl
        assert secret_cmd not in content
        # 但 hash 在
        expected_hash = compute_tool_input_hash({"command": secret_cmd})
        assert expected_hash in content

    def test_uses_fsync(self, logger):
        with patch("os.fsync") as mock_fsync:
            logger.log(
                "Bash", {"command": "ls"},
                _make_decision(), _make_ctx(),
            )
        assert mock_fsync.called

    def test_timestamp_is_float(self, logger):
        logger.log(
            "Bash", {"command": "ls"},
            _make_decision(), _make_ctx(),
        )
        data = json.loads(logger.path.read_text(encoding="utf-8"))
        assert isinstance(data["timestamp"], float)


# ────────────────────────────────────────────────────────────────────
# AuditLogger.log — context 推断
# ────────────────────────────────────────────────────────────────────

class TestLogContextInference:
    def test_sandbox_used_from_context(self, logger):
        ctx = _make_ctx(sandbox_enabled=True)
        logger.log(
            "Bash", {"command": "ls"},
            _make_decision(), ctx,
        )
        data = json.loads(logger.path.read_text(encoding="utf-8"))
        assert data["sandbox_used"] is True

    def test_sandbox_used_explicit_override(self, logger):
        ctx = _make_ctx(sandbox_enabled=False)
        logger.log(
            "Bash", {"command": "ls"},
            _make_decision(), ctx, sandbox_used=True,
        )
        data = json.loads(logger.path.read_text(encoding="utf-8"))
        assert data["sandbox_used"] is True

    def test_mode_from_context(self, logger):
        ctx = _make_ctx(mode="bypassPermissions")
        logger.log(
            "Bash", {"command": "ls"},
            _make_decision(), ctx,
        )
        data = json.loads(logger.path.read_text(encoding="utf-8"))
        assert data["mode"] == "bypassPermissions"

    def test_hook_chain_recorded(self, logger):
        logger.log(
            "Bash", {"command": "ls"},
            _make_decision(), _make_ctx(),
            hook_chain=["secret_hook", "path_hook"],
        )
        data = json.loads(logger.path.read_text(encoding="utf-8"))
        assert data["hook_chain"] == ["secret_hook", "path_hook"]

    def test_classifier_used_recorded(self, logger):
        logger.log(
            "Bash", {"command": "ls"},
            _make_decision(), _make_ctx(),
            classifier_used=True,
        )
        data = json.loads(logger.path.read_text(encoding="utf-8"))
        assert data["classifier_used"] is True

    def test_denial_state_recorded(self, logger):
        logger.log(
            "Bash", {"command": "ls"},
            _make_decision(), _make_ctx(),
            denial_state={"consecutive_denials": 3, "total_denials": 5},
        )
        data = json.loads(logger.path.read_text(encoding="utf-8"))
        assert data["denial_state"]["consecutive_denials"] == 3


# ────────────────────────────────────────────────────────────────────
# reason 提取
# ────────────────────────────────────────────────────────────────────

class TestReasonExtraction:
    def test_rule_reason_source_extracted(self, logger):
        rule = PermissionRule(
            source=PermissionRuleSource.PROJECT,
            behavior=PermissionBehavior.DENY,
            value=PermissionRuleValue(tool_name="Bash", rule_content="rm:*"),
        )
        decision = PermissionDecision(
            behavior=PermissionBehavior.DENY.value,
            decision_reason=RuleReason(
                rule=PermissionRuleData.from_dataclass(rule),
                reason="deny rule",
            ),
        )
        logger.log("Bash", {"command": "rm"}, decision, _make_ctx())
        data = json.loads(logger.path.read_text(encoding="utf-8"))
        assert data["reason_type"] == "rule"
        assert data["rule_source"] == "projectSettings"

    def test_other_reason_no_rule_source(self, logger):
        logger.log(
            "Bash", {"command": "ls"},
            _make_decision(reason=OtherReason(reason="no rule")),
            _make_ctx(),
        )
        data = json.loads(logger.path.read_text(encoding="utf-8"))
        assert data["reason_type"] == "other"
        assert data["rule_source"] is None

    def test_none_reason_uses_unknown(self, logger):
        decision = PermissionDecision(
            behavior=PermissionBehavior.ALLOW.value,
            decision_reason=None,
        )
        logger.log("Bash", {"command": "ls"}, decision, _make_ctx())
        data = json.loads(logger.path.read_text(encoding="utf-8"))
        assert data["reason_type"] == "unknown"


# ────────────────────────────────────────────────────────────────────
# failure graceful
# ────────────────────────────────────────────────────────────────────

class TestFailureGraceful:
    def test_log_failure_does_not_raise(self, logger):
        # mock open 抛异常 → log 不应抛
        with patch("builtins.open", side_effect=OSError("disk full")):
            # 不应抛
            logger.log(
                "Bash", {"command": "ls"},
                _make_decision(), _make_ctx(),
            )

    def test_log_returns_none_on_failure(self, logger):
        with patch("builtins.open", side_effect=OSError("disk full")):
            result = logger.log(
                "Bash", {"command": "ls"},
                _make_decision(), _make_ctx(),
            )
        assert result is None

    def test_query_failure_returns_empty(self, logger):
        # 先写一条让 file 存在(exists() 返 True),再 patch open 抛异常
        logger.log(
            "Bash", {"command": "ls"},
            _make_decision(), _make_ctx(),
        )
        with patch("builtins.open", side_effect=OSError("permission denied")):
            result = logger.query()
        assert result == []


# ────────────────────────────────────────────────────────────────────
# query
# ────────────────────────────────────────────────────────────────────

class TestQuery:
    def _populate(self, logger, records):
        for tool, dec in records:
            logger.log(
                tool, {"command": f"cmd-{tool}"},
                _make_decision(
                    behavior=PermissionBehavior[dec.upper()] if dec.upper() in
                    {"ALLOW", "DENY", "ASK"} else PermissionBehavior.ALLOW
                ),
                _make_ctx(),
            )

    def test_returns_all_when_no_filter(self, logger):
        self._populate(logger, [("Bash", "allow"), ("Read", "deny"), ("Edit", "ask")])
        results = logger.query()
        assert len(results) == 3

    def test_filter_by_tool_name(self, logger):
        self._populate(logger, [("Bash", "allow"), ("Read", "deny"), ("Bash", "ask")])
        results = logger.query(tool_name="Bash")
        assert len(results) == 2
        assert all(r.tool_name == "Bash" for r in results)

    def test_filter_by_decision(self, logger):
        self._populate(logger, [("Bash", "allow"), ("Read", "deny"), ("Edit", "deny")])
        results = logger.query(decision="deny")
        assert len(results) == 2
        assert all(r.decision == "deny" for r in results)

    def test_filter_by_since_ts(self, logger):
        logger.log("Bash", {"command": "a"}, _make_decision(), _make_ctx())
        cutoff = time.time() + 0.01  # 稍后
        time.sleep(0.02)
        logger.log("Read", {"command": "b"}, _make_decision(), _make_ctx())
        results = logger.query(since_ts=cutoff)
        assert len(results) == 1
        assert results[0].tool_name == "Read"

    def test_nonexistent_file_returns_empty(self, logger):
        assert logger.query() == []

    def test_skips_malformed_lines(self, logger):
        # 手动写一条坏 JSON + 一条好 JSON
        with open(logger.path, "a", encoding="utf-8") as f:
            f.write("{bad json\n")
            f.write(json.dumps({
                "timestamp": time.time(), "session_id": "x", "tool_name": "Bash",
                "tool_input_hash": "abc", "decision": "allow", "reason_type": "other",
            }) + "\n")
        results = logger.query()
        assert len(results) == 1


# ────────────────────────────────────────────────────────────────────
# 目录权限
# ────────────────────────────────────────────────────────────────────

class TestDirPermissions:
    def test_creates_dir_with_0o700(self, tmp_path):
        session_dir = tmp_path / "sess"
        AuditLogger(str(session_dir))
        assert session_dir.exists()
        mode = session_dir.stat().st_mode & 0o777
        assert mode == 0o700

    def test_creates_nested_dirs(self, tmp_path):
        session_dir = tmp_path / "a" / "b" / "c"
        AuditLogger(str(session_dir))
        assert session_dir.exists()


# ────────────────────────────────────────────────────────────────────
# 全局单例
# ────────────────────────────────────────────────────────────────────

class TestGlobalSingleton:
    def test_init_sets_global(self, tmp_path):
        session_dir = tmp_path / "global-sess"
        al = init_audit_logger(str(session_dir))
        assert get_audit_logger() is al

    def test_get_before_init_returns_none(self):
        assert get_audit_logger() is None

    def test_reset_clears_global(self, tmp_path):
        init_audit_logger(str(tmp_path / "x"))
        reset_audit_logger_for_testing()
        assert get_audit_logger() is None

    def test_init_overwrites_previous(self, tmp_path):
        al1 = init_audit_logger(str(tmp_path / "sess1"))
        al2 = init_audit_logger(str(tmp_path / "sess2"))
        assert get_audit_logger() is al2
        assert al1 is not al2


# ────────────────────────────────────────────────────────────────────
# AuditRecord 序列化
# ────────────────────────────────────────────────────────────────────

class TestAuditRecordSerialization:
    def test_serializable_to_json(self):
        from dataclasses import asdict
        record = AuditRecord(
            timestamp=time.time(),
            session_id="sess",
            tool_name="Bash",
            tool_input_hash="abc123",
            decision="allow",
            reason_type="other",
        )
        data = asdict(record)
        # 应能 JSON 序列化
        json_str = json.dumps(data)
        assert "Bash" in json_str

    def test_unicode_safe(self, logger):
        # 中文 tool_name / reason 正确序列化(ensure_ascii=False)
        logger.log(
            "Bash", {"command": "echo 你好"},
            _make_decision(reason=OtherReason(reason="中文原因")),
            _make_ctx(),
        )
        content = logger.path.read_text(encoding="utf-8")
        assert "中文原因" in content

    def test_round_trip(self, logger):
        logger.log(
            "Bash", {"command": "ls"},
            _make_decision(), _make_ctx(),
        )
        results = logger.query()
        assert len(results) == 1
        assert results[0].tool_name == "Bash"
        assert results[0].decision == "allow"
