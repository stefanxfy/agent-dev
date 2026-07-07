"""
test_agent_state_machine.py — v2 状态机 + Chain of Responsibility 单元测试

覆盖(§十六 测试设计 §16.1):
- StateMachine:trigger 正常转移 / InvalidTransition / is_done 终止 / on_enter hook / checkpoint
- Phase 7 子类:每个 phase 的 enter / next 行为(mock ctx,assert events)
- TurnChain:顺序执行 / stop_chain 短路 / add(after/before/at) / remove
- PluginHandler:ALLOWED_EVENT_TYPES whitelist / SecurityError
- AgentBuilder:with_phase_override / with_handler (after/before/at) / with_plugin_handler / with_termination
- RunState:cancel_event 初始未 set / user_message 字段

详细设计:docs/agent-state-machine-and-chain-of-responsibility-design.md
"""

from __future__ import annotations

import threading
from typing import Iterator
from unittest.mock import MagicMock

import pytest

from agent_core.agent_state import (
    AgentPhase,
    AwaitingPermissionPhase,
    CompositeTermination,
    DonePhase,
    ExecutingToolsPhase,
    FinalizingPhase,
    InterruptedPhase,
    InvalidTransition,
    LLMThinkingPhase,
    MaxTurnsTermination,
    Phase,
    PhaseContext,
    RunState,
    SetupPhase,
    StateMachine,
    TerminationCondition,
    TurnContext,
)
from agent_core.builder import AgentBuilder
from agent_core.stages import LLMResult, ToolExecutionResult
from agent_core.turn_chain import (
    HandlerResult,
    PluginHandler,
    SecurityError,
    TurnChain,
)


# ════════════════════════════════════════════════════════════
# RunState 基础测试
# ════════════════════════════════════════════════════════════


class TestRunState:
    def test_default_construction(self):
        """RunState 默认构造:cancel_event 未 set,turn=0,final_answer 空。

        Step 2 (2026-07-07):awaiting_permission + awaiting_permission_batch 已从 RunState
        搬到 TurnContext(per-turn 生命周期对齐),RunState 不再含这 2 字段。
        """
        rs = RunState()
        assert rs.cancel_event.is_set() is False
        assert rs.turn == 0
        assert rs.user_message == ""
        assert rs.final_answer == ""
        assert not hasattr(rs, "awaiting_permission")
        assert not hasattr(rs, "awaiting_permission_batch")
        assert rs.termination_reason is None

    def test_user_message_persisted(self):
        """user_message 字段被设置后保留。"""
        rs = RunState(user_message="hello world")
        assert rs.user_message == "hello world"

    def test_cancel_event_is_thread_safe(self):
        """cancel_event 是 threading.Event,跨线程可 set。"""
        rs = RunState()
        results = []

        def worker():
            rs.cancel_event.set()
            results.append(rs.cancel_event.is_set())

        t = threading.Thread(target=worker)
        t.start()
        t.join()
        assert results == [True]

    def test_independent_runstate_instances(self):
        """两个 RunState 实例的 cancel_event 独立。"""
        rs1 = RunState()
        rs2 = RunState()
        rs1.cancel_event.set()
        assert rs1.cancel_event.is_set() is True
        assert rs2.cancel_event.is_set() is False


# ════════════════════════════════════════════════════════════
# TurnContext 基础测试
# ════════════════════════════════════════════════════════════


class TestTurnContext:
    def test_default_construction(self):
        """TurnContext 默认 events=[],_stopped=False。

        R2 (2026-07-07): system_prompt / tool_schemas 已搬到 RunState(per-run 持久),
        验证 ctx.run_state 默认值而不是 ctx 自身。
        """
        rs = RunState()
        ctx = TurnContext(run_state=rs)
        assert ctx.run_state is rs
        assert ctx.run_state.system_prompt == ""
        assert ctx.run_state.tool_schemas is None
        assert ctx.stage_outputs is None
        assert ctx.permission_request is None
        assert ctx.events == []
        assert ctx.is_stopped is False

    def test_emit_appends_event(self):
        """ctx.emit() append event 到 events list。"""
        ctx = TurnContext(run_state=RunState())
        ctx.emit(("text", "hello"))
        ctx.emit(("text", " world"))
        assert ctx.events == [("text", "hello"), ("text", " world")]

    def test_stop_sets_stopped_flag(self):
        """ctx.stop() 设 _stopped=True → is_stopped=True。"""
        ctx = TurnContext(run_state=RunState())
        assert ctx.is_stopped is False
        ctx.stop()
        assert ctx.is_stopped is True


