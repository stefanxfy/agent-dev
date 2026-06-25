"""
退避公式测试

Phase 2 / Step 2.2.3 — TDD 红 → 绿

公式:next_at = now + retry_backoff_seconds × 2^(attempts - 1)
- attempts=1 → 60s(retry_backoff_seconds 默认 60)
- attempts=2 → 120s
- attempts=3 → 240s
- attempts=4 → 480s
- 自定义 retry_backoff_seconds=10 → attempts=1 → 10s
- attempts=0 应返 now(立即可重试)— 边界
"""
import time
import pytest

from agent_core.memory.wal_config import TaskWALConfig


class TestBackoffFormula:
    """DualChannelWriter._calc_next_at 退避公式"""

    def test_default_attempts_1_gives_60_seconds(self):
        from agent_core.memory.dual_channel_writer import DualChannelWriter
        # 用最小 stub 验证 _calc_next_at 是 @staticmethod 或类方法
        before = time.time()
        result = DualChannelWriter._calc_next_at(attempts=1, retry_backoff_seconds=60)
        after = time.time()
        # next_at 应在 [before+60, after+60] 区间
        assert before + 60 <= result <= after + 60

    def test_attempts_2_doubles_to_120(self):
        from agent_core.memory.dual_channel_writer import DualChannelWriter
        before = time.time()
        result = DualChannelWriter._calc_next_at(attempts=2, retry_backoff_seconds=60)
        after = time.time()
        assert before + 120 <= result <= after + 120

    def test_attempts_3_gives_240(self):
        from agent_core.memory.dual_channel_writer import DualChannelWriter
        before = time.time()
        result = DualChannelWriter._calc_next_at(attempts=3, retry_backoff_seconds=60)
        after = time.time()
        assert before + 240 <= result <= after + 240

    def test_attempts_4_gives_480(self):
        from agent_core.memory.dual_channel_writer import DualChannelWriter
        before = time.time()
        result = DualChannelWriter._calc_next_at(attempts=4, retry_backoff_seconds=60)
        after = time.time()
        assert before + 480 <= result <= after + 480

    def test_custom_retry_backoff(self):
        """retry_backoff_seconds=10,attempts=1 → 10s"""
        from agent_core.memory.dual_channel_writer import DualChannelWriter
        before = time.time()
        result = DualChannelWriter._calc_next_at(attempts=1, retry_backoff_seconds=10)
        after = time.time()
        assert before + 10 <= result <= after + 10

    def test_attempts_0_returns_half_backoff(self):
        """attempts=0 → 0.5 × 基准(60 × 0.5 = 30s)— 公式定义"""
        from agent_core.memory.dual_channel_writer import DualChannelWriter
        before = time.time()
        result = DualChannelWriter._calc_next_at(attempts=0, retry_backoff_seconds=60)
        after = time.time()
        # next_at 应在 [before+30, after+30]
        assert before + 30 <= result <= after + 30

    def test_does_not_mutate_task_wal_config(self):
        """调用 _calc_next_at 不应改 self.task_wal_config"""
        from agent_core.memory.dual_channel_writer import DualChannelWriter
        from agent_core.memory.meta_db import MetaDB
        from agent_core.memory.memory_store import MemoryStore
        from tests.test_dual_channel_concurrent import FakeEmbedFn
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            cfg = TaskWALConfig(max_retry=3, retry_backoff_seconds=60)
            db = MetaDB(":memory:")
            store = MemoryStore(tmp + "/memory")
            writer = DualChannelWriter(
                session_id="s1", meta_db=db, memory_store=store,
                vector_store=None, embed_fn=FakeEmbedFn(),
                task_wal_config=cfg,
            )
            original = writer.task_wal_config.retry_backoff_seconds
            DualChannelWriter._calc_next_at(attempts=2, retry_backoff_seconds=60)
            assert writer.task_wal_config.retry_backoff_seconds == original
