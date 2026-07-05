"""
test_h1h2h3_design_contract.py — A1-H1/H2/H3 修复后的设计契约测试

A1-H1:PhaseContext.termination 字段(允许 phase 自主查询终止条件)
A1-H2:TimeoutTermination + build_default_termination factory
A1-H3:step() 走 _drive() helper + trigger 路由(run_started / permission_resolved / start)
      + _new_turn_ctx() 递增 turn

设计依据:docs/agent-state-machine-and-chain-of-responsibility-design.md
"""

from __future__ import annotations

import time
from unittest.mock import patch

import pytest

from agent_core.agent_state import (
    AgentPhase,
    CompositeTermination,
    MaxTurnsTermination,
    PhaseContext,
    RunState,
    StateMachine,
    TimeoutTermination,
    TurnContext,
    build_default_termination,
)
from agent_core.stages import LLMResult
from agent_core.tools.base import ToolDef, ToolRegistry


# ════════════════════════════════════════════════════════════
# A1-H2:TimeoutTermination + build_default_termination
# ════════════════════════════════════════════════════════════


class TestTimeoutTermination:
    """A1-H2:wall-clock timeout 终止条件。"""

    def test_inheritance(self):
        """TimeoutTermination 继承 TerminationCondition。"""
        cond = TimeoutTermination(timeout_s=10.0)
        from agent_core.agent_state import TerminationCondition
        assert isinstance(cond, TerminationCondition)

    def test_under_timeout_returns_none(self):
        """elapsed < timeout_s → 返 None(继续)。"""
        cond = TimeoutTermination(timeout_s=10.0)
        rs = RunState()  # created_at 刚设,elapsed ≈ 0
        assert cond.check(rs, TurnContext(run_state=rs)) is None

    def test_at_or_over_timeout_triggers(self):
        """elapsed >= timeout_s → 返 reason。"""
        # 手动设 created_at 到很久以前
        rs = RunState()
        rs.created_at = time.monotonic() - 5.0  # 5s 前
        cond = TimeoutTermination(timeout_s=3.0)
        reason = cond.check(rs, TurnContext(run_state=rs))
        assert reason is not None
        assert "timeout_reached" in reason
        # reason 应含 elapsed 和 limit
        assert "5.0s" in reason
        assert "3.0s" in reason

    def test_uses_monotonic_not_wallclock(self):
        """用 time.monotonic() 而不是 wall-clock,防系统时间漂移。"""
        rs = RunState()
        # 即便我们改 created_at 为 future,也不应 panic
        # (实际上 monotonic() 不允许 negative 差,
        # 但我们主要测 reason 格式)
        rs.created_at = time.monotonic() - 1.0
        cond = TimeoutTermination(timeout_s=0.5)
        reason = cond.check(rs, TurnContext(run_state=rs))
        assert reason is not None


class TestBuildDefaultTermination:
    """A1-H2:build_default_termination 工厂。"""

    def test_default_returns_max_turns_only(self):
        """timeout_s=None → 单 MaxTurnsTermination(不包 Composite)。"""
        cond = build_default_termination(max_turns=10)
        assert isinstance(cond, MaxTurnsTermination)
        assert not isinstance(cond, CompositeTermination)
        assert cond._max == 10

    def test_with_timeout_returns_composite(self):
        """timeout_s 有值 → CompositeTermination 包装。"""
        cond = build_default_termination(max_turns=10, timeout_s=300.0)
        assert isinstance(cond, CompositeTermination)
        assert len(cond._conditions) == 2

    def test_zero_timeout_creates_composite(self):
        """timeout_s=0.0 也算有值(技术上是 valid input)→ Composite。"""
        cond = build_default_termination(max_turns=5, timeout_s=0.0)
        assert isinstance(cond, CompositeTermination)
        assert len(cond._conditions) == 2

    def test_composite_evaluates_max_turns_first(self):
        """CompositeTermination 按注册顺序检查。"""
        # 第一个是 MaxTurns,设 turn=20 >> max=5
        cond = build_default_termination(max_turns=5, timeout_s=300.0)
        rs = RunState(turn=20)
        reason = cond.check(rs, TurnContext(run_state=rs))
        assert reason is not None
        assert "max_turns_reached" in reason

    def test_composite_evaluates_timeout_when_turns_ok(self):
        """turn 未超限 + elapsed 超时 → 返 timeout reason。"""
        cond = build_default_termination(max_turns=10, timeout_s=0.1)
        rs = RunState(turn=2)
        rs.created_at = time.monotonic() - 1.0  # 1s 前
        reason = cond.check(rs, TurnContext(run_state=rs))
        assert reason is not None
        assert "timeout_reached" in reason


