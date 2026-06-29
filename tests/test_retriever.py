"""
M11 测试 —— MemoryRetriever (semantic 模式 + L4 密钥过滤)

覆盖:
- semantic 检索 + 排序
- 类型过滤
- L4: 检索时密钥过滤(标记 has_secret)
- 空查询 / 空库
- get_by_hash
- RetrievalReport 接口
- side_query 模式 stub (T7 完善)
"""

from __future__ import annotations

import os
import threading

import pytest

from agent_core.memory import (
    MemoryConfig,
    MemoryRetriever,
    MemoryHit,
    RetrievalReport,
    RetrievalMode,
    RetrievalError,
    MemoryStore,
    ChromaVectorStore,
    SecretScanner,
)


# ──────────────────────────────────────────────────────────────────
# 复用 dual_channel 的 FakeEmbedFn
# ──────────────────────────────────────────────────────────────────

class FakeEmbedFn:
    """确定性 1024 维向量(避开 bge-m3 模型加载)"""

    def encode(self, text: str) -> list[float]:
        # 用文本长度 + 字符累加 + 字符编码 → 1024 维确定性向量
        v = [0.0] * 1024
        v[0] = float(len(text))
        for i, ch in enumerate(text[:64]):
            v[(i + 1) % 1024] = float(ord(ch) % 256) / 256.0
        # 末尾 base
        v[1023] = 1.0
        return v


# ──────────────────────────────────────────────────────────────────
# Fixtures (M11: 用 FakeEmbedFn, 避免 bge-m3 模型加载)
# ──────────────────────────────────────────────────────────────────

@pytest.fixture
def workspace(tmp_path):
    memory_root = tmp_path / "memory"
    memory_root.mkdir()
    chroma_dir = tmp_path / "chroma"
    chroma_dir.mkdir()
    store = MemoryStore(memory_root)
    embed = FakeEmbedFn()
    chroma_path = chroma_dir / f"retriever_{os.getpid()}_{threading.get_ident()}"
    with ChromaVectorStore(str(chroma_path), collection=f"retriever_{tmp_path.name}") as vec:
        yield {"store": store, "vec": vec, "embed": embed}


@pytest.fixture
def populated(workspace):
    """写入 4 条记忆 + 向量化(M11 v3 schema: 必传 name/description)"""
    store = workspace["store"]
    vec = workspace["vec"]
    embed = workspace["embed"]

    # 写入 4 条
    store.write(type="user", name="用户名字", description="用户真名",
                title="用户名字", body="用户叫小明", source_quote="我说'我叫小明'")
    store.write(type="user", name="用户改名", description="改名为大明",
                title="用户改名", body="用户改名为大明", source_quote="我说'我改名了'")
    store.write(type="feedback", name="不喜欢打断", description="不喜欢被打断",
                title="不喜欢打断",
                body="用户不喜欢被打断对话。\n\n**Why:** 打断导致用户思路中断。",
                source_quote="我说'别打断我'")
    store.write(type="project", name="项目用 Python", description="项目主体语言",
                title="项目用 Python",
                body="项目主体是 Python。\n\n**Why:** 用户偏好。",
                source_quote="我说'用 Python'")

    # 向量化
    for type_ in ["user", "feedback", "project"]:
        for it in store.list_by_type(type_):
            rel = it["path"]
            data = store.read(rel)
            text = f"{data['frontmatter'].get('title','')}\n{data['body']}"
            emb = embed.encode(text)
            vec.add(it["hash"], emb)
    return workspace


@pytest.fixture
def retriever(populated):
    return MemoryRetriever(
        memory_store=populated["store"],
        vector_store=populated["vec"],
        embed_fn=populated["embed"],
    )


# ──────────────────────────────────────────────────────────────────
# 基础检索 (M11: 只剩 semantic)
# ──────────────────────────────────────────────────────────────────

