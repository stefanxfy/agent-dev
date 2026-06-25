"""
startup_scan 端到端集成测试

Phase 2 / Step 2.2.10 — TDD 红 → 绿

startup_scan() 是启动时调用的 4 步恢复流程:
  1a. delete_done_tasks  — 清理旧 DONE(retention 之外)
  1b. delete_failed_tasks — 清理旧 FAILED 终态(retention 之外)
  2.  melt_stuck_inflight  — 熔断超时 INFLIGHT → FAILED
  3.  reschedule_retryable_failed — FAILED → PENDING (next_at 到期)
  4.  list_dispatchable_tasks — 派工 NONE / PENDING

混合状态测试:塞入以下样本,跑一次 startup_scan,断言最终表里:
  - DONE 旧行被删
  - FAILED 终态旧行被删
  - INFLIGHT-stuck 转 FAILED
  - FAILED-retryable 转 PENDING
  - NONE/PENDING 进入派工列表
"""
import time
import pytest

from agent_core.memory.meta_db import MetaDB
from agent_core.memory.dual_channel_writer import DualChannelWriter
from agent_core.memory.memory_store import MemoryStore
from tests.test_dual_channel_concurrent import FakeEmbedFn


@pytest.fixture
def db():
    return MetaDB(":memory:")


def _make_writer(tmp_path, db, *, session_id="s1"):
    store = MemoryStore(tmp_path / "memory")
    return DualChannelWriter(
        session_id=session_id,
        meta_db=db,
        memory_store=store,
        vector_store=None,
        embed_fn=FakeEmbedFn(),
    )


def _add_done(db, turn_index, *, updated_at=None):
    """加一行 DONE。updated_at 默认现在(不会被 retention 删)"""
    tid = db.insert_task(
        session_id="s1", turn_index=turn_index,
        user_msg="x", assistant_resp="y",
        state="NONE", max_attempts=3,
    )
    db.update_task_state(tid, "PENDING")
    db.update_task_state(tid, "INFLIGHT")
    db.mark_done_with_candidates(tid, "[]")
    if updated_at is not None:
        with db.transaction() as conn:
            conn.execute("UPDATE memory_tasks SET updated_at=? WHERE task_id=?", (updated_at, tid))
    return tid


def _add_failed_terminal(db, turn_index, *, updated_at=None, attempts=3, max_attempts=3):
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
            (attempts, time.time() - 100, tid),
        )
    if updated_at is not None:
        with db.transaction() as conn:
            conn.execute("UPDATE memory_tasks SET updated_at=? WHERE task_id=?", (updated_at, tid))
    return tid


def _add_failed_retryable(db, turn_index, *, next_at):
    """FAILED 但 attempts < max,next_at 给定(可能过期或未到)"""
    tid = db.insert_task(
        session_id="s1", turn_index=turn_index,
        user_msg="x", assistant_resp="y",
        state="NONE", max_attempts=3,
    )
    db.update_task_state(tid, "PENDING")
    db.update_task_state(tid, "INFLIGHT")
    db.update_task_state(tid, "FAILED")
    with db.transaction() as conn:
        conn.execute(
            "UPDATE memory_tasks SET attempts=1, next_at=? WHERE task_id=?",
            (next_at, tid),
        )
    return tid


def _add_inflight_stuck(db, turn_index, *, inflight_at):
    """直接置 INFLIGHT(不走 CAS)— 测试只想建 fixture,attempts 从 0 开始

    偏差 2 修复后,cas_grab_task 会 attempts+1;这里直写 INFLIGHT
    避免污染(测试断言要 attempts=1 表示熔断 +1)
    """
    tid = db.insert_task(
        session_id="s1", turn_index=turn_index,
        user_msg="x", assistant_resp="y",
        state="NONE", max_attempts=3,
    )
    db.update_task_state(tid, "INFLIGHT")
    with db.transaction() as conn:
        conn.execute(
            "UPDATE memory_tasks SET inflight_at=? WHERE task_id=?",
            (inflight_at, tid),
        )
    return tid


def _add_pending(db, turn_index):
    tid = db.insert_task(
        session_id="s1", turn_index=turn_index,
        user_msg="x", assistant_resp="y",
        state="NONE", max_attempts=3,
    )
    db.update_task_state(tid, "PENDING")
    return tid


def _add_none(db, turn_index):
    return db.insert_task(
        session_id="s1", turn_index=turn_index,
        user_msg="x", assistant_resp="y",
        state="NONE", max_attempts=3,
    )


def _get_state(db, tid):
    with db.transaction() as conn:
        row = conn.execute(
            "SELECT state FROM memory_tasks WHERE task_id=?", (tid,)
        ).fetchone()
    return row[0] if row else None


def _row_exists(db, tid):
    with db.transaction() as conn:
        row = conn.execute(
            "SELECT 1 FROM memory_tasks WHERE task_id=?", (tid,)
        ).fetchone()
    return row is not None


