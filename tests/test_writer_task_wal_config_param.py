"""
DualChannelWriter.__init__ 加 task_wal_config 参数测试

Phase 1 / Step 1.2.5 — TDD 红 → 绿

- 默认值:TaskWALConfig()(不传时)
- 显式传:实例化后 self.task_wal_config 等于传入值
- 必须能被从外部读取(public 属性)
- 传入 None 抛错(防呆)
"""
import pytest

from agent_core.memory.wal_config import TaskWALConfig


def _make_minimal_writer(monkeypatch, tmp_path, **overrides):
    """构造一个最小可用的 DualChannelWriter(隔离环境,避免真实 chroma/bge-m3 加载)。"""
    from agent_core.memory.dual_channel_writer import DualChannelWriter
    from agent_core.memory.meta_db import MetaDB
    from agent_core.memory.memory_store import MemoryStore
    from tests.test_dual_channel_concurrent import FakeEmbedFn

    db = MetaDB(":memory:")
    store = MemoryStore(tmp_path / "memory")
    embed = FakeEmbedFn()
    writer = DualChannelWriter(
        session_id="s1",
        meta_db=db,
        memory_store=store,
        vector_store=None,        # Phase 1 不调 extract_candidates
        embed_fn=embed,
        **overrides,
    )
    return writer


class TestDualChannelWriterTaskWALConfigParam:
    """task_wal_config 参数"""

    def test_default_is_task_wal_config_instance(self, tmp_path):
        writer = _make_minimal_writer(None, tmp_path)
        assert isinstance(writer.task_wal_config, TaskWALConfig)

    def test_default_uses_task_wal_config_defaults(self, tmp_path):
        writer = _make_minimal_writer(None, tmp_path)
        assert writer.task_wal_config.max_retry == 3
        assert writer.task_wal_config.retry_backoff_seconds == 60

    def test_explicit_override(self, tmp_path):
        cfg = TaskWALConfig(max_retry=5, retry_backoff_seconds=30)
        writer = _make_minimal_writer(None, tmp_path, task_wal_config=cfg)
        assert writer.task_wal_config is cfg
        assert writer.task_wal_config.max_retry == 5
        assert writer.task_wal_config.retry_backoff_seconds == 30

    def test_explicit_with_retention_days(self, tmp_path):
        """从外部构造 TaskWALConfig(days=...) 也能传进来,验证 days 换算后保留"""
        cfg = TaskWALConfig(done_retention_days=2, failed_retention_days=3)
        writer = _make_minimal_writer(None, tmp_path, task_wal_config=cfg)
        assert writer.task_wal_config.done_retention_seconds == 2 * 86400
        assert writer.task_wal_config.failed_retention_seconds == 3 * 86400

    def test_none_raises(self, tmp_path):
        """传 None 显式拒绝 — 防呆,避免下游忘传"""
        from agent_core.memory.dual_channel_writer import DualChannelWriter
        from agent_core.memory.meta_db import MetaDB
        from agent_core.memory.memory_store import MemoryStore
        from tests.test_dual_channel_concurrent import FakeEmbedFn

        with pytest.raises((ValueError, TypeError, AssertionError)):
            DualChannelWriter(
                session_id="s1",
                meta_db=MetaDB(":memory:"),
                memory_store=MemoryStore(tmp_path / "memory"),
                vector_store=None,
                embed_fn=FakeEmbedFn(),
                task_wal_config=None,
            )
