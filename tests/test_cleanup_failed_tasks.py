"""
DualChannelWriter.cleanup_failed_tasks 测试

Phase 3 / Step 3.3.4 — TDD 红 → 绿

- 公开方法,读 self.task_wal_config.failed_retention_seconds
- 计算 before_timestamp = now - retention,调 meta_db.delete_failed_tasks
- 返删除行数
- 退避中 FAILED(attempts < max_attempts)不被删 — 由 delete_failed_tasks 守
- 显式传 retention_seconds 可覆盖
"""
import time
import pytest

from agent_core.memory.meta_db import MetaDB
from agent_core.memory.dual_channel_writer import DualChannelWriter
from agent_core.memory.memory_store import MemoryStore
from agent_core.memory.wal_config import TaskWALConfig
from tests.test_dual_channel_concurrent import FakeEmbedFn


def _make_writer(tmp_path, *, retention_seconds=86400):
    db = MetaDB(":memory:")
    store = MemoryStore(tmp_path / "memory")
    cfg = TaskWALConfig(failed_retention_seconds=retention_seconds)
    writer = DualChannelWriter(
        session_id="s1", meta_db=db, memory_store=store,
        vector_store=None, embed_fn=FakeEmbedFn(),
        task_wal_config=cfg,
    )
    return writer, db


def _add_terminal_failed(db, turn_index, updated_at, max_attempts=3):
    """FAILED 终态:attempts == max_attempts"""
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
            "UPDATE memory_tasks SET attempts=?, next_at=?, updated_at=? WHERE task_id=?",
            (max_attempts, time.time() - 100, updated_at, tid),
        )
    return tid


def _add_retryable_failed(db, turn_index, updated_at):
    """FAILED 退避中:attempts < max_attempts"""
    tid = _add_terminal_failed(db, turn_index, updated_at, max_attempts=3)
    with db.transaction() as conn:
        conn.execute(
            "UPDATE memory_tasks SET attempts=1 WHERE task_id=?", (tid,),
        )
    return tid


class TestCleanupFailedTasks:
    """DualChannelWriter.cleanup_failed_tasks() — 公开清理终态 FAILED"""

    def test_deletes_terminal_old(self, tmp_path):
        """终态(attempts>=max_attempts)旧行被删"""
        writer, db = _make_writer(tmp_path, retention_seconds=86400)
        old_ts = time.time() - 86400 * 5
        _add_terminal_failed(db, 1, updated_at=old_ts, max_attempts=3)
        _add_terminal_failed(db, 2, updated_at=time.time() - 60, max_attempts=3)

        deleted = writer.cleanup_failed_tasks()
        assert deleted == 1
        with db.transaction() as conn:
            cnt = conn.execute("SELECT COUNT(*) FROM memory_tasks").fetchone()[0]
        assert cnt == 1  # 新的 FAILED 终态保留

    def test_keeps_retryable_failed(self, tmp_path):
        """退避中 FAILED(attempts < max_attempts)不删"""
        writer, db = _make_writer(tmp_path, retention_seconds=86400)
        old_ts = time.time() - 86400 * 5
        _add_retryable_failed(db, 1, updated_at=old_ts)  # attempts=1, max=3

        deleted = writer.cleanup_failed_tasks()
        assert deleted == 0
        with db.transaction() as conn:
            cnt = conn.execute("SELECT COUNT(*) FROM memory_tasks").fetchone()[0]
        assert cnt == 1

    def test_uses_task_wal_config_retention(self, tmp_path):
        """默认读 self.task_wal_config.failed_retention_seconds"""
        writer, db = _make_writer(tmp_path, retention_seconds=86400)
        old_ts = time.time() - 86400 * 2
        _add_terminal_failed(db, 1, updated_at=old_ts, max_attempts=3)
        deleted = writer.cleanup_failed_tasks()
        assert deleted == 1

    def test_override_retention_seconds(self, tmp_path):
        """显式传 retention_seconds 可覆盖"""
        writer, db = _make_writer(tmp_path, retention_seconds=86400)
        # 1 天前终态
        old_ts = time.time() - 86400
        _add_terminal_failed(db, 1, updated_at=old_ts, max_attempts=3)
        # override 60s → 1 天前必删
        deleted = writer.cleanup_failed_tasks(retention_seconds=60)
        assert deleted == 1

    def test_returns_count(self, tmp_path):
        writer, db = _make_writer(tmp_path, retention_seconds=86400)
        old_ts = time.time() - 86400 * 5
        for i in range(3):
            _add_terminal_failed(db, i + 1, updated_at=old_ts, max_attempts=3)
        assert writer.cleanup_failed_tasks() == 3

    def test_empty_table_returns_zero(self, tmp_path):
        writer, _ = _make_writer(tmp_path)
        assert writer.cleanup_failed_tasks() == 0

    def test_does_not_touch_other_states(self, tmp_path):
        """清理方法不动 DONE / PENDING / INFLIGHT"""
        writer, db = _make_writer(tmp_path, retention_seconds=86400)
        old_ts = time.time() - 86400 * 5
        # DONE 旧行
        tid_d = db.insert_task(
            session_id="s1", turn_index=1, user_msg="x", assistant_resp="y",
            state="NONE", max_attempts=3,
        )
        db.update_task_state(tid_d, "PENDING")
        db.update_task_state(tid_d, "INFLIGHT")
        db.mark_done_with_candidates(tid_d, "[]")
        with db.transaction() as conn:
            conn.execute("UPDATE memory_tasks SET updated_at=?", (old_ts,))
        # PENDING
        tid_p = db.insert_task(
            session_id="s1", turn_index=2, user_msg="x", assistant_resp="y",
            state="NONE", max_attempts=3,
        )
        db.update_task_state(tid_p, "PENDING")
        with db.transaction() as conn:
            conn.execute("UPDATE memory_tasks SET updated_at=?", (old_ts,))
        # INFLIGHT
        tid_i = db.insert_task(
            session_id="s1", turn_index=3, user_msg="x", assistant_resp="y",
            state="NONE", max_attempts=3,
        )
        db.update_task_state(tid_i, "PENDING")
        db.update_task_state(tid_i, "INFLIGHT")
        with db.transaction() as conn:
            conn.execute("UPDATE memory_tasks SET updated_at=?", (old_ts,))
        # FAILED 终态 旧行(应被删)
        _add_terminal_failed(db, 4, updated_at=old_ts, max_attempts=3)

        deleted = writer.cleanup_failed_tasks()
        assert deleted == 1  # 只删 FAILED 终态
        with db.transaction() as conn:
            cnt = conn.execute("SELECT COUNT(*) FROM memory_tasks").fetchone()[0]
        assert cnt == 3  # DONE + PENDING + INFLIGHT 保留