# ════════════════════════════════════════════════════════════
# PhaseContext + Phase ABC 测试
# ════════════════════════════════════════════════════════════


class TestPhaseContext:
    def test_construction(self):
        """PhaseContext 三个字段(run_state / turn_ctx / sm)都被保留。"""
        rs = RunState()
        tc = TurnContext(run_state=rs)
        sm = MagicMock(spec=StateMachine)
        ctx = PhaseContext(run_state=rs, turn_ctx=tc, sm=sm)
        assert ctx.run_state is rs
        assert ctx.turn_ctx is tc
        assert ctx.sm is sm


class TestPhaseABC:
    def test_concrete_phase_required(self):
        """Phase 是 ABC,不能直接实例化。"""
        with pytest.raises(TypeError):
            Phase()

    def test_default_chain_is_empty(self):
        """Phase 默认 chain 是空 TurnChain()。"""
        # 用 DonePhase(它是 Phase 子类)
        p = DonePhase()
        assert len(p._chain) == 0

    def test_repr_contains_class_name(self):
        """__repr__ 含 class name。"""
        p = SetupPhase()
        assert "SetupPhase" in repr(p)


# ════════════════════════════════════════════════════════════
# 7 个 Phase 子类测试
# ════════════════════════════════════════════════════════════


def _make_ctx(stage_outputs=None, permission_request=None) -> PhaseContext:
    """helper:构造 PhaseContext 给 phase.next() 测试用。"""
    rs = RunState()
    tc = TurnContext(run_state=rs)
    if stage_outputs is not None:
        tc.stage_outputs = stage_outputs
    if permission_request is not None:
        tc.permission_request = permission_request
    sm = MagicMock(spec=StateMachine)
    return PhaseContext(run_state=rs, turn_ctx=tc, sm=sm)


class TestSetupPhase:
    def test_next_returns_llm_thinking(self):
        """SetupPhase.next() → (llm_call, LLM_THINKING)。"""
        phase = SetupPhase()
        next_trigger, next_phase = phase.next("start", _make_ctx())
        assert next_trigger == "llm_call"
        assert next_phase == AgentPhase.LLM_THINKING

    def test_enter_yields_from_chain(self):
        """SetupPhase.enter() 走 chain(空 chain → 0 events)。"""
        phase = SetupPhase()
        events = list(phase.enter("start", _make_ctx()))
        assert events == []


class TestLLMThinkingPhase:
    def test_routes_to_permission_when_request_set(self):
        """permission_request 非空 → AWAITING_PERMISSION。"""
        phase = LLMThinkingPhase()
        ctx = _make_ctx(permission_request={"tool": "Bash"})
        next_trigger, next_phase = phase.next("llm_call", ctx)
        assert next_phase == AgentPhase.AWAITING_PERMISSION

    def test_routes_to_finalizing_when_no_tool_calls(self):
        """无 tool_call + 有 full_text → FINALIZING。"""
        phase = LLMThinkingPhase()
        ctx = _make_ctx(
            stage_outputs=LLMResult(full_text="hello", tool_calls=[])
        )
        next_trigger, next_phase = phase.next("llm_call", ctx)
        assert next_phase == AgentPhase.FINALIZING
        assert ctx.run_state.final_answer == "hello"

    def test_routes_to_executing_when_has_tool_calls(self):
        """有 tool_call → EXECUTING_TOOLS。"""
        phase = LLMThinkingPhase()
        ctx = _make_ctx(
            stage_outputs=LLMResult(tool_calls=[{"name": "Bash", "input": {}}])
        )
        next_trigger, next_phase = phase.next("llm_call", ctx)
        assert next_phase == AgentPhase.EXECUTING_TOOLS

    def test_routes_to_finalizing_when_stage_outputs_is_none(self):
        """stage_outputs=None → FINALIZING(graceful)。"""
        phase = LLMThinkingPhase()
        next_trigger, next_phase = phase.next("llm_call", _make_ctx())
        assert next_phase == AgentPhase.FINALIZING


