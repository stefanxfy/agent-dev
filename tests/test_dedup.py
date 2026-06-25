"""
语义去重(向量召回 + LLM 判定)测试

- 纯决策:decide_action 三档阈值 + similarity/top_similarity
- LLM 判定器:make_llm_dedup_judge 解析 + 失败放行
- extract_candidates 集成:auto 跳过(不调 LLM)/ 可疑带调 LLM / 不够相似照写
"""
from __future__ import annotations

import hashlib
import os
import threading
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from agent_core.memory.config import DedupConfig
from agent_core.memory.dedup import (
    DedupAction,
    decide_action,
    make_llm_dedup_judge,
    similarity_from_distance,
    top_similarity,
)
from agent_core.memory.dual_channel_writer import (
    DualChannelWriter,
    ExtractionCandidate,
    TurnMessage,
)
from agent_core.memory.memory_store import MemoryStore
from agent_core.memory.meta_db import MetaDB


# ── 纯决策 ─────────────────────────────────────────────────────────

def test_similarity_from_distance_clamps():
    assert similarity_from_distance(0.0) == 1.0
    assert similarity_from_distance(1.0) == 0.0
    assert similarity_from_distance(-1e-9) == 1.0   # 浮点误差钳到 1
    assert similarity_from_distance(1.0000001) == 0.0


def test_top_similarity_empty_is_none():
    assert top_similarity([]) is None
    assert top_similarity([{"distance": 0.1}]) == pytest.approx(0.9)


def test_decide_action_three_bands():
    cfg = DedupConfig(auto_threshold=0.95, judge_floor=0.85)
    assert decide_action(None, cfg) is DedupAction.NEW          # 无召回
    assert decide_action(0.99, cfg) is DedupAction.AUTO_DUPLICATE
    assert decide_action(0.95, cfg) is DedupAction.AUTO_DUPLICATE  # 边界 ≥
    assert decide_action(0.90, cfg) is DedupAction.NEEDS_JUDGE
    assert decide_action(0.85, cfg) is DedupAction.NEEDS_JUDGE     # 边界 ≥
    assert decide_action(0.84, cfg) is DedupAction.NEW
    assert decide_action(0.5, cfg) is DedupAction.NEW


# ── LLM 判定器 ─────────────────────────────────────────────────────

def _router_yielding(text):
    """构造一个 chat() 产出指定文本的假 router"""
    router = MagicMock()
    def chat(messages, **kw):
        chunk = MagicMock()
        chunk.text_delta.text = text
        yield chunk
    router.chat = chat
    return router


def test_llm_judge_parses_is_duplicate_true():
    judge = make_llm_dedup_judge(_router_yielding('{"is_duplicate": true, "reason": "同一事实"}'))
    cand = ExtractionCandidate(type="user", title="周杰伦", body="喜欢周杰伦", source_quote="x")
    assert judge(cand, [{"metadata": {"title": "周杰伦"}, "document": "喜欢周杰伦"}]) is True


def test_llm_judge_parses_false_and_handles_code_fence():
    judge = make_llm_dedup_judge(_router_yielding('```json\n{"is_duplicate": false}\n```'))
    cand = ExtractionCandidate(type="user", title="周深", body="喜欢周深", source_quote="x")
    assert judge(cand, []) is False


def test_llm_judge_failure_returns_false():
    """LLM 返回非 JSON → 解析失败 → 放行(不当重复)"""
    judge = make_llm_dedup_judge(_router_yielding("抱歉我不知道"))
    cand = ExtractionCandidate(type="user", title="x", body="y", source_quote="z")
    assert judge(cand, []) is False


# ── extract_candidates 集成 ─────────────────────────────────────────────────

class _FakeVec:
    """可控 query 结果 + 记录 add 的假向量库"""
    def __init__(self, hits):
        self._hits = hits
        self.added = []
        _collection_name = "fake"
        self._collection_name = _collection_name
        self._path = "fake"

    def query(self, embedding, top_k):
        return self._hits

    def add(self, doc):
        self.added.append(doc)

    def count(self):
        return len(self.added)


class _FakeEmbed:
    dimension = 1024
    model_name = "fake"

    def encode(self, text):
        d = hashlib.sha256(text.encode()).digest()
        return [b / 255.0 for _ in range(32) for b in d]


