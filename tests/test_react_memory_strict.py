"""
ReAct 严格双通道端到端集成测试
参考 spec §8.2
"""
import tempfile
from pathlib import Path
import shutil
from unittest.mock import MagicMock

from agent_core.memory.react_memory_bridge import (
    ReactMemoryBridge,
    MemoryEventKind,
)
from agent_core.memory.dual_channel_writer import DualChannelWriter
from agent_core.memory.extraction_gate import ExtractionGate
from agent_core.memory.memory_store import MemoryStore
from agent_core.memory.meta_db import MetaDB


def _make_bridge(llm_json_response: str, session_id: str = "s1"):
    """helper:构造完整组件栈"""
    tmp = Path(tempfile.mkdtemp(prefix="e2e_"))
    meta = MetaDB(":memory:")
    store = MemoryStore(tmp)
    embed = MagicMock()
    embed.encode.return_value = [0.1] * 4
    vec = MagicMock()
    dual = DualChannelWriter(
        session_id=session_id, meta_db=meta,
        memory_store=store, vector_store=vec, embed_fn=embed,
    )
    router = MagicMock()
    def fake_chat(messages, **kw):
        chunk = MagicMock()
        chunk.text_delta.text = llm_json_response
        yield chunk
    router.chat = fake_chat
    router.config.provider = "mock"
    gate = ExtractionGate(llm_router=router, memory_store=store, session_id=session_id)
    bridge = ReactMemoryBridge(
        dual_channel=dual, gate=gate, memory_store=store,
        session_id=session_id, max_workers=1,
    )
    return bridge, dual, store, tmp


def test_channel_a_writes_daily_log():
    """turn 末尾 memory_tasks 表新增 1 行(state=NONE)"""
    bridge, dual, store, tmp = _make_bridge(
        '{"should_extract": false, "confidence": 0, "candidates": []}'
    )
    try:
        # M11:不再写 JSONL,直接查 memory_tasks 表
        # 又:DualChannelWriter 的幂等检查是 turn_index <= daily_cursor,
        # daily_cursor 初始为 0,turn_index=0 会被短路;这里用 1 绕过.
        list(bridge.on_turn_end(
            user_msg="hello", assistant_resp="hi",
            turn_index=1, input_tokens=100, output_tokens=50, tool_calls_in_turn=0,
        ))
        with dual.meta_db.transaction() as conn:
            row = conn.execute(
                "SELECT turn_index, user_msg, assistant_resp, state "
                "FROM memory_tasks WHERE session_id=? ORDER BY turn_index",
                (bridge.session_id,),
            ).fetchall()
        assert len(row) == 1
        assert row[0][0] == 1
        assert row[0][1] == "hello"
        assert row[0][2] == "hi"
        assert row[0][3] == "NONE"
    finally:
        bridge.shutdown(timeout=5)
        dual.shutdown(timeout=5)
        shutil.rmtree(tmp)


def test_gate1_clears_counter_after_extract():
    """门1 跑完 → 累计清零"""
    bridge, dual, store, tmp = _make_bridge(
        '{"should_extract": true, "confidence": 0.85, "reason": "ok", '
        '"candidates": [{"type": "user", "title": "姓名", "body": "张三", '
        '"source_quote": "我叫张三"}]}'
    )
    try:
        # 累计到 12K(过门1)
        list(bridge.on_turn_end(
            user_msg="Python 协程", assistant_resp="asyncio",
            turn_index=0, input_tokens=6000, output_tokens=6000, tool_calls_in_turn=0,
        ))
        # 跑完后累计应清零
        assert bridge.cumulative_tokens == 0
        assert bridge.cumulative_tool_calls == 0
    finally:
        bridge.shutdown(timeout=5)
        dual.shutdown(timeout=5)
        shutil.rmtree(tmp)


def test_gate2_does_not_clear_counter():
    """门2 跑完 → 累计不清零"""
    bridge, dual, store, tmp = _make_bridge(
        '{"should_extract": true, "confidence": 0.85, "reason": "ok", '
        '"candidates": [{"type": "user", "title": "姓名", "body": "张三", '
        '"source_quote": "我叫张三"}]}'
    )
    try:
        # 累计 200(没过门1,但有"记住"关键词)
        list(bridge.on_turn_end(
            user_msg="记住我叫张三", assistant_resp="好",
            turn_index=0, input_tokens=100, output_tokens=100, tool_calls_in_turn=0,
        ))
        # 累计应保留(门2 跑完不清零)
        assert bridge.cumulative_tokens == 200
        assert bridge.cumulative_tool_calls == 0
    finally:
        bridge.shutdown(timeout=5)
        dual.shutdown(timeout=5)
        shutil.rmtree(tmp)


def test_extract_prompt_has_no_existing_memories_block():
    """旧去重逻辑已移除:提取 prompt 不再注入「已有记忆」。

    去重统一下沉到写盘前的语义去重(向量召回 + 阈值/LLM 判定),
    所以即便库里已有记忆,提取 LLM 的 prompt 里也不含它、不含 existing 块。
    """
    captured_prompts = []

    bridge, dual, store, tmp = _make_bridge(
        '{"should_extract": false, "confidence": 0, "candidates": []}'
    )
    router = bridge.gate.llm_router
    def fake_chat_capture(messages, **kw):
        captured_prompts.append(messages)
        chunk = MagicMock()
        chunk.text_delta.text = '{"should_extract": false, "confidence": 0, "candidates": []}'
        yield chunk
    router.chat = fake_chat_capture

    try:
        # 库里先有一条记忆 —— 旧逻辑会把它塞进 prompt,新逻辑不会
        store.write(
            type="user", title="已有", body="已有记忆XYZ",
            source_quote="turn 1", tags=[],
            extra={"session_id": "s1", "turn_index": 1},
        )
        list(bridge.on_turn_end(
            user_msg="Python", assistant_resp="解释",
            turn_index=5, input_tokens=6000, output_tokens=6000, tool_calls_in_turn=0,
        ))
        assert len(captured_prompts) > 0
        user_msg = captured_prompts[0][-1]["content"]
        assert "existing_memories" not in user_msg, "提取 prompt 不应再含 existing 块"
        assert "已有记忆XYZ" not in user_msg, "已有记忆不应再被注入提取 prompt"
    finally:
        bridge.shutdown(timeout=5)
        dual.shutdown(timeout=5)
        shutil.rmtree(tmp)
