"""Step 1b: 全局 ask rule 匹配(内容级,T-M1 修)。"""

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


class GlobalAskStep:
    """1b: 全局 ask rule 匹配。命中 → ASK(RuleReason);否则 None 继续。

    对齐 engine 原 Step 1b(logic + 日志 1:1)。
    """

    name = "step_1b_global_ask"

    def __call__(self, ctx: "PermissionContext") -> "Optional[tuple[PermissionDecision, str]]":
        permission_logger.debug(
            "🛡️ [step_1b_global_ask] checking ask rules for tool=%s", ctx.tool_name,
        )
        ask_rule = ctx.rule_checker.check_ask(ctx.tool_name, ctx.tool_input)
        if ask_rule is None:
            return None
        permission_logger.info(
            "🛡️ [step_1b_global_ask] HIT tool=%s rule=%s", ctx.tool_name, ask_rule,
        )
        decision = PermissionDecision(
            behavior=PermissionBehavior.ASK.value,
            decision_reason=RuleReason(
                rule=PermissionRuleData.from_dataclass(ask_rule),
                reason=f"global ask rule: {ask_rule}",
            ),
            message=f"Asked by rule: {ask_rule}",
        )
        return decision, "step_1b_global_ask"
