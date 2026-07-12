"""Step 3: classifier fast-path。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

from .._logging import permission_logger
from ..fast_path import check_classifier_fast_path

if TYPE_CHECKING:
    from ..steps import PermissionContext


class FastPathStep:
    """3: classifier fast-path(stage_0_agent / stage_1_accept_edits / stage_2_allowlist)。

    命中(hit=True)→ 返 fast_path.to_permission_decision()(ALLOW 或 ASK)。
    未命中 → 返 None,继续到 Step 4 classifier 完整路径。

    对齐 engine 原 Step 3(logic + 日志 1:1):
      - stage="step_3_fast_path_{fast_path.stage}"(对齐原 monolith)
    """

    name = "step_3_fast_path"

    def __call__(self, ctx: "PermissionContext") -> "Optional[tuple[PermissionDecision, str]]":
        permission_logger.debug(
            "🛡️ [step_3_fast_path] running check_classifier_fast_path",
        )
        fast_path = check_classifier_fast_path(ctx.tool, ctx.tool_input, ctx.context)
        if not fast_path.hit:
            return None
        stage = f"step_3_fast_path_{fast_path.stage}"
        permission_logger.info(
            "🛡️ [%s_hit] stage=%s behavior=%s reason=%s",
            stage, fast_path.stage, fast_path.behavior,
            getattr(fast_path, "reason", "")[:120] if hasattr(fast_path, "reason") else "",
        )
        return fast_path.to_permission_decision(), stage