"""
Classifier Fast Path — 三阶段 fast-path(对齐 doc §4.9)

对齐 Claude Code src/utils/permissions/transcriptClassifier.ts + doc §4.9:
- 阶段 0:agent-like tool(requires_user_interaction=True)→ ASK
- 阶段 1:acceptEdits + tool.check_permissions 放行 → ALLOW
- 阶段 2:auto mode allowlist(Read/Glob/Grep/ListFiles/ReadImage)→ ALLOW
- 阶段 3:fall through → (None, None, None),让 caller 走完整 pipeline

核心设计:
1. **Fast-path 只做 ALLOW/ASK,不 DENY**(对齐 doc §4.9 + CC 设计)
   - DENY 必须在完整 pipeline 后由 safety_check / global deny rule 决定
2. **禁用 fast-path 的 tool**:`Agent` / `REPL`(REPL VM 逃逸攻击防护)
3. **Auto mode allowlist**:5 个只读类工具 — 几乎不可能 destructive
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable, Optional

from .permission_types import (
    OtherReason,
    PermissionBehavior,
    PermissionDecision,
    PermissionMode,
    ToolPermissionContext,
)


logger = logging.getLogger(__name__)


# ────────────────────────────────────────────────────────────────────
# 常量
# ────────────────────────────────────────────────────────────────────

# Auto mode allowlist(对齐 doc §4.9 + CC transcriptClassifier)
_AUTO_MODE_ALLOWLIST: frozenset[str] = frozenset({
    "Read",
    "Glob",
    "Grep",
    "ListFiles",
    "ReadImage",
})
"""
Auto mode 下允许 fast-path ALLOW 的只读工具列表

