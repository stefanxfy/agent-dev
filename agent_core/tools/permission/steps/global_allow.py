"""Step 2b: tool 全局 allow rule 匹配(内容级,T-M1 修)。"""

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


class GlobalAllowStep:
    """2b: tool 全局 allow rule。命中 → ALLOW(RuleReason);否则 None 继续。

    对齐 engine 原 Step 2b(logic + 日志 1:1)。注意:此 step 在 bypass mode 之后,
    即 bypass 优先于 allow rule(与 CC 语义一致)。
    """

    name = "step_2b_global_allow"

    def __call__(self, ctx: "PermissionContext") -> "Optional[tuple[PermissionDecision, str]]":
        permission_logger.debug(
            "🛡️ [step_2b_global_allow] checking allow rules for tool=%s", ctx.tool_name,
        )
        allow_rule = ctx.rule_checker.check_allow(ctx.tool_name, ctx.tool_input)
        if allow_rule is None:
            return None
        permission_logger.info(
            "🛡️ [step_2b_global_allow] HIT tool=%s rule=%s", ctx.tool_name, allow_rule,
        )
        decision = PermissionDecision(
            behavior=PermissionBehavior.ALLOW.value,
            decision_reason=RuleReason(
                rule=PermissionRuleData.from_dataclass(allow_rule),
                reason=f"global allow rule: {allow_rule}",
            ),
        )
        return decision, "step_2b_global_allow"
