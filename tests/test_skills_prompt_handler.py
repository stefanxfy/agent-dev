"""tests/test_skills_prompt_handler.py — SkillsPromptHandler 注入 + C2 guard(选项 A)。

选项 A 重构 (2026-07-06):handler 用 ctx.append_system 累加 skills 段到 ctx.system_prompt
(替代原 stage_inputs merge)。范式:真实 TurnContext + mock agent → handle → 断言 ctx.system_prompt。
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from agent_core.agent_state import RunState, TurnContext
from agent_core.skills.config import SkillsConfig
from agent_core.skills.registry import SkillsRegistry
from agent_core.turn_chain import HandlerResult, SkillsPromptHandler


def _make_registry_with_workspace(tmp_path) -> SkillsRegistry:
    """构造一个指向 tmp_path workspace 的 registry(含 1 个 hello skill)"""
    skill_dir = tmp_path / "hello"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        '---\nname: hello\ndescription: "Greet"\n---\nbody\n',
        encoding="utf-8",
    )
    cfg = SkillsConfig.from_dict({"paths": {"workspace_dir": str(tmp_path)}})
    return SkillsRegistry(cfg)


def _make_ctx(system_prompt: str = "BASE") -> TurnContext:
    """构造真实 TurnContext,system_prompt 预设(模拟 SystemPromptHandler 已 append base)。"""
    ctx = TurnContext(run_state=RunState())
    ctx.system_prompt = system_prompt
    return ctx


def _make_agent(registry=None, tool_names=None):
    """构造 mock agent,含 skills_registry + tools.list_names()。"""
    agent = MagicMock()
    agent.skills_registry = registry
    tools = MagicMock()
    tools.list_names.return_value = tool_names if tool_names is not None else ["Read", "calc", "bash"]
    agent.tools = tools
    return agent


class TestInjection:
    def test_appends_skills_section_to_system_prompt(self, tmp_path):
        reg = _make_registry_with_workspace(tmp_path)
        agent = _make_agent(registry=reg)
        ctx = _make_ctx("BASE")
        result = SkillsPromptHandler(agent).handle(ctx)
        assert isinstance(result, HandlerResult)
        assert "## Skills (mandatory)" in ctx.system_prompt
        assert "BASE" in ctx.system_prompt  # base 保留
        assert "<name>hello</name>" in ctx.system_prompt

    def test_preserves_existing_memory_block(self, tmp_path):
        # 关键:MemoryRetrieval 已 append mem_block,skills 不能覆盖它(append_system 累加)
        reg = _make_registry_with_workspace(tmp_path)
        agent = _make_agent(registry=reg)
        ctx = _make_ctx("BASE\n\n[记忆库 / 3 hits]\n- ...")
        SkillsPromptHandler(agent).handle(ctx)
        assert "[记忆库" in ctx.system_prompt  # memory 保留
        assert "## Skills" in ctx.system_prompt  # skills 追加(非覆盖)

    def test_idempotent(self, tmp_path):
        # 重复 handle 不翻倍(幂等自检:section 已在 ctx.system_prompt 则跳过)
        reg = _make_registry_with_workspace(tmp_path)
        agent = _make_agent(registry=reg)
        ctx = _make_ctx("BASE")
        SkillsPromptHandler(agent).handle(ctx)
        SkillsPromptHandler(agent).handle(ctx)  # 再调一次
        assert ctx.system_prompt.count("## Skills (mandatory)") == 1  # 没翻倍


class TestC2Guard:
    """spec Edge Case / analyze C2:Read 不在 tool set → 跳过。"""

    def test_skip_when_read_not_in_tools(self, tmp_path):
        reg = _make_registry_with_workspace(tmp_path)
        agent = _make_agent(registry=reg, tool_names=["calc", "bash"])  # 无 Read
        ctx = _make_ctx("BASE")
        SkillsPromptHandler(agent).handle(ctx)
        assert "## Skills" not in ctx.system_prompt
        assert ctx.system_prompt == "BASE"  # 未改

    def test_skip_when_tools_none(self, tmp_path):
        reg = _make_registry_with_workspace(tmp_path)
        agent = MagicMock()
        agent.skills_registry = reg
        agent.tools = None
        ctx = _make_ctx("BASE")
        result = SkillsPromptHandler(agent).handle(ctx)
        assert isinstance(result, HandlerResult)
        assert "## Skills" not in ctx.system_prompt


class TestSkipConditions:
    def test_skip_when_registry_none(self):
        agent = _make_agent(registry=None)
        ctx = _make_ctx("BASE")
        SkillsPromptHandler(agent).handle(ctx)
        assert ctx.system_prompt == "BASE"  # 未改

    def test_skip_when_empty_prompt(self, tmp_path):
        # bundled + workspace 都空 → snapshot.prompt 为空 → 不注入
        empty_ws = tmp_path / "empty_ws"
        empty_ws.mkdir()
        empty_bd = tmp_path / "empty_bd"
        empty_bd.mkdir()
        cfg = SkillsConfig.from_dict({
            "paths": {
                "bundled_dir": str(empty_bd),
                "workspace_dir": str(empty_ws),
            }
        })
        reg = SkillsRegistry(cfg)
        agent = _make_agent(registry=reg)
        ctx = _make_ctx("BASE")
        SkillsPromptHandler(agent).handle(ctx)
        assert ctx.system_prompt == "BASE"  # 无 skill → 不注入

    def test_injects_into_empty_system_prompt(self, tmp_path):
        # 边界:system_prompt 空(SystemPromptHandler 未跑或 base 为空)→ 仍注入 skills 段
        reg = _make_registry_with_workspace(tmp_path)
        agent = _make_agent(registry=reg)
        ctx = _make_ctx("")
        SkillsPromptHandler(agent).handle(ctx)
        assert "## Skills" in ctx.system_prompt  # skills 段注入


class TestSnapshotFailureIsolation:
    def test_handler_does_not_raise_on_snapshot_error(self):
        # registry.snapshot() 抛 → handler 吞掉,不改 ctx.system_prompt
        bad_reg = MagicMock()
        bad_reg.snapshot.side_effect = RuntimeError("boom")
        agent = _make_agent(registry=bad_reg)
        ctx = _make_ctx("BASE")
        result = SkillsPromptHandler(agent).handle(ctx)
        assert isinstance(result, HandlerResult)
        assert ctx.system_prompt == "BASE"  # 失败 → 不改
