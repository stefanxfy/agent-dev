"""
skill_index — 单源扫描 + 字典序排序（US1, T013）+ 多源优先级合并（US5, T049）
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from agent_core.skills.skill_store import load_single_skill
from agent_core.skills.types import SkillEntry, SkillSource

if TYPE_CHECKING:
    from agent_core.skills.config import SkillsConfig


logger = logging.getLogger("agent_core.skills")


def _resolve_source(value: str) -> SkillSource:
    """字符串 → SkillSource 枚举（容错：未知值兜底 WORKSPACE）"""
    try:
        return SkillSource(value)
    except ValueError:
        logger.debug("🧩 未知 source 值 %r，兜底 WORKSPACE", value)
        return SkillSource.WORKSPACE


def scan_source(
    root_dir: Path,
    source: SkillSource,
    *,
    max_bytes: int,
    max_candidates: int = 300,
) -> list[SkillEntry]:
    """
    扫描单个来源根目录，返回所有子目录中的 SkillEntry（含失败 load_error）。

    流程：
    - root 不存在 → 返回空（不报错；workspace 默认 ./skills 可能不存在）
    - list 一层子目录（每子目录是一个 skill）
    - max_candidates 防爆（FR/limits.max_candidates_per_root）
    - 每子目录调 load_single_skill（失败设 load_error，不抛）

    Args:
        root_dir: 来源根目录
        source: 来源枚举
        max_bytes: SKILL.md 单文件字节上限
        max_candidates: 单源最多扫描的候选子目录数

    Returns:
        SkillEntry 列表（成功的 + 失败的）
    """
    root_dir = root_dir.expanduser()
    if not root_dir.is_dir():
        logger.debug("🧩 scan_source: root 不存在 skill=%s source=%s", root_dir, source.value)
        return []

    try:
        children = sorted(
            p for p in root_dir.iterdir() if p.is_dir() and not p.name.startswith(".")
        )
    except OSError as e:
        logger.warning("🧩 scan_source: 无法列举 root=%s err=%s", root_dir, e)
        return []

    if len(children) > max_candidates:
        logger.warning(
            "🧩 scan_source: 候选数 %d 超过上限 %d，截断（root=%s source=%s）",
            len(children), max_candidates, root_dir, source.value,
        )
        children = children[:max_candidates]

    entries: list[SkillEntry] = []
    ok_count = 0
    fail_count = 0
    for child in children:
        entry = load_single_skill(child, source, max_bytes=max_bytes)
        entries.append(entry)
        if entry.load_error is None:
            ok_count += 1
        else:
            fail_count += 1

    logger.debug(
        "🧩 scan_source done: source=%s root=%s total=%d ok=%d fail=%d",
        source.value, root_dir, len(entries), ok_count, fail_count,
    )
    return entries


def sort_by_name(entries: list[SkillEntry]) -> list[SkillEntry]:
    """按 skill.name 字典序确定性排序（localeCompare en 等价；保证字节稳定 INV-2）"""
    return sorted(entries, key=lambda e: e.skill.name)


def merge_by_priority(
    per_source_entries: list[tuple[SkillSource, list[SkillEntry]]],
) -> list[SkillEntry]:
    """
    多源合并：同名 skill 高优先级覆盖低优先级（US5, INV-1）。

    Args:
        per_source_entries: [(source, entries), ...] 须按 priority **升序**
            （低优先级先、高优先级后——后写覆盖，高者胜）

    Returns:
        合并后的 entries 列表（高优先级胜；同源内同名先到先得 + 告警，契约 §5.3）
    """
    merged: dict[str, SkillEntry] = {}
    for source, entries in per_source_entries:
        seen_in_source: set[str] = set()  # 本源内已登记名（同源同名先到先得）
        for e in entries:
            name = e.skill.name
            if name in seen_in_source:
                # 同源内同名：先到先得 + 告警（不覆盖同源已登记的）
                logger.warning(
                    "🧩 merge: 同源同名 skill 重复 skill=%s source=%s（先到先得）",
                    name, source.value,
                )
                continue
            seen_in_source.add(name)
            merged[name] = e  # 跨源后写覆盖（高优先级胜；不同目录同 enum 也算跨源）
    logger.debug(
        "🧩 merge_by_priority: sources=%d merged=%d",
        len(per_source_entries), len(merged),
    )
    return list(merged.values())


def load_all(config: "SkillsConfig") -> list[SkillEntry]:
    """
    扫描所有配置源 + 按优先级合并 + 字典序排序（US5 端到端）。

    来源序列取自 config.sources_list()（升序：bundled p1 → workspace p2 或自定义）。
    """
    per_source: list[tuple[SkillSource, list[SkillEntry]]] = []
    for src_cfg in config.sources_list():
        source = _resolve_source(src_cfg.source)
        entries = scan_source(
            src_cfg.dir, source,
            max_bytes=config.limits.max_skill_file_bytes,
            max_candidates=config.limits.max_candidates_per_root,
        )
        per_source.append((source, entries))
    merged = merge_by_priority(per_source)
    return sort_by_name(merged)


__all__ = ["scan_source", "sort_by_name", "merge_by_priority", "load_all"]
