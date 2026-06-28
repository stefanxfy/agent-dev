"""
cas_grab_task inflight_at 写盘测试

Phase 2 / Step 2.2.1 — TDD 红 → 绿

补强 Phase 1.2.2 已有基本 CAS 测试,覆盖 inflight_at 写盘的所有边界:
- CAS 成功 → inflight_at 自动设 now
- CAS 失败 → inflight_at 不动(可能 None,可能保留旧值)
- inflight_at 时间戳精度(用 before/after time.time() 边界)
- 多次 CAS 切换:INFLIGHT → FAILED(用 update_task_state) → PENDING(再次 CAS)
- inflight_at 不被 update_task_state 覆盖(只改 state/updated_at)
"""
import time
import pytest

from agent_core.memory.meta_db import MetaDB


@pytest.fixture
def db():
    return MetaDB(":memory:")


def _new(db, state="NONE", inflight_at=None, turn_index=1):
    return db.insert_task(
        session_id="s1", turn_index=turn_index,
        user_msg="x", assistant_resp="y",
        state=state, max_attempts=3,
    )


class TestCasGrabInflightAt:
    """CAS 成功时 inflight_at 自动写 now()"""

    def test_inflight_at_set_to_now_on_success(self, db):
        before = time.time()
        tid = _new(db, state="PENDING")
        db.cas_grab_task(tid, ["PENDING"], "INFLIGHT")
        after = time.time()
        task = db.get_task(tid)
        assert before <= task["inflight_at"] <= after

    def test_inflight_at_was_none_before_cas(self, db):
        """CAS 前 inflight_at 是 None(只 channel B grab 后才有值)"""
        tid = _new(db, state="PENDING")
        assert db.get_task(tid)["inflight_at"] is None

    def test_inflight_at_from_multiple_from_states(self, db):
        """从多种 from_states 抢占,inflight_at 都写"""
        # 用不同 turn_index 避开 UNIQUE 冲突
        for i, from_state in enumerate(("NONE", "PENDING", "FAILED"), start=1):
            tid = _new(db, state="PENDING", turn_index=i)
            if from_state != "PENDING":
                # 通过 PENDING → INFLIGHT → FAILED 路径转换
                if from_state == "FAILED":
                    db.update_task_state(tid, "INFLIGHT")
                db.update_task_state(tid, from_state)
            ok = db.cas_grab_task(tid, ["NONE", "PENDING", "FAILED"], "INFLIGHT")
            assert ok is True, f"failed to grab from {from_state}"
            assert db.get_task(tid)["inflight_at"] is not None

    def test_cas_failure_preserves_old_inflight_at(self, db):
        """CAS 失败时 inflight_at 保持原值(没被乱写)"""
        # 第一次 CAS 成功,写 inflight_at
        tid = _new(db, state="PENDING")
        db.cas_grab_task(tid, ["PENDING"], "INFLIGHT")
        original_inflight = db.get_task(tid)["inflight_at"]
        # 第二次 CAS 失败(state 已经是 INFLIGHT,不在 from_states 中)
        ok = db.cas_grab_task(tid, ["PENDING", "NONE"], "INFLIGHT")
        assert ok is False
        # inflight_at 不变
        assert db.get_task(tid)["inflight_at"] == original_inflight


class TestUpdateTaskStatePreservesInflightAt:
    """update_task_state 只改 state/updated_at,不动 inflight_at"""

    def test_update_state_does_not_touch_inflight_at(self, db):
        """channel B 把 task 推到 INFLIGHT → update_task_state(FAILED) → inflight_at 保留"""
        tid = _new(db, state="PENDING")
        db.cas_grab_task(tid, ["PENDING"], "INFLIGHT")
        original_inflight = db.get_task(tid)["inflight_at"]
        time.sleep(0.005)  # 让 updated_at 时间差异可测
        db.update_task_state(tid, "FAILED")
        task = db.get_task(tid)
        assert task["state"] == "FAILED"
        # inflight_at 保持(CAS 转移的语义:进了 INFLIGHT 就锁定这个时间)
        assert task["inflight_at"] == original_inflight

    def test_mark_done_preserves_inflight_at(self, db):
        """mark_done_with_candidates 不清 inflight_at(可能给 audit 用)"""
        tid = _new(db, state="PENDING")
        db.cas_grab_task(tid, ["PENDING"], "INFLIGHT")
        original_inflight = db.get_task(tid)["inflight_at"]
        db.mark_done_with_candidates(tid, '[{"type": "user", "title": "x", "body": "y"}]')
        task = db.get_task(tid)
        assert task["state"] == "DONE"
        # inflight_at 保留(诊断用)
        assert task["inflight_at"] == original_inflight

    def test_mark_failed_preserves_inflight_at(self, db):
        """mark_failed 不清 inflight_at(失败时 inflight_at 仍可看出何时尝试)"""
        tid = _new(db, state="PENDING")
        db.cas_grab_task(tid, ["PENDING"], "INFLIGHT")
        original_inflight = db.get_task(tid)["inflight_at"]
        db.mark_failed(tid, attempts=1, next_at=time.time() + 60, error="boom")
        task = db.get_task(tid)
        assert task["state"] == "FAILED"
        assert task["inflight_at"] == original_inflight


class TestCasGrabInflightAtTimestampOrder:
    """多次 CAS 切换的 inflight_at 时间序"""

    def test_inflight_at_updates_on_re_cas(self, db):
        """FAILED → PENDING(用 update_task_state 不动 inflight_at)
        → 再次 CAS 抢占 PENDING → INFLIGHT:inflight_at 重写为新时间"""
        tid = _new(db, state="PENDING")
        db.cas_grab_task(tid, ["PENDING"], "INFLIGHT")
        first_inflight = db.get_task(tid)["inflight_at"]
        time.sleep(0.01)
        # 模拟失败 → 重排回 PENDING
        db.update_task_state(tid, "FAILED")
        db.update_task_state(tid, "PENDING")
        time.sleep(0.01)
        # 再次 CAS 抢占
        db.cas_grab_task(tid, ["PENDING"], "INFLIGHT")
        second_inflight = db.get_task(tid)["inflight_at"]
        # 第二次 inflight_at 比第一次晚
        assert second_inflight > first_inflight
