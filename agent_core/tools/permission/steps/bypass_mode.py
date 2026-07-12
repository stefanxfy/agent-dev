"""Step 2a: bypass mode → ALLOW。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

from .._logging import permission_logger
from ..types import (
    ModeReason,
    PermissionBehavior,
    PermissionDecision,
    PermissionMode,
)

if TYPE_CHECKING:
    from ..steps import PermissionContext


class BypassModeStep:
    """2a: mode == bypassPermissions → ALLOW(ModeReason)。

    对齐 engine 原 Step 2a(logic + 日志 1:1)。
    位置在 hook 之后,allow rule 之前(bypass 最高优先级,跳过所有 rule 匹配)。
    """

    name = "step_2a_bypass_mode"

    def __call__(self, ctx: "PermissionContext") -> "Optional[tuple[PermissionDecision, str]]":
        permission_logger.debug(
            "🛡️ [step_2a_bypass] mode=%s", ctx.context.mode,
        )
        if ctx.context.mode != PermissionMode.BYPASS.value:
            return None
        permission_logger.info(
            "🛡️ [step_2a_bypass_mode] mode=bypassPermissions → ALLOW tool=%s",
            ctx.tool_name,
        )
        decision = PermissionDecision(
            behavior=PermissionBehavior.ALLOW.value,
            decision_reason=ModeReason(
                mode=PermissionMode.BYPASS.value,
                reason="bypassPermissions mode",
            ),
        )
        return decision, "step_2a_bypass_mode"