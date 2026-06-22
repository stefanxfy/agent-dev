"""
Task 7: ReactAgent 接入 ReactMemoryBridge 测试

验证:
1. ReactAgent.__init__ 接受 react_memory_bridge kwarg
2. 实例属性 self.react_memory_bridge 被正确赋值
3. 默认值是 None(向后兼容,无 bridge 时 run() 不调 bridge)
4. 删除 Option C 参数 (memory_extractor / memory_embed_fn) 不再被接受
5. 删除 _extract_and_write 方法
"""
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from agent_core.agent_core import ReactAgent
from agent_core.llm.router import LLMRouter, LLMConfig
from agent_core.tools.base import ToolRegistry


def _make_router():
    return LLMRouter(LLMConfig(provider='zhipu', model='glm-4', api_key='mock'))


def test_react_agent_accepts_react_memory_bridge():
    """Step 7.1: ReactAgent 必须接受 react_memory_bridge kwarg,并存为 self 属性"""
    bridge_mock = MagicMock(name="ReactMemoryBridge")
    agent = ReactAgent(
        llm_router=_make_router(),
        tool_registry=ToolRegistry(),
        react_memory_bridge=bridge_mock,
    )
    assert agent.react_memory_bridge is bridge_mock


def test_react_agent_react_memory_bridge_default_none():
    """默认 react_memory_bridge=None(向后兼容)"""
    agent = ReactAgent(
        llm_router=_make_router(),
        tool_registry=ToolRegistry(),
    )
    assert agent.react_memory_bridge is None


def test_react_agent_no_longer_accepts_memory_extractor():
    """Step 7.4: Option C 参数 memory_extractor 必须删除"""
    with pytest.raises(TypeError):
        ReactAgent(
            llm_router=_make_router(),
            tool_registry=ToolRegistry(),
            memory_extractor=MagicMock(),
        )


def test_react_agent_no_longer_accepts_memory_embed_fn():
    """Step 7.4: Option C 参数 memory_embed_fn 必须删除"""
    with pytest.raises(TypeError):
        ReactAgent(
            llm_router=_make_router(),
            tool_registry=ToolRegistry(),
            memory_embed_fn=lambda x: [0.0],
        )


def test_react_agent_extract_and_write_method_removed():
    """Step 7.4: _extract_and_write 方法必须删除"""
    agent = ReactAgent(
        llm_router=_make_router(),
        tool_registry=ToolRegistry(),
    )
    assert not hasattr(agent, "_extract_and_write"), (
        "Option C _extract_and_write method must be removed in Task 7"
    )


if __name__ == '__main__':
    import sys
    sys.exit(pytest.main([__file__, '-v', '--tb=short']))