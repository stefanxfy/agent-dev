"""M10 C6.4: 运行时切换 config 不重建 agent — MemoryConfig.set_runtime 的最小契约。

3 个 case:
1. 嵌套字段(cost.daily_budget_usd)in-place 改
2. 顶层字段(enabled)in-place 改
3. 未知路径抛 KeyError(明确信号,不要吞成 AttributeError)
"""
from __future__ import annotations

import pytest

from agent_core.memory.config import MemoryConfig


def test_set_runtime_updates_nested_field():
    """cost.daily_budget_usd 路径 in-place 修改(M10 C6.4 Step A)。"""
    config = MemoryConfig()
    original = config.cost.daily_budget_usd
    # 默认值不应等于 0.5,否则测试无意义
    assert original != 0.5
    config.set_runtime("cost.daily_budget_usd", 0.5)
    assert config.cost.daily_budget_usd == 0.5


def test_set_runtime_updates_top_level_field():
    """顶层字段 enabled 直接改(M10 C6.4 Step A)。"""
    config = MemoryConfig()
    assert config.enabled is True
    config.set_runtime("enabled", False)
    assert config.enabled is False


def test_set_runtime_raises_keyerror_on_unknown_path():
    """unknown path 抛 KeyError — 不要吞成 AttributeError。"""
    config = MemoryConfig()
    with pytest.raises(KeyError) as excinfo:
        config.set_runtime("nonexistent.field", 5.0)
    # 错误信息里应该提到未知字段名,便于排查
    assert "nonexistent" in str(excinfo.value)


def test_set_runtime_raises_validation_error_on_type_mismatch():
    """type mismatch → ValidationError (MemoryConfig 启用了 validate_assignment=True)"""
    from pydantic import ValidationError
    from agent_core.memory.config import MemoryConfig
    config = MemoryConfig()
    # enabled 是 bool — 传 object() 应被 Pydantic 拦下
    with pytest.raises(ValidationError):
        config.set_runtime("enabled", object())


def test_react_agent_accepts_memory_config_param():
    """ReactAgent.__init__ 接受 memory_config 参数并存为 self.memory_config"""
    from agent_core.agent_core import ReactAgent
    from agent_core.memory.config import MemoryConfig
    from unittest.mock import MagicMock
    config = MemoryConfig()
    agent = ReactAgent(
        llm_router=MagicMock(),
        tool_registry=MagicMock(),
        memory_config=config,
    )
    assert agent.memory_config is config
