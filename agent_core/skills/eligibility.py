"""
eligibility — skill 资格即时求值（US3, T037）

状态机（data-model §6）：
    load_error → ERRORED
    metadata.always == true → ELIGIBLE（绕过）
    evaluate_requires(bins/anyBins/env/config/os) → 全满足 ELIGIBLE / 否则 MISSING_REQUIREMENTS

纯函数 + 参数注入（架构 §E 陷阱 1：config 须参数注入，禁止读全局 Config）。
- env 默认读 os.environ（生产）；测试注入以验证 INV-5 即时性
- config 默认 {} （v1 未接 agent Config；requires.config 视为 unmet）
- platform_os 默认 platform.system().lower()；测试注入
"""

from __future__ import annotations

import logging
import platform
import shutil
from typing import Any, Optional

from agent_core.skills.types import (
    SkillEligibilityState,
    SkillEntry,
    SkillMetadata,
)


logger = logging.getLogger("agent_core.skills")


def _current_os() -> str:
    """当前 OS 小写名（platform.system().lower()；darwin/linux/windows）"""
    return platform.system().lower()


def _config_truthy(config: dict[str, Any], dotted_path: str) -> bool:
    """
    点号分隔路径取值并判断 truthy（requires.config）。

    例：config={"channels": {"x": 1}}, path="channels.x" → True
    路径不存在 / 中间非 dict / 值 falsy → False。
    """
    cursor: Any = config
    for part in dotted_path.split("."):
        if isinstance(cursor, dict) and part in cursor:
            cursor = cursor[part]
        else:
            return False
    return bool(cursor)


def evaluate_eligibility(
    entry: SkillEntry,
    *,
    env: Optional[dict[str, str]] = None,
    config: Optional[dict[str, Any]] = None,
    platform_os: Optional[str] = None,
) -> tuple[SkillEligibilityState, tuple[str, ...]]:
    """
    求 skill 当前资格（即时，纯函数；data-model §6）。

    Args:
        entry: 已加载的 SkillEntry
        env: 环境变量 dict（None → os.environ；测试注入验 INV-5）
        config: 配置 dict（None → {}，requires.config 视为 unmet）
        platform_os: OS 名（None → platform.system().lower()）

    Returns:
        (state, missing) — missing 为缺失项的人类可读列表（供 status/警告）
    """
    # 1. 加载失败 → ERRORED
    if entry.load_error is not None:
        return SkillEligibilityState.ERRORED, (entry.load_error,)

    md: SkillMetadata = entry.metadata or SkillMetadata()

    # 2. always 绕过
    if md.always:
        return SkillEligibilityState.ELIGIBLE, ()

    env_map = env if env is not None else _read_environ()
    config_map = config if config is not None else {}
    cur_os = platform_os if platform_os is not None else _current_os()

    missing: list[str] = []
    req = md.requires

    # 3. bins：全部 shutil.which 命中
    for b in req.bins:
        if shutil.which(b) is None:
            missing.append(f"bin:{b}")

    # 4. anyBins：任一命中即可
    if req.any_bins and not any(shutil.which(b) is not None for b in req.any_bins):
        missing.append(f"anyBins:{'|'.join(req.any_bins)}")

    # 5. env：全部存在
    for e in req.env:
        if e not in env_map:
            missing.append(f"env:{e}")

    # 6. config：全部 truthy（点号分隔路径）
    for c in req.config:
        if not _config_truthy(config_map, c):
            missing.append(f"config:{c}")

    # 7. os：当前 os 在白名单
    if md.os and cur_os not in md.os:
        missing.append(f"os:{cur_os}∉{list(md.os)}")

    if missing:
        logger.debug(
            "🧩 eligibility: skill=%s MISSING %s",
            entry.skill.name, missing,
        )
        return SkillEligibilityState.MISSING_REQUIREMENTS, tuple(missing)

    logger.debug("🧩 eligibility: skill=%s ELIGIBLE", entry.skill.name)
    return SkillEligibilityState.ELIGIBLE, ()


def _read_environ() -> dict[str, str]:
    """读 os.environ（隔离以便测试 monkeypatch）"""
    import os
    return dict(os.environ)


__all__ = ["evaluate_eligibility"]