class TestBasicSearch:

    def test_semantic_search_finds_relevant(self, retriever):
        report = retriever.search("名字", top_k=3, mode="semantic")
        assert len(report) > 0

    def test_invalid_mode_raises(self, retriever):
        with pytest.raises(RetrievalError):
            retriever.search("test", mode="invalid_mode")

    def test_keyword_mode_rejected(self, retriever):
        """M11:旧 keyword 模式必须抛 RetrievalError"""
        with pytest.raises(RetrievalError):
            retriever.search("test", mode="keyword")

    def test_hybrid_mode_rejected(self, retriever):
        """M11:旧 hybrid 模式必须抛 RetrievalError"""
        with pytest.raises(RetrievalError):
            retriever.search("test", mode="hybrid")

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
        report = retriever.search("用户", top_k=4, mode="semantic")
        for i in range(len(report) - 1):
            assert report[i].score >= report[i + 1].score

    def test_top_k_limits_results(self, retriever):
        report = retriever.search("用户", top_k=2, mode="semantic")
        assert len(report) <= 2

    def test_breakdown_semantic_only(self, retriever):
        """M11:breakdown 只含 semantic,不含 keyword"""
        report = retriever.search("用户", top_k=1, mode="semantic")
        if report.hits:
            assert "semantic" in report[0].breakdown
            assert "keyword" not in report[0].breakdown


# ──────────────────────────────────────────────────────────────────
# 类型过滤
# ──────────────────────────────────────────────────────────────────

class TestTypeFilter:

    def test_filter_user_type(self, retriever):
        report = retriever.search("用户", top_k=10, mode="semantic", types=["user"])
        for h in report:
            assert h.type == "user"

    def test_filter_project_type(self, retriever):
        report = retriever.search("项目", top_k=10, mode="semantic", types=["project"])
        for h in report:
            assert h.type == "project"

    def test_filter_no_match(self, retriever):
        report = retriever.search("用户", top_k=10, mode="semantic", types=["reference"])
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
            type="reference", name="API 文档", description="API 文档",
            title="API 文档",
            body="API 文档见链接 https://example.com",
            source_quote="我说'查 API 文档'"
        )
        # 额外写一条含密钥的
        store.write(
            type="reference", name="Config 示例", description="配置示例",
            title="Config 示例",
            body="我的 key 是 sk-abcdefghijklmnopqrstuvwxyz1234",
            source_quote="示例 config"
        )

        chroma_dir = tmp_path / "chroma_l4"
        chroma_dir.mkdir()
        embed = FakeEmbedFn()
        chroma_path = chroma_dir / f"l4_{os.getpid()}_{threading.get_ident()}"
        with ChromaVectorStore(str(chroma_path), collection="l4_test") as vec:
            for it in store.list_by_type("reference"):
                data = store.read(it["path"])
                text = f"{data['frontmatter'].get('title','')}\n{data['body']}"
                vec.add(it["hash"], embed.encode(text))

            retriever = MemoryRetriever(store, vec, embed)
            report = retriever.search("config", top_k=5, mode="semantic")
            # 找到含 secret 的 hit
            secret_hit = next((h for h in report if h.has_secret), None)
            assert secret_hit is not None
            assert "sk-" in secret_hit.body

    def test_clean_hit_not_marked(self, retriever):
        report = retriever.search("用户", top_k=3, mode="semantic")
        for h in report:
            assert h.has_secret is False


# ──────────────────────────────────────────────────────────────────
# get_by_hash
# ──────────────────────────────────────────────────────────────────

class TestGetByHash:

    def test_get_existing_hit(self, retriever):
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
        report = retriever.search("用户", top_k=3, mode="semantic")
        titles = [h.title for h in report]
        assert len(titles) == len(report)

    def test_report_top(self, retriever):
        report = retriever.search("用户", top_k=3, mode="semantic")
        top2 = report.top(2)
        assert len(top2) <= 2

    def test_report_metadata(self, retriever):
        report = retriever.search("用户", top_k=3, mode="semantic")
        assert report.query == "用户"
        assert report.mode == RetrievalMode.SEMANTIC
        assert report.elapsed_ms > 0
        assert report.total_candidates >= 0


