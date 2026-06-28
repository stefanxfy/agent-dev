"""
delete_failed_tasks 测试

Phase 2 / Step 2.2.6 — TDD 红 → 绿

- 删除 state='FAILED' AND attempts >= max_attempts AND updated_at < before_timestamp
- 不删退避中 FAILED(attempts < max_attempts)
- 不删其它状态
- 返回删除行数
"""
import time
import pytest

from agent_core.memory.meta_db import MetaDB


@pytest.fixture
def db():
    return MetaDB(":memory:")


def _add_failed(db, attempts, max_attempts=3, turn_index=1, updated_at=None):
    """helper:加一行 FAILED 任务(attempts 可控)"""
    tid = db.insert_task(
        session_id="s1", turn_index=turn_index,
        user_msg="x", assistant_resp="y",
        state="NONE", max_attempts=max_attempts,
    )
    db.update_task_state(tid, "PENDING")
    db.update_task_state(tid, "INFLIGHT")
    db.update_task_state(tid, "FAILED")
    # attempts 字段手动设为目标值
    with db.transaction() as conn:
        conn.execute(
            "UPDATE memory_tasks SET attempts=? WHERE task_id=?",
            (attempts, tid),
        )
    if updated_at is not None:
        with db.transaction() as conn:
            conn.execute(
                "UPDATE memory_tasks SET updated_at=? WHERE task_id=?",
                (updated_at, tid),
            )
    return tid


class TestDeleteFailedTasks:
    """delete_failed_tasks(before_timestamp) — 清理终态 FAILED"""

    def test_deletes_terminal_failed(self, db):
        """attempts >= max_attempts 的 FAILED 删除"""
        now = time.time()
        old_ts = now - 86400 * 5
        _add_failed(db, attempts=3, max_attempts=3, updated_at=old_ts)  # 终态
        deleted = db.delete_failed_tasks(before_timestamp=now)
        assert deleted == 1

    def test_keeps_retryable_failed(self, db):
        """attempts < max_attempts 的 FAILED(退避中)不删"""
        now = time.time()
        old_ts = now - 86400 * 5
        _add_failed(db, attempts=1, max_attempts=3, updated_at=old_ts)  # 退避中
        deleted = db.delete_failed_tasks(before_timestamp=now)
        assert deleted == 0
        with db.transaction() as conn:
            cnt = conn.execute("SELECT COUNT(*) FROM memory_tasks").fetchone()[0]
        assert cnt == 1

    def test_keeps_recent_terminal_failed(self, db):
        """updated_at 在 before_timestamp 之后的终态 FAILED 不删"""
        now = time.time()
        _add_failed(db, attempts=3, max_attempts=3, updated_at=now - 60)
        deleted = db.delete_failed_tasks(before_timestamp=now - 86400)
        assert deleted == 0

    def test_does_not_delete_other_states(self, db):
        """非 FAILED 状态不删"""
        now = time.time()
        old_ts = now - 86400 * 100
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

        with db.transaction() as conn:
            conn.execute("UPDATE memory_tasks SET updated_at=?", (old_ts,))

        deleted = db.delete_failed_tasks(before_timestamp=now)
        assert deleted == 0
        with db.transaction() as conn:
            cnt = conn.execute("SELECT COUNT(*) FROM memory_tasks").fetchone()[0]
        assert cnt == 4

    def test_returns_count(self, db):
        now = time.time()
        old_ts = now - 86400 * 5
        for i in range(4):
            _add_failed(db, attempts=3, max_attempts=3, turn_index=i + 1, updated_at=old_ts)
        deleted = db.delete_failed_tasks(before_timestamp=now)
        assert deleted == 4

    def test_max_attempts_per_row(self, db):
        """每行 max_attempts 不同,各自 attempts vs max 决定终态"""
        now = time.time()
        old_ts = now - 86400 * 5
        # 行 1:attempts=5, max=5 → 终态
        _add_failed(db, attempts=5, max_attempts=5, turn_index=1, updated_at=old_ts)
        # 行 2:attempts=3, max=10 → 退避中
        _add_failed(db, attempts=3, max_attempts=10, turn_index=2, updated_at=old_ts)
        # 行 3:attempts=10, max=10 → 终态
        _add_failed(db, attempts=10, max_attempts=10, turn_index=3, updated_at=old_ts)
        deleted = db.delete_failed_tasks(before_timestamp=now)
        assert deleted == 2  # 行 1 + 行 3

    def test_empty_table_returns_zero(self, db):
        assert db.delete_failed_tasks(before_timestamp=time.time()) == 0
