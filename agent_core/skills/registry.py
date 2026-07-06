"""
SkillsRegistry — Facade + cache-aside 快照缓存（US1, T016）

设计（架构 §C）：
- Facade：把"扫描两源 + 合并 + 构建 snapshot + 缓存"简化为 `snapshot() -> SkillSnapshot`
- Cache-Aside：mtime 失效的惰性缓存（research Decision 3，不引 watchdog）
- version 单调递增（INV-6）：每次重建 +1
- 串行化：threading.Lock 保证并发 snapshot() 不重复重建

US1：仅扫 workspace 一源（或 bundled 一源）；US5 (T049) 扩展多源合并。
"""

from __future__ import annotations

import logging
import os
import threading
from pathlib import Path
from typing import Optional

from agent_core.skills.config import SkillsConfig
from agent_core.skills.skill_index import load_all
from agent_core.skills.snapshot import build_snapshot
from agent_core.skills.types import SkillEntry, SkillSnapshot


logger = logging.getLogger("agent_core.skills")


def _dir_mtime(p: Path) -> Optional[float]:
    """安全取目录 mtime；不存在/失败返回 None"""
    try:
        return p.stat().st_mtime_ns
    except OSError:
        return None


class SkillsRegistry:
    """
    skill 快照缓存（Facade + cache-aside）。

    用法：
        registry = SkillsRegistry(config)
        snap = registry.snapshot()   # 命中缓存或重建
        snap.prompt                   # 注入 system prompt 的 ## Skills 段
    """

    def __init__(self, config: SkillsConfig):
        self._config = config
        self._lock = threading.Lock()
        self._cached: Optional[SkillSnapshot] = None
        self._cached_sig: Optional[tuple] = None
        self._version = 0
        # US4：slash 派发按名查 SkillEntry（缓存最近一次加载结果）
        self._last_entries_by_name: dict[str, SkillEntry] = {}

    # ── 公开 API ────────────────────────────────────────────────

    def snapshot(self) -> SkillSnapshot:
        """返回当前 skill 快照（缓存命中或惰性重建）。"""
        with self._lock:
            sig = self._current_sig()
            if self._cached is not None and self._cached_sig == sig:
                logger.debug(
                    "🧩 snapshot cache hit: version=%d skills=%d",
                    self._cached.version, len(self._cached.skills),
                )
                return self._cached
            return self._rebuild(sig)

    def get_entry(self, name: str) -> Optional[SkillEntry]:
        """
        按 skill 名查 SkillEntry（US4 slash 派发用）。

        触发 snapshot() 以确保最近一次加载结果可用；返回带完整 frontmatter/visibility
        的 entry（含 HIDDEN skill），调用方据此判定 user_invocable + 取 file_path。
        """
        if not self._last_entries_by_name:
            self.snapshot()
        return self._last_entries_by_name.get(name)

    @property
    def version(self) -> int:
        """当前缓存版本号（单调递增，INV-6）。"""
        return self._cached.version if self._cached else 0

    # ── 内部 ────────────────────────────────────────────────────

    def _current_sig(self) -> tuple:
        """来源目录 mtime + env 指纹；任一变化则触发重建（mtime→SC-001，env→INV-5）。"""
        p = self._config.paths
        return (
            _dir_mtime(p.bundled_dir.resolve()),
            _dir_mtime(p.workspace_dir.resolve()),
            hash(tuple(sorted(os.environ.items()))),  # env 变化 → 重建（INV-5 即时性）
        )

    def _load_all(self) -> list[SkillEntry]:
        """扫描所有配置的源 + 按优先级合并 + 字典序排序（US5：委托 skill_index.load_all）。"""
        return load_all(self._config)

    def _rebuild(self, sig: tuple) -> SkillSnapshot:
        """重建快照 + bump version + 更新缓存。"""
        self._version += 1
        entries = self._load_all()
        # US4：缓存 by-name 索引（slash 派发查表用）
        self._last_entries_by_name = {e.skill.name: e for e in entries}
        snap = build_snapshot(
            entries,
            env=dict(os.environ),   # eligibility.env 即时求值（data-model §6）
            config={},              # v1: agent Config 未接（requires.config 视为 unmet）
            limits=self._config.limits,
            version=self._version,
        )
        self._cached = snap
        self._cached_sig = sig
        logger.debug(
            "🧩 snapshot rebuilt: version=%d entries=%d prompt_len=%d",
            self._version, len(entries), len(snap.prompt),
        )
        return snap


__all__ = ["SkillsRegistry"]
