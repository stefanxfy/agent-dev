"""tests/test_mcp_config.py — MCP 配置解析单测（Step 2）。"""

from agent_core.mcp.config import parse_mcp_servers, McpServerConfig


def test_parse_stdio_server():
    settings = {"mcp": {"servers": {
        "fs": {"type": "stdio", "command": "npx", "args": ["-y", "server-fs", "/tmp"]}
    }}}
    cfgs = parse_mcp_servers(settings)
    assert len(cfgs) == 1
    c = cfgs[0]
    assert c.name == "fs"
    assert c.kind == "stdio"
    assert c.command == "npx"
    assert c.args == ["-y", "server-fs", "/tmp"]
    assert c.env is None
    assert c.enabled is True


def test_parse_http_server():
    settings = {"mcp": {"servers": {
        "remote": {"type": "http", "url": "https://x.com/mcp",
                   "headers": {"Authorization": "Bearer t"}}
    }}}
    cfgs = parse_mcp_servers(settings)
    assert len(cfgs) == 1
    c = cfgs[0]
    assert c.kind == "http"
    assert c.url == "https://x.com/mcp"
    assert c.headers == {"Authorization": "Bearer t"}


def test_parse_sse_alias_to_http():
    settings = {"mcp": {"servers": {"r": {"type": "sse", "url": "https://x.com"}}}}
    assert parse_mcp_servers(settings)[0].kind == "http"


def test_parse_infer_from_command_when_no_type():
    settings = {"mcp": {"servers": {"r": {"command": "npx", "args": ["x"]}}}}
    assert parse_mcp_servers(settings)[0].kind == "stdio"


def test_parse_infer_from_url_when_no_type():
    settings = {"mcp": {"servers": {"r": {"url": "https://x.com"}}}}
    assert parse_mcp_servers(settings)[0].kind == "http"


def test_parse_empty_and_missing():
    assert parse_mcp_servers({}) == []
    assert parse_mcp_servers({"mcp": {}}) == []
    assert parse_mcp_servers({"mcp": {"servers": {}}}) == []
    assert parse_mcp_servers(None) == []
    assert parse_mcp_servers("not a dict") == []


def test_parse_bad_server_skipped_others_kept():
    settings = {"mcp": {"servers": {
        "good": {"type": "stdio", "command": "npx"},
        "bad": {"type": "stdio"},  # 缺 command
        "bad2": "not a dict",
    }}}
    cfgs = parse_mcp_servers(settings)
    assert len(cfgs) == 1
    assert cfgs[0].name == "good"


def test_parse_enabled_false_preserved():
    # enabled=False 仍解析出来（上层 connect_all 决定是否跳过）
    settings = {"mcp": {"servers": {"r": {"type": "stdio", "command": "npx", "enabled": False}}}}
    cfgs = parse_mcp_servers(settings)
    assert len(cfgs) == 1
    assert cfgs[0].enabled is False


def test_parse_unsupported_type_skipped():
    settings = {"mcp": {"servers": {"r": {"type": "websocket", "url": "ws://x"}}}}
    assert parse_mcp_servers(settings) == []  # websocket 不支持


def test_parse_env_and_dict_types():
    settings = {"mcp": {"servers": {
        "r": {"type": "stdio", "command": "x", "env": {"API_KEY": "k", "PORT": 8080}}
    }}}
    c = parse_mcp_servers(settings)[0]
    assert c.env == {"API_KEY": "k", "PORT": "8080"}  # int 值归一为 str


def test_load_from_settings(tmp_path, monkeypatch):
    import json
    p = tmp_path / "settings.json"
    p.write_text(json.dumps(
        {"mcp": {"servers": {"fs": {"type": "stdio", "command": "npx"}}}}
    ))
    monkeypatch.setenv("AGENT_SETTINGS_PATH", str(p))
    from agent_core.mcp.config import load_mcp_config_from_settings
    cfgs = load_mcp_config_from_settings()
    assert len(cfgs) == 1
    assert cfgs[0].name == "fs"
