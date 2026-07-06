"""
单 skill 加载器（file → SkillEntry）

设计要点（镜像 memory_store.py，但只读）：
1. load_single_skill: 读 SKILL.md → SkillEntry；任何失败设 load_error（不抛）
2. 安全：拒绝 symlink SKILL.md（path_validator）、强制 256KB 上限（FR-004）
3. 容错：malformed / 缺 desc / 超限 → 返回带 load_error 的 entry，不拖垮其他 skill（FR-005、SC-005）
4. 失败时通过 logging emit 事件（FR-023）
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Any, Optional

from agent_core.skills.frontmatter import ParsedSkillMd, parse_skill_md
from agent_core.skills.path_validator import reject_symlink_skill_md
from agent_core.skills.types import (
    DEFAULT_MAX_SKILL_FILE_BYTES,
    Skill,
    SkillEntry,
    SkillFrontmatterError,
    SkillMetadata,
    SkillSource,
    SkillVisibility,
    derive_visibility,
)


logger = logging.getLogger("agent_core.skills")


# body 相对引用识别（contracts/skill-md-format.md §1/§5.2）：references/ scripts/ assets/
# 前缀可选 ./ 或 ../（../ 在 resolve_body_references 中被丢弃，防逃逸 base_dir）
_BODY_REF_RE = re.compile(
    r"(?:\.{1,2}/)*(references|scripts|assets)/[A-Za-z0-9_.\-/]+"
)


def resolve_body_references(body: str, base_dir: Path | str) -> tuple[str, ...]:
    """
    扫描 body 中 references/scripts/assets/ 相对引用，解析为相对 base_dir 的绝对路径。

    契约 contracts/skill-md-format.md §5.2：body 相对路径 → skill 目录的绝对路径
    （模型用 Read 工具读时拿到 base_dir 拼接后的绝对路径）。

    - 仅识别 references/ scripts/ assets/ 三类子目录引用（v1 约定）
    - 跳过含 ``..`` 段的引用（防逃逸 base_dir，安全）
    - 去重 + 字典序排序（确定性，支撑 INV-2 字节稳定）
    - 纯字符串拼接（os.path.normpath），无 IO（不验证文件存在）
    """
    base = Path(base_dir)
    found: set[str] = set()
    for m in _BODY_REF_RE.finditer(body):
        rel = m.group(0)
        # 去掉句尾被贪婪吞入的标点（. - /），如 "assets/logo.png." → "assets/logo.png"
        rel = rel.rstrip(".-/")
        if not rel or ".." in Path(rel).parts:
            # 防逃逸：丢弃任何含 .. 段的引用
            continue
        resolved = os.path.normpath(str(base / rel))
        found.add(resolved)
    return tuple(sorted(found))


def load_single_skill(
    skill_dir: Path,
    source: SkillSource,
    *,
    max_bytes: int = DEFAULT_MAX_SKILL_FILE_BYTES,
) -> SkillEntry:
    """
    加载单个 skill 目录为 SkillEntry。

    任何失败（缺 SKILL.md / 超限 / symlink / malformed frontmatter）都不抛——
    返回带 load_error 的 SkillEntry（name 回退到目录名），让上层决定如何呈现。

    Args:
        skill_dir: skill 目录（内含 SKILL.md）
        source: 来源（BUNDLED / WORKSPACE）
        max_bytes: SKILL.md 单文件字节上限（FR-004，默认 256 KB）

    Returns:
        SkillEntry（成功：含 Skill；失败：load_error 非 None）
    """
    skill_dir = skill_dir.expanduser()
    fallback_name = skill_dir.name
    skill_md = skill_dir / "SKILL.md"

    def _error(msg: str) -> SkillEntry:
        logger.warning("🧩 skill load failed: skill=%s source=%s err=%s", fallback_name, source.value, msg)
        # 失败时仍返回一个占位 Skill（name=目录名），便于 status 列出
        placeholder = Skill(
            name=fallback_name,
            description="",
            file_path=str(skill_md),
            base_dir=str(skill_dir),
            source=source,
        )
        return SkillEntry(skill=placeholder, load_error=msg)

    # 1. SKILL.md 存在性
    if not skill_md.is_file():
        return _error(f"缺少 SKILL.md 文件: {skill_md}")

    # 2. symlink 拒绝（安全）
    try:
        reject_symlink_skill_md(skill_md)
    except Exception as e:  # PathSecurityError
        return _error(f"安全校验失败: {e}")

    # 3. 大小上限（FR-004）
    try:
        size = skill_md.stat().st_size
    except OSError as e:
        return _error(f"无法 stat SKILL.md: {e}")
    if size > max_bytes:
        return _error(
            f"SKILL.md 超过大小上限 {max_bytes} 字节（实际 {size}）"
        )

    # 4. 读取 + 解析
    try:
        content = skill_md.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        return _error(f"读取 SKILL.md 失败: {e}")

    parsed: ParsedSkillMd = parse_skill_md(content, fallback_name=fallback_name)
    if not parsed.ok:
        return _error(parsed.error or "未知解析错误")

    assert parsed.frontmatter is not None  # parsed.ok 已保证
    fm = parsed.frontmatter
    skill = Skill(
        name=fm["name"],
        description=fm["description"],
        file_path=str(skill_md),
        base_dir=str(skill_dir),
        source=source,
    )
    # US2 (T029)：解析 body 相对引用为绝对路径（供模型 Read 用）
    body_refs = resolve_body_references(parsed.body, skill_dir)
    # US3 (T036)：触发控制 + 资格字段（frontmatter 推导）
    disable_model_invocation = bool(fm.get("disable_model_invocation", False))
    user_invocable = bool(fm.get("user_invocable", True))
    metadata = fm.get("metadata")  # validate_frontmatter 已预解析为 SkillMetadata | None
    if metadata is not None and not isinstance(metadata, SkillMetadata):
        metadata = None
    visibility = derive_visibility(disable_model_invocation)
    if body_refs:
        logger.debug(
            "🧩 skill body refs resolved: skill=%s count=%d",
            skill.name, len(body_refs),
        )
    logger.debug(
        "🧩 skill loaded: skill=%s source=%s desc_len=%d visible=%s",
        skill.name, source.value, len(skill.description), visibility.value,
    )
    return SkillEntry(
        skill=skill,
        body_references=body_refs,
        metadata=metadata,
        disable_model_invocation=disable_model_invocation,
        user_invocable=user_invocable,
        visibility=visibility,
    )


__all__ = ["load_single_skill", "resolve_body_references"]
