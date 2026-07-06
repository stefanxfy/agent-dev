"""
snapshot — SkillSnapshot 构建流水线（US1, T015；US3 T038 接入 eligibility/visibility）

流水线：eligibility+visibility 过滤 → sort → budget 三级降级（full→compact→truncate）
→ render + ⚠️ 预算警告前置（FR-017/018，绝不静默丢）。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Optional

from agent_core.skills.eligibility import evaluate_eligibility
from agent_core.skills.prompt import render_skills_section
from agent_core.skills.types import (
    Skill,
    SkillEligibilityState,
    SkillEntry,
    SkillRenderMode,
    SkillSnapshot,
    SkillSummary,
    SkillVisibility,
)

if TYPE_CHECKING:
    # 仅类型注解用；运行时避免循环（config 不 import snapshot，但保守起见走 TYPE_CHECKING）
    from agent_core.skills.config import LimitsConfig

logger = logging.getLogger("agent_core.skills")


# 预算警告文案（对齐 OpenClaw workspace.ts:1010-1014）
_WARN_COMPACT = (
    "⚠️ Skills catalog using compact format (descriptions omitted). "
    "Run check to audit."
)
_WARN_TRUNCATE_TEMPLATE = (
    "⚠️ Skills truncated: included {included} of {total} "
    "(compact format, descriptions omitted). Run check to audit."
)
# 紧凑模式预留警告字符预算
_WARN_RESERVE = 200


def _filter_eligible_visible(
    entries: list[SkillEntry],
    *,
    env: Optional[dict[str, str]] = None,
    config: Optional[dict[str, Any]] = None,
) -> tuple[list[SkillEntry], list[tuple[SkillEntry, SkillEligibilityState, tuple[str, ...]]]]:
    """
    US3 过滤（T038）：evaluate_eligibility + visibility。

    - 即时求值每个 entry 的 eligibility（data-model §6）
    - prompt 仅含 ELIGIBLE AND MODEL_VISIBLE
    - 全部 entry 的判定返回，供 SkillSnapshot.skills summary（status 用）
    - 排除项经 logging emit 事件（FR-023）

    Returns:
        (prompt_entries, all_evaluated)
        - prompt_entries: 进 <available_skills> 的 entry（已 Eligible+Visible，未排序）
        - all_evaluated: [(entry, state, missing), ...] 全部 entry 的判定
    """
    all_evaluated: list[tuple[SkillEntry, SkillEligibilityState, tuple[str, ...]]] = []
    prompt_entries: list[SkillEntry] = []
    excluded = 0
    for e in entries:
        state, missing = evaluate_eligibility(e, env=env, config=config)
        all_evaluated.append((e, state, missing))
        if state == SkillEligibilityState.ELIGIBLE and e.visibility == SkillVisibility.MODEL_VISIBLE:
            prompt_entries.append(e)
        else:
            excluded += 1
            logger.debug(
                "🧩 filter: exclude skill=%s state=%s visible=%s missing=%s",
                e.skill.name, state.value, e.visibility.value, missing,
            )
    if excluded:
        logger.debug(
            "🧩 filter: in=%d prompt=%d excluded=%d",
            len(entries), len(prompt_entries), excluded,
        )
    return prompt_entries, all_evaluated


def _apply_budget_limits(
    entries: list[SkillEntry],
    *,
    max_in_prompt: int,
    max_chars: int,
) -> tuple[list[SkillEntry], SkillRenderMode, int, Optional[str]]:
    """
    三级预算降级（research Decision 8）。

    Returns:
        (final_entries, render_mode, truncated_count, warning_or_None)
    """
    total = len(entries)

    # 1. 按 max_in_prompt 截断
    by_count = entries[:max_in_prompt]
    if len(by_count) < total:
        logger.debug(
            "🧩 budget: max_in_prompt 截断 %d→%d", total, len(by_count)
        )

    # 2. Tier 1 FULL
    full_text = render_skills_section(by_count, mode=SkillRenderMode.FULL)
    if len(full_text) <= max_chars:
        logger.debug("🧩 budget: FULL fits len=%d <= %d", len(full_text), max_chars)
        return by_count, SkillRenderMode.FULL, total - len(by_count), None

    # 3. Tier 2 COMPACT（去 description）
    compact_text = render_skills_section(by_count, mode=SkillRenderMode.COMPACT)
    compact_budget = max_chars - _WARN_RESERVE
    if len(compact_text) <= compact_budget:
        logger.warning(
            "🧩 budget: degrade FULL→COMPACT (full_len=%d > %d, compact_len=%d)",
            len(full_text), max_chars, len(compact_text),
        )
        return by_count, SkillRenderMode.COMPACT, total - len(by_count), _WARN_COMPACT

    # 4. Tier 3 TRUNCATE：二分搜索最大前缀
    lo, hi = 0, len(by_count)
    # lo=0 时 render 空（return ""），故保证至少能装下 0 个
    best = 0
    while lo <= hi:
        mid = (lo + hi) // 2
        candidate = render_skills_section(by_count[:mid], mode=SkillRenderMode.COMPACT)
        if len(candidate) <= compact_budget:
            best = mid
            lo = mid + 1
        else:
            hi = mid - 1

    truncated_count = total - best
    warn = _WARN_TRUNCATE_TEMPLATE.format(included=best, total=total)
    logger.warning(
        "🧩 budget: degrade COMPACT→TRUNCATE included=%d of %d (compact_len budget=%d)",
        best, total, compact_budget,
    )
    return by_count[:best], SkillRenderMode.TRUNCATE, truncated_count, warn


def build_snapshot(
    entries: list[SkillEntry],
    *,
    env: Optional[dict[str, str]] = None,         # eligibility.env（None → os.environ）
    config: Optional[dict[str, Any]] = None,       # eligibility.config（None → {}）
    limits: Optional["LimitsConfig"] = None,
    version: int = 0,
) -> SkillSnapshot:
    """
    构建 SkillSnapshot（纯函数，参数注入 entries/env/config/limits）。

    流水线：eligibility+visibility 过滤 → sort → budget 三级降级 → render + ⚠️ 警告前置。
    """
    # 1. filter（US3：eligibility + visibility）
    visible, all_evaluated = _filter_eligible_visible(entries, env=env, config=config)

    # 2. sort by name（确定性，INV-2）
    visible = sorted(visible, key=lambda e: e.skill.name)

    # 3. limits 默认值
    if limits is None:
        # 延迟 import 避免循环（snapshot 只需 default 值）
        from agent_core.skills.config import LimitsConfig as _LC
        limits = _LC()

    # 4. budget 降级
    final_entries, mode, truncated_count, warning = _apply_budget_limits(
        visible,
        max_in_prompt=limits.max_skills_in_prompt,
        max_chars=limits.max_skills_prompt_chars,
    )

    # 5. render
    prompt_body = render_skills_section(final_entries, mode=mode)
    if warning and prompt_body:
        prompt = f"{warning}\n\n{prompt_body}"
    elif warning:
        prompt = warning  # 即使 truncate 到 0，也保留警告（不静默）
    else:
        prompt = prompt_body

    # 6. summary（含所有 entry 的判定，供 status 用；eligible/missing/visibility 反映即时求值）
    summaries = [
        SkillSummary(
            name=e.skill.name,
            source=e.skill.source,
            eligible=(state == SkillEligibilityState.ELIGIBLE),
            missing=missing,
            visibility=e.visibility,
            user_invocable=e.user_invocable,
            eligibility_state=state,
            load_error=e.load_error,
            body_references=e.body_references,
        )
        for (e, state, missing) in all_evaluated
    ]

    logger.debug(
        "🧩 build_snapshot done: in=%d visible=%d rendered=%d mode=%s prompt_len=%d",
        len(entries), len(visible), len(final_entries), mode.value, len(prompt),
    )

    return SkillSnapshot(
        prompt=prompt,
        skills=summaries,
        version=version,
        render_mode=mode,
        truncated_count=truncated_count,
    )


__all__ = ["build_snapshot"]
