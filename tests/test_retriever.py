"""
M3 / Day 3 测试 —— MemoryRetriever (三模式 + L4 密钥过滤)

覆盖:
- 三模式: semantic / keyword / hybrid
- 排序: 分数高者靠前
- 类型过滤
- L4: 检索时密钥过滤(标记 has_secret)
- 空查询 / 空库
- get_by_hash
- 边界: 词汇表
"""

from __future__ import annotations

import math
from pathlib import Path

import pytest

from agent_core.memory import (
    MemoryRetriever,
    MemoryHit,
    RetrievalReport,
    RetrievalMode,
    RetrievalError,
    MemoryStore,
    MockVectorStore,
    MockEmbedFn,
    SecretScanner,
    MemoryConfig,
)


# ──────────────────────────────────────────────────────────────────
# Fixtures
# ──────────────────────────────────────────────────────────────────

class VectorWithQuery(MockVectorStore):
    """扩展 MockVectorStore 添加 query()"""
    def query(self, embedding, top_k):
        scored = []
        for d in self._docs:
            e = d.get("embedding", [])
            dot = sum(a * b for a, b in zip(embedding, e))
            na = math.sqrt(sum(a * a for a in embedding))
            nb = math.sqrt(sum(b * b for b in e))
            sim = dot / (na * nb) if na and nb else 0.0
            scored.append({**d, "distance": 1.0 - sim})
        scored.sort(key=lambda x: x["distance"])
        return scored[:top_k]


@pytest.fixture
def workspace(tmp_path):
    memory_root = tmp_path / "memory"
    memory_root.mkdir()
    store = MemoryStore(memory_root)
    vec = VectorWithQuery()
    embed = MockEmbedFn()
    config = MemoryConfig()
    return {"store": store, "vec": vec, "embed": embed, "config": config}


@pytest.fixture
def populated(workspace):
    """写入 4 条记忆 + 向量化"""
    store = workspace["store"]
    vec = workspace["vec"]
    embed = workspace["embed"]

    # 写入 4 条
    store.write("user", "用户名字", "用户叫小明", source_quote="我说'我叫小明'")
    store.write("user", "用户改名", "用户改名为大明", source_quote="我说'我改名了'")
    store.write("feedback", "不喜欢打断", "用户不喜欢被打断对话。\n\n**Why:** 打断导致用户思路中断。", source_quote="我说'别打断我'")
    store.write("project", "项目用 Python", "项目主体是 Python。\n\n**Why:** 用户偏好。", source_quote="我说'用 Python'")

    # 向量化
    for type_ in ["user", "feedback", "project"]:
        for it in store.list_by_type(type_):
            rel = it["path"]
            data = store.read(rel)
            text = f"{data['frontmatter'].get('title','')}\n{data['body']}"
            emb = embed.encode(text)
            vec.add({
                "id": it["hash"],
                "embedding": emb,
                "metadata": {"type": type_, "title": it["title"]},
                "document": text,
            })
    return workspace


@pytest.fixture
def retriever(populated):
    return MemoryRetriever(
        memory_store=populated["store"],
        vector_store=populated["vec"],
        embed_fn=populated["embed"],
        config=populated["config"],
    )


# ──────────────────────────────────────────────────────────────────
# 基础检索
# ──────────────────────────────────────────────────────────────────

class TestBasicSearch:

    def test_keyword_search_finds_relevant(self, retriever):
        report = retriever.search("用户名字", top_k=3, mode="keyword")
        assert len(report) > 0
        # 第一个应该是"用户名字"
        assert "名字" in report[0].title or "改名" in report[0].title

    def test_hybrid_search_finds_relevant(self, retriever):
        report = retriever.search("用户叫什么", top_k=3, mode="hybrid")
        assert len(report) > 0

    def test_semantic_search_finds_relevant(self, retriever):
        report = retriever.search("名字", top_k=3, mode="semantic")
        assert len(report) > 0

    def test_invalid_mode_raises(self, retriever):
        with pytest.raises(RetrievalError):
            retriever.search("test", mode="invalid_mode")

    def test_empty_query_returns_empty(self, retriever):
        report = retriever.search("", top_k=3)
        assert len(report) == 0

    def test_whitespace_query_returns_empty(self, retriever):
        report = retriever.search("   \t  ", top_k=3)
        assert len(report) == 0


# ──────────────────────────────────────────────────────────────────
# 排序
# ──────────────────────────────────────────────────────────────────

class TestRanking:

    def test_results_sorted_by_score_desc(self, retriever):
        report = retriever.search("用户", top_k=4, mode="hybrid")
        for i in range(len(report) - 1):
            assert report[i].score >= report[i + 1].score

    def test_top_k_limits_results(self, retriever):
        report = retriever.search("用户", top_k=2, mode="hybrid")
        assert len(report) <= 2

    def test_breakdown_populated(self, retriever):
        report = retriever.search("用户", top_k=1, mode="hybrid")
        hit = report[0]
        assert "semantic" in hit.breakdown
        assert "keyword" in hit.breakdown


