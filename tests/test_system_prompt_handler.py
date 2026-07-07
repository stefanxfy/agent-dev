"""tests/test_system_prompt_handler.py — SystemPromptHandler(选项 A)。

选项 A 重构 (2026-07-06):handler 把 agent.system_prompt append 到 ctx.run_state.system_prompt
(替代原 SystemPromptAssembler.place() + stage_inputs)。
"""
from __future__ import annotations

from unittest.mock import MagicMock

from agent_core.agent_state import RunState, TurnContext
from agent_core.turn_chain import HandlerResult, SystemPromptHandler


def _make_ctx() -> TurnContext:
    return TurnContext(run_state=RunState())


def _make_agent(system_prompt=""):
    agent = MagicMock()
    agent.system_prompt = system_prompt
    return agent


class TestSystemPromptHandler:
    def test_appends_base_to_empty_system_prompt(self):
        agent = _make_agent("You are helpful")
        ctx = _make_ctx()
        SystemPromptHandler(agent).handle(ctx)
        assert ctx.run_state.system_prompt == "You are helpful"

    def test_appends_to_existing_system_prompt(self):
        # 验证 append 语义(累加,非覆盖)— append_system 的核心契约
        agent = _make_agent("BASE")
        ctx = _make_ctx()
        ctx.run_state.system_prompt = "PRE"
        SystemPromptHandler(agent).handle(ctx)
        assert ctx.run_state.system_prompt == "PRE\n\nBASE"

    def test_skips_when_empty_system_prompt(self):
        agent = _make_agent("")
        ctx = _make_ctx()
        ctx.run_state.system_prompt = "EXISTING"
        SystemPromptHandler(agent).handle(ctx)
        assert ctx.run_state.system_prompt == "EXISTING"  # 空 base 不 append

    def test_skips_when_system_prompt_none(self):
        agent = _make_agent(None)
        ctx = _make_ctx()
        result = SystemPromptHandler(agent).handle(ctx)
        assert isinstance(result, HandlerResult)
        assert ctx.run_state.system_prompt == ""

    def test_skips_when_agent_none(self):
        ctx = _make_ctx()
        result = SystemPromptHandler(None).handle(ctx)
        assert isinstance(result, HandlerResult)
        assert ctx.run_state.system_prompt == ""