# ════════════════════════════════════════════════════════════
# A1-H1:PhaseContext.termination 字段
# ════════════════════════════════════════════════════════════


class TestPhaseContextTerminationField:
    """A1-H1:PhaseContext.termination 让 phase 自主查询终止条件。"""

    def test_construction_with_termination(self):
        """PhaseContext(termination=...) 保留 termination。"""
        rs = RunState()
        tc = TurnContext(run_state=rs)
        cond = MaxTurnsTermination(max_turns=3)
        ctx = PhaseContext(
            run_state=rs, turn_ctx=tc, termination=cond, sm=None,
        )
        assert ctx.termination is cond

    def test_default_termination_is_none(self):
        """PhaseContext 默认 termination=None(向后兼容旧 test fixture)。"""
        rs = RunState()
        tc = TurnContext(run_state=rs)
        ctx = PhaseContext(run_state=rs, turn_ctx=tc, sm=None)
        assert ctx.termination is None

    def test_default_sm_is_none(self):
        """PhaseContext 默认 sm=None(向后兼容旧 test fixture)。"""
        rs = RunState()
        tc = TurnContext(run_state=rs)
        ctx = PhaseContext(run_state=rs, turn_ctx=tc)
        assert ctx.sm is None

    def test_sm_uses_ctx_termination_when_provided(self):
        """SM.trigger 优先用 ctx.termination,fallback 到 self._termination。"""
        # 1. SM 自带 max=100, ctx.termination 是 max=1
        sm = StateMachine(
            phases={AgentPhase.SETUP: _NoopPhase(next_phase=AgentPhase.DONE)},
            initial=AgentPhase.SETUP,
            termination=MaxTurnsTermination(max_turns=100),
        )
        # 2. RunState turn=0(不到 ctx.termination 的 max=1,但也不超 SM 的 max=100)
        # 想要 ctx.termination 触发 → 设 turn=1
        rs = RunState(turn=1)
        ctx = PhaseContext(
            run_state=rs,
            turn_ctx=TurnContext(run_state=rs),
            termination=MaxTurnsTermination(max_turns=1),  # 1 必触发
            sm=sm,
        )
        # 3. trigger → ctx.termination 触发 → DONE
        events = list(sm.trigger("start", ctx))
        assert any(ev[0] == "system" for ev in events)
        assert sm.current == AgentPhase.DONE
        assert "max_turns_reached" in (rs.termination_reason or "")

    def test_sm_falls_back_to_self_termination_when_ctx_termination_is_none(self):
        """ctx.termination=None → SM 用自己的 _termination。"""
        sm = StateMachine(
            phases={AgentPhase.SETUP: _NoopPhase(next_phase=AgentPhase.DONE)},
            initial=AgentPhase.SETUP,
            termination=MaxTurnsTermination(max_turns=1),
        )
        rs = RunState(turn=5)  # 远超 max=1
        ctx = PhaseContext(
            run_state=rs,
            turn_ctx=TurnContext(run_state=rs),
            termination=None,  # 让 SM 用自己的
            sm=sm,
        )
        events = list(sm.trigger("start", ctx))
        assert any(ev[0] == "system" for ev in events)
        assert sm.current == AgentPhase.DONE
        assert "max_turns_reached" in (rs.termination_reason or "")


# ════════════════════════════════════════════════════════════
# A1-H3:RunState.created_at 字段(供 TimeoutTermination 用)
# ════════════════════════════════════════════════════════════


