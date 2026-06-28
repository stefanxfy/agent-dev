"""
记忆任务 WAL 配置 (Phase 1 / Step 1.2.3 + Phase 3 / Step 3.3.1)

设计要点:
- env 字段走 `MEMORY_WAL__<FIELD>` 双下划线约定(项目惯例)
- *_RETENTION_DAYS 是便利写法,model_validator 在 `mode="before"` 阶段换算成 *_RETENTION_SECONDS
- days 和 seconds 同时给时,days 胜出
- 5 字段,max_retry ∈ [1,10],其它 ≥ 1
"""
from __future__ import annotations

import os

from pydantic import BaseModel, ConfigDict, Field, model_validator


def _coerce_env_value(v: str):
    """env 值类型推断(true/false/int/float/原样 str)。与 config.py 同款。"""
    v_lower = v.strip().lower()
    if v_lower in ("true", "yes", "1", "on"):
        return True
    if v_lower in ("false", "no", "0", "off"):
        return False
    try:
        return int(v)
    except ValueError:
        pass
    try:
        return float(v)
    except ValueError:
        pass
    return v


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

    @classmethod
    def from_env(cls, prefix: str = "MEMORY_WAL_") -> "TaskWALConfig":
        """从环境变量构造。

        约定:
            MEMORY_WAL__MAX_RETRY=5
            MEMORY_WAL__RETRY_BACKOFF_SECONDS=60
            MEMORY_WAL__DONE_RETENTION_DAYS=1
            MEMORY_WAL__FAILED_RETENTION_DAYS=1
        (前缀默认 `MEMORY_WAL_`,允许 caller 传别的用于测试。)

        Phase 3 / Step 3.3.1:复用项目 `_coerce_env_value` 风格(类型推断),
        走 Pydantic 校验;非法值抛 ValidationError。
        """
        data: dict = {}
        for k, v in os.environ.items():
            if not k.startswith(prefix):
                continue
            # MEMORY_WAL__MAX_RETRY → "max_retry" (剥前缀 + 一个 _)
            key = k[len(prefix):].lstrip("_").lower()
            data[key] = _coerce_env_value(v)
        return cls.model_validate(data)


__all__ = ["TaskWALConfig"]
