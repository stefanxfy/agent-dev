"""
FinalAnswerPersistHandler (Stage C) 测试套件(2026-07-01 引入)。

覆盖 Plan B §15 step 23 的 5 个 case — Stage C 接管 final answer 4 字段落库:

| case | 验证 |
|---|---|
| C1 | 正常 final text + thinking + tool_logs + usage → add_assistant_message 调 1 次,4 字段全部传入 |
| C2 | thinking=None → 不传 thinking kwarg |
| C3 | usage=None → 不传 usage kwarg |
| C4 | tool_logs=[] (空) → 不传 tool_logs kwarg |
| C5 | stop_reason="max_tokens" → 不调 add_assistant_message |
| C6 (extra) | stop_reason="length" → 不调 |
| C7 (extra) | 有 tool_calls → 不调(Stage A 已写) |

设计参考:docs/agent-state-machine-and-chain-of-responsibility-design.md §10
"""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
from unittest.mock import MagicMock

from agent_core.turn_chain import (
    FinalAnswerPersistHandler,
    HandlerResult,
    TurnContext,
)


# ────────────────────────────────────────────────────────────────────
# Fixtures / helpers
# ────────────────────────────────────────────────────────────────────


def _make_fake_turn_ctx(stage_out=None) -> TurnContext:
    """构造最小 TurnContext(Stage C 只读 ctx.stage_outputs)。"""
    ctx = MagicMock(spec=TurnContext)
    ctx.stage_outputs = stage_out
    ctx.events = []
    ctx.emit = lambda e: ctx.events.append(e)
    return ctx


def _make_fake_agent(has_session_manager=True, pending_tool_logs=None):
    """构造最小 fake agent(Stage C 读 RunState.pending_tool_logs)。"""
    agent = MagicMock()
    agent._session_manager = MagicMock() if has_session_manager else None
    run_state = MagicMock()
    run_state.pending_tool_logs = list(pending_tool_logs) if pending_tool_logs is not None else []
    agent._run_state = run_state
    return agent


@dataclass
class FakeUsage:
    """Fake UsageStats dataclass(handler 用 _usage_asdict 转 dict)。"""
    input_tokens: int = 100
    output_tokens: int = 50


def _make_stage_out(
    full_text="回答了用户的问题。",
    thinking_text="我在思考",
    stop_reason="end_turn",
    tool_calls=None,
    usage=None,
):
    """构造最小 _LLMResult(stage_outputs)的 SimpleNamespace 形式。"""
    return SimpleNamespace(
        tool_calls=tool_calls or [],
        full_text=full_text,
        thinking_text=thinking_text,
        stop_reason=stop_reason,
        usage=usage,
    )


# ────────────────────────────────────────────────────────────────────
# C1: 正常 final text + 4 字段完整
# ────────────────────────────────────────────────────────────────────


def test_c1_stage_c_writes_final_text_with_all_four_fields():
    """C1:正常 stop_reason="end_turn" + full_text 非空 + 无 tool_calls →
    add_assistant_message(text, thinking, tool_logs, usage) 调 1 次,4 字段完整传入。

    对应 v1 #7 (run() final answer 4 字段 add_assistant_message at agent_core.py L2234)。
    RunState.pending_tool_logs 写后清空(避免跨 turn double-write)。
    """
    agent = _make_fake_agent(pending_tool_logs=[{"type": "result", "name": "Bash", "output": "ok", "success": True}])
    handler = FinalAnswerPersistHandler(agent)
    usage = FakeUsage(input_tokens=200, output_tokens=80)
    stage_out = _make_stage_out(
        full_text="回答了。",
        thinking_text="深度思考过程",
        stop_reason="end_turn",
        usage=usage,
    )
    ctx = _make_fake_turn_ctx(stage_out=stage_out)

    handler.handle(ctx)

    # 1. add_assistant_message 调 1 次,4 字段完整传入
    agent._session_manager.add_assistant_message.assert_called_once()
    args, kwargs = agent._session_manager.add_assistant_message.call_args
    assert args[0] == "回答了。"
    assert kwargs["thinking"] == "深度思考过程"
    assert kwargs["tool_logs"] == [
        {"type": "result", "name": "Bash", "output": "ok", "success": True},
    ]
    # usage dataclass 必须被 _usage_asdict 转 dict
    assert kwargs["usage"] == {"input_tokens": 200, "output_tokens": 80}
    # 2. RunState.pending_tool_logs 写后清空
    assert agent._run_state.pending_tool_logs == []


# ────────────────────────────────────────────────────────────────────
# C2: thinking=None → 不传 thinking kwarg
# ────────────────────────────────────────────────────────────────────


def test_c2_stage_c_omits_thinking_kwarg_when_none():
    """C2:thinking_text 为空 / None → 不传 thinking kwarg(add_assistant_message 调用)。

    测试目的是 verify handler 的 "可选字段不传 None" 语义,避免 SessionManager 收到
    thinking=None 反而写一条空 thinking 字段到 jsonl。
    """
    agent = _make_fake_agent(pending_tool_logs=[])
    handler = FinalAnswerPersistHandler(agent)
    stage_out = _make_stage_out(
        full_text="ok",
        thinking_text="",  # 空字符串
        stop_reason="end_turn",
    )
    ctx = _make_fake_turn_ctx(stage_out=stage_out)

    handler.handle(ctx)

    agent._session_manager.add_assistant_message.assert_called_once()
    kwargs = agent._session_manager.add_assistant_message.call_args.kwargs
    assert "thinking" not in kwargs  # 不传


