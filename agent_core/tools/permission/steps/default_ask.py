"""Step 7: 默认 ASK(passthrough)。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

from .._logging import permission_logger
from ..types import OtherReason, PermissionBehavior, PermissionDecision

if TYPE_CHECKING:
    from ..steps import PermissionContext


class DefaultAskStep:
    """7: 无规则匹配 → 默认 ASK(OtherReason)。

    这是 pipeline 的兜底 step,总是返非 None(ASK),保证 orchestrator 不会走到
    "no step returned a decision" 的 raise。

    对齐 engine 原 Step 7(logic + 日志 1:1)。
    """

    name = "step_7_default_ask"

    def __call__(self, ctx: "PermissionContext") -> "Optional[tuple[PermissionDecision, str]]":
        permission_logger.info(
            "🛡️ [step_7_default_ask] no matching rule → ASK tool=%s", ctx.tool_name,
        )
        decision = PermissionDecision(
            behavior=PermissionBehavior.ASK.value,
            decision_reason=OtherReason(
                reason="no matching rule, default ask",
            ),
            message="No matching permission rule",
        )
        return decision, "step_7_default_ask"