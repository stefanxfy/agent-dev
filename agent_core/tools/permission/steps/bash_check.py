"""Step 1c': BashTool 专属(对齐 spec §4.5)。"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Callable, Optional

from .._logging import permission_logger
from ..types import PermissionBehavior, PermissionDecision

if TYPE_CHECKING:
    from ..steps import PermissionContext

_logger = logging.getLogger(__name__)


class BashCheckStep:
    """1c': Bash 工具专属决策路径。

    Bash 是最易被 prompt injection 利用的工具,所有 Bash 调用走 bash_check_permissions
    (subcommand 级 rule + classifier + sandbox auto-allow)。

    行为(对齐 engine 原 Step 1c'):
      - 只对 tool_name == "Bash" 触发
      - 调注入的 bash_check_fn(orchestrator 用 lambda 延迟绑定 → 保留 monkeypatch 能力)
      - bash_decision 行为:
        - DENY → 终止,stage="step_1c_bash_deny"
        - ASK → 终止,stage="step_1c_bash_ask"
        - ALLOW → 终止,stage="step_1c_bash_allow"
        - PASSTHROUGH → 返 None,fall through 到 Step 1d+
      - bash_check_fn 抛异常 → 吞掉,返 None(降级继续)

    bash_check_fn 注入而非硬编码,是为了保留 monkeypatch 能力
    (test_agent_core_bash_sandbox.py patch engine._run_bash_check_permissions)。
    """

    name = "step_1c_bash"

    def __init__(self, bash_check_fn: Optional[Callable[[dict], Optional[PermissionDecision]]] = None):
        """Args:
            bash_check_fn: 接受 tool_input,返 PermissionDecision 或 None。
                orchestrator 注入 lambda(闭包 self,调用时查 self.__dict__)。
        """
        self._bash_check_fn = bash_check_fn

    def __call__(self, ctx: "PermissionContext") -> "Optional[tuple[PermissionDecision, str]]":
        if ctx.tool_name != "Bash":
            return None
        permission_logger.debug(
            "🛡️ [step_1c_bash] tool=Bash → run bash_check_permissions",
        )
        if self._bash_check_fn is None:
            return None
        try:
            bash_decision = self._bash_check_fn(ctx.tool_input)
        except Exception as e:
            _logger.warning(
                "bash_check_fn 异常: %s — 降级继续", e,
            )
            return None
        if bash_decision is None:
            return None
        # PASSTHROUGH → 返 None fall through
        if bash_decision.behavior == PermissionBehavior.PASSTHROUGH.value:
            permission_logger.debug(
                "🛡️ [step_1c_bash_passthrough] tool=Bash fall through to 1d+",
            )
            return None
        # DENY / ASK / ALLOW → 终止 + 细 stage
        behavior = bash_decision.behavior
        stage = f"step_1c_bash_{behavior}"
        permission_logger.info(
            "🛡️ [%s] tool=Bash reason=%s",
            stage, (getattr(bash_decision, "message", "") or "")[:120],
        )
        return bash_decision, stage