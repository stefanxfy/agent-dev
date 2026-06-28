"""
TaskWALConfig Pydantic 模型测试

Phase 1 / Step 1.2.3 — TDD 红 → 绿
- 4 个核心字段默认值
- model_validator 把 *_RETENTION_DAYS 换算成 *_RETENTION_SECONDS
- days 和 seconds 互斥,days 赢
- 字段约束(ge=1,le=10)
"""
import pytest
from pydantic import ValidationError


class TestTaskWALConfigDefaults:
    """不传任何参数时的默认值"""

    def test_default_max_retry_is_3(self):
        from agent_core.memory.wal_config import TaskWALConfig
        assert TaskWALConfig().max_retry == 3

    def test_default_retry_backoff_is_60_seconds(self):
        from agent_core.memory.wal_config import TaskWALConfig
        assert TaskWALConfig().retry_backoff_seconds == 60

    def test_default_done_retention_is_86400_seconds(self):
        """1 天 = 86400 秒"""
        from agent_core.memory.wal_config import TaskWALConfig
        assert TaskWALConfig().done_retention_seconds == 86400

    def test_default_failed_retention_is_86400_seconds(self):
        from agent_core.memory.wal_config import TaskWALConfig
        assert TaskWALConfig().failed_retention_seconds == 86400


class TestTaskWALConfigDaysToSecondsConversion:
    """model_validator 把 _DAYS 换算成 _SECONDS"""

    def test_done_days_converted_to_seconds(self):
        from agent_core.memory.wal_config import TaskWALConfig
        cfg = TaskWALConfig(done_retention_days=3)
        assert cfg.done_retention_seconds == 3 * 86400

    def test_failed_days_converted_to_seconds(self):
        from agent_core.memory.wal_config import TaskWALConfig
        cfg = TaskWALConfig(failed_retention_days=7)
        assert cfg.failed_retention_seconds == 7 * 86400

    def test_days_not_in_final_model(self):
        """model_validator 移除中间字段,model 里只留 _SECONDS"""
        from agent_core.memory.wal_config import TaskWALConfig
        TaskWALConfig(done_retention_days=2)
        # model_fields 是 class 属性(Pydantic 2.11+ 强制)
        field_names = set(TaskWALConfig.model_fields.keys())
        assert "done_retention_days" not in field_names
        assert "done_retention_seconds" in field_names

    def test_days_overrides_seconds_when_both_given(self):
        """同时给 days 和 seconds,days 赢"""
        from agent_core.memory.wal_config import TaskWALConfig
        cfg = TaskWALConfig(done_retention_days=2, done_retention_seconds=999)
        assert cfg.done_retention_seconds == 2 * 86400


class TestTaskWALConfigConstraints:
    """字段边界"""

    def test_max_retry_must_be_at_least_1(self):
        from agent_core.memory.wal_config import TaskWALConfig
        with pytest.raises(ValidationError):
            TaskWALConfig(max_retry=0)

    def test_max_retry_must_be_at_most_10(self):
        from agent_core.memory.wal_config import TaskWALConfig
        with pytest.raises(ValidationError):
            TaskWALConfig(max_retry=11)

    def test_retry_backoff_must_be_at_least_1(self):
        from agent_core.memory.wal_config import TaskWALConfig
        with pytest.raises(ValidationError):
            TaskWALConfig(retry_backoff_seconds=0)

    def test_done_retention_must_be_at_least_1(self):
        from agent_core.memory.wal_config import TaskWALConfig
        with pytest.raises(ValidationError):
            TaskWALConfig(done_retention_seconds=0)

    def test_extra_field_rejected(self):
        """extra='forbid' 拒绝未知字段"""
        from agent_core.memory.wal_config import TaskWALConfig
        with pytest.raises(ValidationError):
            TaskWALConfig(unknown_field=1)
