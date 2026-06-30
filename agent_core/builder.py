"""
AgentBuilder — 统一 agent 组装入口

v2 重构引入(详见 docs/agent-state-machine-and-chain-of-responsibility-design.md §11):

解决 v1 问题:
- 扩展点 API 不一致(extra_phase_transitions vs extra_turn_handlers)
- 顺序问题(extra 永远在最后,无法指定插入位置)
- DI 散落(各 handler 各自注入依赖)

核心 API:
- with_phase_override:完全替换某个 phase
- with_handler:在指定 handler 之后/之前插入
- with_termination:替换 termination 条件
- with_plugin_handler:注册受限的 plugin handler(详见 §12)
- use_real_session_persist:切换 SessionPersistHandler 模式(NORMAL 完整接管 / DELEGATE 旧 delegation)
- build_default_*_chain:4 个默认 chain factory(从 agent_core.py 抽离,§4.3 / §15)
- build:组装并返回 agent(agent 仍由 ReactAgent 构造)
"""

from __future__ import annotations

from enum import Enum
from typing import TYPE_CHECKING, Any, Optional

from agent_core.agent_state import AgentPhase, Phase, TerminationCondition
from agent_core.turn_chain import (
    AuditLogHandler,
    ChunkParseHandler,
    Handler,
    HandlerResult,
    LLMCallHandler,
    MemoryBridgeExtractHandler,
    MemoryRetrievalHandler,
    PermissionCheckHandler,
    PluginHandler,
    SessionPersistHandler,
    SystemPromptHandler,
    ToolDispatchHandler,
    ToolExecuteHandler,
    ToolsSchemaPrepareHandler,
    TurnChain,
)


if TYPE_CHECKING:
    from agent_core.agent_core import ReactAgent


__all__ = [
    "AgentBuilder",
    "SessionPersistMode",
    "build_default_inputs_chain",
    "build_default_llm_chain",
    "build_default_tool_chain",
    "build_default_output_chain",
]


# ────────────────────────────────────────────────────────────────────
# SessionPersistHandler 模式(2026-06-30 D6-4 引入)
# ────────────────────────────────────────────────────────────────────


class SessionPersistMode(str, Enum):
    """SessionPersistHandler 行为模式(D6-4 引入)。

    NORMAL:
        - SessionPersistHandler 真实现 + 完整接管 session 写入
          (assistant_with_tools / assistant_message / tool_results)
        - 这是生产默认,跟 docs/agent-state-machine-and-chain-of-responsibility-design.md §10 一致

    DELEGATE:
        - SessionPersistHandler 走 no-op delegation,返回 HandlerResult() early
        - session 写入回到 v1 streaming 路径
        - 用于:过渡期兼容 / 3rd party 扩展 / debug 对比
    """
    NORMAL = "normal"
    DELEGATE = "delegate"


# ────────────────────────────────────────────────────────────────────
# 4 个默认 Chain Factory(D6-1 抽离自 agent_core.py:336-351)
# ────────────────────────────────────────────────────────────────────
# 这些函数把"4 类 chain 的默认构造"从 ReactAgent.__init__ 中抽到 builder.py,
# 让 ReactAgent.__init__ 变薄壳,也允许测试 / 第三方代码复用同一套默认 chain。


def build_default_inputs_chain(agent) -> TurnChain:
    """SETUP phase 默认 chain(MemoryRetrieval → SystemPrompt → ToolsSchemaPrepare)。

    对应 docs §4.3 table inputs_chain 行 + §15 step 2。
    """
    return TurnChain([
        MemoryRetrievalHandler(agent),
        SystemPromptHandler(agent),
        ToolsSchemaPrepareHandler(agent),
    ])


def build_default_llm_chain(agent) -> TurnChain:
    """LLM_THINKING phase 默认 chain(LLMCall → ChunkParse)。

    对应 docs §4.3 table llm_chain 行 + §15 step 2。
    """
    return TurnChain([
        LLMCallHandler(agent),
        ChunkParseHandler(agent),
    ])


def build_default_tool_chain(agent) -> TurnChain:
    """EXECUTING_TOOLS phase 默认 chain(PermissionCheck → ToolDispatch → ToolExecute)。

    对应 docs §4.3 table tool_chain 行 + §15 step 2。
    """
    return TurnChain([
        PermissionCheckHandler(agent),
        ToolDispatchHandler(agent),
        ToolExecuteHandler(agent),
    ])


