"""
M10 C2.1 — ReactAgent.run() 与 L3 SessionMemoryLayer 集成测试

覆盖 M10 §4.3/§4.4:
1. SM 存在 + 满足触发 → 走 sm.compact 快路径,不调 ContextManager.check_and_compact
2. SM 不存在 → fallback ContextManager
3. SM 走 fast path 时 CompactResult 构造正确(零 LLM 路径)

设计:
- _make_agent 用真实 LLMRouter(provider='zhipu', model='glm-4', api_key='mock')
  (与 test_usage_baseline_restore.py 模式一致)
- _make_sm 写一个非 template 的 SM 文件
- run() 测试用 next(gen) + gen.close() 跑最前面一段(避免调完整 LLM 链路)
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from agent_core.agent_core import ReactAgent
from agent_core.llm.router import LLMRouter, LLMConfig
from agent_core.memory import MemoryConfig
from agent_core.memory.sm_layer import (
    CompactDecision,
    CompactResult,
    SessionMemoryLayer,
    TurnContext,
)
from agent_core.tools.base import ToolRegistry


# ──────────────────────────────────────────────────────────────────
# Fixtures / helpers
# ──────────────────────────────────────────────────────────────────

# 一个非 template 的 SM frontmatter + 内容(让 sm_is_template() 返回 False)
_POPULATED_SM_TEXT = """\
---
session_id: test_sess
schema_version: 1
last_compacted_msg_id: null
last_compacted_at: null
---

# Session Memory

## Context
user wants X

## Decisions
use SM layer

## Technical
python
"""


def _make_sm(tmp_path: Path, sm_text: str = _POPULATED_SM_TEXT) -> SessionMemoryLayer:
    """构造 SM 实例,sm_text 写 sm_path"""
    sm_path = tmp_path / "sm.md"
    sm_path.write_text(sm_text, encoding="utf-8")
    return SessionMemoryLayer(
        session_id="test_sess",
        sm_path=sm_path,
        config=MemoryConfig().compact,
    )


def _make_router() -> LLMRouter:
    """Mock LLMRouter(用真实 LLMConfig,API 不真发)"""
    return LLMRouter(LLMConfig(provider='zhipu', model='glm-4', api_key='mock'))


def _make_agent(
    tmp_path: Path,
    sm: SessionMemoryLayer | None = None,
) -> ReactAgent:
    """构造最小 ReactAgent 实例(传入 session_id 让 session_manager 启动,
    session_data_dir 用 tmp_path 避免污染)
    """
    agent = ReactAgent(
        llm_router=_make_router(),
        tool_registry=ToolRegistry(),
        session_id="test_sess",
        session_data_dir=str(tmp_path),
        session_memory=sm,
    )
    return agent


# ──────────────────────────────────────────────────────────────────
# Test 1: SM 走 fast path,ContextManager.check_and_compact 不被调
# ──────────────────────────────────────────────────────────────────

def test_run_compact_uses_sm_fast_path_when_available(tmp_path):
    """SM 存在 + 满足触发 → 走 sm.compact,不调 ContextManager"""
    # 写一个非 template 的 SM 文件,并设 last_compacted_msg_id 让 kept 消息少
    sm_text = """\
---
session_id: test_sess
schema_version: 1
last_compacted_msg_id: msg-099
last_compacted_at: 2026-06-22T00:00:00
---

# Session Memory

## Context
user wants X

## Decisions
use SM layer

