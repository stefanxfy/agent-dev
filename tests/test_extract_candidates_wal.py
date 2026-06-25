"""
extract_candidates 走 memory_tasks 测试

Phase 2 / Step 2.2.2 + Phase 4 适配 — TDD 红 → 绿

- 调 extract_candidates 后,所有处理过的 task state → DONE,candidates_payload 落盘
- 不再 add_pending / remove_pending / bump_pending_attempts
- 不再 set_cursor("extract", ...)(Phase 4 已删)
- LLM 失败时 task state → FAILED,extraction_error 落盘,attempts 自增
- extract_candidates 不再有 advance_cursor 参数(recover_pending 已删)
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
    """T1/T2 锁定后的新契约:add(id, embedding) / query()→[{id, distance}]"""
    def __init__(self):
        self.items: list[tuple[str, list[float]]] = []  # [(id, embedding), ...]

    def add(self, id: str, embedding: list[float]) -> None:
        self.items.append((id, embedding))

    def query(self, embedding, top_k=5):
        # 按 id 字母序选 top_k(简单 mock,不需要真算相似度)
        sorted_items = sorted(self.items, key=lambda x: x[0])
        return [
            {"id": item[0], "distance": 0.1 * i}
            for i, item in enumerate(sorted_items[:top_k])
        ]

    def count(self):
        return len(self.items)


def _make_writer(tmp_path, *, session_id="s1"):
    from agent_core.memory.dual_channel_writer import DualChannelWriter
    from agent_core.memory.meta_db import MetaDB
    from agent_core.memory.memory_store import MemoryStore
    from conftest import FakeEmbedFn

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
    """extract_candidates 成功后 task state → DONE,candidates_payload 落盘"""

    def test_successful_extract_marks_task_done(self, tmp_path):
        writer, db, _ = _make_writer(tmp_path)
        # 准备 1 个 NONE task(模拟 persist_turn 刚落盘)
        tid = db.insert_task(
            session_id="s1", turn_index=1,
            user_msg="我叫张三", assistant_resp="记住了",
            state="NONE", max_attempts=3,
        )
        # 改用 PENDING(走 extract_candidates 的 CAS 起点)
        db.update_task_state(tid, "PENDING")

        # 直接调 extract_candidates — Phase 4 无 advance_cursor 参数
        from agent_core.memory.dual_channel_writer import TurnMessage, ExtractionCandidate
        messages = [TurnMessage(turn_index=1, user_msg="我叫张三", assistant_resp="记住了")]

        def real_extractor(_msgs):
            return [ExtractionCandidate(
                type="user", title="姓名", body="张三",
                source_quote="我叫张三", tags=[], score=0.5,
            )]

        fut = writer.extract_candidates(
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

        fut = writer.extract_candidates(
            messages=messages, llm_extractor=real_extractor,
        )
        fut.result(timeout=5)

        # pending_writes 表保持空
        assert _count_pending(db) == 0

    def test_extract_does_not_write_to_cursors_table(self, tmp_path):
        """Phase 4:set_cursor 已删,extract_candidates 不再写 cursors 表"""
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

        fut = writer.extract_candidates(
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
            assert cursor_count == 0, f"extract_candidates 不应再写 cursors,实际 {cursor_count}"
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
            writer._do_extract_candidates(messages=messages, extractor=boom)

        # 验证:task state → FAILED,extraction_error 含 LLM 错误
        task = db.get_task(tid)
        assert task["state"] == "FAILED"
        assert task["attempts"] >= 1, f"attempts 应该自增但实际 {task['attempts']}"
        assert task["extraction_error"] is not None
        assert "simulated LLM crash" in task["extraction_error"]


class TestChannelBTerminalFailedAfterMaxAttempts:
    """设计文档 § 3.3 + § 5.1 阶段 5:attempts >= max_attempts → 终态 FAILED

    验证偏差 3 修复:连续失败 max_attempts 次后:
    - state 仍为 FAILED
    - attempts == max_attempts
    - next_at == None(终态,不再有退避时间,启动清理会接管)
    - extraction_error 保留最后一次错误
    """

    def test_max_attempts_reached_terminates_failed(self, tmp_path):
        writer, db, _ = _make_writer(tmp_path)
        # max_attempts=3
        tid = db.insert_task(
            session_id="s1", turn_index=1,
            user_msg="x", assistant_resp="y",
            state="PENDING", max_attempts=3,
        )
        from agent_core.memory.dual_channel_writer import TurnMessage
        messages = [TurnMessage(turn_index=1, user_msg="x", assistant_resp="y")]

        def boom(_msgs):
            raise RuntimeError("simulated LLM crash")

        # 跑 max_attempts 次(每次 CAS 会 attempts+1,第 3 次时 attempts=3==max)
        for i in range(3):
            # 重置 state 为 PENDING/FAILED(模拟 startup_scan 重排或手动重试)
            db.update_task_state(tid, "FAILED" if i > 0 else "PENDING")
            with pytest.raises(Exception):
                writer._do_extract_candidates(messages=messages, extractor=boom)

        # 验证终态
        task = db.get_task(tid)
        assert task["state"] == "FAILED", f"应该是 FAILED 终态,实际 {task['state']}"
        assert task["attempts"] == 3, f"attempts 应等于 max(3),实际 {task['attempts']}"
        assert task["next_at"] is None, (
            f"终态 next_at 应为 None,实际 {task['next_at']} "
            f"(有 next_at 表示会重试,违反设计文档 § 3.3)"
        )
        assert task["extraction_error"] is not None
        assert "simulated LLM crash" in task["extraction_error"]


class TestChannelBCasFromStatesIncludesFailed:
    """设计文档 § 5.1 阶段 1:CAS from_states 含 FAILED

    验证偏差 1 修复:FAILED 状态的 task 也能被 extract_candidates 抢占(runtime 期间
    不必等 startup_scan 重排)
    """

    def test_cas_grabs_failed_state(self, tmp_path):
        writer, db, _ = _make_writer(tmp_path)
        tid = db.insert_task(
            session_id="s1", turn_index=1,
            user_msg="x", assistant_resp="y",
            state="PENDING", max_attempts=3,
        )
        # 模拟 startup_scan 重排:FAILED → PENDING
        db.mark_failed(tid, attempts=1, next_at=time.time() - 10, error="prev")
        assert db.get_task(tid)["state"] == "FAILED"

        # 调 extract_candidates,应该能抢到(FAILED 也在 from_states 里)
        from agent_core.memory.dual_channel_writer import TurnMessage
        messages = [TurnMessage(turn_index=1, user_msg="x", assistant_resp="y")]

        def ok(_msgs):
            return []  # 空 candidates,无副作用

        # 不抛异常 = 抢到
        writer._do_extract_candidates(messages=messages, extractor=ok)

        # 验证:task 变 DONE(CAS + mark_done_with_candidates 路径走完)
        task = db.get_task(tid)
        assert task["state"] == "DONE", (
            f"FAILED 状态应能被 extract_candidates 抢占,实际 {task['state']}"
        )


class TestChannelBCasIncrementsAttempts:
    """设计文档 § 5.1 阶段 1:CAS 时 attempts=attempts+1

    验证偏差 2 修复:CAS 抢占时 attempts 自增(不再依赖失败路径单独 +1)
    """

    def test_cas_increments_attempts_on_grab(self, tmp_path):
        from agent_core.memory.meta_db import MetaDB
        db = MetaDB(":memory:")
        tid = db.insert_task(
            session_id="s1", turn_index=1,
            user_msg="x", assistant_resp="y",
            state="PENDING", max_attempts=3,
        )
        assert db.get_task(tid)["attempts"] == 0

        # CAS 抢占
        grabbed = db.cas_grab_task(tid, ["PENDING", "NONE", "FAILED"], "INFLIGHT")
        assert grabbed is True
        # 验证:attempts 立即 +1
        assert db.get_task(tid)["attempts"] == 1, (
            f"CAS 应该 +1 attempts,实际 {db.get_task(tid)['attempts']}"
        )


def test_extract_candidates_vec_add_uses_positional_id_embedding(tmp_path):
    """Channel B 提取后 vec.add 必须只用位置参数 (id, embedding),不再传 metadata/document。

    验证 Chroma 严格分离(方案 A)契约:写盘路径不传结构化字段。
    """
    from unittest.mock import MagicMock
    from agent_core.memory.memory_store import MemoryStore
    from agent_core.memory.meta_db import MetaDB
    from agent_core.memory.dual_channel_writer import DualChannelWriter
    from agent_core.memory.extractor import ExtractionCandidate
    from conftest import FakeEmbedFn

    # 用 MagicMock spy 替代真实 Chroma
    vec = MagicMock()
    vec.count.return_value = 0

    embed = FakeEmbedFn()
    dual = DualChannelWriter(
        session_id="s1",
        meta_db=MetaDB(":memory:"),
        memory_store=MemoryStore(tmp_path),
        vector_store=vec,
        embed_fn=embed,
    )

    # 模拟一次 extract:直接调用内部方法,绕过 LLM
    # 让 _do_extract_candidates 接受一个 candidate 并走完写盘
    from agent_core.memory.dual_channel_writer import ExtractionCandidate, TurnMessage
    msg = TurnMessage(turn_index=1, user_msg="我叫张三", assistant_resp="记住了")
    cand = ExtractionCandidate(
        type="user", title="姓名", body="张三",
        source_quote="我叫张三", tags=["person"],
    )

    # 直接调 _do_extract_candidates(用 lambda extractor)
    dual._do_extract_candidates(
        [msg],
        extractor=lambda _msgs: [cand],
    )

    # 验证 vec.add 被调,只用位置参数 (id, embedding)
    assert vec.add.called, "vec.add 未被调用"

    for call in vec.add.call_args_list:
        args, kwargs = call
        assert len(args) == 2, (
            f"vec.add 应只接 2 个位置参数 (id, embedding),得到 {len(args)} 个: {args}"
        )
        assert isinstance(args[0], str), (
            f"arg[0] 应是 id str,得到 {type(args[0]).__name__}: {args[0]!r}"
        )
        assert isinstance(args[1], list), (
            f"arg[1] 应是 embedding list,得到 {type(args[1]).__name__}"
        )
        assert not kwargs, (
            f"vec.add 应不接 kwargs(metadata/document/dict),得到 {kwargs}"
        )

    dual.shutdown(timeout=5)
