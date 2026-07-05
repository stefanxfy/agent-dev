"""
共享沙箱清理逻辑(平台/backend 无关)。

设计依据:docs/tool/sandbox-pluggable-design.md §10。

来源:从 agent_core/tools/sandbox_manager.py 的 _get_sandbox_tmp_dir /
_scrub_bare_git / _cleanup_sandbox_tmp_dir / _safe_rmtree 移过来,改为模块级函数。

实现选择(偏差说明):设计 §10 原文写"base class 或共享 mixin"。实施选**模块级函数**
而非 mixin —— 功能等价,但更松耦合(无需多继承,无 MRO 风险),且 SandboxManager
._build_runtime_config 也能直接 import get_sandbox_tmp_dir 复用,不强制走继承链。
"""

from __future__ import annotations

import logging
import os
import shutil
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)
sandbox_logger = logging.getLogger("agent_core.sandbox")


# ────────────────────────────────────────────────────────────────────
# sandbox tmp dir
# ────────────────────────────────────────────────────────────────────

def get_sandbox_tmp_dir() -> str:
    """
    对齐 CC sandboxTmpDir — <tmpdir>/claude-<uid> mode 0o700。

    每个用户独立 tmp dir(防跨用户读写),权限 0o700(仅 owner)。
    SandboxManager._build_runtime_config 把它加进 allowWrite 默认白名单。
    """
    import tempfile

    uid = os.getuid() if hasattr(os, "getuid") else 0
    tmp = Path(tempfile.gettempdir()) / f"claude-{uid}"
    try:
        tmp.mkdir(mode=0o700, exist_ok=True)
    except OSError as e:
        logger.warning("sandbox tmp dir 创建失败: %s", e)
    return str(tmp)


def _safe_rmtree(path: Path) -> None:
    """安全删除目录(异常不抛)。"""
    try:
        shutil.rmtree(path)
    except OSError as e:
        logger.warning("删除 %s 失败: %s", path, e)


# ────────────────────────────────────────────────────────────────────
# bare-git scrub(防 CC #29316)
# ────────────────────────────────────────────────────────────────────

def scrub_bare_git(search_dirs: Optional[list[str]] = None) -> int:
    """
    对齐 CC scrubBareGit — 扫描 .git 残留并删除异常裸 git 目录。

    防 CC #29316 bare-git scrub 攻击:恶意仓库在 .git/config 里塞 alias / hook,
    用户 `cd repo && git status` 时触发 RCE。沙箱执行后扫一遍,删掉异常的裸 .git 目录。

    Args:
        search_dirs: 扫描目录列表(None → [cwd, sandbox_tmp_dir])

    Returns:
        删除的 bare-git 目录数
    """
    if search_dirs is None:
        search_dirs = [os.getcwd(), get_sandbox_tmp_dir()]

    removed = 0
    for search_dir in search_dirs:
        search_path = Path(search_dir)
        if not search_path.exists():
            continue
        try:
            for entry in search_path.iterdir():
                if not entry.is_dir():
                    continue
                name = entry.name
                # sandbox tmp 里的 .git 一定是异常的(沙箱内不该有 git 仓库)
                if search_dir == get_sandbox_tmp_dir() and name == ".git":
                    _safe_rmtree(entry)
                    removed += 1
                # cwd 下的 xxx.git 形式 bare repo
                elif name.endswith(".git") and name != ".git":
                    _safe_rmtree(entry)
                    removed += 1
        except OSError as e:
            logger.warning("bare-git scrub 扫描 %s 失败: %s", search_dir, e)
    if removed:
        logger.info("bare-git scrub: 删除 %d 个异常 .git 目录", removed)
    return removed


# ────────────────────────────────────────────────────────────────────
# sandbox tmp dir mtime 过期
# ────────────────────────────────────────────────────────────────────

def cleanup_sandbox_tmp_dir(max_age_hours: float = 24.0) -> int:
    """
    对齐 CC cleanupSandboxTmpDir — mtime 过期清理。

    sandbox_tmp_dir 里的子目录按 mtime 过期(默认 24h),防止临时文件无限堆积。

    Returns:
        删除的子目录数
    """
    tmp_dir = Path(get_sandbox_tmp_dir())
    if not tmp_dir.exists():
        return 0

    now = time.time()
    max_age_seconds = max_age_hours * 3600
    removed = 0
    try:
        for entry in tmp_dir.iterdir():
            if not entry.is_dir():
                continue
            try:
                mtime = entry.stat().st_mtime
            except OSError:
                continue
            if (now - mtime) > max_age_seconds:
                _safe_rmtree(entry)
                removed += 1
    except OSError as e:
        logger.warning("sandbox tmp dir 清理失败: %s", e)
    if removed:
        logger.info("sandbox tmp cleanup: 删除 %d 个过期子目录", removed)
    return removed


# ────────────────────────────────────────────────────────────────────
# 组合入口
# ────────────────────────────────────────────────────────────────────

def run_default_cleanup(backend_name: str = "?") -> None:
    """
    默认 cleanup:bare-git scrub + tmp 过期。

    各 backend.cleanup() 调此(NativeBackend / SrtBackend 共享同一套清理逻辑)。
    NullBackend 也调(对 host 上执行的命令也跑 bare-git 防护)。
    """
    sandbox_logger.debug("⚙️ [sandbox_cleanup_start] backend=%s", backend_name)
    try:
        scrub_bare_git()
        cleanup_sandbox_tmp_dir()
    except Exception as e:
        logger.warning("沙箱 cleanup 部分失败: %s", e)
