"""
Skill 路径校验与解析

设计要点（精简版，借鉴 agent_core/memory/path_validator.py 但更轻）：
1. skills 是只读发现（不像 memory 有写入），路径校验聚焦：
   - symlink SKILL.md 拒绝（防逃逸，对齐 OpenClaw local-loader）
   - skill_dir 不逃逸出 source root（防 ../ 穿越）
2. body 相对路径解析：把 SKILL.md body 中的相对引用解析到 skill base_dir
3. 不抛通用 Exception，统一抛 PathSecurityError（复用 memory 的异常）

注：v1 不实现 memory 的 4 层 Unicode/NFD 防御——skills 来源受信（bundled 包内 +
workspace 项目内），威胁面远小于用户输入路径。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Union

from agent_core.memory.path_validator import PathSecurityError


def resolve_skill_dir(raw: Union[str, Path]) -> Path:
    """展开 ~ + 规范化 skill 目录路径（不要求存在）"""
    return Path(raw).expanduser()


def reject_symlink_skill_md(skill_md_path: Path) -> None:
    """
    拒绝 SKILL.md 是 symlink（防 symlink 逃逸，对齐 OpenClaw local-loader）。

    Args:
        skill_md_path: SKILL.md 路径

    Raises:
        PathSecurityError: 若该路径是 symlink
    """
    if skill_md_path.is_symlink():
        raise PathSecurityError(
            f"SKILL.md 不得是 symlink（安全约束）：{skill_md_path}"
        )


def ensure_within_root(skill_dir: Path, root: Path) -> None:
    """
    校验 skill_dir 解析后仍在 root 内（防 ../ 穿越）。

    Args:
        skill_dir: 待校验的 skill 目录（已 resolve）
        root: 来源根目录（已 resolve）

    Raises:
        PathSecurityError: 若 skill_dir 逃逸出 root
    """
    skill_dir = skill_dir.resolve()
    root = root.resolve()
    skill_str = str(skill_dir)
    root_str = str(root)
    if skill_str != root_str and not skill_str.startswith(root_str + os.sep):
        raise PathSecurityError(
            f"skill 目录越界：{skill_str!r} 不在来源根 {root_str!r} 内"
        )


def resolve_relative_ref(skill_base_dir: Path, ref: str) -> Path:
    """
    把 SKILL.md body 中的相对引用解析为绝对路径（相对 skill base_dir）。

    用于 US2 (T029)：body 中 references/cheatsheet.md → skill_base_dir/references/cheatsheet.md

    Args:
        skill_base_dir: skill 目录（SKILL.md 父目录）
        ref: body 中的相对路径字符串

    Returns:
        解析后的绝对路径（不保证存在）
    """
    # 绝对路径原样返回（尊重作者显式绝对路径）
    if os.path.isabs(ref):
        return Path(ref)
    return (skill_base_dir / ref).resolve()


__all__ = [
    "resolve_skill_dir",
    "reject_symlink_skill_md",
    "ensure_within_root",
    "resolve_relative_ref",
    "PathSecurityError",  # re-export
]
