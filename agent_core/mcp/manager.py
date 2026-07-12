"""
McpManager — 多 server MCP 编排器（常驻 event loop + 同步门面）。

架构（决策 #10）:
  - 一个 daemon 线程跑 loop.run_forever()，所有 server 的 transport+session 全程
    活在这个 loop（MCP ClientSession 绑定单一 loop，不能跨 loop 用）。
  - 对外暴露【同步】API（connect_all / call_tool / dispose），内部用
    run_coroutine_threadsafe(coro, loop).result(timeout) 同步取值；
    超时由 fut.result 真正 cancel 协程（比 asyncio.run 软超时更硬）。
  - 故障隔离: 单 server 连接失败只 warn 跳过，不阻断其他（对齐 OpenClaw/CC）。
  - 生命周期: dispose 幂等（_closed 守卫）+ atexit 注册；mgr 跨会话复用
    （agent.close() 不 dispose，Phase 5），atexit 兜底进程退出清理。
  - server 级 deny strip 第一道（connect 前过滤，省连接开销）。

keep_alive + health-check（Phase 5 + C.5 拆分）:
  - _keep_alive: 单 server 单次连接生命周期（connect → 持有 → 断连结束 task）。
  - McpHealthScheduler（C.5 抽出）：集中 health-check 调度器（跑在常驻 loop），
    每 _health_interval 秒取 top-K 最久未检测的 server 并行 probe：
    connected→ping list_tools，disconnected→spawn 重连。
    env: MCP_HEALTH_CHECK_INTERVAL(30s) / MCP_HEALTH_CHECK_CONCURRENCY(5)。
  - 断连：scheduler._teardown_server（pop _servers + cancel task + 注销工具）+
    scheduler.set_health disconnected。
"""

from __future__ import annotations

import asyncio
import atexit
import logging
import threading
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Optional

from agent_core.mcp.client import connect_server
from agent_core.mcp.health import McpHealthScheduler, _ServerHealth   # C.5:re-export _ServerHealth
from agent_core.mcp.materialize import (
    _normalize_call_result,
    _normalize_prompt_result,
    _normalize_resource_result,
    materialize_tools,
)
from agent_core.mcp.names import is_server_denied

logger = logging.getLogger("agent_core.mcp")  # 🔌


@dataclass
class _LiveServer:
    """已连接 server 的运行时状态。"""
    session: Any       # mcp.ClientSession
    tool_defs: list    # list[ToolDef]
    resources: list = field(default_factory=list)   # list[types.Resource]
    prompts: list = field(default_factory=list)     # list[types.Prompt]


