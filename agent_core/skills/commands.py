"""
commands — slash 命令解析（US4, T042）

解析用户输入 "/skill-name <args>" → (skill_name, args) | None。
- 仅识别以单 / 开头的首条命令（不做多命令拆分）
- skill-name 经 sanitize（lowercase + 非 [a-z0-9_] → _ + 截 32 字符）
- args 为剩余文本（去首尾空白；可为空串）

镜像 OpenClaw slash prompt-rewrite 路径（spec FR-020）。
"""

from __future__ import annotations

import logging
import re
from typing import Optional

logger = logging.getLogger("agent_core.skills")


# name/args 分隔：首个空白（支持多空格 / tab）；DOTALL 以容纳多行 args
_NAME_ARGS_RE = re.compile(r"(\S+)(?:[\s]+(.*))?", re.DOTALL)
# sanitize：非 [a-z0-9_] → _
_SANITIZE_RE = re.compile(r"[^a-z0-9_]")

_MAX_NAME_LEN = 32


def sanitize_skill_command_name(raw: str) -> str:
    """
    规范化 skill 命令名：lowercase + 非 [a-z0-9_] → _ + 截 32 字符。

    sanitize_skill_command_name("Hello-World!") → "hello_world_"
    """
    s = raw.lower()
    s = _SANITIZE_RE.sub("_", s)
    return s[:_MAX_NAME_LEN]


def resolve_skill_command(text: str) -> Optional[tuple[str, str]]:
    """
    解析 "/skill-name <args>" → (skill_name, args) | None。

    - 非 str / 空 / 不以 / 开头 → None
    - 仅 "/" 或 "/ xxx"（/ 后直接空白）→ None
    - skill-name 经 sanitize；sanitize 后为空 → None

    Returns:
        (skill_name, args) 元组；args 为去首尾空白的剩余文本（可为 ""）
    """
    if not isinstance(text, str):
        return None
    s = text.strip()
    if not s.startswith("/"):
        return None
    body = s[1:]
    if not body or body[0].isspace():
        return None  # 仅 "/" 或 "/<空白>..."

    m = _NAME_ARGS_RE.match(body)
    if not m:
        return None
    raw_name = m.group(1)
    args = (m.group(2) or "").strip()

    name = sanitize_skill_command_name(raw_name)
    # 去掉首尾可能因 sanitize 产生的下划线后的空名
    if not name or set(name) == {"_"}:
        logger.debug("🧩 slash: sanitized name 为空 raw=%s", raw_name)
        return None
    logger.debug("🧩 slash resolved: raw=%s → name=%s args_len=%d", raw_name, name, len(args))
    return (name, args)


__all__ = ["resolve_skill_command", "sanitize_skill_command_name"]
