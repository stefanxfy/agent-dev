"""
M11 e2e: L1 MEMORY.md 启动加载 + L2 side_query 召回 + 写盘触发 rebuild
"""

from __future__ import annotations

import os
import threading
import time
from types import SimpleNamespace

import pytest

from agent_core.memory import (
    MemoryRetriever, MemoryStore, ChromaVectorStore, RetrievalMode,
)


# ──────────────────────────────────────────────────────────────────
# Helpers (M11: 不依赖 bge-m3)
# ──────────────────────────────────────────────────────────────────

class FakeEmbedFn:
    """确定性 1024 维向量(避开 bge-m3)"""

    def encode(self, text: str) -> list[float]:
        v = [0.0] * 1024
        v[0] = float(len(text))
        for i, ch in enumerate(text[:64]):
            v[(i + 1) % 1024] = float(ord(ch) % 256) / 256.0
        v[1023] = 1.0
        return v


class FakeLLMRouter:
    """sideQuery 用:返回固定 JSON 列出 selected_paths"""

    def __init__(self, selected_paths: list[str] | None = None):
        self.selected_paths = selected_paths or []
        self.calls: list = []

    def chat(self, messages, cache_namespace=None):
        self.calls.append({"cache_namespace": cache_namespace})
        payload = (
            '{"selected_paths": [' +
            ", ".join(f'"{p}"' for p in self.selected_paths) +
            ']}'
        )
        chunk = SimpleNamespace()
        td = SimpleNamespace()
        td.text = payload
        chunk.text_delta = td
        yield chunk


def _make_fake_vec():
    """空 vec 容器(sideQuery 模式不需要 vec)"""
    from agent_core.memory import ChromaVectorStore
    chroma_dir = f"/tmp/e2e_vec_{os.getpid()}_{threading.get_ident()}"
    os.makedirs(chroma_dir, exist_ok=True)
    return ChromaVectorStore(
        chroma_dir, collection=f"e2e_{os.getpid()}_{threading.get_ident()}"
    )


# ──────────────────────────────────────────────────────────────────
# L1 启动加载
# ──────────────────────────────────────────────────────────────────

def test_e2e_l1_loads_memory_index(tmp_path):
    """L1:启动后 MEMORY.md 含全部 3 条记忆"""
    from agent_core.memory.memory_index import MemoryIndex

    memory_root = tmp_path / "memory"
    store = MemoryStore(memory_root)
    store.write(type="user", name="用户叫小明",
                description="Python 后端工程师,深圳",
                body="小明是 Python 后端工程师,深圳",
                source_quote="我说'我是 Python 工程师'")
    store.write(type="feedback", name="不要 mock DB",
                description="学习阶段 mock 会掩盖真实行为",
                body="学习阶段不要 mock DB。\n\n**Why:** mock 掩盖真实行为。",
                source_quote="我说'不要 mock'")
    store.write(type="project", name="Go REST 项目结构",
                description="用了 chi + sqlx + testify",
                body="项目用 chi + sqlx + testify 组合。\n\n**Why:** 团队偏好。",
                source_quote="我说'用 chi + sqlx'")

    index = MemoryIndex(memory_root)
    index.rebuild()  # 同步 rebuild

    content = index.load_index()
    assert "用户叫小明" in content
    assert "不要 mock DB" in content
    assert "Go REST 项目结构" in content


# ──────────────────────────────────────────────────────────────────
# L2 sideQuery 召回
# ──────────────────────────────────────────────────────────────────

def test_e2e_l2_side_query_returns_selected(tmp_path):
    """L2:sideQuery 让 LLM 选 path,读全文命中"""
    memory_root = tmp_path / "memory"
    store = MemoryStore(memory_root)
    store.write(type="user", name="用户叫小明",
                description="Python 后端工程师,深圳",
                body="小明是 Python 工程师",
                source_quote="小明")
    store.write(type="feedback", name="不要 mock DB",
                description="学习阶段 mock 会掩盖真实行为",
                body="不要 mock DB\n\n**Why:** mock 掩盖行为。",
                source_quote="不要 mock")
    store.write(type="project", name="Go REST 项目结构",
                description="chi + sqlx + testify",
                body="Go REST 项目结构\n\n**Why:** 团队偏好。",
                source_quote="chi + sqlx")

    # 拿真实 rel_path
    user_path = store.list_by_type("user")[0]["path"]
    feedback_path = store.list_by_type("feedback")[0]["path"]
    project_path = store.list_by_type("project")[0]["path"]

    # sideQuery 走主 LLM router;但模式不走向量
    with _make_fake_vec() as vec:
        retriever = MemoryRetriever(
            memory_root if False else store,  # 显式 store
            vec, FakeEmbedFn(),
            llm_router=FakeLLMRouter(selected_paths=[user_path, feedback_path]),
        )
        report = retriever.search(
            "用户身份", top_k=2, mode=RetrievalMode.SIDE_QUERY
        )

    assert report.mode == RetrievalMode.SIDE_QUERY
    assert len(report.hits) == 2
    titles = {h.title for h in report.hits}
    assert "用户叫小明" in titles
    assert "不要 mock DB" in titles
    # 未被选的 project 不会出现
    assert "Go REST 项目结构" not in titles