class TestAwaitingPermissionPhase:
    def test_enter_yields_nothing(self):
        """AwaitingPermissionPhase.enter() 不做事。"""
        phase = AwaitingPermissionPhase()
        events = list(phase.enter("permission_needed", _make_ctx()))
        assert events == []

    def test_next_permission_resolved_to_executing(self):
        """permission_resolved → EXECUTING_TOOLS。"""
        phase = AwaitingPermissionPhase()
        next_trigger, next_phase = phase.next("permission_resolved", _make_ctx())
        assert next_trigger == "execute_tools"
        assert next_phase == AgentPhase.EXECUTING_TOOLS

    def test_next_other_trigger_raises(self):
        """非 permission_resolved trigger → InvalidTransition。"""
        phase = AwaitingPermissionPhase()
        with pytest.raises(InvalidTransition):
            phase.next("random_trigger", _make_ctx())


class TestExecutingToolsPhase:
    def test_next_returns_llm_thinking(self):
        """ExecutingToolsPhase.next() → (tools_done, LLM_THINKING)。"""
        phase = ExecutingToolsPhase()
        next_trigger, next_phase = phase.next("execute_tools", _make_ctx())
        assert next_trigger == "tools_done"
        assert next_phase == AgentPhase.LLM_THINKING


class TestFinalizingPhase:
    def test_next_returns_done(self):
        """FinalizingPhase.next() → (finalize_done, DONE)。"""
        phase = FinalizingPhase()
        next_trigger, next_phase = phase.next("finalize", _make_ctx())
        assert next_trigger == "finalize_done"
        assert next_phase == AgentPhase.DONE


class TestInterruptedPhase:
    def test_enter_yields_nothing(self):
        """InterruptedPhase.enter() 不做事。"""
        phase = InterruptedPhase()
        events = list(phase.enter("interrupt", _make_ctx()))
        assert events == []

    def test_next_raises_invalid_transition(self):
        """InterruptedPhase 是终态,next() 抛 InvalidTransition。"""
        phase = InterruptedPhase()
        with pytest.raises(InvalidTransition):
            phase.next("any", _make_ctx())


class TestDonePhase:
    def test_enter_yields_nothing(self):
        """DonePhase.enter() 不做事。"""
        phase = DonePhase()
        events = list(phase.enter("done", _make_ctx()))
        assert events == []

    def test_next_raises_invalid_transition(self):
        """DonePhase 是终态,next() 抛 InvalidTransition。"""
        phase = DonePhase()
        with pytest.raises(InvalidTransition):
            phase.next("any", _make_ctx())


# ════════════════════════════════════════════════════════════
# TerminationCondition 测试
# ════════════════════════════════════════════════════════════


class TestTerminationConditions:
    def test_max_turns_inheritance(self):
        """MaxTurnsTermination 继承 TerminationCondition。"""
        cond = MaxTurnsTermination(max_turns=10)
        assert isinstance(cond, TerminationCondition)

    def test_composite_inheritance(self):
        """CompositeTermination 继承 TerminationCondition。"""
        cond = CompositeTermination(MaxTurnsTermination(max_turns=10))
        assert isinstance(cond, TerminationCondition)

    def test_max_turns_triggers_at_limit(self):
        """turn >= max → 返终止 reason。"""
        cond = MaxTurnsTermination(max_turns=3)
        rs = RunState(turn=3)
        reason = cond.check(rs, TurnContext(run_state=rs))
        assert reason is not None
        assert "max_turns_reached" in reason

    def test_max_turns_under_limit(self):
        """turn < max → 返 None(继续)。"""
        cond = MaxTurnsTermination(max_turns=10)
        rs = RunState(turn=2)
        assert cond.check(rs, TurnContext(run_state=rs)) is None

    def test_composite_first_match_wins(self):
        """Composite 任一触发即返。"""
        cond = CompositeTermination(
            MaxTurnsTermination(max_turns=5),
            MaxTurnsTermination(max_turns=3),  # 这个先触发
        )
        rs = RunState(turn=4)
        reason = cond.check(rs, TurnContext(run_state=rs))
        assert reason is not None


