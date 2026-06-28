"""
M7 / Day 7 集成测试 —— 3 cases

覆盖:
1. test_router_signature_has_cache_namespace — chat() 接受 cache_namespace
2. test_memory_status_chunk_aggregation — UI chunk 累积逻辑(模拟 streamlit session_state)
3. test_schema_strict_validation — validate_frontmatter 严校验

注意:端到端 Agent run 测试需要 langchain_core(本环境未装),
故 case 聚焦在 M7 两个独立可测的接入点 + schema 严校验:
- Router 合约(router.py chat 签名)
- UI chunk 累积(extract logic 出来单测)
- Frontmatter 严校验(M11 不兼容旧 schema)
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from agent_core.memory import (
    CURRENT_SCHEMA_VERSION,
    MemoryConfig,
    MemoryStore,
)
from agent_core.memory.types import validate_frontmatter


# ──────────────────────────────────────────────────────────────────
# Fixtures
# ──────────────────────────────────────────────────────────────────

@pytest.fixture
def memory_root(tmp_path):
    root = tmp_path / "memory"
    root.mkdir()
    return root


# ──────────────────────────────────────────────────────────────────
# 2. Router 合约
# ──────────────────────────────────────────────────────────────────
# 3. Router 合约 —— cache_namespace 签名
# ──────────────────────────────────────────────────────────────────

class TestRouterContract:

    def test_chat_accepts_cache_namespace_kwarg(self):
        """LLMRouter.chat 接受 cache_namespace 参数(签名 + docstring)"""
        from agent_core.llm.router import LLMRouter
        import inspect

        sig = inspect.signature(LLMRouter.chat)
        assert "cache_namespace" in sig.parameters, "chat() 必须有 cache_namespace 参数"
        # 默认值是 None
        assert sig.parameters["cache_namespace"].default is None

    def test_chat_docstring_documents_cache_namespace(self):
        """chat() docstring 提到 cache_namespace(给使用者看)"""
        from agent_core.llm.router import LLMRouter
        doc = LLMRouter.chat.__doc__ or ""
        assert "cache_namespace" in doc
        # 应说明 cache_namespace 的语义(不是孤立单词)
        assert "Anthropic" in doc or "anthropic" in doc


# ──────────────────────────────────────────────────────────────────
# 4. UI chunk 累积逻辑(从 app_langgraph.py 提取,单测)
# ──────────────────────────────────────────────────────────────────

class TestUIChunkAggregation:
    """
    测试 session_state.memory_stats / token_stats 的累积逻辑
    (从 app_langgraph.py 的 chunk 处理代码抽出,避免依赖 streamlit)
    """

    def _accumulate_usage(self, stats: dict, usage_obj) -> None:
        """从 app_langgraph.py 抽出的 usage chunk 累积逻辑"""
        stats["input"] += getattr(usage_obj, "input_tokens", 0)
        stats["output"] += getattr(usage_obj, "output_tokens", 0)
        stats["thinking"] += getattr(usage_obj, "thinking_tokens", 0)
        stats["cached"] += getattr(usage_obj, "cached_tokens", 0)

    def _accumulate_memory_status(self, ms: dict, content: dict) -> None:
        """从 app_langgraph.py 抽出的 memory_status chunk 累积逻辑"""
        ms["total_searches"] += 1
        ms["total_hits"] += int(content.get("hits", 0))
        ms["current_turn_hits"] = int(content.get("hits", 0))
        ms["stored_total"] = int(content.get("stored_total", 0))
        if content.get("zero_hit"):
            ms["last_zero_hit_turn"] = ms["total_searches"]

    def test_cached_tokens_now_accumulated(self):
        """M7 修复:cached_tokens 不再被静默丢弃"""
        stats = {"input": 0, "output": 0, "thinking": 0, "cached": 0}
        usage = MagicMock(input_tokens=100, output_tokens=50, thinking_tokens=20, cached_tokens=80)
        self._accumulate_usage(stats, usage)
        assert stats == {"input": 100, "output": 50, "thinking": 20, "cached": 80}

    def test_memory_status_zero_hit_marks_last_zero_turn(self):
        """memory_status chunk zero_hit=True → 记下 last_zero_hit_turn"""
        ms = {"total_searches": 0, "total_hits": 0, "last_zero_hit_turn": None,
              "current_turn_hits": 0, "stored_total": 0}
        # turn 1: 有 2 hits
        self._accumulate_memory_status(ms, {"hits": 2, "stored_total": 10, "zero_hit": False})
        assert ms["total_searches"] == 1
        assert ms["total_hits"] == 2
        assert ms["last_zero_hit_turn"] is None
        # turn 2: 0 hits
        self._accumulate_memory_status(ms, {"hits": 0, "stored_total": 10, "zero_hit": True})
        assert ms["total_searches"] == 2
        assert ms["total_hits"] == 2  # 总和不变
        assert ms["last_zero_hit_turn"] == 2
        # turn 3: 又有 1 hits
        self._accumulate_memory_status(ms, {"hits": 1, "stored_total": 10, "zero_hit": False})
        assert ms["total_searches"] == 3
        assert ms["total_hits"] == 3
        assert ms["last_zero_hit_turn"] == 2  # 不变


# ──────────────────────────────────────────────────────────────────
# 5. Schema 严校验 —— reject v>current
# ──────────────────────────────────────────────────────────────────

# 64-char hex(SHA-256)用于 test fixtures
_FAKE_SHA256 = "0" * 64


class TestSchemaStrict:

    def test_validate_frontmatter_rejects_future_version(self):
        """未来版本(> CURRENT)→ ValueError,错误信息提到 schema_version"""
        with pytest.raises(ValueError, match="schema_version"):
            validate_frontmatter({
                "type": "user",
                "name": "test",
                "description": "测试描述",
                "schema_version": CURRENT_SCHEMA_VERSION + 99,
                "created_at": "2025-01-01",
                "item_hash": _FAKE_SHA256,
            })

    def test_validate_frontmatter_accepts_current_version(self):
        """CURRENT 版本 + 完整必填字段 → OK"""
        fm = {
            "type": "user",
            "name": "ok",
            "description": "测试描述",
            "schema_version": CURRENT_SCHEMA_VERSION,
            "created_at": "2025-01-01",
            "item_hash": _FAKE_SHA256,
            "importance": 5,
        }
        result = validate_frontmatter(fm)
        assert result["schema_version"] == CURRENT_SCHEMA_VERSION
        assert result["item_hash"] == _FAKE_SHA256

    def test_validate_frontmatter_rejects_missing_item_hash(self):
        """缺 item_hash → ValueError,提示必填字段"""
        with pytest.raises(ValueError, match="item_hash"):
            validate_frontmatter({
                "type": "user",
                "name": "missing hash",
                "description": "测试描述",
                "schema_version": CURRENT_SCHEMA_VERSION,
                "created_at": "2025-01-01",
            })

    def test_validate_frontmatter_rejects_short_item_hash(self):
        """item_hash 不是 64 字符 hex → ValueError"""
        with pytest.raises(ValueError, match="item_hash"):
            validate_frontmatter({
                "type": "user",
                "name": "bad hash",
                "description": "测试描述",
                "schema_version": CURRENT_SCHEMA_VERSION,
                "created_at": "2025-01-01",
                "item_hash": "deadbeef",  # 太短
            })


