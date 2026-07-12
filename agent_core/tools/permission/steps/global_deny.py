"""Step 1a: 全局 deny rule 匹配(内容级,T-M1 修)。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

from .._logging import permission_logger
from ..types import (
    PermissionBehavior,
    PermissionDecision,
    PermissionRuleData,
    RuleReason,
)

if TYPE_CHECKING:
    from ..steps import PermissionContext


class GlobalDenyStep:
    """1a: 全局 deny rule 匹配。命中 → DENY(RuleReason);否则 None 继续 pipeline。

    对齐 engine 原 Step 1a(logic + 日志 1:1):
      - 进入:debug [step_1a_global_deny] checking
      - 命中:info [step_1a_global_deny] HIT
      - 通过 ctx.rule_checker.check_deny 做 source 优先级 + 内容级匹配
    """

    name = "step_1a_global_deny"

    def __call__(self, ctx: "PermissionContext") -> "Optional[tuple[PermissionDecision, str]]":
        permission_logger.debug(
            "🛡️ [step_1a_global_deny] checking deny rules for tool=%s", ctx.tool_name,
        )
        deny_rule = ctx.rule_checker.check_deny(ctx.tool_name, ctx.tool_input)
        if deny_rule is None:
            return None
        permission_logger.info(
            "🛡️ [step_1a_global_deny] HIT tool=%s rule=%s", ctx.tool_name, deny_rule,
        )
        decision = PermissionDecision(
            behavior=PermissionBehavior.DENY.value,
            decision_reason=RuleReason(
                rule=PermissionRuleData.from_dataclass(deny_rule),
                reason=f"global deny rule: {deny_rule}",
            ),
            message=f"Denied by rule: {deny_rule}",
        )
        return decision, "step_1a_global_deny"
