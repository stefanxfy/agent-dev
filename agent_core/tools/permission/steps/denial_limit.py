"""Step 6: denial limit 检查。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

from .._logging import permission_logger
from ..denial import check_denial_limit

if TYPE_CHECKING:
    from ..steps import PermissionContext


class DenialLimitStep:
    """6: 连续 deny 次数达上限 → 返 check_denial_limit 的 decision(终止 pipeline)。

    对齐 engine 原 Step 6(logic + 日志 1:1)。check_denial_limit 返 None 表示未达上限,
    返 PermissionDecision 表示已达上限(通常是 DENY + 提示)。
    """

    name = "step_6_denial_limit"

    def __call__(self, ctx: "PermissionContext") -> "Optional[tuple[PermissionDecision, str]]":
        if ctx.denial_state is None:
            return None
        consecutive = getattr(ctx.denial_state, "consecutive_denials", 0)
        total = getattr(ctx.denial_state, "total_denials", 0)
        permission_logger.debug(
            "🛡️ [step_6_denial_limit] consecutive=%d total=%d",
            consecutive, total,
        )
        limit_decision = check_denial_limit(ctx.denial_state)
        if limit_decision is None:
            return None
        permission_logger.info(
            "🛡️ [step_6_denial_limit] HIT tool=%s consecutive=%d total=%d",
            ctx.tool_name, consecutive, total,
        )
        return limit_decision, "step_6_denial_limit"