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
    query_audit,
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


# ────────────────────────────────────────────────────────────────────
# M3 查询扩展 — 多维 filter
# ────────────────────────────────────────────────────────────────────

class TestQueryM3Filters:
    def _make_rule_decision(self, behavior, source=PermissionRuleSource.PROJECT, content="Bash(rm:*)"):
        rule_value = PermissionRuleValue(tool_name="Bash", rule_content=content)
        rule = PermissionRule(
            source=source,
            behavior=behavior,
            value=rule_value,
        )
        return PermissionDecision(
            behavior=behavior.value,
            decision_reason=RuleReason(
                rule=PermissionRuleData.from_dataclass(rule),
                reason=f"hit {source.value}",
            ),
        )

    def test_filter_by_reason_type(self, logger):
        rule_decision = self._make_rule_decision(PermissionBehavior.DENY)
        logger.log("Bash", {"command": "rm"}, rule_decision, _make_ctx())
        logger.log(
            "Bash", {"command": "ls"},
            _make_decision(reason=OtherReason(reason="no rule")),
            _make_ctx(),
        )
        results = logger.query(reason_type="rule")
        assert len(results) == 1
        assert results[0].reason_type == "rule"

        results = logger.query(reason_type="other")
        assert len(results) == 1
        assert results[0].reason_type == "other"

    def test_filter_by_rule_source(self, logger):
        logger.log(
            "Bash", {"command": "rm"},
            self._make_rule_decision(PermissionBehavior.DENY, source=PermissionRuleSource.PROJECT),
            _make_ctx(),
        )
        logger.log(
            "Bash", {"command": "rm"},
            self._make_rule_decision(PermissionBehavior.DENY, source=PermissionRuleSource.LOCAL),
            _make_ctx(),
        )
        results = logger.query(rule_source="projectSettings")
        assert len(results) == 1
        assert results[0].rule_source == "projectSettings"

    def test_filter_by_stage(self, logger):
        logger.log(
            "Bash", {"command": "ls"},
            _make_decision(), _make_ctx(),
            stage="step_1a_global_deny",
        )
        logger.log(
            "Bash", {"command": "rm"},
            _make_decision(), _make_ctx(),
            stage="step_2a_bypass_mode",
        )
        results = logger.query(stage="step_1a_global_deny")
        assert len(results) == 1
        assert results[0].stage == "step_1a_global_deny"

    def test_filter_by_sandbox_used(self, logger):
        ctx_with_sb = _make_ctx(sandbox_enabled=True)
        ctx_no_sb = _make_ctx(sandbox_enabled=False)
        logger.log("Bash", {"command": "ls"}, _make_decision(), ctx_with_sb)
        logger.log("Bash", {"command": "ls"}, _make_decision(), ctx_no_sb)
        results = logger.query(sandbox_used=True)
        assert len(results) == 1
        assert results[0].sandbox_used is True

        results = logger.query(sandbox_used=False)
        assert len(results) == 1
        assert results[0].sandbox_used is False

    def test_limit(self, logger):
        for i in range(5):
            logger.log(
                "Bash", {"command": f"cmd-{i}"},
                _make_decision(), _make_ctx(),
            )
        results = logger.query(limit=3)
        assert len(results) == 3

    def test_reverse(self, logger):
        for i in range(3):
            logger.log(
                "Bash", {"command": f"cmd-{i}"},
                _make_decision(), _make_ctx(),
            )
        forward = logger.query()
        backward = logger.query(reverse=True)
        assert forward[0].tool_input_hash != backward[0].tool_input_hash or forward[0] is not backward[0]
        # 倒序首条 = 正序末条
        assert backward[0].tool_input_hash == forward[-1].tool_input_hash

    def test_combined_filters(self, logger):
        # 2 条 Bash/rule/projectSettings + 1 条 Bash/other + 1 条 Read/rule
        logger.log(
            "Bash", {"command": "rm"},
            self._make_rule_decision(PermissionBehavior.DENY, source=PermissionRuleSource.PROJECT),
            _make_ctx(),
        )
        logger.log(
            "Bash", {"command": "sudo"},
            self._make_rule_decision(PermissionBehavior.DENY, source=PermissionRuleSource.PROJECT),
            _make_ctx(),
        )
        logger.log(
            "Bash", {"command": "ls"},
            _make_decision(reason=OtherReason(reason="default")),
            _make_ctx(),
        )
        logger.log(
            "Read", {"path": "/etc/passwd"},
            self._make_rule_decision(PermissionBehavior.DENY, source=PermissionRuleSource.LOCAL),
            _make_ctx(),
        )
        # tool_name=Bash + reason_type=rule + rule_source=projectSettings → 2 条
        results = logger.query(
            tool_name="Bash", reason_type="rule", rule_source="projectSettings",
        )
        assert len(results) == 2
        assert all(r.tool_name == "Bash" for r in results)
        assert all(r.reason_type == "rule" for r in results)
        assert all(r.rule_source == "projectSettings" for r in results)


# ────────────────────────────────────────────────────────────────────
# M3 查询扩展 — 模块级 query_audit
# ────────────────────────────────────────────────────────────────────