# ──────────────────────────────────────────────────────────────────
# semantic 模式 + 空 vec
# ──────────────────────────────────────────────────────────────────

class TestSemanticEmpty:

    def test_semantic_returns_empty_when_vec_empty(self, tmp_path):
        """semantic 模式:vec 空时返空(不静默降级)"""
        memory_root = tmp_path / "memory"
        memory_root.mkdir()
        store = MemoryStore(memory_root)
        store.write(type="user", name="用户", description="用户真名",
                    title="用户", body="用户叫小明", source_quote="q")

        chroma_dir = tmp_path / "chroma_empty2"
        chroma_dir.mkdir()
        chroma_path = chroma_dir / f"empty_{os.getpid()}_{threading.get_ident()}"
        with ChromaVectorStore(str(chroma_path), collection="empty_test2") as vec:
            embed = FakeEmbedFn()
            retriever = MemoryRetriever(store, vec, embed)
            # 纯 semantic:vec 空 → 空报告(M11 不降级到 keyword)
            report = retriever.search("用户", top_k=1, mode="semantic")
            assert len(report) == 0


# ──────────────────────────────────────────────────────────────────
# semantic 检索字段 (workspace 注入)
# ──────────────────────────────────────────────────────────────────

def test_semantic_hit_metadata_from_memory_store_only(workspace):
    """retriever 的 MemoryHit.title/tags/importance 必须从 MemoryStore 读"""
    store = workspace["store"]
    vec = workspace["vec"]
    embed = workspace["embed"]
    store.write(type="user", name="测试", description="测试描述",
                title="测试", body="内容", source_quote="src")
    it = store.list_by_type("user")[0]
    data = store.read(it["path"])
    text = f"{data['frontmatter'].get('title','')}\n{data['body']}"
    vec.add(it["hash"], embed.encode(text))

    retriever = MemoryRetriever(store, vec, embed)
    report = retriever.search("测试", top_k=5, mode="semantic")
    if report.hits:
        hit = report[0]
        assert hit.title
        assert hit.rel_path.startswith("user/")

# ──────────────────────────────────────────────────────────────────
# M11: side_query 模式
# ──────────────────────────────────────────────────────────────────

class FakeLLMRouter:
    """T7 测试用 stub LLM router, 返回固定 JSON"""

    def __init__(self, selected_paths: list[str] | None = None,
                 raise_exc: bool = False):
        self.selected_paths = selected_paths or []
        self.raise_exc = raise_exc
        self.calls: list = []

    def chat(self, messages, cache_namespace=None):
        self.calls.append({"messages": messages,
                           "cache_namespace": cache_namespace})
        if self.raise_exc:
            raise RuntimeError("simulated LLM failure")
        payload = (
            '{"selected_paths": [' +
            ", ".join(f'"{p}"' for p in self.selected_paths) +
            ']}'
        )
        chunk = type("Chunk", (), {})()
        td = type("TD", (), {})()
        td.text = payload
        chunk.text_delta = td
        yield chunk

    def invoke(self, messages, *, cache_namespace=None, **kwargs):
        """invoke() 收归 chat() + chunk 聚合 — 测试用 stub 走与 chat() 相同路径。"""
        chunks = list(self.chat(messages, cache_namespace=cache_namespace))
        return "".join(
            c.text_delta.text for c in chunks
            if getattr(c, "text_delta", None) is not None
        )


@pytest.fixture
def memory_root_with_llm_stub(populated):
    """populated + 一个 FakeLLMRouter"""
    workspace = populated
    workspace["llm_router"] = FakeLLMRouter(selected_paths=[
        "user/记忆1.md", "user/记忆2.md"
    ])
    return workspace