对齐 doc §4.9 列举的 5 个工具:
- Read:读文件
- Glob:文件匹配
- Grep:内容搜索
- ListFiles:列目录
- ReadImage:读图片(M1 stub)
"""


# 禁用 fast-path 的工具(防止 sandbox 逃逸)
_FAST_PATH_DISABLED_TOOLS: frozenset[str] = frozenset({
    "Agent",
    "REPL",
})
"""
这些 tool 即使符合 fast-path 条件,也强制走完整 pipeline:
- Agent:子 agent 可能调用 destructive tool,需完整 permission 检查
- REPL:VM 逃逸风险,M1 不实现
"""


# ────────────────────────────────────────────────────────────────────
# Stage result — 三阶段返值类型
# ────────────────────────────────────────────────────────────────────

@dataclass
class FastPathResult:
    """
    Fast-path 阶段结果

    字段:
    - hit: True 表示该阶段匹配(可短路返回)
    - behavior: 命中时的 behavior(allow / ask)
    - reason: 决策原因
    - stage: 命中的阶段名(stage_0_agent / stage_1_accept_edits / stage_2_allowlist)
    """
    hit: bool
    behavior: Optional[str] = None
    reason: Optional[str] = None
    stage: Optional[str] = None

    @classmethod
    def miss(cls) -> "FastPathResult":
        """未命中(fall through 到下一阶段或完整 pipeline)"""
        return cls(hit=False)

    @classmethod
    def allow(cls, reason: str, stage: str) -> "FastPathResult":
        """命中 ALLOW"""
        return cls(
            hit=True,
            behavior=PermissionBehavior.ALLOW.value,
            reason=reason,
            stage=stage,
        )

    @classmethod
    def ask(cls, reason: str, stage: str) -> "FastPathResult":
        """命中 ASK"""
        return cls(
            hit=True,
            behavior=PermissionBehavior.ASK.value,
            reason=reason,
            stage=stage,
        )

    def to_permission_decision(self) -> PermissionDecision:
        """转 PermissionDecision(给 PermissionEngine 用)"""
        if not self.hit:
            return PermissionDecision(behavior=PermissionBehavior.PASSTHROUGH.value)

        behavior = self.behavior or PermissionBehavior.ASK.value
        decision = PermissionDecision(
            behavior=behavior,
            message=self.reason,
        )
        if behavior == PermissionBehavior.ASK.value:
            decision.decision_reason = OtherReason(reason=self.reason or "fast_path_ask")
        return decision


# ────────────────────────────────────────────────────────────────────
# Tool 工具(duck-typed interface)
# ────────────────────────────────────────────────────────────────────

# ToolDef 在 Step 10 才加这些字段;此处用 duck-typed Protocol 兼容
# 完整 Protocol:
#   class ToolLike(Protocol):
#       name: str
#       requires_user_interaction: bool
#       check_permissions: Optional[Callable[[dict, ToolPermissionContext], PermissionDecision]]


def _is_tool_like(obj: Any) -> bool:
    """检查 obj 是否 duck-type 兼容 ToolDef"""
    return hasattr(obj, "name")


def _get_check_permissions(obj: Any) -> Optional[Callable]:
    """获取 tool.check_permissions(可能不存在)"""
    return getattr(obj, "check_permissions", None)


def _get_requires_user_interaction(obj: Any) -> bool:
    """获取 tool.requires_user_interaction(可能不存在,默认 False)"""
    return bool(getattr(obj, "requires_user_interaction", False))


# ────────────────────────────────────────────────────────────────────
# is_auto_mode_allowlisted_tool — 检查工具是否在 allowlist
# ────────────────────────────────────────────────────────────────────

def is_auto_mode_allowlisted_tool(tool_name: str) -> bool:
    """
    检查工具是否在 auto mode allowlist 中

    Args:
        tool_name: 工具名

    Returns:
        True 如果在 allowlist 中
    """
    return tool_name in _AUTO_MODE_ALLOWLIST


def is_fast_path_disabled_tool(tool_name: str) -> bool:
    """
    检查工具是否禁用 fast-path

    Args:
        tool_name: 工具名

    Returns:
        True 如果禁用 fast-path(Agent / REPL 等)
    """
    return tool_name in _FAST_PATH_DISABLED_TOOLS


# ────────────────────────────────────────────────────────────────────
# 三阶段 fast-path
# ────────────────────────────────────────────────────────────────────

def check_classifier_fast_path(
    tool: Any,
    tool_input: dict,
    context: ToolPermissionContext,
) -> FastPathResult:
    """
    三阶段 fast-path 检查(对齐 doc §4.9)

    阶段:
      Stage 0:requires_user_interaction=True → ASK
      Stage 1:mode == acceptEdits + 非 fast-path-disabled + check_permissions 返 ALLOW → ALLOW
      Stage 2:mode == auto + tool 在 allowlist → ALLOW
      Stage 3:fall through → miss()

    Args:
        tool: ToolDef 实例(duck-typed)
        tool_input: 工具输入参数 dict
        context: ToolPermissionContext

    Returns:
        FastPathResult(hit / behavior / reason / stage)
    """
    if not _is_tool_like(tool):
        return FastPathResult.miss()

    tool_name = tool.name

    # ── 阶段 0:agent-like tool 强制 ASK ──────────────────────────
    if _get_requires_user_interaction(tool):
        return FastPathResult.ask(
            reason=f"tool {tool_name} requires user interaction",
            stage="stage_0_agent",
        )

    # ── 阶段 1:acceptEdits mode + tool.check_permissions 放行 ─────
    if context.mode == PermissionMode.ACCEPT_EDITS.value:
        # fast-path-disabled tool 仍走完整 pipeline
        if is_fast_path_disabled_tool(tool_name):
            return FastPathResult.miss()

        # 调用 tool.check_permissions(如有)
        check_permissions_fn = _get_check_permissions(tool)
        if check_permissions_fn is not None:
            try:
                tool_decision = check_permissions_fn(tool_input, context)
                if tool_decision.behavior == PermissionBehavior.ALLOW.value:
                    return FastPathResult.allow(
                        reason=f"tool {tool_name} check_permissions allow in acceptEdits mode",
                        stage="stage_1_accept_edits",
                    )
            except Exception as e:
                # check_permissions 抛异常不阻断 pipeline(降级到完整 pipeline)
                logger.warning(
                    "tool %s check_permissions 抛异常: %s — 降级到完整 pipeline",
                    tool_name, e,
                )
                return FastPathResult.miss()
        else:
            # acceptEdits mode + 无 check_permissions → 默认 ALLOW(对齐 CC)
            # 例:内置 Read/Edit tool 的处理
            return FastPathResult.allow(
                reason=f"tool {tool_name} auto-allowed in acceptEdits mode",
                stage="stage_1_accept_edits",
            )

    # ── 阶段 2:auto mode + allowlist ──────────────────────────────
    if context.mode == PermissionMode.AUTO.value:
        if is_auto_mode_allowlisted_tool(tool_name):
            return FastPathResult.allow(
                reason=f"tool {tool_name} in auto mode allowlist",
                stage="stage_2_allowlist",
            )

    # ── 阶段 3:fall through ──────────────────────────────────────
    return FastPathResult.miss()


# ────────────────────────────────────────────────────────────────────
# _swap_mode — context mode 临时切换(对齐 CC 中用于 fast-path sub-context)
# ────────────────────────────────────────────────────────────────────

def _swap_mode(context: ToolPermissionContext, new_mode: str) -> ToolPermissionContext:
    """
    临时把 context 的 mode 改成 new_mode(用 Pydantic model_copy)

    主要给 auto mode 下的 classifier sub-context 用:
      - classifier 内部用 mode='acceptEdits' 走 fast-path(避免弹窗嵌套)

    Args:
        context: 原始 context
        new_mode: 新 mode

    Returns:
        新的 ToolPermissionContext(mode 已替换)
    """
    return context.model_copy(update={"mode": new_mode})
