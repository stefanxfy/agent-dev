"""Step 1d: requires_user_interaction → ASK。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

from .._logging import permission_logger
from ..types import OtherReason, PermissionBehavior, PermissionDecision

if TYPE_CHECKING:
    from ..steps import PermissionContext


class RequiresUserStep:
    """1d: tool.requires_user_interaction == True → ASK(OtherReason)。

    对齐 engine 原 Step 1d(logic + 日志 1:1)。
    """

    name = "step_1d_requires_user"

    def __call__(self, ctx: "PermissionContext") -> "Optional[tuple[PermissionDecision, str]]":
        requires_user = getattr(ctx.tool, "requires_user_interaction", False)
        permission_logger.debug(
            "🛡️ [step_1d_requires_user] tool=%s requires_user_interaction=%s",
            ctx.tool_name, requires_user,
        )
        if not requires_user:
            return None
        permission_logger.info(
            "🛡️ [step_1d_requires_user] HIT tool=%s → ASK", ctx.tool_name,
        )
        decision = PermissionDecision(
            behavior=PermissionBehavior.ASK.value,
            decision_reason=OtherReason(
                reason=f"tool {ctx.tool_name} requires user interaction",
            ),
            message=f"Tool {ctx.tool_name} requires user interaction",
        )
        return decision, "step_1d_requires_user"