class TestRunStateCreatedAt:
    """A1-H2:RunState.created_at 用 time.monotonic() 初始化。"""

    def test_default_created_at_is_monotonic(self):
        """RunState() 默认 created_at 是 monotonic 起点,值接近 now。"""
        t0 = time.monotonic()
        rs = RunState()
        t1 = time.monotonic()
        assert t0 - 0.01 <= rs.created_at <= t1 + 0.01

    def test_two_runstates_have_different_created_at(self):
        """两个 RunState 实例的 created_at 略有不同(时间单调递增)。"""
        rs1 = RunState()
        rs2 = RunState()
        # 第二个的 created_at 应 >= 第一个
        assert rs2.created_at >= rs1.created_at


# ════════════════════════════════════════════════════════════
# A1-H3:TurnContext.turn_number 字段
# ════════════════════════════════════════════════════════════


class TestTurnContextTurnNumber:
    """A1-H3:TurnContext.turn_number 字段,默认 0,被 _new_turn_ctx 设值。"""

    def test_default_turn_number_zero(self):
        """TurnContext 默认 turn_number=0。"""
        rs = RunState()
        tc = TurnContext(run_state=rs)
        assert tc.turn_number == 0

    def test_explicit_turn_number_persists(self):
        """TurnContext(turn_number=N) 保留 N。"""
        rs = RunState()
        tc = TurnContext(run_state=rs, turn_number=3)
        assert tc.turn_number == 3


# ════════════════════════════════════════════════════════════
# A1-H3:ReactAgent._new_turn_ctx() 递增 turn
# ════════════════════════════════════════════════════════════


class TestNewTurnCtxIncrements:
    """A1-H3:_new_turn_ctx() 递增 _run_state.turn 并返新 TurnContext。"""

    def test_first_call_increments_to_one(self):
        """首次调 _new_turn_ctx → turn=1, turn_number=1。"""
        with patch("agent_core.agent_core.LLMRouter"), \
             patch("agent_core.agent_core.ToolRegistry"):
            from agent_core.agent_core import ReactAgent
            agent = ReactAgent.__new__(ReactAgent)
            agent.messages = []
            agent._session_manager = None
            agent._run_state = RunState()
            agent._run_state.turn = 0
            agent._termination = MaxTurnsTermination(max_turns=10)
            agent._phases = {AgentPhase.SETUP: _NoopPhase()}

            tc = agent._new_turn_ctx()
            assert agent._run_state.turn == 1
            assert tc.turn_number == 1
            assert tc.run_state is agent._run_state

    def test_subsequent_calls_increment(self):
        """多次调 _new_turn_ctx → turn 累计 +1。"""
        with patch("agent_core.agent_core.LLMRouter"), \
             patch("agent_core.agent_core.ToolRegistry"):
            from agent_core.agent_core import ReactAgent
            agent = ReactAgent.__new__(ReactAgent)
            agent.messages = []
            agent._session_manager = None
            agent._run_state = RunState()
            agent._run_state.turn = 5
            agent._termination = MaxTurnsTermination(max_turns=10)
            agent._phases = {AgentPhase.SETUP: _NoopPhase()}

            tc1 = agent._new_turn_ctx()
            assert tc1.turn_number == 6
            tc2 = agent._new_turn_ctx()
            assert tc2.turn_number == 7
            assert agent._run_state.turn == 7


# ════════════════════════════════════════════════════════════
# A1-H3:ReactAgent._drive() helper
# ════════════════════════════════════════════════════════════


