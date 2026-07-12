"""tests/test_mcp_materialize.py — MCP 物化层单测（Step 5）。"""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from agent_core.mcp.materialize import (
    materialize_tools,
    _normalize_call_result,
    _sanitize_unicode,
)


# ── _normalize_call_result ──────────────────────────────────────────
def test_normalize_text_content():
    r = SimpleNamespace(isError=False, content=[SimpleNamespace(type="text", text="hello")], structuredContent=None)
    assert _normalize_call_result(r) == "hello"


def test_normalize_multiple_text_contents_joined():
    r = SimpleNamespace(isError=False, content=[
        SimpleNamespace(type="text", text="a"),
        SimpleNamespace(type="text", text="b"),
    ], structuredContent=None)
    assert _normalize_call_result(r) == "a\nb"


def test_normalize_image_placeholder():
    r = SimpleNamespace(isError=False, content=[
        SimpleNamespace(type="image", data="abcd1234", mimeType="image/png"),
    ], structuredContent=None)
    assert _normalize_call_result(r) == "[image: image/png, 8 chars base64]"


def test_normalize_audio_placeholder():
    r = SimpleNamespace(isError=False, content=[
        SimpleNamespace(type="audio", mimeType="audio/wav"),
    ], structuredContent=None)
    assert _normalize_call_result(r) == "[audio: audio/wav]"


def test_normalize_resource_placeholder():
    r = SimpleNamespace(isError=False, content=[
        SimpleNamespace(type="resource", uri="file:///x"),
    ], structuredContent=None)
    assert _normalize_call_result(r) == "[resource: file:///x]"


def test_normalize_is_error_raises():
    r = SimpleNamespace(isError=True, content=[SimpleNamespace(type="text", text="boom")], structuredContent=None)
    with pytest.raises(RuntimeError, match="boom"):
        _normalize_call_result(r)


def test_normalize_structured_appended():
    r = SimpleNamespace(isError=False, content=[SimpleNamespace(type="text", text="x")], structuredContent={"k": 1})
    out = _normalize_call_result(r)
    assert "x" in out and "[structured]" in out and '"k": 1' in out


def test_normalize_empty_result():
    r = SimpleNamespace(isError=False, content=[], structuredContent=None)
    assert _normalize_call_result(r) == "(empty result)"


# ── _sanitize_unicode ───────────────────────────────────────────────
def test_sanitize_replaces_control_chars():
    assert _sanitize_unicode("a\x00b") == "a\\x00b"
    assert _sanitize_unicode("x\x07y") == "x\\x07y"


def test_sanitize_preserves_tab_newline():
    assert _sanitize_unicode("a\tb\nc\rd") == "a\tb\nc\rd"


# ── materialize_tools ───────────────────────────────────────────────
def test_materialize_basic():
    tool = SimpleNamespace(
        name="read_file", description="read a file",
        inputSchema={"type": "object", "properties": {"p": {"type": "string"}}},
    )
    mgr = MagicMock()
    mgr.call_tool.return_value = "result"
    defs = materialize_tools("fs", [tool], mgr)
    assert len(defs) == 1
    td = defs[0]
    assert td.name == "mcp__fs__read_file"
    assert td.category == "mcp"
    assert "[mcp:fs]" in td.description
    assert td.parameters == {"type": "object", "properties": {"p": {"type": "string"}}}
    # handler 闭包调通 manager.call_tool
    out = td.handler(p="x")
    mgr.call_tool.assert_called_once_with("fs", "read_file", {"p": "x"})
    assert out == "result"


def test_materialize_handler_pops_cancel_event():
    tool = SimpleNamespace(name="t", description="d", inputSchema={})
    mgr = MagicMock()
    mgr.call_tool.return_value = "ok"
    defs = materialize_tools("s", [tool], mgr)
    defs[0].handler(_cancel_event="evt", arg=1)
    mgr.call_tool.assert_called_once_with("s", "t", {"arg": 1})  # _cancel_event 被吞


def test_materialize_name_conflict_dedup():
    t1 = SimpleNamespace(name="read", description="d", inputSchema={})
    t2 = SimpleNamespace(name="read", description="d", inputSchema={})
    defs = materialize_tools("fs", [t1, t2], MagicMock())
    assert defs[0].name == "mcp__fs__read"
    assert defs[1].name == "mcp__fs__read_2"


def test_materialize_empty_schema_fallback():
    tool = SimpleNamespace(name="t", description="d", inputSchema=None)
    defs = materialize_tools("s", [tool], MagicMock())
    assert defs[0].parameters == {"type": "object", "properties": {}}


def test_materialize_cleans_unsafe_tool_name():
    tool = SimpleNamespace(name="read.file", description="d", inputSchema={})
    defs = materialize_tools("my-server", [tool], MagicMock())
    assert defs[0].name == "mcp__my_server__read_file"