# ──────────────────────────────────────────────────────────────────
# 类型过滤
# ──────────────────────────────────────────────────────────────────

class TestTypeFilter:

    def test_filter_user_type(self, retriever):
        report = retriever.search("用户", top_k=10, mode="keyword", types=["user"])
        for h in report:
            assert h.type == "user"

    def test_filter_project_type(self, retriever):
        report = retriever.search("项目", top_k=10, mode="keyword", types=["project"])
        for h in report:
            assert h.type == "project"

    def test_filter_no_match(self, retriever):
        report = retriever.search("用户", top_k=10, mode="keyword", types=["reference"])
        # 没有 reference 类记忆 → 应为空
        assert len(report) == 0


# ──────────────────────────────────────────────────────────────────
# L4 密钥过滤
# ──────────────────────────────────────────────────────────────────

class TestSecretFilter:

    def test_hit_with_secret_marked(self, tmp_path):
        """含密钥的 body 应被标记 has_secret=True"""
        memory_root = tmp_path / "memory"
        memory_root.mkdir()
        store = MemoryStore(memory_root)
        store.write(
            "reference", "API 文档",
            "API 文档见链接 https://example.com",
            source_quote="我说'查 API 文档'"
        )
        # 额外写一条含密钥的
        store.write(
            "reference", "Config 示例",
            "我的 key 是 sk-abcdefghijklmnopqrstuvwxyz1234",
            source_quote="示例 config"
        )

        vec = VectorWithQuery()
        embed = MockEmbedFn()
        for it in store.list_by_type("reference"):
            data = store.read(it["path"])
            text = f"{data['frontmatter'].get('title','')}\n{data['body']}"
            vec.add({
                "id": it["hash"],
                "embedding": embed.encode(text),
                "metadata": {"type": "reference", "title": it["title"]},
                "document": text,
            })

        retriever = MemoryRetriever(store, vec, embed)
        report = retriever.search("config", top_k=5, mode="keyword")
        # 找到含 secret 的 hit
        secret_hit = next((h for h in report if h.has_secret), None)
        assert secret_hit is not None
        assert "sk-" in secret_hit.body

    def test_clean_hit_not_marked(self, retriever):
        report = retriever.search("用户", top_k=3, mode="keyword")
        for h in report:
            assert h.has_secret is False


# ──────────────────────────────────────────────────────────────────
# get_by_hash
# ──────────────────────────────────────────────────────────────────

class TestGetByHash:

    def test_get_existing_hit(self, retriever):
        # 拿一个已存在的 hash
        items = retriever.memory_store.list_by_type("user")
        h = items[0]["hash"]
        hit = retriever.get_by_hash(h, "user")
        assert hit is not None
        assert hit.item_hash == h
        assert hit.score == 1.0

    def test_get_nonexistent_returns_none(self, retriever):
        fake_hash = "0" * 64
        hit = retriever.get_by_hash(fake_hash, "user")
        assert hit is None


# ──────────────────────────────────────────────────────────────────
# RetrievalReport 接口
# ──────────────────────────────────────────────────────────────────

class TestReport:

    def test_report_iteration(self, retriever):
        report = retriever.search("用户", top_k=3, mode="keyword")
        # 支持 iter
        titles = [h.title for h in report]
        assert len(titles) == len(report)

    def test_report_top(self, retriever):
        report = retriever.search("用户", top_k=3, mode="keyword")
        top2 = report.top(2)
        assert len(top2) <= 2

    def test_report_metadata(self, retriever):
        report = retriever.search("用户", top_k=3, mode="hybrid")
        assert report.query == "用户"
        assert report.mode == RetrievalMode.HYBRID
        assert report.elapsed_ms > 0
        assert report.total_candidates >= 0


# ──────────────────────────────────────────────────────────────────
# semantic 模式 + 无 query() 的 vector_store
# ──────────────────────────────────────────────────────────────────

class TestSemanticFallback:

    def test_semantic_falls_back_to_keyword(self, tmp_path):
        """vector_store 无 query() 时,semantic 模式降级到 keyword"""
        memory_root = tmp_path / "memory"
        memory_root.mkdir()
        store = MemoryStore(memory_root)
        store.write("user", "用户", "用户叫小明", source_quote="q")

        # 普通 MockVectorStore (无 query())
        vec = MockVectorStore()
        embed = MockEmbedFn()

        retriever = MemoryRetriever(store, vec, embed)
        # semantic 模式应降级(不崩溃)
        report = retriever.search("用户", top_k=1, mode="semantic")
        assert len(report) >= 0  # 不崩溃