def _run_one_candidate(tmp_path, hits, *, judge=None, cfg=None):
    """A 写 1 turn → B 提取 1 个候选,返回 (result, writer, vec, md_count)"""
    store = MemoryStore(tmp_path / "mem")
    (tmp_path / "mem").mkdir(exist_ok=True)
    meta = MetaDB(":memory:")
    vec = _FakeVec(hits)
    w = DualChannelWriter(
        session_id="s", meta_db=meta, memory_store=store,
        vector_store=vec, embed_fn=_FakeEmbed(),
        dedup_config=cfg if cfg is not None else DedupConfig(),
        dedup_judge=judge,
    )
    w.persist_turn("我喜欢周杰伦", "已记", turn_index=0)

    def extractor(msgs):
        return [ExtractionCandidate(
            type="user", title="喜欢的歌手：周杰伦",
            body="用户喜欢华语流行音乐歌手周杰伦", source_quote="我喜欢周杰伦",
            tags=[], score=0.9,
        )]

    f = w.extract_candidates([TurnMessage(0, "我喜欢周杰伦", "已记")], llm_extractor=extractor)
    result = f.result(timeout=5)
    md_count = len(list((store.root / "user").glob("*.md"))) if (store.root / "user").exists() else 0
    w.shutdown(timeout=5)
    return result, vec, md_count


def test_auto_duplicate_skips_without_llm(tmp_path):
    """相似度 ≥ 0.95 → 直接跳过,不调 LLM,不写 .md"""
    judge = MagicMock()
    hits = [{"distance": 0.03, "metadata": {"title": "周杰伦"}, "document": "喜欢周杰伦"}]  # sim 0.97
    result, vec, md_count = _run_one_candidate(tmp_path, hits, judge=judge)

    assert result["written"] == 0 and result["skipped"] == 1
    assert md_count == 0, "auto 重复不应写 .md"
    assert vec.added == [], "auto 重复不应写向量"
    judge.assert_not_called()  # 省 token:auto 档不调 LLM


def test_judge_band_calls_llm_and_skips_when_duplicate(tmp_path):
    """相似度在 [0.85, 0.95) → 调 LLM;判重复 → 跳过"""
    judge = MagicMock(return_value=True)
    hits = [{"distance": 0.10, "metadata": {"title": "周杰伦"}, "document": "喜欢周杰伦"}]  # sim 0.90
    result, vec, md_count = _run_one_candidate(tmp_path, hits, judge=judge)

    judge.assert_called_once()
    assert result["written"] == 0 and result["skipped"] == 1
    assert md_count == 0


def test_judge_band_writes_when_not_duplicate(tmp_path):
    """相似度在可疑带 → LLM 判「不重复」→ 照常写盘"""
    judge = MagicMock(return_value=False)
    hits = [{"distance": 0.10, "metadata": {"title": "周深"}, "document": "喜欢周深"}]  # sim 0.90
    result, vec, md_count = _run_one_candidate(tmp_path, hits, judge=judge)

    judge.assert_called_once()
    assert result["written"] == 1
    assert md_count == 1
    assert len(vec.added) == 1


def test_low_similarity_writes_without_llm(tmp_path):
    """相似度 < judge_floor → 新记忆,不调 LLM,正常写"""
    judge = MagicMock()
    hits = [{"distance": 0.40, "metadata": {"title": "无关"}, "document": "明天下雨"}]  # sim 0.60
    result, vec, md_count = _run_one_candidate(tmp_path, hits, judge=judge)

    judge.assert_not_called()
    assert result["written"] == 1
    assert md_count == 1


def test_dedup_disabled_writes_everything(tmp_path):
    """dedup_config.enabled=False → 完全不去重(即便相似度极高也照写)"""
    judge = MagicMock()
    hits = [{"distance": 0.01, "metadata": {"title": "周杰伦"}, "document": "喜欢周杰伦"}]  # sim 0.99
    result, vec, md_count = _run_one_candidate(
        tmp_path, hits, judge=judge, cfg=DedupConfig(enabled=False),
    )
    judge.assert_not_called()
    assert result["written"] == 1
    assert md_count == 1
