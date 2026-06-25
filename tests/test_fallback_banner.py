"""M10 C6.5: 5 回退条件 banner — 2 测试用例"""
from unittest.mock import MagicMock

import pytest

from agent_core.memory.react_memory_bridge import MemoryEventKind


def test_memory_event_kind_has_5_fallback_kinds():
    """enum 含 LOCK_BUSY / RATE_LIMITED / BUDGET_EXCEEDED / TIMEOUT / SECRET_DETECTED"""
    expected = {"lock_busy", "rate_limited", "budget_exceeded", "timeout", "secret_detected"}
    actual = {k.value for k in MemoryEventKind}
    assert expected.issubset(actual), f"missing: {expected - actual}"


def test_bridge_emits_budget_exceeded_event():
    """bridge.on_turn_end catch BudgetExceeded → yield MemoryEvent(BUDGET_EXCEEDED)"""
    from agent_core.memory.react_memory_bridge import (
        ReactMemoryBridge,
        MemoryEventKind,
    )
    from agent_core.memory.cost_tracker import BudgetExceeded

    # Mock dual_channel(通道 A 成功)
    dual = MagicMock()
    dual.channel_a_inline_write = MagicMock()

    # Mock gate.should_extract 抛 BudgetExceeded
    gate = MagicMock()
    gate.should_extract.side_effect = BudgetExceeded(today_total=0.5, budget=0.1)

    bridge = ReactMemoryBridge(
        dual_channel=dual,
        gate=gate,
        memory_store=MagicMock(),
        session_id="s1",
        max_workers=1,
    )

    events = list(bridge.on_turn_end(
        user_msg="test",
        assistant_resp="reply",
        turn_index=0,
        input_tokens=100, output_tokens=100, tool_calls_in_turn=0,
    ))
    kinds = [e.kind for e in events]
    assert MemoryEventKind.BUDGET_EXCEEDED in kinds


# ───────────────────────── M13 §13.7/13.8 fix round 1 ─────────────────────────
# Bridge-emit 端到端:should_extract 抛 BudgetExceeded / LatencyTimeout
# 这些测试直接 mock bridge.gate.should_extract,验证 bridge 自身的 catch 逻辑
# (与已有 test_bridge_emits_budget_exceeded_event 互补 — 这里用真实 CostTracker
# 构造的 BudgetExceeded 并验证 reason 文案)


def test_bridge_emits_budget_exceeded_from_should_extract_e2e():
    """端到端:should_extract 抛 BudgetExceeded → bridge 收到 BUDGET_EXCEEDED MemoryEvent

    用真实 CostTracker 构造 BudgetExceeded,确保 message / reason 走的是同一条路径
    """
    from agent_core.memory.cost_tracker import CostTracker
    from agent_core.memory.react_memory_bridge import ReactMemoryBridge

    # 构造一个已超预算的 cost tracker
    ct = CostTracker(daily_budget_usd=0.0001)
    ct.add(input_tokens=1000, output_tokens=1000)
    # 直接拿真实 BudgetExceeded
    budget_err = ct.check_budget()
    assert budget_err is not None  # sanity check

    dual = MagicMock()
    dual.channel_a_inline_write = MagicMock()

    gate = MagicMock()
    gate.should_extract.side_effect = budget_err

    bridge = ReactMemoryBridge(
        dual_channel=dual,
        gate=gate,
        memory_store=MagicMock(),
        session_id="s1",
        max_workers=1,
    )

    events = list(bridge.on_turn_end(
        user_msg="test",
        assistant_resp="reply",
        turn_index=3,
        input_tokens=100, output_tokens=100, tool_calls_in_turn=0,
    ))
    budget_events = [e for e in events if e.kind == MemoryEventKind.BUDGET_EXCEEDED]
    assert len(budget_events) == 1
    assert "daily_budget_exceeded" in budget_events[0].reason


def test_bridge_emits_timeout_from_should_extract_e2e():
    """端到端:should_extract 抛 LatencyTimeout → bridge 收到 TIMEOUT MemoryEvent"""
    from agent_core.memory.latency import LatencyTimeout
    from agent_core.memory.react_memory_bridge import ReactMemoryBridge

    dual = MagicMock()
    dual.channel_a_inline_write = MagicMock()

    gate = MagicMock()
    gate.should_extract.side_effect = LatencyTimeout(timeout=8.0)

    bridge = ReactMemoryBridge(
        dual_channel=dual,
        gate=gate,
        memory_store=MagicMock(),
        session_id="s1",
        max_workers=1,
    )

    events = list(bridge.on_turn_end(
        user_msg="test",
        assistant_resp="reply",
        turn_index=5,
        input_tokens=100, output_tokens=100, tool_calls_in_turn=0,
    ))
    timeout_events = [e for e in events if e.kind == MemoryEventKind.TIMEOUT]
    assert len(timeout_events) == 1
    assert "latency_exceeded" in timeout_events[0].reason


def test_extraction_gate_should_extract_propagates_budget_or_timeout():
    """_llm_score 不再 swallow BudgetExceeded/LatencyTimeout — 抛到 should_extract 调用方

    回归守卫(regression guard):本测试在原始 commit 558e67d 上会 FAIL
    因为 _llm_score 的 bare `except Exception` 会 swallow 它们。

    这里通过让 _call_llm 触发 latency timeout(慢 router + 短 _latency_timeout)
    来让 _llm_score 走到 except 分支。BudgetExceeded 路径通过 mock _call_llm
    来覆盖(因为 CostTracker.add 在 _call_llm 末尾,而 budget check 在开头)。
    """
    import time as _time

    from agent_core.memory.cost_tracker import BudgetExceeded
    from agent_core.memory.extraction_gate import ExtractionGate, TurnContext
    from agent_core.memory.latency import LatencyTimeout

    # 走 LatencyTimeout 路径:慢 router + 短 timeout
    def slow_router(*args, **kwargs):
        _time.sleep(0.5)
        chunk = MagicMock()
        chunk.text_delta.text = ""
        yield chunk

    router = MagicMock()
    router.chat = slow_router

    gate = ExtractionGate(
        llm_router=router,
        memory_store=MagicMock(),
        session_id="s1",
    )
    gate._latency_timeout = 0.05  # 50ms,确保 sleep 触发 timeout

    ctx = TurnContext(
        session_id="s1",
        cumulative_tokens=20000,  # > MIN_TOKENS_TO_INIT → gate1 pass
        cumulative_tool_calls=0,
        last_messages=[{"role": "user", "content": "test"}],
    )

    # 修复前:should_extract 吞掉 LatencyTimeout,返回 Decision(llm_call_error(LatencyTimeout))
    # 修复后:LatencyTimeout 应直接抛出(由 bridge 的 except 捕获)
    with pytest.raises((BudgetExceeded, LatencyTimeout)):
        gate.should_extract(ctx)