@pytest.fixture
def memory_root_with_broken_llm(populated):
    """LLM 失败的 stub"""
    workspace = populated
    workspace["llm_router"] = FakeLLMRouter(raise_exc=True)
    return workspace


def test_side_query_basic(memory_root_with_llm_stub):
    """sideQuery 模式:LLM 选 path,读全文,构造 MemoryHit"""
    from agent_core.memory.retriever import MemoryRetriever, RetrievalMode
    ms = memory_root_with_llm_stub["store"]
    vec = memory_root_with_llm_stub["vec"]
    embed = memory_root_with_llm_stub["embed"]
    llm_router = memory_root_with_llm_stub["llm_router"]
    retriever = MemoryRetriever(ms, vec, embed, llm_router=llm_router)
    report = retriever.search(
        "用户叫什么", top_k=2, mode=RetrievalMode.SIDE_QUERY
    )
    assert report.mode == RetrievalMode.SIDE_QUERY
    # 即使 LLM stub 返了不存在的 path, retriever 会读不到 → 跳过
    # 但 hits 可能为 0; 至少 mode 正确
    for hit in report.hits:
        assert hit.breakdown == {"side_query": 1.0}


def test_side_query_already_surfaced_filter(memory_root_with_llm_stub):
    """already_surfaced 过滤已展示过的记忆"""
    from agent_core.memory.retriever import MemoryRetriever, RetrievalMode
    ms = memory_root_with_llm_stub["store"]
    vec = memory_root_with_llm_stub["vec"]
    embed = memory_root_with_llm_stub["embed"]
    llm_router = memory_root_with_llm_stub["llm_router"]
    retriever = MemoryRetriever(ms, vec, embed, llm_router=llm_router)

    # 列出真实存在的 path
    user_paths = []
    for t in ("user", "feedback", "project"):
        for it in ms.list_by_type(t):
            user_paths.append(it["path"])

    surfaced = {user_paths[0]} if user_paths else set()
    report = retriever.search(
        "用户", top_k=3, mode=RetrievalMode.SIDE_QUERY,
        already_surfaced=surfaced,
    )
    for hit in report.hits:
        assert hit.rel_path not in surfaced


def test_side_query_failure_returns_empty(memory_root_with_broken_llm):
    """LLM 失败时 sideQuery 降级返空(不抛)"""
    from agent_core.memory.retriever import MemoryRetriever, RetrievalMode
    ms = memory_root_with_broken_llm["store"]
    vec = memory_root_with_broken_llm["vec"]
    embed = memory_root_with_broken_llm["embed"]
    llm_router = memory_root_with_broken_llm["llm_router"]
    retriever = MemoryRetriever(ms, vec, embed, llm_router=llm_router)
    report = retriever.search(
        "user", top_k=2, mode=RetrievalMode.SIDE_QUERY
    )
    assert report.hits == []


def test_side_query_no_llm_router_returns_empty(retriever):
    """无 llm_router 时 sideQuery 降级返空"""
    from agent_core.memory.retriever import RetrievalMode
    report = retriever.search("user", top_k=2, mode=RetrievalMode.SIDE_QUERY)
    assert report.hits == []


def test_side_query_uses_cache_namespace(memory_root_with_llm_stub):
    """sideQuery 调 LLM 时用 cache_namespace=memory_side_query 隔离"""
    from agent_core.memory.retriever import MemoryRetriever, RetrievalMode
    ms = memory_root_with_llm_stub["store"]
    vec = memory_root_with_llm_stub["vec"]
    embed = memory_root_with_llm_stub["embed"]
    llm_router = memory_root_with_llm_stub["llm_router"]
    retriever = MemoryRetriever(ms, vec, embed, llm_router=llm_router)
    retriever.search("user", top_k=1, mode=RetrievalMode.SIDE_QUERY)
    assert llm_router.calls
    assert llm_router.calls[0]["cache_namespace"] == "memory_side_query"


