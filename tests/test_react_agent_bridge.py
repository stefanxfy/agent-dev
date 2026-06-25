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
from agent_core.llm.router import (
    LLMRouter, LLMConfig, StreamChunk, TextDelta, ToolCallDelta, UsageStats,
)
from agent_core.tools.base import ToolRegistry


def _make_router():
    return LLMRouter(LLMConfig(provider='zhipu', model='glm-4', api_key='mock'))


def _usage_chunk():
    return StreamChunk(usage=UsageStats(input_tokens=10, output_tokens=5))


def _text_chunks(text, stop_reason="end_turn"):
    """LLM 流:一段文本 + usage(+ 终止原因),无 tool_call → 走最终回答分支"""
    yield StreamChunk(text_delta=TextDelta(text=text))
    yield _usage_chunk()
    if stop_reason:
        yield StreamChunk(stop_reason=stop_reason)


def _tool_chunks(i):
    """LLM 流:一个 tool_call + usage → 继续工具循环,永不收尾"""
    yield StreamChunk(tool_call=ToolCallDelta(
        tool_name="noop", tool_input={}, tool_use_id=f"t{i}",
    ))
    yield _usage_chunk()


def test_extraction_uses_run_user_message_and_final_answer():
    """Bug 1e:正常收尾 → on_turn_end 收到本 run 的 user_message + final_answer。"""
    bridge = MagicMock()
    bridge.on_turn_end.return_value = []  # 可迭代
    agent = ReactAgent(
        llm_router=_make_router(), tool_registry=ToolRegistry(),
        react_memory_bridge=bridge, max_turns=3,
    )
    agent.llm.chat = lambda messages, **kw: _text_chunks("好的,已记住周杰伦")

    list(agent.run("我喜欢周杰伦,请记住"))

    bridge.on_turn_end.assert_called_once()
    kw = bridge.on_turn_end.call_args.kwargs
    assert kw["user_msg"] == "我喜欢周杰伦,请记住"
    assert kw["assistant_resp"] == "好的,已记住周杰伦"


def test_no_mispair_when_tool_run_has_no_final_answer():
    """Bug 1e:工具循环被 max_turns 截断、无最终文本回答 → 不调 on_turn_end。

    修复前:末尾用 reversed 全局扫描 last_assistant,会抓到「上一轮」的回答误配。
    本测试构造 run1(文本收尾)→ run2(工具循环到顶,无收尾),断言 on_turn_end
    只为 run1 触发一次;修复前 run2 会误配 run1 的回答 → call_count==2。
    """
    bridge = MagicMock()
    bridge.on_turn_end.return_value = []
    agent = ReactAgent(
        llm_router=_make_router(), tool_registry=ToolRegistry(),
        react_memory_bridge=bridge, max_turns=2,
    )
    agent.tools.execute = MagicMock(return_value={"status": "success", "output": "ok"})

    # run1:文本收尾 → 提取一次
    agent.llm.chat = lambda messages, **kw: _text_chunks("答案A")
    list(agent.run("问题1"))
    assert bridge.on_turn_end.call_count == 1
    assert bridge.on_turn_end.call_args.kwargs["assistant_resp"] == "答案A"

    # run2:工具循环到 max_turns,无最终文本回答
    n = {"i": 0}
    def tool_only(messages, **kw):
        n["i"] += 1
        return _tool_chunks(n["i"])
    agent.llm.chat = tool_only
    list(agent.run("问题2"))

    assert bridge.on_turn_end.call_count == 1, (
        "工具轮无完整回答不该提取(更不该 reversed 扫到上一轮的'答案A'误配)"
    )


def test_no_extraction_when_answer_truncated_by_max_tokens():
    """Bug 1e:回答因 max_tokens 截断(无 tool_call 但 stop_reason=max_tokens)→ 不提取。"""
    bridge = MagicMock()
    bridge.on_turn_end.return_value = []
    agent = ReactAgent(
        llm_router=_make_router(), tool_registry=ToolRegistry(),
        react_memory_bridge=bridge, max_turns=3,
    )
    # 文本被切断,终止原因是 max_tokens
    agent.llm.chat = lambda messages, **kw: _text_chunks("我喜欢周杰", stop_reason="max_tokens")

    list(agent.run("我喜欢周杰伦,请记住"))

    bridge.on_turn_end.assert_not_called()


def test_no_extraction_when_answer_truncated_openai_length():
    """Bug 1e:OpenAI 兼容的 length 终止原因同样视为截断 → 不提取。"""
    bridge = MagicMock()
    bridge.on_turn_end.return_value = []
    agent = ReactAgent(
        llm_router=_make_router(), tool_registry=ToolRegistry(),
        react_memory_bridge=bridge, max_turns=3,
    )
    agent.llm.chat = lambda messages, **kw: _text_chunks("半句", stop_reason="length")

    list(agent.run("问题"))

    bridge.on_turn_end.assert_not_called()


def test_extraction_when_stop_reason_absent_is_backward_compatible():
    """Bug 1e:provider 没给 stop_reason(None)→ 不拦,照常提取(向后兼容)。"""
    bridge = MagicMock()
    bridge.on_turn_end.return_value = []
    agent = ReactAgent(
        llm_router=_make_router(), tool_registry=ToolRegistry(),
        react_memory_bridge=bridge, max_turns=3,
    )
    agent.llm.chat = lambda messages, **kw: _text_chunks("完整回答", stop_reason=None)

    list(agent.run("问题"))

    bridge.on_turn_end.assert_called_once()


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