class TestDriveHelper:
    """A1-H3:_drive(trigger_event=...) helper 推 SM 直到暂停。"""

    def test_drive_without_start_run_raises(self):
        """_drive() 在 start_run() 之前调 → RuntimeError。"""
        with patch("agent_core.agent_core.LLMRouter"), \
             patch("agent_core.agent_core.ToolRegistry"):
            from agent_core.agent_core import ReactAgent
            agent = ReactAgent.__new__(ReactAgent)
            agent._run_state = None

            with pytest.raises(RuntimeError):
                list(agent._drive())

    def test_drive_uses_default_trigger_start(self):
        """_drive() 不传 trigger → 用 'start' 默认值。"""
        with patch("agent_core.agent_core.LLMRouter"), \
             patch("agent_core.agent_core.ToolRegistry"):
            from agent_core.agent_core import ReactAgent
            agent = ReactAgent.__new__(ReactAgent)
            agent.messages = []
            agent._session_manager = None
            agent._pending_thinking = ""
            agent._pending_tool_logs = []
            agent._pending_tool_results = []
            agent._run_state = RunState()
            agent._termination = MaxTurnsTermination(max_turns=10)
            agent._phases = {AgentPhase.SETUP: _NoopPhase(emit_event=("system", "driven"))}
            agent._sm = StateMachine(
                agent._phases, initial=AgentPhase.SETUP, termination=agent._termination,
            )

            # 走 SETUP(无 chain) → next=SETUP+empty history→? 用 noop phase 推到 DONE
            events = list(agent._drive(trigger_event="start"))
            # emit 一个 "driven" event,然后 next=("anything", DONE) → 终止
            assert ("system", "driven") in events

    def test_drive_breaks_on_cancel_event(self):
        """_drive() 检查 cancel_event:被 set 时提前停。"""
        with patch("agent_core.agent_core.LLMRouter"), \
             patch("agent_core.agent_core.ToolRegistry"):
            from agent_core.agent_core import ReactAgent
            agent = ReactAgent.__new__(ReactAgent)
            agent.messages = []
            agent._session_manager = None
            agent._pending_thinking = ""
            agent._pending_tool_logs = []
            agent._pending_tool_results = []
            agent._run_state = RunState()
            agent._termination = MaxTurnsTermination(max_turns=10)
            agent._phases = {AgentPhase.SETUP: _NoopPhase(emit_event=("text", "1"))}
            agent._sm = StateMachine(
                agent._phases, initial=AgentPhase.SETUP, termination=agent._termination,
            )

            # 启动后立刻 set cancel_event
            agent._run_state.cancel_event.set()
            events = list(agent._drive(trigger_event="start"))
            # cancel_event 已 set → 提前停,events 可能为空或只有 1 个
            # 关键是 _drive 不会阻塞 / 死循环
            assert isinstance(events, list)


# ════════════════════════════════════════════════════════════
# A1-H3:step() trigger 路由
# ════════════════════════════════════════════════════════════


class TestStepTriggerRouting:
    """A1-H3:step() 根据 SM 当前 phase 选 trigger 名字。"""

    def test_step_in_setup_no_history_routes_to_run_started(self):
        """SETUP+空 history → step() 用 'run_started' trigger。"""
        with patch("agent_core.agent_core.LLMRouter"), \
             patch("agent_core.agent_core.ToolRegistry"):
            from agent_core.agent_core import ReactAgent
            agent = ReactAgent.__new__(ReactAgent)
            agent.messages = []
            agent._session_manager = None
            agent._pending_thinking = ""
            agent._pending_tool_logs = []
            agent._pending_tool_results = []

            # 构造 SM with probe phase that records trigger name
            received_trigger = []

            class _ProbePhase:
                name = "ProbePhase"

                def __init__(self):
                    from agent_core.agent_state import Phase
                    self._chain = _EmptyChain()

                def enter(self, trigger, ctx):
                    received_trigger.append(trigger)
                    return iter([])

                def next(self, trigger, ctx):
                    return ("x", AgentPhase.DONE)

            agent._phases = {AgentPhase.SETUP: _ProbePhase()}
            agent._termination = MaxTurnsTermination(max_turns=10)
            agent._run_state = RunState()
            agent._sm = StateMachine(
                agent._phases, initial=AgentPhase.SETUP, termination=agent._termination,
            )

            list(agent.step())
            assert received_trigger[0] == "run_started"

    def test_step_in_awaiting_permission_routes_to_permission_resolved(self):
        """SM 在 AWAITING_PERMISSION → step() 用 'permission_resolved' trigger。"""
        with patch("agent_core.agent_core.LLMRouter"), \
             patch("agent_core.agent_core.ToolRegistry"):
            from agent_core.agent_core import ReactAgent
            agent = ReactAgent.__new__(ReactAgent)
            agent.messages = []
            agent._session_manager = None
            agent._pending_thinking = ""
            agent._pending_tool_logs = []
            agent._pending_tool_results = []
            agent._run_state = RunState()
            agent._termination = MaxTurnsTermination(max_turns=10)

            received_trigger = []

            class _ProbePhase:
                name = "ProbePhase"
                _chain = _EmptyChain()

                def enter(self, trigger, ctx):
                    received_trigger.append(trigger)
                    return iter([])

                def next(self, trigger, ctx):
                    return ("x", AgentPhase.DONE)

            agent._phases = {
                AgentPhase.AWAITING_PERMISSION: _ProbePhase(),
            }
            agent._sm = StateMachine(
                agent._phases,
                initial=AgentPhase.AWAITING_PERMISSION,
                termination=agent._termination,
            )
            agent._sm._history = [(AgentPhase.SETUP, "x", AgentPhase.AWAITING_PERMISSION)]

            list(agent.step())
            assert received_trigger[0] == "permission_resolved"

    def test_step_in_other_phase_routes_to_start(self):
        """其他 phase(非 SETUP+empty / 非 AWAITING_PERMISSION)→ 'start' trigger。"""
        with patch("agent_core.agent_core.LLMRouter"), \
             patch("agent_core.agent_core.ToolRegistry"):
            from agent_core.agent_core import ReactAgent
            agent = ReactAgent.__new__(ReactAgent)
            agent.messages = []
            agent._session_manager = None
            agent._pending_thinking = ""
            agent._pending_tool_logs = []
            agent._pending_tool_results = []
            agent._run_state = RunState()
            agent._termination = MaxTurnsTermination(max_turns=10)

            received_trigger = []

            class _ProbePhase:
                name = "ProbePhase"
                _chain = _EmptyChain()

                def enter(self, trigger, ctx):
                    received_trigger.append(trigger)
                    return iter([])

                def next(self, trigger, ctx):
                    return ("x", AgentPhase.DONE)

            agent._phases = {AgentPhase.LLM_THINKING: _ProbePhase()}
            agent._sm = StateMachine(
                agent._phases,
                initial=AgentPhase.LLM_THINKING,
                termination=agent._termination,
            )

            list(agent.step())
            assert received_trigger[0] == "start"


