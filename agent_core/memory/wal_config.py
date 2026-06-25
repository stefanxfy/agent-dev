"""
记忆任务 WAL 配置 (Phase 1 / Step 1.2.3)

设计要点:
- env 字段走 `MEMORY_WAL__<FIELD>` 双下划线约定(项目惯例)
- *_RETENTION_DAYS 是便利写法,model_validator 在 `mode="before"` 阶段换算成 *_RETENTION_SECONDS
- days 和 seconds 同时给时,days 胜出
- 5 字段,max_retry ∈ [1,10],其它 ≥ 1
"""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator


class TaskWALConfig(BaseModel):
    """记忆任务 WAL 状态机配置。

    - max_retry: FAILED 任务最大重试次数(attempts >= max_attempts 视为终态)
    - retry_backoff_seconds: 退避基准秒数(实际 = 60 × 2^(attempts-1))
    - done_retention_seconds: DONE 行保留时间(启动扫描时清理)
    - failed_retention_seconds: 终态 FAILED 行保留时间(启动扫描时清理)
    """

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    max_retry: int = Field(default=3, ge=1, le=10)
    retry_backoff_seconds: int = Field(default=60, ge=1)
    done_retention_seconds: int = Field(default=86400, ge=1)
    failed_retention_seconds: int = Field(default=86400, ge=1)

    @model_validator(mode="before")
    @classmethod
    def _convert_days(cls, data):
        """便利写法:接受 done_retention_days / failed_retention_days,转成秒。"""
        if not isinstance(data, dict):
            return data
        # done_retention_days → done_retention_seconds
        if "done_retention_days" in data:
            days = data.pop("done_retention_days")
            # days 胜出:若显式给了 days,忽略 _seconds(用户意图明确)
            data.pop("done_retention_seconds", None)
            data["done_retention_seconds"] = days * 86400
        # failed_retention_days → failed_retention_seconds
        if "failed_retention_days" in data:
            days = data.pop("failed_retention_days")
            data.pop("failed_retention_seconds", None)
            data["failed_retention_seconds"] = days * 86400
        return data


__all__ = ["TaskWALConfig"]