class TestQueryAuditModule:
    def _populate_session(self, sessions_root: Path, session_id: str, records):
        """在 sessions_root/<session_id>/audit.jsonl 写入 records"""
        session_dir = sessions_root / session_id
        session_dir.mkdir(parents=True, exist_ok=True)
        logger = AuditLogger(str(session_dir))
        for tool, behavior, reason_type, stage in records:
            logger.log(
                tool, {"command": f"{tool}-{behavior.value}"},
                _make_decision(behavior=behavior, reason=OtherReason(reason=reason_type)),
                _make_ctx(),
                stage=stage,
            )
        return logger

    def test_resolves_via_explicit_root(self, tmp_path):
        self._populate_session(
            tmp_path, "session-1",
            [("Bash", PermissionBehavior.ALLOW, "default", "step_1c")],
        )
        results = query_audit("session-1", sessions_root=str(tmp_path))
        assert len(results) == 1
        assert results[0].session_id == "session-1"

    def test_nonexistent_session_returns_empty(self, tmp_path):
        results = query_audit("ghost-session", sessions_root=str(tmp_path))
        assert results == []

    def test_query_audit_with_filters(self, tmp_path):
        # 两条 Bash + 一条 Read,含一个带 RuleReason 的记录用于测试 reason_type 维度
        session_dir = tmp_path / "session-A"
        session_dir.mkdir(parents=True, exist_ok=True)
        al = AuditLogger(str(session_dir))
        # 1. Bash allow / other reason
        al.log(
            "Bash", {"command": "ls"}, _make_decision(), _make_ctx(),
            stage="step_1c",
        )
        # 2. Bash deny / rule reason / projectSettings
        rule = PermissionRule(
            source=PermissionRuleSource.PROJECT,
            behavior=PermissionBehavior.DENY,
            value=PermissionRuleValue(tool_name="Bash", rule_content="Bash(rm:*)"),
        )
        al.log(
            "Bash", {"command": "rm"},
            PermissionDecision(
                behavior=PermissionBehavior.DENY.value,
                decision_reason=RuleReason(
                    rule=PermissionRuleData.from_dataclass(rule),
                    reason="hit deny",
                ),
            ),
            _make_ctx(),
            stage="step_1a_global_deny",
        )
        # 3. Read ask / other reason
        al.log(
            "Read", {"path": "/x"},
            _make_decision(behavior=PermissionBehavior.ASK),
            _make_ctx(),
            stage="step_1b_global_ask",
        )

        # tool_name=Bash → 2
        results = query_audit(
            "session-A", tool_name="Bash", sessions_root=str(tmp_path),
        )
        assert len(results) == 2

        # decision=deny → 1
        results = query_audit(
            "session-A", decision="deny", sessions_root=str(tmp_path),
        )
        assert len(results) == 1
        assert results[0].decision == "deny"

        # reason_type=rule + stage 过滤
        results = query_audit(
            "session-A", reason_type="rule", stage="step_1a_global_deny",
            sessions_root=str(tmp_path),
        )
        assert len(results) == 1
        assert results[0].stage == "step_1a_global_deny"
        assert results[0].rule_source == "projectSettings"

    def test_query_audit_limit_and_reverse(self, tmp_path):
        self._populate_session(
            tmp_path, "session-B",
            [(f"Bash", PermissionBehavior.ALLOW, "default", "step_1c") for _ in range(5)],
        )
        results = query_audit(
            "session-B", limit=3, sessions_root=str(tmp_path),
        )
        assert len(results) == 3

    def test_query_audit_does_not_require_global_singleton(self, tmp_path):
        # 即使全局 audit logger 是 None,query_audit 也能正常工作
        reset_audit_logger_for_testing()
        assert get_audit_logger() is None
        self._populate_session(
            tmp_path, "session-C",
            [("Bash", PermissionBehavior.ALLOW, "default", "step_1c")],
        )
        results = query_audit("session-C", sessions_root=str(tmp_path))
        assert len(results) == 1

    def test_corrupt_lines_skipped(self, tmp_path):
        session_dir = tmp_path / "session-D"
        session_dir.mkdir(parents=True, exist_ok=True)
        audit_path = session_dir / "audit.jsonl"
        # 写一条坏 JSON + 一条好 JSON
        with open(audit_path, "w", encoding="utf-8") as f:
            f.write("{not valid json\n")
            f.write(json.dumps({
                "timestamp": time.time(), "session_id": "session-D",
                "tool_name": "Bash", "tool_input_hash": "abc",
                "decision": "allow", "reason_type": "other",
            }) + "\n")
        results = query_audit("session-D", sessions_root=str(tmp_path))
        assert len(results) == 1
        assert results[0].tool_name == "Bash"

    def test_auto_root_picks_existing_cwd_data_sessions(self, tmp_path, monkeypatch):
        # 切到 tmp_path,创建 data/sessions/sX/audit.jsonl
        monkeypatch.chdir(tmp_path)
        (tmp_path / "data" / "sessions" / "auto-session").mkdir(parents=True)
        AuditLogger(str(tmp_path / "data" / "sessions" / "auto-session")).log(
            "Bash", {"command": "ls"}, _make_decision(), _make_ctx(),
        )
        results = query_audit("auto-session")  # 不传 sessions_root
        assert len(results) == 1
