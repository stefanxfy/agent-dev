"""
Channel B 走 memory_tasks 测试

Phase 2 / Step 2.2.2 + Phase 4 适配 — TDD 红 → 绿

- 调 channel_b_background_extract 后,所有处理过的 task state → DONE,candidates_payload 落盘
- 不再 add_pending / remove_pending / bump_pending_attempts
- 不再 set_cursor("extract", ...)(Phase 4 已删)
- LLM 失败时 task state → FAILED,extraction_error 落盘,attempts 自增
- channel_b_background_extract 不再有 advance_cursor 参数(recover_pending 已删)
"""
import json
import sqlite3
import time
import threading
import pytest


# ─── 测试夹具 ───

class _FakeCandidate:
    def __init__(self, type_="user", title="t", body="b", tags=None, source_quote=""):
        self.type = type_
        self.title = title
        self.body = body
        self.tags = tags or []
        self.source_quote = source_quote
        self.score = 0.5


class _FakeVectorStore:
    """最小 stub:只接 add(),存到内存(Phase 2.2.10b Channel B 走新表测试用)。"""
    def __init__(self):
        self.items: list[dict] = []

    def add(self, item: dict) -> None:
        self.items.append(item)

    def query(self, embedding, top_k=5):
        return []  # 语义去重不参与,空 list = 无命中


def _make_writer(tmp_path, *, session_id="s1"):
    from agent_core.memory.dual_channel_writer import DualChannelWriter
    from agent_core.memory.meta_db import MetaDB
    from agent_core.memory.memory_store import MemoryStore
    from tests.test_dual_channel_concurrent import FakeEmbedFn

    db = MetaDB(":memory:")
    store = MemoryStore(tmp_path / "memory")
    embed = FakeEmbedFn()
    writer = DualChannelWriter(
        session_id=session_id,
        meta_db=db,
        memory_store=store,
        vector_store=_FakeVectorStore(),  # Phase 2.2.10b 走新表,需要 vector_store.add
        embed_fn=embed,
    )
    return writer, db, embed


def _list_tasks(db, session_id="s1"):
    with db.transaction() as conn:
        return conn.execute(
            "SELECT turn_index, state, candidates_payload, extraction_error, attempts "
            "FROM memory_tasks WHERE session_id=? ORDER BY turn_index",
            (session_id,),
        ).fetchall()


def _count_pending(db, session_id="s1"):
    """Phase 4:pending_writes 表已 DROP,总是返回 0"""
    try:
        with db.transaction() as conn:
            return conn.execute(
                "SELECT COUNT(*) FROM pending_writes WHERE session_id=?", (session_id,)
            ).fetchone()[0]
    except sqlite3.OperationalError:
        return 0  # 表不存在 → 等价 0 行


# ─── Step 2.2.2 测试 ───

