"""tests/test_mcp_prompts.py — Phase 2 Step 3: prompts 单测。"""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from agent_core.mcp.materialize import (
    _content_block_to_text,
    _normalize_prompt_result,
    materialize_prompt_tools,
    render_mcp_prompts_section,
)
from agent_core.mcp.manager import McpManager, _LiveServer


# ── _content_block_to_text ──────────────────────────────────────────
def test_content_block_text():
    assert _content_block_to_text(SimpleNamespace(type="text", text="hi")) == "hi"


def test_content_block_image_audio_resource():
    assert _content_block_to_text(SimpleNamespace(type="image", mimeType="image/png")) == "[image: image/png]"
    assert _content_block_to_text(SimpleNamespace(type="audio", mimeType="audio/wav")) == "[audio: audio/wav]"
    assert _content_block_to_text(SimpleNamespace(type="resource", uri="file:///x")) == "[resource: file:///x]"


def test_content_block_unknown():
    assert _content_block_to_text(SimpleNamespace(type="weird")) == "[weird content]"


# ── _normalize_prompt_result ────────────────────────────────────────
def test_normalize_prompt_messages():
    r = SimpleNamespace(messages=[
        SimpleNamespace(role="user", content=SimpleNamespace(type="text", text="q")),
        SimpleNamespace(role="assistant", content=SimpleNamespace(type="text", text="a")),
    ])
    out = _normalize_prompt_result(r)
    assert "[user] q" in out
    assert "[assistant] a" in out


def test_normalize_prompt_empty():
    assert _normalize_prompt_result(SimpleNamespace(messages=[])) == "(empty prompt)"


# ── render_mcp_prompts_section ──────────────────────────────────────
def test_render_prompts_section_lists_names():
    section = render_mcp_prompts_section({
        "srv": [SimpleNamespace(name="review", description="code review", arguments=[
            SimpleNamespace(name="lang"), SimpleNamespace(name="code")])],
    })
    assert "## MCP Prompts" in section
    assert "server=srv name=review" in section
    assert "args=[lang, code]" in section
    assert "code review" in section


def test_render_prompts_section_empty_returns_empty():
    assert render_mcp_prompts_section({}) == ""
    assert render_mcp_prompts_section({"s": []}) == ""


# ── materialize_prompt_tools ────────────────────────────────────────
def test_materialize_prompt_tools_get_mcp_prompt():
    mgr = MagicMock()
    mgr.get_prompt.return_value = "rendered"
    tools = materialize_prompt_tools(mgr)
    assert len(tools) == 1
    assert tools[0].name == "get_mcp_prompt"
    assert tools[0].category == "mcp"
    out = tools[0].handler(server="s", name="review", arguments={"lang": "py"})
    assert out == "rendered"
    mgr.get_prompt.assert_called_with("s", "review", {"lang": "py"})


def test_materialize_prompt_handler_pops_cancel_event():
    mgr = MagicMock()
    mgr.get_prompt.return_value = "ok"
    tools = materialize_prompt_tools(mgr)
    tools[0].handler(_cancel_event="x", server="s", name="p")
    mgr.get_prompt.assert_called_once_with("s", "p", None)


# ── manager.registered_prompts / get_prompt ─────────────────────────
def test_manager_registered_prompts():
    mgr = McpManager([])
    mgr._servers["s"] = _LiveServer(
        session=None, tool_defs=[],
        prompts=[SimpleNamespace(name="p1", description="d", arguments=[])],
    )
    assert mgr.registered_prompts()["s"][0].name == "p1"


def test_manager_get_prompt_unknown_server_raises():
    with pytest.raises(RuntimeError, match="未连接"):
        McpManager([]).get_prompt("ghost", "p")


# ── McpPromptsHandler（段注入 + 幂等 + guard）──────────────────────
def _make_agent_with_prompts(prompts_by_server):
    agent = MagicMock()
    agent._mcp_manager = MagicMock()
    agent._mcp_manager.registered_prompts.return_value = prompts_by_server
    return agent


def test_mcp_prompts_handler_injects_section():
    from agent_core.agent_state import RunState, TurnContext
    from agent_core.turn_chain import McpPromptsHandler

    agent = _make_agent_with_prompts({
        "s": [SimpleNamespace(name="p1", description="d", arguments=[])]})
    ctx = TurnContext(run_state=RunState())
    McpPromptsHandler(agent).handle(ctx)
    assert "## MCP Prompts" in ctx.run_state.system_prompt
    assert "server=s name=p1" in ctx.run_state.system_prompt


def test_mcp_prompts_handler_no_manager_skips():
    from agent_core.agent_state import RunState, TurnContext
    from agent_core.turn_chain import McpPromptsHandler

    agent = MagicMock()
    agent._mcp_manager = None
    ctx = TurnContext(run_state=RunState())
    McpPromptsHandler(agent).handle(ctx)
    assert "## MCP Prompts" not in ctx.run_state.system_prompt


def test_mcp_prompts_handler_empty_prompts_skips():
    from agent_core.agent_state import RunState, TurnContext
    from agent_core.turn_chain import McpPromptsHandler

    agent = _make_agent_with_prompts({"s": []})
    ctx = TurnContext(run_state=RunState())
    McpPromptsHandler(agent).handle(ctx)
    assert "## MCP Prompts" not in ctx.run_state.system_prompt


def test_mcp_prompts_handler_idempotent():
    from agent_core.agent_state import RunState, TurnContext
    from agent_core.turn_chain import McpPromptsHandler

    agent = _make_agent_with_prompts({
        "s": [SimpleNamespace(name="p1", description="d", arguments=[])]})
    ctx = TurnContext(run_state=RunState())
    h = McpPromptsHandler(agent)
    h.handle(ctx)
    before = ctx.run_state.system_prompt
    h.handle(ctx)   # 第二次不翻倍
    assert ctx.run_state.system_prompt == before
