"""
DROP 旧表迁移测试

Phase 4 / Step 4.4.1 — TDD 红 → 绿

- _DDL 不再包含 CREATE TABLE cursors
- _DDL 不再包含 CREATE TABLE pending_writes
- _DDL 末尾追加 DROP TABLE IF EXISTS cursors / pending_writes
  (迁移逻辑 — 已存在的旧表也会被删)
- memory_tasks / candidates / candidate_decisions 仍保留
- MetaDB(":memory:") 仍可正常操作
"""
import pytest

from agent_core.memory.meta_db import MetaDB, _DDL


class TestDropLegacyTables:
    """_DDL 不再含旧表 CREATE,有 DROP 兜底迁移"""

    def test_ddl_does_not_create_cursors(self):
        """旧 cursors 表 CREATE 语句被删(只允许 DROP)"""
        # 仅检查 CREATE 语句 — DROP 是迁移兼容,允许
        cursor_creates = [
            line for line in _DDL.splitlines()
            if "CREATE TABLE" in line and "cursors" in line
        ]
        assert cursor_creates == [], (
            f"_DDL 不应再 CREATE 旧 cursors 表,发现: {cursor_creates}"
        )

    def test_ddl_does_not_create_pending_writes(self):
        creates = [
            line for line in _DDL.splitlines()
            if "CREATE TABLE" in line and "pending_writes" in line
        ]
        assert creates == [], (
            f"_DDL 不应再 CREATE 旧 pending_writes 表,发现: {creates}"
        )

    def test_ddl_drops_legacy_tables(self):
        """DROP 旧表(迁移用)"""
        drops = [
            line.strip() for line in _DDL.splitlines()
            if line.strip().upper().startswith("DROP TABLE")
        ]
        joined = " ".join(drops)
        assert "cursors" in joined, f"DROP 应含 cursors: {drops}"
        assert "pending_writes" in joined, f"DROP 应含 pending_writes: {drops}"

    def test_ddl_keeps_memory_tasks(self):
        """memory_tasks(新 single source of truth)仍 CREATE"""
        assert "CREATE TABLE" in _DDL
        assert "memory_tasks" in _DDL
        # 关键字段(允许空格差异)
        for col in (
            "task_id", "session_id", "turn_index", "state",
            "candidates_payload", "extraction_error",
        ):
            assert col in _DDL, f"memory_tasks 应含 {col}"
        # UNIQUE(session_id, turn_index)(允许空格)
        assert "UNIQUE" in _DDL
        assert "(session_id, turn_index)" in _DDL

    def test_ddl_keeps_candidates_table(self):
        """candidates + candidate_decisions 表保留(不是旧表,被引用)"""
        # candidates 主表
        assert "CREATE TABLE IF NOT EXISTS candidates" in _DDL
        # candidate_decisions 关联表
        assert "CREATE TABLE IF NOT EXISTS candidate_decisions" in _DDL
        # idx_candidates_status 索引也保留
        assert "idx_candidates_status" in _DDL

    def test_meta_db_in_memory_still_works(self):
        """删旧表 CREATE 后 MetaDB :memory: 仍可初始化 + 操作"""
        db = MetaDB(":memory:")
        # 试插一行 memory_tasks
        tid = db.insert_task(
            session_id="s1", turn_index=1,
            user_msg="x", assistant_resp="y",
            state="NONE", max_attempts=3,
        )
        assert tid is not None
        # 试调 list_dispatchable
        result = db.list_dispatchable_tasks()
        assert len(result) == 1
