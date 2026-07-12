"""tests/test_mcp_names.py — MCP 工具命名安全化单测（Step 2）。"""

from agent_core.mcp.names import make_tool_name, server_prefix, is_server_denied


# ── make_tool_name ──────────────────────────────────────────────────
def test_make_tool_name_basic():
    assert make_tool_name("fs", "read_file") == "mcp__fs__read_file"


def test_make_tool_name_cleans_special_chars():
    # - 和 . 都归一为 _
    assert make_tool_name("my-server", "read.file") == "mcp__my_server__read_file"
    assert make_tool_name("fs@1", "tool/sep") == "mcp__fs_1__tool_sep"


def test_make_tool_name_empty_segment():
    assert make_tool_name("", "tool") == "mcp__unnamed__tool"
    assert make_tool_name("fs", "") == "mcp__fs__unnamed"


def test_make_tool_name_truncates_over_64():
    long_tool = "x" * 80
    name = make_tool_name("fs", long_tool)
    assert len(name) <= 64
    assert name.startswith("mcp__fs__")


# ── server_prefix ───────────────────────────────────────────────────
def test_server_prefix():
    assert server_prefix("fs") == "mcp__fs"
    assert server_prefix("my-server") == "mcp__my_server"


# ── is_server_denied ────────────────────────────────────────────────
def test_is_server_denied_exact():
    assert is_server_denied("fs", ["mcp__fs"]) is True


def test_is_server_denied_wildcard():
    assert is_server_denied("fs", ["mcp__fs*"]) is True


def test_is_server_denied_paren():
    assert is_server_denied("fs", ["mcp__fs(*)"]) is True


def test_is_server_denied_not_matched():
    assert is_server_denied("fs", ["mcp__other"]) is False
    assert is_server_denied("fs", ["Bash"]) is False


def test_is_server_denied_no_prefix_false_positive():
    # 关键：mcp__fs 不该命中 mcp__fs2（防前缀误匹配）
    assert is_server_denied("fs2", ["mcp__fs"]) is False
    assert is_server_denied("fs2", ["mcp__fs*"]) is False


def test_is_server_denied_empty_rules():
    assert is_server_denied("fs", []) is False
    assert is_server_denied("fs", None) is False
