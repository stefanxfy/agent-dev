"""
delete_done_tasks 测试

Phase 2 / Step 2.2.5 — TDD 红 → 绿

- 删除 state='DONE' AND updated_at < before_timestamp 的行
- 返回删除行数
- 不删 FAILED / PENDING / NONE / INFLIGHT
- 时间边界:updated_at == before_timestamp 不删
- 空表返 0
- 单 session vs 多 session
"""
import time
import pytest

from agent_core.memory.meta_db import MetaDB


@pytest.fixture
def db():
    return MetaDB(":memory:")


def _add_done(db, session_id="s1", turn_index=1, updated_at=None):
    """helper:加一行 DONE 任务"""
    tid = db.insert_task(
        session_id=session_id, turn_index=turn_index,
        user_msg="x", assistant_resp="y",
        state="NONE", max_attempts=3,
    )
    db.update_task_state(tid, "PENDING")
    db.update_task_state(tid, "INFLIGHT")
    db.mark_done_with_candidates(tid, "[]")
    if updated_at is not None:
        # 改写 updated_at(模拟"几天前完成")
        with db.transaction() as conn:
            conn.execute(
                "UPDATE memory_tasks SET updated_at=? WHERE task_id=?",
                (updated_at, tid),
            )
    return tid


class TestDeleteDoneTasks:
    """delete_done_tasks(before_timestamp) — 清理超期 DONE"""

    def test_deletes_old_done(self, db):
        now = time.time()
        old_ts = now - 86400 * 2  # 2 天前
        _add_done(db, updated_at=old_ts)
        deleted = db.delete_done_tasks(before_timestamp=now - 86400)  # 1 天前边界
        assert deleted == 1
        # 表里应空
        with db.transaction() as conn:
            cnt = conn.execute("SELECT COUNT(*) FROM memory_tasks").fetchone()[0]
        assert cnt == 0

    def test_keeps_recent_done(self, db):
        """updated_at 在 before_timestamp 之后的 DONE 不删"""
        now = time.time()
        _add_done(db, updated_at=now - 60)  # 60s 前,1 天前边界会保留
        deleted = db.delete_done_tasks(before_timestamp=now - 86400)
        assert deleted == 0

    def test_does_not_delete_other_states(self, db):
        """非 DONE 状态不删(即使 updated_at 很久)"""
        now = time.time()
        old_ts = now - 86400 * 100  # 100 天前
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
        # FAILED
        tid_f = db.insert_task(
            session_id="s1", turn_index=3, user_msg="x", assistant_resp="y",
            state="NONE", max_attempts=3,
        )
        db.update_task_state(tid_f, "PENDING")
        db.update_task_state(tid_f, "INFLIGHT")
        db.update_task_state(tid_f, "FAILED")

        # 把所有行 updated_at 改到很老
        with db.transaction() as conn:
            conn.execute("UPDATE memory_tasks SET updated_at=?", (old_ts,))

        deleted = db.delete_done_tasks(before_timestamp=now)
        assert deleted == 0  # 没删任何
        with db.transaction() as conn:
            cnt = conn.execute("SELECT COUNT(*) FROM memory_tasks").fetchone()[0]
        assert cnt == 3

    def test_returns_count(self, db):
        """返回删除行数"""
        now = time.time()
        old_ts = now - 86400 * 10
        for i in range(5):
            _add_done(db, turn_index=i + 1, updated_at=old_ts)
        deleted = db.delete_done_tasks(before_timestamp=now)
        assert deleted == 5

    def test_empty_table_returns_zero(self, db):
        """空表返 0"""
        assert db.delete_done_tasks(before_timestamp=time.time()) == 0

    def test_mixed_old_and_recent(self, db):
        """老 + 新混合:只删老的"""
        now = time.time()
        old_ts = now - 86400 * 10
        # 3 老 + 2 新
        for i in range(3):
            _add_done(db, turn_index=i + 1, updated_at=old_ts)
        for i in range(3, 5):
            _add_done(db, turn_index=i + 1, updated_at=now - 60)
        deleted = db.delete_done_tasks(before_timestamp=now - 86400)
        assert deleted == 3
        with db.transaction() as conn:
            cnt = conn.execute("SELECT COUNT(*) FROM memory_tasks").fetchone()[0]
        assert cnt == 2

    def test_boundary_inclusive_exclusive(self, db):
        """updated_at == before_timestamp 边界(根据 SQL 决定,常见是 <)"""
        now = time.time()
        # 设 before=100,updated_at=100 → 不删(< not <=)
        _add_done(db, updated_at=100.0)
        deleted = db.delete_done_tasks(before_timestamp=100.0)
        # 约定 < 严格小,100.0 == 100.0 不删
        assert deleted == 0
