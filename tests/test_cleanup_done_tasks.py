"""
DualChannelWriter.cleanup_done_tasks 测试

Phase 3 / Step 3.3.3 — TDD 红 → 绿

- 公开方法,读 self.task_wal_config.done_retention_seconds
- 计算 before_timestamp = now - retention,调 meta_db.delete_done_tasks
- 返删除行数
- retention=0 边界
- 不传 retention_seconds 时走 self.task_wal_config.done_retention_seconds
- 可传 retention_seconds 覆盖(测试/运维用)
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
    cfg = TaskWALConfig(done_retention_seconds=retention_seconds)
    writer = DualChannelWriter(
        session_id="s1", meta_db=db, memory_store=store,
        vector_store=None, embed_fn=FakeEmbedFn(),
        task_wal_config=cfg,
    )
    return writer, db


def _add_done(db, turn_index, updated_at):
    tid = db.insert_task(
        session_id="s1", turn_index=turn_index,
        user_msg="x", assistant_resp="y",
        state="NONE", max_attempts=3,
    )
    db.update_task_state(tid, "PENDING")
    db.update_task_state(tid, "INFLIGHT")
    db.mark_done_with_candidates(tid, "[]")
    with db.transaction() as conn:
        conn.execute("UPDATE memory_tasks SET updated_at=? WHERE task_id=?",
                     (updated_at, tid))
    return tid


class TestCleanupDoneTasks:
    """DualChannelWriter.cleanup_done_tasks() — 公开清理封装"""

    def test_deletes_old_done(self, tmp_path):
        writer, db = _make_writer(tmp_path, retention_seconds=86400)
        old_ts = time.time() - 86400 * 5
        _add_done(db, 1, updated_at=old_ts)
        _add_done(db, 2, updated_at=time.time() - 60)

        deleted = writer.cleanup_done_tasks()
        assert deleted == 1
        with db.transaction() as conn:
            cnt = conn.execute("SELECT COUNT(*) FROM memory_tasks").fetchone()[0]
        assert cnt == 1

    def test_uses_task_wal_config_retention(self, tmp_path):
        """默认读 self.task_wal_config.done_retention_seconds"""
        # 写 2 天前的 DONE,retention=1天 → 应删
        # 同样 2 天前但 retention=10天 → 应保留
        writer, db = _make_writer(tmp_path, retention_seconds=86400)
        old_ts = time.time() - 86400 * 2
        _add_done(db, 1, updated_at=old_ts)
        # retention=86400(1天)→ 2 天前应被删
        deleted = writer.cleanup_done_tasks()
        assert deleted == 1

    def test_override_retention_seconds(self, tmp_path):
        """显式传 retention_seconds 可覆盖"""
        writer, db = _make_writer(tmp_path, retention_seconds=86400)
        # 1 天前完成
        old_ts = time.time() - 86400
        _add_done(db, 1, updated_at=old_ts)
        # 默认 retention=1天 → 边界(可能删可能不删,用更激进的值)
        # override retention=60s → 1 天前必删
        deleted = writer.cleanup_done_tasks(retention_seconds=60)
        assert deleted == 1

    def test_returns_count(self, tmp_path):
        writer, db = _make_writer(tmp_path, retention_seconds=86400)
        old_ts = time.time() - 86400 * 5
        for i in range(3):
            _add_done(db, i + 1, updated_at=old_ts)
        assert writer.cleanup_done_tasks() == 3

    def test_empty_table_returns_zero(self, tmp_path):
        writer, _ = _make_writer(tmp_path)
        assert writer.cleanup_done_tasks() == 0

    def test_does_not_touch_other_states(self, tmp_path):
        """清理方法不动 FAILED / PENDING / INFLIGHT"""
        writer, db = _make_writer(tmp_path, retention_seconds=86400)
        old_ts = time.time() - 86400 * 5
        # PENDING
        tid_p = db.insert_task(
            session_id="s1", turn_index=1, user_msg="x", assistant_resp="y",
            state="PENDING", max_attempts=3,
        )
        # DONE 旧行
        _add_done(db, 2, updated_at=old_ts)
        # FAILED 终态 旧行(用 FAILED 终态会被 delete_failed_tasks 删,不在本方法范围)
        tid_f = db.insert_task(
            session_id="s1", turn_index=3, user_msg="x", assistant_resp="y",
            state="NONE", max_attempts=3,
        )
        db.update_task_state(tid_f, "PENDING")
        db.update_task_state(tid_f, "INFLIGHT")
        db.update_task_state(tid_f, "FAILED")
        with db.transaction() as conn:
            conn.execute("UPDATE memory_tasks SET updated_at=?", (old_ts,))
        # INFLIGHT
        tid_i = db.insert_task(
            session_id="s1", turn_index=4, user_msg="x", assistant_resp="y",
            state="NONE", max_attempts=3,
        )
        db.update_task_state(tid_i, "PENDING")
        db.update_task_state(tid_i, "INFLIGHT")
        with db.transaction() as conn:
            conn.execute("UPDATE memory_tasks SET updated_at=?", (old_ts,))

        deleted = writer.cleanup_done_tasks()
        # 只删 1 行 DONE
        assert deleted == 1
        with db.transaction() as conn:
            cnt = conn.execute("SELECT COUNT(*) FROM memory_tasks").fetchone()[0]
        assert cnt == 3  # PENDING + FAILED + INFLIGHT 保留
