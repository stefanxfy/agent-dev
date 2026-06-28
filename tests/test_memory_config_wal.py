"""
MemoryConfig.wal 挂载测试

Phase 1 / Step 1.2.4 — TDD 红 → 绿
- MemoryConfig().wal 返回 TaskWALConfig 实例
- wal 默认值来自 TaskWALConfig 默认值
- from_env 读 MEMORY_WAL__* 字段
"""
import pytest

from agent_core.memory.wal_config import TaskWALConfig


class TestMemoryConfigWALField:
    """MemoryConfig 暴露 wal 字段"""

    def test_wal_field_is_task_wal_config_instance(self):
        from agent_core.memory.config import MemoryConfig
        cfg = MemoryConfig()
        assert isinstance(cfg.wal, TaskWALConfig)

    def test_wal_default_max_retry_is_3(self):
        from agent_core.memory.config import MemoryConfig
        cfg = MemoryConfig()
        assert cfg.wal.max_retry == 3

    def test_wal_default_retry_backoff_is_60(self):
        from agent_core.memory.config import MemoryConfig
        cfg = MemoryConfig()
        assert cfg.wal.retry_backoff_seconds == 60

    def test_wal_default_done_retention_is_86400(self):
        from agent_core.memory.config import MemoryConfig
        cfg = MemoryConfig()
        assert cfg.wal.done_retention_seconds == 86400

    def test_wal_default_failed_retention_is_86400(self):
        from agent_core.memory.config import MemoryConfig
        cfg = MemoryConfig()
        assert cfg.wal.failed_retention_seconds == 86400

    def test_wal_field_present_in_to_dict(self):
        """to_dict() 应包含 wal 子段(序列化完整)"""
        from agent_core.memory.config import MemoryConfig
        cfg = MemoryConfig()
        d = cfg.to_dict()
        assert "wal" in d
        assert d["wal"]["max_retry"] == 3


class TestMemoryConfigWALFromEnv:
    """from_env 读 MEMORY_WAL__* 字段"""

    def test_from_env_max_retry(self, monkeypatch):
        monkeypatch.setenv("MEMORY_WAL__MAX_RETRY", "5")
        from agent_core.memory.config import MemoryConfig
        cfg = MemoryConfig.from_env()
        assert cfg.wal.max_retry == 5

    def test_from_env_retry_backoff_seconds(self, monkeypatch):
        monkeypatch.setenv("MEMORY_WAL__RETRY_BACKOFF_SECONDS", "120")
        from agent_core.memory.config import MemoryConfig
        cfg = MemoryConfig.from_env()
        assert cfg.wal.retry_backoff_seconds == 120

    def test_from_env_done_retention_days_converted(self, monkeypatch):
        """MEMORY_WAL__DONE_RETENTION_DAYS=2 → done_retention_seconds=172800"""
        monkeypatch.setenv("MEMORY_WAL__DONE_RETENTION_DAYS", "2")
        from agent_core.memory.config import MemoryConfig
        cfg = MemoryConfig.from_env()
        assert cfg.wal.done_retention_seconds == 2 * 86400

    def test_from_env_failed_retention_days_converted(self, monkeypatch):
        """MEMORY_WAL__FAILED_RETENTION_DAYS=7 → failed_retention_seconds=604800"""
        monkeypatch.setenv("MEMORY_WAL__FAILED_RETENTION_DAYS", "7")
        from agent_core.memory.config import MemoryConfig
        cfg = MemoryConfig.from_env()
        assert cfg.wal.failed_retention_seconds == 7 * 86400

    def test_from_env_unrelated_wal_field_ignored(self, monkeypatch):
        """未知 wal 字段不污染其它子段(Pydantic 拒绝 + 隔离)"""
        monkeypatch.setenv("MEMORY_WAL__MAX_RETRY", "4")
        from agent_core.memory.config import MemoryConfig
        cfg = MemoryConfig.from_env()
        # max_retry 应被设上,但其它字段保留默认
        assert cfg.wal.max_retry == 4
        assert cfg.wal.retry_backoff_seconds == 60

    def test_from_env_wal_does_not_leak_to_other_sections(self, monkeypatch):
        """wal 字段不会污染 retrieval/cost 等其它子段"""
        monkeypatch.setenv("MEMORY_WAL__MAX_RETRY", "5")
        monkeypatch.setenv("MEMORY_COST__DAILY_BUDGET_USD", "0.5")
        from agent_core.memory.config import MemoryConfig
        cfg = MemoryConfig.from_env()
        assert cfg.wal.max_retry == 5
        assert cfg.cost.daily_budget_usd == 0.5
        # 其它 wal 字段保留默认
        assert cfg.wal.retry_backoff_seconds == 60