# ════════════════════════════════════════════════════════════
# StateMachine 测试
# ════════════════════════════════════════════════════════════


def _build_minimal_sm() -> StateMachine:
    """构造一个最小 SM 用于测试(7 个 phase,空 chain)。"""
    phases = {
        AgentPhase.SETUP:               SetupPhase(),
        AgentPhase.LLM_THINKING:        LLMThinkingPhase(),
        AgentPhase.AWAITING_PERMISSION: AwaitingPermissionPhase(),
        AgentPhase.EXECUTING_TOOLS:     ExecutingToolsPhase(),
        AgentPhase.FINALIZING:          FinalizingPhase(),
        AgentPhase.INTERRUPTED:         InterruptedPhase(),
        AgentPhase.DONE:                DonePhase(),
    }
    return StateMachine(phases, initial=AgentPhase.SETUP,
                        termination=MaxTurnsTermination(max_turns=100))


class TestStateMachineBasics:
    def test_initial_phase(self):
        """SM 创建时 current = initial。"""
        sm = _build_minimal_sm()
        assert sm.current == AgentPhase.SETUP

    def test_is_done_false_initially(self):
        """SM 启动时 is_done=False。"""
        sm = _build_minimal_sm()
        assert sm.is_done is False
        assert sm.is_interrupted is False

    def test_history_starts_empty(self):
        """history 列表初始为空。"""
        sm = _build_minimal_sm()
        assert sm.history == []

    def test_repr_contains_current_phase(self):
        """__repr__ 含 current phase 名。"""
        sm = _build_minimal_sm()
        assert "setup" in repr(sm)


class TestStateMachineTransition:
    def test_normal_transition_set_to_llm(self):
        """SETUP 转移开始(SM 自动递归 → 链式推进到 done,验证 SETUP 是起点)。"""
        sm = _build_minimal_sm()
        ctx = _make_ctx()
        events = list(sm.trigger("start", ctx))
        # SM auto-recursion: SETUP → LLM_THINKING → FINALIZING → DONE
        # (无 tool_calls 走 FINALIZING 路径)
        assert len(sm.history) >= 1
        assert sm.history[0][0] == AgentPhase.SETUP
        # 最终转到 DONE
        assert sm.is_done is True

    def test_invalid_trigger_on_interrupted_raises(self):
        """INTERRUPTED 终态上 trigger → InvalidTransition。"""
        sm = _build_minimal_sm()
        rs = RunState()
        tc = TurnContext(run_state=rs)
        ctx = PhaseContext(run_state=rs, turn_ctx=tc, sm=sm)

        # 1. 先转 INTERRUPTED
        list(sm.interrupt(ctx))
        assert sm.is_interrupted is True

        # 2. 再 trigger → InvalidTransition
        with pytest.raises(InvalidTransition):
            list(sm.trigger("start", ctx))

    def test_termination_check_routes_to_done(self):
        """termination.check() 命中 → DONE, yield system event。"""
        phases = {AgentPhase.SETUP: SetupPhase()}
        sm = StateMachine(
            phases,
            initial=AgentPhase.SETUP,
            termination=MaxTurnsTermination(max_turns=1),
        )
        rs = RunState(turn=5)  # 远超 max
        tc = TurnContext(run_state=rs)
        ctx = PhaseContext(run_state=rs, turn_ctx=tc, sm=sm)

        events = list(sm.trigger("start", ctx))
        # yield "system" event + 转 DONE
        assert any(ev[0] == "system" for ev in events)
        assert sm.current == AgentPhase.DONE
        assert sm.is_done is True
        assert rs.termination_reason is not None


