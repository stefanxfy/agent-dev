"""tests/test_mcp_hello_http.py — hello_mcp_server.py 的 streamable-http 路径 E2E 测试。

跨进程双开验证：subprocess.Popen 启 server 在随机端口 + 探活等 ready +
McpManager 走 streamable-http client 完整跑通 tools/resources/prompts/list_changed
各原语，证明 transport 切换无功能差异。

跑：.venv/bin/python -m pytest tests/test_mcp_hello_http.py -v

实现笔记：MCP SDK 默认 httpx.AsyncClient trust_env=True，会读 macOS 系统代理
（scutil HTTPProxy）把 localhost POST 转发到 127.0.0.1:7890 → 502 Bad Gateway。
client.py 在 _open_transport 里显式 trust_env=False 绕过，curl/requests 走 PAC 例外
正常。
"""

import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request

import pytest

from agent_core.mcp.config import McpServerConfig
from agent_core.mcp.manager import McpManager

_FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "hello_mcp_server.py")


def _free_port() -> int:
    """拿一个 ephemeral 端口（0 让 OS 分配，getsockname 取回）。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_http_ready(url: str, timeout: float = 15.0) -> None:
    """探活 streamable-http server：循环 GET 直到连接成功。

    streamable-http 的 /mcp 端点 GET 不接 application/json 会返 406，
    任何 < 500 都说明 server 在听（4xx 是协议层错误，不是连接错误）。
    """
    deadline = time.time() + timeout
    last_err = None
    while time.time() < deadline:
        try:
            urllib.request.urlopen(url, timeout=1.5)
            return
        except urllib.error.HTTPError as e:
            if e.code < 500:
                return
            last_err = e
        except (urllib.error.URLError, ConnectionRefusedError, OSError) as e:
            last_err = e
            time.sleep(0.2)
    raise RuntimeError(f"HTTP server at {url} not ready after {timeout}s: {last_err}")


@pytest.fixture
def hello_http_server():
    """subprocess 启 hello_mcp_server.py 在随机端口，yield URL，terminate 关。"""
    port = _free_port()
    url = f"http://127.0.0.1:{port}/mcp"
    proc = subprocess.Popen(
        [sys.executable, _FIXTURE, "--transport=http", "--port", str(port)],
        stdin=subprocess.DEVNULL,   # 关键：HTTP transport 不要继承 stdin
        stdout=subprocess.DEVNULL,  # mcp 用 stdout 通信，禁掉避免混入 pytest capture
        stderr=subprocess.PIPE,     # 留 stderr 排查
    )
    try:
        _wait_http_ready(url, timeout=10.0)
        yield url
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=3.0)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=2.0)


def _new_mgr(url: str, call_timeout=20):
    return McpManager(
        [McpServerConfig(name="hello", kind="http", url=url)],
        connect_timeout=30, call_timeout=call_timeout,
    )


# ─── connect 基础 ──────────────────────────────────────────────────

def test_http_connect_lists_server_and_tools(hello_http_server):
    """HTTP transport 下 connect_all 能拿到 7 个 tools。"""
    mgr = _new_mgr(hello_http_server)
    try:
        results = mgr.connect_all()
        assert "hello" in results
        tool_names = sorted(td.name for td in results["hello"])
        assert tool_names == sorted([
            "mcp__hello__echo",
            "mcp__hello__add",
            "mcp__hello__get_image",
            "mcp__hello__read_root_file",
            "mcp__hello__long_operation",
            "mcp__hello__trigger_error",
            "mcp__hello__manage_dynamic_tool",
        ])
    finally:
        mgr.dispose()


# ─── Tools 内容类型 ────────────────────────────────────────────────

def test_http_tool_echo_returns_text(hello_http_server):
    mgr = _new_mgr(hello_http_server)
    try:
        mgr.connect_all()
        out = mgr.call_tool("hello", "echo", {"text": "ping"})
        assert "echo: ping" in out
    finally:
        mgr.dispose()


def test_http_tool_add_returns_structured_content(hello_http_server):
    mgr = _new_mgr(hello_http_server)
    try:
        mgr.connect_all()
        out = mgr.call_tool("hello", "add", {"a": 7, "b": 35})
        assert "42" in out
        assert "[structured]" in out
    finally:
        mgr.dispose()


def test_http_tool_get_image_returns_image_placeholder(hello_http_server):
    """image 走 _normalize_call_result 的 image 分支。"""
    mgr = _new_mgr(hello_http_server)
    try:
        mgr.connect_all()
        out = mgr.call_tool("hello", "get_image", {})
        assert "[image:" in out
        assert "image/png" in out
    finally:
        mgr.dispose()


def test_http_tool_trigger_error_raises(hello_http_server):
    mgr = _new_mgr(hello_http_server)
    try:
        mgr.connect_all()
        try:
            mgr.call_tool("hello", "trigger_error", {"reason": "boom"})
        except RuntimeError as e:
            assert "boom" in str(e)
        else:
            raise AssertionError("expected RuntimeError")
    finally:
        mgr.dispose()


# ─── Resources ──────────────────────────────────────────────────────

def test_http_read_resource_text(hello_http_server):
    mgr = _new_mgr(hello_http_server)
    try:
        mgr.connect_all()
        out = mgr.read_resource("hello", "text://hello")
        assert "hello from text resource" in out
    finally:
        mgr.dispose()


def test_http_read_resource_uri_template_substitution(hello_http_server):
    """config://{key} URI 模板 HTTP transport 下也工作。"""
    mgr = _new_mgr(hello_http_server)
    try:
        mgr.connect_all()
        out = mgr.read_resource("hello", "config://demo")
        assert "demo=value-demo" in out
    finally:
        mgr.dispose()


