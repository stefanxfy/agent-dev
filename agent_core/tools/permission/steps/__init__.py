"""agent_core.tools.permission.steps 子包(D.3a)。

check_permissions 拆 step handler 链的基础设施:
  - PermissionStep Protocol(单 step 协议)
  - PermissionContext dataclass(step 间共享的可变上下文)

D.3a 阶段:只定义类型,不动 check_permissions 行为。
D.3b/c 才把 13 个 inline step 提取为独立 step 类 + 重写 check_permissions 为 orchestrator。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Optional, Protocol, runtime_checkable

if TYPE_CHECKING:
    from agent_core.tools.permission.denial import DenialTrackingState
    from agent_core.tools.permission.hook import HookRegistry
    from agent_core.tools.permission.classifier import HaikuClassifier
    from agent_core.tools.permission.rule_checker import RuleChecker
    from agent_core.tools.permission.types import (
        PermissionDecision,
        ToolPermissionContext,
    )


@dataclass
class PermissionContext:
    """单次 check_permissions 的可变上下文(step 间共享)。

    设计:
      - 输入字段(tool / tool_input / messages)只读 — step 不应改写它们
      - engine 引用字段(context / hook_registry / classifier / denial_state /
        audit_logger / provider)指向 PermissionEngine 当前状态,step 可读
      - 这是 D.3a 的纯类型定义;D.3c 重写 check_permissions 时由 orchestrator 构造
    """

    # ── 输入(只读,无默认值,必须显式传入)──
    tool: Any                          # duck-typed ToolDef(取 .name / .category / .check_permissions)
    tool_input: dict

    # ── engine 引用(可变,step 可读 + 调用 helper)──
    context: "ToolPermissionContext"        # permission mode / rules

    # ── 可选输入 / 引用(都有默认值)──
    messages: Optional[list[dict]] = None   # classifier 用,可空
    hook_registry: Optional["HookRegistry"] = None
    classifier: Optional["HaikuClassifier"] = None
    denial_state: Optional["DenialTrackingState"] = None
    audit_logger: Any = None                # AuditLogger or None
    provider: str = "anthropic"
    rule_checker: Optional["RuleChecker"] = None   # 内容级 rule 匹配器(D.3b 注入)

    # ── 派生(orchestrator 预算好,避免每 step 重复 getattr)──
    tool_name: str = "unknown"

    @property
    def mode(self) -> str:
        """当前 permission mode 的便捷访问。"""
        return getattr(self.context, "mode", "?")


@runtime_checkable
class PermissionStep(Protocol):
    """单个决策 step 的协议(check_permissions pipeline 节点)。

    契约:
      - __call__(ctx) 返回 Optional[tuple[PermissionDecision, str]]
      - 返回 None → continue pipeline(下一个 step)
      - 返回 (decision, stage) → 终止 pipeline:
          * decision: PermissionDecision
          * stage: 细粒度决策点标识(如 step_1c_bash_deny / step_4_classifier_allow),
                   写入 audit.jsonl 的 stage 字段,对齐原 monolith 粒度
      - 每个 step 类应有 `name` 属性(粗粒度标签,日志/调试用)
      - step 不应改 ctx 的输入字段(tool / tool_input / messages)
      - step 不直接调 audit_logger(denial_state 更新 + audit 写在 _log_and_return)
      - step 应在关键节点打 debug(进入)/ info(命中)日志(对齐 CLAUDE.md 核心路径日志要求)
    """

    name: str

    def __call__(self, ctx: PermissionContext) -> "Optional[tuple[PermissionDecision, str]]":
        ...
