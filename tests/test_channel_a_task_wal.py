"""
channel_a_inline_write 走 memory_tasks 表测试

Phase 1 / Step 1.2.6 — TDD 红 → 绿

- 调用 channel_a_inline_write → memory_tasks 新增一行(state=NONE)
- 不再写 JSONL 文件
- 不再 add_pending / remove_pending
- 不再 set_cursor
- self.daily_cursor 仍维护(同旧语义,Phase 4 删)
- 显式 turn_index < 旧 cursor → 幂等跳过(不调 insert_task)
- turn_index=None → 内部用 daily_cursor + 1
- 同 (session, turn) 二次调用 → 幂等不抛
"""
import json
import time
import sqlite3
from pathlib import Path

import pytest


def _make_writer(tmp_path, **overrides):
    from agent_core.memory.dual_channel_writer import DualChannelWriter
    from agent_core.memory.meta_db import MetaDB
    from agent_core.memory.memory_store import MemoryStore
    from tests.test_dual_channel_concurrent import FakeEmbedFn

    db = MetaDB(":memory:")
    store = MemoryStore(tmp_path / "memory")
    embed = FakeEmbedFn()
    writer = DualChannelWriter(
        session_id="s-wal-a",
        meta_db=db,
        memory_store=store,
        vector_store=None,
        embed_fn=embed,
        **overrides,
    )
    return writer, db


def _count_tasks(db, session_id="s-wal-a"):
    with db.transaction() as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM memory_tasks WHERE session_id=?", (session_id,)
        ).fetchone()[0]


def _list_tasks(db, session_id="s-wal-a"):
    with db.transaction() as conn:
        rows = conn.execute(
            "SELECT turn_index, state, user_msg, assistant_resp FROM memory_tasks "
            "WHERE session_id=? ORDER BY turn_index",
            (session_id,),
        ).fetchall()
    return rows


def _count_pending(db, session_id="s-wal-a"):
    with db.transaction() as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM pending_writes WHERE session_id=?", (session_id,)
        ).fetchone()[0]


def _count_cursors(db, session_id="s-wal-a"):
    with db.transaction() as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM cursors WHERE session_id=?", (session_id,)
        ).fetchone()[0]


class TestChannelAWritesToNewTable:
    """channel_a_inline_write 直接走 memory_tasks"""

    def test_writes_to_memory_tasks(self, tmp_path):
        writer, db = _make_writer(tmp_path)
        writer.channel_a_inline_write("hi", "hello")
        rows = _list_tasks(db)
        assert len(rows) == 1
        assert rows[0][0] == 1
        assert rows[0][1] == "NONE"  # Channel A 刚落盘,未决
        assert rows[0][2] == "hi"
        assert rows[0][3] == "hello"

    def test_no_jsonl_file_created(self, tmp_path):
        """不再写 daily log JSONL 文件"""
        writer, db = _make_writer(tmp_path)
        writer.channel_a_inline_write("hi", "hello")
        # 找 .jsonl 文件
        jsonl_files = list(tmp_path.rglob("*.jsonl"))
        assert jsonl_files == [], f"不应有 JSONL,但发现: {jsonl_files}"

    def test_no_add_pending_called(self, tmp_path):
        """不再 add_pending / remove_pending"""
        writer, db = _make_writer(tmp_path)
        writer.channel_a_inline_write("hi", "hello")
        assert _count_pending(db) == 0

    def test_no_cursor_table_written(self, tmp_path):
        """不再 set_cursor(cursors 表不增加行)"""
        writer, db = _make_writer(tmp_path)
        writer.channel_a_inline_write("hi", "hello")
        assert _count_cursors(db) == 0

    def test_returns_new_turn_index(self, tmp_path):
        writer, _ = _make_writer(tmp_path)
        idx = writer.channel_a_inline_write("hi", "hello")
        assert idx == 1  # 第一次写 → turn 1

    def test_sequential_writes_increment_turn(self, tmp_path):
        writer, db = _make_writer(tmp_path)
        i1 = writer.channel_a_inline_write("a", "A")
        i2 = writer.channel_a_inline_write("b", "B")
        i3 = writer.channel_a_inline_write("c", "C")
        assert (i1, i2, i3) == (1, 2, 3)
        assert _count_tasks(db) == 3
        rows = _list_tasks(db)
        assert [r[0] for r in rows] == [1, 2, 3]

    def test_explicit_turn_index_advances(self, tmp_path):
        writer, _ = _make_writer(tmp_path)
        i1 = writer.channel_a_inline_write("a", "A", turn_index=10)
        i2 = writer.channel_a_inline_write("b", "B", turn_index=11)
        assert (i1, i2) == (10, 11)

    def test_state_field_is_none_after_channel_a(self, tmp_path):
        """Channel A 落盘后,state 应是 NONE(等 Channel B 走完后变 PENDING)"""
        writer, db = _make_writer(tmp_path)
        writer.channel_a_inline_write("x", "y")
        with db.transaction() as conn:
            task = conn.execute(
                "SELECT state FROM memory_tasks WHERE session_id=? AND turn_index=1",
                ("s-wal-a",),
            ).fetchone()
        assert task[0] == "NONE"


class TestChannelAIdempotency:
    """幂等去重 / 跳过"""

    def test_explicit_turn_index_below_cursor_noop(self, tmp_path):
        """传 turn_index <= daily_cursor → 跳过,不入表"""
        writer, db = _make_writer(tmp_path)
        writer.channel_a_inline_write("first", "1st")
        # 再用显式小 turn_index 调
        idx = writer.channel_a_inline_write("dup", "dup", turn_index=0)
        assert idx == 1  # 返回当前 cursor
        assert _count_tasks(db) == 1  # 没有新行
        # 验证表里还是 1 行(Step 1.2.7 的 UNIQUE 冲突测试是底层 insert_task 的)


class TestChannelAMaintainsDailyCursor:
    """为 Phase 4 删前兼容,writer.daily_cursor 仍同步推进"""

    def test_daily_cursor_attribute_exists(self, tmp_path):
        writer, _ = _make_writer(tmp_path)
        assert hasattr(writer, "daily_cursor")
        assert writer.daily_cursor == 0  # 初始

    def test_daily_cursor_advances_after_write(self, tmp_path):
        writer, _ = _make_writer(tmp_path)
        writer.channel_a_inline_write("a", "A")
        assert writer.daily_cursor == 1
        writer.channel_a_inline_write("b", "B")
        assert writer.daily_cursor == 2