# ────────────────────────────────────────────────────────────────────
# C3: usage=None → 不传 usage kwarg
# ────────────────────────────────────────────────────────────────────


def test_c3_stage_c_omits_usage_kwarg_when_none():
    """C3:stage_out.usage 为 None → 不传 usage kwarg。

    usage=None 时 handler 不强行写入,避免 session.storage.flush 报
    "Object of type NoneType is not JSON serializable"(防御性)。
    """
    agent = _make_fake_agent(pending_tool_logs=[])
    handler = FinalAnswerPersistHandler(agent)
    stage_out = _make_stage_out(
        full_text="回答了。",
        thinking_text="t",
        stop_reason="end_turn",
        usage=None,  # 无 usage
    )
    ctx = _make_fake_turn_ctx(stage_out=stage_out)

    handler.handle(ctx)

    agent._session_manager.add_assistant_message.assert_called_once()
    kwargs = agent._session_manager.add_assistant_message.call_args.kwargs
    assert "usage" not in kwargs


# ────────────────────────────────────────────────────────────────────
# C4: tool_logs=[] → 不传 tool_logs kwarg
# ────────────────────────────────────────────────────────────────────


def test_c4_stage_c_omits_tool_logs_kwarg_when_empty():
    """C4:RunState.pending_tool_logs = [] (空) → 不传 tool_logs kwarg。

    普通文本回复(无 tool 调用)轮 — user 没让 LLM 调 tool,自然没有 tool_logs。
    """
    agent = _make_fake_agent(pending_tool_logs=[])
    handler = FinalAnswerPersistHandler(agent)
    stage_out = _make_stage_out(
        full_text="hi",
        thinking_text="",
        stop_reason="end_turn",
    )
    ctx = _make_fake_turn_ctx(stage_out=stage_out)

    handler.handle(ctx)

    agent._session_manager.add_assistant_message.assert_called_once()
    kwargs = agent._session_manager.add_assistant_message.call_args.kwargs
    assert "tool_logs" not in kwargs


# ────────────────────────────────────────────────────────────────────
# C5: stop_reason="max_tokens" → 不写 final
# ────────────────────────────────────────────────────────────────────


def test_c5_stage_c_skips_final_when_max_tokens():
    """C5:stop_reason="max_tokens" → 不调 add_assistant_message(视为截断)。

    对齐 test_react_agent_bridge L100:max_tokens 路径 bridge.on_turn_end 不调,
    本 handler 同样不写 final(避免 partial 文本当作 final answer 落库,触发
    bridge 误抽记忆)。
    """
    agent = _make_fake_agent(pending_tool_logs=[])
    handler = FinalAnswerPersistHandler(agent)
    stage_out = _make_stage_out(
        full_text="被截断的文本...",  # 仍有 full_text 但视为截断
        thinking_text="",
        stop_reason="max_tokens",
    )
    ctx = _make_fake_turn_ctx(stage_out=stage_out)

    handler.handle(ctx)

    agent._session_manager.add_assistant_message.assert_not_called()


# ────────────────────────────────────────────────────────────────────
# C6 (extra): stop_reason="length" → 不写
# ────────────────────────────────────────────────────────────────────


def test_c6_stage_c_skips_final_when_length():
    """C6:stop_reason="length"(OpenAI 截断) → 不调 add_assistant_message。

    对齐 test_react_agent_bridge L116:length 路径 bridge.on_turn_end 不调。
    """
    agent = _make_fake_agent(pending_tool_logs=[])
    handler = FinalAnswerPersistHandler(agent)
    stage_out = _make_stage_out(
        full_text="被截断的文本...",
        thinking_text="",
        stop_reason="length",
    )
    ctx = _make_fake_turn_ctx(stage_out=stage_out)

    handler.handle(ctx)

    agent._session_manager.add_assistant_message.assert_not_called()


# ────────────────────────────────────────────────────────────────────
# C7 (extra): 有 tool_calls → 不写(Stage A 已写)
# ────────────────────────────────────────────────────────────────────


def test_c7_stage_c_skips_final_when_tool_calls_present():
    """C7:stage_out.tool_calls 非空 → 不调 add_assistant_message。

    关键 idempotency — Stage A (LLMCallPersistHandler Stage A1) 已经在 llm_chain 末位
    写过 assistant+tool_use blocks,Stage C 不可重复写 assistant_message
    (否则同一 LLM 响应落两条 entry)。
    """
    agent = _make_fake_agent(pending_tool_logs=[])
    handler = FinalAnswerPersistHandler(agent)
    # fake tool_calls(属性访问接口)
    tool_call = SimpleNamespace(tool_use_id="call_001", tool_name="Bash", tool_input={"command": "ls"})
    stage_out = _make_stage_out(
        full_text="好的,运行:",
        thinking_text="",
        stop_reason="end_turn",
        tool_calls=[tool_call],
    )
    ctx = _make_fake_turn_ctx(stage_out=stage_out)

    handler.handle(ctx)

    agent._session_manager.add_assistant_message.assert_not_called()
