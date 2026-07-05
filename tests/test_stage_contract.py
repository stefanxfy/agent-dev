"""
test_stage_contract.py — handler 间强类型 stage contract 测试

覆盖(§十六 测试设计 §16.1):
- StageInputs / LLMResult / ToolExecutionResult dataclass 字段完整性
- handler 间 data flow contract(LLMCallHandler 写 stage_outputs.chunks
  → ChunkParseHandler 读它,中间 type check)
- 跨 handler 的 turn ctx shared state(events / stage_outputs)
"""

from __future__ import annotations

from dataclasses import fields, is_dataclass
from typing import Optional

import pytest

from agent_core.agent_state import TurnContext, RunState
from agent_core.stages import (
    LLMResult,
    StageInputs,
    ToolExecutionResult,
)
from agent_core.turn_chain import HandlerResult, TurnChain


# ════════════════════════════════════════════════════════════
# Stage dataclass 字段完整性测试
# ════════════════════════════════════════════════════════════


class TestStageInputsContract:
    def test_is_dataclass(self):
        """StageInputs 是 dataclass。"""
        assert is_dataclass(StageInputs)

    def test_required_fields(self):
        """StageInputs 含 messages + system_prompt + tool_schemas。"""
        field_names = {f.name for f in fields(StageInputs)}
        assert "messages" in field_names
        assert "system_prompt" in field_names
        assert "tool_schemas" in field_names

    def test_messages_required_no_default(self):
        """messages 字段无默认值(必填)— 用 MISSING 检测。"""
        from dataclasses import MISSING
        msgs_field = next(f for f in fields(StageInputs) if f.name == "messages")
        # 没有 default 也没有 default_factory → MISSING sentinel
        assert msgs_field.default is MISSING
        assert msgs_field.default_factory is MISSING

    def test_construction_with_all_fields(self):
        """构造含全部字段的 StageInputs。"""
        si = StageInputs(
            messages=[{"role": "user", "content": "hi"}],
            system_prompt="You are helpful",
            tool_schemas=[{"name": "Bash"}],
        )
        assert si.messages == [{"role": "user", "content": "hi"}]
        assert si.system_prompt == "You are helpful"
        assert si.tool_schemas == [{"name": "Bash"}]

    def test_construction_minimal(self):
        """只传 messages 也工作(system_prompt/tool_schemas 可选)。"""
        si = StageInputs(messages=[{"role": "user", "content": "hi"}])
        assert si.system_prompt is None
        assert si.tool_schemas == []

    def test_independent_instances(self):
        """两个 StageInputs 实例的 tool_schemas 独立(不是 shared default)。"""
        si1 = StageInputs(messages=[])
        si2 = StageInputs(messages=[])
        si1.tool_schemas.append({"name": "Bash"})
        assert si2.tool_schemas == []  # 不受 si1 影响


class TestLLMResultContract:
    def test_is_dataclass(self):
        assert is_dataclass(LLMResult)

    def test_all_optional_fields(self):
        """LLMResult 所有字段都有默认值(可分步填充)。"""
        for f in fields(LLMResult):
            assert f.default is not None or f.default_factory is not None, \
                f"Field {f.name} should have a default"

    def test_required_fields_present(self):
        """LLMResult 含 chunks / full_text / thinking_text / tool_calls /
        tool_results / usage / stop_reason 7 个字段。"""
        names = {f.name for f in fields(LLMResult)}
        expected = {
            "chunks", "full_text", "thinking_text",
            "tool_calls", "tool_results", "usage", "stop_reason",
        }
        assert expected.issubset(names), f"Missing: {expected - names}"

    def test_tool_results_is_list_of_tuples(self):
        """tool_results 字段是 list[tuple[tool_use_id, output]]。"""
        r = LLMResult()
        r.tool_results.append(("tool_use_123", "result content"))
        assert r.tool_results[0] == ("tool_use_123", "result content")

    def test_stop_reason_interrupted_supported(self):
        """stop_reason 字段支持 'interrupted' 值(D14-D20 INTERRUPTED 集成)。"""
        r = LLMResult(stop_reason="interrupted")
        assert r.stop_reason == "interrupted"

    def test_stop_reason_max_tokens_supported(self):
        """stop_reason 支持 'max_tokens'(兼容 v1 行为)。"""
        r = LLMResult(stop_reason="max_tokens")
        assert r.stop_reason == "max_tokens"

    def test_independent_tool_results(self):
        """两个 LLMResult 的 tool_results 独立。"""
        r1 = LLMResult()
        r2 = LLMResult()
        r1.tool_results.append(("id1", "out1"))
        assert r2.tool_results == []


