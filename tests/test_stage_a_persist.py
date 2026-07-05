"""
LLMCallPersistHandler (Stage A) 测试套件(2026-07-01 引入)。

覆盖 Plan B §15 step 21 的 5 个 case — Stage A 接管 v1 streaming 路径
8 处 add_* 中的 #1/#2/#3/#4/#5/#6/#8:

| case | 验证 |
|---|---|
| A1 | stage_out.tool_calls 非空 → add_assistant_with_tools(text, tool_calls) 调一次 |
| A2 | stop_reason="llm_error" → add_assistant_message(fallback text) 调一次 |
| A3 | stop_reason="interrupted" → add_assistant_message(partial) 调一次 |
| A4 | stop_reason="end_turn" + 无 tool_calls + 有 full_text → 不调(add_assistant_message),留给 SessionPersistHandler (Stage C) |
| A5 | stage_out is None → 不调 session_manager(no-op guard) |

设计参考:docs/agent-state-machine-and-chain-of-responsibility-design.md §10
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from agent_core.turn_chain import (
    HandlerResult,
    LLMCallPersistHandler,
    TurnContext,
)


# ────────────────────────────────────────────────────────────────────
# Fixtures / helpers
# ────────────────────────────────────────────────────────────────────


def _make_fake_turn_ctx(stage_out=None) -> TurnContext:
    """构造最小 TurnContext — Stage A 只读 ctx.stage_outputs。"""
    ctx = MagicMock(spec=TurnContext)
    ctx.stage_outputs = stage_out
    ctx.events = []
    ctx.emit = lambda e: ctx.events.append(e)
    return ctx


def _make_fake_agent(has_session_manager: bool = True):
    """构造最小 fake agent(handler 只用 ._session_manager + 兜底 logger)。"""
    agent = MagicMock()
    agent._session_manager = MagicMock() if has_session_manager else None
    agent._logger = MagicMock()
    return agent


def _make_tool_call(tool_use_id: str, name: str, input_: dict):
    """tool_call 对象用 SimpleNamespace 模拟(真实代码里 .tool_use_id/.tool_name/.tool_input)。"""
    return SimpleNamespace(tool_use_id=tool_use_id, tool_name=name, tool_input=input_)


# ────────────────────────────────────────────────────────────────────
# A1: Stage A1 — 普通 tool_use 路径
# ────────────────────────────────────────────────────────────────────


def test_a1_stage_a1_writes_assistant_with_tools_on_tool_calls():
    """A1:stage_out.tool_calls 非空 → add_assistant_with_tools(text, tc_list) 调 1 次。

    对应 v1 #8 (agent_core.py L2493)+ #3/#4 (permission pause add_assistant_with_tools)。
    tool_use_id / name / input 字段必须映射成 dict 传给 add_assistant_with_tools。
    """
    agent = _make_fake_agent()
    handler = LLMCallPersistHandler(agent)
    tool_calls = [
        _make_tool_call("call_001", "Bash", {"command": "ls"}),
    ]
    stage_out = SimpleNamespace(
        tool_calls=tool_calls,
        full_text="好的,运行 ls:",
        thinking_text="",
        stop_reason="end_turn",
        usage=None,
    )
    ctx = _make_fake_turn_ctx(stage_out=stage_out)

    result = handler.handle(ctx)

    # 1. handle() 返 HandlerResult (无 stop_chain)
    assert isinstance(result, HandlerResult)
    assert not result.stop_chain
    # 2. add_assistant_with_tools 调 1 次,text + tc_list 正确传入
    agent._session_manager.add_assistant_with_tools.assert_called_once()
    call_kwargs = agent._session_manager.add_assistant_with_tools.call_args.kwargs
    assert call_kwargs["text"] == "好的,运行 ls:"
    assert call_kwargs["tool_calls"] == [
        {"id": "call_001", "name": "Bash", "input": {"command": "ls"}},
    ]
    # 3. add_assistant_message 不调(Stage C 接 normal final text)
    agent._session_manager.add_assistant_message.assert_not_called()


# ────────────────────────────────────────────────────────────────────
# A2: Stage A2 — LLM error 路径
# ────────────────────────────────────────────────────────────────────


def test_a2_stage_a2_writes_fallback_assistant_message_on_llm_error():
    """A2:stop_reason="llm_error" → add_assistant_message(text+usage+thinking) 调 1 次。

    对应 v1 #1 (agent_core.py L1042)+ #5 (agent_core.py L2145)。
    text 兜底走 full_text 或 "[llm_error]"(若 full_text 空)。
    """
    agent = _make_fake_agent()
    handler = LLMCallPersistHandler(agent)
    # fake UsageStats dataclass — _usage_asdict 用 __dataclass_fields__ 判定
    from dataclasses import dataclass

    @dataclass
    class FakeUsage:
        input_tokens: int = 100
        output_tokens: int = 0
    usage = FakeUsage(input_tokens=50, output_tokens=0)

    stage_out = SimpleNamespace(
        tool_calls=[],
        full_text="",  # 空 → 走 fallback
        thinking_text="partial thinking",
        stop_reason="llm_error",
        usage=usage,
    )
    ctx = _make_fake_turn_ctx(stage_out=stage_out)

    handler.handle(ctx)

    # 1. add_assistant_message 调 1 次
    agent._session_manager.add_assistant_message.assert_called_once()
    call_args = agent._session_manager.add_assistant_message.call_args
    # positional text
    assert call_args.args[0] == "[llm_error]"
    # kwargs 含 thinking + usage(dict 化)
    assert call_args.kwargs["thinking"] == "partial thinking"
    assert call_args.kwargs["usage"] == {"input_tokens": 50, "output_tokens": 0}
    # 2. add_assistant_with_tools 不调
    agent._session_manager.add_assistant_with_tools.assert_not_called()


# ────────────────────────────────────────────────────────────────────
# A3: Stage A3 — stream interrupt 路径
# ────────────────────────────────────────────────────────────────────


def test_a3_stage_a3_writes_partial_assistant_message_on_interrupted():
    """A3:stop_reason="interrupted" → add_assistant_message(partial) 调 1 次。

    对应 v1 #2 (agent_core.py L1103)+ #6 (agent_core.py L2230)。
    _iter_phase_llm cancel_event 触发中断时,partial = full_text (可能非空)。
    """
    agent = _make_fake_agent()
    handler = LLMCallPersistHandler(agent)
    stage_out = SimpleNamespace(
        tool_calls=[],
        full_text="好的,我开始理解了你需要...",
        thinking_text="",
        stop_reason="interrupted",
        usage=None,
    )
    ctx = _make_fake_turn_ctx(stage_out=stage_out)

    handler.handle(ctx)

    # 1. add_assistant_message 调 1 次,text = partial(full_text)
    agent._session_manager.add_assistant_message.assert_called_once()
    assert agent._session_manager.add_assistant_message.call_args.args[
        0
    ] == "好的,我开始理解了你需要..."
    # 2. usage / thinking 都为空 → 不应传入 kwargs
    kwargs = agent._session_manager.add_assistant_message.call_args.kwargs
    assert "thinking" not in kwargs
    assert "usage" not in kwargs


# ────────────────────────────────────────────────────────────────────
# A4: 正常 final text — Stage C 接管
# ────────────────────────────────────────────────────────────────────


def test_a4_stage_a_no_op_on_normal_final_text():
    """A4:stop_reason="end_turn" + 无 tool_calls + 有 full_text → 不调 session_manager。

    这是关键的"Stage A 不重复写"语义 — 否则会跟 SessionPersistHandler (Stage C) /
    FinalAnswerPersistHandler (Step 3 实施) double-write 同一行。
    """
    agent = _make_fake_agent()
    handler = LLMCallPersistHandler(agent)
    stage_out = SimpleNamespace(
        tool_calls=[],
        full_text="回答了用户的问题。",
        thinking_text="我在思考",
        stop_reason="end_turn",
        usage=None,
    )
    ctx = _make_fake_turn_ctx(stage_out=stage_out)

    handler.handle(ctx)

    agent._session_manager.add_assistant_message.assert_not_called()
    agent._session_manager.add_assistant_with_tools.assert_not_called()


# ────────────────────────────────────────────────────────────────────
# A5: stage_outputs = None — no-op guard
# ────────────────────────────────────────────────────────────────────


def test_a5_stage_a_no_op_when_stage_outputs_none():
    """A5:stage_out is None → 不调 session_manager。

    触发场景:EXECUTING_TOOLS / FINALIZING phase 触发 LLMCallPersistHandler.handle()
    (例如 _sm 重复 invoke chain)— 此时 stage_out 还没填,handler 必须跳过。
    """
    agent = _make_fake_agent()
    handler = LLMCallPersistHandler(agent)

    # stage_out = None — handle() 第一行就 return
    ctx = _make_fake_turn_ctx(stage_out=None)
    handler.handle(ctx)

    agent._session_manager.add_assistant_message.assert_not_called()
    agent._session_manager.add_assistant_with_tools.assert_not_called()


# ────────────────────────────────────────────────────────────────────
# Extra: agent._session_manager is None → no-op
# ────────────────────────────────────────────────────────────────────


def test_a6_stage_a_no_session_manager():
    """扩展:agent._session_manager is None → handle() 直接 return,不抛。

    触发场景:用户禁用 session(暂存模式)— handler 必须容错。
    """
    agent = _make_fake_agent(has_session_manager=False)
    handler = LLMCallPersistHandler(agent)
    tool_calls = [_make_tool_call("call_007", "Bash", {"command": "ls"})]
    stage_out = SimpleNamespace(
        tool_calls=tool_calls,
        full_text="ok",
        thinking_text="",
        stop_reason="end_turn",
        usage=None,
    )
    ctx = _make_fake_turn_ctx(stage_out=stage_out)

    result = handler.handle(ctx)  # 不抛
    assert isinstance(result, HandlerResult)
