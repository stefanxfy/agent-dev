"""
DistillationLoop —— cron-style 外层循环 (M6 / Day 6)

设计要点:
1. 包装器,不重写:`DistillationScheduler`(distiller.py)已实现单次 run,gates/锁/候选写
   这里只加定时轮询 + 后台 daemon 线程
2. 可中断 sleep:`threading.Event.wait(N)` 替代 `time.sleep(N)`,stop() 能立即唤醒
3. 异常隔离:`run()` 抛异常时记 log + 返回错误 result,不让 loop 线程崩
4. tick_once 公开:无 daemon 也能手动驱动(测试 + 集成场景用)

不在本模块范围:
- ❌ 持久化(crash 重启恢复)— M7 集成阶段
- ❌ 跨进程协调 — 单进程 daemon,跨进程由 IPCLock 兜底
- ❌ 动态调频 — interval_seconds 启动时定,运行中不变

Public API:
- `DistillationLoop(scheduler, on_result=None)` —— 注入 scheduler(便于 mock)
- `tick_once() -> Optional[DistillationResult]` —— 一次检查 + 一次 run
- `start(interval_seconds=300)` —— 起 daemon 线程
- `stop(timeout=5.0)` —— 通知 loop 退出,等线程 join
- `is_running -> bool` —— 检查是否在跑
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from agent_core.memory.distiller import (
    DistillationResult,
    DistillationScheduler,
)
from agent_core.memory.tracing import tracer

logger = logging.getLogger("memory.scheduler")

__all__ = ["DistillationLoop"]


class DistillationLoop:
    """
    cron-style 蒸馏循环

    用法 1 — 手动驱动(测试 / 集成场景):
        loop = DistillationLoop(scheduler)
        result = loop.tick_once()

    用法 2 — 后台 daemon(生产):
        loop = DistillationLoop(scheduler)
        loop.start(interval_seconds=300)  # 每 5 分钟
        ...
        loop.stop(timeout=5.0)
    """

    def __init__(
        self,
        scheduler: DistillationScheduler,
        on_result: Optional[Callable[[Optional[DistillationResult]], None]] = None,
    ):
        self._scheduler = scheduler
        self._on_result = on_result

        self._stop_event: Optional[threading.Event] = None
        self._thread: Optional[threading.Thread] = None
        self._interval_seconds: int = 0
        self._tick_count: int = 0

        # M10 C3.2: 状态可观测字段
        self._last_tick_at: Optional[str] = None  # ISO timestamp of last tick
        self._last_result: Optional[DistillationResult] = None  # last run result (None if gate 拦住)

    # ── 公开 API ──────────────────────────────────────

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def tick_count(self) -> int:
        """累计 tick 次数(测试用)"""
        return self._tick_count

    def tick_once(self) -> Optional[DistillationResult]:
        """
        一次检查 + 一次 run。

        Returns:
            DistillationResult: gate 通过 + run 跑完(成功或失败)
            None: gate 拦住(没触发 run)
        """
        self._tick_count += 1

        with tracer.start_as_current_span("memory.loop.tick") as span:
            ok, reason = self._scheduler.should_distill()
            span.set_attribute("memory.distill.gate_ok", ok)
            span.set_attribute("memory.distill.gate_reason", reason)

            if not ok:
                logger.debug(f"tick_once gate 拦住: {reason}")
                # M10 C3.2: 状态可观测 — 必须在 _fire_callback 之前更新
                self._last_tick_at = datetime.now(timezone.utc).isoformat()
                self._last_result = None
                self._fire_callback(None)
                return None

            try:
                # 真跑(dry_run=False 才会写候选到 _candidate/)
                result = self._scheduler.run(dry_run=False)
            except Exception as e:
                # 异常隔离:不让 loop 崩
                logger.exception(f"tick_once run 异常: {e}")
                result = DistillationResult(success=False, error=str(e))

            span.set_attribute("memory.distill.success", result.success)
            span.set_attribute("memory.distill.candidates", len(result.candidates))
            # M10 C3.2: 状态可观测 — callback 抛错也不能阻断字段更新
            self._last_tick_at = datetime.now(timezone.utc).isoformat()
            self._last_result = result
            self._fire_callback(result)
            return result

    def start(self, interval_seconds: int = 300) -> None:
        """
        起 daemon 线程,每 interval_seconds 调一次 tick_once。

        Args:
            interval_seconds: 间隔秒数(M6 demo 用 5,生产建议 300)
        """
        if self.is_running:
            logger.warning("DistillationLoop 已在跑,start() 忽略")
            return

        if interval_seconds < 1:
            raise ValueError(f"interval_seconds 必须 >= 1,实际 {interval_seconds}")

        self._interval_seconds = interval_seconds
        self._stop_event = threading.Event()
        self._thread = threading.Thread(
            target=self._loop,
            daemon=True,
            name="distill-loop",
        )
        self._thread.start()
        logger.info(f"DistillationLoop started, interval={interval_seconds}s")

    def stop(self, timeout: float = 5.0) -> bool:
        """
        通知 loop 退出,等线程 join(最多 timeout 秒)。

        Returns:
            True = loop 已退出;False = 超时(loop 仍在跑)
        """
        if not self.is_running:
            return True

        assert self._stop_event is not None
        self._stop_event.set()
        assert self._thread is not None
        self._thread.join(timeout=timeout)
        stopped = not self._thread.is_alive()
        if stopped:
            logger.info("DistillationLoop stopped")
        else:
            logger.warning(f"DistillationLoop stop 超时 {timeout}s")
        return stopped

    def get_status(self) -> dict[str, Any]:
        """M10 C3.2: sidebar 用的状态摘要

        Returns:
            {
                "running": bool,
                "tick_count": int,
                "interval_seconds": int,
                "last_tick_at": ISO str or None,
                "last_result_success": bool or None,  # None = no result yet
                "last_candidates_count": int or None,
            }
        """
        return {
            "running": self.is_running,
            "tick_count": self.tick_count,
            "interval_seconds": self._interval_seconds,
            "last_tick_at": self._last_tick_at,
            "last_result_success": self._last_result.success if self._last_result else None,
            "last_candidates_count": len(self._last_result.candidates) if self._last_result else None,
        }

    # ── 内部 ────────────────────────────────────────────

    def _loop(self) -> None:
        """daemon 线程主循环"""
        assert self._stop_event is not None
        # 第一次立即 tick(不等满 interval)
        self.tick_once()
        while not self._stop_event.is_set():
            # 可中断 sleep:stop() 设 event 后立即醒
            if self._stop_event.wait(self._interval_seconds):
                break
            self.tick_once()

    def _fire_callback(self, result: Optional[DistillationResult]) -> None:
        """调 on_result 回调(若有);异常隔离不让 callback 干掉 loop"""
        if self._on_result is None:
            return
        try:
            self._on_result(result)
        except Exception:
            logger.exception("DistillationLoop on_result 回调异常")