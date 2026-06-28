"""
独立审计通道 — 所有 permission / sandbox / hook 决策的可追溯记录
对齐 Claude Code src/services/analytics/analyticsHooks.ts + doc §4.8

为什么不混到 session.jsonl:
- session.jsonl 可被 `claude --continue` 裁剪(用户操作)
- audit logger 只追加,不可变;事后审计 + 合规检查的 source of truth
- 安全事件需要 atomic write(避免 partial record 导致审计丢失)

写入策略:
- 每个 tool_use 触发一次 audit record(decision 后立即写,不延迟)
- 文件名:data/sessions/<id>/audit.jsonl
- 字段:timestamp / tool_name / tool_input_hash / decision / reason /
       rule_source / sandbox_used / hook_chain / classifier_used
- tool_input 不存原文(可能含密钥);只存 sha256(tool_input)[:16]
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

from .permission_types import (
    PermissionBehavior,
    PermissionDecision,
    ToolPermissionContext,
)

logger = logging.getLogger(__name__)


# ────────────────────────────────────────────────────────────────────
# AuditRecord — 单次决策记录(对齐 CC telemetry 字段集)
# ────────────────────────────────────────────────────────────────────

@dataclass
class AuditRecord:
    """
    单次 tool_use 的安全决策记录(对齐 CC telemetry 字段集 + doc §4.8)

    字段:
    - timestamp: time.time() 决策时刻
    - session_id: 当前 session
    - tool_name: "Bash" / "Read" / ...
    - tool_input_hash: sha256(tool_input)[:16],不存原文(防泄密钥)
    - decision: PermissionBehavior 值
    - reason_type: PermissionDecisionReason.type
    - reason_detail: reason 细节
    - rule_source: 命中 rule 的 source
    - mode: PermissionMode 值
    - sandbox_used: 是否走 sandbox
    - hook_chain: 执行的 hook 名列表
    - classifier_used: 是否调过 classifier
    - classifier_decision: classifier 返 allow/deny/None
    - denial_state: denial_tracking 当前 state
    """
    timestamp: float
    session_id: str
    tool_name: str
    tool_input_hash: str
    decision: str
    reason_type: str
    reason_detail: Optional[str] = None
    rule_source: Optional[str] = None
    mode: Optional[str] = None
    sandbox_used: bool = False
    hook_chain: list[str] = field(default_factory=list)
    classifier_used: bool = False
    classifier_decision: Optional[str] = None
    denial_state: Optional[dict] = None


def compute_tool_input_hash(tool_input: dict) -> str:
    """
    计算 tool_input 的 sha256 hash(前 16 字符)
    对齐 doc §4.8:不存原文(可能含密钥),只存 hash

    sort_keys=True 保证相同 input → 相同 hash(便于去重 / 比对)
    """
    try:
        serialized = json.dumps(tool_input, sort_keys=True, default=str)
    except (TypeError, ValueError) as e:
        logger.warning("tool_input 序列化失败,用 repr fallback: %s", e)
        serialized = repr(tool_input)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()[:16]


# ────────────────────────────────────────────────────────────────────
# AuditLogger — 单例 + per-session append-only JSONL
# ────────────────────────────────────────────────────────────────────

class AuditLogger:
    """
    审计日志记录器(对齐 CC analyticsHooks.ts firePermissionDecision)

    生命周期:
      - __init__(session_data_dir):创建 audit.jsonl 文件(dir mode 0o700)
      - log(...):atomic append 一条 record(flush + fsync)
      - query(...):事后审计读取(M2 简化 filter)

    铁律(对齐 doc §4.8):audit 失败绝不阻断主流程(try/except 全包)
    """

    def __init__(self, session_data_dir: str):
        """
        Args:
            session_data_dir: session 数据目录(data/sessions/<id>/)
        """
        self.session_data_dir = Path(session_data_dir)
        self.session_id = self.session_data_dir.name
        self.path = self.session_data_dir / "audit.jsonl"
        # 创建父目录 mode 0o700(对齐 doc §4.8)
        try:
            self.path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        except OSError as e:
            logger.warning("audit 目录创建失败: %s", e)

    def log(
        self,
        tool_name: str,
        tool_input: dict,
        decision: PermissionDecision,
        context: ToolPermissionContext,
        hook_chain: Optional[list[str]] = None,
        classifier_used: bool = False,
        classifier_decision: Optional[Any] = None,
        denial_state: Optional[dict] = None,
        sandbox_used: Optional[bool] = None,
    ) -> None:
        """
        记录一次 permission 决策(atomic append,不抛异常影响主流程)
        对齐 CC analyticsHooks.ts:firePermissionDecision

        Args:
            tool_name: 工具名
            tool_input: 工具输入(只存 hash,不存原文)
            decision: PermissionDecision
            context: 权限上下文
            hook_chain: 执行的 hook 名列表
            classifier_used: 是否调过 classifier
            classifier_decision: classifier 返 allow/deny/None
            denial_state: denial_tracking 当前 state
            sandbox_used: 是否走 sandbox(None → 从 context.sandbox_enabled 推断)
        """
        try:
            tool_input_hash = compute_tool_input_hash(tool_input or {})

            reason = decision.decision_reason
            reason_type = "unknown"
            reason_detail = None
            rule_source = None
            if reason is not None:
                reason_type = getattr(reason, "type", "unknown") or "unknown"
                reason_detail = getattr(reason, "reason", None)
                # 从 RuleReason 提取 source
                rule = getattr(reason, "rule", None)
                if rule is not None:
                    source_val = getattr(rule, "source", None)
                    if source_val is not None:
                        rule_source = source_val.value if hasattr(source_val, "value") else str(source_val)

            # sandbox_used 推断
            effective_sandbox_used = (
                sandbox_used if sandbox_used is not None
                else getattr(context, "sandbox_enabled", False)
            )

            # mode 推断
            mode_val = getattr(context, "mode", None)

            # classifier_decision 转字符串
            classifier_decision_str: Optional[str] = None
            if classifier_decision is not None:
                if hasattr(classifier_decision, "value"):
                    classifier_decision_str = classifier_decision.value
                else:
                    classifier_decision_str = str(classifier_decision)

            record = AuditRecord(
                timestamp=time.time(),
                session_id=self.session_id,
                tool_name=tool_name,
                tool_input_hash=tool_input_hash,
                decision=decision.behavior if isinstance(decision.behavior, str)
                else getattr(decision.behavior, "value", str(decision.behavior)),
                reason_type=reason_type,
                reason_detail=reason_detail,
                rule_source=rule_source,
                mode=mode_val if isinstance(mode_val, str)
                else getattr(mode_val, "value", None),
                sandbox_used=bool(effective_sandbox_used),
                hook_chain=list(hook_chain or []),
                classifier_used=bool(classifier_used),
                classifier_decision=classifier_decision_str,
                denial_state=denial_state,
            )

            self._write_record(record)
        except Exception as e:
            # 审计失败绝不能影响主流程(对齐 CC 'audit must never block decision')
            logger.warning("audit log failed: %s", e)

    def _write_record(self, record: AuditRecord) -> None:
        """atomic append 一条 record(flush + fsync 保证 durability)"""
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(asdict(record), ensure_ascii=False) + "\n")
            f.flush()
            os.fsync(f.fileno())  # atomic durability

    # ── 查询接口(M2 简化版,M3 再加复杂查询)─────────────────

    def query(
        self,
        since_ts: Optional[float] = None,
        tool_name: Optional[str] = None,
        decision: Optional[str] = None,
    ) -> list[AuditRecord]:
        """
        事后审计查询(M2 简化:读 + filter)

        Args:
            since_ts: 只返 timestamp >= since_ts 的记录
            tool_name: 按 tool 名 filter
            decision: 按 decision filter

        Returns:
            匹配的 AuditRecord 列表(按时间序)
        """
        if not self.path.exists():
            return []
        results: list[AuditRecord] = []
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        data = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    # filter
                    if since_ts is not None and data.get("timestamp", 0) < since_ts:
                        continue
                    if tool_name is not None and data.get("tool_name") != tool_name:
                        continue
                    if decision is not None and data.get("decision") != decision:
                        continue
                    results.append(AuditRecord(**data))
        except OSError as e:
            logger.warning("audit query 失败: %s", e)
        return results


# ────────────────────────────────────────────────────────────────────
# 全局单例(对齐 CC — 由 ReactAgent 注入 session_data_dir 后初始化)
# ────────────────────────────────────────────────────────────────────

_audit_logger: Optional[AuditLogger] = None


def init_audit_logger(session_data_dir: str) -> AuditLogger:
    """
    初始化全局 audit logger 单例
    对齐 doc §4.8:由 ReactAgent / web/app.py 在 session 启动时注入

    Args:
        session_data_dir: session 数据目录

    Returns:
        初始化后的 AuditLogger
    """
    global _audit_logger
    _audit_logger = AuditLogger(session_data_dir)
    return _audit_logger


def get_audit_logger() -> Optional[AuditLogger]:
    """获取全局 audit logger 单例(未初始化返 None)"""
    return _audit_logger


def reset_audit_logger_for_testing() -> None:
    """测试专用:重置全局单例(production 不调)"""
    global _audit_logger
    _audit_logger = None
