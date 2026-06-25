"""
get_task_by_turn 测试

Phase 2 / Step 2.2.10b — TDD 红 → 绿

Channel B 走新表时,需要按 (session_id, turn_index) 反查 task_id。
UNIQUE(session_id, turn_index) 保证一条 = 一个 task。

- 查到返 dict(含 task_id / state / attempts / max_attempts)
- 查不到返 None
- 不同 session 同 turn 互不干扰
"""
import pytest

from agent_core.memory.meta_db import MetaDB


@pytest.fixture
def db():
    return MetaDB(":memory:")


def _add(db, session_id="s1", turn_index=1, state="NONE"):
    tid = db.insert_task(
        session_id=session_id, turn_index=turn_index,
        user_msg="x", assistant_resp="y",
        state=state, max_attempts=3,
    )
    if state != "NONE":
        db.update_task_state(tid, "PENDING")
        if state in ("INFLIGHT", "DONE", "FAILED"):
            db.update_task_state(tid, "INFLIGHT")
        if state == "DONE":
            db.mark_done_with_candidates(tid, "[]")
        elif state == "FAILED":
            db.update_task_state(tid, "FAILED")
    return tid


class TestGetTaskByTurn:
    """get_task_by_turn(session_id, turn_index) -> dict | None"""

    def test_returns_none_state(self, db):
        _add(db, session_id="s1", turn_index=1, state="NONE")
        task = db.get_task_by_turn("s1", 1)
        assert task is not None
        assert task["state"] == "NONE"
        assert task["session_id"] == "s1"
        assert task["turn_index"] == 1

    def test_returns_pending(self, db):
        _add(db, session_id="s1", turn_index=2, state="PENDING")
        task = db.get_task_by_turn("s1", 2)
        assert task is not None
        assert task["state"] == "PENDING"

    def test_returns_done_with_candidates(self, db):
        _add(db, session_id="s1", turn_index=3, state="DONE")
        task = db.get_task_by_turn("s1", 3)
        assert task is not None
        assert task["state"] == "DONE"
        assert task["candidates_payload"] == "[]"

    def test_returns_none_when_missing(self, db):
        """没这行返 None,不抛"""
        assert db.get_task_by_turn("s1", 999) is None

    def test_session_isolation(self, db):
        """不同 session 同 turn 互不干扰"""
        _add(db, session_id="s1", turn_index=1, state="PENDING")
        _add(db, session_id="s2", turn_index=1, state="DONE")
        s1 = db.get_task_by_turn("s1", 1)
        s2 = db.get_task_by_turn("s2", 1)
        assert s1["session_id"] == "s1"
        assert s1["state"] == "PENDING"
        assert s2["session_id"] == "s2"
        assert s2["state"] == "DONE"

    def test_returned_dict_has_task_id(self, db):
        """dict 含 task_id(caller 需用来 CAS 抢占 / mark_done)"""
        _add(db, session_id="s1", turn_index=1, state="PENDING")
        task = db.get_task_by_turn("s1", 1)
        assert "task_id" in task
        assert task["task_id"] is not None
        assert isinstance(task["task_id"], int)

    def test_works_after_state_transitions(self, db):
        """状态转移后仍能查到(同 task_id)"""
        tid = _add(db, session_id="s1", turn_index=1, state="NONE")
        db.update_task_state(tid, "PENDING")
        db.cas_grab_task(tid, ["PENDING"], "INFLIGHT")
        task = db.get_task_by_turn("s1", 1)
        assert task["task_id"] == tid
        assert task["state"] == "INFLIGHT"