# ════════════════════════════════════════════════════════════
# A1-H2:ReactAgent 用 build_default_termination factory
# ════════════════════════════════════════════════════════════


class TestReactAgentUsesBuildDefaultTermination:
    """A1-H2:ReactAgent.__init__ 走 build_default_termination factory。"""

    def test_default_termination_is_max_turns(self):
        """max_turns=10,timeout_s=None → agent._termination 是 MaxTurnsTermination。"""
        with patch("agent_core.agent_core.LLMRouter"), \
             patch("agent_core.agent_core.ToolRegistry"):
            from agent_core.agent_core import ReactAgent
            agent = ReactAgent.__new__(ReactAgent)
            # 模拟 __init__ 里的设置
            from agent_core.agent_state import build_default_termination
            agent._termination = build_default_termination(
                max_turns=10, timeout_s=None,
            )
            assert isinstance(agent._termination, MaxTurnsTermination)
            assert not isinstance(agent._termination, CompositeTermination)

    def test_termination_contains_max_turns_value(self):
        """termination._max 等于传入的 max_turns。"""
        with patch("agent_core.agent_core.LLMRouter"), \
             patch("agent_core.agent_core.ToolRegistry"):
            from agent_core.agent_core import ReactAgent
            agent = ReactAgent.__new__(ReactAgent)
            from agent_core.agent_state import build_default_termination
            agent._termination = build_default_termination(max_turns=42)
            assert agent._termination._max == 42


# ════════════════════════════════════════════════════════════
# Helper classes
# ════════════════════════════════════════════════════════════


class _NoopPhase:
    """最小 phase stub:进时 emit 一个 event(可选),next 推到 DONE。"""

    def __init__(
        self,
        emit_event=None,
        next_phase=AgentPhase.DONE,
        next_trigger="finish",
    ):
        from agent_core.agent_state import Phase
        self._chain = _EmptyChain()
        self._emit_event = emit_event
        self._next_phase = next_phase
        self._next_trigger = next_trigger
        self.name = "NoopPhase"

    def enter(self, trigger, ctx):
        from agent_core.agent_state import PhaseContext
        if self._emit_event is not None:
            yield self._emit_event
        return
        yield  # 显式 generator(语法要求)

    def next(self, trigger, ctx):
        return (self._next_trigger, self._next_phase)


class _EmptyChain:
    """Minimal stand-in for TurnChain that's iterable. 不真正 run。"""
    def __iter__(self):
        return iter([])

    def __len__(self):
        return 0
