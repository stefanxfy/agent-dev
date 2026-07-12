"""
McpHealthScheduler — MCP server health-check 调度器（从 McpManager 抽出，C.5 M-M1）。

职责:
  - 后台 loop 每 _health_interval 秒轮询 top-_health_concurrency 个最久未检测 server
  - connected → ping list_tools；disconnected → spawn keep_alive 重连
  - 维护 frozen _ServerHealth 快照（整体替换 → GIL 下跨线程读安全）
  - dispose 时经 _sleep_or_dispose 打断 + cancel task 优雅退出

设计:
  - 持有 McpManager back-reference，用于访问 _configs / _servers / _keep_alive_tasks /
    _keep_alive / _safe_on_tools_changed / _loop / _closed。
  - 自身状态：_health / _health_task / _health_sleep_futs / _health_interval / _health_concurrency
  - McpManager 通过 self._scheduler 持有本类，connect_all → ensure_task()，dispose → stop()
  - 公开 API：get_health / set_health / init_health / ensure_task / stop
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from agent_core.mcp.manager import McpManager

logger = logging.getLogger("agent_core.mcp")  # 🔌


@dataclass(frozen=True)
class _ServerHealth:
    """单个 server 的健康状态快照（frozen，整体替换 → GIL 下跨线程读安全）。

    status: "connected" / "disconnected" / "connecting"
    last_check_at: time.monotonic() 时间戳（调度器按此取 top-K 最久未检测）
    last_error: 最近一次检测/断连错误（给 UI 显示）
    """
    status: str
    last_check_at: float
    last_error: Optional[str] = None


class McpHealthScheduler:
    """MCP server health-check 调度器（C.5 M-M1：从 McpManager 抽出的独立类）。

    持有 McpManager 的 back-reference，本身维护 health 状态 + 后台 task。
    McpManager 暴露给外界的 health 公共 API（get_health）已 delegate 到本类。
    """

    def __init__(self, manager: "McpManager", *, interval: float, concurrency: int):
        """
        Args:
            manager: 所属 McpManager 实例（back-reference，用于访问 _configs /
                     _servers / _keep_alive_tasks / _loop / _closed / _keep_alive /
                     _safe_on_tools_changed）
            interval: probe 间隔（秒），由 McpManager 从 env MCP_HEALTH_CHECK_INTERVAL 读
            concurrency: 每轮最多 probe 的 server 数（top-K 限并发）
        """
        self._mgr = manager
        self._health_interval = float(interval)
        self._health_concurrency = max(1, int(concurrency))
        self._health: dict[str, _ServerHealth] = {}            # 所有 enabled server 健康状态
        self._health_task: Optional[asyncio.Task] = None
        self._health_sleep_futs: list = []                    # dispose 打断 sleep 用

    # ── 公共 API ─────────────────────────────────────────────────────

    def get_health(self) -> dict:
        """返回 {name: {status, last_check_at, last_error}} 快照（UI 跨线程读，GIL 安全）。

        镜像 DistillationLoop.get_status 范式：返回普通 dict，主线程读安全。
        """
        return {
            n: {"status": h.status, "last_check_at": h.last_check_at,
                "last_error": h.last_error}
            for n, h in self._health.items()
        }

    def set_health(self, name: str, status: str, last_error: Optional[str] = None) -> None:
        """整体替换 _health[name]（frozen _ServerHealth，GIL 下跨线程读安全）。"""
        self._health[name] = _ServerHealth(status, time.monotonic(), last_error)

    def init_health(self, name: str) -> None:
        """初始化 health 状态为 connecting（仅当尚未初始化，setdefault 语义）。

        跨会话 mgr 复用时，已 connected 的 server 不会被重置。
        """
        self._health.setdefault(name, _ServerHealth("connecting", 0.0))

    def ensure_task(self) -> None:
        """在常驻 loop 内启动 health-check 调度器（幂等，只启一次）。

        由 McpManager.connect_all 通过 loop.call_soon_threadsafe 调度到 mcp loop 线程。
        """
        if self._mgr._closed or self._mgr._loop is None:
            return
        if self._health_task is not None and not self._health_task.done():
            return
        self._health_task = self._mgr._loop.create_task(self._health_loop())

    def stop(self) -> None:
        """dispose 时调：打断 pending sleep + 等 task 退出。

        由 McpManager.dispose 调（loop 线程内）。本方法内部捕获 RuntimeError（loop 已停）。
        """
        # 1. set 所有 pending sleep future，让 _health_loop 立即退出
        if self._mgr._loop is not None:
            try:
                self._mgr._loop.call_soon_threadsafe(self._wake_health_sleeps)
            except RuntimeError:
                pass   # loop 已停
        # 2. 等 task 退出
        if self._health_task is not None and not self._health_task.done():
            try:
                fut = asyncio.run_coroutine_threadsafe(
                    asyncio.wait_for(self._health_task, timeout=5.0), self._mgr._loop)
                fut.result(timeout=7.0)
            except Exception as e:
                logger.debug("🔌 health_task 退出超时/异常: %s", e)
        self._health_sleep_futs.clear()

    # ── 内部 loop ────────────────────────────────────────────────────

    async def _health_loop(self) -> None:
        """集中 health-check 调度器（跑在常驻 loop）。

        每 _health_interval 秒一轮：取 top-_health_concurrency 个最久未检测的 server
        （last_check_at 最小），并行 probe。connected → ping list_tools；disconnected →
        spawn 重连。错开不批量（top-K 限并发）。dispose 经 _sleep_or_dispose 打断。
        """
        while not self._mgr._closed:
            await self._sleep_or_dispose(self._health_interval)
            if self._mgr._closed:
                return
            # 取 top-K 最久未检测（快照后排序，避免迭代中 _health 变动）
            snapshot = list(self._health.items())
            snapshot.sort(key=lambda nh: nh[1].last_check_at)
            batch = [n for n, _ in snapshot[: self._health_concurrency]]
            if batch:
                await asyncio.gather(*[self._probe(n) for n in batch], return_exceptions=True)

    async def _probe(self, name: str) -> None:
        """探测单个 server：connected → ping；disconnected/connecting → spawn 重连。"""
        cfg = self._mgr._configs.get(name)
        if cfg is None:
            return
        if name in self._mgr._servers:   # connected → ping list_tools
            try:
                await asyncio.wait_for(
                    self._mgr._servers[name].session.list_tools(),
                    timeout=self._health_interval,
                )
                self.set_health(name, "connected")
            except Exception as e:
                logger.warning("🔌 mcp server '%s' health-check ping 失败 → 断连: %s", name, e)
                await self._teardown_server(name)
                self.set_health(name, "disconnected", last_error=str(e))
        else:   # disconnected/connecting → spawn 重连（不阻塞调度器；ready=None 无人 await）
            task = self._mgr._keep_alive_tasks.get(name)
            if task is None or task.done():
                self.set_health(name, "connecting")
                self._mgr._keep_alive_tasks[name] = self._mgr._loop.create_task(
                    self._mgr._keep_alive(name, cfg, None))

    async def _teardown_server(self, name: str) -> None:
        """ping 失败时：pop _servers + cancel keep_alive task + 注销幽灵工具。"""
        self._mgr._servers.pop(name, None)
        task = self._mgr._keep_alive_tasks.get(name)
        if task is not None and not task.done():
            task.cancel()
        self._mgr._safe_on_tools_changed(name)

    async def _sleep_or_dispose(self, seconds: float) -> None:
        """可被 dispose 打断的 sleep：创建 future，wait_for(timeout)；dispose set 全部 future。"""
        fut = self._mgr._loop.create_future()
        self._health_sleep_futs.append(fut)
        try:
            await asyncio.wait_for(fut, timeout=seconds)
        except asyncio.TimeoutError:
            pass   # 正常到时
        finally:
            if fut in self._health_sleep_futs:
                self._health_sleep_futs.remove(fut)

    def _wake_health_sleeps(self) -> None:
        """dispose 时调（loop 线程内）：set 所有 pending sleep future，让 _health_loop 立即退出。"""
        for fut in list(self._health_sleep_futs):
            if not fut.done():
                fut.set_result(None)
        self._health_sleep_futs.clear()