"""
Channel B 走 memory_tasks 测试

Phase 2 / Step 2.2.2 — TDD 红 → 绿

- 调 _do_channel_b_extract 后,所有处理过的 task state → DONE,candidates_payload 落盘
- 不再 add_pending(channel_b_extract)
- 不再 remove_pending
- 仍 set_cursor("extract", ...) — Phase 4 才删
- LLM 失败时 task state → FAILED,extraction_error 落盘,attempts 自增
- LLM 失败的 task 不会被旧 recover_pending 看到(它只读 pending_writes 表)
"""
import json
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
    with db.transaction() as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM pending_writes WHERE session_id=?", (session_id,)
        ).fetchone()[0]


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

        def fake_extractor(messages):
            return [_FakeCandidate(type_="user", title="姓名", body="张三")]

        # 直接调 _do_channel_b_extract 内部方法,跳过 extract_cursor 复杂逻辑
        # 但 _do_channel_b_extract 设计上是 private,且需要 to_process / extract_cursor 参数
        # 实际生产中 channel_b_background_extract 入口已封装,我们改用 _do_channel_b_extract 直接测
        from agent_core.memory.dual_channel_writer import TurnMessage
        to_process = [TurnMessage(turn_index=1, user_msg="我叫张三", assistant_resp="记住了")]
        writer.extract_cursor = 0
        writer.daily_cursor = 1  # window = [0, 1] inclusive, 包含 to_process[0]

        # 写一个最小可工作的 _do_channel_b_extract 调用 — 但需要 vector_store
        # 这里我们用 channel_b_background_extract 入口
        from agent_core.memory.dual_channel_writer import ExtractionCandidate
        # 用 list comprehension 让 extractor 返回 ExtractionCandidate 实例
        def real_extractor(messages):
            return [ExtractionCandidate(
                type="user", title="姓名", body="张三",
                source_quote="我叫张三", tags=[], score=0.5,
            )]

        fut = writer.channel_b_background_extract(
            messages=to_process, llm_extractor=real_extractor,
            advance_cursor=True,
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
        to_process = [TurnMessage(turn_index=1, user_msg="x", assistant_resp="y")]
        writer.extract_cursor = 0
        writer.daily_cursor = 1

        def real_extractor(messages):
            return [ExtractionCandidate(
                type="user", title="t", body="b",
                source_quote="x", tags=[], score=0.5,
            )]

        fut = writer.channel_b_background_extract(
            messages=to_process, llm_extractor=real_extractor, advance_cursor=True,
        )
        fut.result(timeout=5)

        # pending_writes 表保持空
        assert _count_pending(db) == 0

    def test_extract_still_sets_extract_cursor(self, tmp_path):
        """set_cursor('extract', ...) 仍写,Phase 4 才删 cursor 表"""
        writer, db, _ = _make_writer(tmp_path)
        tid = db.insert_task(
            session_id="s1", turn_index=5,
            user_msg="x", assistant_resp="y",
            state="PENDING", max_attempts=3,
        )
        from agent_core.memory.dual_channel_writer import TurnMessage, ExtractionCandidate
        to_process = [TurnMessage(turn_index=5, user_msg="x", assistant_resp="y")]
        writer.extract_cursor = 0
        writer.daily_cursor = 5

        def real_extractor(messages):
            return [ExtractionCandidate(
                type="user", title="t", body="b",
                source_quote="x", tags=[], score=0.5,
            )]

        fut = writer.channel_b_background_extract(
            messages=to_process, llm_extractor=real_extractor, advance_cursor=True,
        )
        fut.result(timeout=5)

        # extract_cursor 仍写入 cursors 表
        with db.transaction() as conn:
            row = conn.execute(
                "SELECT cursor_kind, value FROM cursors WHERE session_id=?",
                ("s1",),
            ).fetchall()
        kinds = {r[0]: r[1] for r in row}
        assert "extract" in kinds
        assert kinds["extract"] == 6  # max_processed(5) + 1


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
        writer.extract_cursor = 0
        writer.daily_cursor = 1

        def boom(messages):
            raise RuntimeError("simulated LLM crash")

        # channel_b_background_extract 是 fire-and-forget,需要 try/except
        # 实际它的 future 会 raise,但 writer 已用 .result() 抛
        # 我们用直接调 _do_channel_b_extract 测失败路径更干净
        with pytest.raises(Exception):
            # _do_channel_b_extract 新签名: messages / extractor / advance_cursor
            writer._do_channel_b_extract(
                messages=messages, extractor=boom, advance_cursor=True,
            )

        # 验证:task state → FAILED,extraction_error 含 LLM 错误
        task = db.get_task(tid)
        # Phase 2.2.10b:失败时 mark_failed — state=FAILED, attempts+1,
        # extraction_error 落盘, next_at 设退避
        assert task["state"] == "FAILED"
        assert task["attempts"] >= 1, f"attempts 应该自增但实际 {task['attempts']}"
        assert task["extraction_error"] is not None
        assert "simulated LLM crash" in task["extraction_error"]
