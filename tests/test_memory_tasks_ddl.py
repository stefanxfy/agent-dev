"""
memory_tasks 表 DDL 验证测试

Phase 1 / Step 1.2.1 — TDD 红 → 绿
验证 meta_db._DDL 字符串包含 memory_tasks 表定义 + 3 索引 + CHECK 约束 + UNIQUE 约束。
不建实例,只断言字符串包含必要片段(快速、隔离)。
"""
from agent_core.memory.meta_db import _DDL


class TestMemoryTasksDDL:
    """memory_tasks DDL 必须满足的硬性约束"""

    def test_ddl_contains_memory_tasks_table(self):
        """_DDL 必须含 CREATE TABLE memory_tasks"""
        assert "CREATE TABLE IF NOT EXISTS memory_tasks" in _DDL

    def test_ddl_has_required_columns(self):
        """_DDL 必须含全部 13 列(task_id/state/attempts/.../candidates_payload)"""
        required_cols = [
            "task_id", "session_id", "turn_index", "state",
            "attempts", "max_attempts",
            "next_at", "inflight_at",
            "user_msg", "assistant_resp",
            "turn_metadata", "candidates_payload", "extraction_error",
            "created_at", "updated_at",
        ]
        for col in required_cols:
            assert col in _DDL, f"DDL 缺列: {col}"

    def test_ddl_has_unique_constraint(self):
        """UNIQUE(session_id, turn_index) 防止重复 turn"""
        assert "UNIQUE (session_id, turn_index)" in _DDL or \
               "UNIQUE(session_id, turn_index)" in _DDL

    def test_ddl_has_state_check_constraint(self):
        """state 字段 CHECK 约束 5 态"""
        assert "CHECK(state IN" in _DDL
        # 5 态都要在 DDL 字符串里出现
        for s in ("'NONE'", "'PENDING'", "'INFLIGHT'", "'DONE'", "'FAILED'"):
            assert s in _DDL, f"DDL 缺状态: {s}"

    def test_ddl_has_three_indexes(self):
        """3 个索引都要在 DDL 字符串里"""
        for idx in (
            "idx_tasks_state",
            "idx_tasks_session_turn",
            "idx_tasks_compaction",
        ):
            assert idx in _DDL, f"DDL 缺索引: {idx}"

    def test_ddl_state_default_none(self):
        """state 字段默认 'NONE'(persist_turn 刚落盘完未决状态)"""
        assert "DEFAULT 'NONE'" in _DDL or 'DEFAULT "NONE"' in _DDL