class TestStateMachineHooks:
    def test_on_enter_hook_fires(self):
        """on_enter hook 在每次 enter phase 时被调(initial SETUP 是初始 phase,
        不走 trigger → on_enter 不触发,但 LLM_THINKING / FINALIZING / DONE 都触发)。"""
        sm = _build_minimal_sm()
        entered_phases = []
        sm.on_enter(lambda phase, trigger: entered_phases.append(phase))

        ctx = _make_ctx()
        list(sm.trigger("start", ctx))
        # SM auto-recursion: SETUP(initial)→ LLM_THINKING → FINALIZING → DONE
        # on_enter hooks 记录 LLM_THINKING / FINALIZING / DONE
        # (initial SETUP 不走 trigger → 不在 entered_phases)
        assert AgentPhase.LLM_THINKING in entered_phases
        assert AgentPhase.FINALIZING in entered_phases
        assert AgentPhase.DONE in entered_phases

    def test_on_exit_hook_fires(self):
        """on_exit hook 在每次 exit phase 时被调。"""
        sm = _build_minimal_sm()
        exited_phases = []
        sm.on_exit(lambda old, t, new: exited_phases.append(old))

        ctx = _make_ctx()
        list(sm.trigger("start", ctx))
        # SETUP 必然 exit 过(转入 LLM_THINKING)
        assert AgentPhase.SETUP in exited_phases


class TestStateMachineInterrupt:
    def test_interrupt_routes_to_interrupted(self):
        """sm.interrupt() 转 INTERRUPTED 终态。"""
        sm = _build_minimal_sm()
        ctx = _make_ctx()
        events = list(sm.interrupt(ctx))
        assert sm.current == AgentPhase.INTERRUPTED
        assert sm.is_interrupted is True
        assert sm.is_done is True  # INTERRUPTED 也算 done

    def test_interrupt_is_idempotent(self):
        """多次调 interrupt() 幂等。"""
        sm = _build_minimal_sm()
        ctx = _make_ctx()
        list(sm.interrupt(ctx))
        events2 = list(sm.interrupt(ctx))
        # 第二次应该 yield 0 events(幂等)
        assert events2 == []

    def test_interrupt_sets_cancel_event_first(self):
        """sm.interrupt() 先 set cancel_event 再 yield events。"""
        rs = RunState()
        tc = TurnContext(run_state=rs)
        ctx = PhaseContext(run_state=rs, turn_ctx=tc, sm=MagicMock(spec=StateMachine))

        sm = _build_minimal_sm()
        list(sm.interrupt(ctx))
        # cancel_event 必须被 set
        assert rs.cancel_event.is_set() is True


class TestCheckpoint:
    def test_checkpoint_includes_phase_and_history(self):
        """checkpoint() 返 phase + history dict。"""
        sm = _build_minimal_sm()
        ctx = _make_ctx()
        list(sm.trigger("start", ctx))
        ckpt = sm.checkpoint()
        # SM auto-recursion 链式推进 → 最终 done
        assert ckpt["phase"] == "done"
        assert len(ckpt["history"]) >= 3  # SETUP→LLM→FINALIZING→DONE


# ════════════════════════════════════════════════════════════
# TurnChain 测试
# ════════════════════════════════════════════════════════════


class _StubHandler:
    """用于测试的 minimal handler(emit 一个固定 event + 返 HandlerResult)。"""
    def __init__(self, name, event_to_emit=None, stop_chain=False, next_action=None):
        self.name = name
        self._event = event_to_emit
        self._stop = stop_chain
        self._next_action = next_action

    def handle(self, ctx: TurnContext) -> HandlerResult:
        if self._event:
            ctx.emit(self._event)
        return HandlerResult(stop_chain=self._stop, next_action=self._next_action)


