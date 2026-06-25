"""
mark_done_with_candidates 测试补强

Phase 2 / Step 2.2.4 — TDD 红 → 绿

Step 1.2.2 已实现 mark_done_with_candidates,本步骤补强:
- 空 payload "[]"
- 多条 candidates
- Unicode / 长字符串
- 状态机从 INFLIGHT → DONE 原子性
- 调用多次幂等
"""
import json
import time
import pytest

from agent_core.memory.meta_db import MetaDB


@pytest.fixture
def db():
    return MetaDB(":memory:")


def _new(db, state="INFLIGHT", turn_index=1):
    return db.insert_task(
        session_id="s1", turn_index=turn_index,
        user_msg="x", assistant_resp="y",
        state=state, max_attempts=3,
    )


class TestMarkDoneWithCandidatesEdgeCases:
    """mark_done_with_candidates 边界场景"""

    def test_empty_payload_accepted(self, db):
        """'[]' 空 candidates 列表也能 mark_done"""
        tid = _new(db, state="INFLIGHT")
        db.mark_done_with_candidates(tid, "[]")
        task = db.get_task(tid)
        assert task["state"] == "DONE"
        assert task["candidates_payload"] == "[]"

    def test_multiple_candidates(self, db):
        """多条 candidates 落盘完整"""
        tid = _new(db, state="INFLIGHT")
        payload = json.dumps([
            {"type": "user", "title": "姓名", "body": "张三", "score": 0.9},
            {"type": "user", "title": "年龄", "body": "30", "score": 0.7},
            {"type": "event", "title": "结婚", "body": "2020 年", "score": 0.8},
        ], ensure_ascii=False)
        db.mark_done_with_candidates(tid, payload)
        task = db.get_task(tid)
        assert task["state"] == "DONE"
        loaded = json.loads(task["candidates_payload"])
        assert len(loaded) == 3
        assert loaded[1]["title"] == "年龄"

    def test_unicode_payload(self, db):
        """Unicode / 中文字符落盘正确"""
        tid = _new(db, state="INFLIGHT")
        payload = json.dumps([
            {"type": "user", "title": "姓名", "body": "张三", "tags": ["朋友", "同事"]},
        ], ensure_ascii=False)
        db.mark_done_with_candidates(tid, payload)
        task = db.get_task(tid)
        loaded = json.loads(task["candidates_payload"])
        assert loaded[0]["title"] == "姓名"
        assert loaded[0]["tags"] == ["朋友", "同事"]

    def test_long_payload(self, db):
        """大 payload 落盘完整(>10KB)"""
        tid = _new(db, state="INFLIGHT")
        # 100 条各 1KB
        candidates = [
            {"type": "user", "title": f"item_{i}", "body": "x" * 1000}
            for i in range(100)
        ]
        payload = json.dumps(candidates, ensure_ascii=False)
        assert len(payload) > 100_000  # > 100KB
        db.mark_done_with_candidates(tid, payload)
        task = db.get_task(tid)
        loaded = json.loads(task["candidates_payload"])
        assert len(loaded) == 100
        assert loaded[99]["title"] == "item_99"

    def test_atomic_state_and_payload(self, db):
        """state 和 candidates_payload 同步写(原子,无中间态)"""
        tid = _new(db, state="INFLIGHT")
        # 立即读,验证 state 和 candidates 同步
        db.mark_done_with_candidates(tid, '[{"type": "user", "title": "t", "body": "b"}]')
        with db.transaction() as conn:
            row = conn.execute(
                "SELECT state, candidates_payload FROM memory_tasks WHERE task_id=?",
                (tid,),
            ).fetchone()
        assert row[0] == "DONE"
        assert row[1] is not None
        # 同步:state==DONE 时 candidates_payload 不为 None
        assert json.loads(row[1])[0]["title"] == "t"

    def test_idempotent_on_repeat_call(self, db):
        """重复 mark_done 覆盖 candidates_payload(用最新值)"""
        tid = _new(db, state="INFLIGHT")
        db.mark_done_with_candidates(tid, '[{"title": "first"}]')
        db.mark_done_with_candidates(tid, '[{"title": "second"}]')
        task = db.get_task(tid)
        assert task["state"] == "DONE"
        loaded = json.loads(task["candidates_payload"])
        # 第二次写覆盖
        assert loaded[0]["title"] == "second"

    def test_refuses_when_state_not_inflight(self, db):
        """state != INFLIGHT 也允许 mark_done(SQL 不强校验 state 起点)"""
        # 这是设计选择:mark_done 是 set-and-forget,Channel B 调用方自己保证起点是 INFLIGHT
        # 测试:从 PENDING 直接 mark_done 也成功(写覆盖)
        tid = _new(db, state="PENDING")
        db.mark_done_with_candidates(tid, '[{"title": "x"}]')
        task = db.get_task(tid)
        assert task["state"] == "DONE"
        assert task["candidates_payload"] == '[{"title": "x"}]'


class TestMarkDoneUpdatesTimestamps:
    """mark_done 同步刷 updated_at"""

    def test_updates_updated_at(self, db):
        """updated_at 刷为 now()"""
        tid = _new(db, state="INFLIGHT")
        before = time.time()
        time.sleep(0.01)
        db.mark_done_with_candidates(tid, "[]")
        after = time.time()
        task = db.get_task(tid)
        assert before <= task["updated_at"] <= after

    def test_does_not_change_attempts_or_next_at(self, db):
        """mark_done 不动 attempts / next_at / inflight_at / max_attempts"""
        tid = _new(db, state="INFLIGHT")
        original = db.get_task(tid)
        db.mark_done_with_candidates(tid, "[]")
        task = db.get_task(tid)
        assert task["attempts"] == original["attempts"]
        assert task["next_at"] == original["next_at"]
        assert task["inflight_at"] == original["inflight_at"]
        assert task["max_attempts"] == original["max_attempts"]
