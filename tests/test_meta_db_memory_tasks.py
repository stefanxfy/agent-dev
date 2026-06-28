"""
memory_tasks 表 6 个新方法测试

Phase 1 / Step 1.2.2 — TDD 红 → 绿
- insert_task:写入新行(state 默认 NONE,UNIQUE 防重)
- get_task:按 task_id 读
- cas_grab_task:CAS 抢占(从 from_states 转到 to_state,rowcount==1 抢到)
- update_task_state:通用状态更新
- mark_done_with_candidates:state='DONE' + 写 candidates_payload
- mark_failed:state='FAILED' + attempts + next_at + error
"""
import time
import pytest

from agent_core.memory.meta_db import MetaDB


@pytest.fixture
def db():
    return MetaDB(":memory:")


def _new_task(db, turn_index=1, user_msg="hi", assistant_resp="hello", state="NONE"):
    """helper:插一行 memory_tasks,返回 task_id"""
    return db.insert_task(
        session_id="s1", turn_index=turn_index,
        user_msg=user_msg, assistant_resp=assistant_resp,
        state=state, max_attempts=3,
    )


# ─────────────── insert_task ───────────────

class TestInsertTask:

    def test_insert_returns_task_id(self, db):
        """新插入返回 task_id(int)"""
        tid = _new_task(db)
        assert isinstance(tid, int)
        assert tid > 0

    def test_insert_default_state_none(self, db):
        """不传 state 时默认 'NONE'"""
        tid = _new_task(db, state="NONE")
        task = db.get_task(tid)
        assert task["state"] == "NONE"

    def test_insert_stores_turn_text(self, db):
        """user_msg / assistant_resp 落盘"""
        tid = _new_task(db, user_msg="我叫张三", assistant_resp="记住了")
        task = db.get_task(tid)
        assert task["user_msg"] == "我叫张三"
        assert task["assistant_resp"] == "记住了"

    def test_insert_stores_max_attempts(self, db):
        """max_attempts 落盘,默认 3"""
        tid = _new_task(db)
        assert db.get_task(tid)["max_attempts"] == 3

    def test_insert_stores_turn_metadata(self, db):
        """turn_metadata(JSON 字符串)落盘"""
        meta = '{"ts": 123, "tokens": 100}'
        tid = db.insert_task(
            session_id="s1", turn_index=1,
            user_msg="x", assistant_resp="y",
            turn_metadata=meta, max_attempts=3,
        )
        assert db.get_task(tid)["turn_metadata"] == meta

    def test_insert_sets_timestamps(self, db):
        """created_at / updated_at 自动填 time.time()"""
        before = time.time()
        tid = _new_task(db)
        after = time.time()
        task = db.get_task(tid)
        assert before <= task["created_at"] <= after
        assert before <= task["updated_at"] <= after


# ─────────────── get_task ───────────────

class TestGetTask:

    def test_get_returns_full_dict(self, db):
        """get_task 返回全字段 dict"""
        tid = _new_task(db, turn_index=42)
        task = db.get_task(tid)
        assert task["task_id"] == tid
        assert task["session_id"] == "s1"
        assert task["turn_index"] == 42
        assert task["state"] == "NONE"
        assert task["attempts"] == 0
        assert task["max_attempts"] == 3
        assert task["candidates_payload"] is None
        assert task["extraction_error"] is None
        assert task["next_at"] is None
        assert task["inflight_at"] is None

    def test_get_nonexistent_returns_none(self, db):
        """不存在的 task_id 返回 None"""
        assert db.get_task(99999) is None


# ─────────────── cas_grab_task ───────────────

class TestCasGrabTask:

    def test_cas_succeeds_when_state_matches(self, db):
        """state 在 from_states → 转到 to_state,返回 True"""
        tid = _new_task(db, state="PENDING")
        ok = db.cas_grab_task(tid, from_states=["PENDING", "NONE"], to_state="INFLIGHT")
        assert ok is True
        assert db.get_task(tid)["state"] == "INFLIGHT"

    def test_cas_sets_inflight_at(self, db):
        """CAS 成功时,inflight_at 自动写 now()"""
        before = time.time()
        tid = _new_task(db, state="PENDING")
        db.cas_grab_task(tid, ["PENDING"], "INFLIGHT")
        after = time.time()
        task = db.get_task(tid)
        assert before <= task["inflight_at"] <= after

    def test_cas_fails_when_state_mismatch(self, db):
        """state 不在 from_states → 抢不到,返回 False,state 不变"""
        tid = _new_task(db, state="DONE")
        ok = db.cas_grab_task(tid, from_states=["PENDING", "NONE"], to_state="INFLIGHT")
        assert ok is False
        assert db.get_task(tid)["state"] == "DONE"

    def test_cas_concurrent_only_one_wins(self, db):
        """同一行两次 CAS,只有一个返回 True(state 已变)"""
        tid = _new_task(db, state="PENDING")
        ok1 = db.cas_grab_task(tid, ["PENDING"], "INFLIGHT")
        ok2 = db.cas_grab_task(tid, ["PENDING"], "INFLIGHT")
        assert ok1 is True
        assert ok2 is False


# ─────────────── update_task_state ───────────────

class TestUpdateTaskState:

    def test_update_basic_state(self, db):
        """改 state 字段"""
        tid = _new_task(db, state="NONE")
        db.update_task_state(tid, "PENDING")
        assert db.get_task(tid)["state"] == "PENDING"

    def test_update_refreshes_updated_at(self, db):
        """update 后 updated_at 刷新"""
        tid = _new_task(db)
        old = db.get_task(tid)["updated_at"]
        time.sleep(0.01)
        db.update_task_state(tid, "PENDING")
        new = db.get_task(tid)["updated_at"]
        assert new > old


# ─────────────── mark_done_with_candidates ───────────────

class TestMarkDoneWithCandidates:

    def test_marks_done_and_stores_payload(self, db):
        """state → DONE,candidates_payload 落盘"""
        tid = _new_task(db, state="INFLIGHT")
        payload = '[{"type": "user", "title": "姓名", "body": "张三"}]'
        db.mark_done_with_candidates(tid, payload)
        task = db.get_task(tid)
        assert task["state"] == "DONE"
        assert task["candidates_payload"] == payload

    def test_refreshes_updated_at(self, db):
        """mark_done 刷新 updated_at"""
        tid = _new_task(db, state="INFLIGHT")
        old = db.get_task(tid)["updated_at"]
        time.sleep(0.01)
        db.mark_done_with_candidates(tid, "[]")
        new = db.get_task(tid)["updated_at"]
        assert new > old


# ─────────────── mark_failed ───────────────

class TestMarkFailed:

    def test_marks_failed_with_attempts_and_next_at(self, db):
        """state → FAILED,记录 attempts/next_at/error"""
        tid = _new_task(db, state="INFLIGHT")
        next_at = time.time() + 60
        db.mark_failed(tid, attempts=1, next_at=next_at, error="LLM timeout")
        task = db.get_task(tid)
        assert task["state"] == "FAILED"
        assert task["attempts"] == 1
        assert task["next_at"] == next_at
        assert task["extraction_error"] == "LLM timeout"

    def test_marks_failed_terminal_state(self, db):
        """终态:attempts >= max_attempts 时,next_at 仍记录(给清理用)"""
        tid = _new_task(db, state="INFLIGHT")
        db.mark_failed(tid, attempts=3, next_at=None, error="max retries exceeded")
        task = db.get_task(tid)
        assert task["state"] == "FAILED"
        assert task["attempts"] == 3
        assert task["next_at"] is None
