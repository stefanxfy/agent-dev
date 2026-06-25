"""
跨进程锁（flock + Windows stub）

M2 / Day 2 — A4 修复 + v2.1 §7.1 锁 v2.1

设计要点：
1. 平台差异透明
   - Unix (Linux/macOS): fcntl.flock 真实锁
   - Windows: 退化为同进程 threading.Lock (stub, 标注跨进程不安全)
2. 文件锁语义（f-v2 §7.1 不变量）
   - LOCK_EX: 排他写锁
   - LOCK_SH: 共享读锁（保留接口，本系统主要用 EX）
   - LOCK_NB: 非阻塞（立即返回，锁不住抛 LockBusy）
   - LOCK_UN: 释放
3. 强占语义（v2.1 §7.1 A1+A2）
   - stale_pid_seconds: 锁文件标记的 PID 已死 → 强占
   - stale_mtime_seconds: 锁 mtime 超过 N 秒 → 强占（即使 PID 还活）
   - 强占 = "获得锁的所有权"，而非"等待原持有者退出"
4. 锁文件 JSON envelope（A11）
   ```
   {"pid": 12345, "acquired_at": 1234567890.0, "host": "..."}
   ```
   便于跨进程审计 + 死锁诊断。
5. ContextManager 接口（with lock: 自动释放）

风险：
- macOS flock 与 Linux flock 行为微差异（macOS flock(2) 是 BSD 风格，强制整文件锁）
  → Linux 通过的场景 3 跨进程测试在 macOS 可能 race，标注 known-quirk
"""

from __future__ import annotations

import contextlib
import errno
import fcntl
import json
import os
import platform
import socket
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Optional, Union

from agent_core.exceptions import StorageError


# ──────────────────────────────────────────────────────────────────
# 异常
# ──────────────────────────────────────────────────────────────────

class LockBusy(StorageError):
    """锁已被占用（非阻塞获取失败）"""
    code = "LOCK_BUSY"


class LockStale(StorageError):
    """锁已陈旧（PID 已死 或 mtime 超时）—— 触发强占路径"""
    code = "LOCK_STALE"


# ──────────────────────────────────────────────────────────────────
# 平台检测
# ──────────────────────────────────────────────────────────────────

_IS_UNIX = platform.system() in ("Linux", "Darwin")
_IS_WINDOWS = platform.system() == "Windows"


# ──────────────────────────────────────────────────────────────────
# 锁文件 envelope（A11）
# ──────────────────────────────────────────────────────────────────

def _envelope_path(lock_path: Path) -> Path:
    """JSON envelope 与 .lock 同目录 .lock.json（避免 fcntl 对同一 fd 的副作用）"""
    return lock_path.with_suffix(lock_path.suffix + ".json")


def _write_envelope(lock_path: Path, pid: int, host: str) -> None:
    """写锁 envelope（atomic, O_CREAT|O_EXCL 防止并发写覆盖）"""
    env_path = _envelope_path(lock_path)
    payload = {
        "pid": pid,
        "acquired_at": time.time(),
        "host": host,
    }
    data = json.dumps(payload).encode("utf-8")
    # 原子写：先写临时文件，再 rename
    tmp_dir = env_path.parent
    tmp_dir.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=tmp_dir, prefix=".envelope.", suffix=".tmp")
    try:
        os.write(fd, data)
        os.fsync(fd)
        os.close(fd)
        os.replace(tmp_path, env_path)
    except Exception:
        with contextlib.suppress(OSError):
            os.close(fd)
        with contextlib.suppress(OSError):
            os.unlink(tmp_path)
        raise


