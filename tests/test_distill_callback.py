"""M11.6 distill_callback 测试 — 跟 M11.5 sm_callback 同模式

覆盖:
1. 工厂返回同步 callback
2. callback 调 router.chat 拼 messages[system, user]
3. 流式 chunks 正确聚合成完整 response
4. retry/backoff: max_retries=2,attempt 1 fail → attempt 2 succeed
5. retry 耗尽 + on_failure="raise" → RuntimeError
6. retry 耗尽 + on_failure="return_empty" → 返 ""
7. LLM 返空字符串 → 视为失败,重试
8. cache_namespace 透传给 router
9. DISTILL_SYSTEM_PROMPT 含期望字段(type / title / why / body / confidence / sources / tags)
"""

from __future__ import annotations

import logging
import time
from unittest.mock import MagicMock

import pytest

from agent_core.memory.distill_callback import (
    DISTILL_SYSTEM_PROMPT,
    make_distill_callback,
)


# ──────────────────────────────────────────────────────────────────
# Fixtures
# ──────────────────────────────────────────────────────────────────

def _make_chunk(text: str):
    """构造一个 mock chunk,text_delta.text == text"""
    chunk = MagicMock()
    chunk.text_delta = MagicMock(text=text)
    chunk.thinking_delta = None
    chunk.tool_call = None
    return chunk


# ──────────────────────────────────────────────────────────────────
# 1. 基础同步 callback
# ──────────────────────────────────────────────────────────────────

class TestBasicCallback:

    def test_callback_returns_string(self):
        """callback 是同步 (prompt) -> str"""
        router = MagicMock()
        router.chat.return_value = iter([_make_chunk('[{"type":"user","title":"t","body":"b"}]')])

        cb = make_distill_callback(router=router)
        result = cb("test prompt")
        assert isinstance(result, str)
        assert "type" in result and "user" in result

    def test_router_chat_called_with_messages_and_cache_namespace(self):
        """callback 把 system+user messages + cache_namespace 透传给 router.chat"""
        router = MagicMock()
        router.chat.return_value = iter([_make_chunk("[]")])

        cb = make_distill_callback(router=router, cache_namespace="distill_xyz")
        cb("user prompt body")

        # router.chat 只被调一次
        assert router.chat.call_count == 1
        call_kwargs = router.chat.call_args.kwargs
        assert call_kwargs["cache_namespace"] == "distill_xyz"
        messages = call_kwargs["messages"]
        assert len(messages) == 2
        assert messages[0]["role"] == "system"
        assert "蒸馏" in messages[0]["content"] or "autoDream" in messages[0]["content"]
        assert messages[1]["role"] == "user"
        assert messages[1]["content"] == "user prompt body"

    def test_chunks_aggregated_in_order(self):
        """流式 chunks 按顺序拼接"""
        router = MagicMock()
        router.chat.return_value = iter([
            _make_chunk("[{"),
            _make_chunk('"type":"user"'),
            _make_chunk(',"title":"t"'),
            _make_chunk(',"body":"b"'),
            _make_chunk("}]"),
        ])

        cb = make_distill_callback(router=router)
        result = cb("p")
        assert result == '[{"type":"user","title":"t","body":"b"}]'

    def test_thinking_and_tool_call_chunks_ignored(self):
        """thinking_delta / tool_call chunks 应被忽略,只取 text_delta.text"""
        router = MagicMock()

        chunk_with_thinking = MagicMock()
        chunk_with_thinking.text_delta = None
        chunk_with_thinking.thinking_delta = MagicMock(text="思考中")
        chunk_with_thinking.tool_call = None

        router.chat.return_value = iter([
            chunk_with_thinking,
            _make_chunk("real response"),
            chunk_with_thinking,
        ])

        cb = make_distill_callback(router=router)
        result = cb("p")
        assert result == "real response"


# ──────────────────────────────────────────────────────────────────
# 2. retry / backoff
# ──────────────────────────────────────────────────────────────────

