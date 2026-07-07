"""
AgentBuilder API 单元测试(2026-06-30 — D6-5)。

覆盖(5 case,Plan B Step 8 删 D6-5.4 + D6-5.6):
    D6-5.1: factory 函数返回的 TurnChain 包含预期 handler 顺序
    D6-5.2: with_handler(after=...) / before=... / at=... 顺序正确
    D6-5.3: with_plugin_handler 拒绝非 PluginHandler 子类
    D6-5.5: with_termination / with_phase_override 状态被记录
    D6-5.7: factory 函数对 stub agent 友好(handler __init__ 不访问 agent 属性)

Plan B Step 8 (2026-07-01):删 use_real_session_persist / SessionPersistMode DELEGATE 模式
(D6-5.4 + D6-5.6),output_chain 恒为真实现 — TestSessionPersistToggle +
TestSessionPersistDelegateMode 类删除,test_output_chain_delegate_mode_session_persist_is_noop
并入 test_output_chain_has_three_handlers。

设计参考:docs/agent-state-machine-and-chain-of-responsibility-design.md §11(AgentBuilder)
+ §15(Chain of Responsibility)
"""

from __future__ import annotations

from typing import List
from unittest.mock import MagicMock

import pytest

from agent_core.builder import (
    AgentBuilder,
    build_default_inputs_chain,
    build_default_llm_chain,
    build_default_output_chain,
    build_default_tool_chain,
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

    def test_inputs_chain_has_six_handlers_in_order(self):
        """inputs_chain 6 个 handler(选项 A):turn_indicator + context_compaction +
        tools_schema_prepare + system_prompt + memory_retrieval + skills_prompt。

        选项 A 重构 (2026-07-06):system_prompt 装配回归 inputs_chain,3 个 handler 通过
        ctx.append_system 顺序累加。加新 system 段 = with_handler(after=X)。
        """
        agent = _StubAgent()
        chain = build_default_inputs_chain(agent)

        names = [h.name for h in chain]
        assert names == [
            "turn_indicator",
            "context_compaction",
            "tools_schema_prepare",
            "system_prompt",
            "memory_retrieval",
            "skills_prompt",
        ], f"unexpected order: {names}"

    def test_llm_chain_has_four_handlers_in_order(self):
        """LLM chain:llm_call + chunk_parse + **inline_xml_fallback** + llm_call_persist。
        003-react-inline-xml-fallback-parser(T012):inline_xml_fallback 插在 chunk_parse 后、
        llm_call_persist 前,确保 stage_outputs.tool_calls 在 assistant message 落盘前补回。"""
        agent = _StubAgent()
        chain = build_default_llm_chain(agent)

        names = [h.name for h in chain]
        assert names == [
            "llm_call", "chunk_parse", "inline_xml_fallback", "llm_call_persist"
        ]

    def test_tool_chain_has_four_handlers_in_order(self):
        """Tool chain:permission_check + tool_dispatch + tool_execute + tool_pair_persist(Stage B,Plan B Step 2)。"""
        agent = _StubAgent()
        chain = build_default_tool_chain(agent)

        names = [h.name for h in chain]
        assert names == ["permission_check", "tool_dispatch", "tool_execute", "tool_pair_persist"]

    def test_output_chain_has_seven_handlers(self):
        """output_chain 7 个 handler:final_answer_bookkeeping + final_answer_persist + audit_log + memory_bridge_extract + l3_sm_extract_trigger + session_flush + env_cleanup。

        Plan B Step 8:SessionPersistMode DELEGATE 模式已删,output_chain 恒为真实现。
        Plan B Final Phase (2026-07-02):_iter_phase_finalize 拆为 4 handler,
        output_chain 从 3 handler 扩展为 5(handler count +2)。
        Plan B Final Phase Step 2 (2026-07-02):加 L3SMExtractTriggerHandler
        取代 v1 run() L1776-L1821 内联块,output_chain 扩展为 6(handler count +1)。
        002-skill-secret-injection T035 (2026-07-06):append EnvCleanupHandler
        在 outputs_chain 末位(handler count +1 → 7)。
        """
        agent = _StubAgent()
        chain = build_default_output_chain(agent)

        names = [h.name for h in chain]
        assert names == [
            "final_answer_bookkeeping",
            "final_answer_persist",
            "audit_log",
            "memory_bridge_extract",
            "l3_sm_extract_trigger",
            "session_flush",
            "env_cleanup",
        ]


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
        custom_phase = SetupPhase(TurnChain([MemoryRetrievalHandler(_StubAgent())]))
        assert builder.with_phase_override(AgentPhase.SETUP, custom_phase) is builder


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

        # Plan B Step 1-2 加 Stage A/B 持久化 handler + R4 加 ContextCompaction +
        # 2026-07-02 SRP 重构加 TurnIndicator 并把 SystemPrompt/MemoryRetrieval 拆为真实现
        assert len(inputs_chain) == 6    # turn_indicator + context_compaction + tools_schema_prepare + system_prompt + memory_retrieval + skills_prompt (选项 A)
        assert len(llm_chain) == 4       # llm_call + chunk_parse + inline_xml_fallback + llm_call_persist (003)
        assert len(tool_chain) == 4      # permission_check + tool_dispatch + tool_execute + tool_pair_persist
        assert len(output_chain) == 7    # final_answer_bookkeeping + final_answer_persist + audit_log + memory_bridge_extract + l3_sm_extract_trigger + session_flush + env_cleanup (002 T035 2026-07-06)


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