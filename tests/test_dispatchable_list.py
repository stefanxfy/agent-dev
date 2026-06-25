"""
list_dispatchable_tasks 测试

Phase 2 / Step 2.2.9 — TDD 红 → 绿

- 返 state IN ('NONE', 'PENDING') 的 task
- ORDER BY turn_index ASC
- 支持 session_id 过滤(None 时跨 session)
- 支持 limit(默认 100)
- 排除 FAILED / DONE / INFLIGHT
- 空表返 []
"""
import time
import pytest

from agent_core.memory.meta_db import MetaDB


@pytest.fixture
def db():
    return MetaDB(":memory:")


def _add(db, session_id="s1", turn_index=1, state="NONE", max_attempts=3):
    tid = db.insert_task(
        session_id=session_id, turn_index=turn_index,
        user_msg="x", assistant_resp="y",
        state=state, max_attempts=max_attempts,
    )
    if state != "NONE":
        # 转换路径:NONE → PENDING → INFLIGHT → ...
        db.update_task_state(tid, "PENDING")
        if state in ("INFLIGHT", "DONE", "FAILED"):
            db.update_task_state(tid, "INFLIGHT")
        if state == "DONE":
            db.mark_done_with_candidates(tid, "[]")
        elif state == "FAILED":
            db.update_task_state(tid, "FAILED")
    return tid


class TestListDispatchableTasks:
    """list_dispatchable_tasks() — 派工列表"""

    def test_returns_none_and_pending(self, db):
        """返 state IN (NONE, PENDING) 的 task"""
        _add(db, turn_index=1, state="NONE")
        _add(db, turn_index=2, state="PENDING")
        _add(db, turn_index=3, state="INFLIGHT")
        _add(db, turn_index=4, state="DONE")
        _add(db, turn_index=5, state="FAILED")
        result = db.list_dispatchable_tasks()
        assert len(result) == 2
        turn_indices = [r["turn_index"] for r in result]
        assert turn_indices == [1, 2]

    def test_order_by_turn_index_asc(self, db):
        """按 turn_index 升序"""
        for ti in [5, 1, 3, 2, 4]:
            _add(db, turn_index=ti, state="PENDING")
        result = db.list_dispatchable_tasks()
        assert [r["turn_index"] for r in result] == [1, 2, 3, 4, 5]

    def test_session_id_filter(self, db):
        """session_id=None 跨 session,否则只返指定 session"""
        _add(db, session_id="s1", turn_index=1, state="PENDING")
        _add(db, session_id="s2", turn_index=1, state="PENDING")
        _add(db, session_id="s3", turn_index=1, state="PENDING")
        result_s1 = db.list_dispatchable_tasks(session_id="s1")
        assert len(result_s1) == 1
        assert result_s1[0]["session_id"] == "s1"
        # 不传 session_id
        result_all = db.list_dispatchable_tasks()
        assert len(result_all) == 3

    def test_limit(self, db):
        """limit 参数生效"""
        for i in range(10):
            _add(db, turn_index=i + 1, state="PENDING")
        result = db.list_dispatchable_tasks(limit=3)
        assert len(result) == 3
        assert [r["turn_index"] for r in result] == [1, 2, 3]

    def test_empty_table(self, db):
        """空表返 []"""
        assert db.list_dispatchable_tasks() == []

    def test_excludes_other_states(self, db):
        """返结果不含 FAILED / DONE / INFLIGHT"""
        _add(db, turn_index=1, state="INFLIGHT")
        _add(db, turn_index=2, state="DONE")
        _add(db, turn_index=3, state="FAILED")
        result = db.list_dispatchable_tasks()
        assert result == []

    def test_returns_full_dict(self, db):
        """返字典含 task_id / session_id / turn_index / state / attempts / max_attempts"""
        _add(db, turn_index=1, state="PENDING", max_attempts=5)
        result = db.list_dispatchable_tasks()
        assert len(result) == 1
        row = result[0]
        for key in (
            "task_id", "session_id", "turn_index", "state",
            "attempts", "max_attempts", "next_at", "inflight_at",
        ):
            assert key in row, f"missing key: {key}"
        assert row["session_id"] == "s1"
        assert row["turn_index"] == 1
        assert row["state"] == "PENDING"
        assert row["max_attempts"] == 5
