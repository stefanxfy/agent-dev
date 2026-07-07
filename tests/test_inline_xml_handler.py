"""
Handler tests for 003-react-inline-xml-fallback-parser.

TDD: T010 写在 InlineXmlFallbackHandler 实现前(应失败);
T023-T026, T029 写在 handler 完整实现后。

测试范围:InlineXmlFallbackHandler.handle(ctx) 行为。
- 集成用 _LLMResult as stage_outputs(真实结构)
- 不启动完整 agent — 只构造最小 ctx 模拟 stage_outputs
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from agent_core.turn_chain import (
    InlineXmlFallbackHandler,
    _LLMResult,
    TurnContext,
    AgentPhase,
)


FIXTURES_DIR = Path(__file__).parent / "fixtures" / "inline_xml"


def _load_fixture_first(name: str) -> dict:
    p = FIXTURES_DIR / name
    return json.loads([l for l in p.read_text().splitlines() if l.strip()][0])


def _make_ctx(stage_outputs: _LLMResult, agent=None) -> TurnContext:
    """最小 TurnContext:只设 stage_outputs。"""
    from agent_core.agent_state import RunState
    run_state = RunState() if False else None  # 测试不依赖 run_state
    ctx = TurnContext(run_state=run_state)
    ctx.stage_outputs = stage_outputs
    return ctx


# ── US1: T010 — handler no-op when structured calls present ───────


class TestUS1NoOpWhenStructured:
    def test_handler_no_op_when_structured_calls_present(self):
        """当 stage_outputs.tool_calls 已有结构化 tool_call,handler 应当 no-op,
        不重复追加 inline-XML 解析结果(避免重复执行同一 tool)。"""
        case = _load_fixture_first("06_mixed_with_structured.jsonl")
        # 模拟 ChunkParseHandler 已写 1 个结构化 tool_call(stop_reason=tool_use)
        existing = case.get("structured_tool_calls", [])
        existing_tc = existing[0]
        from agent_core.llm.types import ToolCallDelta
        seed = ToolCallDelta(
            tool_name=existing_tc["tool_name"],
            tool_input=existing_tc["tool_input"],
            tool_use_id=existing_tc["tool_use_id"],
            is_final=existing_tc.get("is_final", True),
        )
        stage = _LLMResult(
            tool_calls=[seed],
            full_text=case["text"],
            stop_reason="tool_use",
        )
        ctx = _make_ctx(stage)

        handler = InlineXmlFallbackHandler(agent=None)
        handler.handle(ctx)

        # 结构化 tool_call 应保留;inline-XML 不应被追加
        assert len(ctx.stage_outputs.tool_calls) == 1
        assert ctx.stage_outputs.tool_calls[0].tool_use_id == "toolu_real_1"
        # stop_reason 不应被覆盖(已经是 tool_use)
        assert ctx.stage_outputs.stop_reason == "tool_use"


# ── US4: T023-T026, T029 — observable / audit / disabled / benchmark / idempotency


class TestUS4ActivationLog:
    def test_activation_logs_single_info_marker(self, caplog):
        """T023 — 激活时恰好 1 条 INFO 来自 agent_core.react.inline_xml_fallback 子 logger;
        格式:provider=<hash> blocks=<int> first_tool=<name> original_stop=<stop>"""
        from agent_core.llm.types import ToolCallDelta
        stage = _LLMResult(
            tool_calls=[],
            full_text='<tool_call>{"name":"Bash","input":{"command":"echo"}}</tool_call>',
            stop_reason="end_turn",
        )
        ctx = _make_ctx(stage)

        handler = InlineXmlFallbackHandler(agent=None)
        with caplog.at_level(logging.INFO, logger="agent_core.react.inline_xml_fallback"):
            handler.handle(ctx)

        info_records = [
            r for r in caplog.records
            if r.name == "agent_core.react.inline_xml_fallback" and r.levelno == logging.INFO
        ]
        assert len(info_records) == 1
        msg = info_records[0].getMessage()
        assert "blocks=1" in msg
        assert "first_tool=Bash" in msg
        assert "original_stop=end_turn" in msg
        # 不应包含 input 内容(canary 防泄漏)
        assert "echo" not in msg

    def test_disabled_by_config_no_op_with_debug_log(self, caplog):
        """T025 — cfg.inline_xml_fallback.enabled=False → no-op,1 条 DEBUG,无 INFO"""
        from agent_core.llm.types import ToolCallDelta
        from agent_core.config import config as _cfg

        # 改 singleton config(测试隔离:teardown 还原)
        original_enabled = _cfg.inline_xml_fallback.enabled
        try:
            _cfg.inline_xml_fallback.enabled = False

            stage = _LLMResult(
                tool_calls=[],
                full_text='<tool_call>{"name":"Bash","input":{"command":"echo"}}</tool_call>',
                stop_reason="end_turn",
            )
            ctx = _make_ctx(stage)

            handler = InlineXmlFallbackHandler(agent=None)
            with caplog.at_level(logging.DEBUG, logger="agent_core.react.inline_xml_fallback"):
                handler.handle(ctx)

            assert ctx.stage_outputs.tool_calls == []
            assert ctx.stage_outputs.stop_reason == "end_turn"
            info_records = [
                r for r in caplog.records
                if r.name == "agent_core.react.inline_xml_fallback" and r.levelno == logging.INFO
            ]
            assert info_records == []
            debug_records = [
                r for r in caplog.records
                if r.name == "agent_core.react.inline_xml_fallback" and r.levelno == logging.DEBUG
            ]
            assert len(debug_records) == 1
            assert "disabled" in debug_records[0].getMessage().lower()
        finally:
            _cfg.inline_xml_fallback.enabled = original_enabled

    def test_single_block_under_5ms_benchmark(self):
        """T026 — 单 block 解析 1000-iter 平均 < 5ms(per SC-005)"""
        import time
        text = '<tool_call>{"name":"Bash","input":{"command":"echo hi"}}</tool_call>'
        # warmup
        for _ in range(50):
            parse_inline_xml_tool_calls_text(text)
        n_iter = 1000
        t0 = time.perf_counter()
        for _ in range(n_iter):
            parse_inline_xml_tool_calls_text(text)
        elapsed_ms = (time.perf_counter() - t0) * 1000
        mean_ms = elapsed_ms / n_iter
        assert mean_ms < 5.0, f"mean {mean_ms:.3f} ms exceeds 5 ms budget"

    def test_handler_is_idempotent_on_second_pass(self, caplog):
        """T029 — FR-008:handler 对同一 stage_outputs 第二次调用应 no-op。
        不会有第二个 INFO 日志,不会重复追加 tool_calls。"""
        from agent_core.llm.types import ToolCallDelta
        stage = _LLMResult(
            tool_calls=[],
            full_text='<tool_call>{"name":"Bash","input":{"command":"echo"}}</tool_call>',
            stop_reason="end_turn",
        )
        ctx = _make_ctx(stage)

        handler = InlineXmlFallbackHandler(agent=None)
        with caplog.at_level(logging.INFO, logger="agent_core.react.inline_xml_fallback"):
            handler.handle(ctx)
            first_count = len([t for t in ctx.stage_outputs.tool_calls])
            first_stop = ctx.stage_outputs.stop_reason
            first_info_count = sum(
                1 for r in caplog.records
                if r.name == "agent_core.react.inline_xml_fallback" and r.levelno == logging.INFO
            )
            # 第二次
            handler.handle(ctx)
            second_count = len([t for t in ctx.stage_outputs.tool_calls])
            second_stop = ctx.stage_outputs.stop_reason
            second_info_count = sum(
                1 for r in caplog.records
                if r.name == "agent_core.react.inline_xml_fallback" and r.levelno == logging.INFO
            )

        assert first_count == 1
        assert second_count == 1, "second pass should not re-append"
        assert first_stop == "tool_use"
        assert second_stop == "tool_use"
        assert first_info_count == 1
        assert second_info_count == 1, "second pass should not emit another INFO"


# ── helpers ───────────────────────────────────────────────────────


def parse_inline_xml_tool_calls_text(text: str):
    from agent_core.react import parse_inline_xml_tool_calls
    return parse_inline_xml_tool_calls(text)
