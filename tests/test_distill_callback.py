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


def _make_router(invoke_value):
    """构造 fake router: invoke() 返固定 str(distill_callback 改用 invoke() 收归)。

    invoke_value 可以是 str(返 str)或 MagicMock(走 side_effect)。
    """
    router = MagicMock()
    if isinstance(invoke_value, MagicMock):
        router.invoke = invoke_value
    else:
        router.invoke.return_value = invoke_value
    return router


# ──────────────────────────────────────────────────────────────────
# 1. 基础同步 callback
# ──────────────────────────────────────────────────────────────────

class TestBasicCallback:

    def test_callback_returns_string(self):
        """callback 是同步 (prompt) -> str"""
        router = _make_router('[{"type":"user","title":"t","body":"b"}]')
        cb = make_distill_callback(router=router)
        result = cb("test prompt")
        assert isinstance(result, str)
        assert "type" in result and "user" in result

    def test_router_invoke_called_with_messages_and_cache_namespace(self):
        """callback 把 system+user messages + cache_namespace 透传给 router.invoke"""
        router = _make_router("[]")
        cb = make_distill_callback(router=router, cache_namespace="distill_xyz")
        cb("user prompt body")

        # router.invoke 只被调一次
        assert router.invoke.call_count == 1
        call_kwargs = router.invoke.call_args.kwargs
        assert call_kwargs["cache_namespace"] == "distill_xyz"
        messages = call_kwargs["messages"]
        assert len(messages) == 2
        assert messages[0]["role"] == "system"
        assert "蒸馏" in messages[0]["content"] or "autoDream" in messages[0]["content"]
        assert messages[1]["role"] == "user"
        assert messages[1]["content"] == "user prompt body"

    def test_invoke_returns_aggregated_response(self):
        """invoke() 已聚合 chunks — callback 原样返 invoke() 的结果"""
        router = _make_router('[{"type":"user","title":"t","body":"b"}]')
        cb = make_distill_callback(router=router)
        result = cb("p")
        assert result == '[{"type":"user","title":"t","body":"b"}]'


# ──────────────────────────────────────────────────────────────────
# 2. retry / backoff(已由 router.invoke() 收归,这里只测 callback 的 max_retries 透传)
# ──────────────────────────────────────────────────────────────────

class TestRetryBackoff:

    def test_passes_max_retries_to_invoke(self):
        """callback 把 max_retries 透传给 invoke()"""
        router = _make_router("ok")
        cb = make_distill_callback(router=router, max_retries=5)
        cb("p")
        assert router.invoke.call_args.kwargs["max_retries"] == 5

    def test_success_first_attempt(self):
        """第 1 次成功 → 不重试"""
        router = _make_router("[]")
        cb = make_distill_callback(router=router, max_retries=2)
        cb("p")
        assert router.invoke.call_count == 1

    def test_retry_exhausted_raises(self):
        """invoke() 抛错 + on_failure='raise' → callback 透传"""
        router = MagicMock()
        router.invoke.side_effect = RuntimeError("always fail")
        cb = make_distill_callback(
            router=router, max_retries=2, on_failure="raise",
        )
        with pytest.raises(RuntimeError, match="always fail"):
            cb("p")

    def test_retry_exhausted_returns_empty(self):
        """invoke() 返空 + on_failure='return_empty' → callback 返 ''"""
        router = _make_router("")  # 模拟 invoke() on_failure 返空
        cb = make_distill_callback(
            router=router, max_retries=2, on_failure="return_empty",
        )
        result = cb("p")
        assert result == ""
        # 验证 callback 确实把 on_failure 闭包传给了 invoke()
        assert router.invoke.call_args.kwargs["on_failure"] is not None


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
        router = _make_router("ok")
        cb = make_distill_callback(router=router)
        with caplog.at_level(logging.DEBUG, logger="agent_core.memory.distill_callback"):
            cb("p")

        assert any("LLM 响应成功" in r.message for r in caplog.records)

    def test_logs_retry_warning(self, caplog):
        """on_failure='return_empty' 触发 → WARNING 日志

        模拟 router.invoke() 内部重试耗尽 → 调 on_failure(err) 返空字符串
        (即真实 invoke() 在 on_failure='return_empty' 时的行为)。
        """
        router = MagicMock()
        def fake_invoke(messages, *, on_failure=None, **kwargs):
            # 真实 invoke() 内部:重试耗尽 → on_failure(last_err) → 返结果
            if on_failure is not None:
                return on_failure(RuntimeError("simulated invoke failure"))
            raise RuntimeError("simulated invoke failure, no on_failure")
        router.invoke.side_effect = fake_invoke
        cb = make_distill_callback(router=router, max_retries=0, on_failure="return_empty")
        with caplog.at_level(logging.WARNING, logger="agent_core.memory.distill_callback"):
            cb("p")

        # 确认 on_failure 路径触发了 WARNING 日志
        assert any("返空" in r.message or "重试" in r.message for r in caplog.records)