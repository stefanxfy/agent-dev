"""tests/test_retriever_surfaced.py — _surfaced 封装进 retriever 的去重逻辑（不依赖 chroma）

验证：
- _filter_surfaced 纯逻辑（过滤已 surface 的 entries）
- search() 命中后更新 _surfaced（STAGE 5）
- reset_surfaced() 清空

不依赖 chromadb：用 fake deps 构造 retriever + monkeypatch 内部方法（_retrieve_candidates 等）。
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from agent_core.memory.retriever import MemoryHit, MemoryRetriever
from agent_core.memory.config import MemoryConfig


def _make_hit(rel_path: str, score: float = 0.9) -> MemoryHit:
    return MemoryHit(
        item_hash=f"h_{rel_path}", type="feedback", title=rel_path,
        body="body", rel_path=rel_path, score=score,
    )


def _make_retriever() -> MemoryRetriever:
    """构造 retriever 不依赖 chroma（fake deps；内部方法后续 monkeypatch）。"""
    return MemoryRetriever(
        memory_store=SimpleNamespace(root=Path(".")),
        vector_store=SimpleNamespace(count=lambda: 0),
        embed_fn=SimpleNamespace(model_name="fake"),
        config=MemoryConfig(),
        secret_scanner=MagicMock(),  # 跳过 get_default_scanner()
        llm_router=None,
    )


class TestFilterSurfaced:
    """_filter_surfaced 纯逻辑（跨轮去重核心）。"""

    def test_no_surfaced_returns_all(self):
        r = _make_retriever()
        entries = [SimpleNamespace(rel_path="a"), SimpleNamespace(rel_path="b")]
        assert r._filter_surfaced(entries) == entries

    def test_filters_surfaced(self):
        r = _make_retriever()
        r._surfaced = {"a", "b"}
        entries = [SimpleNamespace(rel_path=p) for p in ("a", "b", "c", "d")]
        result = r._filter_surfaced(entries)
        assert [e.rel_path for e in result] == ["c", "d"]

    def test_all_surfaced_returns_empty(self):
        r = _make_retriever()
        r._surfaced = {"a"}
        entries = [SimpleNamespace(rel_path="a")]
        assert r._filter_surfaced(entries) == []

    def test_entry_without_rel_path_kept(self):
        # getattr 默认 "" —— 无 rel_path 的 entry 不被过滤（防御）
        r = _make_retriever()
        r._surfaced = {"a"}
        entries = [SimpleNamespace(), SimpleNamespace(rel_path="b")]
        result = r._filter_surfaced(entries)
        assert len(result) == 2  # 无 rel_path 的保留 + b 未在 surfaced


class TestSearchUpdatesSurfaced:
    """search() 命中后更新 _surfaced（STAGE 5，封装的核心）。"""

    def test_search_records_surfaced_rel_paths(self, monkeypatch):
        r = _make_retriever()
        hits = [_make_hit("feedback/a.md"), _make_hit("feedback/b.md")]
        monkeypatch.setattr(r, "_retrieve_candidates", lambda *a, **kw: hits)
        monkeypatch.setattr(r, "_rerank", lambda candidates, mode: candidates)
        monkeypatch.setattr(r, "_filter_secrets", lambda ranked: ranked)

        report = r.search("query", mode="side_query")

        assert len(report.hits) == 2
        assert r._surfaced == {"feedback/a.md", "feedback/b.md"}

    def test_search_empty_query_does_not_touch_surfaced(self, monkeypatch):
        r = _make_retriever()
        r._surfaced = {"existing"}
        # 空 query 早退（search 开头 return empty report）
        report = r.search("", mode="side_query")
        assert report.hits == []
        assert r._surfaced == {"existing"}  # 未被改动

    def test_search_no_hits_keeps_surfaced(self, monkeypatch):
        r = _make_retriever()
        r._surfaced = {"existing"}
        monkeypatch.setattr(r, "_retrieve_candidates", lambda *a, **kw: [])
        monkeypatch.setattr(r, "_rerank", lambda candidates, mode: candidates)
        monkeypatch.setattr(r, "_filter_secrets", lambda ranked: ranked)

        report = r.search("q", mode="side_query")
        assert report.hits == []
        assert r._surfaced == {"existing"}  # 无新命中，不更新


class TestResetSurfaced:
    """reset_surfaced() 在 session 边界清空。"""

    def test_reset_clears_set(self):
        r = _make_retriever()
        r._surfaced = {"a", "b"}
        r.reset_surfaced()
        assert r._surfaced == set()

    def test_reset_then_filter_returns_all(self):
        r = _make_retriever()
        r._surfaced = {"a"}
        r.reset_surfaced()
        entries = [SimpleNamespace(rel_path="a"), SimpleNamespace(rel_path="b")]
        assert r._filter_surfaced(entries) == entries


class TestEncapsulation:
    """封装不变量：_surfaced 是 retriever 内部状态，初始为空。"""

    def test_fresh_retriever_has_empty_surfaced(self):
        r = _make_retriever()
        assert r._surfaced == set()
        assert isinstance(r._surfaced, set)