# ──────────────────────────────────────────────────────────────────
# min_score 阈值过滤 (2026-06-26 修复: 之前是 dead param)
# ──────────────────────────────────────────────────────────────────

class TestMinScoreFilter:
    """min_score 从 MemoryConfig.retrieval.min_score 读,过滤低相关 hit

    修复前: search() 收 min_score 但 _semantic_search 完全没用,任何距离都返回
    修复后: _semantic_search 在算完 sim 后,sim < min_score 直接 skip
    """

    def test_min_score_default_keeps_all_hits(self, populated):
        """min_score=0.3 (默认) — FakeEmbedFn 4 条全部 sim > 0.3,全保留"""
        from agent_core.memory.retriever import RetrievalMode
        ms, vec, embed = populated["store"], populated["vec"], populated["embed"]
        r = MemoryRetriever(
            memory_store=ms, vector_store=vec, embed_fn=embed,
            config=MemoryConfig(),
        )
        rep = r.search("用户", top_k=10, mode=RetrievalMode.SEMANTIC)
        # 默认 min_score=0.3 不应该把现有测试的 hit 过滤掉
        assert len(rep.hits) == 4, f"默认 0.3 应保留全部 4 条,实际 {len(rep.hits)}"

    def test_min_score_high_filters_all(self, populated):
        """min_score=0.99 — 全部 hit 被过滤"""
        from agent_core.memory.config import RetrievalConfig
        from agent_core.memory.retriever import RetrievalMode
        ms, vec, embed = populated["store"], populated["vec"], populated["embed"]
        r = MemoryRetriever(
            memory_store=ms, vector_store=vec, embed_fn=embed,
            config=MemoryConfig(retrieval=RetrievalConfig(min_score=0.99)),
        )
        rep = r.search("用户", top_k=10, mode=RetrievalMode.SEMANTIC)
        assert len(rep.hits) == 0, (
            f"min_score=0.99 应过滤全部,实际剩 {len(rep.hits)} hits: "
            f"{[h.title for h in rep.hits]}"
        )

    def test_min_score_partial_filter(self, populated):
        """min_score=0.5 — 介于 0 和 0.99 之间,只保留部分 hit"""
        from agent_core.memory.config import RetrievalConfig
        from agent_core.memory.retriever import RetrievalMode
        ms, vec, embed = populated["store"], populated["vec"], populated["embed"]
        r_low = MemoryRetriever(
            memory_store=ms, vector_store=vec, embed_fn=embed,
            config=MemoryConfig(retrieval=RetrievalConfig(min_score=0.0)),
        )
        r_high = MemoryRetriever(
            memory_store=ms, vector_store=vec, embed_fn=embed,
            config=MemoryConfig(retrieval=RetrievalConfig(min_score=0.5)),
        )
        rep_low = r_low.search("用户", top_k=10, mode=RetrievalMode.SEMANTIC)
        rep_high = r_high.search("用户", top_k=10, mode=RetrievalMode.SEMANTIC)
        # 0.5 一定 ≤ 全量 0.0
        assert len(rep_high.hits) <= len(rep_low.hits)
        # 留下来的每一个 score 都 >= 0.5
        for h in rep_high.hits:
            assert h.score >= 0.5, (
                f"min_score=0.5 时 score={h.score} < 0.5 的 hit 漏过来了"
            )

    def test_min_score_from_env(self, tmp_path, monkeypatch):
        """MEMORY_RETRIEVAL__MIN_SCORE=0.5 从 .env 读到并生效"""
        from agent_core.memory.config import MemoryConfig
        monkeypatch.setenv("MEMORY_RETRIEVAL__MIN_SCORE", "0.5")
        cfg = MemoryConfig.from_env()
        assert cfg.retrieval.min_score == 0.5, (
            f"env 应读到 0.5,实际 {cfg.retrieval.min_score}"
        )
