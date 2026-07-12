"""tests/test_mcp_unregister.py — Phase 2 Step 4: ToolRegistry unregister 单测。"""

from agent_core.tools.base import ToolDef, ToolRegistry


def _tool(name):
    return ToolDef(
        name=name, description="d",
        parameters={"type": "object", "properties": {}},
        handler=lambda **k: "ok",
    )


def test_unregister_single_existing():
    r = ToolRegistry()
    r.register(_tool("mcp__s__t1"))
    assert r.unregister("mcp__s__t1") is True
    assert "mcp__s__t1" not in r.list_names()


def test_unregister_nonexistent_returns_false():
    assert ToolRegistry().unregister("ghost") is False


def test_unregister_by_prefix_removes_matches_keeps_others():
    r = ToolRegistry()
    r.register(_tool("mcp__s__t1"))
    r.register(_tool("mcp__s__t2"))
    r.register(_tool("Bash"))     # 不该被删
    count = r.unregister_by_prefix("mcp__s__")
    assert count == 2
    assert "mcp__s__t1" not in r.list_names()
    assert "mcp__s__t2" not in r.list_names()
    assert "Bash" in r.list_names()


def test_unregister_by_prefix_no_match_returns_zero():
    r = ToolRegistry()
    r.register(_tool("Bash"))
    assert r.unregister_by_prefix("mcp__x__") == 0
    assert "Bash" in r.list_names()


def test_unregister_does_not_affect_others():
    r = ToolRegistry()
    r.register(_tool("a"))
    r.register(_tool("b"))
    r.unregister("a")
    assert r.list_names() == ["b"]


def test_unregister_then_list_schemas_excludes_it():
    """unregister 后 list_schemas 不应再含该工具（验证 schema 与 _tools 一致）。"""
    r = ToolRegistry()
    r.register(_tool("mcp__s__t1"))
    r.unregister_by_prefix("mcp__s__")
    schemas = r.list_schemas()
    assert all(s["name"] != "mcp__s__t1" for s in schemas)
