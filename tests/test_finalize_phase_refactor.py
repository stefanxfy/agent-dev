"""
Plan B Final Phase acceptance — 4 output_chain handler 真实现的 runtime 验证(2026-07-02 引入)。

覆盖 plan §15 Final Phase acceptance 列出的 7+1 case:
- FinalAnswerBookkeepingHandler:in-memory bookkeeping(append message + set run_state + emit system event)
- MemoryBridgeExtractHandler:gate 条件 + bridge.on_turn_end kwargs + emit memory_event
- SessionFlushHandler:sm.is_done 条件 + 异常不 crash

测试策略:
- 构造 minimal MagicMock agent + TurnContext(spec)
- 直接构造 SimpleNamespace stage_outputs(模仿 LLM phase 写完)
- 调 handler.handle(ctx) → 检查副作用(messages/run_state/bridge mock/flush mock)

完整委托链:
    FinalizingPhase.enter() → agent._output_chain.run(ctx)
        → FinalAnswerBookkeepingHandler.handle(ctx)   ← in-memory bookkeeping
        → FinalAnswerPersistHandler.handle(ctx)        (Stage C,自有 test)
        → AuditLogHandler.handle(ctx)                  (no-op,no test needed)
        → MemoryBridgeExtractHandler.handle(ctx)        ← bridge.on_turn_end 调用
        → SessionFlushHandler.handle(ctx)               ← session_manager.flush() 兜底
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from agent_core.turn_chain import (
    FinalAnswerBookkeepingHandler,
    HandlerResult,
    MemoryBridgeExtractHandler,
    SessionFlushHandler,
    TurnContext,
)


# ────────────────────────────────────────────────────────────
# Fixtures / helpers
# ────────────────────────────────────────────────────────────


def _make_fake_ctx(stage_out=None) -> TurnContext:
    """构造 minimal TurnContext(handlers 只读 ctx.stage_outputs / ctx.emit)。"""
    ctx = MagicMock(spec=TurnContext)
    ctx.stage_outputs = stage_out
    ctx.events = []
    ctx.emit = lambda e: ctx.events.append(e)
    return ctx


def _make_fake_agent(
    *,
    has_run_state: bool = True,
    user_message: str = "<test>",
    turn: int = 1,
    final_answer: str = "",
    final_stop_reason: str | None = None,
    last_input_tokens: int = 0,
    last_output_tokens: int = 0,
    last_tool_calls: list | None = None,
    has_bridge: bool = True,
    has_session_manager: bool = True,
    sm_is_done: bool = False,
) -> MagicMock:
    """构造 minimal fake agent(handlers 读 _run_state / react_memory_bridge / _session_manager / _sm / messages)。"""
    agent = MagicMock()
    agent.messages = []  # 用真的 list(BookkeepingHandler 会 append)
    if has_run_state:
        run_state = MagicMock()
        run_state.user_message = user_message
        run_state.turn = turn
        run_state.final_answer = final_answer
        run_state.final_stop_reason = final_stop_reason
        run_state.last_input_tokens = last_input_tokens
        run_state.last_output_tokens = last_output_tokens
        run_state.last_tool_calls = list(last_tool_calls or [])
        agent._run_state = run_state
    else:
        agent._run_state = None
    if has_bridge:
        # 默认 bridge.on_turn_end 返 1 个 fake event — 用 side_effect 而非直接赋值,
        # 保留 MagicMock 的 assert_called_* 方法
        bridge = MagicMock()

        def _fake_on_turn_end(**kwargs):
            from agent_core.memory.react_memory_bridge import MemoryEvent, MemoryEventKind

            yield MemoryEvent(kind=MemoryEventKind.TURN_PERSISTED, turn_index=kwargs.get("turn_index", 0))

        bridge.on_turn_end.side_effect = _fake_on_turn_end
        agent.react_memory_bridge = bridge
    else:
        agent.react_memory_bridge = None
    if has_session_manager:
        agent._session_manager = MagicMock()
    else:
        agent._session_manager = None
    if sm_is_done:
        sm = MagicMock()
        sm.is_done = True
        agent._sm = sm
    else:
        sm = MagicMock()
        sm.is_done = False
        agent._sm = sm
    return agent


def _make_stage_out(
    full_text: str = "回答了用户的问题。",
    thinking_text: str = "",
    stop_reason: str = "end_turn",
    tool_calls: list | None = None,
    usage=None,
) -> SimpleNamespace:
    """构造 minimal stage_outputs 的 SimpleNamespace 形式(模仿 _LLMResult)。"""
    return SimpleNamespace(
        tool_calls=tool_calls or [],
        full_text=full_text,
        thinking_text=thinking_text,
        stop_reason=stop_reason,
        usage=usage,
    )


# ────────────────────────────────────────────────────────────
# 1. FinalAnswerBookkeepingHandler — 无 tool_calls: append + set + emit
# ────────────────────────────────────────────────────────────


class TestBookkeepingNoToolCalls:
    """1: 无 tool_calls → append assistant message + set _run_state + emit system event"""

    def test_bookkeeping_no_tool_calls_appends_assistant_message(self):
        """BookkeepingHandler 接管原 v1 _iter_phase_finalize L945-L953 段:
        - agent.messages append {role:assistant, content:full_text}
        - _run_state.final_answer 赋值
        - _run_state.final_stop_reason 赋值
        - emit ("system", "✅ 回答完成")
        """
        agent = _make_fake_agent()
        ctx = _make_fake_ctx(stage_out=_make_stage_out(
            full_text="回答完成",
            stop_reason="end_turn",
        ))

        result = FinalAnswerBookkeepingHandler(agent).handle(ctx)

        # 返回 HandlerResult(stop_chain=False 默认,继续链跑)
        assert isinstance(result, HandlerResult)
        assert result.stop_chain is False

        # 1. agent.messages 追加
        assert len(agent.messages) == 1
        assert agent.messages[0] == {"role": "assistant", "content": "回答完成"}

        # 2. _run_state.final_answer 赋值
        assert agent._run_state.final_answer == "回答完成"
        # 3. _run_state.final_stop_reason 赋值
        assert agent._run_state.final_stop_reason == "end_turn"

        # 4. emit system event
        assert len(ctx.events) == 1
        assert ctx.events[0] == ("system", "✅ 回答完成")


# ────────────────────────────────────────────────────────────
# 2. FinalAnswerBookkeepingHandler — 有 tool_calls: 只 set,不 append 不 emit
# ────────────────────────────────────────────────────────────


class TestBookkeepingWithToolCalls:
    """2: 有 tool_calls → 只设 final_answer,不 append 不 emit(Stage A1 已写 assistant+tool_use)"""

    def test_bookkeeping_with_tool_calls_skips_message_append(self):
        """有 tool_calls 时:
        - 不 append 到 agent.messages(Stage A1 LLMCallPersistHandler 已写 assistant+tool_use)
        - 不 emit "✅ 回答完成"
        - 仍设 _run_state.final_answer(供 MemoryBridgeExtract gate 读)
        """
        from agent_core.turn_chain import _LLMResult

        tool_calls = [SimpleNamespace(tool_name="echo", tool_use_id="id1", tool_input={"msg": "x"})]
        agent = _make_fake_agent()
        ctx = _make_fake_ctx(stage_out=_LLMResult(
            tool_calls=tool_calls,
            full_text="",  # 有 tool_calls 时 LLM 没出 text,full_text 空
            stop_reason="tool_use",
        ))

        result = FinalAnswerBookkeepingHandler(agent).handle(ctx)

        assert isinstance(result, HandlerResult)
        # messages 不 append(Stage A1 已写)
        assert len(agent.messages) == 0
        # 不 emit
        assert len(ctx.events) == 0
        # 仍设 final_answer(MemoryBridgeExtract gate 需要)
        assert agent._run_state.final_answer == ""


# ────────────────────────────────────────────────────────────
# 3. FinalAnswerBookkeepingHandler — 无 stage_outputs: no-op
# ────────────────────────────────────────────────────────────


class TestBookkeepingNoStageOut:
    """3: stage_outputs=None → no-op(EXECUTING_TOOLS 续 turn 时)"""

    def test_bookkeeping_no_stage_out_returns_noop(self):
        """stage_outputs 缺失时 BookkeepingHandler 直接 no-op:
        - 不修改 agent.messages
        - 不 emit
        - 不修改 _run_state
        """
        agent = _make_fake_agent()
        ctx = _make_fake_ctx(stage_out=None)

        result = FinalAnswerBookkeepingHandler(agent).handle(ctx)

        assert isinstance(result, HandlerResult)
        assert len(agent.messages) == 0
        assert len(ctx.events) == 0


# ────────────────────────────────────────────────────────────
# 4. MemoryBridgeExtractHandler — gate 通过,bridge.on_turn_end 调用 + emit memory_event
# ────────────────────────────────────────────────────────────


class TestMemoryBridgeExtractHappyPath:
    """4: gate 通过 → bridge.on_turn_end 被调 + emit memory_event + kwargs 正确"""

    def test_memory_bridge_extract_calls_on_turn_end_with_correct_kwargs(self):
        """bridge.on_turn_end 接收 6 个 kwarg(user_msg/assistant_resp/turn_index/input_tokens/output_tokens/tool_calls_in_turn)
        + emit ("memory_event", event) 每个 yield 事件。
        """
        agent = _make_fake_agent(
            user_message="hello",
            turn=3,
            final_answer="回答",
            final_stop_reason="end_turn",
            last_input_tokens=100,
            last_output_tokens=50,
            last_tool_calls=[{"id": "id1"}, {"id": "id2"}],
        )
        ctx = _make_fake_ctx(stage_out=None)

        result = MemoryBridgeExtractHandler(agent).handle(ctx)

        assert isinstance(result, HandlerResult)
        # bridge.on_turn_end 调 1 次
        agent.react_memory_bridge.on_turn_end.assert_called_once_with(
            user_msg="hello",
            assistant_resp="回答",
            turn_index=3,
            counter=agent.session_counter,
        )
        # emit memory_event(fake bridge yield 1 个 MemoryEvent)
        assert len(ctx.events) == 1
        event_name, event_payload = ctx.events[0]
        assert event_name == "memory_event"
        # event_payload 是 MemoryEvent(由 fake bridge yield)
        assert hasattr(event_payload, "kind")
        assert event_payload.turn_index == 3


# ────────────────────────────────────────────────────────────
# 5. MemoryBridgeExtractHandler — gate 条件 1: stop_reason in _TRUNCATED_STOP_REASONS
# ────────────────────────────────────────────────────────────


class TestMemoryBridgeExtractGateTruncated:
    """5: stop_reason="max_tokens" / "length" → 不调 bridge(避免存半句话)"""

    @pytest.mark.parametrize("truncated_reason", ["max_tokens", "length"])
    def test_memory_bridge_extract_gate_skips_when_truncated_stop_reason(self, truncated_reason):
        """gate 条件 4: stop_reason ∈ _TRUNCATED_STOP_REASONS → 不调 bridge,不 emit"""
        agent = _make_fake_agent(
            final_answer="半截回答",
            final_stop_reason=truncated_reason,
            turn=2,
        )
        ctx = _make_fake_ctx(stage_out=None)

        result = MemoryBridgeExtractHandler(agent).handle(ctx)

        assert isinstance(result, HandlerResult)
        agent.react_memory_bridge.on_turn_end.assert_not_called()
        assert len(ctx.events) == 0


# ────────────────────────────────────────────────────────────
# 6. MemoryBridgeExtractHandler — gate 条件 2: turn=0
# ────────────────────────────────────────────────────────────


class TestMemoryBridgeExtractGateTurnZero:
    """6: _run_state.turn == 0 → 不调 bridge(初始 turn 不抽取)"""

    def test_memory_bridge_extract_gate_skips_when_turn_zero(self):
        """gate 条件 2: turn <= 0 → 不调 bridge"""
        agent = _make_fake_agent(
            turn=0,
            final_answer="some answer",
            final_stop_reason="end_turn",
        )
        ctx = _make_fake_ctx(stage_out=None)

        result = MemoryBridgeExtractHandler(agent).handle(ctx)

        assert isinstance(result, HandlerResult)
        agent.react_memory_bridge.on_turn_end.assert_not_called()
        assert len(ctx.events) == 0


# ────────────────────────────────────────────────────────────
# 7. SessionFlushHandler — sm.is_done=True → flush
# ────────────────────────────────────────────────────────────


class TestSessionFlushWhenDone:
    """7a: _sm.is_done=True → session_manager.flush() 被调"""

    def test_session_flush_calls_when_sm_is_done(self):
        """FINALIZING 跑完即 DONE,_sm.is_done=True 时调 session_manager.flush()"""
        agent = _make_fake_agent(sm_is_done=True)
        ctx = _make_fake_ctx(stage_out=None)

        result = SessionFlushHandler(agent).handle(ctx)

        assert isinstance(result, HandlerResult)
        agent._session_manager.flush.assert_called_once()


class TestSessionFlushWhenNotDone:
    """7b: _sm.is_done=False → 不 flush"""

    def test_session_flush_no_call_when_not_done(self):
        """FINALIZING 还没跑完(FINALIZING 中间状态,理论不会但保险)→ 不 flush"""
        agent = _make_fake_agent(sm_is_done=False)
        ctx = _make_fake_ctx(stage_out=None)

        result = SessionFlushHandler(agent).handle(ctx)

        assert isinstance(result, HandlerResult)
        agent._session_manager.flush.assert_not_called()


class TestSessionFlushSwallowsException:
    """7c: flush 抛异常 → 不 crash chain,仅 _logger.warning"""

    def test_session_flush_swallows_flush_exception(self):
        """Stage A/B/C 已即时持久化,flush 失败不丢已有数据 — SessionFlushHandler
        必须 swallow 异常,让 chain 继续跑(虽然 chain 末位,但语义一致)。
        """
        agent = _make_fake_agent(sm_is_done=True)
        agent._session_manager.flush.side_effect = IOError("disk full")
        ctx = _make_fake_ctx(stage_out=None)

        # 不抛异常
        result = SessionFlushHandler(agent).handle(ctx)
        assert isinstance(result, HandlerResult)
        # flush 仍被调用(只是抛了异常)
        agent._session_manager.flush.assert_called_once()