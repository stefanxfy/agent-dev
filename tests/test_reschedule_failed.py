"""
reschedule_retryable_failed 测试

Phase 2 / Step 2.2.8 — TDD 红 → 绿

- state='FAILED' AND attempts < max_attempts AND next_at <= now → PENDING
- 保留 attempts(extraction_error 不动,inflight_at=None?)
- 不动 next_at(给 audit 用)
- 不重排 attempts >= max_attempts(终态)
- 不重排 next_at > now(还没到时间)
- 不重排非 FAILED
"""
import time
import pytest

from agent_core.memory.meta_db import MetaDB


@pytest.fixture
def db():
    return MetaDB(":memory:")


def _add_retryable_failed(db, attempts=1, max_attempts=3, next_at=None, turn_index=1):
    tid = db.insert_task(
        session_id="s1", turn_index=turn_index,
        user_msg="x", assistant_resp="y",
        state="NONE", max_attempts=max_attempts,
    )
    db.update_task_state(tid, "PENDING")
    db.update_task_state(tid, "INFLIGHT")
    db.update_task_state(tid, "FAILED")
    with db.transaction() as conn:
        conn.execute(
            "UPDATE memory_tasks SET attempts=?, next_at=? WHERE task_id=?",
            (attempts, next_at, tid),
        )
    return tid


class TestRescheduleRetryableFailed:
    """reschedule_retryable_failed() — 退避到期 FAILED → PENDING"""

    def test_overdue_failed_rescheduled(self, db):
        """next_at <= now 的 FAILED(退避中)→ PENDING"""
        now = time.time()
        # 60s 前就该重试了
        _add_retryable_failed(db, attempts=1, max_attempts=3, next_at=now - 60)
        rescheduled = db.reschedule_retryable_failed()
        assert rescheduled == 1
        with db.transaction() as conn:
            row = conn.execute(
                "SELECT state, attempts, next_at FROM memory_tasks LIMIT 1"
            ).fetchone()
        assert row[0] == "PENDING"
        assert row[1] == 1  # attempts 保留
        assert row[2] == now - 60  # next_at 保留

    def test_future_failed_kept(self, db):
        """next_at > now 的 FAILED 不重排(还没到时间)"""
        now = time.time()
        _add_retryable_failed(db, attempts=1, max_attempts=3, next_at=now + 60)
        rescheduled = db.reschedule_retryable_failed()
        assert rescheduled == 0
        with db.transaction() as conn:
            row = conn.execute("SELECT state FROM memory_tasks LIMIT 1").fetchone()
        assert row[0] == "FAILED"

    def test_terminal_failed_kept(self, db):
        """attempts >= max_attempts(终态)不重排"""
        now = time.time()
        _add_retryable_failed(db, attempts=3, max_attempts=3, next_at=now - 100)
        rescheduled = db.reschedule_retryable_failed()
        assert rescheduled == 0
        with db.transaction() as conn:
            row = conn.execute("SELECT state FROM memory_tasks LIMIT 1").fetchone()
        assert row[0] == "FAILED"

    def test_does_not_touch_other_states(self, db):
        """非 FAILED 不重排"""
        now = time.time()
        # NONE
        db.insert_task(
            session_id="s1", turn_index=1, user_msg="x", assistant_resp="y",
            state="NONE", max_attempts=3,
        )
        # PENDING
        tid_p = db.insert_task(
            session_id="s1", turn_index=2, user_msg="x", assistant_resp="y",
            state="NONE", max_attempts=3,
        )
        db.update_task_state(tid_p, "PENDING")
        # DONE
        tid_d = db.insert_task(
            session_id="s1", turn_index=3, user_msg="x", assistant_resp="y",
            state="NONE", max_attempts=3,
        )
        db.update_task_state(tid_d, "PENDING")
        db.update_task_state(tid_d, "INFLIGHT")
        db.mark_done_with_candidates(tid_d, "[]")
        # INFLIGHT
        tid_i = db.insert_task(
            session_id="s1", turn_index=4, user_msg="x", assistant_resp="y",
            state="NONE", max_attempts=3,
        )
        db.update_task_state(tid_i, "PENDING")
        db.update_task_state(tid_i, "INFLIGHT")

        # 把所有行 next_at 设为过期
        with db.transaction() as conn:
            conn.execute("UPDATE memory_tasks SET next_at=?", (now - 100,))

        rescheduled = db.reschedule_retryable_failed()
        assert rescheduled == 0

    def test_returns_count(self, db):
        now = time.time()
        for i in range(3):
            _add_retryable_failed(
                db, attempts=1, max_attempts=3,
                next_at=now - 60, turn_index=i + 1,
            )
        rescheduled = db.reschedule_retryable_failed()
        assert rescheduled == 3

    def test_mixed_reschedules_only_due(self, db):
        """混合:过期的重排,没到的不动"""
        now = time.time()
        _add_retryable_failed(db, attempts=1, max_attempts=3, next_at=now - 60, turn_index=1)
        _add_retryable_failed(db, attempts=1, max_attempts=3, next_at=now + 60, turn_index=2)
        _add_retryable_failed(db, attempts=1, max_attempts=3, next_at=now - 30, turn_index=3)
        rescheduled = db.reschedule_retryable_failed()
        assert rescheduled == 2

    def test_no_next_at_failed_kept(self, db):
        """FAILED 但 next_at IS NULL 的(老数据)不重排"""
        now = time.time()
        # 手动构造一行 FAILED 无 next_at
        tid = db.insert_task(
            session_id="s1", turn_index=1, user_msg="x", assistant_resp="y",
            state="NONE", max_attempts=3,
        )
        db.update_task_state(tid, "PENDING")
        db.update_task_state(tid, "INFLIGHT")
        db.update_task_state(tid, "FAILED")
        # 不设 next_at
        rescheduled = db.reschedule_retryable_failed()
        # NULL > now → 不重排
        assert rescheduled == 0

    def test_preserves_extraction_error(self, db):
        """重排后 extraction_error 保留(debug 可见上次失败原因)"""
        now = time.time()
        tid = _add_retryable_failed(
            db, attempts=1, max_attempts=3, next_at=now - 60,
        )
        with db.transaction() as conn:
            conn.execute(
                "UPDATE memory_tasks SET extraction_error='LLM timeout' WHERE task_id=?",
                (tid,),
            )
        db.reschedule_retryable_failed()
        with db.transaction() as conn:
            row = conn.execute(
                "SELECT state, extraction_error FROM memory_tasks WHERE task_id=?",
                (tid,),
            ).fetchone()
        assert row[0] == "PENDING"
        assert row[1] == "LLM timeout"
