"""
Plan B Final Phase Step 2 — L3SMExtractTriggerHandler 真实现的 runtime 验证(2026-07-02 引入)。

覆盖 plan §15 Step 8 列出的 7 case:
- Gate 条件 4 prongs:session_memory/turn/final_answer/stop_reason
- Dual-gate (sm_layer.should_extract_now):True 路径 spawn future + capture;False 路径 skip
- 异常处理:extract_incremental 抛异常被 swallow,不 crash chain

测试策略:
- 构造 minimal MagicMock agent + RunState + TurnContext(spec)
- 调 handler.handle(ctx) → 检查副作用(sm_layer mock call + run_state.pending_sm_extract_future)
- 复用 test_finalize_phase_refactor.py 的 helper pattern

完整委托链:
    FinalizingPhase.enter() → agent._output_chain.run(ctx)
        → FinalAnswerBookkeepingHandler.handle(ctx)
        → FinalAnswerPersistHandler.handle(ctx)        (Stage C)
        → AuditLogHandler.handle(ctx)                  (no-op)
        → MemoryBridgeExtractHandler.handle(ctx)
        → L3SMExtractTriggerHandler.handle(ctx)         ← L3 SM extract trigger (本测试)
        → SessionFlushHandler.handle(ctx)
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from agent_core.agent_state import RunState
from agent_core.turn_chain import (
    HandlerResult,
    L3SMExtractTriggerHandler,
    TurnContext,
)


# ────────────────────────────────────────────────────────────
# Fixtures / helpers
# ────────────────────────────────────────────────────────────


def _make_fake_ctx() -> TurnContext:
    """构造 minimal TurnContext(handler 只读 ctx.stage_outputs / ctx.emit)。"""
    ctx = MagicMock(spec=TurnContext)
    ctx.stage_outputs = None  # L3SMExtractTriggerHandler 不读 stage_outputs
    ctx.events = []
    ctx.emit = lambda e: ctx.events.append(e)
    return ctx


def _make_fake_agent(
    *,
    has_run_state: bool = True,
    has_session_memory: bool = True,
    turn: int = 1,
    final_answer: str = "完整回答",
    final_stop_reason: str | None = "end_turn",
    last_input_tokens: int = 100,
    last_output_tokens: int = 50,
    last_tool_calls: list | None = None,
    sm_should_extract_now: bool = True,
    extract_future: str = "fake_future",
    extract_raises: bool = False,
) -> MagicMock:
    """构造 minimal fake agent(L3SMExtractTriggerHandler 读 session_memory / _run_state)。

    Returns:
        MagicMock agent with:
        - session_memory: MagicMock(可配 should_extract_now / extract_incremental)
        - _run_state: RunState 实例(可配 turn/final_answer/final_stop_reason/last_*)
        - _messages_with_ids(): 返回 [user_msg](可被 mock override)
    """
    agent = MagicMock()

    # session_memory 配置
    if has_session_memory:
        sm = MagicMock()
        sm.should_extract_now.return_value = sm_should_extract_now
        if extract_raises:
            sm.extract_incremental.side_effect = RuntimeError("simulated extract failure")
        else:
            sm.extract_incremental.return_value = extract_future
        agent.session_memory = sm
    else:
        agent.session_memory = None

    # _run_state 配置
    if has_run_state:
        run_state = RunState(
            user_message="test_user_input",
            turn=turn,
            final_answer=final_answer,
            final_stop_reason=final_stop_reason,
            last_input_tokens=last_input_tokens,
            last_output_tokens=last_output_tokens,
            last_tool_calls=list(last_tool_calls or []),
        )
        agent._run_state = run_state
    else:
        agent._run_state = None

    # messages 真 list(Plan C 2026-07-02:handler 调模块级 messages_with_ids(agent.messages),
    # 不再调 agent._messages_with_ids())。给真 list 让模块函数真跑(注入 stable id)。
    agent.messages = [
        {"role": "user", "content": "test_user_input"},
    ]

    return agent


# ────────────────────────────────────────────────────────────
# 1. Gate 通过 + dual-gate True → spawn future + capture to RunState
# ────────────────────────────────────────────────────────────


class TestGateTrueSpawnsFuture:
    """1: 4-prong gate 全过 + dual-gate True → extract_incremental 调 1 次 +
    future 写到 run_state.pending_sm_extract_future"""

    def test_gate_true_spawns_future_and_captures(self):
        """happy path:
        - session_memory 有 + turn=1 > 0 + final_answer 存在 + stop_reason="end_turn"(非 _TRUNCATED_STOP_REASONS)
        - should_extract_now=True → extract_incremental 调 1 次
        - run_state.pending_sm_extract_future 被设为返回的 future
        """
        agent = _make_fake_agent(
            turn=3,
            final_answer="完整回答",
            final_stop_reason="end_turn",
            last_input_tokens=1000,
            last_output_tokens=500,
            last_tool_calls=[{"id": "t1"}, {"id": "t2"}],
            sm_should_extract_now=True,
            extract_future="fake_future_value",
        )
        ctx = _make_fake_ctx()

        result = L3SMExtractTriggerHandler(agent).handle(ctx)

        # 返回 HandlerResult(stop_chain=False)
        assert isinstance(result, HandlerResult)
        assert result.stop_chain is False

        # dual-gate 调 1 次,参数对齐 v1 L1791-L1795
        agent.session_memory.should_extract_now.assert_called_once_with(
            current_token_count=1500,
            tool_count_delta=2,
            tool_count_last_turn=2,
        )
        # extract_incremental 调 1 次,参数对齐 v1 L1808-L1814
        agent.session_memory.extract_incremental.assert_called_once()
        kwargs = agent.session_memory.extract_incremental.call_args.kwargs
        assert kwargs["llm_callback"] is None
        assert kwargs["current_token_count"] == 1500
        assert kwargs["tool_count_delta"] == 2
        assert kwargs["tool_count_last_turn"] == 2

        # future 写到 run_state.pending_sm_extract_future
        assert agent._run_state.pending_sm_extract_future == "fake_future_value"


# ────────────────────────────────────────────────────────────
# 2. Dual-gate False → skip extract, future 不写
# ────────────────────────────────────────────────────────────


class TestDualGateFalseSkips:
    """2: should_extract_now=False(节流)→ extract_incremental 不调,
    future 不写(对齐 v1 L1796-L1800)"""

    def test_dual_gate_false_skips_extract(self):
        """dual-gate 拦住(token Δ < 5K 或 tool Δ < 3)→ 完全跳过 extract"""
        agent = _make_fake_agent(
            turn=2,
            final_answer="回答",
            sm_should_extract_now=False,  # 节流
        )
        ctx = _make_fake_ctx()

        result = L3SMExtractTriggerHandler(agent).handle(ctx)

        assert isinstance(result, HandlerResult)
        # should_extract_now 仍被调(M11.7 节流判断)
        agent.session_memory.should_extract_now.assert_called_once()
        # extract_incremental 不调
        agent.session_memory.extract_incremental.assert_not_called()
        # future 不写
        assert agent._run_state.pending_sm_extract_future is None


# ────────────────────────────────────────────────────────────
# 3. Gate 条件 4: stop_reason ∈ _TRUNCATED_STOP_REASONS → no-op
# ────────────────────────────────────────────────────────────


class TestGateTruncatedStopReason:
    """3: final_stop_reason="max_tokens"/"length" → handler no-op(避免存半句话)"""

    @pytest.mark.parametrize("truncated_reason", ["max_tokens", "length"])
    def test_gate_closed_when_truncated_stop_reason(self, truncated_reason):
        """gate 条件 4: stop_reason ∈ _TRUNCATED_STOP_REASONS → 不调 sm"""
        agent = _make_fake_agent(
            turn=2,
            final_answer="半截回答",
            final_stop_reason=truncated_reason,
        )
        ctx = _make_fake_ctx()

        result = L3SMExtractTriggerHandler(agent).handle(ctx)

        assert isinstance(result, HandlerResult)
        # sm 完全不调
        agent.session_memory.should_extract_now.assert_not_called()
        agent.session_memory.extract_incremental.assert_not_called()
        # future 不写
        assert agent._run_state.pending_sm_extract_future is None


# ────────────────────────────────────────────────────────────
# 4. Gate 条件 3: final_answer 为空 → no-op
# ────────────────────────────────────────────────────────────


class TestGateNoFinalAnswer:
    """4: final_answer="" → handler no-op(没拿到完整回答就不抽)"""

    def test_gate_closed_when_no_final_answer(self):
        """gate 条件 3: final_answer 空 → 不调 sm"""
        agent = _make_fake_agent(
            turn=2,
            final_answer="",  # 没拿到完整回答
            final_stop_reason="end_turn",
        )
        ctx = _make_fake_ctx()

        result = L3SMExtractTriggerHandler(agent).handle(ctx)

        assert isinstance(result, HandlerResult)
        agent.session_memory.should_extract_now.assert_not_called()
        agent.session_memory.extract_incremental.assert_not_called()
        assert agent._run_state.pending_sm_extract_future is None


# ────────────────────────────────────────────────────────────
# 5. Gate 条件 2: turn=0 → no-op
# ────────────────────────────────────────────────────────────


class TestGateTurnZero:
    """5: _run_state.turn=0 → handler no-op(初始 turn 不抽)"""

    def test_gate_closed_when_turn_zero(self):
        """gate 条件 2: turn <= 0 → 不调 sm"""
        agent = _make_fake_agent(
            turn=0,
            final_answer="some answer",
            final_stop_reason="end_turn",
        )
        ctx = _make_fake_ctx()

        result = L3SMExtractTriggerHandler(agent).handle(ctx)

        assert isinstance(result, HandlerResult)
        agent.session_memory.should_extract_now.assert_not_called()
        agent.session_memory.extract_incremental.assert_not_called()
        assert agent._run_state.pending_sm_extract_future is None


# ────────────────────────────────────────────────────────────
# 6. session_memory 缺失 → no-op
# ────────────────────────────────────────────────────────────


class TestNoSessionMemory:
    """6: agent.session_memory=None → handler no-op(向后兼容老 caller)"""

    def test_no_session_memory_no_op(self):
        """session_memory 缺失(老 v1 path 调方)→ handler 不抛异常,直接 no-op"""
        agent = _make_fake_agent(has_session_memory=False)
        ctx = _make_fake_ctx()

        # 不抛异常
        result = L3SMExtractTriggerHandler(agent).handle(ctx)

        assert isinstance(result, HandlerResult)
        # run_state.pending_sm_extract_future 仍是 None
        assert agent._run_state.pending_sm_extract_future is None


# ────────────────────────────────────────────────────────────
# 7. extract_incremental 抛异常 → swallowed, 不 propagate
# ────────────────────────────────────────────────────────────


class TestExceptionSwallowed:
    """7: extract_incremental 抛异常(网络/IO 失败)→ handler swallow + _logger.warning,
    不 crash chain — SessionFlushHandler 继续跑兜底 flush"""

    def test_extract_incremental_exception_swallowed(self):
        """extract_incremental 抛 RuntimeError → handler 返回 HandlerResult, 不 propagate"""
        agent = _make_fake_agent(
            turn=2,
            final_answer="回答",
            final_stop_reason="end_turn",
            extract_raises=True,  # extract_incremental 抛 RuntimeError
        )
        ctx = _make_fake_ctx()

        # 不抛异常(被 handler 内部 try/except 吞掉)
        result = L3SMExtractTriggerHandler(agent).handle(ctx)

        assert isinstance(result, HandlerResult)
        # extract_incremental 调了,但抛了
        agent.session_memory.extract_incremental.assert_called_once()
        # future 不写(因为 extract 抛了)
        assert agent._run_state.pending_sm_extract_future is None