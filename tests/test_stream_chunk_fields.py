"""
字段契约测试:agent_core/llm/types.py

2026-07-07 bug fix:router.chat() 的 collection 代码曾误用
chunk.thinking_delta.text (实际字段是 .thinking),导致生产
AttributeError 中断 LLM stream。本测试锁住 StreamChunk 各
delta 类的字段名,防止字段重命名时再次踩坑。

如果 types.py 的 dataclass 字段改动,此测试必须同步更新。
"""
from __future__ import annotations

import pytest

from agent_core.llm.types import (
    StreamChunk,
    TextDelta,
    ThinkingDelta,
    ToolCallDelta,
)


class TestStreamChunkDeltaFields:
    """锁字段名,防止 router.py 等下游误用而崩溃。"""

    def test_text_delta_has_text_field(self):
        td = TextDelta(text="hello", is_final=True)
        assert td.text == "hello"
        assert td.is_final is True

    def test_thinking_delta_has_thinking_field_NOT_text(self):
        """🧩 这是关键字段契约:ThinkingDelta 字段是 .thinking (不是 .text)。
        router.chat() collection 必须用 .thinking,否则 AttributeError。
        """
        th = ThinkingDelta(thinking="let me think", is_final=True)
        # 正确字段
        assert th.thinking == "let me think"
        # 反向断言:不应有 .text 字段(否则破坏契约清晰度)
        assert not hasattr(th, "text"), (
            "BUG:ThinkingDelta 增加了 .text 字段!这会破坏 router.chat() "
            "对 thinking_delta.thinking 的契约。如真要加,同步改 router.py。"
        )

    def test_tool_call_delta_has_correct_fields(self):
        """ToolCallDelta 字段是 tool_name/tool_input/tool_use_id(非 name/input)。"""
        tc = ToolCallDelta(
            tool_name="Read",
            tool_input={"path": "/x"},
            tool_use_id="abc123",
            is_final=True,
        )
        assert tc.tool_name == "Read"
        assert tc.tool_input == {"path": "/x"}
        assert tc.tool_use_id == "abc123"
        # 防御:不应有易混字段
        assert not hasattr(tc, "name"), "TC 不应暴露 .name,用 .tool_name"
        assert not hasattr(tc, "input"), "TC 不应暴露 .input,用 .tool_input"


class TestStreamChunkCollectionContract:
    """模拟 router.chat() 的 collection 模式(可直接 copy-paste 自 router.py),
    验证每个 chunk 的字段访问不会 AttributeError。
    """

    def _simulate_collection(self, chunks: list[StreamChunk]):
        """Mirror router.py:148-183 的 collection 逻辑。"""
        text_buf: list[str] = []
        thinking_buf: list[str] = []
        tool_calls_buf: list = []
        usage = None
        stop_reason = None
        chunks_count = 0
        for chunk in chunks:
            chunks_count += 1
            if chunk.text_delta and chunk.text_delta.text:
                text_buf.append(chunk.text_delta.text)
            if chunk.thinking_delta and chunk.thinking_delta.thinking:
                thinking_buf.append(chunk.thinking_delta.thinking)
            if chunk.tool_call is not None:
                tool_calls_buf.append(chunk.tool_call)
            if chunk.usage is not None:
                usage = chunk.usage
            if chunk.stop_reason is not None:
                stop_reason = chunk.stop_reason
        return {
            "text": "".join(text_buf),
            "thinking": "".join(thinking_buf),
            "tool_calls_count": len(tool_calls_buf),
            "stop_reason": stop_reason,
            "chunks_count": chunks_count,
        }

    def test_collection_thinking_chunk_does_not_raise(self):
        """复现 bug:chunk 含 thinking_delta 时不应崩。"""
        from agent_core.llm.types import ThinkingDelta
        chunks = [
            StreamChunk(thinking_delta=ThinkingDelta(thinking="reasoning here")),
            StreamChunk(text_delta=TextDelta(text="answer")),
            StreamChunk(stop_reason="end_turn"),
        ]
        result = self._simulate_collection(chunks)
        assert result["thinking"] == "reasoning here"
        assert result["text"] == "answer"
        assert result["stop_reason"] == "end_turn"
        assert result["chunks_count"] == 3

    def test_collection_tool_call_chunk(self):
        """tool_call 字段正确收集。"""
        from agent_core.llm.types import ToolCallDelta
        chunks = [
            StreamChunk(tool_call=ToolCallDelta(
                tool_name="Bash",
                tool_input={"command": "echo hi"},
                tool_use_id="call_1",
            )),
        ]
        result = self._simulate_collection(chunks)
        assert result["tool_calls_count"] == 1
        # 验证后续 dump 字段名(2026-07-07 fix 后)
        tc = chunks[0].tool_call
        dump = {
            "tool_name": getattr(tc, "tool_name", None),
            "tool_input": getattr(tc, "tool_input", None),
            "tool_use_id": getattr(tc, "tool_use_id", None),
        }
        assert dump["tool_name"] == "Bash"
        assert dump["tool_input"] == {"command": "echo hi"}
        assert dump["tool_use_id"] == "call_1"