def build_default_output_chain(
    agent,
    *,
    session_persist_mode: SessionPersistMode = SessionPersistMode.NORMAL,
) -> TurnChain:
    """FINALIZING phase 默认 chain(SessionPersist → AuditLog → MemoryBridgeExtract)。

    对应 docs §4.3 table output_chain 行 + §15 step 2。

    Args:
        agent: ReactAgent 实例(handler 构造需要)
        session_persist_mode:
            NORMAL(默认):SessionPersistHandler 真实现 + 完整接管 session 写入
            DELEGATE:SessionPersistHandler 走 no-op,v1 streaming 路径负责
    """
    persist_handler = SessionPersistHandler(agent)
    if session_persist_mode == SessionPersistMode.DELEGATE:
        # DELEGATE 模式:把 handler 的 handle() 替换成 no-op
        _make_session_persist_delegate(persist_handler)
    return TurnChain([
        persist_handler,
        AuditLogHandler(agent),
        MemoryBridgeExtractHandler(agent),
    ])


def _make_session_persist_delegate(handler: SessionPersistHandler) -> None:
    """把 SessionPersistHandler 实例切换成 DELEGATE 模式(handle() 变 no-op)。

    实现细节:monkey-patch 实例方法。
    为什么这样:不引入新类,纯 runtime 切换,build 调用结束后不污染 import。
    设计文档 §11.1 "with_handler / with_phase_override" 都是 runtime 切换,
    跟本设计一致。
    """
    def _noop_handle(ctx):  # noqa: ANN001 - ctx 不强类型,跟原 handle 一致
        return HandlerResult()
    handler.handle = _noop_handle  # type: ignore[method-assign]  # monkey-patch instance method


def _append_plugin_to_all_chains(
    agent, plugin_handler: PluginHandler, *, after: Optional[str]
) -> None:
    """plugin handler append 到所有 named hook point 命中的 chain(per §12.2 白名单)。

    简化实现(D8 修复):用 _should_apply_to_chain 过滤 — plugin handler 只 apply
    到含 named hook 的 chain,不在 LLMCall 等特定 handler 之前的限定通过
    PluginHandler.emit_validated 的 event 白名单强制,这里只保证位置:
    不在 LLMCall handler 之前插。

    注:完整实现需要按 phase 路由,本简化版 append 到所有匹配的 chain 便于
    3rd party 收集遥测/UI hint,符合 §12.2 "只能 append 到 chain 末端" 的最小
    约束。
    """
    opts = {"after": after, "before": None, "at": None}
    for chain in (
        agent._inputs_chain,
        agent._llm_chain,
        agent._tool_chain,
        agent._output_chain,
    ):
        if chain is None:
            continue
        if _should_apply_to_chain(chain, opts):
            chain.add(plugin_handler, **opts)


# ────────────────────────────────────────────────────────────────────
# AgentBuilder
# ────────────────────────────────────────────────────────────────────


