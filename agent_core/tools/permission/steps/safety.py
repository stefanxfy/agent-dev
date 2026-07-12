"""Step 1e: safety_check 命中 → ASK。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

from .._logging import permission_logger
from ..safety import safety_check
from ..types import PermissionBehavior, PermissionDecision, SafetyCheckReason

if TYPE_CHECKING:
    from ..steps import PermissionContext


class SafetyStep:
    """1e: safety_check(tool_name, tool_input) == True → ASK(SafetyCheckReason)。

    敏感路径(.agent_data/settings.json 等)或含 secret(sk-ant-*)触发。
    classifier_approvable=False 表示此 ASK 不允许 classifier 自动覆盖。

    对齐 engine 原 Step 1e(logic + 日志 1:1)。
    """

    name = "step_1e_safety_check"

    def __call__(self, ctx: "PermissionContext") -> "Optional[tuple[PermissionDecision, str]]":
        permission_logger.debug(
            "🛡️ [step_1e_safety_check] running for tool=%s", ctx.tool_name,
        )
        if not safety_check(ctx.tool_name, ctx.tool_input):
            return None
        permission_logger.info(
            "🧪 [step_1e_safety_check] HIT tool=%s → ASK (敏感路径或含 secret)",
            ctx.tool_name,
        )
        decision = PermissionDecision(
            behavior=PermissionBehavior.ASK.value,
            decision_reason=SafetyCheckReason(
                reason=f"safety check flagged {ctx.tool_name} input",
                classifier_approvable=False,
            ),
            message="Safety check flagged this action",
        )
        return decision, "step_1e_safety_check"