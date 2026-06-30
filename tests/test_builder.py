"""
AgentBuilder API 单元测试(2026-06-30 — D6-5)。

覆盖(7 case):
    D6-5.1: factory 函数返回的 TurnChain 包含预期 handler 顺序
    D6-5.2: with_handler(after=...) / before=... / at=... 顺序正确
    D6-5.3: with_plugin_handler 拒绝非 PluginHandler 子类
    D6-5.4: use_real_session_persist 切换影响 build_default_output_chain 模式
    D6-5.5: with_termination / with_phase_override 状态被记录
    D6-5.6: SessionPersistHandler 在 DELEGATE 模式下 handle() 返回 HandlerResult() early
    D6-5.7: factory 函数对 stub agent 友好(handler __init__ 不访问 agent 属性)

设计参考:docs/agent-state-machine-and-chain-of-responsibility-design.md §11(AgentBuilder)
+ §15(Chain of Responsibility)
"""

from __future__ import annotations

from typing import List
from unittest.mock import MagicMock

import pytest

from agent_core.builder import (
    AgentBuilder,
    SessionPersistMode,
    build_default_inputs_chain,
    build_default_llm_chain,
    build_default_output_chain,
    build_default_tool_chain,
    _make_session_persist_delegate,
)
from agent_core.turn_chain import (
    AuditLogHandler,
    ChunkParseHandler,
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


# ────────────────────────────────────────────────────────────────────
# 公共 helper:stub agent(handler __init__ 不访问,任意 object 都行)
# ────────────────────────────────────────────────────────────────────


class _StubAgent:
    """AgentBuilder 测试用 stub:什么都不做。

    handlers 只在 __init__ 存 agent 引用,不在构造时调用 agent.* 属性。
    所以这个 stub 可以是空类。
    """
    pass


# ────────────────────────────────────────────────────────────────────
# D6-5.1 factory 函数
# ────────────────────────────────────────────────────────────────────


class TestFactoryChains:
    """D6-5.1:4 个 build_default_*_chain() 返回的 TurnChain 包含预期 handler 顺序。"""

    def test_inputs_chain_has_three_handlers_in_order(self):
        agent = _StubAgent()
        chain = build_default_inputs_chain(agent)

        names = [h.name for h in chain]
        assert names == [
            "memory_retrieval",
            "system_prompt",
            "tools_schema_prepare",
        ], f"unexpected order: {names}"

    def test_llm_chain_has_two_handlers_in_order(self):
        agent = _StubAgent()
        chain = build_default_llm_chain(agent)

        names = [h.name for h in chain]
        assert names == ["llm_call", "chunk_parse"]

    def test_tool_chain_has_three_handlers_in_order(self):
        agent = _StubAgent()
        chain = build_default_tool_chain(agent)

        names = [h.name for h in chain]
        assert names == ["permission_check", "tool_dispatch", "tool_execute"]

    def test_output_chain_normal_mode_has_three_handlers(self):
        """NORMAL 模式:SessionPersist + AuditLog + MemoryBridgeExtract。"""
        agent = _StubAgent()
        chain = build_default_output_chain(agent, session_persist_mode=SessionPersistMode.NORMAL)

        names = [h.name for h in chain]
        assert names == [
            "session_persist",
            "audit_log",
            "memory_bridge_extract",
        ]

    def test_output_chain_delegate_mode_session_persist_is_noop(self):
        """DELEGATE 模式:SessionPersistHandler.handle() 被替换成 no-op。"""
        agent = _StubAgent()
        chain = build_default_output_chain(agent, session_persist_mode=SessionPersistMode.DELEGATE)

        # 找 session_persist handler 实例
        session_persist = next(h for h in chain if h.name == "session_persist")
        # 调 handle(),应该返回 HandlerResult(stop_chain=False) early,无副作用
        ctx = MagicMock()
        result = session_persist.handle(ctx)
        assert isinstance(result, HandlerResult)
        assert result.stop_chain is False


# ────────────────────────────────────────────────────────────────────
# D6-5.2 with_handler 顺序
# ────────────────────────────────────────────────────────────────────


class TestWithHandlerOrdering:
    """D6-5.2:named hook point — after/before/at 三种插入位置。"""

    def test_with_handler_after_named_handler(self):
        """after="llm_call" → 新 handler 插在 llm_call 之后。"""
        # 直接用 TurnChain 测(AgentBuilder.with_handler 只把指令 append 到 list,
        # apply 在 build() 时执行,需要 build 流程。但 TurnChain.add() 是同一份逻辑,
        # 所以直接测 TurnChain.add 即可验证 named hook point 的正确性)
        chain = TurnChain([LLMCallHandler(_StubAgent()), ChunkParseHandler(_StubAgent())])

        class CostTracker:
            name = "cost_tracker"
            def handle(self, ctx): return HandlerResult()

        chain.add(CostTracker(), after="llm_call")
        names = [h.name for h in chain]
        assert names == ["llm_call", "cost_tracker", "chunk_parse"]

    def test_with_handler_before_named_handler(self):
        chain = TurnChain([PermissionCheckHandler(_StubAgent()), ToolExecuteHandler(_StubAgent())])

        class AuditHook:
            name = "audit_hook"
            def handle(self, ctx): return HandlerResult()

        chain.add(AuditHook(), before="tool_execute")
        names = [h.name for h in chain]
        assert names == ["permission_check", "audit_hook", "tool_execute"]

    def test_with_handler_at_index(self):
        chain = TurnChain([
            MemoryRetrievalHandler(_StubAgent()),
            SystemPromptHandler(_StubAgent()),
            ToolsSchemaPrepareHandler(_StubAgent()),
        ])

        class Inject:
            name = "inject"
            def handle(self, ctx): return HandlerResult()

        chain.add(Inject(), at=1)
        names = [h.name for h in chain]
        assert names == ["memory_retrieval", "inject", "system_prompt", "tools_schema_prepare"]

    def test_with_handler_default_appends_to_end(self):
        chain = TurnChain([MemoryRetrievalHandler(_StubAgent())])

        class Tail:
            name = "tail"
            def handle(self, ctx): return HandlerResult()

        chain.add(Tail())
        names = [h.name for h in chain]
        assert names == ["memory_retrieval", "tail"]


# ────────────────────────────────────────────────────────────────────
# D6-5.3 plugin handler type check
# ────────────────────────────────────────────────────────────────────


class TestPluginHandlerTypeCheck:
    """D6-5.3:with_plugin_handler 拒绝非 PluginHandler 子类。"""

    def test_with_plugin_handler_rejects_non_subclass(self):
        class NotAPlugin:
            """故意不继承 PluginHandler。"""
            name = "not_a_plugin"
            def handle(self, ctx): return HandlerResult()

        builder = AgentBuilder()
        with pytest.raises(TypeError) as excinfo:
            builder.with_plugin_handler(NotAPlugin())
        assert "plugin handler 必须继承 PluginHandler" in str(excinfo.value)
        assert "NotAPlugin" in str(excinfo.value)

    def test_with_plugin_handler_accepts_subclass(self):
        class MyPlugin(PluginHandler):
            name = "my_plugin"
            def handle(self, ctx): return HandlerResult()

        builder = AgentBuilder()
        # 不抛
        result = builder.with_plugin_handler(MyPlugin())
        assert result is builder  # 返回 self 支持链式


# ────────────────────────────────────────────────────────────────────
# D6-5.4 use_real_session_persist toggle
# ────────────────────────────────────────────────────────────────────


class TestSessionPersistToggle:
    """D6-5.4:use_real_session_persist() 切换后,_real_session_persist 字段被设。"""

    def test_default_is_true(self):
        builder = AgentBuilder()
        assert builder._real_session_persist is True

    def test_set_false(self):
        builder = AgentBuilder()
        builder.use_real_session_persist(False)
        assert builder._real_session_persist is False

    def test_set_true_explicitly(self):
        builder = AgentBuilder()
        builder.use_real_session_persist(False)
        builder.use_real_session_persist(True)
        assert builder._real_session_persist is True

    def test_session_persist_mode_enum_values(self):
        assert SessionPersistMode.NORMAL.value == "normal"
        assert SessionPersistMode.DELEGATE.value == "delegate"


# ────────────────────────────────────────────────────────────────────
# D6-5.5 with_termination / with_phase_override 状态记录
# ────────────────────────────────────────────────────────────────────


class TestBuilderStateRecording:
    """D6-5.5:with_termination / with_phase_override 把传入对象存到 builder 状态。"""

    def test_with_termination_stores(self):
        from agent_core.agent_state import MaxTurnsTermination
        builder = AgentBuilder()
        term = MaxTurnsTermination(max_turns=5)
        builder.with_termination(term)
        assert builder._termination_override is term

    def test_with_phase_override_stores(self):
        from agent_core.agent_state import AgentPhase, SetupPhase
        from agent_core.turn_chain import TurnChain
        custom_phase = SetupPhase(TurnChain([MemoryRetrievalHandler(_StubAgent())]))
        builder = AgentBuilder()
        builder.with_phase_override(AgentPhase.SETUP, custom_phase)
        assert builder._phase_overrides[AgentPhase.SETUP] is custom_phase

    def test_with_handler_records_insert(self):
        class Hook:
            name = "hook"
            def handle(self, ctx): return HandlerResult()

        builder = AgentBuilder()
        builder.with_handler(Hook(), after="llm_call")
        assert len(builder._handler_inserts) == 1
        h, opts = builder._handler_inserts[0]
        assert h.name == "hook"
        assert opts["after"] == "llm_call"
        assert opts["before"] is None
        assert opts["at"] is None

    def test_chained_builder_calls_return_self(self):
        """链式调用:每个 with_* 方法返回 self。"""
        from agent_core.agent_state import AgentPhase, SetupPhase
        from agent_core.turn_chain import TurnChain

        builder = AgentBuilder()
        assert builder.with_termination(MagicMock()) is builder
        assert builder.use_real_session_persist(False) is builder
        custom_phase = SetupPhase(TurnChain([MemoryRetrievalHandler(_StubAgent())]))
        assert builder.with_phase_override(AgentPhase.SETUP, custom_phase) is builder


# ────────────────────────────────────────────────────────────────────
# D6-5.6 SessionPersistHandler DELEGATE mode 内部行为
# ────────────────────────────────────────────────────────────────────


class TestSessionPersistDelegateMode:
    """D6-5.6:_make_session_persist_delegate() 把 handle() 替换成 no-op。"""

    def test_make_session_persist_delegate_returns_handler_result(self):
        """直接调:替换 handle 后,无 _pending_tool_results 也无 session_manager 也能跑。"""
        agent = _StubAgent()
        handler = SessionPersistHandler(agent)
        _make_session_persist_delegate(handler)

        ctx = MagicMock()
        result = handler.handle(ctx)
        assert isinstance(result, HandlerResult)
        assert result.stop_chain is False

    def test_make_session_persist_delegate_no_session_calls(self):
        """DELEGATE 模式:即使 agent 有 _pending_tool_results,也不调 session_manager。"""
        agent = MagicMock()
        agent._pending_tool_results = [("tu_x", "out")]
        agent._session_manager = MagicMock()
        handler = SessionPersistHandler(agent)
        _make_session_persist_delegate(handler)

        ctx = MagicMock()
        handler.handle(ctx)
        # 不调 add_tool_results
        agent._session_manager.add_tool_results.assert_not_called()
        # pending 不清(因为 DELEGATE 模式不进 try/finally)
        assert agent._pending_tool_results == [("tu_x", "out")]


# ────────────────────────────────────────────────────────────────────
# D6-5.7 factory 函数对 stub agent 友好(handler __init__ 不访问)
# ────────────────────────────────────────────────────────────────────


class TestFactoryStubAgentFriendliness:
    """D6-5.7:stub agent(无任何 _session_manager / _pending_tool_results / tools 属性)
    能让所有 4 个 factory 成功构造 TurnChain。
    """

    def test_all_four_factories_accept_stub_agent(self):
        agent = _StubAgent()
        # 不抛
        inputs_chain = build_default_inputs_chain(agent)
        llm_chain = build_default_llm_chain(agent)
        tool_chain = build_default_tool_chain(agent)
        output_chain = build_default_output_chain(agent)

        assert len(inputs_chain) == 3
        assert len(llm_chain) == 2
        assert len(tool_chain) == 3
        assert len(output_chain) == 3


# ────────────────────────────────────────────────────────────────────
# D7-1:plugin handler §12 白名单 enforcement(with_handler 拒绝 PluginHandler)
# ────────────────────────────────────────────────────────────────────


class TestPluginHandlerEnforcement:
    """D7-1:with_handler 拒绝 PluginHandler 子类,强制走 with_plugin_handler。

    设计依据:docs §12.2 — plugin handler 只能 append 到 chain 末端,before/at 不允许。
    with_handler 提供 before/at,会被滥用绕过白名单。本测试确认 runtime check 拦截。
    """

    def test_with_handler_rejects_plugin_handler_via_after(self):
        """plugin handler 用 with_handler(after=...) 也拒绝(必须走 with_plugin_handler)。"""
        class MyPlugin(PluginHandler):
            name = "my_plugin"
            def handle(self, ctx): return HandlerResult()

        builder = AgentBuilder()
        with pytest.raises(TypeError) as excinfo:
            builder.with_handler(MyPlugin(), after="llm_call")
        assert "plugin handler" in str(excinfo.value)
        assert "with_plugin_handler" in str(excinfo.value)
        assert "MyPlugin" in str(excinfo.value)

    def test_with_handler_rejects_plugin_handler_via_before(self):
        """plugin handler + before= 显然违反白名单(LLMCall 之前)。"""
        class MyPlugin(PluginHandler):
            name = "my_plugin"
            def handle(self, ctx): return HandlerResult()

        builder = AgentBuilder()
        with pytest.raises(TypeError):
            builder.with_handler(MyPlugin(), before="llm_call")

    def test_with_handler_accepts_plain_handler(self):
        """普通 handler(非 PluginHandler)用 with_handler 正常通过。"""
        class PlainHook:
            name = "plain_hook"
            def handle(self, ctx): return HandlerResult()

        builder = AgentBuilder()
        # 不抛
        builder.with_handler(PlainHook(), after="llm_call")
        builder.with_handler(PlainHook(), before="llm_call")
        builder.with_handler(PlainHook(), at=0)

    def test_with_plugin_handler_still_works_after_d7_1(self):
        """D7-1 改动不影响 with_plugin_handler 的合法用法。"""
        class MyPlugin(PluginHandler):
            name = "my_plugin"
            def handle(self, ctx): return HandlerResult()

        builder = AgentBuilder()
        # 不抛
        result = builder.with_plugin_handler(MyPlugin(), after="memory_retrieval")
        assert result is builder
        assert len(builder._plugin_handler_inserts) == 1


# ────────────────────────────────────────────────────────────────────
# D7-2:with_phase_override 校验 phase 是 Phase 实例 + 有 _chain
# ────────────────────────────────────────────────────────────────────


class TestPhaseOverrideValidation:
    """D7-2:with_phase_override 拒绝非 Phase 实例 / 缺 _chain 的对象。"""

    def test_with_phase_override_accepts_real_phase(self):
        """正常的 Phase 实例(有 _chain)被接受。"""
        from agent_core.agent_state import AgentPhase
        from agent_core.agent_state import SetupPhase
        from agent_core.turn_chain import TurnChain

        chain = TurnChain([MemoryRetrievalHandler(_StubAgent())])
        custom_phase = SetupPhase(chain)

        builder = AgentBuilder()
        builder.with_phase_override(AgentPhase.SETUP, custom_phase)
        assert builder._phase_overrides[AgentPhase.SETUP] is custom_phase

    def test_with_phase_override_rejects_non_phase(self):
        """非 Phase 实例被拒(防止用户传 MagicMock / 错类型)。"""
        from agent_core.agent_state import AgentPhase

        builder = AgentBuilder()
        with pytest.raises(TypeError) as excinfo:
            builder.with_phase_override(AgentPhase.SETUP, "not a phase")
        assert "Phase" in str(excinfo.value)

    def test_with_phase_override_rejects_object_without_chain(self):
        """对象缺 _chain 属性被拒(防止 Phase 子类漏实现)。"""
        from agent_core.agent_state import AgentPhase, Phase

        class BrokenPhase(Phase):
            """故意不调 super().__init__,所以 _chain 不存在。"""
            def __init__(self):
                # 不调 super().__init__,绕开 _chain 默认赋值
                pass
            def enter(self, trigger, ctx): return iter([])
            def next(self, trigger, ctx):
                from agent_core.agent_state import AgentPhase
                return ("done", AgentPhase.DONE)

        builder = AgentBuilder()
        with pytest.raises(TypeError) as excinfo:
            builder.with_phase_override(AgentPhase.SETUP, BrokenPhase())
        assert "_chain" in str(excinfo.value)