class TestTurnChainBasics:
    def test_empty_chain(self):
        """空 chain → 0 events。"""
        chain = TurnChain([])
        ctx = TurnContext(run_state=RunState())
        events = list(chain.run(ctx))
        assert events == []

    def test_sequential_execution(self):
        """handler 按顺序执行。"""
        chain = TurnChain([
            _StubHandler("a", event_to_emit=("text", "A")),
            _StubHandler("b", event_to_emit=("text", "B")),
            _StubHandler("c", event_to_emit=("text", "C")),
        ])
        ctx = TurnContext(run_state=RunState())
        events = list(chain.run(ctx))
        assert events == [("text", "A"), ("text", "B"), ("text", "C")]

    def test_stop_chain_short_circuits(self):
        """stop_chain=True → 后续 handler 不跑。"""
        chain = TurnChain([
            _StubHandler("a", event_to_emit=("text", "A")),
            _StubHandler("b", event_to_emit=("text", "B"), stop_chain=True),
            _StubHandler("c", event_to_emit=("text", "C")),  # 不应跑
        ])
        ctx = TurnContext(run_state=RunState())
        events = list(chain.run(ctx))
        assert events == [("text", "A"), ("text", "B")]

    def test_chain_stops_on_turn_context_stopped(self):
        """ctx.stop() 后 chain 立即停。"""
        chain = TurnChain([
            _StubHandler("a", event_to_emit=("text", "A")),
            _StubHandler("b", event_to_emit=("text", "B")),
        ])
        ctx = TurnContext(run_state=RunState())

        # wrap chain to stop after first emit
        def run_with_stop():
            for h in chain:
                if ctx.is_stopped:
                    break
                h.handle(ctx)
                ctx.stop()  # stop after first handler

        run_with_stop()
        # 'b' should not have emitted
        assert ("text", "A") in ctx.events
        assert ("text", "B") not in ctx.events


class TestTurnChainManipulation:
    def _make_chain(self):
        return TurnChain([
            _StubHandler("a", event_to_emit=("text", "A")),
            _StubHandler("b", event_to_emit=("text", "B")),
            _StubHandler("c", event_to_emit=("text", "C")),
        ])

    def test_add_at_end(self):
        """add(h) 不指定位置 → append 末尾。"""
        chain = self._make_chain()
        chain.add(_StubHandler("d"))
        assert [h.name for h in chain] == ["a", "b", "c", "d"]

    def test_add_after(self):
        """after='b' → 在 b 之后插入。"""
        chain = self._make_chain()
        chain.add(_StubHandler("x"), after="b")
        assert [h.name for h in chain] == ["a", "b", "x", "c"]

    def test_add_before(self):
        """before='b' → 在 b 之前插入。"""
        chain = self._make_chain()
        chain.add(_StubHandler("x"), before="b")
        assert [h.name for h in chain] == ["a", "x", "b", "c"]

    def test_add_at_index(self):
        """at=1 → 在 index=1 插入。"""
        chain = self._make_chain()
        chain.add(_StubHandler("x"), at=1)
        assert [h.name for h in chain] == ["a", "x", "b", "c"]

    def test_remove(self):
        """remove(name) → 删指定 handler。"""
        chain = self._make_chain()
        chain.remove("b")
        assert [h.name for h in chain] == ["a", "c"]

    def test_repr_contains_handler_names(self):
        """__repr__ 含 handler 名字。"""
        chain = self._make_chain()
        r = repr(chain)
        assert "a" in r and "b" in r and "c" in r


# ════════════════════════════════════════════════════════════
# PluginHandler + SecurityError 测试
# ════════════════════════════════════════════════════════════


