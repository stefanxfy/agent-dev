"""
melt_stuck_inflight 测试

Phase 2 / Step 2.2.7 — TDD 红 → 绿

- state=INFLIGHT AND inflight_at < now - max_age_seconds → state=FAILED
- 写 attempts+1, next_at = now + 60×2^(attempts-1),extraction_error='inflight timeout'
- 不动 inflight_at(给 audit 用)
- 返熔断行数
- 不熔断正常 INFLIGHT
- 不动非 INFLIGHT
"""
import time
import pytest

from agent_core.memory.meta_db import MetaDB


@pytest.fixture
def db():
    return MetaDB(":memory:")


def _add_inflight(db, turn_index=1, inflight_at=None, max_attempts=3):
    tid = db.insert_task(
        session_id="s1", turn_index=turn_index,
        user_msg="x", assistant_resp="y",
        state="NONE", max_attempts=max_attempts,
    )
    db.update_task_state(tid, "PENDING")
    db.cas_grab_task(tid, ["PENDING"], "INFLIGHT")
    if inflight_at is not None:
        with db.transaction() as conn:
            conn.execute(
                "UPDATE memory_tasks SET inflight_at=? WHERE task_id=?",
                (inflight_at, tid),
            )
    return tid


class TestMeltStuckInflight:
    """melt_stuck_inflight(max_age_seconds) — INFLIGHT 熔断"""

    def test_old_inflight_marked_failed(self, db):
        """inflight_at 超过 max_age_seconds 的 INFLIGHT → FAILED"""
        now = time.time()
        old_inflight = now - 1800 - 1  # 30 分钟 + 1 秒前
        tid = _add_inflight(db, inflight_at=old_inflight)
        melted = db.melt_stuck_inflight(max_age_seconds=1800)
        assert melted == 1
        task = db.get_task(tid)
        assert task["state"] == "FAILED"

    def test_recent_inflight_preserved(self, db):
        """inflight_at 新的 INFLIGHT 不动"""
        now = time.time()
        recent = now - 60  # 60s 前
        tid = _add_inflight(db, inflight_at=recent)
        melted = db.melt_stuck_inflight(max_age_seconds=1800)
        assert melted == 0
        task = db.get_task(tid)
        assert task["state"] == "INFLIGHT"

    def test_does_not_touch_other_states(self, db):
        """非 INFLIGHT 不动(即使 inflight_at 很久)"""
        now = time.time()
        old_ts = now - 86400 * 5
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
        # 把所有 inflight_at 改到很老
        with db.transaction() as conn:
            conn.execute("UPDATE memory_tasks SET inflight_at=?", (old_ts,))

        melted = db.melt_stuck_inflight(max_age_seconds=1800)
        assert melted == 0
        # 状态未变
        with db.transaction() as conn:
            rows = conn.execute("SELECT turn_index, state FROM memory_tasks ORDER BY turn_index").fetchall()
        assert rows[0][1] == "NONE"
        assert rows[1][1] == "PENDING"
        assert rows[2][1] == "DONE"

    def test_failed_records_attempts_increment(self, db):
        """熔断后 attempts + 1,next_at 设为下次可重试时间"""
        now = time.time()
        old_inflight = now - 1800 - 1
        tid = _add_inflight(db, inflight_at=old_inflight, max_attempts=3)
        # 原 attempts=0
        before = db.get_task(tid)
        assert before["attempts"] == 0

        db.melt_stuck_inflight(max_age_seconds=1800)
        task = db.get_task(tid)
        assert task["attempts"] == 1
        # next_at 应在 now + 60 左右
        assert task["next_at"] is not None
        assert task["next_at"] > now + 30  # 大于 now+30s(60s 减点时间误差)

    def test_failed_records_extraction_error(self, db):
        """extraction_error 写 'inflight timeout' 标记"""
        now = time.time()
        old_inflight = now - 1800 - 1
        tid = _add_inflight(db, inflight_at=old_inflight)
        db.melt_stuck_inflight(max_age_seconds=1800)
        task = db.get_task(tid)
        assert task["extraction_error"] is not None
        assert "inflight" in task["extraction_error"].lower() or "timeout" in task["extraction_error"].lower()

    def test_preserves_original_inflight_at(self, db):
        """熔断不动 inflight_at(audit 需保留)"""
        now = time.time()
        old_inflight = now - 1800 - 1
        tid = _add_inflight(db, inflight_at=old_inflight)
        db.melt_stuck_inflight(max_age_seconds=1800)
        task = db.get_task(tid)
        assert task["inflight_at"] == old_inflight

    def test_returns_count(self, db):
        now = time.time()
        old = now - 1800 - 1
        for i in range(3):
            _add_inflight(db, turn_index=i + 1, inflight_at=old)
        melted = db.melt_stuck_inflight(max_age_seconds=1800)
        assert melted == 3

    def test_max_attempts_terminal_after_melt(self, db):
        """attempts+1 >= max_attempts 后,该行变成终态 FAILED"""
        now = time.time()
        old = now - 1800 - 1
        # max_attempts=2, attempts=1 → 熔断后 attempts=2 → 终态
        tid = _add_inflight(db, inflight_at=old, max_attempts=2)
        with db.transaction() as conn:
            conn.execute("UPDATE memory_tasks SET attempts=1 WHERE task_id=?", (tid,))
        db.melt_stuck_inflight(max_age_seconds=1800)
        task = db.get_task(tid)
        assert task["state"] == "FAILED"
        assert task["attempts"] == 2
        # next_at 仍记录(给 startup_scan 步骤 6 cleanup_failed_tasks 用)
        assert task["next_at"] is not None
