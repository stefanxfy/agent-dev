"""tests/test_mcp_health.py — McpHealthScheduler 独立单测（C.5 M-M1）。

覆盖:
1. set_health / get_health 整体替换 snapshot
2. init_health setdefault 语义（已存在不覆盖）
3. ensure_task 幂等（第二次调不重启 task）
4. stop 打断 pending sleep + 等 task 退出
5. stop 在 loop 已停时安全（RuntimeError 吞掉）

注：scheduler 的内部 loop / probe / teardown 路径已由 test_mcp_reconnect.py
覆盖（端到端：kill/restart/disconnect/reconnect），本文件只测 scheduler 自身
边界条件 + 公开 API 行为。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from agent_core.mcp.config import McpServerConfig
from agent_core.mcp.health import McpHealthScheduler, _ServerHealth
from agent_core.mcp.manager import McpManager


async def _wait_task_done(task, timeout):
    """辅助：等到 task 完成或超时（屏蔽 cancel/timeout 异常）。"""
    try:
        await asyncio.wait_for(asyncio.shield(task), timeout=timeout)
    except (asyncio.CancelledError, asyncio.TimeoutError):
        pass


# ── helpers ────────────────────────────────────────────────────────
def _make_mgr_for_scheduler(health_interval=0.5):
    """构造一个 manager + scheduler，loop 已起来（ensure_loop 触发）。

    health_interval 默认 0.5s（测试加速，避免 30s 默认值让 stop 超时）。
    """
    mgr = McpManager([])
    mgr._ensure_loop()   # 启常驻 loop（scheduler 需要 _mgr._loop 引用）
    mgr._scheduler._health_interval = health_interval
    return mgr, mgr._scheduler


# ── set_health / get_health ─────────────────────────────────────────
class TestSetGetHealth:
    def test_set_and_get_roundtrip(self):
        """set_health(name, status) → get_health() 返回对应 status。"""
        mgr, sch = _make_mgr_for_scheduler()
        try:
            sch.set_health("s1", "connected")
            sch.set_health("s2", "disconnected", last_error="boom")
            snap = sch.get_health()
            assert snap["s1"]["status"] == "connected"
            assert snap["s1"]["last_error"] is None
            assert snap["s2"]["status"] == "disconnected"
            assert snap["s2"]["last_error"] == "boom"
            # 快照里应含 last_check_at 时间戳
            assert snap["s1"]["last_check_at"] > 0
        finally:
            mgr.dispose()

    def test_set_health_overwrites_existing(self):
        """set_health 整体替换（非 merge）。"""
        mgr, sch = _make_mgr_for_scheduler()
        try:
            sch.set_health("s1", "disconnected", last_error="first")
            sch.set_health("s1", "connected")
            snap = sch.get_health()
            assert snap["s1"]["status"] == "connected"
            assert snap["s1"]["last_error"] is None   # 替换后清空
        finally:
            mgr.dispose()

    def test_get_health_empty(self):
        """空 health 字典 → 空快照。"""
        mgr, sch = _make_mgr_for_scheduler()
        try:
            assert sch.get_health() == {}
        finally:
            mgr.dispose()


# ── init_health setdefault 语义 ─────────────────────────────────────
class TestInitHealth:
    def test_init_health_sets_new_server(self):
        """未初始化的 server → 写入 connecting 状态。"""
        mgr, sch = _make_mgr_for_scheduler()
        try:
            sch.init_health("new_server")
            snap = sch.get_health()
            assert "new_server" in snap
            assert snap["new_server"]["status"] == "connecting"
        finally:
            mgr.dispose()

    def test_init_health_does_not_overwrite(self):
        """已存在条目不覆盖（跨会话 mgr 复用关键不变量）。"""
        mgr, sch = _make_mgr_for_scheduler()
        try:
            sch.set_health("s1", "connected", last_error="alive")
            sch.init_health("s1")
            snap = sch.get_health()
            # 应保持 connected + last_error，不被重置
            assert snap["s1"]["status"] == "connected"
            assert snap["s1"]["last_error"] == "alive"
        finally:
            mgr.dispose()


# ── ensure_task 幂等 ───────────────────────────────────────────────
class TestEnsureTask:
    def test_ensure_task_starts_scheduler(self):
        """首次 ensure_task → 创建后台 task。"""
        mgr, sch = _make_mgr_for_scheduler()
        try:
            sch.ensure_task()
            assert sch._health_task is not None
            assert not sch._health_task.done()
        finally:
            mgr.dispose()

    def test_ensure_task_is_idempotent(self):
        """第二次 ensure_task 不重启 task（活跃 task 仍在跑）。"""
        mgr, sch = _make_mgr_for_scheduler()
        try:
            sch.ensure_task()
            task1 = sch._health_task
            sch.ensure_task()
            task2 = sch._health_task
            assert task1 is task2   # 同对象引用，未重启
        finally:
            mgr.dispose()

    def test_ensure_task_after_done_restarts(self):
        """task done 后再 ensure_task → 启动新 task。"""
        mgr, sch = _make_mgr_for_scheduler()
        try:
            sch.ensure_task()
            old = sch._health_task
            # 强制让旧 task 立即结束（cancel + 等 cancel 完成）
            old.cancel()
            fut = asyncio.run_coroutine_threadsafe(
                _wait_task_done(old, timeout=2.0), mgr._loop)
            fut.result(timeout=3.0)
            assert old.done()
            sch.ensure_task()
            new = sch._health_task
            assert new is not None
            assert new is not old
            assert not new.done()
        finally:
            mgr.dispose()

    def test_ensure_task_after_closed_no_op(self):
        """mgr._closed=True → ensure_task 跳过。"""
        mgr, sch = _make_mgr_for_scheduler()
        try:
            mgr._closed = True
            sch.ensure_task()
            # 若跳过则 _health_task 保持 None
            assert sch._health_task is None
        finally:
            mgr._closed = False
            mgr.dispose()


# ── stop 行为 ──────────────────────────────────────────────────────
class TestStop:
    def test_stop_after_ensure_task_exits_quickly(self):
        """ensure_task 后 stop → 不应阻塞（应 < 5s 返回）。

        注：调用方需先设 mgr._closed = True（生产里 dispose 做这件事），
        stop 才能让 task 真正退出（否则 task 会醒来继续下一轮 probe）。
        """
        import time
        mgr, sch = _make_mgr_for_scheduler()
        try:
            sch.ensure_task()
            mgr._closed = True   # 模拟 dispose 流程：先 closed 再 stop
            t0 = time.time()
            sch.stop()
            elapsed = time.time() - t0
            assert elapsed < 5.0, f"stop blocked {elapsed:.1f}s"
            # task 应已 done 或 None
            assert sch._health_task is None or sch._health_task.done()
        finally:
            mgr._closed = False
            mgr.dispose()

    def test_stop_clears_sleep_futs(self):
        """stop 后 _health_sleep_futs 应为空。"""
        mgr, sch = _make_mgr_for_scheduler()
        try:
            sch.ensure_task()
            sch.stop()
            assert sch._health_sleep_futs == []
        finally:
            mgr.dispose()

    def test_stop_without_ensure_task_safe(self):
        """从未 ensure_task 调 stop → 不抛（_health_task is None 跳过等待）。"""
        mgr, sch = _make_mgr_for_scheduler()
        try:
            sch.stop()   # 无 task，stop 应快速返回
            assert sch._health_task is None
        finally:
            mgr.dispose()

    def test_stop_after_loop_stopped_safe(self):
        """loop 已停后 stop → RuntimeError 被吞，不抛。"""
        mgr, sch = _make_mgr_for_scheduler()
        # 触发 loop 关闭但不 dispose
        mgr._loop.call_soon_threadsafe(mgr._loop.stop)
        mgr._loop_thread.join(timeout=2.0)
        # 此时 loop 已停；scheduler.stop 内部 call_soon_threadsafe 应吞 RuntimeError
        sch.stop()   # 不应抛
        assert sch._health_task is None or sch._health_task.done()


# ── 跨线程状态 GIL 安全（frozen dataclass 整体替换）──────────────────
class TestHealthSnapshotThreadSafety:
    def test_snapshot_returns_independent_dict(self):
        """get_health 返回新 dict，与内部状态解耦（避免外部修改污染内部）。"""
        mgr, sch = _make_mgr_for_scheduler()
        try:
            sch.set_health("s1", "connected")
            snap = sch.get_health()
            snap["s1"]["status"] = "tampered"
            # 内部状态不应被外部修改影响
            fresh = sch.get_health()
            assert fresh["s1"]["status"] == "connected"
        finally:
            mgr.dispose()

    def test_set_health_uses_frozen_dataclass(self):
        """_ServerHealth 是 frozen — 整体替换语义保证跨线程读。"""
        h = _ServerHealth("connected", 1.0)
        with pytest.raises(Exception):   # FrozenInstanceError
            h.status = "disconnected"