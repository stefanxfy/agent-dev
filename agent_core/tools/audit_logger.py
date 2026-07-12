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

from .permission.types import (
    PermissionBehavior,
    PermissionDecision,
    ToolPermissionContext,
)

logger = logging.getLogger(__name__)

# 📋 audit 子系统 logger — 与 AGENT_LOG_AUDIT env 联动
audit_logger = logging.getLogger("agent_core.audit")


# ────────────────────────────────────────────────────────────────────
# 默认 sessions root 解析(对齐 doc §4.8 + SessionStorage 默认路径)
# ────────────────────────────────────────────────────────────────────


def _resolve_sessions_root(sessions_root: Optional[str]) -> Path:
    """
    解析 sessions 根目录,优先级(对齐 SessionStorage._get_default_data_dir):
      1. 显式传入的 sessions_root
      2. 环境变量 AGENT_DATA_DIR / sessions 子目录
      3. 仓库级 ./data/sessions/(开发默认)
      4. ~/.agent_data/sessions/(用户级)
    """
    if sessions_root:
        return Path(sessions_root)
    env = os.environ.get("AGENT_DATA_DIR")
    if env:
        return Path(env) / "sessions"
    cwd_candidate = Path("data") / "sessions"
    if cwd_candidate.exists():
        return cwd_candidate
    return Path.home() / ".agent_data" / "sessions"


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
    - stage: PermissionEngine 决策阶段(step_1a_global_deny 等,审计追溯用)
    - hook_chain: 执行的 hook 名列表
    - classifier_used: 是否调过 classifier
    - classifier_decision: classifier 返 allow/deny/None
    - denial_state: denial_tracking 当前 state
    - tool_category: builtin / shell / read / mcp 等(ToolDef.category;Optional 向后兼容)
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
    stage: Optional[str] = None
    hook_chain: list[str] = field(default_factory=list)
    classifier_used: bool = False
    classifier_decision: Optional[str] = None
    denial_state: Optional[dict] = None
    # 🆕 T-C2:tool_category 用于区分 builtin / shell / read / mcp 等,
    # 让 MCP 工具调用在审计中可识别(原 0 消费,2026-07 修复)
    tool_category: Optional[str] = None


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
        audit_logger.info(
            "📋 [audit_logger_init] session_id=%s path=%s",
            self.session_id, self.path,
        )

    def log(
        self,
        tool_name: str,
        tool_input: dict,
        decision: PermissionDecision,
        context: Optional[ToolPermissionContext] = None,
        hook_chain: Optional[list[str]] = None,
        classifier_used: bool = False,
        classifier_decision: Optional[Any] = None,
        denial_state: Optional[dict] = None,
        sandbox_used: Optional[bool] = None,
        stage: Optional[str] = None,
        tool_category: Optional[str] = None,
    ) -> None:
        """
        记录一次 permission 决策(atomic append,不抛异常影响主流程)
        对齐 CC analyticsHooks.ts:firePermissionDecision

        Args:
            tool_name: 工具名
            tool_input: 工具输入(只存 hash,不存原文)
            decision: PermissionDecision
            context: 权限上下文(可空,但传了才能记录 mode/sandbox)
            hook_chain: 执行的 hook 名列表
            classifier_used: 是否调过 classifier
            classifier_decision: classifier 返 allow/deny/None
            denial_state: denial_tracking 当前 state
            sandbox_used: 是否走 sandbox(None → 从 context.sandbox_enabled 推断)
            stage: PermissionEngine 决策阶段(step_1a_global_deny 等)
            tool_category: 工具类别(builtin / shell / read / mcp …)None 向后兼容
        """
        try:
            audit_logger.debug(
                "📋 [audit_log_entry] tool=%s behavior=%s",
                tool_name, getattr(decision, "behavior", "?"),
            )
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
                else (getattr(context, "sandbox_enabled", False) if context else False)
            )

            # mode 推断
            mode_val = getattr(context, "mode", None) if context else None

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
                stage=stage,
                hook_chain=list(hook_chain or []),
                classifier_used=bool(classifier_used),
                classifier_decision=classifier_decision_str,
                denial_state=denial_state,
                tool_category=tool_category,
            )
            audit_logger.info(
                "📋 [audit_record_built] tool=%s decision=%s reason_type=%s "
                "stage=%s sandbox_used=%s classifier_used=%s",
                tool_name, record.decision, record.reason_type, record.stage,
                record.sandbox_used, record.classifier_used,
            )

            self._write_record(record)
        except Exception as e:
            # 审计失败绝不能影响主流程(对齐 CC 'audit must never block decision')
            logger.warning("audit log failed: %s", e)

    def _write_record(self, record: AuditRecord) -> None:
        """atomic append 一条 record(flush + fsync 保证 durability)"""
        line = json.dumps(asdict(record), ensure_ascii=False)
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
            f.flush()
            os.fsync(f.fileno())  # atomic durability
        audit_logger.debug(
            "📋 [audit_record_written] tool=%s bytes=%d path=%s",
            record.tool_name, len(line), self.path,
        )

    # ── 查询接口(M2 简化版 + M3 增强)─────────────────
    #
    # M2:M2 阶段实现 since_ts / tool_name / decision 三个基础 filter
    # M3 (本版) 新增 reason_type / rule_source / stage / sandbox_used /
    #     limit / reverse 等维度,补全 doc §4.8 查询接口

    def query(
        self,
        since_ts: Optional[float] = None,
        tool_name: Optional[str] = None,
        decision: Optional[str] = None,
        reason_type: Optional[str] = None,
        rule_source: Optional[str] = None,
        stage: Optional[str] = None,
        sandbox_used: Optional[bool] = None,
        limit: Optional[int] = None,
        reverse: bool = False,
    ) -> list[AuditRecord]:
        """
        事后审计查询实例方法(读 self.path)

        Args:
            since_ts: 仅返 timestamp >= since_ts 的记录
            tool_name: 按 tool 名精确匹配
            decision: 按 decision 精确匹配(allow / deny / ask / passthrough)
            reason_type: 按 reason_type 精确匹配(rule/mode/hook/...)
            rule_source: 按 rule_source 精确匹配(仅 reason_type=rule 的记录有意义)
            stage: 按 PermissionEngine 阶段匹配(step_1a_global_deny 等)
            sandbox_used: True/False 过滤是否走沙箱
            limit: 最多返 N 条(None=全部)
            reverse: True → 倒序(最新在前);默认 False(按写入顺序)

        Returns:
            匹配的 AuditRecord 列表;文件不存在 / 异常 → []
        """
        records = self._read_all_records()
        return _filter_records(
            records,
            since_ts=since_ts,
            tool_name=tool_name,
            decision=decision,
            reason_type=reason_type,
            rule_source=rule_source,
            stage=stage,
            sandbox_used=sandbox_used,
            limit=limit,
            reverse=reverse,
        )

    def _read_all_records(self) -> list[AuditRecord]:
        """读 self.path 全部记录(malformed line 跳过)"""
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
                    try:
                        results.append(AuditRecord(**data))
                    except (TypeError, ValueError) as e:
                        # 字段不匹配(旧 schema / 损坏 schema)→ 跳过
                        logger.warning("audit record 字段不匹配,跳过: %s", e)
        except OSError as e:
            logger.warning("audit query 读文件失败: %s", e)
        return results


