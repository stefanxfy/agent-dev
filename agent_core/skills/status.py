"""
status — skill 诊断格式化（Polish, T053；FR-022 用户可调用面）

format_skill_status(registry) -> str：每 skill 一行
  name / source / eligibility / visibility / missing 或 ⚠️ load_error
+ 头部汇总（total / eligible / hidden / errored）。

供 scripts/skills_check.py CLI 与调试用；纯文本表格，非 UI。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from agent_core.skills.types import (
    SkillEligibilityState,
    SkillVisibility,
)

if TYPE_CHECKING:
    from agent_core.skills.registry import SkillsRegistry

logger = logging.getLogger("agent_core.skills")


def format_skill_status(registry: "SkillsRegistry") -> str:
    """
    格式化 registry 当前快照为多行文本表格（FR-022）。

    每行：name | source | eligibility | visibility | missing/load_error
    头部：total / eligible / model_visible / hidden / errored 计数。
    """
    snap = registry.snapshot()
    summaries = sorted(snap.skills, key=lambda s: s.name)

    total = len(summaries)
    n_eligible = sum(1 for s in summaries if s.eligible)
    n_hidden = sum(1 for s in summaries if s.visibility == SkillVisibility.HIDDEN_FROM_MODEL)
    n_errored = sum(1 for s in summaries if s.eligibility_state == SkillEligibilityState.ERRORED)

    lines: list[str] = []
    lines.append(
        f"Skills status: total={total} eligible={n_eligible} "
        f"model_visible={total - n_hidden} hidden_from_model={n_hidden} errored={n_errored} "
        f"(snapshot version={snap.version}, render_mode={snap.render_mode.value})"
    )

    if not summaries:
        lines.append("  (无 skill — 在 skills/ 或 agent_core/skills/builtin/ 下放 SKILL.md)")
        return "\n".join(lines)

    # 表头
    header = f"  {'name':<24} {'source':<10} {'eligibility':<20} {'visibility':<18} detail"
    lines.append(header)
    lines.append(f"  {'-' * 24} {'-' * 10} {'-' * 20} {'-' * 18} {'-' * 30}")

    for s in summaries:
        detail = "-"
        if s.load_error:
            detail = f"⚠️ {s.load_error}"
        elif s.missing:
            detail = ", ".join(s.missing)
        vis = "hidden_from_model" if s.visibility == SkillVisibility.HIDDEN_FROM_MODEL else "model_visible"
        uinv = "" if s.user_invocable else " [no-slash]"
        ref_suffix = f" refs={len(s.body_references)}" if s.body_references else ""
        lines.append(
            f"  {s.name:<24} {s.source.value:<10} {s.eligibility_state.value:<20} {vis:<18} {detail}{uinv}{ref_suffix}"
        )
        # Convergence (T064)：body 引用的绝对路径（作者用 skills_check 校验资产是否齐全）
        for ref in s.body_references:
            lines.append(f"      └─ {ref}")

    return "\n".join(lines)


__all__ = ["format_skill_status"]
