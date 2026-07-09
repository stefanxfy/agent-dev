"""tests/test_memory_retrieval_handler.py — MemoryRetrievalHandler 集成测试(选项 A)。

选项 A 重构 (2026-07-06):handler 内聚 retrieve + append_system + emit memory_status。
重点锁定:side_query mode/top_k bug 修复(handle 层)+ memory_status 事件格式 + resilience。
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from agent_core.agent_state import RunState, TurnContext
from agent_core.memory.config import MemoryConfig
from agent_core.turn_chain import HandlerResult, MemoryRetrievalHandler


def _hit(title="t", body="b", type="feedback", rel_path="x.md"):
    return SimpleNamespace(title=title, body=body, type=type, rel_path=rel_path)


def _make_ctx() -> TurnContext:
    return TurnContext(run_state=RunState())


def _make_agent(
    hits=None,
    mode="semantic",
    top_k=5,
    stored_counts=None,
    messages=None,
    search_raises=False,
):
    agent = MagicMock()
    cfg = MemoryConfig()
    cfg.retrieval.mode = mode
    cfg.retrieval.top_k = top_k
    agent.memory_config = cfg
    retriever = MagicMock()
    if search_raises:
        retriever.search.side_effect = RuntimeError("boom")
    else:
        retriever.search.return_value = MagicMock(hits=(hits or []))
    agent.memory_retriever = retriever
    store = MagicMock()
    store.count_by_type.return_value = stored_counts if stored_counts is not None else {"feedback": 3}
    agent.memory_store = store
    agent.messages = messages if messages is not None else [{"role": "user", "content": "test query"}]
    return agent


class TestHandleFlow:
    def test_appends_mem_block_to_system_prompt(self):
        hits = [_hit(title="反偷懒", body="不准 silent 缩 scope")]
        agent = _make_agent(hits=hits)
        ctx = _make_ctx()
        ctx.run_state.system_prompt = "BASE"
        MemoryRetrievalHandler(agent).handle(ctx)
        assert "[记忆库 / 1 hits]" in ctx.run_state.system_prompt
        assert "反偷懒" in ctx.run_state.system_prompt
        assert "不准 silent 缩 scope" in ctx.run_state.system_prompt
        assert "BASE" in ctx.run_state.system_prompt  # base 保留(append 不覆盖)

    def test_emits_memory_status_with_hits(self):
        hits = [_hit(title="a", body="x" * 100), _hit(title="b", body="y" * 100)]
        agent = _make_agent(hits=hits, stored_counts={"feedback": 5})
        ctx = _make_ctx()
        MemoryRetrievalHandler(agent).handle(ctx)
        status_events = [e for e in ctx.events if e[0] == "memory_status"]
        assert len(status_events) == 1
        payload = status_events[0][1]
        assert payload["hits"] == 2
        assert payload["stored_total"] == 5
        assert payload["injected_tokens"] > 0
        assert payload["zero_hit"] is False

    def test_no_hits_does_not_append_but_emits_zero_hit(self):
        agent = _make_agent(hits=[])
        ctx = _make_ctx()
        ctx.run_state.system_prompt = "BASE"
        MemoryRetrievalHandler(agent).handle(ctx)
        assert ctx.run_state.system_prompt == "BASE"  # 不 append
        status_events = [e for e in ctx.events if e[0] == "memory_status"]
        assert len(status_events) == 1
        assert status_events[0][1]["zero_hit"] is True
        assert status_events[0][1]["hits"] == 0

    def test_skips_when_retriever_none(self):
        agent = _make_agent()
        agent.memory_retriever = None
        ctx = _make_ctx()
        result = MemoryRetrievalHandler(agent).handle(ctx)
        assert isinstance(result, HandlerResult)
        assert ctx.run_state.system_prompt == ""  # 未改
        assert all(e[0] != "memory_status" for e in ctx.events)  # 不 emit


class TestSideQueryModeWiring:
    """★ side_query bug 修复锁定:handle 层必须把 config.retrieval.mode/top_k 传给 search。"""

    def test_handle_passes_side_query_mode_from_config(self):
        agent = _make_agent(hits=[], mode="side_query", top_k=7)
        ctx = _make_ctx()
        MemoryRetrievalHandler(agent).handle(ctx)
        kwargs = agent.memory_retriever.search.call_args.kwargs
        assert kwargs["mode"] == "side_query"
        assert kwargs["top_k"] == 7

    def test_handle_passes_semantic_default(self):
        agent = _make_agent(hits=[], mode="semantic", top_k=5)
        ctx = _make_ctx()
        MemoryRetrievalHandler(agent).handle(ctx)
        kwargs = agent.memory_retriever.search.call_args.kwargs
        assert kwargs["mode"] == "semantic"
        assert kwargs["top_k"] == 5

    def test_handle_falls_back_when_memory_config_none(self):
        agent = _make_agent(hits=[])
        agent.memory_config = None  # 向后兼容
        ctx = _make_ctx()
        MemoryRetrievalHandler(agent).handle(ctx)
        kwargs = agent.memory_retriever.search.call_args.kwargs
        assert kwargs["mode"] == "semantic"
        assert kwargs["top_k"] == 5


class TestResilience:
    def test_search_failure_emits_zero_hit_and_no_append(self):
        agent = _make_agent(search_raises=True)
        ctx = _make_ctx()
        ctx.run_state.system_prompt = "BASE"
        result = MemoryRetrievalHandler(agent).handle(ctx)
        assert isinstance(result, HandlerResult)  # 不崩
        assert ctx.run_state.system_prompt == "BASE"  # 不 append
        status_events = [e for e in ctx.events if e[0] == "memory_status"]
        assert status_events[0][1]["zero_hit"] is True

    def test_memory_store_failure_falls_back_to_zero(self):
        hits = [_hit(title="a", body="x")]
        agent = _make_agent(hits=hits)
        agent.memory_store.count_by_type.side_effect = RuntimeError("db down")
        ctx = _make_ctx()
        MemoryRetrievalHandler(agent).handle(ctx)
        status_events = [e for e in ctx.events if e[0] == "memory_status"]
        assert status_events[0][1]["stored_total"] == 0  # 兜底 0,不崩


class TestFindLastUserQuery:
    def test_returns_last_user_text(self):
        agent = _make_agent(messages=[
            {"role": "user", "content": "第一句"},
            {"role": "assistant", "content": "回复"},
            {"role": "user", "content": "第二句"},
        ])
        q = MemoryRetrievalHandler(agent)._find_last_user_query()
        assert q == "第二句"

    def test_returns_none_when_no_user(self):
        agent = _make_agent(messages=[{"role": "assistant", "content": "hi"}])
        assert MemoryRetrievalHandler(agent)._find_last_user_query() is None

    def test_skips_non_string_content(self):
        # tool_result 消息 content 是 list(非 str)→ 跳过,找最近的 str user
        agent = _make_agent(messages=[
            {"role": "user", "content": [{"type": "tool_result"}]},
            {"role": "user", "content": "真正文本"},
        ])
        assert MemoryRetrievalHandler(agent)._find_last_user_query() == "真正文本"


class TestFormatBlock:
    def test_format_includes_type_title_body(self):
        hits = [_hit(type="feedback", title="规则", body="内容")]
        block = MemoryRetrievalHandler(MagicMock())._format_block(hits)
        assert "[记忆库 / 1 hits]" in block
        assert "[feedback]" in block
        assert "规则" in block
        assert "内容" in block

    def test_format_truncates_long_body(self):
        hits = [_hit(body="x" * 500)]
        block = MemoryRetrievalHandler(MagicMock())._format_block(hits)
        assert "x" * 200 in block  # 截断到 200
        assert "x" * 201 not in block