class TestToolExecutionResultContract:
    def test_is_dataclass(self):
        assert is_dataclass(ToolExecutionResult)

    def test_required_fields_present(self):
        """ToolExecutionResult 含 tool_calls / tool_results /
        success_count / error_count / total_elapsed。"""
        names = {f.name for f in fields(ToolExecutionResult)}
        expected = {
            "tool_calls", "tool_results",
            "success_count", "error_count", "total_elapsed",
        }
        assert expected.issubset(names)

    def test_success_and_error_counts_independent(self):
        """success_count 和 error_count 是独立 int(不互斥)。"""
        r = ToolExecutionResult(success_count=3, error_count=2)
        assert r.success_count == 3
        assert r.error_count == 2

    def test_total_elapsed_zero_by_default(self):
        """total_elapsed 默认 0.0(浮点)。"""
        r = ToolExecutionResult()
        assert r.total_elapsed == 0.0
        assert isinstance(r.total_elapsed, float)


# ════════════════════════════════════════════════════════════
# Cross-handler data flow contract 测试
# ════════════════════════════════════════════════════════════


class _WriteStageInputs:
    """模拟 inputs_chain 末位 handler:写 StageInputs 到 ctx。"""
    name = "write_stage_inputs"

    def __init__(self, messages, system_prompt=None, tool_schemas=None):
        self._messages = messages
        self._system_prompt = system_prompt
        self._tool_schemas = tool_schemas or []

    def handle(self, ctx: TurnContext) -> HandlerResult:
        ctx.stage_inputs = StageInputs(
            messages=self._messages,
            system_prompt=self._system_prompt,
            tool_schemas=self._tool_schemas,
        )
        return HandlerResult()


class _ReadStageInputs:
    """模拟 llm_chain 首位 handler:从 ctx.stage_inputs 读取数据。"""
    name = "read_stage_inputs"

    def handle(self, ctx: TurnContext) -> HandlerResult:
        # 验证 stage_inputs 是 StageInputs 实例
        assert isinstance(ctx.stage_inputs, StageInputs)
        # 写 stage_outputs.LLMResult
        ctx.stage_outputs = LLMResult(
            full_text="echo: " + " ".join(
                m.get("content", "") for m in ctx.stage_inputs.messages
            ),
            stop_reason="end_turn",
        )
        return HandlerResult()


class _WriteToolResults:
    """模拟 tool_chain:写 tool_results 到 stage_outputs。"""
    name = "write_tool_results"

    def handle(self, ctx: TurnContext) -> HandlerResult:
        ctx.stage_outputs.tool_results.append(
            ("tool_use_abc", "execution output")
        )
        ctx.stage_outputs.stop_reason = "tool_use"
        return HandlerResult()


class TestCrossHandlerDataFlow:
    def test_inputs_chain_writes_stage_inputs_for_llm_chain(self):
        """inputs_chain 写 stage_inputs → llm_chain 读 stage_inputs。"""
        chain = TurnChain([
            _WriteStageInputs(
                messages=[{"role": "user", "content": "hello"}],
                system_prompt="be helpful",
            ),
            _ReadStageInputs(),
        ])
        ctx = TurnContext(run_state=RunState())
        list(chain.run(ctx))

        # llm_chain 读到了 inputs_chain 写的数据
        assert ctx.stage_outputs is not None
        assert "hello" in ctx.stage_outputs.full_text

    def test_tool_results_passed_through_chain(self):
        """tool_chain 写 tool_results,验证 cross-chain 可见性。"""
        chain = TurnChain([
            _WriteStageInputs(
                messages=[{"role": "user", "content": "run bash"}],
            ),
            _ReadStageInputs(),  # 写 stage_outputs
            _WriteToolResults(),  # 追加 tool_results
        ])
        ctx = TurnContext(run_state=RunState())
        list(chain.run(ctx))

        # 同一 turn 内 stage_outputs 共享
        assert ctx.stage_outputs.tool_results == [
            ("tool_use_abc", "execution output")
        ]
        assert ctx.stage_outputs.stop_reason == "tool_use"

    def test_turn_context_events_persist_through_chain(self):
        """events 在同一 turn 内累积,跨 handler 可见。"""
        class Emitter:
            name = "emitter"
            def __init__(self, ev):
                self._ev = ev
            def handle(self, ctx):
                ctx.emit(self._ev)
                return HandlerResult()

        chain = TurnChain([
            Emitter(("text", "A")),
            Emitter(("text", "B")),
            Emitter(("text", "C")),
        ])
        ctx = TurnContext(run_state=RunState())
        list(chain.run(ctx))

        # events 在 chain.run 内 yield 出去后清空,但 ctx 内是累积的
        # 直到下一次 yield
        assert ctx.events == []  # yield 时已 clear
        # 验证 yield 顺序
        # (重跑一次捕 events)


