"""
TaskWALConfig.from_env 测试

Phase 3 / Step 3.3.1 — TDD 红 → 绿

- 不设 env → 用默认值
- 设 MEMORY_WAL__MAX_RETRY=5 → max_retry=5
- 设 MEMORY_WAL__DONE_RETENTION_DAYS=7 → done_retention_seconds=604800
- days 和 seconds 同时给 → days 胜出
- 类型推断: int / bool / 字符串
- 非 MEMORY_WAL_ 前缀被忽略
"""
import os
import pytest

from agent_core.memory.wal_config import TaskWALConfig


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    """每个测试前清掉所有 MEMORY_WAL__* env,避免污染。"""
    for k in list(os.environ.keys()):
        if k.startswith("MEMORY_WAL__"):
            monkeypatch.delenv(k, raising=False)
    yield


class TestTaskWALConfigFromEnv:
    """TaskWALConfig.from_env(prefix='MEMORY_WAL_') -> TaskWALConfig"""

    def test_no_env_returns_defaults(self):
        cfg = TaskWALConfig.from_env()
        assert cfg.max_retry == 3
        assert cfg.retry_backoff_seconds == 60
        assert cfg.done_retention_seconds == 86400
        assert cfg.failed_retention_seconds == 86400

    def test_max_retry_from_env(self, monkeypatch):
        monkeypatch.setenv("MEMORY_WAL__MAX_RETRY", "5")
        cfg = TaskWALConfig.from_env()
        assert cfg.max_retry == 5

    def test_retry_backoff_from_env(self, monkeypatch):
        monkeypatch.setenv("MEMORY_WAL__RETRY_BACKOFF_SECONDS", "120")
        cfg = TaskWALConfig.from_env()
        assert cfg.retry_backoff_seconds == 120

    def test_done_retention_seconds_from_env(self, monkeypatch):
        monkeypatch.setenv("MEMORY_WAL__DONE_RETENTION_SECONDS", "172800")
        cfg = TaskWALConfig.from_env()
        assert cfg.done_retention_seconds == 172800

    def test_done_retention_days_conversion(self, monkeypatch):
        """MEMORY_WAL__DONE_RETENTION_DAYS=7 → 7*86400=604800 seconds"""
        monkeypatch.setenv("MEMORY_WAL__DONE_RETENTION_DAYS", "7")
        cfg = TaskWALConfig.from_env()
        assert cfg.done_retention_seconds == 604800

    def test_failed_retention_days_conversion(self, monkeypatch):
        monkeypatch.setenv("MEMORY_WAL__FAILED_RETENTION_DAYS", "14")
        cfg = TaskWALConfig.from_env()
        assert cfg.failed_retention_seconds == 14 * 86400

    def test_days_wins_over_seconds(self, monkeypatch):
        """同时给 days 和 seconds → days 胜出"""
        monkeypatch.setenv("MEMORY_WAL__DONE_RETENTION_DAYS", "3")
        monkeypatch.setenv("MEMORY_WAL__DONE_RETENTION_SECONDS", "99999")
        cfg = TaskWALConfig.from_env()
        assert cfg.done_retention_seconds == 3 * 86400

    def test_all_four_fields_from_env(self, monkeypatch):
        monkeypatch.setenv("MEMORY_WAL__MAX_RETRY", "5")
        monkeypatch.setenv("MEMORY_WAL__RETRY_BACKOFF_SECONDS", "30")
        monkeypatch.setenv("MEMORY_WAL__DONE_RETENTION_DAYS", "1")
        monkeypatch.setenv("MEMORY_WAL__FAILED_RETENTION_DAYS", "2")
        cfg = TaskWALConfig.from_env()
        assert cfg.max_retry == 5
        assert cfg.retry_backoff_seconds == 30
        assert cfg.done_retention_seconds == 86400
        assert cfg.failed_retention_seconds == 172800

    def test_invalid_env_raises(self, monkeypatch):
        """越界值 (max_retry>10) 抛 ValidationError"""
        from pydantic import ValidationError
        monkeypatch.setenv("MEMORY_WAL__MAX_RETRY", "100")
        with pytest.raises(ValidationError):
            TaskWALConfig.from_env()

    def test_non_wal_env_ignored(self, monkeypatch):
        """MEMORY_RETRIEVAL__MODE 这类不被 TaskWALConfig 解析"""
        monkeypatch.setenv("MEMORY_RETRIEVAL__MODE", "vector")
        cfg = TaskWALConfig.from_env()
        # 走默认
        assert cfg.max_retry == 3