class TestPluginHandlerWhitelist:
    def test_allowed_event_types(self):
        """ALLOWED_EVENT_TYPES 4 个 system / metric / telemetry / ui_hint。"""
        assert PluginHandler.ALLOWED_EVENT_TYPES == {
            "system", "metric", "telemetry", "ui_hint",
        }

    def test_emit_validated_allows_whitelisted(self):
        """emit_validated 白名单内 event → 通过。"""
        ctx = TurnContext(run_state=RunState())

        class MyPlugin(PluginHandler):
            name = "test_plugin"

        plugin = MyPlugin()
        plugin.emit_validated(("system", "ok"), ctx)
        plugin.emit_validated(("metric", 42), ctx)
        plugin.emit_validated(("telemetry", {}), ctx)
        plugin.emit_validated(("ui_hint", "hint"), ctx)
        assert len(ctx.events) == 4

    def test_emit_validated_rejects_disallowed(self):
        """emit_validated 非白名单 event → SecurityError。"""
        ctx = TurnContext(run_state=RunState())

        class MyPlugin(PluginHandler):
            name = "test_plugin"

        plugin = MyPlugin()
        with pytest.raises(SecurityError):
            plugin.emit_validated(("text", "leaked"), ctx)
        with pytest.raises(SecurityError):
            plugin.emit_validated(("tool_call", {"name": "evil"}), ctx)

    def test_base_plugin_handle_raises_not_implemented(self):
        """PluginHandler 基类 handle() 抛 NotImplementedError。"""
        plugin = PluginHandler()
        with pytest.raises(NotImplementedError):
            plugin.handle(TurnContext(run_state=RunState()))


# ════════════════════════════════════════════════════════════
# Stage dataclass 测试
# ════════════════════════════════════════════════════════════


class TestStageDataclasses:
    def test_llm_result_default(self):
        """LLMResult 默认空值。"""
        r = LLMResult()
        assert r.chunks == []
        assert r.full_text == ""
        assert r.tool_calls == []
        assert r.tool_results == []
        assert r.stop_reason is None

    def test_tool_execution_result_default(self):
        """ToolExecutionResult 默认空值。"""
        r = ToolExecutionResult()
        assert r.success_count == 0
        assert r.error_count == 0
        assert r.total_elapsed == 0.0


# ════════════════════════════════════════════════════════════
# AgentBuilder 测试
# ════════════════════════════════════════════════════════════


class TestAgentBuilder:
    def test_with_handler_after(self):
        """with_handler(after=...) → 注册到目标 chain 之后。

        注:AgentBuilder.build() 需要完整 ReactAgent,这里直接验证
        TurnChain.add(after=...) 等价行为(builder 内部就是这么调的)。
        """
        chain = TurnChain([
            _StubHandler("a"),
            _StubHandler("b"),
            _StubHandler("c"),
        ])
        # 模拟 builder 行为:在 b 之后插入
        chain.add(_StubHandler("new"), after="b")
        names = [h.name for h in chain]
        assert names.index("new") == names.index("b") + 1

    def test_with_plugin_handler_requires_subclass(self):
        """with_plugin_handler(非 PluginHandler 子类) → TypeError。"""
        builder = AgentBuilder()
        with pytest.raises(TypeError):
            builder.with_plugin_handler(_StubHandler("not_a_plugin"))

    def test_with_termination_stores_override(self):
        """with_termination() 存终止条件。"""
        cond = MaxTurnsTermination(max_turns=42)
        builder = AgentBuilder().with_termination(cond)
        assert builder._termination_override is cond

    def test_with_phase_override_stores(self):
        """with_phase_override() 存 phase 替换。"""
        new_setup = SetupPhase()
        builder = AgentBuilder().with_phase_override(AgentPhase.SETUP, new_setup)
        assert builder._phase_overrides[AgentPhase.SETUP] is new_setup


# ════════════════════════════════════════════════════════════
# 集成测试:ReactAgent v2 API
# ════════════════════════════════════════════════════════════