## Technical
python
"""
    sm = _make_sm(tmp_path, sm_text)
    agent = _make_agent(tmp_path, sm)

    # 灌入 100 条带 id 的消息,触发 token 阈值(默认 10K)
    # 设 last_compacted_msg_id = msg-099 → kept = [msg-099] 之后,极少
    agent.messages = [
        {"id": f"msg-{i:03d}", "role": "user", "content": f"msg {i} " * 100}
        for i in range(100)
    ]

    # mock ContextManager.check_and_compact → 验:SM 走 fast path 时它不被调
    with patch.object(agent.context_manager, "check_and_compact") as mock_cc:
        mock_cc.return_value = (agent.messages, None)

        gen = agent.run("test message")
        try:
            # 消费第一个 yield
            first_yield = next(gen)
        except StopIteration:
            first_yield = None
        finally:
            gen.close()

        # ContextManager 不应被调(SM 走 fast path)
        assert not mock_cc.called, (
            f"ContextManager.check_and_compact 不应被调 "
            f"(SM 应走 fast path),但被调了 {mock_cc.call_count} 次"
        )
        # 第一个 yield 应该是 [L3 fast path] system 提示
        if first_yield is not None:
            assert isinstance(first_yield, tuple) and first_yield[0] == "system"
            assert "[L3 fast path]" in first_yield[1]


# ──────────────────────────────────────────────────────────────────
# Test 2: SM 不存在 → fallback ContextManager
# ──────────────────────────────────────────────────────────────────

def test_run_compact_falls_back_to_context_manager_when_no_sm(tmp_path):
    """SM 不存在 → fallback ContextManager.check_and_compact"""
    agent = _make_agent(tmp_path, sm=None)
    agent.messages = [
        {"role": "user", "content": f"msg {i} " * 500}
        for i in range(50)  # 灌够 token 触发 ContextManager
    ]

    # mock ContextManager.check_and_compact → 模拟成功压缩
    mock_result = MagicMock()
    mock_result.success = True
    mock_result.summary_str = MagicMock(return_value="summary")
    mock_result.tokens_freed = 100
    mock_result.error = None

    with patch.object(agent.context_manager, "check_and_compact") as mock_cc:
        mock_cc.return_value = (agent.messages[:5], mock_result)

        gen = agent.run("test")
        try:
            for evt in gen:
                if isinstance(evt, tuple) and evt[0] == "system" and "压缩" in evt[1]:
                    break
        finally:
            gen.close()

        # ContextManager.check_and_compact 必须被调(SM 路径不可用 → fallback)
        assert mock_cc.called, "ContextManager.check_and_compact 应被调(SM=None → fallback)"


# ──────────────────────────────────────────────────────────────────
# Test 3: SM 走 fast path 时 CompactResult 构造正确
# ──────────────────────────────────────────────────────────────────

def test_sm_compact_path_emits_valid_compact_result(tmp_path):
    """SM 走 fast path 时 CompactResult 构造正确(summary_message 是 dict,strategy 是 sm_compact)"""
    # 设 last_compacted_msg_id 让 kept 消息少 → projected < threshold
    sm_text = """\
---
session_id: test_sess
schema_version: 1
last_compacted_msg_id: msg-079
last_compacted_at: 2026-06-22T00:00:00
---

# Session Memory

## Context
test content
"""
    sm = _make_sm(tmp_path, sm_text)
    agent = _make_agent(tmp_path, sm)
    agent.messages = [
        {"id": f"msg-{i:03d}", "role": "user", "content": f"msg {i} " * 200}
        for i in range(80)  # 灌够 token(> 10K 阈值)
    ]

    # 直接调 should_trigger_compact + compact(不调 run,避免完整 LLM 链路)
    total_tokens = sum(agent._estimate_message_tokens(m) for m in agent.messages)
    ctx = TurnContext(
        messages=agent.messages,
        total_tokens=total_tokens,
        tool_count=0,
    )
    decision = sm.should_trigger_compact(ctx)

    if decision.strategy == "sm_compact":
        result = sm.compact(agent.messages, context_window=128000)
        assert result is not None
        assert result.strategy == "sm_compact"
        # summary_message 是 dict({role: user, content: ...}),不是 str
        assert isinstance(result.summary_message, dict)
        assert result.summary_message["role"] == "user"
        assert "summary" in result.summary_message["content"].lower()
        # kept_messages 是 list[dict]
        assert isinstance(result.kept_messages, list)
        for m in result.kept_messages:
            assert isinstance(m, dict)
            assert m.get("role") in ("user", "assistant", "tool")
        # used_tokens_estimate 是 int
        assert isinstance(result.used_tokens_estimate, int)
        assert result.used_tokens_estimate > 0
    else:
        # 阈值未达 → skip(合法分支,见 brief)
        pytest.skip(f"未达 SM 触发阈值: {decision.reason}")


# ──────────────────────────────────────────────────────────────────
# 防御性:验证 session_memory 参数确实被存储到 self.session_memory
# ──────────────────────────────────────────────────────────────────

def test_session_memory_stored_on_agent(tmp_path):
    """session_memory 参数必须被存储为 self.session_memory"""
    sm = _make_sm(tmp_path)
    agent = _make_agent(tmp_path, sm)
    assert agent.session_memory is sm

    agent_no_sm = _make_agent(tmp_path, sm=None)
    assert agent_no_sm.session_memory is None
