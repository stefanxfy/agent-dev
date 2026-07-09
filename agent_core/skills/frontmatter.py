"""
SKILL.md frontmatter 解析（复用 memory 的 parse_frontmatter + skill 专属校验）

设计要点（research Decision 5）：
1. 复用 agent_core.memory.memory_store.parse_frontmatter（同一份解析器，避免双份维护漂移）
2. 在解析结果上跑 skill 专属校验（types.validate_frontmatter）
3. 解析失败返回结构化错误（不抛），由 skill_store 转为 SkillEntry.load_error
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from agent_core.memory.memory_store import parse_frontmatter as _memory_parse_frontmatter
from agent_core.memory.memory_store import MemoryStoreError
from agent_core.skills.types import SkillFrontmatter, SkillFrontmatterError, validate_frontmatter


logger = logging.getLogger("agent_core.skills")


@dataclass(frozen=True)
class ParsedSkillMd:
    """SKILL.md 解析结果（成功）/ error（失败）"""
    frontmatter: Optional[SkillFrontmatter] = None
    body: str = ""
    error: Optional[str] = None  # 非 None 表示解析失败（malformed / 校验失败）

    @property
    def ok(self) -> bool:
        return self.error is None and self.frontmatter is not None


def parse_skill_md(content: str, *, fallback_name: Optional[str] = None) -> ParsedSkillMd:
    """
    解析 SKILL.md 内容（frontmatter + body）。

    Args:
        content: SKILL.md 文件全文
        fallback_name: name 缺失时的回退（通常传目录名）

    Returns:
        ParsedSkillMd：成功含 frontmatter+body；失败含 error 字符串（不抛）
    """
    # 统一换行（容忍 \r\n）
    normalized = content.replace("\r\n", "\n")
    if not normalized.startswith("---\n"):
        # 容忍首行正好是 --- 但无换行（极短文件）
        if normalized.strip() == "---":
            return ParsedSkillMd(error="SKILL.md 仅含 frontmatter 起始符，无内容")
        return ParsedSkillMd(
            error="SKILL.md 必须以 '---\\n' 开头的 YAML frontmatter 开始"
        )

    # 1. 复用 memory 的 parse_frontmatter（YAML 解析）
    try:
        data, body = _memory_parse_frontmatter(normalized)
    except MemoryStoreError as e:
        logger.debug("🧩 frontmatter parse failed: fallback=%s err=%s", fallback_name, e)
        return ParsedSkillMd(error=f"frontmatter 解析失败: {e}")

    # 2. skill 专属校验（description 必填等）
    try:
        fm = validate_frontmatter(data, fallback_name=fallback_name)
    except SkillFrontmatterError as e:
        logger.debug("🧩 frontmatter validate failed: skill=%s err=%s", fallback_name, e)
        return ParsedSkillMd(error=f"frontmatter 校验失败: {e}")

    logger.debug(
        "🧩 frontmatter parsed: skill=%s desc_len=%d body_len=%d",
        fm.get("name"), len(fm.get("description", "")), len(body),
    )
    return ParsedSkillMd(frontmatter=fm, body=body)


__all__ = [
    "ParsedSkillMd",
    "parse_skill_md",
]
