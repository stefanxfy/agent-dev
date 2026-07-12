"""Step 4: mode 后处理(auto classifier / async agent / dontAsk)。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

from .._logging import permission_logger
from ..classifier import is_classifier_enabled
from ..types import (
    AsyncAgentReason,
    ClassifierReason,
    ModeReason,
    PermissionBehavior,
    PermissionDecision,
    PermissionMode,
)

if TYPE_CHECKING:
    from ..steps import PermissionContext


class ModePostProcessStep:
    """4: mode 后处理三分支(按顺序短路)。

    对齐 engine 原 Step 4(logic + 日志 1:1):
      a) auto mode + classifier enabled + available + should_block → DENY
         stage="step_4_classifier_deny"
      b) auto mode + classifier enabled + available + not should_block → ALLOW
         stage="step_4_classifier_allow"
      c) auto mode + classifier unavailable → fall through(不终止)
      d) should_avoid_permission_prompts(后台 agent)→ DENY
         stage="step_4_async_agent"
      e) dontAsk mode → DENY
         stage="step_4_dontask"

    classifier 时长(duration_ms)日志保留(与原行为一致)。
    """

    name = "step_4_mode_postprocess"

    def __call__(self, ctx: "PermissionContext") -> "Optional[tuple[PermissionDecision, str]]":
        # ── 分支 a/b/c:auto mode classifier ──
        if ctx.context.mode == PermissionMode.AUTO.value:
            classifier_enabled = is_classifier_enabled(
                provider=ctx.provider,
                mode=PermissionMode.AUTO,
                no_settings_match=ctx.context.no_settings_match,
            )
            permission_logger.debug(
                "🛡️ [step_4_classifier] mode=%s classifier_enabled=%s",
                ctx.context.mode, classifier_enabled,
            )
            if classifier_enabled and ctx.classifier is not None:
                import time as _t
                _t0 = _t.time()
                result = ctx.classifier.classify(
                    ctx.messages or [],
                    ctx.tool_name,
                    ctx.tool_input,
                    ctx.context,
                )
                _ms = (_t.time() - _t0) * 1000
                permission_logger.info(
                    "🤖 [step_4_classifier_result] tool=%s should_block=%s "
                    "unavailable=%s duration_ms=%.1f reason=%s",
                    ctx.tool_name, result.should_block, result.unavailable,
                    _ms, (result.reason or "")[:120],
                )
                if not result.unavailable:
                    if result.should_block:
                        decision = PermissionDecision(
                            behavior=PermissionBehavior.DENY.value,
                            decision_reason=ClassifierReason(
                                classifier=result.model,
                                reason=result.reason,
                            ),
                            message=f"Classifier denied: {result.reason}",
                        )
                        return decision, "step_4_classifier_deny"
                    decision = PermissionDecision(
                        behavior=PermissionBehavior.ALLOW.value,
                        decision_reason=ClassifierReason(
                            classifier=result.model,
                            reason=result.reason,
                        ),
                        message=f"Classifier allowed: {result.reason}",
                    )
                    return decision, "step_4_classifier_allow"
                # unavailable → fall through 到分支 d/e

        # ── 分支 d:async agent(auto-deny)──
        if ctx.context.should_avoid_permission_prompts:
            permission_logger.info(
                "🛡️ [step_4_async_agent] should_avoid_permission_prompts=True → DENY tool=%s",
                ctx.tool_name,
            )
            decision = PermissionDecision(
                behavior=PermissionBehavior.DENY.value,
                decision_reason=AsyncAgentReason(
                    reason="async agent without user prompts",
                ),
            )
            return decision, "step_4_async_agent"

        # ── 分支 e:dontAsk → DENY ──
        if ctx.context.mode == PermissionMode.DONT_ASK.value:
            permission_logger.info(
                "🛡️ [step_4_dontask] mode=dontAsk → DENY tool=%s", ctx.tool_name,
            )
            decision = PermissionDecision(
                behavior=PermissionBehavior.DENY.value,
                decision_reason=ModeReason(
                    mode=PermissionMode.DONT_ASK.value,
                    reason="dontAsk mode auto-denies",
                ),
            )
            return decision, "step_4_dontask"

        return None