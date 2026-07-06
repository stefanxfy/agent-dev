"""
SkillsRegistry — Facade + cache-aside 快照缓存（US1, T016）

设计（架构 §C）：
- Facade：把"扫描两源 + 合并 + 构建 snapshot + 缓存"简化为 `snapshot() -> SkillSnapshot`
- Cache-Aside：mtime 失效的惰性缓存（research Decision 3，不引 watchdog）
- version 单调递增（INV-6）：每次重建 +1
- 串行化：threading.Lock 保证并发 snapshot() 不重复重建

US1：仅扫 workspace 一源（或 bundled 一源）；US5 (T049) 扩展多源合并。

002-skill-secret-injection (T005):
- __init__ 末尾调 _validate_entries_against_skills: 一次性聚合所有错误抛 ConfigValidationError
- 静态校验(static at load, 不读 env 不读文件):
  1. entries key MUST ⊆ loaded skill names (unknown skill name → 报错)
  2. entry.secrets key MUST ⊆ skill.metadata.requires.env (secret not required → 报错)
"""

from __future__ import annotations

import logging
import os
import threading
from pathlib import Path
from typing import Optional

from agent_core.skills.config import SkillsConfig
from agent_core.skills.env_overrides import ConfigValidationError, SkillEntryConfig
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
        # 002-skill-secret-injection (T005): __init__ 末尾校验 entries 字段
        # 懒到首次 snapshot() 前不报错(skill 还没加载 → entries 校验无意义)
        # 但 __init__ 已经能 load entries; 走 lazy 路径: 在 _rebuild 后调一次校验
        # 这样 unknown skill name 错误在首次 snapshot() 时才报(load_all 已跑)

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
    def entries(self) -> list[SkillEntry]:
        """当前缓存的所有 SkillEntry 列表（US1 secret injection 消费）。

        002-skill-secret-injection (T009/T012, 2026-07-06):
        SkillsPromptHandler 调此属性取 list[SkillEntry] 喂给
        apply_skill_env_overrides(entries, skills_config) — 后者需要每个
        entry 的 metadata.requires.env 来过滤哪些 key 应注入。

        触发 snapshot() 以确保最近一次加载结果可用；空 list 表示还没加载。
        """
        if not self._last_entries_by_name:
            self.snapshot()
        return list(self._last_entries_by_name.values())

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
        """重建快照 + bump version + 更新缓存。

        T005: 重建后首次调 _validate_entries_against_skills（一次性聚合错误），
        把 loaded skills + config.entries 一起校验。后续 rebuild 缓存命中跳过校验。
        """
        self._version += 1
        entries = self._load_all()
        # US4：缓存 by-name 索引（slash 派发查表用）
        self._last_entries_by_name = {e.skill.name: e for e in entries}
        # T005: entries 字段校验(只在首次 rebuild 跑, 缓存命中路径不重复)
        self._validate_entries_against_skills(entries)
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

    def _validate_entries_against_skills(
        self, entries: list[SkillEntry]
    ) -> None:
        """002-skill-secret-injection (T005): 校验 config.entries 字段。

        静态校验(不读 env / 不读文件), 在 _rebuild 末尾跑一次:

        1. config.entries 为 None / 空 → no-op(legacy 模式)
        2. entries key ⊆ loaded skill names? 不在 → 报错
        3. entry.secrets key ⊆ skill.metadata.requires.env? 不在 → 报错

        错误聚合: 一次性收集所有错误后 raise ConfigValidationError
        (data-model §Validation Rules + spec FR-005/FR-006)。
        """
        cfg_entries = getattr(self._config, "entries", None)
        if not cfg_entries:
            return  # legacy / 空 dict → 无需校验

        loaded_skill_names = {e.skill.name for e in entries}
        loaded_by_name = {e.skill.name: e for e in entries}
        errors: list[str] = []

        for skill_name, entry_cfg in cfg_entries.items():
            # 1. unknown skill name 检查
            if skill_name not in loaded_skill_names:
                errors.append(
                    f"skills.entries 包含未知 skill 名 {skill_name!r}: "
                    f"loaded skills={sorted(loaded_skill_names)}"
                )
                continue  # 没 loaded → 跳过 requires 校验(无意义)

            # 2. secret key ⊆ requires.env 检查
            entry = loaded_by_name[skill_name]
            required_env = set()
            metadata = getattr(entry, "metadata", None)
            if metadata is not None and metadata.requires is not None:
                required_env = set(metadata.requires.env or ())

            secrets_keys = set(entry_cfg.secrets.keys())
            unknown_secrets = secrets_keys - required_env
            if unknown_secrets:
                errors.append(
                    f"skill {skill_name!r} 的 secrets {sorted(unknown_secrets)} "
                    f"不在 requires.env={sorted(required_env)} 中"
                )

        if errors:
            # 聚合所有错误一次性抛(spec FR-006)
            joined = "\n".join(f"  - {e}" for e in errors)
            logger.error(
                "❌ skills.entries 配置校验失败:\n%s", joined,
            )
            raise ConfigValidationError(
                f"skills.entries 配置校验失败 ({len(errors)} 处错误):\n{joined}"
            )


__all__ = ["SkillsRegistry"]