# ────────────────────────────────────────────────────────────────────
# M3 查询扩展 — 多维 filter helper + 模块级 query_audit
# ────────────────────────────────────────────────────────────────────


def _filter_records(
    records: list[AuditRecord],
    *,
    since_ts: Optional[float] = None,
    tool_name: Optional[str] = None,
    decision: Optional[str] = None,
    reason_type: Optional[str] = None,
    rule_source: Optional[str] = None,
    stage: Optional[str] = None,
    sandbox_used: Optional[bool] = None,
    limit: Optional[int] = None,
    reverse: bool = False,
) -> list[AuditRecord]:
    """
    多维 filter helper(独立函数,无副作用,便于跨 session 共用)

    字段过滤语义:None = 不参与过滤;值 = 精确匹配(字符串)或严格 bool 匹配
    时间序:默认按文件写入顺序;reverse=True 倒序(最新在前)
    limit:None = 不限;正整数 = 最多返 N 条
    """
    results: list[AuditRecord] = []
    for r in records:
        if since_ts is not None and r.timestamp < since_ts:
            continue
        if tool_name is not None and r.tool_name != tool_name:
            continue
        if decision is not None and r.decision != decision:
            continue
        if reason_type is not None and r.reason_type != reason_type:
            continue
        if rule_source is not None and r.rule_source != rule_source:
            continue
        if stage is not None and r.stage != stage:
            continue
        if sandbox_used is not None and r.sandbox_used != sandbox_used:
            continue
        results.append(r)
    if reverse:
        results.reverse()
    if limit is not None and limit >= 0:
        results = results[:limit]
    return results


def query_audit(
    session_id: str,
    *,
    tool_name: Optional[str] = None,
    decision: Optional[str] = None,
    reason_type: Optional[str] = None,
    rule_source: Optional[str] = None,
    stage: Optional[str] = None,
    sandbox_used: Optional[bool] = None,
    since_ts: Optional[float] = None,
    limit: Optional[int] = None,
    reverse: bool = False,
    sessions_root: Optional[str] = None,
) -> list[AuditRecord]:
    """
    事后审计查询(模块级,doc §4.8 spec)

    不依赖单例/实例,直接根据 session_id 找到对应 audit.jsonl 读 + 多维过滤。
    适合后台审计、合规导出、UI 表格渲染等场景。

    Args:
        session_id: 目标 session ID(对应 data/sessions/<id>/audit.jsonl)
        tool_name / decision / reason_type / rule_source / stage /
            sandbox_used / since_ts: 与 AuditLogger.query 同语义
        limit: 最多返 N 条(None=全部)
        reverse: True → 倒序
        sessions_root: 显式指定 sessions 根目录;None → 见 _resolve_sessions_root

    Returns:
        匹配的 AuditRecord 列表;session 不存在 / 读失败 → []
    """
    root = _resolve_sessions_root(sessions_root)
    audit_path = root / session_id / "audit.jsonl"
    if not audit_path.exists():
        # sessions_root 可能直接是 data 目录本身(sessions 子层)
        alt = root.parent / "sessions" / session_id / "audit.jsonl" if root.name != "sessions" else None
        if alt is not None and alt.exists():
            audit_path = alt
        else:
            return []
    # 复用 AuditLogger 的 _read_all_records(单实例即可,不需写盘)
    reader = AuditLogger(str(audit_path.parent))
    reader.path = audit_path  # path 必须用 <session>/audit.jsonl
    records = reader._read_all_records()
    return _filter_records(
        records,
        since_ts=since_ts,
        tool_name=tool_name,
        decision=decision,
        reason_type=reason_type,
        rule_source=rule_source,
        stage=stage,
        sandbox_used=sandbox_used,
        limit=limit,
        reverse=reverse,
    )


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
