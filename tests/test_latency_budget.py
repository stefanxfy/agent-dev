"""M10 C6.3: latency timeout — 3 测试用例"""
from unittest.mock import MagicMock

import pytest

from agent_core.memory.latency import LatencyTimeout


def test_extraction_gate_raises_latency_timeout_on_slow_llm():
    """LLM call 超时 → gate._call_llm 抛 LatencyTimeout"""
    from agent_core.memory.extraction_gate import ExtractionGate

    def slow_router(*args, **kwargs):
        import time
        time.sleep(0.5)
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
    gate._latency_timeout = 0.1  # 100ms,比 sleep 短
    with pytest.raises(LatencyTimeout):
        gate._call_llm("test")


def test_extraction_gate_returns_text_when_llm_fast():
    """LLM 快 → 返回 text,无异常"""
    from agent_core.memory.extraction_gate import ExtractionGate

    def fast_router(*args, **kwargs):
        chunk = MagicMock()
        chunk.text_delta.text = "hello"
        yield chunk

    router = MagicMock()
    router.chat = fast_router

    gate = ExtractionGate(
        llm_router=router,
        memory_store=MagicMock(),
        session_id="s1",
    )
    gate._latency_timeout = 5.0
    text = gate._call_llm("test")
    assert "hello" in text


def test_bridge_emits_timeout_event_on_latency_timeout():
    """bridge.on_turn_end catch LatencyTimeout → yield MemoryEvent(TIMEOUT)"""
    from agent_core.memory.react_memory_bridge import (
        ReactMemoryBridge,
        MemoryEventKind,
    )
    from agent_core.memory.latency import LatencyTimeout

    # Mock dual_channel(persist_turn 成功)
    dual = MagicMock()
    dual.persist_turn = MagicMock()  # 不抛异常
    dual.extract_candidates = MagicMock()

    # Mock gate.should_extract 抛 LatencyTimeout
    gate = MagicMock()
    gate.should_extract.side_effect = LatencyTimeout(timeout=30.0)

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
    assert MemoryEventKind.TIMEOUT in kinds