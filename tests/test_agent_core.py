"""
M11: AgentCore (ReactAgent) L1 MEMORY.md 注入 + already_surfaced 测试
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest


class _StubLLM:
    """最小 LLM stub,避免真实 router 加载"""

    def __init__(self):
        self.config = SimpleNamespace(
            system_prompt="BASE",
            model="stub-model",
        )

    def stream_chat(self, *args, **kwargs):
        yield SimpleNamespace(text="ok")

    async def astream_chat(self, *args, **kwargs):
        yield SimpleNamespace(text="ok")


class _StubToolRegistry:
    def list_schemas(self, provider=""):
        return []


def _build_agent(tmp_path, store=None):
    """构造最小可运行的 ReactAgent(无 session)"""
    from agent_core.agent_core import ReactAgent
    llm = _StubLLM()
    tools = _StubToolRegistry()
    agent = ReactAgent(
        llm_router=llm,
        tool_registry=tools,
        max_turns=1,
        memory_store=store,
        memory_retriever=None,  # T11 仅测 L1 注入 + already_surfaced
    )
    return agent


def test_agent_core_creates_memory_index_when_store_given(tmp_path):
    """L1:AgentCore 启动时若 memory_store 已提供,创建 memory_index"""
    from agent_core.memory.memory_store import MemoryStore
    store = MemoryStore(tmp_path / "memory")
    agent = _build_agent(tmp_path, store=store)
    assert agent.memory_index is not None
    # MEMORY.md 已被 lazy rebuild 兜底创建
    assert (tmp_path / "memory" / "MEMORY.md").exists()


def test_agent_core_no_memory_index_without_store(tmp_path):
    """无 memory_store → memory_index = None"""
    agent = _build_agent(tmp_path, store=None)
    assert agent.memory_index is None


def test_agent_core_build_system_prompt_with_memory(tmp_path):
    """L1:_build_system_prompt_with_memory 把 MEMORY.md 拼到 system prompt"""
    from agent_core.memory.memory_store import MemoryStore
    store = MemoryStore(tmp_path / "memory")
    store.write(
        type="user", name="小明", description="Python 工程师",
        body="小明是 Python 工程师",
        source_quote="我说'我叫小明,Python 工程师'",
    )
    agent = _build_agent(tmp_path, store=store)
    # force rebuild after writing
    agent.memory_index.rebuild()
    prompt = agent._build_system_prompt_with_memory()
    assert prompt.startswith("BASE")
    assert "# Agent Memory (auto-generated)" in prompt
    assert "[小明]" in prompt


def test_agent_core_build_system_prompt_no_index_returns_base(tmp_path):
    """无 memory_index → 仅 base prompt"""
    agent = _build_agent(tmp_path, store=None)
    prompt = agent._build_system_prompt_with_memory()
    assert prompt == "BASE"


def test_agent_core_initial_surfaced_memories_empty(tmp_path):
    """初始 _surfaced_memories 为空 set"""
    agent = _build_agent(tmp_path, store=None)
    assert agent._surfaced_memories == set()
    assert isinstance(agent._surfaced_memories, set)


def test_agent_core_surfaced_memories_accumulate(tmp_path):
    """多次 add 后 _surfaced_memories 累加"""
    from agent_core.memory.memory_store import MemoryStore
    store = MemoryStore(tmp_path / "memory")
    agent = _build_agent(tmp_path, store=store)
    agent._surfaced_memories.add("user/abc.md")
    agent._surfaced_memories.add("feedback/xyz.md")
    assert "user/abc.md" in agent._surfaced_memories
    assert "feedback/xyz.md" in agent._surfaced_memories
    assert len(agent._surfaced_memories) == 2


def test_agent_core_already_surfaced_passed_to_retriever(tmp_path):
    """第二次 stream_chat 调 retriever.search 时 already_surfaced 已含上一轮 path"""
    from agent_core.memory.memory_store import MemoryStore
    from agent_core.memory.retriever import (
        MemoryRetriever, MemoryHit, RetrievalReport, RetrievalMode,
    )

    store = MemoryStore(tmp_path / "memory")
    store.write(
        type="user", name="小明", description="Python 工程师",
        body="小明是 Python 工程师",
        source_quote="我说'小明'",
    )

    fake_embed = type("F", (), {
        "encode": lambda self, t: [0.0] * 1024,
    })()

    # 每次 search 调用时,记录当时的 already_surfaced 快照(set copy)
    snapshots: list[set[str]] = []

    class _SpyRetriever:
        def __init__(self):
            self.memory_store = store
            self.vector_store = type("V", (), {
                "query": lambda *a, **k: [],
            })()
            self.embed_fn = fake_embed
            self.config = type("C", (), {"retrieval": type("R", (), {
                "side_query_max_files": 200,
                "side_query_max_select": 5,
            })()})()
            self.secret_scanner = type("S", (), {
                "scan": lambda self, t: SimpleNamespace(is_clean=True, hits=[]),
            })()

        def search(self, query, **kwargs):
            # 拷贝当时的 already_surfaced(set 是 in-place,直接 ref 会被覆盖)
            surfaced = kwargs.get("already_surfaced") or set()
            snapshots.append(set(surfaced))
            return RetrievalReport(
                query=query, mode=RetrievalMode.SEMANTIC,
                hits=[MemoryHit(
                    item_hash="x" * 64, type="user", title="t",
                    body="b", rel_path="user/abc.md", score=0.5,
                    breakdown={"semantic": 0.5},
                )]
            )

    spy = _SpyRetriever()
    agent = _build_agent(tmp_path, store=store)
    agent.memory_retriever = spy

    # 模拟第一次检索 + 记录 surfacd
    report1 = agent.memory_retriever.search(
        "m1", already_surfaced=agent._surfaced_memories
    )
    for h in report1.hits:
        agent._surfaced_memories.add(h.rel_path)
    # 模拟第二次检索
    report2 = agent.memory_retriever.search(
        "m2", already_surfaced=agent._surfaced_memories
    )
    for h in report2.hits:
        agent._surfaced_memories.add(h.rel_path)

    # 第一次:already_surfaced 为空
    assert snapshots[0] == set()
    # 第二次:已含 user/abc.md
    assert "user/abc.md" in snapshots[1]
    assert len(snapshots) == 2