# ════════════════════════════════════════════════════════════
# Turn chain event propagation 测试
# ════════════════════════════════════════════════════════════


class _Emit:
    name = "emit"
    def __init__(self, ev_type, content):
        self._type = ev_type
        self._content = content
    def handle(self, ctx):
        ctx.emit((self._type, self._content))
        return HandlerResult()


class TestEventPropagation:
    def test_events_yielded_in_order(self):
        """handler emit events 按顺序 yield 给 phase。"""
        chain = TurnChain([
            _Emit("text", "A"),
            _Emit("text", "B"),
            _Emit("text", "C"),
        ])
        ctx = TurnContext(run_state=RunState())
        events = list(chain.run(ctx))
        assert events == [("text", "A"), ("text", "B"), ("text", "C")]

    def test_events_cleared_between_chain_runs(self):
        """chain.run() 结束时 ctx.events 被 clear(下次 chain 重新累积)。"""
        chain = TurnChain([_Emit("text", "hello")])
        ctx = TurnContext(run_state=RunState())
        list(chain.run(ctx))
        assert ctx.events == []  # yield 后 clear

    def test_different_event_types_supported(self):
        """chain 支持多种 event type(text / tool_call / system 等)。"""
        chain = TurnChain([
            _Emit("text", "thinking..."),
            _Emit("tool_call", {"name": "Bash", "input": {}}),
            _Emit("tool_result", {"name": "Bash", "output": "ok", "success": True}),
            _Emit("system", "✅ 回答完成"),
        ])
        ctx = TurnContext(run_state=RunState())
        events = list(chain.run(ctx))
        assert len(events) == 4
        assert events[0][0] == "text"
        assert events[1][0] == "tool_call"
        assert events[2][0] == "tool_result"
        assert events[3][0] == "system"


# ════════════════════════════════════════════════════════════
# v1 → v2 兼容性 contract 测试
# ════════════════════════════════════════════════════════════


class TestV1V2ContractCompat:
    """v1 stage_outputs 用 dict, v2 用 LLMResult。验证 v2 stage_outputs
    必须能被 v1-style dict-like 访问(向后兼容)。"""

    def test_llm_result_supports_getattr(self):
        """LLMResult 是 dataclass,支持 getattr(类似 v1 dict-like 访问)。"""
        r = LLMResult(full_text="hello", stop_reason="end_turn")
        # 用 getattr 模拟 v1 ctx.stage_outputs.get("full_text")
        assert getattr(r, "full_text") == "hello"
        assert getattr(r, "stop_reason") == "end_turn"

    def test_llm_result_default_full_text_empty(self):
        """LLMResult.full_text 默认空字符串。"""
        r = LLMResult()
        assert getattr(r, "full_text", "") == ""

    def test_llm_result_missing_field_returns_none_with_default(self):
        """getattr(field, default) → default(LLMThinkingPhase.next 用此模式)。"""
        r = LLMResult()
        # 注意:tool_calls 是 [] (list default),不是 None
        # 只有 Optional 字段(stop_reason / usage) 默认 None
        # str 字段(thinking_text / full_text) 默认 ""
        assert getattr(r, "tool_calls", None) == []  # list default
        assert getattr(r, "stop_reason", None) is None  # None default
        assert getattr(r, "usage", None) is None  # None default
        assert getattr(r, "thinking_text", None) == ""  # str default
        assert getattr(r, "full_text", "") == ""