class AgentBuilder:
    """统一 agent 组装入口。

    用法:
        agent = (AgentBuilder()
            .with_handler(CostTrackingHandler(pricing), after="llm_call")
            .with_termination(CompositeTermination(
                MaxTurnsTermination(10),
                TimeoutTermination(300.0),
            ))
            .build(agent_kwargs={...}))
    """

    def __init__(self):
        self._phase_overrides: dict[AgentPhase, Phase] = {}
        self._handler_inserts: list[tuple[Handler, dict]] = []
        self._plugin_handler_inserts: list[tuple[PluginHandler, dict]] = []
        self._termination_override: Optional[TerminationCondition] = None
        # 默认 SessionPersistHandler 模式(NORMAL=真实现)
        self._real_session_persist: bool = True

    def with_phase_override(self, phase: AgentPhase, override: Phase) -> "AgentBuilder":
        """完全替换某个 phase(高级用法:换 phase 自己的 chain 或 enter/next 行为)。

        Args:
            phase: 要替换的 phase key(如 AgentPhase.SETUP)
            override: 完整的 Phase 实例(带 chain)
                - override 的 chain 由用户自己构造,builder 不替换
                - 因此 with_handler() 后续不会影响 override 的 chain — 用户要手动改
                - 想用 builder 的 chain,直接用 AgentBuilder 默认 phase 即可,不需要 override

        Raises:
            TypeError: 如果 override 不是 Phase 子类,或 _chain 属性缺失
        """
        if not isinstance(override, Phase):
            raise TypeError(
                f"phase override 必须是 Phase 实例,got {type(override).__name__}"
            )
        if not hasattr(override, "_chain"):
            # Phase.__init__ 保证 _chain 存在;到这里说明用户传了非 Phase 子类
            raise TypeError(
                f"phase override {type(override).__name__} 缺少 _chain 属性"
            )
        self._phase_overrides[phase] = override
        return self

    def with_handler(
        self,
        handler: Handler,
        *,
        after: Optional[str] = None,
        before: Optional[str] = None,
        at: Optional[int] = None,
    ) -> "AgentBuilder":
        """named hook point:在指定 handler 之后/之前插入,或指定 index。

        Args:
            handler: 要插入的 handler
            after: 在名为 after 的 handler 之后插入
            before: 在名为 before 的 handler 之前插入
            at: 在指定 index 插入(优先级最低)

        Raises:
            TypeError: 如果 handler 是 PluginHandler 子类 — plugin handler 必须用
                with_plugin_handler() 注册(§12 白名单约束:只能 append 到 chain 末端,
                before/at 都不允许)
        """
        # §12.2 白名单 runtime check:plugin handler 不能用 with_handler(bypass 风险)
        if isinstance(handler, PluginHandler):
            raise TypeError(
                f"plugin handler {type(handler).__name__} 必须用 with_plugin_handler() "
                f"注册(§12 白名单 — plugin handler 只能 append 到 chain 末端,"
                f"before=/at= 都不允许)"
            )
        self._handler_inserts.append(
            (handler, {"after": after, "before": before, "at": at})
        )
        return self

    def with_plugin_handler(
        self,
        handler: PluginHandler,
        *,
        after: Optional[str] = None,
    ) -> "AgentBuilder":
        """注册 plugin handler(受限,详见设计文档 §12)。

        PluginHandler 子类仅可 emit 白名单内的 event type,
        只能 append 到 chain 末端(不能在 LLMCall 之前插)。
        这里统一写到 _plugin_handler_inserts 而非 _handler_inserts,
        让 build() 区分做白名单 runtime check。
        """
        if not isinstance(handler, PluginHandler):
            raise TypeError(
                f"plugin handler 必须继承 PluginHandler,got {type(handler).__name__}"
            )
        self._plugin_handler_inserts.append(
            (handler, {"after": after})
        )
        return self

    def with_termination(self, termination: TerminationCondition) -> "AgentBuilder":
        self._termination_override = termination
        return self

    def use_real_session_persist(self, enabled: bool = True) -> "AgentBuilder":
        """切换 SessionPersistHandler 模式(D6-4 引入)。

        enabled=True(默认生产):SessionPersistHandler 真实现 + 完整接管 session 写入
        enabled=False(DELEGATE):SessionPersistHandler 走 no-op,
                                 session 写入交给 v1 streaming 路径(_iter_phase_tools /
                                 _iter_phase_llm / _iter_phase_finalize)

        DELEGATE 用法:
            - 过渡期兼容(老测试 / 老外部集成)
            - 调试对比(Switch 前后 JSONL 行为差异验证)
            - 3rd party 扩展(用户想自己接管 session 写入)

        注意:D6-3 让 SessionPersistHandler 接管了所有 session 写入。DELEGATE 模式下,
        v1 streaming 路径的 add_* 调用必须同时存在才能正常写盘。本 builder 通过
        build_default_output_chain(session_persist_mode=...) 同步切换两个写入路径。
        """
        self._real_session_persist = enabled
        return self

    def build(self, agent_kwargs: dict) -> "ReactAgent":
        """构造 agent + 应用所有 builder 改动。

        默认 chain 通过 build_default_*_chain() 工厂生成。

        agent_kwargs 只能包含 ReactAgent.__init__ 真实接受的参数(llm_router /
        tool_registry / max_turns / memory_* / 等)。不允许传 _inputs_chain 等
        chain 属性 — ReactAgent.__init__ 不接受,会 TypeError。

        想用 custom chain 的正确方式:
            - builder.with_phase_override(phase, custom_phase) — 自定义 phase 自己 hold chain
            - builder.with_handler(handler, after=X) — 在 factory 默认 chain 里插入
            - 直接 agent._inputs_chain = custom_chain(在 build() 返回后)

        为向后兼容(老调用方通过 ReactAgent(...) 直接构造时):
        - agent_kwargs 不传 chain → ReactAgent.__init__ 内部已用 factory 默认
        - agent_kwargs 传非法 chain kwarg → TypeError(由 ReactAgent 抛)
        """
        from agent_core.agent_core import ReactAgent

        # Step 1: 构造 agent(走 ReactAgent.__init__)
        agent = ReactAgent(**agent_kwargs)

        # Step 2: 决定 chain — 调用方传了就尊重,否则用 factory 默认
        if not agent_kwargs.get("_inputs_chain"):
            agent._inputs_chain = build_default_inputs_chain(agent)
        if not agent_kwargs.get("_llm_chain"):
            agent._llm_chain = build_default_llm_chain(agent)
        if not agent_kwargs.get("_tool_chain"):
            agent._tool_chain = build_default_tool_chain(agent)
        if not agent_kwargs.get("_output_chain"):
            mode = (
                SessionPersistMode.NORMAL
                if self._real_session_persist
                else SessionPersistMode.DELEGATE
            )
            agent._output_chain = build_default_output_chain(
                agent, session_persist_mode=mode,
            )

        # Step 3: 重建 SM phases(因为 chain 实例换了 — 原本 phase 持有的是 __init__ 时的 chain)
        _rebuild_phases_from_chains(agent)

        # Step 4: 应用 plugin handler inserts(白名单 runtime check:只能在 chain 末端 append)
        for handler, opts in self._plugin_handler_inserts:
            if opts.get("after") is None:
                raise ValueError(
                    f"plugin handler 必须在 named handler 之后 append(after=...)"
                )
            _append_plugin_to_all_chains(agent, handler, after=opts.get("after"))

        # Step 5: 应用 trusted handler inserts(可任意位置:after/before/at)
        # 关键修复(D8):每个 with_handler 只对它命中的 chain 生效 —
        # named hook (after="llm_call") 在没该 handler 的 chain 里 KeyError,跳过。
        # 无 named hook (裸 with_handler) 默认 append 到所有 4 chain 末端。
        for chain in (
            agent._inputs_chain,
            agent._llm_chain,
            agent._tool_chain,
            agent._output_chain,
        ):
            if chain is None:
                continue
            for handler, opts in self._handler_inserts:
                if _should_apply_to_chain(chain, opts):
                    if any(opt is not None for opt in opts.values()):
                        chain.add(handler, **opts)
                    else:
                        chain.add(handler)  # 默认 append 到末端

        # Step 6: 应用 phase overrides(完全替换某个 phase)
        for phase, override in self._phase_overrides.items():
            agent._sm._phases[phase] = override

        # Step 7: 应用 termination override
        if self._termination_override is not None:
            agent._sm._termination = self._termination_override

        return agent


