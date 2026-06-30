"""
Turn Chain — 职责链(Chain of Responsibility)

v2 重构引入(详见 docs/agent-state-machine-and-chain-of-responsibility-design.md §4):

核心组件:
1. HandlerResult:handler 执行结果(stop_chain + next_action)
2. Handler 协议:每个 handler 是 class,__init__ 注入 agent 引用,handle(ctx) -> HandlerResult
3. TurnChain:执行器,按顺序跑 handlers,遇 stop_chain 提前终止
4. TurnContext:per-turn 工作内存(handler 间共享)
5. 11 个内置 handler class(4 个 chain 分类)

Plan A 实施(2026-06-30):handler 接真业务,每个 handler.handle() 通过
agent 引用调对应的 _iter_xxx() helper。run() 也复用同一套 helper,
行为完全一致。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator, Optional, Protocol, runtime_checkable

from agent_core.agent_state import Event, RunState, TurnContext


__all__ = [
    "HandlerResult",
    "Handler",
    "TurnChain",
    "MemoryRetrievalHandler",
    "SystemPromptHandler",
    "ToolsSchemaPrepareHandler",
    "LLMCallHandler",
    "ChunkParseHandler",
    "PermissionCheckHandler",
    "ToolDispatchHandler",
    "ToolExecuteHandler",
    "SessionPersistHandler",
    "AuditLogHandler",
    "MemoryBridgeExtractHandler",
]


_logger = logging.getLogger("agent_core.turn_chain")


# ────────────────────────────────────────────────────────────
# HandlerResult — 单个 handler 的执行结果
# ────────────────────────────────────────────────────────────

@dataclass
class HandlerResult:
    """单个 handler 的执行结果。

    关键字段:
    - stop_chain:True → 后续 handler 不跑(短路)
    - next_action:给 state machine 看的信号(可选,目前未使用,保留扩展)
    """
    stop_chain: bool = False
    next_action: Optional[str] = None


# ────────────────────────────────────────────────────────────
# Handler 协议
# ────────────────────────────────────────────────────────────

@runtime_checkable
class Handler(Protocol):
    """职责链节点协议。

    实现要求:
    - name:str(用于 named hook point)
    - handle(ctx: TurnContext) -> HandlerResult
    """
    name: str

    def handle(self, ctx: TurnContext) -> HandlerResult: ...


class PluginHandler:
    """3rd party plugin handler 基类(详见设计文档 §12)。

    权限受限:
    - 只能读 ctx(turn 上下文),不直接碰 self.xxx
    - 只能 emit 白名单内的 event type
    - 只能 append 到 chain 末端(不能在 LLMCall 之前插)

    验证:AgentBuilder.with_plugin_handler() 入口 type check。
    """
    ALLOWED_EVENT_TYPES = {"system", "metric", "telemetry", "ui_hint"}

    name: str = "plugin_handler"

    def handle(self, ctx: TurnContext) -> HandlerResult:
        raise NotImplementedError("PluginHandler 子类必须实现 handle()")

    def emit_validated(self, event: Event, ctx: TurnContext) -> None:
        """emit event 时白名单 check。"""
        if event[0] not in self.ALLOWED_EVENT_TYPES:
            raise SecurityError(
                f"PluginHandler {self.name} 不允许 emit event type='{event[0]}',"
                f"允许的类型:{self.ALLOWED_EVENT_TYPES}"
            )
        ctx.emit(event)


class SecurityError(Exception):
    """PluginHandler emit 不在白名单 event type 时抛错。"""


# ────────────────────────────────────────────────────────────
# TurnChain — 执行器
# ────────────────────────────────────────────────────────────

class TurnChain:
    """职责链执行器:按顺序跑 handlers,遇 stop_chain 提前终止。"""

    def __init__(self, handlers: list[Handler]):
        self._handlers = list(handlers)
        self._name_to_idx = {h.name: i for i, h in enumerate(self._handlers)}

    def run(self, ctx: TurnContext) -> Iterator[Event]:
        """执行链:每个 handler 调一次,遇 stop_chain 停。

        注意:这是 generator,逐个 yield events(供 phase.enter 流式调用)。

        设计:handler 通过 ctx.emit() 累计事件,TurnChain.run() 每次 handler
        调用后清空 ctx.events 再 yield 出去。这样 handler 调多少次,事件都按
        handler 调用顺序流出。
        """
        for h in self._handlers:
            if ctx.is_stopped:
                break
            result = h.handle(ctx)
            for ev in ctx.events:
                yield ev
            ctx.events.clear()
            if result.stop_chain:
                break

    def add(
        self,
        handler: Handler,
        *,
        after: Optional[str] = None,
        before: Optional[str] = None,
        at: Optional[int] = None,
    ) -> None:
        """named hook point:在指定 handler 之后/之前插入,或指定 index。"""
        if at is not None:
            self._handlers.insert(at, handler)
        elif after is not None:
            idx = self._name_to_idx[after]
            self._handlers.insert(idx + 1, handler)
        elif before is not None:
            idx = self._name_to_idx[before]
            self._handlers.insert(idx, handler)
        else:
            self._handlers.append(handler)
        self._name_to_idx = {h.name: i for i, h in enumerate(self._handlers)}

    def remove(self, name: str) -> None:
        self._handlers = [h for h in self._handlers if h.name != name]
        self._name_to_idx = {h.name: i for i, h in enumerate(self._handlers)}

    def __iter__(self):
        return iter(self._handlers)

    def __len__(self) -> int:
        return len(self._handlers)

    def __repr__(self) -> str:
        names = [h.name for h in self._handlers]
        return f"<TurnChain {' → '.join(names)}>"


# ════════════════════════════════════════════════════════════
# 内置 Handler(11 个,4 个 chain 分类)
# ════════════════════════════════════════════════════════════
# Plan A: 每个 handler 接 agent 引用,handle() 调 agent._iter_phase_xxx() 真业务。
# Stop-chain 用法:每个 chain 的「主 handler」处理完后 stop_chain=True,后续 handler 跳过。
# 这样 chain 中多个 handler 都能保留作为扩展点,但默认只有一个干活。

_StopChain = HandlerResult(stop_chain=True)


# ── 1. MemoryRetrievalHandler(inputs_chain) ───────────────
class MemoryRetrievalHandler:
    """inputs_chain 首位:检索相关记忆注入 messages,emit memory_status event。

    Plan A: 调 agent._iter_phase_setup() 处理整个 SETUP phase 的真业务。
    """
    name = "memory_retrieval"

    def __init__(self, agent):
        self._agent = agent

    def handle(self, ctx: TurnContext) -> HandlerResult:
        agent = self._agent
        if agent is None:
            return HandlerResult()
        for ev in agent._iter_phase_setup(ctx):
            ctx.emit(ev)
        return _StopChain  # inputs_chain 由本 handler 一手包办


# ── 2. SystemPromptHandler(inputs_chain) ──────────────────
class SystemPromptHandler:
    """inputs_chain 中段:确保 messages 头部有 system prompt。

    Plan A: 与 MemoryRetrievalHandler 协同 — SETUP phase 走完会通过
    MemoryRetrievalHandler 的 stop_chain 短路,所以本 handler 默认不会被触发。
    若用户禁用 memory_retriever,本 handler 仍可作为兜底注入 system prompt。
    """
    name = "system_prompt"

    def __init__(self, agent):
        self._agent = agent

    def handle(self, ctx: TurnContext) -> HandlerResult:
        agent = self._agent
        if agent is None or not agent.system_prompt:
            return HandlerResult()
        # 兜底:仅当 stage_inputs 没被 setup 阶段写入时,本 handler 注入 system prompt
        if getattr(ctx, "stage_inputs", None) is not None:
            return HandlerResult()
        messages = list(agent.messages)
        has_system = any(m.get("role") == "system" for m in messages)
        if not has_system:
            messages.insert(0, {"role": "system", "content": agent.system_prompt})
        ctx.stage_inputs = messages
        return HandlerResult()


# ── 3. ToolsSchemaPrepareHandler(inputs_chain) ────────────
class ToolsSchemaPrepareHandler:
    """inputs_chain 末位:准备 tool schemas(按 provider 格式)。

    Plan A: 与 MemoryRetrievalHandler 协同 — 默认被 stop_chain 短路。
    本 handler 仅作扩展点,可被独立激活(用户传 disable_memory=True 时)。
    """
    name = "tools_schema_prepare"

    def __init__(self, agent):
        self._agent = agent

    def handle(self, ctx: TurnContext) -> HandlerResult:
        agent = self._agent
        if agent is None:
            return HandlerResult()
        # 把 tool_schemas 缓存到 turn_ctx(LLMCallHandler 复用)
        try:
            ctx.tool_schemas = agent.tools.list_schemas(provider=agent._detect_provider())
        except Exception as e:
            _logger.warning(f"ToolsSchemaPrepare failed: {e}")
        return HandlerResult()


# ── 4. LLMCallHandler(llm_chain) ──────────────────────────
class LLMCallHandler:
    """llm_chain 首位:调 LLM + 收 chunks,emit text/thinking/tool_call/usage events。

    Plan A: 调 agent._iter_phase_llm() 处理整个 LLM_THINKING phase 真业务。
    写入 ctx.stage_outputs = _LLMResult(tool_calls/full_text/...)。
    """
    name = "llm_call"

    def __init__(self, agent):
        self._agent = agent

    def handle(self, ctx: TurnContext) -> HandlerResult:
        agent = self._agent
        if agent is None:
            return HandlerResult()
        for ev in agent._iter_phase_llm(ctx):
            ctx.emit(ev)
        return _StopChain  # llm_chain 由本 handler 一手包办


# ── 5. ChunkParseHandler(llm_chain) ───────────────────────
class ChunkParseHandler:
    """llm_chain 末位:流式解析 LLM chunks,emit text/thinking/tool_call events。

    Plan A: 默认被 LLMCallHandler 的 stop_chain 短路。仅作扩展点。
    """
    name = "chunk_parse"

    def __init__(self, agent):
        self._agent = agent

    def handle(self, ctx: TurnContext) -> HandlerResult:
        # LLMCallHandler 已 emit events + 写 stage_outputs,本 handler 默认 no-op
        return HandlerResult()


# ── 6. PermissionCheckHandler(tool_chain,首位) ───────────
class PermissionCheckHandler:
    """tool_chain 首位:permission 决策。

    Plan A: 默认被 ToolExecuteHandler 的 stop_chain 短路。
    仅作扩展点 — 若用户想在 tool 执行前单独跑 permission 检查,
    可禁用 ToolExecuteHandler 的 stop_chain,让本 handler 先跑。
    """
    name = "permission_check"

    def __init__(self, agent):
        self._agent = agent

    def handle(self, ctx: TurnContext) -> HandlerResult:
        # 默认 no-op(ToolExecuteHandler 已处理 permission)
        return HandlerResult()


# ── 7. ToolDispatchHandler(tool_chain) ────────────────────
class ToolDispatchHandler:
    """tool_chain 中段:emit tool_call event(单/并行)。

    Plan A: 默认被 ToolExecuteHandler 的 stop_chain 短路。
    """
    name = "tool_dispatch"

    def __init__(self, agent):
        self._agent = agent

    def handle(self, ctx: TurnContext) -> HandlerResult:
        # 默认 no-op(ToolExecuteHandler 已 emit tool_call event)
        return HandlerResult()


# ── 8. ToolExecuteHandler(tool_chain) ─────────────────────
class ToolExecuteHandler:
    """tool_chain 末位:单工具串行 / 多工具并行执行,emit tool_call/tool_result events。

    Plan A: 调 agent._iter_phase_tools() 处理整个 EXECUTING_TOOLS phase 真业务。
    关键:permission 标记检测 → turn_ctx.permission_request 写入 →
    state machine 转 AWAITING_PERMISSION phase(自动)。
    """
    name = "tool_execute"

    def __init__(self, agent):
        self._agent = agent

    def handle(self, ctx: TurnContext) -> HandlerResult:
        agent = self._agent
        if agent is None:
            return HandlerResult()
        for ev in agent._iter_phase_tools(ctx):
            ctx.emit(ev)
        return _StopChain  # tool_chain 由本 handler 一手包办


# ── 9. SessionPersistHandler(output_chain) ────────────────
class SessionPersistHandler:
    """output_chain 首位:tool_results 持久化(无条件下刷 _pending_tool_results)。

    === 职责范围(2026-06-30 D1 真实现) ===
        唯一职责:刷 agent._pending_tool_results → session_manager.add_tool_results
        这一段从 _iter_phase_finalize:1582-1601 搬过来,改成"无条件刷"
        (原逻辑要求 stage_out.tool_calls truthy 才写 → Fix C 之后 resume 路径下
        LLM 直接给最终回答 → stage_out.tool_calls=[] → 整段被跳过
        → orphan tool_use 漏洞,c85e9b4e.jsonl 全部 3 个 session 受影响)。
        刷完后清空 _pending_tool_results,避免跨 turn 累加。

    === 职责范围外(故意 NOT moved,D6-3 取舍 A 决定) ===
        以下 7 处 assistant session 写入刻意保留在 v1 streaming 路径,不搬入本 handler:

        [_iter_phase_tools 路径 — L1326, L1444 add_assistant_with_tools]
            这两处只在 AWAITING_PERMISSION 分支调,permission allowed/denied 分支
            不调(已知 v1 JSONL 写盘不一致;统一修复超出 D6 scope)。搬到本 handler
            会打破 "permission 实时 yield awaiting_permission" 的同步 timing,且要么
            接受 JSONL 行为变化、要么引入 awaiting 信号传递(高耦合),所以维持原状。

        [_iter_phase_llm 路径 — L1039, L1098, L2131, L2215, L2263 add_assistant_message]
            这 5 处分别在 LLM error / retry / final answer / fallback / 等位置调,
            散布在 _iter_phase_llm 复杂 streaming 逻辑里。搬入本 handler(FINALIZING 触发)
            会:
            - 引入 assistant_buffer 跨 turn 状态管理
            - 把 error/retry 路径的即时写盘延后到 FINALIZING,crash-on-tool-execute 场景
              会丢失 assistant message
            - 复杂度高,1 个 turn 干不完(estimated 200+ 行 surgery + 测试)
            因此维持 v1 streaming 路径,D6-3 标 deferred 到后续 M11。

    === 跟 docs/agent-state-machine-and-chain-of-responsibility-design.md §10 的偏差 ===
        §10 期望 SessionPersistHandler "一手包办所有 session 写入"。本次实施(D6-3)
        选择 partial migration:只接管 tool_results,assistant writes 留 v1 streaming。
        偏差在 AgentBuilder 公开 API 同步暴露 `use_real_session_persist()` toggle,
        让 DELEGATE 模式可以走完全 v1 路径作为对照。

    === Plan A 路由 ===
        MemoryBridgeExtractHandler 仍 stop_chain 短路本 handler 之后的潜在扩展点。
        本 handler 不 stop_chain → 后续 AuditLogHandler / MemoryBridgeExtractHandler 还能跑。
    """
    name = "session_persist"

    def __init__(self, agent):
        self._agent = agent

    def handle(self, ctx: TurnContext) -> HandlerResult:
        agent = self._agent
        if agent is None or agent._session_manager is None:
            return HandlerResult()
        pending = getattr(agent, "_pending_tool_results", None)
        if not pending:
            return HandlerResult()
        try:
            results = [
                {"tool_use_id": tid, "content": output}
                for tid, output in pending
            ]
            agent._session_manager.add_tool_results(results)
        except Exception as e:
            _logger.warning(f"SessionPersistHandler: add_tool_results failed: {e}")
        finally:
            agent._pending_tool_results = []
        return HandlerResult()


# ── 10. AuditLogHandler(output_chain) ─────────────────────
class AuditLogHandler:
    """output_chain 中段:写 audit log(tool 决策 + 工具结果)。

    Plan A: 默认 no-op(permission_engine 内部已统一写 audit)。
    仅作扩展点 — 用户可在此 hook 自己的 audit 逻辑。
    """
    name = "audit_log"

    def __init__(self, agent):
        self._agent = agent

    def handle(self, ctx: TurnContext) -> HandlerResult:
        # 默认 no-op(permission_engine 内部 audit 已统一写)
        return HandlerResult()


# ── 11. MemoryBridgeExtractHandler(output_chain) ─────────
class MemoryBridgeExtractHandler:
    """output_chain 末位:run 末尾触发 memory bridge.on_turn_end。

    Plan A: 调 agent._iter_phase_finalize() 处理整个 FINALIZING phase 真业务。
    包含 session 持久化 + memory bridge extract。
    """
    name = "memory_bridge_extract"

    def __init__(self, agent):
        self._agent = agent

    def handle(self, ctx: TurnContext) -> HandlerResult:
        agent = self._agent
        if agent is None:
            return HandlerResult()
        for ev in agent._iter_phase_finalize(ctx):
            ctx.emit(ev)
        return _StopChain  # output_chain 由本 handler 一手包办