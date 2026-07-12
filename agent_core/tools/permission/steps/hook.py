"""Step 1.5: PreToolUse hook chain。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

from .._logging import permission_logger
from ..types import OtherReason, PermissionBehavior, PermissionDecision

if TYPE_CHECKING:
    from ..steps import PermissionContext


class HookStep:
    """1.5: PreToolUse hook chain(在 safety_check 后 / bypass mode 前)。

    hook 可覆盖后续 global allow(对齐 CC 语义)。
    行为(对齐 engine 原 Step 1.5):
      - DENY → 终止,stage="step_1_5_hook_deny"
      - ASK → 终止,stage="step_1_5_hook_ask"
      - 其他(allow/passthrough)→ 返 None 继续 pipeline
    """

    name = "step_1_5_hook"

    def __call__(self, ctx: "PermissionContext") -> "Optional[tuple[PermissionDecision, str]]":
        if ctx.hook_registry is None:
            return None
        hook_count = len(ctx.hook_registry.list_hooks("PreToolUse"))
        permission_logger.debug(
            "🛡️ [step_1_5_hook] running PreToolUse hook chain (n=%d)",
            hook_count,
        )
        hook_result = ctx.hook_registry.run_pre_tool_use(
            ctx.tool_name, ctx.tool_input, ctx.context,
        )
        if hook_result.behavior == PermissionBehavior.DENY.value:
            permission_logger.info(
                "🛡️ [step_1_5_hook_deny] hook=%s reason=%s",
                hook_result.hook_name, (hook_result.reason or "")[:120],
            )
            decision = PermissionDecision(
                behavior=PermissionBehavior.DENY.value,
                decision_reason=OtherReason(
                    reason=f"hook {hook_result.hook_name} denied: "
                           f"{hook_result.reason or 'no reason'}",
                ),
                message=hook_result.reason,
            )
            return decision, "step_1_5_hook_deny"
        if hook_result.behavior == PermissionBehavior.ASK.value:
            permission_logger.info(
                "🛡️ [step_1_5_hook_ask] hook=%s reason=%s",
                hook_result.hook_name, (hook_result.reason or "")[:120],
            )
            updated = hook_result.updated_input or ctx.tool_input
            decision = PermissionDecision(
                behavior=PermissionBehavior.ASK.value,
                decision_reason=OtherReason(
                    reason=f"hook {hook_result.hook_name} asked: "
                           f"{hook_result.reason or 'no reason'}",
                ),
                updated_input=updated,
                message=hook_result.reason,
            )
            return decision, "step_1_5_hook_ask"
        return None