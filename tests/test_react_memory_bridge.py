import tempfile
from pathlib import Path
import shutil
from unittest.mock import MagicMock

from agent_core.memory.react_memory_bridge import (
    ReactMemoryBridge,
    MemoryEvent,
    MemoryEventKind,
)
from agent_core.memory.dual_channel_writer import DualChannelWriter
from agent_core.memory.extraction_gate import ExtractionGate, TurnContext
from agent_core.memory.memory_store import MemoryStore
from agent_core.memory.meta_db import MetaDB


def test_on_turn_end_high_confidence_writes():
    tmp = Path(tempfile.mkdtemp(prefix="bridge_test_"))
    try:
        meta = MetaDB(":memory:")
        store = MemoryStore(tmp)
        embed = MagicMock(); embed.encode.return_value = [0.1] * 4
        vec = MagicMock()
        dual = DualChannelWriter(
            session_id="s1", meta_db=meta,
            memory_store=store, vector_store=vec, embed_fn=embed,
        )

        # mock LLM 返 high confidence
        def fake_chat(messages, **kw):
            chunk = MagicMock()
            chunk.text_delta.text = '''{
              "should_extract": true,
              "confidence": 0.85,
              "reason": "ok",
              "candidates": [
                {"type": "user", "title": "姓名", "body": "张三",
                 "source_quote": "我叫张三"}
              ]
            }'''
            yield chunk
        router = MagicMock()
        router.chat = fake_chat
        router.config.provider = "mock"

        gate = ExtractionGate(llm_router=router, memory_store=store, session_id="s1")
        bridge = ReactMemoryBridge(
            dual_channel=dual, gate=gate, memory_store=store,
            session_id="s1", max_workers=1,
        )

        events = list(bridge.on_turn_end(
            user_msg="记住我叫张三",
            assistant_resp="好的张三",
            turn_index=0,
            input_tokens=100, output_tokens=100, tool_calls_in_turn=0,
            last_messages=[{"role": "user", "content": "记住我叫张三"}],
            recent_turns=[],
        ))

        kinds = [e.kind for e in events]
        assert MemoryEventKind.CHANNEL_A_OK in kinds
        # 门3 过 → gate_pass + extract_dispatched(异步等不到 done)
        assert any(k in kinds for k in (
            MemoryEventKind.GATE_PASS, MemoryEventKind.EXTRACT_DISPATCHED,
        ))
    finally:
        bridge.shutdown(timeout=5)
        dual.shutdown(timeout=5)
        shutil.rmtree(tmp)


def test_on_turn_end_below_threshold_skips():
    tmp = Path(tempfile.mkdtemp(prefix="bridge_test_"))
    try:
        meta = MetaDB(":memory:")
        store = MemoryStore(tmp)
        embed = MagicMock(); embed.encode.return_value = [0.1] * 4
        vec = MagicMock()
        dual = DualChannelWriter(
            session_id="s2", meta_db=meta,
            memory_store=store, vector_store=vec, embed_fn=embed,
        )
        router = MagicMock()  # 不会调
        gate = ExtractionGate(llm_router=router, memory_store=store, session_id="s2")
        bridge = ReactMemoryBridge(
            dual_channel=dual, gate=gate, memory_store=store,
            session_id="s2", max_workers=1,
        )

        events = list(bridge.on_turn_end(
            user_msg="今天天气不错",
            assistant_resp="是的",
            turn_index=0,
            input_tokens=100, output_tokens=100, tool_calls_in_turn=0,
            last_messages=[{"role": "user", "content": "今天天气不错"}],
            recent_turns=[],
        ))

        kinds = [e.kind for e in events]
        assert MemoryEventKind.CHANNEL_A_OK in kinds
        assert MemoryEventKind.GATE_SKIP in kinds
    finally:
        bridge.shutdown(timeout=5)
        dual.shutdown(timeout=5)
        shutil.rmtree(tmp)