# ─── Prompts ────────────────────────────────────────────────────────

def test_http_get_prompt_multi_message(hello_http_server):
    mgr = _new_mgr(hello_http_server)
    try:
        mgr.connect_all()
        out = mgr.get_prompt("hello", "multi_turn_dialog", {"topic": "asyncio"})
        assert "asyncio" in out
        assert out.count("[user]") >= 2 or out.count("assistant") >= 1
    finally:
        mgr.dispose()


# ─── list_changed 动态增删（验证 transport 切到 HTTP 也走通）────────

def test_http_dynamic_tool_add_triggers_list_changed(hello_http_server):
    """HTTP transport 下 add 动态 tool → 通知 → 客户端重 list → registered_tools 反映。"""
    mgr = _new_mgr(hello_http_server)
    try:
        mgr.connect_all()
        before = [td.name for td in mgr.registered_tools()["hello"]]
        assert "mcp__hello__dyn_http" not in before

        out = mgr.call_tool(
            "hello", "manage_dynamic_tool",
            {"action": "add", "name": "dyn_http"},
        )
        assert "added dyn_http" in out

        time.sleep(1.0)   # 等 list_changed 通知 + 重 list 完成
        after = [td.name for td in mgr.registered_tools()["hello"]]
        assert "mcp__hello__dyn_http" in after
    finally:
        mgr.dispose()


# ─── Roots 集成 ─────────────────────────────────────────────────────

def test_http_read_root_file_within_root(hello_http_server):
    """HTTP transport 下 roots 声明同样生效（client.list_roots callback）。"""
    with tempfile.TemporaryDirectory() as tmp:
        sample = os.path.join(tmp, "sample.txt")
        with open(sample, "w") as f:
            f.write("http-roots-ok")

        mgr = McpManager(
            [McpServerConfig(name="hello", kind="http", url=hello_http_server)],
            connect_timeout=30, call_timeout=15,
            roots=[{"uri": f"file://{tmp}", "name": "tmp"}],
        )
        try:
            mgr.connect_all()
            out = mgr.call_tool("hello", "read_root_file", {"path": sample})
            assert "http-roots-ok" in out
        finally:
            mgr.dispose()
