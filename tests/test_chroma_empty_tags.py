"""
Bug 3 修复测试 — ChromaDB 拒绝空 tags list 的 metadata

复现:extract_candidates 提取出 tags=[] 的 candidate 时,ChromaDB upsert 会抛
  ValueError: Expected metadata list value for key 'tags' to be non-empty in upsert.
导致 _do_extract_candidates 走 except → pending stuck + attempts=1。

修复:chroma_store.add 入口处过滤掉空 list 的 metadata 字段。

不变量:
1. vec.add(... tags=[]) → 成功(tags 字段被剔除,其他字段保留)
2. extract_candidates 走 extractor 返回 tags=[] → 正常写入(memory + vec)
"""
from __future__ import annotations

import hashlib
import os
import threading
from pathlib import Path

import pytest

from agent_core.memory.chroma_store import ChromaVectorStore
from agent_core.memory.dual_channel_writer import (
    DualChannelWriter, TurnMessage, ExtractionCandidate,
)
from agent_core.memory.memory_store import MemoryStore
from agent_core.memory.meta_db import MetaDB


# FakeEmbedFn / 4 fixture → conftest.py(Phase 4 / Step 4.4.7)
# pytest 自动发现 conftest;FakeEmbedFn 在测试函数里显式 import:
from conftest import FakeEmbedFn  # noqa: E402


# ──────────────────────────────────────────────────────────────────
# Bug 3 修复测试
# ──────────────────────────────────────────────────────────────────

class TestBug3FixEmptyTagsMetadata:
    """Bug 3:ChromaDB 不接受空 list 作为 metadata value"""

    def test_vec_add_with_empty_tags_succeeds(
        self, chroma_dir,
    ):
        """Bug 3 修复:vec.add(... tags=[]) → 成功(过滤空 list 字段)"""
        chroma_path = chroma_dir / f"empty_tags_{os.getpid()}_{threading.get_ident()}"
        with ChromaVectorStore(str(chroma_path), collection="empty_tags") as vec:
            embed = FakeEmbedFn()
            # 关键:tags=[] 在旧代码会抛 ValueError
            vec.add({
                "id": "test_id",
                "embedding": embed.encode("test"),
                "metadata": {
                    "type": "user",
                    "title": "no tags",
                    "tags": [],  # ← 触发 Bug 3
                    "session_id": "s1",
                },
                "document": "no tags",
            })

            # 验证:向量被写入,get 能读出来
            result = vec._collection.get(ids=["test_id"])
            assert "test_id" in result["ids"]
            # 验证:tags 字段被剔除(因为空 list)
            meta = result["metadatas"][0]
            assert "tags" not in meta, (
                f"空 tags 应被剔除,实际 metadata={meta}"
            )
            # 其他字段保留
            assert meta["type"] == "user"
            assert meta["title"] == "no tags"
            assert meta["session_id"] == "s1"

    def test_vec_add_with_nonempty_tags_preserved(
        self, chroma_dir,
    ):
        """回归测试:tags=非空 → 仍正常存储,不被过滤"""
        chroma_path = chroma_dir / f"with_tags_{os.getpid()}_{threading.get_ident()}"
        with ChromaVectorStore(str(chroma_path), collection="with_tags") as vec:
            embed = FakeEmbedFn()
            vec.add({
                "id": "test_id_2",
                "embedding": embed.encode("test"),
                "metadata": {
                    "type": "user",
                    "title": "with tags",
                    "tags": ["food", "preference"],
                    "session_id": "s1",
                },
                "document": "with tags",
            })
            result = vec._collection.get(ids=["test_id_2"])
            meta = result["metadatas"][0]
            assert meta["tags"] == ["food", "preference"]

    def test_extract_candidates_with_empty_tags_succeeds(
        self, meta_db_path, memory_root, logs_dir, chroma_dir,
    ):
        """Bug 3 修复:extract_candidates 走 extractor 返回 tags=[] → 正常写入

        完整集成测试:模拟 LLM extractor 返回 tags=[] 的候选,
        验证 persist_turn → extract_candidates 完整路径不会因空 tags 卡住。
        """
        embed = FakeEmbedFn()
        chroma_path = chroma_dir / f"extract_candidates_empty_{os.getpid()}_{threading.get_ident()}"
        meta_db = MetaDB(meta_db_path)
        memory_store = MemoryStore(memory_root)

        with ChromaVectorStore(str(chroma_path), collection="extract_candidates_empty") as vec:
            writer = DualChannelWriter("s1", meta_db, memory_store, vec, embed)

            # 1. persist_turn 写一条 turn
            writer.persist_turn("用户偏好消息", "好的")

            # 2. extract_candidates 提取,extractor 返回 tags=[] 的候选
            messages = [TurnMessage(1, "用户偏好消息", "好的")]
            future = writer.extract_candidates(
                messages,
                llm_extractor=lambda _m: [
                    ExtractionCandidate(
                        type="user",
                        title="无 tag 偏好",
                        body="用户这条偏好没有 tags",
                        source_quote="用户偏好消息",
                        tags=[],  # ← 触发 Bug 3 的关键
                        score=0.7,
                    )
                ],
            )
            result = future.result(timeout=10)

            # 3. 关键断言:候选被成功写入(memory + vec)
            assert result["written"] == 1, (
                f"Bug 3 修复失败:tags=[] 应被允许,实际 written={result}"
            )

            # 4. Phase 4:无 pending 残留 — memory_tasks 走 CAS,无 pending 表
            with meta_db.transaction() as conn:
                pending_rows = conn.execute(
                    "SELECT COUNT(*) FROM memory_tasks WHERE session_id=? AND state='INFLIGHT'",
                    ("s1",),
                ).fetchone()[0]
            assert pending_rows == 0, (
                f"成功路径不应留 INFLIGHT,实际 {pending_rows}"
            )

            # 5. Phase 4:task 已 DONE(cursors 表已 DROP,看 memory_tasks)
            with meta_db.transaction() as conn:
                task_state = conn.execute(
                    "SELECT state FROM memory_tasks WHERE session_id=? AND turn_index=1",
                    ("s1",),
                ).fetchone()[0]
            assert task_state == "DONE"

            writer.shutdown(timeout=3)