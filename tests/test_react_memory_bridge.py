import tempfile
import time
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
        ))

        kinds = [e.kind for e in events]
        assert MemoryEventKind.CHANNEL_A_OK in kinds
        assert MemoryEventKind.GATE_SKIP in kinds
    finally:
        bridge.shutdown(timeout=5)
        dual.shutdown(timeout=5)
        shutil.rmtree(tmp)


def test_gate_extraction_context_is_current_turn_only():
    """Bug 1d:gate 的提取素材只由「本轮 user+assistant」就地构造。

    历史窗口概念已彻底移除:on_turn_end 不再接收 last_messages/recent_turns 参数,
    所以结构上不可能把历史喂进提取——gate 的 prompt 只含本轮内容。
    本测试断言 LLM 实际收到的 prompt 含本轮 user/assistant 文本,不含其他会话内容。
    """
    tmp = Path(tempfile.mkdtemp(prefix="bridge_bug1d_"))
    try:
        meta = MetaDB(":memory:")
        store = MemoryStore(tmp)
        embed = MagicMock(); embed.encode.return_value = [0.1] * 4
        vec = MagicMock()
        dual = DualChannelWriter(
            session_id="s_bug1d", meta_db=meta,
            memory_store=store, vector_store=vec, embed_fn=embed,
        )

        captured = {}

        def fake_chat(messages, **kw):
            # 记录 LLM 实际收到的 user prompt(turns_text 嵌在里面)
            captured["prompt"] = messages[-1]["content"]
            chunk = MagicMock()
            chunk.text_delta.text = (
                '{"should_extract": false, "confidence": 0.1, "reason": "no", "candidates": []}'
            )
            yield chunk
        router = MagicMock()
        router.chat = fake_chat
        router.config.provider = "mock"

        gate = ExtractionGate(llm_router=router, memory_store=store, session_id="s_bug1d")
        bridge = ReactMemoryBridge(
            dual_channel=dual, gate=gate, memory_store=store,
            session_id="s_bug1d", max_workers=1,
        )

        # on_turn_end 只接收本轮一对(user_msg / assistant_resp),无历史参数
        list(bridge.on_turn_end(
            user_msg="我喜欢周杰伦,请记住",
            assistant_resp="收到,周杰伦已记",
            turn_index=1,
            input_tokens=100, output_tokens=100, tool_calls_in_turn=0,
        ))

        bridge.shutdown(timeout=5)
        dual.shutdown(timeout=5)

        assert "prompt" in captured, "gate 应触发 LLM 评分(命中'记住'关键词)"
        # 提取素材正是本轮的 user + assistant 文本
        assert "我喜欢周杰伦" in captured["prompt"], "本轮 user 内容应在提取素材里"
        assert "周杰伦已记" in captured["prompt"], "本轮 assistant 内容应在提取素材里"
        # 别的会话/历史内容(本测试从未提供)不可能出现
        assert "桃子" not in captured["prompt"]
    finally:
        shutil.rmtree(tmp)


def _wait_extract_done(dual, prev_cursor, timeout=5.0):
    """轮询等 channel B 后台提取完成(extract_cursor 推过 prev_cursor)。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if dual.extract_cursor > prev_cursor:
            return True
        time.sleep(0.02)
    return False


def test_second_turn_persists_when_run_local_turn_index_resets():
    """回归(Bug 1b):连续两轮、run-local turn_index 都传 1(模拟 Streamlit 每条
    用户消息 ReAct 步数重置回 1)→ 第二轮记忆必须照样落盘。

    修复前:channel A 用 session-global daily_cursor+1,channel B 却用 run-local
    turn_index;第一轮后 extract_cursor 推到 2,第二轮 turn_index 又是 1,
    channel B 窗口过滤 extract_cursor(2) <= 1 失败 → to_process=[] → 静默丢弃。
    """
    tmp = Path(tempfile.mkdtemp(prefix="bridge_bug1b_"))
    try:
        meta = MetaDB(":memory:")
        store = MemoryStore(tmp)
        embed = MagicMock(); embed.encode.return_value = [0.1] * 4
        vec = MagicMock()
        dual = DualChannelWriter(
            session_id="s_bug1b", meta_db=meta,
            memory_store=store, vector_store=vec, embed_fn=embed,
        )

        # gate 每轮返回一条 high-confidence candidate,body 取自当轮 user_msg
        # (body 不同 → item_hash 不同 → 不会被幂等去重成 1 条)
        def fake_chat(messages, **kw):
            user_text = messages[-1]["content"]
            # 用 "日本" 做判别(只出现在第 2 轮 user_msg);不能用 "看书",
            # 因为第 2 轮 prompt 会把第 1 轮已存记忆"喜欢看书"塞进 existing 段。
            if "日本" in user_text:
                body, title = "不喜欢日本人", "态度"
            else:
                body, title = "喜欢看书", "爱好"
            chunk = MagicMock()
            chunk.text_delta.text = (
                '{"should_extract": true, "confidence": 0.9, "reason": "ok",'
                f' "candidates": [{{"type": "user", "title": "{title}",'
                f' "body": "{body}", "source_quote": "{body}"}}]}}'
            )
            yield chunk
        router = MagicMock()
        router.chat = fake_chat
        router.config.provider = "mock"

        gate = ExtractionGate(llm_router=router, memory_store=store, session_id="s_bug1b")
        bridge = ReactMemoryBridge(
            dual_channel=dual, gate=gate, memory_store=store,
            session_id="s_bug1b", max_workers=1,
        )

        # ── 第 1 轮:run-local turn_index=1 ──
        list(bridge.on_turn_end(
            user_msg="我喜欢看书,请记住",
            assistant_resp="好的,已记",
            turn_index=1,
            input_tokens=100, output_tokens=100, tool_calls_in_turn=0,
        ))
        assert _wait_extract_done(dual, prev_cursor=0), "第 1 轮提取应完成"

        # ── 第 2 轮:run-local turn_index 又是 1(关键:模拟 run 计数重置)──
        cursor_after_t1 = dual.extract_cursor
        list(bridge.on_turn_end(
            user_msg="我不喜欢日本人,请记住",
            assistant_resp="好的,已记",
            turn_index=1,  # ← 仍是 1,复现 bug 触发条件
            input_tokens=100, output_tokens=100, tool_calls_in_turn=0,
        ))
        assert _wait_extract_done(dual, prev_cursor=cursor_after_t1), \
            "第 2 轮提取应完成(修复前会因 to_process=[] 静默丢弃)"

        bridge.shutdown(timeout=5)
        dual.shutdown(timeout=5)

        # 两轮各一条记忆都应落盘
        md_files = list((tmp / "user").glob("*.md"))
        bodies = "\n".join(p.read_text(encoding="utf-8") for p in md_files)
        assert "不喜欢日本人" in bodies, \
            f"第 2 轮记忆未落盘;现有 {len(md_files)} 个文件:\n{bodies}"
        assert "喜欢看书" in bodies, "第 1 轮记忆也应在"
    finally:
        shutil.rmtree(tmp)