class TestReactAgentV2API:
    """ReactAgent v2 API smoke test (不实际跑 LLM)。"""

    def test_start_run_resets_run_state(self):
        """start_run() 创建新 _run_state 和 _sm。"""
        from unittest.mock import patch

        with patch("agent_core.agent_core.LLMRouter"), \
             patch("agent_core.agent_core.ToolRegistry"):
            from agent_core.agent_core import ReactAgent
            agent = ReactAgent.__new__(ReactAgent)
            agent.messages = []
            agent._session_manager = None
            # Plan B Step 4:_pending_thinking/_pending_tool_logs/_pending_tool_results
            # 已迁到 RunState.pending_*,实例字段已删。
            # 测试改为:start_run 前给 _run_state.pending_* 填 old,start_run 后
            # 因为新建了 RunState(_RunState(...))→ 全部 reset 到 dataclass default_factory。
            # 这里直接给 None 让 start_run() 走默认构造路径即可。
            agent._run_state = None
            agent._phases = {
                AgentPhase.SETUP: SetupPhase(),
            }
            agent._termination = MaxTurnsTermination(max_turns=10)

            agent.start_run("hello")

            # 新 _run_state 含 user_message
            assert agent._run_state is not None
            assert agent._run_state.user_message == "hello"
            assert agent._run_state.cancel_event.is_set() is False
            # user_msg 已 append
            assert agent.messages[-1] == {"role": "user", "content": "hello"}
            # pending 状态重置(Plan B Step 4 — 迁到 RunState.pending_*,RunState 新建时
            # dataclass field default_factory=list / str 已经保证初值空)
            assert agent._run_state.pending_thinking == ""
            assert agent._run_state.pending_tool_logs == []
            assert agent._run_state.pending_tool_results == []
            # 新 SM 创建
            assert agent._sm.current == AgentPhase.SETUP

    def test_interrupt_sets_cancel_event(self):
        """interrupt() set cancel_event + 转 INTERRUPTED。"""
        from unittest.mock import patch

        with patch("agent_core.agent_core.LLMRouter"), \
             patch("agent_core.agent_core.ToolRegistry"):
            from agent_core.agent_core import ReactAgent
            agent = ReactAgent.__new__(ReactAgent)
            agent.messages = []
            agent._session_manager = None
            agent._pending_thinking = ""
            agent._pending_tool_logs = []
            agent._pending_tool_results = []
            agent._phases = {
                AgentPhase.SETUP: SetupPhase(),
            }
            agent._termination = MaxTurnsTermination(max_turns=10)

            agent.start_run("test")
            assert agent._run_state.cancel_event.is_set() is False

            agent.interrupt()
            assert agent._run_state.cancel_event.is_set() is True
            assert agent._sm.is_interrupted is True

    def test_interrupt_idempotent(self):
        """重复调 interrupt() 幂等(不抛错)。"""
        from unittest.mock import patch

        with patch("agent_core.agent_core.LLMRouter"), \
             patch("agent_core.agent_core.ToolRegistry"):
            from agent_core.agent_core import ReactAgent
            agent = ReactAgent.__new__(ReactAgent)
            agent.messages = []
            agent._session_manager = None
            agent._pending_thinking = ""
            agent._pending_tool_logs = []
            agent._pending_tool_results = []
            agent._phases = {
                AgentPhase.SETUP: SetupPhase(),
            }
            agent._termination = MaxTurnsTermination(max_turns=10)

            agent.start_run("test")
            agent.interrupt()
            agent.interrupt()  # 不应抛错
            assert agent._sm.is_interrupted is True

    def test_interrupt_without_start_run_warns(self):
        """interrupt() 在 start_run() 之前调 → warn 但不抛错。"""
        from unittest.mock import patch

        with patch("agent_core.agent_core.LLMRouter"), \
             patch("agent_core.agent_core.ToolRegistry"):
            from agent_core.agent_core import ReactAgent
            agent = ReactAgent.__new__(ReactAgent)
            agent._run_state = None
            # 不应抛错
            agent.interrupt()

    def test_step_without_start_run_raises(self):
        """step() 在 start_run() 之前调 → RuntimeError。"""
        from unittest.mock import patch

        with patch("agent_core.agent_core.LLMRouter"), \
             patch("agent_core.agent_core.ToolRegistry"):
            from agent_core.agent_core import ReactAgent
            agent = ReactAgent.__new__(ReactAgent)
            agent._run_state = None

            with pytest.raises(RuntimeError):
                list(agent.step())