class TestChannelBMarksDoneWithCandidates:
    """Channel B 成功后 task state → DONE,candidates_payload 落盘"""

    def test_successful_extract_marks_task_done(self, tmp_path):
        writer, db, _ = _make_writer(tmp_path)
        # 准备 1 个 NONE task(模拟 Channel A 刚落盘)
        tid = db.insert_task(
            session_id="s1", turn_index=1,
            user_msg="我叫张三", assistant_resp="记住了",
            state="NONE", max_attempts=3,
        )
        # 改用 PENDING(走 channel_b 的 CAS 起点)
        db.update_task_state(tid, "PENDING")

        # 直接调 channel_b_background_extract — Phase 4 无 advance_cursor 参数
        from agent_core.memory.dual_channel_writer import TurnMessage, ExtractionCandidate
        messages = [TurnMessage(turn_index=1, user_msg="我叫张三", assistant_resp="记住了")]

        def real_extractor(_msgs):
            return [ExtractionCandidate(
                type="user", title="姓名", body="张三",
                source_quote="我叫张三", tags=[], score=0.5,
            )]

        fut = writer.channel_b_background_extract(
            messages=messages, llm_extractor=real_extractor,
        )
        result = fut.result(timeout=5)

        # 验证:task 已 DONE,candidates 落盘
        task = db.get_task(tid)
        assert task["state"] == "DONE"
        assert task["candidates_payload"] is not None
        payload = json.loads(task["candidates_payload"])
        assert len(payload) == 1
        assert payload[0]["title"] == "姓名"
        assert payload[0]["body"] == "张三"

    def test_extract_does_not_write_to_pending_table(self, tmp_path):
        """不再 add_pending / remove_pending(pending_writes 表保持空)"""
        writer, db, _ = _make_writer(tmp_path)
        tid = db.insert_task(
            session_id="s1", turn_index=1,
            user_msg="x", assistant_resp="y",
            state="PENDING", max_attempts=3,
        )
        from agent_core.memory.dual_channel_writer import TurnMessage, ExtractionCandidate
        messages = [TurnMessage(turn_index=1, user_msg="x", assistant_resp="y")]

        def real_extractor(_msgs):
            return [ExtractionCandidate(
                type="user", title="t", body="b",
                source_quote="x", tags=[], score=0.5,
            )]

        fut = writer.channel_b_background_extract(
            messages=messages, llm_extractor=real_extractor,
        )
        fut.result(timeout=5)

        # pending_writes 表保持空
        assert _count_pending(db) == 0

    def test_extract_does_not_write_to_cursors_table(self, tmp_path):
        """Phase 4:set_cursor 已删,Channel B 不再写 cursors 表"""
        writer, db, _ = _make_writer(tmp_path)
        tid = db.insert_task(
            session_id="s1", turn_index=5,
            user_msg="x", assistant_resp="y",
            state="PENDING", max_attempts=3,
        )
        from agent_core.memory.dual_channel_writer import TurnMessage, ExtractionCandidate
        messages = [TurnMessage(turn_index=5, user_msg="x", assistant_resp="y")]

        def real_extractor(_msgs):
            return [ExtractionCandidate(
                type="user", title="t", body="b",
                source_quote="x", tags=[], score=0.5,
            )]

        fut = writer.channel_b_background_extract(
            messages=messages, llm_extractor=real_extractor,
        )
        fut.result(timeout=5)

        # cursors 表无任何行(Phase 4 表已 DROP,任何写都不可能)
        try:
            with db.transaction() as conn:
                cursor_count = conn.execute(
                    "SELECT COUNT(*) FROM cursors WHERE session_id=?",
                    ("s1",),
                ).fetchone()[0]
            assert cursor_count == 0, f"Channel B 不应再写 cursors,实际 {cursor_count}"
        except sqlite3.OperationalError:
            # 表已 DROP → 表不存在等价 0 行 ✅
            pass


class TestChannelBFailureMarksTaskFailed:
    """LLM 失败时 task state → FAILED,extraction_error 落盘,attempts 自增"""

    def test_failure_marks_task_failed(self, tmp_path):
        writer, db, _ = _make_writer(tmp_path)
        tid = db.insert_task(
            session_id="s1", turn_index=1,
            user_msg="x", assistant_resp="y",
            state="PENDING", max_attempts=3,
        )
        from agent_core.memory.dual_channel_writer import TurnMessage, DualChannelError
        messages = [TurnMessage(turn_index=1, user_msg="x", assistant_resp="y")]

        def boom(_msgs):
            raise RuntimeError("simulated LLM crash")

        with pytest.raises(Exception):
            # Phase 4 新签名: messages / extractor(无 advance_cursor)
            writer._do_channel_b_extract(messages=messages, extractor=boom)

        # 验证:task state → FAILED,extraction_error 含 LLM 错误
        task = db.get_task(tid)
        assert task["state"] == "FAILED"
        assert task["attempts"] >= 1, f"attempts 应该自增但实际 {task['attempts']}"
        assert task["extraction_error"] is not None
        assert "simulated LLM crash" in task["extraction_error"]
