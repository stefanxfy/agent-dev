"""
memory_tasks UNIQUE 幂等性测试

Phase 1 / Step 1.2.7 — TDD 红 → 绿
- 同 (session_id, turn_index) 二次 insert_task → 返回 None,不抛
- UNIQUE 冲突时原行不被覆盖(user_msg/assistant_resp/state 保留)
- 多次(3+)冲突都返回 None
- 不同 (session, turn) 插入不受影响
"""
import time
import pytest

from agent_core.memory.meta_db import MetaDB


@pytest.fixture
def db():
    return MetaDB(":memory:")


class TestInsertTaskUNIQUEConflict:
    """UNIQUE 冲突时 insert_task 幂等返回 None"""

    def test_duplicate_returns_none(self, db):
        """同 (session, turn) 二次插入返回 None"""
        tid1 = db.insert_task(
            session_id="s1", turn_index=1,
            user_msg="first", assistant_resp="1st",
        )
        assert tid1 is not None
        tid2 = db.insert_task(
            session_id="s1", turn_index=1,
            user_msg="dup", assistant_resp="dup",
        )
        assert tid2 is None  # 幂等

    def test_duplicate_does_not_overwrite(self, db):
        """UNIQUE 冲突时原行的 user_msg/assistant_resp 保留"""
        tid = db.insert_task(
            session_id="s1", turn_index=1,
            user_msg="original", assistant_resp="first answer",
        )
        db.insert_task(
            session_id="s1", turn_index=1,
            user_msg="overwrite attempt", assistant_resp="should not stick",
        )
        task = db.get_task(tid)
        assert task["user_msg"] == "original"
        assert task["assistant_resp"] == "first answer"

    def test_three_duplicates_all_return_none(self, db):
        """连续 3 次同 key 插入都返回 None"""
        for _ in range(3):
            tid = db.insert_task(
                session_id="s1", turn_index=1,
                user_msg="x", assistant_resp="y",
            )
        assert tid is None  # 第 2/3 次都返回 None
        # 第一次 tid 不会到这里(被覆盖),检查表里就 1 行
        with db.transaction() as conn:
            cnt = conn.execute(
                "SELECT COUNT(*) FROM memory_tasks WHERE session_id='s1' AND turn_index=1"
            ).fetchone()[0]
        assert cnt == 1

    def test_different_session_allowed(self, db):
        """不同 session 同 turn_index 互不影响"""
        t1 = db.insert_task(
            session_id="s1", turn_index=1,
            user_msg="a", assistant_resp="A",
        )
        t2 = db.insert_task(
            session_id="s2", turn_index=1,
            user_msg="b", assistant_resp="B",
        )
        assert t1 is not None
        assert t2 is not None
        assert t1 != t2

    def test_different_turn_in_same_session_allowed(self, db):
        """同 session 不同 turn_index 互不影响"""
        t1 = db.insert_task(
            session_id="s1", turn_index=1,
            user_msg="a", assistant_resp="A",
        )
        t2 = db.insert_task(
            session_id="s1", turn_index=2,
            user_msg="b", assistant_resp="B",
        )
        assert t1 is not None
        assert t2 is not None

    def test_duplicate_does_not_increment_attempts(self, db):
        """UNIQUE 冲突时原行 attempts 字段不被改"""
        tid = db.insert_task(
            session_id="s1", turn_index=1,
            user_msg="x", assistant_resp="y",
            state="PENDING",
        )
        # 模拟后续因 UNIQUE 冲突重试
        db.insert_task(
            session_id="s1", turn_index=1,
            user_msg="x", assistant_resp="y",
            state="INFLIGHT",
        )
        task = db.get_task(tid)
        assert task["attempts"] == 0  # 没变
        assert task["state"] == "PENDING"  # 没被覆盖

    def test_returned_none_is_actual_none_not_zero(self, db):
        """None 必须是 None,不是 0(混淆 falsy)"""
        tid1 = db.insert_task(
            session_id="s1", turn_index=1,
            user_msg="a", assistant_resp="A",
        )
        tid2 = db.insert_task(
            session_id="s1", turn_index=1,
            user_msg="b", assistant_resp="B",
        )
        assert tid2 is None
        # 显式 type 检查
        assert type(tid2) is type(None)


class TestInsertTaskUNIQUEConflictStatePreservation:
    """冲突后原行所有字段保留"""

    def test_state_preserved_on_conflict(self, db):
        """原行 state 字段保留(INFLIGHT 不会被覆盖为 NONE)"""
        tid = db.insert_task(
            session_id="s1", turn_index=1,
            user_msg="x", assistant_resp="y",
            state="INFLIGHT",
        )
        db.insert_task(
            session_id="s1", turn_index=1,
            user_msg="x", assistant_resp="y",
            state="FAILED",  # 试图覆盖
        )
        task = db.get_task(tid)
        assert task["state"] == "INFLIGHT"

    def test_turn_metadata_preserved(self, db):
        """原行 turn_metadata 保留"""
        meta = '{"ts": 100, "tokens": 50}'
        tid = db.insert_task(
            session_id="s1", turn_index=1,
            user_msg="x", assistant_resp="y",
            turn_metadata=meta,
        )
        db.insert_task(
            session_id="s1", turn_index=1,
            user_msg="x", assistant_resp="y",
            turn_metadata='{"overwrite": true}',
        )
        task = db.get_task(tid)
        assert task["turn_metadata"] == meta
