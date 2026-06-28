"""
agent_core.py 记忆检索 wiring 回归测试(2026-06-26 M11 修复)

覆盖:
1. agent 调 memory_retriever.search() 时必须传 mode + top_k(而不是硬编码)
   来自 self.memory_config.retrieval —— 否则 .env 里 MEMORY_RETRIEVAL__MODE / __TOP_K
   改了不生效。
2. memory_config 为 None 时必须有兜底默认值(向后兼容老 caller)
3. already_surfaced 必须传(用于多轮去重)

为什么独立于 web/app.py:
- web/app.py import streamlit,测试环境无 streamlit
- agent_core.py 顶层 import 是干净的,可以直接构造 ReactAgent
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from agent_core.agent_core import ReactAgent
from agent_core.memory.config import MemoryConfig


# ──────────────────────────────────────────────────────────────────
# Fixtures
# ──────────────────────────────────────────────────────────────────


@pytest.fixture
def mock_router():
    """最小 LLMRouter mock —— 不让 __init__ 走 LLM 真实初始化"""
    router = MagicMock()
    router.config = MagicMock()
    router.config.model = "mock-model"
    router.config.system_prompt = ""
    return router


@pytest.fixture
def mock_registry():
    """最小 ToolRegistry mock"""
    return MagicMock()


@pytest.fixture
def agent(mock_router, mock_registry):
    """最小 ReactAgent —— memory_retriever 用 MagicMock 替身"""
    return ReactAgent(
        llm_router=mock_router,
        tool_registry=mock_registry,
        max_turns=1,
        memory_retriever=MagicMock(),
    )


# ──────────────────────────────────────────────────────────────────
# Tests
# ──────────────────────────────────────────────────────────────────


class TestMemorySearchWiring:
    """核心 invariant:memory_retriever.search() 必须收 mode + top_k 来自 config"""

    def test_search_receives_mode_from_memory_config(self, agent):
        """retriever.search() 必须收到 memory_config.retrieval.mode,而不是硬编码 'semantic'"""
        agent.memory_config = MemoryConfig()
        agent.memory_config.retrieval.mode = "side_query"
        agent.memory_retriever.search.return_value = MagicMock(hits=[])

        agent._call_memory_retriever("test query")

        call_kwargs = agent.memory_retriever.search.call_args.kwargs
        assert call_kwargs["mode"] == "side_query", (
            f"mode 必须是 side_query(来自 config),实际传 {call_kwargs.get('mode')!r}"
        )

    def test_search_receives_top_k_from_memory_config(self, agent):
        """retriever.search() 必须收到 memory_config.retrieval.top_k,而不是硬编码 5"""
        agent.memory_config = MemoryConfig()
        agent.memory_config.retrieval.top_k = 9
        agent.memory_retriever.search.return_value = MagicMock(hits=[])

        agent._call_memory_retriever("test query")

        call_kwargs = agent.memory_retriever.search.call_args.kwargs
        assert call_kwargs["top_k"] == 9, (
            f"top_k 必须是 9(来自 config),实际传 {call_kwargs.get('top_k')!r}"
        )

    def test_search_falls_back_when_memory_config_is_none(self, agent):
        """memory_config 为 None 时必须有兜底(向后兼容老 caller)"""
        agent.memory_config = None
        agent.memory_retriever.search.return_value = MagicMock(hits=[])

        agent._call_memory_retriever("test query")

        call_kwargs = agent.memory_retriever.search.call_args.kwargs
        assert call_kwargs["mode"] == "semantic", (
            f"无 config 时 mode 兜底应为 'semantic',实际 {call_kwargs.get('mode')!r}"
        )
        assert call_kwargs["top_k"] == 5, (
            f"无 config 时 top_k 兜底应为 5,实际 {call_kwargs.get('top_k')!r}"
        )

    def test_search_passes_already_surfaced(self, agent):
        """already_surfaced 必须传(多轮去重依赖)"""
        agent.memory_config = MemoryConfig()
        agent._surfaced_memories = {"user/foo.md", "user/bar.md"}
        agent.memory_retriever.search.return_value = MagicMock(hits=[])

        agent._call_memory_retriever("test query")

        call_kwargs = agent.memory_retriever.search.call_args.kwargs
        assert call_kwargs["already_surfaced"] == {"user/foo.md", "user/bar.md"}, (
            "already_surfaced 必须是 self._surfaced_memories 引用,不是新 set"
        )

    def test_search_passes_query_as_first_arg(self, agent):
        """query 必须作为第一个位置参数"""
        agent.memory_config = MemoryConfig()
        agent.memory_retriever.search.return_value = MagicMock(hits=[])

        agent._call_memory_retriever("我是谁")

        call_args = agent.memory_retriever.search.call_args.args
        assert call_args[0] == "我是谁"
