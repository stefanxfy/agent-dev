"""Step 1c: tool.check_permissions 调用。"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Optional

from .._logging import permission_logger
from ..types import PermissionBehavior, PermissionDecision

if TYPE_CHECKING:
    from ..steps import PermissionContext

_logger = logging.getLogger(__name__)


class ToolCheckStep:
    """1c: 调 tool.check_permissions(tool_input, context)。

    若 tool 无 check_permissions 方法 → 跳过(返 None)。
    若 tool.check_permissions 返 DENY → 终止返 DENY。
    其他 behavior(ASK/ALLOW/PASSTHROUGH)→ 忽略,继续 pipeline。

    异常被吞掉(降级继续),对齐 engine 原行为。
    """

    name = "step_1c_tool_check"

    def __call__(self, ctx: "PermissionContext") -> "Optional[tuple[PermissionDecision, str]]":
        check_permissions_fn = getattr(ctx.tool, "check_permissions", None)
        permission_logger.debug(
            "🛡️ [step_1c_tool_check] tool=%s has_check_permissions=%s",
            ctx.tool_name, check_permissions_fn is not None,
        )
        if check_permissions_fn is None:
            return None
        try:
            tool_decision = check_permissions_fn(ctx.tool_input, ctx.context)
            if tool_decision.behavior == PermissionBehavior.DENY.value:
                permission_logger.info(
                    "🛡️ [step_1c_tool_check_deny] tool=%s behavior=deny reason=%s",
                    ctx.tool_name, (getattr(tool_decision, "message", "") or "")[:120],
                )
                return tool_decision, "step_1c_tool_check_deny"
        except Exception as e:
            _logger.warning(
                "tool %s check_permissions 抛异常: %s — 降级继续",
                ctx.tool_name, e,
            )
        return None