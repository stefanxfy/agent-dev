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
    dual.extract_cursor = 0
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
        last_messages=[{"role": "user", "content": "记住我喜欢 Python"}],
        recent_turns=[],
    ))
    kinds = [e.kind for e in events]
    assert MemoryEventKind.BUDGET_EXCEEDED in kinds