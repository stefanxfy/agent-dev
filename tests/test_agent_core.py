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
    """L1:SystemPromptAssembler.build() 把 MEMORY.md 拼到 system prompt

    2026-07-02:_build_system_prompt_with_memory 已迁入 SystemPromptAssembler。
    agent._assembler.build() 是新路径(走 L1 启动通道)。
    """
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
    prompt = agent._assembler.build()
    assert prompt.startswith("BASE")
    assert "# Agent Memory (auto-generated)" in prompt
    assert "[小明]" in prompt


def test_agent_core_build_system_prompt_no_index_returns_base_plus_trust(tmp_path):
    """无 memory_index → base + TRUSTING_RECALL_SECTION H2 段"""
    agent = _build_agent(tmp_path, store=None)
    prompt = agent._assembler.build()
    assert prompt.startswith("BASE")
    assert "## Before recommending from memory" in prompt


def test_agent_core_build_system_prompt_contains_trust_section_with_index(tmp_path):
    """有 index 时,prompt 仍含 TRUSTING_RECALL_SECTION"""
    from agent_core.memory.memory_store import MemoryStore
    store = MemoryStore(tmp_path / "memory")
    store.write(
        type="user", name="小明", description="Python 工程师",
        body="小明是 Python 工程师",
        source_quote="我说'我是小明'",
    )
    agent = _build_agent(tmp_path, store=store)
    agent.memory_index.rebuild()
    prompt = agent._assembler.build()
    assert "## Before recommending from memory" in prompt
    assert "[小明]" in prompt
    # H1 (MEMORY.md) 出现在 H2 (Before recommending) 之前
    assert prompt.index("# Agent Memory") < prompt.index(
        "## Before recommending from memory"
    )


# 注：原 test_agent_core_initial_surfaced_memories_empty /
# test_agent_core_surfaced_memories_accumulate /
# test_agent_core_already_surfaced_passed_to_retriever 三个测试已删除。
# 它们测的旧契约（agent 持 _surfaced_memories + handler 传 already_surfaced）
# 已被封装进 retriever —— 新契约的覆盖见 tests/test_retriever_surfaced.py（10 用例）。