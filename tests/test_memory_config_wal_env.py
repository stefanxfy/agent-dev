"""
MemoryConfig.from_env 集成 wal 段验证

Phase 3 / Step 3.3.2 — TDD 红 → 绿

- 设 4 个 MEMORY_WAL__* env,MemoryConfig.from_env().wal 拿到正确值
- 没设 → .wal 是 TaskWALConfig 默认实例
- days 写法被换算
- 不影响其它段(retrieval / distillation / ...)
"""
import os
import pytest

from agent_core.memory.config import MemoryConfig
from agent_core.memory.wal_config import TaskWALConfig


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for k in list(os.environ.keys()):
        if k.startswith("MEMORY_"):
            monkeypatch.delenv(k, raising=False)
    yield


class TestMemoryConfigFromEnvWal:
    """MemoryConfig.from_env() 正确读 MEMORY_WAL__* 系列"""

    def test_no_env_uses_default_wal(self):
        cfg = MemoryConfig.from_env()
        assert isinstance(cfg.wal, TaskWALConfig)
        assert cfg.wal.max_retry == 3
        assert cfg.wal.retry_backoff_seconds == 60
        assert cfg.wal.done_retention_seconds == 86400
        assert cfg.wal.failed_retention_seconds == 86400

    def test_max_retry_from_env(self, monkeypatch):
        monkeypatch.setenv("MEMORY_WAL__MAX_RETRY", "5")
        cfg = MemoryConfig.from_env()
        assert cfg.wal.max_retry == 5

    def test_retry_backoff_from_env(self, monkeypatch):
        monkeypatch.setenv("MEMORY_WAL__RETRY_BACKOFF_SECONDS", "120")
        cfg = MemoryConfig.from_env()
        assert cfg.wal.retry_backoff_seconds == 120

    def test_done_retention_days_conversion(self, monkeypatch):
        """MEMORY_WAL__DONE_RETENTION_DAYS=7 → wal.done_retention_seconds=604800"""
        monkeypatch.setenv("MEMORY_WAL__DONE_RETENTION_DAYS", "7")
        cfg = MemoryConfig.from_env()
        assert cfg.wal.done_retention_seconds == 7 * 86400

    def test_failed_retention_days_conversion(self, monkeypatch):
        monkeypatch.setenv("MEMORY_WAL__FAILED_RETENTION_DAYS", "14")
        cfg = MemoryConfig.from_env()
        assert cfg.wal.failed_retention_seconds == 14 * 86400

    def test_all_four_wal_fields(self, monkeypatch):
        monkeypatch.setenv("MEMORY_WAL__MAX_RETRY", "5")
        monkeypatch.setenv("MEMORY_WAL__RETRY_BACKOFF_SECONDS", "30")
        monkeypatch.setenv("MEMORY_WAL__DONE_RETENTION_DAYS", "1")
        monkeypatch.setenv("MEMORY_WAL__FAILED_RETENTION_DAYS", "2")
        cfg = MemoryConfig.from_env()
        assert cfg.wal.max_retry == 5
        assert cfg.wal.retry_backoff_seconds == 30
        assert cfg.wal.done_retention_seconds == 86400
        assert cfg.wal.failed_retention_seconds == 172800

    def test_wal_does_not_affect_other_sections(self, monkeypatch):
        """设 wal env 不影响 retrieval / distillation 等"""
        monkeypatch.setenv("MEMORY_WAL__MAX_RETRY", "5")
        monkeypatch.setenv("MEMORY_RETRIEVAL__TOP_K", "20")
        cfg = MemoryConfig.from_env()
        assert cfg.wal.max_retry == 5
        assert cfg.retrieval.top_k == 20

    def test_wal_partial_env(self, monkeypatch):
        """只设一个字段,其它走默认"""
        monkeypatch.setenv("MEMORY_WAL__MAX_RETRY", "7")
        cfg = MemoryConfig.from_env()
        assert cfg.wal.max_retry == 7
        assert cfg.wal.retry_backoff_seconds == 60
        assert cfg.wal.done_retention_seconds == 86400
        assert cfg.wal.failed_retention_seconds == 86400