class McpManager:
    """多 server MCP 编排器（跨会话复用，常驻 loop + 同步门面）。

    C.5 拆分后，health-check 调度逻辑委托给 self._scheduler（McpHealthScheduler）。
    本类聚焦：loop 管理 / 连接编排 / 同步门面 / 资源-提示 facade / 生命周期。
    """

    def __init__(
        self,
        server_configs,
        *,
        deny_rules: Optional[list] = None,
        connect_timeout: float = 30.0,
        call_timeout: float = 60.0,
        roots: Optional[list] = None,
        on_tools_changed=None,
    ):
        """
        Args:
            server_configs: McpServerConfig 列表（来自 config.load_mcp_config_from_settings）
            deny_rules: server 级 deny 规则字符串列表（组合根注入）
            connect_timeout: 单 server 连接（initialize+list）超时
            call_timeout: 单次 call_tool 默认超时
            roots: client 级 roots（[{uri,name}]，声明给 server 的可访问根目录）
            on_tools_changed: tools 变化（list_changed / 断连注销 / 重连注册）回调
                              (server_name)->None，组合根用它同步 ToolRegistry
        """
        self._configs = {c.name: c for c in server_configs}
        self._deny_rules = list(deny_rules or [])
        self._connect_timeout = connect_timeout
        self._call_timeout = call_timeout
        self._roots = list(roots or [])
        self._on_tools_changed = on_tools_changed
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._loop_thread: Optional[threading.Thread] = None
        self._servers: dict[str, _LiveServer] = {}
        self._dispose_events: dict[str, asyncio.Event] = {}      # loop-bound
        self._keep_alive_tasks: dict[str, asyncio.Task] = {}
        self._pending_refresh: dict[str, set[str]] = {}   # name -> {kinds}（连接阶段 race 暂存）
        self._closed = False
        # C.5:health-check 调度器（独立类，env 配置由 manager 注入 interval/concurrency）
        try:
            from agent_core.config import config
            health_interval = float(config.int("MCP_HEALTH_CHECK_INTERVAL", 30))
            health_concurrency = max(1, config.int("MCP_HEALTH_CHECK_CONCURRENCY", 5))
        except Exception:
            health_interval = 30.0
            health_concurrency = 5
        self._scheduler = McpHealthScheduler(
            self, interval=health_interval, concurrency=health_concurrency,
        )
        # active agent/registry 引用（组合根每次 rerun set_active 更新）。
        # on_tools_changed 回调跑在 mcp loop 线程，不能查 streamlit session_state（线程局部），
        # 改用这两个引用（跨线程 GIL 安全的引用读）。
        self._active_agent: Any = None
        self._active_registry: Any = None

    # ── 常驻 loop ───────────────────────────────────────────────────
    def _ensure_loop(self):
        if self._loop is not None:
            return
        self._loop = asyncio.new_event_loop()
        self._loop_thread = threading.Thread(
            target=self._loop.run_forever, daemon=True, name="mcp-loop")
        self._loop_thread.start()
        atexit.register(self.dispose)

    def _run(self, coro, timeout: float):
        """提交协程到常驻 loop，阻塞取值。

        超时由内层 asyncio.wait_for 真 cancel 协程（避免 dispose 时残留 pending task）；
        外层 fut.result(timeout+2) 略宽兜底（正常由内层 wait_for 先超时返回）。
        """
        if self._closed:
            raise RuntimeError("McpManager 已关闭")
        self._ensure_loop()

        async def _wrap():
            task = asyncio.ensure_future(coro)
            try:
                return await asyncio.wait_for(task, timeout=timeout)
            except asyncio.TimeoutError:
                task.cancel()
                raise TimeoutError(f"MCP 操作超时({timeout}s)") from None

        fut = asyncio.run_coroutine_threadsafe(_wrap(), self._loop)
        return fut.result(timeout=timeout + 2)

    # ── 连接编排 ────────────────────────────────────────────────────
    def _should_connect(self, name: str, cfg) -> bool:
        """C.3 M-C2:判断 server 是否应被连接(enabled + 非 server 级 deny)。

        返回 True 表示应连接,False 表示跳过(已 log 原因)。
        """
        if not cfg.enabled:
            logger.debug("🔌 mcp server '%s' disabled, skip", name)
            return False
        if is_server_denied(name, self._deny_rules):
            logger.info("🔌 mcp server '%s' 被 server 级 deny，不连接", name)
            return False
        return True

    def _init_health_for_new_server(self, name: str) -> None:
        """C.3 M-C2 + C.5:初始化 health 状态为 connecting（仅当尚未初始化，setdefault 语义）。

        delegate 给 scheduler；保留本方法做 facade（C.3 helper 不变）。
        """
        self._scheduler.init_health(name)

    def _connect_one_sync(self, name: str, cfg) -> list:
        """C.3 M-C2:同步包装单 server 连接 + 故障隔离(失败只 warn 返 [])。"""
        try:
            tool_defs = self._run(
                self._connect_one(name, cfg),
                timeout=cfg.connect_timeout or self._connect_timeout,
            )
            logger.info(
                "🔌 mcp server '%s' 连接成功: %d tools %s",
                name, len(tool_defs), [t.name for t in tool_defs],
            )
            return tool_defs
        except Exception as e:
            logger.warning("🔌 mcp server '%s' 连接失败，跳过: %s", name, e)
            return []

    def connect_all(self) -> dict:
        """
        连接所有 enabled 且未被 deny 的 server，返回 {server_name: [ToolDef]}。

        server 级 deny strip 第一道（is_server_denied）+ per-server 故障隔离
        （单 server 失败只 warn 跳过，返空 list，不阻断其余）。
        启动 health-check 调度器（跨会话 mgr 复用时幂等，只启一次）。

        C.3 M-C2:本方法缩到 ~10 行 orchestrator,3 个 helper 各司其职。
        C.5 M-M1:health 调度委托给 self._scheduler。
        """
        results: dict[str, list] = {}
        for name, cfg in self._configs.items():
            if not self._should_connect(name, cfg):
                continue
            self._init_health_for_new_server(name)
            results[name] = self._connect_one_sync(name, cfg)
        # 启动 health-check 调度器(只启一次,跨会话 mgr 复用时已跑)
        self._ensure_loop()
        self._loop.call_soon_threadsafe(self._scheduler.ensure_task)
        return results

    async def _connect_one(self, name: str, cfg) -> list:
        """在常驻 loop 内: spawn keep_alive task + 等 ready future，返回 tool_defs。"""
        self._dispose_events[name] = asyncio.Event()   # 提前建，避免与 dispose 的 race
        loop = asyncio.get_running_loop()
        ready = loop.create_future()
        self._keep_alive_tasks[name] = loop.create_task(self._keep_alive(name, cfg, ready))
        return await ready   # 等 keep_alive 连接成功（tool_defs）或抛异常

    async def _keep_alive(self, name: str, cfg, ready: Optional[asyncio.Future] = None) -> None:
        """单次连接生命周期：connect_server → materialize → set ready → 持有到 dispose → 退出。

        关键约束: connect_server 的 __aenter__/__aexit__ 必须在同一 task —— SDK transport/session
        内部用 anyio cancel scope，绑定进入它的 task；跨 task exit 会抛
        "Attempted to exit cancel scope in a different task than it was entered in"。

        断连/重连由 _health_loop 调度器统一管理（Phase 5）：本 task 单次连接，transport 断连
        → async with 抛异常 → except 更新 _health 为 disconnected + pop _servers + 注销工具
        → task 结束。调度器后续 probe 该 disconnected server → spawn 新 _keep_alive 重连。
        """
        try:
            async with connect_server(
                name, cfg,
                init_timeout=cfg.connect_timeout or 30.0,
                list_timeout=cfg.connect_timeout or 30.0,
                roots_callback=self._make_roots_callback(),
                message_handler=self._make_message_handler(name),
            ) as handle:
                tool_defs = materialize_tools(name, handle.tools, self)
                self._servers[name] = _LiveServer(
                    session=handle.session, tool_defs=tool_defs,
                    resources=getattr(handle, "resources", None) or [],
                    prompts=getattr(handle, "prompts", None) or [],
                )
                # 消费连接阶段积攒的 pending refresh（list_changed 在 _servers 就绪前到达的 race）
                pending_kinds = self._pending_refresh.pop(name, None)
                if pending_kinds:
                    logger.info("🔌 mcp server '%s' 连接就绪，补刷 %s（连接阶段 list_changed）", name, sorted(pending_kinds))
                    for kind in sorted(pending_kinds):
                        await self._do_refresh(name, kind, self._servers[name], notify=False)
                self._scheduler.set_health(name, "connected")
                self._safe_on_tools_changed(name)   # 注册（首连 + 调度器重连都走这）
                if ready is not None and not ready.done():
                    ready.set_result(self._servers[name].tool_defs)
                await self._dispose_events[name].wait()   # 持有 with 直到 dispose
        except Exception as e:
            if self._closed:
                return   # dispose 中，静默退出
            # 断连：清死 session + 标记 disconnected + 注销工具（调度器后续重连）
            was_live = name in self._servers
            self._servers.pop(name, None)
            self._scheduler.set_health(name, "disconnected", last_error=str(e))
            if ready is not None and not ready.done():
                ready.set_exception(e)   # 首次连接失败 → connect_all warn 跳过（首连才传 ready）
            elif was_live:
                logger.warning("🔌 mcp server '%s' 断连: %s（调度器将重连）", name, e)
                self._safe_on_tools_changed(name)   # 移除工具（断连期间不可见）

    def _safe_on_tools_changed(self, name: str) -> None:
        """调组合根回调 on_tools_changed（unregister + register + 失效 tool_schemas）。

        异常吞掉（只 log warning），不阻断调度器/probe。回调由组合根（web/app.py）注入，
        闭包动态查当前 agent/registry（解 stale 引用）。
        """
        cb = self._on_tools_changed
        if cb is None:
            return
        try:
            cb(name)
        except Exception as e:
            logger.warning("🔌 on_tools_changed 回调异常(server=%s): %s", name, e)

    # ── health-check 调度器（C.5 M-M1：抽到 self._scheduler）──────────
    def get_health(self) -> dict:
        """返回 {name: {status, last_check_at, last_error}} 快照（UI 跨线程读，GIL 安全）。

        C.5 delegate 给 McpHealthScheduler.get_health。
        """
        return self._scheduler.get_health()

    def set_active(self, agent: Any, registry: Any) -> None:
        """组合根每次 rerun 调：更新当前 agent/registry 引用。

        on_tools_changed 回调跑在 mcp loop 线程，用它同步 ToolRegistry——不能查 streamlit
        session_state（ScriptRunContext 线程局部，mcp loop 拿到 None）。主线程 set / loop 线程读，
        GIL 下引用读原子，跨线程安全。每次 rerun 更新也顺带解 stale（agent 重建后指向新的）。
        """
        self._active_agent = agent
        self._active_registry = registry

    # ── ClientSession callback 工厂（roots / message_handler）──────────
    def _make_roots_callback(self):
        """构造 list_roots_callback（传给 ClientSession → 声明 roots capability）。

        server 发 roots/list 时返回 self._roots（配置或缺省 cwd）。callback 是 async，
        跑在常驻 loop（ClientSession 所在 loop）。
        """
        roots = list(self._roots)

        async def _roots_cb(context):
            from mcp import types
            mcp_roots = [
                types.Root(uri=r["uri"], name=r.get("name") or None)
                for r in roots
            ]
            return types.ListRootsResult(roots=mcp_roots)

        return _roots_cb

    def _make_message_handler(self, name: str):
        """构造 message_handler（收 ServerNotification：list_changed / resource_updated 等）。

        关键约束: message_handler 跑在 ClientSession._receive_loop（同一个 task）。
        如果 handler 内部 await list_tools RPC（send_request），response 必须经 receive_loop
        自己处理 → 自死锁（receive_loop 永远卡在 await response，response 永远进不来）。
        修法: handler 立即返回，把 list_changed 处理丢给独立 asyncio task 跑。
        """
        async def _handler(message):
            try:
                from mcp import types
                if isinstance(message, types.ServerNotification):
                    # 重要：spawn 后立刻返回（不能 await _handle_notification 内的 list_tools）
                    asyncio.create_task(self._handle_notification(name, message.root))
                elif isinstance(message, Exception):
                    logger.debug("🔌 mcp server '%s' msg exception: %s", name, message)
            except Exception as e:
                logger.warning("🔌 mcp server '%s' message_handler error: %s", name, e)

        return _handler

    async def _handle_notification(self, name: str, notif) -> None:
        """分发 list_changed / resource_updated 通知。

        跑在常驻 loop（message_handler 所在 loop）。
        list_changed 在 _servers 未就绪（连接阶段 race）时记 pending，由 _keep_alive 补刷。
        """
        from mcp import types
        if isinstance(notif, types.ToolListChangedNotification):
            logger.info("🔌 mcp server '%s' tools/list_changed → 刷新", name)
            await self._refresh(name, "tools")
        elif isinstance(notif, types.ResourceListChangedNotification):
            logger.info("🔌 mcp server '%s' resources/list_changed → 刷新", name)
            await self._refresh(name, "resources")
        elif isinstance(notif, types.PromptListChangedNotification):
            logger.info("🔌 mcp server '%s' prompts/list_changed → 刷新", name)
            await self._refresh(name, "prompts")
        elif isinstance(notif, types.ResourceUpdatedNotification):
            logger.debug(
                "🔌 mcp server '%s' resource updated: %s（MVP 不做 subscribe 细粒度刷新）",
                name, getattr(getattr(notif, "params", None), "uri", "?"),
            )
        # 其它通知忽略

    async def _refresh(self, name: str, kind: str, *, notify: bool = True) -> None:
        """刷新 server 的 tools/resources/prompts。

        连接阶段 race 修复：若 _servers[name] 未就绪（_keep_alive 还在连接），记 pending，
        由 _keep_alive 在设置 _servers 后补刷（notify=False，因组合根随后用 _servers 注册最新）。

        Args:
            kind: "tools" / "resources" / "prompts"
            notify: True（运行期）→ tools 刷新后调 on_tools_changed 同步 ToolRegistry；
                    False（连接阶段补刷）→ 跳过（组合根注册时自然取最新 _servers）
        """
        live = self._servers.get(name)
        if live is None:
            # 多种 kind（tools/resources/prompts）可同时 pending，不互相覆盖
            self._pending_refresh.setdefault(name, set()).add(kind)
            logger.debug("🔌 mcp server '%s' %s/list_changed 到达时未就绪，记 pending", name, kind)
            return
        await self._do_refresh(name, kind, live, notify)

    async def _do_refresh(self, name: str, kind: str, live, notify: bool) -> None:
        """实际刷新（live 已就绪）。tools 刷新后按 notify 决定是否调 on_tools_changed。

        on_tools_changed（组合根注册）：unregister_by_prefix + register 新 tool_defs +
        失效 run_state.tool_schemas（跨线程：本方法跑在常驻 loop，回调操作主线程对象，
        CPython GIL 下 dict 操作原子，小并发窗口可接受）。
        """
        try:
            if kind == "tools":
                result = await live.session.list_tools()
                live.tool_defs = materialize_tools(name, result.tools or [], self)
                if notify:
                    # C.2 M-C1:统一走 _safe_on_tools_changed(原 inline 块重复,
                    # 异常吞掉逻辑跟现有 helper 完全一致)
                    self._safe_on_tools_changed(name)
            elif kind == "resources":
                live.resources = (await live.session.list_resources()).resources or []
            elif kind == "prompts":
                live.prompts = (await live.session.list_prompts()).prompts or []
        except Exception as e:
            logger.warning("🔌 mcp server '%s' 刷新 %s 失败: %s", name, kind, e)

    # ── 工具调用（同步门面，供 materialize handler 调）──────────────
    def call_tool(self, server: str, tool: str, arguments: dict, *, timeout: Optional[float] = None) -> str:
        """同步门面: 查 server → 提交 call_tool 到常驻 loop → 归一化结果为 str。"""
        timeout = timeout if timeout is not None else self._call_timeout
        live = self._servers.get(server)
        if live is None:
            raise RuntimeError(f"mcp server '{server}' 未连接")
        coro = live.session.call_tool(
            tool, arguments,
            read_timeout_seconds=timedelta(seconds=timeout),
        )
        result = self._run(coro, timeout=timeout + 5)
        return _normalize_call_result(result)

    def registered_tools(self) -> dict:
        """返回 {server_name: [ToolDef]}（已 materialize，供组合根注册到 ToolRegistry）。

        ToolDef 的 handler 闭包绑定 self.call_tool，所以 agent 重建时新 registry
        可安全重新注册同一批 ToolDef（manager 连接 session 级复用，不重连）。
        """
        return {name: live.tool_defs for name, live in self._servers.items()}

    # ── resources（同步门面，供 materialize 全局工具调）─────────────
    def list_resources(self, server: Optional[str] = None) -> str:
        """聚合所有/单 server 的 resources，返回 LLM 可读文本（每行 '[server] uri — desc'）。"""
        lines = []
        for name, live in self._servers.items():
            if server and name != server:
                continue
            for r in (live.resources or []):
                uri = getattr(r, "uri", "?")
                desc = getattr(r, "description", None) or getattr(r, "name", "") or ""
                lines.append(f"[{name}] {uri} — {desc}".rstrip(" —"))
        return "\n".join(lines) if lines else "(no resources)"

    def read_resource(self, server: str, uri: str) -> str:
        """同步门面: read_resource(uri) → 归一 str。"""
        live = self._servers.get(server)
        if live is None:
            raise RuntimeError(f"mcp server '{server}' 未连接")
        result = self._run(live.session.read_resource(uri), timeout=self._call_timeout + 5)
        return _normalize_resource_result(result)

    def registered_resources(self) -> dict:
        """返回 {server_name: [Resource]}（供组合根 / 段注入 handler 用）。"""
        return {name: live.resources for name, live in self._servers.items()}

    # ── prompts（同步门面，供 materialize get_mcp_prompt 工具调）─────
    def registered_prompts(self) -> dict:
        """返回 {server_name: [Prompt]}（供 McpPromptsHandler 段注入）。"""
        return {name: live.prompts for name, live in self._servers.items()}

    def get_prompt(self, server: str, name: str, arguments: Optional[dict] = None) -> str:
        """同步门面: get_prompt(name, arguments) → 归一 str。"""
        live = self._servers.get(server)
        if live is None:
            raise RuntimeError(f"mcp server '{server}' 未连接")
        result = self._run(
            live.session.get_prompt(name, arguments or {}),
            timeout=self._call_timeout + 5,
        )
        return _normalize_prompt_result(result)

    # ── 生命周期 ────────────────────────────────────────────────────
    def dispose(self):
        """幂等关闭: 停 health 调度器 → signal dispose_events → 等 keep_alive 退出 → 停 loop。"""
        if self._closed:
            return
        self._closed = True
        if self._loop is None:
            return

        # 0. 停 health-check 调度器（set sleep futs 打断 + cancel task）
        # C.5 delegate 给 McpHealthScheduler.stop
        self._scheduler.stop()

        # 1. signal 所有 dispose_events（让 keep_alive 退 with，关连接）
        for evt in list(self._dispose_events.values()):
            try:
                self._loop.call_soon_threadsafe(evt.set)
            except RuntimeError:
                pass   # loop 已停

        # 2. 等 keep_alive tasks 退出（直接 run_coroutine_threadsafe，绕过 _closed 检查）
        for name, task in list(self._keep_alive_tasks.items()):
            if task.done():
                continue
            try:
                fut = asyncio.run_coroutine_threadsafe(
                    asyncio.wait_for(task, timeout=10.0), self._loop)
                fut.result(timeout=12.0)
            except Exception as e:
                logger.debug("🔌 keep_alive '%s' 退出超时/异常: %s", name, e)

        # 3. 停 loop + join thread
        try:
            self._loop.call_soon_threadsafe(self._loop.stop)
        except RuntimeError:
            pass
        if self._loop_thread is not None:
            self._loop_thread.join(timeout=5.0)

        self._servers.clear()
        self._dispose_events.clear()
        self._keep_alive_tasks.clear()
        logger.debug("🔌 McpManager disposed")