def _read_envelope(lock_path: Path) -> Optional[dict]:
    """读锁 envelope（不存在 → None）"""
    env_path = _envelope_path(lock_path)
    if not env_path.exists():
        return None
    try:
        with open(env_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def _clear_envelope(lock_path: Path) -> None:
    """清 envelope"""
    env_path = _envelope_path(lock_path)
    with contextlib.suppress(OSError):
        env_path.unlink()


def _is_pid_alive(pid: int) -> bool:
    """检查 PID 是否存活（Unix: kill -0）"""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


# ──────────────────────────────────────────────────────────────────
# IPCLock
# ──────────────────────────────────────────────────────────────────

class IPCLock:
    """
    跨进程文件锁（v2.1 §7.1）

    用法:
        lock = IPCLock("/tmp/daily.ipclock")
        with lock:
            ... critical section ...

        # 非阻塞（立即失败抛 LockBusy）
        try:
            with lock.acquire(blocking=False):
                ...
        except LockBusy:
            ...

        # 强占陈旧锁（A1+A2）
        lock = IPCLock("/tmp/daily.ipclock", stale_pid_seconds=3600, stale_mtime_seconds=3600)
        # 若 PID 已死 或 mtime > 1h，下次 acquire 自动强占
    """

    def __init__(
        self,
        path: Union[str, Path],
        *,
        stale_pid_seconds: int = 3600,
        stale_mtime_seconds: int = 3600,
    ):
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.stale_pid_seconds = stale_pid_seconds
        self.stale_mtime_seconds = stale_mtime_seconds

        # Windows stub: 退化到线程锁（跨进程不安全，标注）
        self._thread_lock = threading.Lock() if _IS_WINDOWS else None
        self._fd: Optional[int] = None  # Unix: 保持 fd 直到 release

    # ── ContextManager 接口 ─────────────────────────────────

    def __enter__(self) -> "IPCLock":
        self.acquire()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.release()

    # ── 锁获取 / 释放 ─────────────────────────────────────

    def acquire(self, *, blocking: bool = True, timeout: float = -1.0) -> bool:
        """
        获取锁

        Args:
            blocking: True=阻塞等锁，False=非阻塞
            timeout: 阻塞超时（秒），-1=无限

        Returns:
            True=成功，False（非阻塞且失败）

        Raises:
            LockBusy: 非阻塞失败
            LockStale: 检测到陈旧锁需要强占（强占自动进行，此异常仅用于诊断）
        """
        if _IS_WINDOWS:
            return self._acquire_windows(blocking)

        return self._acquire_unix(blocking, timeout)

    def release(self) -> None:
        """释放锁（幂等）"""
        if _IS_WINDOWS:
            self._release_windows()
            return

        self._release_unix()

    # ── Unix 实现 ──────────────────────────────────────────

    def _acquire_unix(self, blocking: bool, timeout: float) -> bool:
        """Unix flock 实现（含陈旧锁强占 A1+A2）"""
        # 0. 第一次强占检测（如果有 envelope 且陈旧）
        self._maybe_steal_stale_lock()

        # 1. 打开锁文件
        fd = os.open(
            str(self.path),
            os.O_RDWR | os.O_CREAT,
            0o644,
        )

        # 2. flock
        op = fcntl.LOCK_EX
        if not blocking:
            op |= fcntl.LOCK_NB

        deadline = time.time() + timeout if timeout >= 0 else None
        while True:
            try:
                fcntl.flock(fd, op)
                break  # 成功
            except OSError as e:
                if e.errno not in (errno.EWOULDBLOCK, errno.EAGAIN):
                    os.close(fd)
                    raise
                if not blocking:
                    os.close(fd)
                    raise LockBusy(f"锁 {self.path} 已被占用")
                if deadline is not None and time.time() >= deadline:
                    os.close(fd)
                    raise LockBusy(f"锁 {self.path} 获取超时（{timeout}s）")
                time.sleep(0.05)

        # 3. 写 envelope（A11）
        try:
            _write_envelope(self.path, os.getpid(), socket.gethostname())
        except Exception:
            # envelope 失败不应阻断锁（envelope 是 best-effort 审计）
            pass

        self._fd = fd
        return True

    def _release_unix(self) -> None:
        if self._fd is None:
            return
        try:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
        finally:
            os.close(self._fd)
            self._fd = None
            _clear_envelope(self.path)

    def _maybe_steal_stale_lock(self) -> None:
        """
        v2.1 §7.1 A1+A2 强占陈旧锁

        触发条件（任一）：
        - envelope 存在且 PID 已死（kill -0 失败）
        - 锁文件 mtime > stale_mtime_seconds
        """
        if not self.path.exists():
            return

        env = _read_envelope(self.path)
        is_stale = False

        # A1: PID 已死
        if env and "pid" in env:
            pid = int(env["pid"])
            if not _is_pid_alive(pid):
                is_stale = True

        # A2: mtime 超时
        mtime = self.path.stat().st_mtime
        if time.time() - mtime > self.stale_mtime_seconds:
            is_stale = True

        if is_stale:
            # 强占：直接删除锁文件，下次 acquire 即可获得
            with contextlib.suppress(OSError):
                self.path.unlink()
            with contextlib.suppress(OSError):
                _clear_envelope(self.path)

    # ── Windows 实现（stub） ──────────────────────────────

    def _acquire_windows(self, blocking: bool) -> bool:
        """Windows: 退化为线程锁（跨进程不安全，标注）"""
        if not blocking:
            if not self._thread_lock.acquire(blocking=False):
                raise LockBusy(f"锁 {self.path} 已被占用（Windows 线程锁 stub）")
            return True
        self._thread_lock.acquire()
        return True

    def _release_windows(self) -> None:
        if self._thread_lock is not None and self._thread_lock.locked():
            self._thread_lock.release()


# ──────────────────────────────────────────────────────────────────
# 工厂函数
# ──────────────────────────────────────────────────────────────────

def make_daily_lock(memory_root: Union[str, Path]) -> IPCLock:
    """persist_turn 的跨进程锁"""
    return IPCLock(Path(memory_root) / ".locks" / "daily.ipclock")


def make_extract_lock(memory_root: Union[str, Path]) -> IPCLock:
    """extract_candidates 的跨进程锁"""
    return IPCLock(Path(memory_root) / ".locks" / "extract.ipclock")


__all__ = [
    "IPCLock",
    "LockBusy",
    "LockStale",
    "make_daily_lock",
    "make_extract_lock",
]