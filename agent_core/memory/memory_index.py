"""
MEMORY.md 物理索引(M11)

借鉴 Claude Code 的 MEMORY.md 模式:
- <memory_root>/MEMORY.md
- 双重硬上限: 200 行 / 25KB
- 写盘后异步 rebuild(1s 合并窗口)
"""

from __future__ import annotations

import itertools
import logging
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

MAX_ENTRYPOINT_LINES = 200
MAX_ENTRYPOINT_BYTES = 25_000
FRONTMATTER_MAX_LINES = 30
MARK_DIRTY_DELAY = 1.0  # 1s 合并窗口
MEMORY_FILE_NAME = "MEMORY.md"
MEMORY_FILE_HEADER = "# Agent Memory (auto-generated)\n"

# ──────────────────────────────────────────────────────────────────
# scan_memory_files
# ──────────────────────────────────────────────────────────────────

_VALID_TYPES = ("user", "feedback", "event", "project", "reference")


def _parse_frontmatter_head(head: str) -> dict:
    """极简 frontmatter 解析(T9 会替换为复用 store 的正式版)"""
    fm: dict = {}
    in_fm = False
    for line in head.splitlines():
        if line.strip() == "---":
            if in_fm:
                break
            in_fm = True
            continue
        if in_fm and ":" in line:
            key, _, val = line.partition(":")
            fm[key.strip()] = val.strip().strip('"').strip("'")
    return fm


def scan_memory_files(
    memory_root: Path,
    max_files: int = 200,
    frontmatter_max_lines: int = FRONTMATTER_MAX_LINES,
    types_filter: Optional[list[str]] = None,
) -> list["MemoryFileEntry"]:
    """扫 memory_root 下所有 .md, 只读前 N 行 frontmatter, 按 mtime 倒序截 max_files"""
    if not memory_root.exists():
        return []

    all_files: list[Path] = []
    for t in (types_filter or list(_VALID_TYPES)):
        type_dir = memory_root / t
        if type_dir.exists():
            all_files.extend(type_dir.glob("*.md"))

    all_files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    all_files = all_files[:max_files]

    entries: list[MemoryFileEntry] = []
    for p in all_files:
        try:
            with p.open("r", encoding="utf-8") as f:
                head = "".join(itertools.islice(f, frontmatter_max_lines))
            fm = _parse_frontmatter_head(head)
            entries.append(MemoryFileEntry(
                rel_path=str(p.relative_to(memory_root)),
                name=fm.get("name") or fm.get("title", "?"),
                description=fm.get("description", "无描述"),
                type=fm.get("type", "user"),
                mtime_ms=int(p.stat().st_mtime * 1000),
            ))
        except (OSError, ValueError, KeyError):
            continue
    return entries


# ──────────────────────────────────────────────────────────────────
# MemoryFileEntry
# ──────────────────────────────────────────────────────────────────

@dataclass
class MemoryFileEntry:
    rel_path: str
    name: str
    description: str
    type: str
    mtime_ms: int


def format_memory_manifest(entries: list[MemoryFileEntry]) -> str:
    """渲染 MEMORY.md manifest: '- [name](rel_path) — description' per line"""
    return "\n".join(
        f"- [{e.name}]({e.rel_path}) — {e.description}"
        for e in entries
    )


# ──────────────────────────────────────────────────────────────────
# MemoryIndex
# ──────────────────────────────────────────────────────────────────

class MemoryIndex:
    """维护 MEMORY.md 物理索引(异步 rebuild, 1s 合并窗口)"""

    def __init__(self, memory_root: Path):
        self.root = Path(memory_root)
        self.path = self.root / MEMORY_FILE_NAME
        self._lock = threading.Lock()
        self._pending = False
        self._timer: Optional[threading.Timer] = None

    def mark_dirty(self) -> None:
        """标记过期, 1s 后异步 rebuild(合并窗口)"""
        with self._lock:
            if self._pending:
                return
            self._pending = True
            self._timer = threading.Timer(MARK_DIRTY_DELAY, self.rebuild)
            self._timer.daemon = True
            self._timer.start()

    def flush(self) -> None:
        """强制同步 rebuild(测试 / 进程关闭前)"""
        with self._lock:
            if self._timer:
                self._timer.cancel()
        self.rebuild()

    def rebuild(self) -> None:
        """重建 MEMORY.md 文件(同步, 持锁)"""
        with self._lock:
            self._pending = False
            entries = scan_memory_files(self.root, max_files=MAX_ENTRYPOINT_LINES)
            content = self._render(entries)
            truncated = self._truncate(content)
            try:
                self.root.mkdir(parents=True, exist_ok=True)
                self.path.write_text(truncated, encoding="utf-8")
                logger.debug(
                    f"MEMORY.md rebuilt: {len(entries)} entries, "
                    f"{len(truncated)} bytes"
                )
            except OSError as e:
                logger.warning(f"MEMORY.md 写盘失败: {e}")

    def load_index(self) -> str:
        """同步读取 MEMORY.md 内容(L1 启动加载用)"""
        if not self.path.exists():
            self.rebuild()
        return self.path.read_text(encoding="utf-8") if self.path.exists() else ""

    def _render(self, entries: list[MemoryFileEntry]) -> str:
        lines = [MEMORY_FILE_HEADER]
        for e in entries:
            lines.append(f"- [{e.name}]({e.rel_path}) — {e.description}")
        return "\n".join(lines) + "\n"

    def _truncate(self, content: str) -> str:
        """双重硬上限: 先按行截, 再按字节截"""
        lines = content.splitlines()
        if len(lines) > MAX_ENTRYPOINT_LINES:
            content = "\n".join(lines[:MAX_ENTRYPOINT_LINES]) + "\n"
            logger.info(
                f"MEMORY.md 超过 {MAX_ENTRYPOINT_LINES} 行, 截断"
            )
        if len(content.encode("utf-8")) > MAX_ENTRYPOINT_BYTES:
            content = content.encode("utf-8")[:MAX_ENTRYPOINT_BYTES].decode(
                "utf-8", errors="ignore"
            )
            logger.info(
                f"MEMORY.md 超过 {MAX_ENTRYPOINT_BYTES} 字节, 截断"
            )
        return content