def _should_apply_to_chain(chain: TurnChain, opts: dict) -> bool:
    """判断 with_handler 的 named hook point 在这条 chain 上是否合法。

    规则(D8 修复):
        - 无 named hook (after/before/at 都是 None) → apply 到所有 chain
        - 有 named hook (after=X) → apply 只到 chain 里有名为 X 的 handler 的那条
        - 有 named hook (before=X) → 同上
        - 有 named hook (at=N) → apply 到所有 chain(N 是 index,跨 chain 通用)

    为什么:d8 E2E 测试发现 `with_handler(after="llm_call")` 之前会 KeyError
    失败 — builder naively apply 到 4 个 chain,但 llm_call 只在 llm_chain 里。
    """
    after = opts.get("after")
    before = opts.get("before")
    at = opts.get("at")

    # 无 named hook → apply 到所有 chain(append 到末端)
    if after is None and before is None and at is None:
        return True

    # at=N 是 index 模式,跨 chain 通用
    if at is not None:
        return True

    # after=X / before=X → 看 chain 里有没有名为 X 的 handler
    target = after if after is not None else before
    chain_handler_names = {h.name for h in chain}
    return target in chain_handler_names


def _rebuild_phases_from_chains(agent: "ReactAgent") -> None:
    """从新的 chain 重建 _phases dict(因为 phase 实例持有原 chain 引用)。

    用法:builder.build() 在调用方替换 _inputs_chain/_llm_chain/_tool_chain/_output_chain 后,
    调用本函数同步更新 _sm._phases 持有新 chain 的 phase 实例。

    实现:同样的 Phase 类 + 新 chain 构造新 phase 实例,替换 dict[AgentPhase] -> Phase。
    """
    from agent_core.agent_state import (
        SetupPhase, LLMThinkingPhase, AwaitingPermissionPhase,
        ExecutingToolsPhase, FinalizingPhase, InterruptedPhase, DonePhase,
    )
    new_phases = {
        AgentPhase.SETUP:               SetupPhase(agent._inputs_chain),
        AgentPhase.LLM_THINKING:        LLMThinkingPhase(agent._llm_chain),
        AgentPhase.AWAITING_PERMISSION: AwaitingPermissionPhase(),
        AgentPhase.EXECUTING_TOOLS:     ExecutingToolsPhase(agent._tool_chain),
        AgentPhase.FINALIZING:          FinalizingPhase(agent._output_chain),
        AgentPhase.INTERRUPTED:         InterruptedPhase(),
        AgentPhase.DONE:                DonePhase(),
    }
    agent._sm._phases = new_phases