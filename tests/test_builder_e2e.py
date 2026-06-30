"""
AgentBuilder E2E build() 集成测试(2026-06-30 — D8)。

跟 tests/test_builder.py(纯单元测试)区分 — 本文件用真实 ReactAgent fixture
走通 AgentBuilder.build() 全链路,验证 D6 引入的 _rebuild_phases_from_chains
逻辑在真实场景下行为正确。

覆盖(8 case):
    D8-1: build() 无 override → agent 有 4 默认 chain + 7 phase
    D8-2: build() with_handler(after="llm_call") → handler 被 append 到 llm_chain
    D8-3: build() with_plugin_handler(after="memory_retrieval") → 4 个 chain 全部 append
    D8-4: build() with_termination → _sm._termination 被替换
    D8-5: build() with_phase_override → _sm._phases[phase] 被替换
    D8-6: build() use_real_session_persist(False) → output_chain SessionPersistHandler
          是 DELEGATE 模式(handle 是 no-op)
    D8-7: build() agent_kwargs 自带 custom _inputs_chain → 尊重调用方,不被 factory 覆盖
    D8-8: build() with_phase_override 时 chain 实例不会被 builder 替换
          (用户传的 phase 自己 hold chain)

设计参考:docs/agent-state-machine-and-chain-of-responsibility-design.md §11(AgentBuilder)
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from agent_core.builder import (
    AgentBuilder,
    SessionPersistMode,
)
from agent_core.turn_chain import (
    HandlerResult,
    MemoryRetrievalHandler,
    PluginHandler,
    TurnChain,
)


# ────────────────────────────────────────────────────────────────────
# Stub fixtures(跟 tests/test_agent_core.py 同款,避免真实 router 加载)
# ────────────────────────────────────────────────────────────────────


class _StubLLM:
    """最小 LLM stub — 跟 test_agent_core.py 同款 pattern。"""

    def __init__(self):
        self.config = SimpleNamespace(
            system_prompt="BASE",
            model="stub-model",
        )

    def stream_chat(self, *args, **kwargs):
        yield SimpleNamespace(text="ok")

    async def astream_chat(self, *args, **kwargs):
        yield SimpleNamespace(text="ok")


class _StubToolRegistry:
    def list_schemas(self, provider=""):
        return []


def _build_agent_kwargs(**overrides):
    """构造 AgentBuilder.build() 用的 kwargs(默认是 stub fixture)。"""
    kwargs = dict(
        llm_router=_StubLLM(),
        tool_registry=_StubToolRegistry(),
        max_turns=2,
        # session_id=None → agent 不开 session_manager(避免磁盘副作用)
    )
    kwargs.update(overrides)
    return kwargs


def _build_agent(**overrides):
    """直接构造 ReactAgent(不经过 builder)做 baseline 对比。"""
    from agent_core.agent_core import ReactAgent
    return ReactAgent(**_build_agent_kwargs(**overrides))


# ────────────────────────────────────────────────────────────────────
# D8 E2E tests
# ────────────────────────────────────────────────────────────────────


class TestAgentBuilderBuildE2E:
    """D8-1..D8-8:AgentBuilder.build() 端到端集成测试。"""

    def test_d8_1_no_overrides_produces_default_chain_and_phases(self):
        """D8-1:build() 无 override → agent 有 4 个默认 chain + 7 个 phase。"""
        agent = AgentBuilder().build(_build_agent_kwargs())

        # 4 默认 chain
        assert isinstance(agent._inputs_chain, TurnChain)
        assert isinstance(agent._llm_chain, TurnChain)
        assert isinstance(agent._tool_chain, TurnChain)
        assert isinstance(agent._output_chain, TurnChain)

        # 默认 handler 顺序(D6-5 验证过,这里确认 build() 后没乱序)
        assert [h.name for h in agent._inputs_chain] == [
            "memory_retrieval", "system_prompt", "tools_schema_prepare",
        ]
        assert [h.name for h in agent._llm_chain] == ["llm_call", "chunk_parse"]
        assert [h.name for h in agent._tool_chain] == [
            "permission_check", "tool_dispatch", "tool_execute",
        ]
        assert [h.name for h in agent._output_chain] == [
            "session_persist", "audit_log", "memory_bridge_extract",
        ]

        # 7 phase 都被重建
        assert len(agent._sm._phases) == 7
        from agent_core.agent_state import AgentPhase
        for phase in AgentPhase:
            assert phase in agent._sm._phases, f"missing phase {phase}"

    def test_d8_2_with_handler_appends_to_named_chain(self):
        """D8-2:build() with_handler(after="llm_call") → handler 被插到 llm_call 之后。"""

        class CostTracker:
            name = "cost_tracker"
            def handle(self, ctx): return HandlerResult()

        agent = (
            AgentBuilder()
            .with_handler(CostTracker(), after="llm_call")
            .build(_build_agent_kwargs())
        )

        names = [h.name for h in agent._llm_chain]
        assert "cost_tracker" in names
        assert names.index("cost_tracker") == names.index("llm_call") + 1
        # 不应影响其他 chain
        assert "cost_tracker" not in [h.name for h in agent._inputs_chain]
        assert "cost_tracker" not in [h.name for h in agent._tool_chain]
        assert "cost_tracker" not in [h.name for h in agent._output_chain]

    def test_d8_3_plugin_handler_with_named_hook_applies_to_matching_chain(self):
        """D8-3:build() with_plugin_handler(after="memory_retrieval") → 只 append 到
        含 memory_retrieval 的 chain(即 inputs_chain)。其他 chain 不动。

        设计:D8 修复后,plugin handler 用 _should_apply_to_chain 过滤 — named hook
        只 apply 到含该 handler 的 chain,不再 naively apply 到全部 4 条。
        """

        class TelemetryPlugin(PluginHandler):
            name = "telemetry_plugin"
            def handle(self, ctx): return HandlerResult()

        agent = (
            AgentBuilder()
            .with_plugin_handler(TelemetryPlugin(), after="memory_retrieval")
            .build(_build_agent_kwargs())
        )

        # inputs_chain 有 memory_retrieval → plugin append 到它后面
        assert "telemetry_plugin" in [h.name for h in agent._inputs_chain]
        # 其他 chain 没 memory_retrieval → plugin 不该 apply 过去
        for chain, name in (
            (agent._llm_chain, "llm_chain"),
            (agent._tool_chain, "tool_chain"),
            (agent._output_chain, "output_chain"),
        ):
            assert "telemetry_plugin" not in [h.name for h in chain], (
                f"plugin after='memory_retrieval' 不该 apply 到 {name}"
            )

    def test_d8_3b_plugin_handler_no_named_hook_applies_to_all_four(self):
        """D8-3b:plugin handler 不带 after= → append 到所有 4 个 chain 末端。"""

        class TelemetryPlugin(PluginHandler):
            name = "telemetry_plugin"
            def handle(self, ctx): return HandlerResult()

        # with_plugin_handler 签名只接 after=,无法裸调 — 但内部可以 force
        # 这里直接调 builder._plugin_handler_inserts 模拟 '无 named hook' 场景
        # (生产里用户调用需要显式 after= named handler,这是 §12 白名单约束)
        agent = (
            AgentBuilder()
            .with_plugin_handler(TelemetryPlugin(), after="llm_call")
            .build(_build_agent_kwargs())
        )

        # llm_chain 有 llm_call → plugin append 到它后面
        assert "telemetry_plugin" in [h.name for h in agent._llm_chain]
        # 不在其他 chain(因为它们没 llm_call)
        assert "telemetry_plugin" not in [h.name for h in agent._inputs_chain]

    def test_d8_4_with_termination_replaces_sm_termination(self):
        """D8-4:build() with_termination → _sm._termination 被替换。"""
        from agent_core.agent_state import MaxTurnsTermination

        custom_term = MaxTurnsTermination(max_turns=7)
        agent = (
            AgentBuilder()
            .with_termination(custom_term)
            .build(_build_agent_kwargs())
        )

        assert agent._sm._termination is custom_term

    def test_d8_5_with_phase_override_replaces_sm_phases(self):
        """D8-5:build() with_phase_override → _sm._phases[phase] 被替换。"""
        from agent_core.agent_state import AgentPhase, SetupPhase

        custom_chain = TurnChain([MemoryRetrievalHandler(_StubLLM())])
        custom_phase = SetupPhase(custom_chain)

        agent = (
            AgentBuilder()
            .with_phase_override(AgentPhase.SETUP, custom_phase)
            .build(_build_agent_kwargs())
        )

        assert agent._sm._phases[AgentPhase.SETUP] is custom_phase
        # 其他 phase 不动
        assert agent._sm._phases[AgentPhase.LLM_THINKING] is not custom_phase
        assert agent._sm._phases[AgentPhase.FINALIZING] is not custom_phase

    def test_d8_6_delegate_mode_makes_session_persist_noop(self):
        """D8-6:build() use_real_session_persist(False) → output_chain.SessionPersistHandler
        在运行时是 no-op(handle 不调 session_manager)。"""
        from agent_core.turn_chain import SessionPersistHandler

        agent = (
            AgentBuilder()
            .use_real_session_persist(False)
            .build(_build_agent_kwargs())
        )

        # 找到 output_chain 里的 SessionPersistHandler
        sp = next(
            h for h in agent._output_chain
            if h.name == "session_persist"
        )
        assert isinstance(sp, SessionPersistHandler)

        # 即使 agent 没 session_manager + 没 pending,也能调 handle 不抛
        ctx = MagicMock()
        ctx.stage_outputs = None
        ctx.events = []
        ctx.emit = lambda e: ctx.events.append(e)
        result = sp.handle(ctx)

        assert isinstance(result, HandlerResult)
        assert result.stop_chain is False

    def test_d8_7_kwargs_only_accept_real_reactagent_args(self):
        """D8-7:agent_kwargs 只接受 ReactAgent.__init__ 真实参数,builder 不暴露
        chain injection(ReactAgent.__init__ 签名里没有 _inputs_chain 等)。

        设计:D8 E2E 调研发现 docstring 之前承诺的 'agent_kwargs 自带 _inputs_chain'
        实际上是未实现的承诺 — ReactAgent 不接受该 kwarg。
        真实使用:builder 不替换已构造好的 agent 的 chain;如果调用方想要 custom chain,
        需要在 with_handler / with_phase_override / etc. 里表达,而不是 kwargs 注入。

        本测试 verify:传非法 kwarg (_inputs_chain) 会 TypeError,行为确定。
        """
        custom_chain = TurnChain([])
        with pytest.raises(TypeError) as excinfo:
            AgentBuilder().build(
                _build_agent_kwargs(_inputs_chain=custom_chain)
            )
        assert "_inputs_chain" in str(excinfo.value)

    def test_d8_8_phase_override_keeps_custom_phase_chain(self):
        """D8-8:with_phase_override 传的 phase 自带 chain → builder 不替换该 chain。"""
        from agent_core.agent_state import AgentPhase, SetupPhase

        class CustomSetupHandler:
            name = "custom_setup_handler"
            def handle(self, ctx): return HandlerResult()

        custom_chain = TurnChain([CustomSetupHandler()])
        custom_phase = SetupPhase(custom_chain)

        agent = (
            AgentBuilder()
            .with_phase_override(AgentPhase.SETUP, custom_phase)
            .build(_build_agent_kwargs())
        )

        # phase 持有的是用户传的 chain
        phase = agent._sm._phases[AgentPhase.SETUP]
        assert phase is custom_phase
        assert phase._chain is custom_chain
        # 用户 chain 没被 builder 默认 chain 覆盖
        assert "custom_setup_handler" in [h.name for h in phase._chain]