# ──────────────────────────────────────────────────────────────────
# L2 sideQuery:already_surfaced 过滤
# ──────────────────────────────────────────────────────────────────

def test_e2e_side_query_already_surfaced_filter(tmp_path):
    """sideQuery:已展示过的 path 在 manifest 中被过滤,LLM 看不到"""
    from agent_core.memory.memory_index import MemoryIndex

    memory_root = tmp_path / "memory"
    store = MemoryStore(memory_root)
    store.write(type="user", name="记忆A",
                description="d", body="A", source_quote="a")
    store.write(type="user", name="记忆B",
                description="d", body="B", source_quote="b")
    path_a = store.list_by_type("user")[0]["path"]
    path_b = store.list_by_type("user")[1]["path"]

    # 先 rebuild 让 manifest 出现
    MemoryIndex(memory_root).rebuild()

    # LLM stub 知道 manifest 被过滤了,只返 path_b
    with _make_fake_vec() as vec:
        retriever = MemoryRetriever(
            store, vec, FakeEmbedFn(),
            llm_router=FakeLLMRouter(selected_paths=[path_b]),
        )
        report = retriever.search(
            "x", top_k=2, mode=RetrievalMode.SIDE_QUERY,
            already_surfaced={path_a},
        )
    paths = {h.rel_path for h in report.hits}
    assert path_a not in paths
    assert path_b in paths

    # 反向验证:无 already_surfaced 时,LLM 即使返 path_a 也能被读到
    with _make_fake_vec() as vec:
        retriever2 = MemoryRetriever(
            store, vec, FakeEmbedFn(),
            llm_router=FakeLLMRouter(selected_paths=[path_a, path_b]),
        )
        report2 = retriever2.search(
            "x", top_k=2, mode=RetrievalMode.SIDE_QUERY,
        )
    paths2 = {h.rel_path for h in report2.hits}
    assert path_a in paths2


# ──────────────────────────────────────────────────────────────────
# 写盘后 MEMORY.md 异步 rebuild (1s)
# ──────────────────────────────────────────────────────────────────

def test_e2e_write_triggers_index_rebuild(tmp_path):
    """写盘后 MEMORY.md 1.1s 内更新"""
    from agent_core.memory.memory_index import MemoryIndex

    memory_root = tmp_path / "memory"
    store = MemoryStore(memory_root)
    index = MemoryIndex(memory_root)

    store.write(type="user", name="新记忆",
                description="新描述", body="b", source_quote="b")
    index.mark_dirty()

    time.sleep(1.2)
    content = (memory_root / "MEMORY.md").read_text(encoding="utf-8")
    assert "新记忆" in content


# ──────────────────────────────────────────────────────────────────
# L1 + L2 + already_surfaced 联动
# ──────────────────────────────────────────────────────────────────

def test_e2e_l1_l2_already_surfaced_round_trip(tmp_path):
    """完整链路:L1 加载 → L2 检索 → 已展示去重"""
    from agent_core.memory.memory_index import MemoryIndex

    memory_root = tmp_path / "memory"
    store = MemoryStore(memory_root)
    store.write(type="user", name="小明",
                description="Python 工程师", body="x", source_quote="x")
    store.write(type="feedback", name="不要 mock",
                description="mock 掩盖行为",
                body="不要 mock\n\n**Why:** mock 掩盖行为。",
                source_quote="y")
    p_user = store.list_by_type("user")[0]["path"]
    p_feed = store.list_by_type("feedback")[0]["path"]

    # L1:启动加载
    idx = MemoryIndex(memory_root)
    idx.rebuild()
    assert "小明" in idx.load_index()

    # L2:第一次检索 → 2 条
    surfaced: set[str] = set()
    with _make_fake_vec() as vec:
        retriever = MemoryRetriever(
            store, vec, FakeEmbedFn(),
            llm_router=FakeLLMRouter(selected_paths=[p_user, p_feed]),
        )
        r1 = retriever.search("?", top_k=2, mode=RetrievalMode.SIDE_QUERY)
        for h in r1.hits:
            surfaced.add(h.rel_path)
        assert len(r1.hits) == 2

        # 第二次:同 query,但 LLM 仍可能再选 user → 应被去重
        retriever2 = MemoryRetriever(
            store, vec, FakeEmbedFn(),
            llm_router=FakeLLMRouter(selected_paths=[p_user, p_feed]),
        )
        r2 = retriever2.search(
            "?", top_k=2, mode=RetrievalMode.SIDE_QUERY,
            already_surfaced=surfaced,
        )
        paths2 = {h.rel_path for h in r2.hits}
        assert p_user not in paths2
        # 仅剩未展示过的
        assert paths2 <= {p_feed}