class TestStartupScanEnd2End:
    """混合状态 → startup_scan → 期望最终态"""

    def test_cleans_old_done(self, tmp_path, db):
        """DONE 旧行(>done_retention)被删,新行保留"""
        old_ts = time.time() - 86400 * 5
        tid_old = _add_done(db, 1, updated_at=old_ts)
        tid_new = _add_done(db, 2, updated_at=time.time() - 60)

        writer = _make_writer(tmp_path, db)
        result = writer.startup_scan()

        assert result["done_deleted"] == 1
        assert not _row_exists(db, tid_old), "旧 DONE 应被删"
        assert _row_exists(db, tid_new), "新 DONE 应保留"

    def test_cleans_old_failed_terminal(self, tmp_path, db):
        """FAILED 终态旧行被删,新行保留,退避中保留"""
        old_ts = time.time() - 86400 * 5
        tid_terminal_old = _add_failed_terminal(db, 1, updated_at=old_ts, attempts=3, max_attempts=3)
        tid_terminal_new = _add_failed_terminal(db, 2, updated_at=time.time() - 60, attempts=3, max_attempts=3)
        tid_retryable = _add_failed_retryable(db, 3, next_at=time.time() + 100)

        writer = _make_writer(tmp_path, db)
        result = writer.startup_scan()

        assert result["failed_deleted"] == 1
        assert not _row_exists(db, tid_terminal_old), "终态旧 FAILED 应被删"
        assert _row_exists(db, tid_terminal_new), "新 FAILED 应保留"
        assert _row_exists(db, tid_retryable), "退避中 FAILED 应保留"

    def test_melts_stuck_inflight(self, tmp_path, db):
        """INFLIGHT-stuck → FAILED(attempts+1)"""
        now = time.time()
        old = now - 1800 - 1
        tid = _add_inflight_stuck(db, 1, inflight_at=old)

        writer = _make_writer(tmp_path, db)
        result = writer.startup_scan()

        assert result["inflight_melted"] == 1
        assert _get_state(db, tid) == "FAILED"
        task = db.get_task(tid)
        assert task["attempts"] == 1
        assert task["extraction_error"] is not None

    def test_reschedules_overdue_failed(self, tmp_path, db):
        """FAILED 退避到期(next_at <= now)→ PENDING"""
        now = time.time()
        tid_due = _add_failed_retryable(db, 1, next_at=now - 60)
        tid_future = _add_failed_retryable(db, 2, next_at=now + 100)

        writer = _make_writer(tmp_path, db)
        result = writer.startup_scan()

        assert result["failed_rescheduled"] == 1
        assert _get_state(db, tid_due) == "PENDING"
        assert _get_state(db, tid_future) == "FAILED"

    def test_dispatchable_list_includes_pending_and_none(self, tmp_path, db):
        """startup_scan 返回派工列表含 NONE + PENDING,按 turn_index ASC"""
        _add_done(db, 1)  # 不在派工列表
        _add_none(db, 3)
        _add_pending(db, 2)
        _add_failed_terminal(db, 4)  # 终态 — 不派工
        # inflight 不在派工列表
        now = time.time()
        _add_inflight_stuck(db, 5, inflight_at=now - 100)  # 100s 前 — 不算 stuck (30min)

        writer = _make_writer(tmp_path, db)
        result = writer.startup_scan()
        dispatched = result["dispatchable"]

        turn_indices = [r["turn_index"] for r in dispatched]
        assert turn_indices == [2, 3]
        states = {r["state"] for r in dispatched}
        assert states == {"NONE", "PENDING"}

    def test_mixed_scenario_full_pipeline(self, tmp_path, db):
        """完整 4 步 + 派工:一次性跑全套"""
        now = time.time()
        # 旧 DONE → 删
        old_done = _add_done(db, 1, updated_at=now - 86400 * 5)
        # 新 DONE → 留
        new_done = _add_done(db, 2, updated_at=now - 60)
        # 旧 FAILED 终态 → 删
        old_failed = _add_failed_terminal(
            db, 3, updated_at=now - 86400 * 5, attempts=3, max_attempts=3
        )
        # INFLIGHT-stuck → 熔断
        stuck = _add_inflight_stuck(db, 4, inflight_at=now - 1800 - 1)
        # FAILED 退避到期 → 重排
        retryable_due = _add_failed_retryable(db, 5, next_at=now - 60)
        # NONE → 派工
        none_t = _add_none(db, 6)
        # PENDING → 派工
        pending_t = _add_pending(db, 7)

        writer = _make_writer(tmp_path, db)
        result = writer.startup_scan()

        # 验证每步结果
        assert result["done_deleted"] == 1
        assert result["failed_deleted"] == 1
        assert result["inflight_melted"] == 1
        assert result["failed_rescheduled"] == 1

        # 验证行最终态
        assert not _row_exists(db, old_done)
        assert _row_exists(db, new_done)
        assert not _row_exists(db, old_failed)
        assert _get_state(db, stuck) == "FAILED"
        assert _get_state(db, retryable_due) == "PENDING"
        # 派工列表含 (6, NONE) 和 (7, PENDING)
        dispatched = result["dispatchable"]
        turn_states = [(r["turn_index"], r["state"]) for r in dispatched]
        assert (6, "NONE") in turn_states
        assert (7, "PENDING") in turn_states
        # 还有 (5, PENDING) — 重排后变 PENDING
        assert (5, "PENDING") in turn_states
        # 总数 = 3
        assert len(dispatched) == 3


class TestStartupScanReturnShape:
    """startup_scan() 返 dict 字段契约"""

    def test_return_has_all_step_results(self, tmp_path, db):
        writer = _make_writer(tmp_path, db)
        result = writer.startup_scan()
        assert "done_deleted" in result
        assert "failed_deleted" in result
        assert "inflight_melted" in result
        assert "failed_rescheduled" in result
        assert "dispatchable" in result

    def test_empty_table_startup_scan(self, tmp_path, db):
        """空表 startup_scan 不崩,全返 0 / []"""
        writer = _make_writer(tmp_path, db)
        result = writer.startup_scan()
        assert result["done_deleted"] == 0
        assert result["failed_deleted"] == 0
        assert result["inflight_melted"] == 0
        assert result["failed_rescheduled"] == 0
        assert result["dispatchable"] == []