class TestRetryBackoff:

    def test_success_first_attempt(self):
        """第 1 次成功 → 不重试"""
        router = MagicMock()
        router.chat.return_value = iter([_make_chunk("[]")])

        cb = make_distill_callback(router=router, max_retries=2)
        cb("p")
        assert router.chat.call_count == 1

    def test_retry_then_success(self, monkeypatch):
        """第 1 次 fail → 第 2 次 succeed,mock time.sleep 加速"""
        # mock sleep 避免真的等
        monkeypatch.setattr("time.sleep", lambda s: None)

        router = MagicMock()
        router.chat.side_effect = [
            RuntimeError("transient err"),
            iter([_make_chunk("ok_response")]),
        ]

        cb = make_distill_callback(router=router, max_retries=2)
        result = cb("p")
        assert result == "ok_response"
        assert router.chat.call_count == 2

    def test_retry_exhausted_raises(self, monkeypatch):
        """max_retries 全部失败 + on_failure='raise' → RuntimeError"""
        monkeypatch.setattr("time.sleep", lambda s: None)

        router = MagicMock()
        router.chat.side_effect = RuntimeError("always fail")

        cb = make_distill_callback(
            router=router, max_retries=2, on_failure="raise",
        )
        with pytest.raises(RuntimeError, match="Distill LLM callback 失败"):
            cb("p")
        assert router.chat.call_count == 2

    def test_retry_exhausted_returns_empty(self, monkeypatch):
        """max_retries 全部失败 + on_failure='return_empty' → 返 ''"""
        monkeypatch.setattr("time.sleep", lambda s: None)

        router = MagicMock()
        router.chat.side_effect = RuntimeError("always fail")

        cb = make_distill_callback(
            router=router, max_retries=2, on_failure="return_empty",
        )
        result = cb("p")
        assert result == ""

    def test_empty_response_triggers_retry(self, monkeypatch):
        """LLM 返空字符串 → 视为失败,触发重试"""
        monkeypatch.setattr("time.sleep", lambda s: None)

        router = MagicMock()
        # 第一次返空 chunk → 失败
        empty_chunk = MagicMock()
        empty_chunk.text_delta = MagicMock(text="")
        empty_chunk.thinking_delta = None
        empty_chunk.tool_call = None
        router.chat.side_effect = [
            iter([empty_chunk]),
            iter([_make_chunk("valid")]),
        ]

        cb = make_distill_callback(router=router, max_retries=2)
        result = cb("p")
        assert result == "valid"
        assert router.chat.call_count == 2


# ──────────────────────────────────────────────────────────────────
# 3. 系统 prompt 内容
# ──────────────────────────────────────────────────────────────────

class TestSystemPrompt:

    def test_system_prompt_required_fields(self):
        """系统 prompt 包含期望字段说明"""
        for field_name in ("type", "title", "why", "body", "confidence", "sources", "tags"):
            assert field_name in DISTILL_SYSTEM_PROMPT, \
                f"DISTILL_SYSTEM_PROMPT 缺字段: {field_name}"

    def test_system_prompt_empty_array_allowed(self):
        """prompt 显式说返 [] 表示无需整理"""
        assert "[]" in DISTILL_SYSTEM_PROMPT or "空数组" in DISTILL_SYSTEM_PROMPT


# ──────────────────────────────────────────────────────────────────
# 4. logging 行为
# ──────────────────────────────────────────────────────────────────

class TestLogging:

    def test_logs_success(self, caplog):
        """成功 → DEBUG 日志"""
        router = MagicMock()
        router.chat.return_value = iter([_make_chunk("ok")])

        cb = make_distill_callback(router=router)
        with caplog.at_level(logging.DEBUG, logger="agent_core.memory.distill_callback"):
            cb("p")

        assert any("LLM 响应成功" in r.message for r in caplog.records)

    def test_logs_retry_warning(self, caplog, monkeypatch):
        """retry 失败 → WARNING 日志"""
        monkeypatch.setattr("time.sleep", lambda s: None)

        router = MagicMock()
        router.chat.side_effect = RuntimeError("transient")

        cb = make_distill_callback(router=router, max_retries=2, on_failure="return_empty")
        with caplog.at_level(logging.WARNING, logger="agent_core.memory.distill_callback"):
            cb("p")

        assert any("LLM 调用失败" in r.message for r in caplog.records)
        assert any("重试" in r.message and "返空字符串" in r.message for r in caplog.records)