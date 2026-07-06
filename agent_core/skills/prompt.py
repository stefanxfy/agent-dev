"""
prompt — available-skills 段渲染（纯函数，FR-007/008/009/010）

输出格式（对齐 contracts/skill-md-format.md §6）：
- 段头固定指令（告诉模型如何使用：scan / read at most one / never guess path）
- <available_skills> XML 块

两种模式：
- full: name + description + location
- compact: name + location（去 description；段头改 "matches its name"）

字节确定性（架构 §E 陷阱 3）：纯函数 + 输入已按 name 排序 → 相同输入字节相同。
"""

from __future__ import annotations

from xml.sax.saxutils import escape as _xml_escape

from agent_core.skills.types import SkillEntry, SkillRenderMode


# 段头指令（fixed）
_HEADER_FULL = """## Skills (mandatory)

Before replying: scan the `<available_skills>` block and its `<description>` entries.
When the task matches a skill, use the Read tool to load its SKILL.md at the exact `<location>`.
Read **at most one** skill up front; read **none** when no match exists.
Never guess or hardcode a skill path — always use the location from `<available_skills>`.
For skills that call external APIs with write side-effects, mind rate limits (HTTP 429)."""

_HEADER_COMPACT = """## Skills (mandatory)

Before replying: scan the `<available_skills>` block and its `<name>` entries.
When the task matches a skill, use the Read tool to load its SKILL.md at the exact `<location>`.
Read **at most one** skill up front; read **none** when no match exists.
Never guess or hardcode a skill path — always use the location from `<available_skills>`.
For skills that call external APIs with write side-effects, mind rate limits (HTTP 429)."""


def escape_xml(s: str) -> str:
    """XML 转义（& < > \" '）"""
    return _xml_escape(s, {'"': "&quot;", "'": "&apos;"})


def _format_one_full(entry: SkillEntry) -> str:
    s = entry.skill
    return (
        f"  <skill>\n"
        f"    <name>{escape_xml(s.name)}</name>\n"
        f"    <description>{escape_xml(s.description)}</description>\n"
        f"    <location>{escape_xml(s.file_path)}</location>\n"
        f"  </skill>"
    )


def _format_one_compact(entry: SkillEntry) -> str:
    s = entry.skill
    return (
        f"  <skill>\n"
        f"    <name>{escape_xml(s.name)}</name>\n"
        f"    <location>{escape_xml(s.file_path)}</location>\n"
        f"  </skill>"
    )


def render_skills_section(
    entries: list[SkillEntry],
    mode: SkillRenderMode = SkillRenderMode.FULL,
) -> str:
    """
    渲染 available-skills 段（纯函数）。

    Args:
        entries: 已排序的 eligible + model-visible SkillEntry 列表
        mode: FULL（含 description）/ COMPACT（去 description）/ TRUNCATE（同 COMPACT，调用方已截断）

    Returns:
        完整的 ## Skills 段文本（不含预算警告，警告由 snapshot.py 前置）

    Notes:
        - 空列表 → 返回空字符串（snapshot 决定是否注入）
        - TRUNCATE 模式渲染等同 COMPACT（差异仅在调用方截断了 entries）
    """
    if not entries:
        return ""

    if mode == SkillRenderMode.FULL:
        header = _HEADER_FULL
        body = "\n".join(_format_one_full(e) for e in entries)
    else:
        # COMPACT 和 TRUNCATE 渲染一致
        header = _HEADER_COMPACT
        body = "\n".join(_format_one_compact(e) for e in entries)

    return f"{header}\n\n<available_skills>\n{body}\n</available_skills>"


__all__ = ["render_skills_section", "escape_xml"]
