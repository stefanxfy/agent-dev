#!/usr/bin/env python
"""hello_mcp_server.py — 综合 MCP server，覆盖 Tier 1 + Tier 2 测试需求。

设计目的：
- 8 tools 覆盖 agent_core MCP client materialize 的所有 content 分支：
  text / structured_content / image / roots-scoped / progress / isError / 动态 list_changed 触发
- 4 resources 覆盖 text / URI 模板 / blob / metadata 各种 contents 类型
- 5 prompts 覆盖无参 / 必选 / 可选 / 多消息 / 嵌入 resource 引用
- 同时支持 stdio（自动化 CI）+ streamable-http（生产双进程验证）transport

作为 subprocess fixture 用法：
    python tests/fixtures/hello_mcp_server.py                            # stdio
    python tests/fixtures/hello_mcp_server.py --transport=http --port 8765  # HTTP

list_changed 触发路径：
- manage_dynamic_tool("add"/"remove", name) → mcpserver.add_tool/remove_tool 改 FastMCP
  内部 _tool_manager → 手动 `asyncio.create_task(ctx.request_context.session.send_tool_list_changed())`
  fire-and-forget 通知（不能用 await：handler 内 await 会与当前 response frame 争
  ServerSession write_lock，client 端连接超时）
- multi-kind list_changed（tools + resources / prompts 同时）由 unit test
  tests/test_mcp_list_changed.py::test_pending_multi_kinds_all_refreshed 覆盖；
  fixture 不复测 —— ServerSession.send_notification 在 stdio 下与 response frame 互锁
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import logging
import os
import sys

from pydantic import BaseModel, Field

from mcp import types as mcp_types
from mcp.server.fastmcp import Context, FastMCP, Image

# ─── Server-side logging ─────────────────────────────────────────────
# mcp 协议占用 stdout，logger 走 stderr（pytest capture / 终端 / 测试 fixture
# 都能看到）。每个 tool / resource / prompt handler 入口打 call 日志 +
# args，HTTP/stdio 调试时一眼能定位是哪个方法被调、参数是什么。
#
# 关掉：HELLO_MCP_QUIET=1（CI/批量测试安静跑）
# 调详：HELLO_MCP_DEBUG=1（连 read_resource URI、get_prompt name 也打）
_handler = logging.StreamHandler(sys.stderr)
_handler.setFormatter(logging.Formatter("[hello_mcp] %(message)s"))
server_logger = logging.getLogger("hello_mcp.server")
server_logger.setLevel(logging.DEBUG)
server_logger.addHandler(_handler)
server_logger.propagate = False
if os.environ.get("HELLO_MCP_QUIET") == "1":
    server_logger.setLevel(logging.WARNING)
elif os.environ.get("HELLO_MCP_DEBUG") != "1":
    # 默认 INFO：tool 调通就打一行，handler 内部不刷屏
    server_logger.setLevel(logging.INFO)


def _log_call(kind: str, method: str, **params) -> None:
    """统一打 handler 入口日志：`>> tool/echo(text='hi')` 形式，走 stderr。

    kind: 'tool' / 'resource' / 'prompt'。params 为已 bound 的调用参数；
    过长值（ctx / bytes）由调用方决定是否传（默认 ctx 不传，避免刷屏）。
    第 2 参数故意叫 method（不叫 name）——避免与 handler 自带 name= 参数关键字冲突。
    """
    args = ", ".join(f"{k}={v!r}" for k, v in params.items())
    server_logger.info(f">> {kind}/{method}({args})")


# ─── 常量 ─────────────────────────────────────────────────────────────
# 1x1 透明 PNG（base64） — 用于 image / blob resource
PNG_B64 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
PNG_BYTES = base64.b64decode(PNG_B64)


# ─── Pydantic models（structured_content 必需） ─────────────────────
class AddResult(BaseModel):
    """add() 的 structured_content 输出。"""
    sum: int = Field(description="Sum of inputs")
    a: int = Field(description="First addend")
    b: int = Field(description="Second addend")


# ─── MCP server 实例 ────────────────────────────────────────────────
# host/port 经 settings 在 main() __main__ 段注入（默认 stdio 不需要）
mcpserver = FastMCP(
    "hello",
    instructions="Comprehensive test server for agent_core MCP client.",
)


# ─── Tools (8) ──────────────────────────────────────────────────────

@mcpserver.tool()
def echo(text: str) -> str:
    """回显文本（基础 text content 路径）。"""
    _log_call("tool", "echo", text=text)
    return f"echo: {text}"


@mcpserver.tool(structured_output=True)
def add(a: int, b: int) -> AddResult:
    """两数相加（structured_content：触发 client 的 .structuredContent 分支）。"""
    _log_call("tool", "add", a=a, b=b)
    return AddResult(sum=a + b, a=a, b=b)


@mcpserver.tool()
def get_image() -> Image:
    """返回 1x1 PNG（image content：触发 client 的 image 分支）。"""
    _log_call("tool", "get_image")
    return Image(data=PNG_BYTES, format="png")


@mcpserver.tool()
async def read_root_file(path: str, ctx: Context) -> str:
    """从客户端声明的 roots 内读取文件。

    路径超出 roots 边界或文件不存在 → 返回 isError 形式的 str（demo 给 client 文本错误处理）。

    注意：roots 验证需 client 端在 initialize 时声明 roots capability（agent_core 已支持）。
    """
    _log_call("tool", "read_root_file", path=path)
    try:
        roots_result = await ctx.session.list_roots()
    except Exception as e:
        return f"Error: list_roots not supported or failed: {e}"

    roots = []
    for r in roots_result.roots:
        # r.uri 是 mcp.types.FileUrl，有 .path 属性
        u = r.uri
        if hasattr(u, "path") and u.path:
            roots.append(u.path.rstrip("/"))

    if not roots:
        return f"Error: client declared no roots; cannot read {path}"

    abs_path = os.path.abspath(path)
    in_roots = any(abs_path.startswith(r) for r in roots)
    server_logger.debug(f"  read_root_file: abs={abs_path} roots={roots} in_roots={in_roots}")
    if not in_roots:
        return f"Error: {abs_path} not in declared roots {roots}"
    if not os.path.exists(abs_path):
        return f"Error: file not found: {abs_path}"
    try:
        with open(abs_path, "r", encoding="utf-8") as f:
            return f.read()
    except OSError as e:
        return f"Error: read failed: {e}"


@mcpserver.tool()
async def long_operation(steps: int = 3, ctx: Context = None) -> str:
    """长操作，每步 report progress（验证 client 不阻塞、不丢通知）。"""
    _log_call("tool", "long_operation", steps=steps)
    if ctx is None:
        return "Error: no context"
    for i in range(steps):
        await ctx.report_progress(i + 1, steps)
        server_logger.debug(f"  progress {i + 1}/{steps}")
    return f"completed {steps} steps"


@mcpserver.tool()
def trigger_error(reason: str = "intentional") -> str:
    """返回 isError=true 的工具（触发 client ToolPairPersist 错误路径）。"""
    _log_call("tool", "trigger_error", reason=reason)
    from mcp.server.fastmcp.exceptions import ToolError
    raise ToolError(f"intentional error: {reason}")


# ─── 动态 tool 触发 list_changed ────────────────────────────────────
_DYNAMIC_TOOLS: dict[str, str] = {}   # name → description


def _make_dynamic_handler(name: str):
    async def _h():
        return f"i am {name}, dynamically added"
    _h.__name__ = f"dyn_{name}"
    return _h


@mcpserver.tool()
async def manage_dynamic_tool(action: str, name: str, ctx: Context) -> str:
    """动态 add/remove tool → 手动 emit tools/list_changed 通知。

    实现要点（踩坑后定稿）:
    - 必须是 `async def`：FastMCP 对 sync tool handler 跑在线程池，线程里
      没有 event loop，`asyncio.create_task` 会抛 "no current event loop"，
      进而让 handler 返不回 response → client 端 connection timeout。
    - send_tool_list_changed 必须 fire-and-forget：直接 await 会与本 handler
      的 response frame 抢 ServerSession 的 write_lock（因为 ServerSession 的
      notification 和 response 共用同一个 write_stream）；asyncio.create_task
      把 notification 推到后台任务，handler 先把 response frame 写出去。

    Args:
        action: 'add' 或 'remove'
        name: tool 名
        ctx: FastMCP Context（FastMCP 自动注入）

    Returns:
        操作结果描述（client 拿来确认是否成功）

    Note:
        multi-kind list_changed（同时 tools + resources / prompts）由 unit test
        tests/test_mcp_list_changed.py::test_pending_multi_kinds_all_refreshed 覆盖；
        本 fixture 不重复测 —— ServerSession.send_notification 在 stdio transport 下
        会与 response frame 互相争 stream lock。
    """
    _log_call("tool", "manage_dynamic_tool", action=action, name=name)
    if action == "add":
        if name in _DYNAMIC_TOOLS:
            return f"tool {name} already exists"
        handler = _make_dynamic_handler(name)
        mcpserver.add_tool(handler, name=name, description=f"dynamically added: {name}")
        _DYNAMIC_TOOLS[name] = f"dynamically added: {name}"
        asyncio.create_task(ctx.request_context.session.send_tool_list_changed())
        server_logger.info(f"  ↪ add '{name}' → emit tools/list_changed (now {len(_DYNAMIC_TOOLS)} dyn tools)")
        return f"added {name}"
    elif action == "remove":
        if name in _DYNAMIC_TOOLS:
            mcpserver.remove_tool(name)
            del _DYNAMIC_TOOLS[name]
            asyncio.create_task(ctx.request_context.session.send_tool_list_changed())
            server_logger.info(f"  ↪ remove '{name}' → emit tools/list_changed (now {len(_DYNAMIC_TOOLS)} dyn tools)")
            return f"removed {name}"
        return f"tool {name} not found"
    return f"unknown action {action}"


# ─── Resources (4) ────────────────────────────────────────────────

@mcpserver.resource("config://{key}")
def get_config(key: str) -> str:
    """URI 模板 resource：用 {key} 路径参数动态生成内容（验证 template 解析）。"""
    _log_call("resource", "config://{key}", key=key)
    return f"config:{key}=value-{key}"


@mcpserver.resource("text://hello")
def get_text_hello() -> str:
    """静态文本 resource。"""
    _log_call("resource", "text://hello")
    return "hello from text resource"


@mcpserver.resource("blob://logo", mime_type="image/png")
def get_logo_blob() -> bytes:
    """二进制 blob resource（验证 client blob 归一分支）。"""
    _log_call("resource", "blob://logo")
    return PNG_BYTES


@mcpserver.resource("meta://info")
def get_meta() -> str:
    """JSON 元数据 resource。"""
    _log_call("resource", "meta://info")
    import json
    return json.dumps({
        "name": "hello_mcp_server",
        "version": "1.0",
        "primitives": ["tools", "resources", "prompts"],
    }, indent=2)


# ─── Prompts (5) ──────────────────────────────────────────────────

@mcpserver.prompt()
def simple_greeting() -> str:
    """无参 prompt。"""
    _log_call("prompt", "simple_greeting")
    return "Say hello to the user warmly."


@mcpserver.prompt()
def review_code(code: str) -> str:
    """必选参数 prompt。"""
    _log_call("prompt", "review_code", code=code)
    return f"Please review this code:\n\n```\n{code}\n```"


@mcpserver.prompt()
def code_review_with_context(code: str, language: str = "python") -> str:
    """必选 + 可选参数 prompt。"""
    _log_call("prompt", "code_review_with_context", code=code, language=language)
    return f"Please review this {language} code:\n\n```{language}\n{code}\n```"


@mcpserver.prompt()
def multi_turn_dialog(topic: str) -> list[mcp_types.PromptMessage]:
    """多消息 prompt：返回 system + user 两条消息（验证 multi-message 归一）。"""
    _log_call("prompt", "multi_turn_dialog", topic=topic)
    return [
        mcp_types.PromptMessage(
            role="assistant",
            content=mcp_types.TextContent(type="text", text=f"You are an expert on {topic}."),
        ),
        mcp_types.PromptMessage(
            role="user",
            content=mcp_types.TextContent(type="text", text=f"Explain {topic} in 3 sentences."),
        ),
    ]


@mcpserver.prompt()
def with_embedded_resource(text: str) -> list[mcp_types.PromptMessage]:
    """嵌入 resource 引用 的 prompt：验证 client 处理 EmbeddedResource 类型。"""
    _log_call("prompt", "with_embedded_resource", text=text)
    return [
        mcp_types.PromptMessage(
            role="user",
            content=mcp_types.TextContent(type="text", text=f"Here is text: {text}"),
        ),
        mcp_types.PromptMessage(
            role="user",
            content=mcp_types.EmbeddedResource(
                type="resource",
                resource=mcp_types.TextResourceContents(
                    uri="text://hello",
                    mimeType="text/plain",
                    text="hello from text resource",
                ),
            ),
        ),
    ]


# ─── Transport 选择 ────────────────────────────────────────────────
def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="hello_mcp_server")
    p.add_argument("--transport", choices=["stdio", "http"], default="stdio",
                   help="Transport: stdio (default) 或 streamable-http")
    p.add_argument("--port", type=int, default=8765,
                   help="HTTP port（仅 --transport=http 生效）")
    return p.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    if args.transport == "http":
        # streamable-http: FastMCP 内部用 uvicorn 在 settings.host/port 跑
        # 测试用 --transport=http 时 client 走 streamable_http_client(新 API) +
        # 自构造 httpx.AsyncClient(trust_env=False 绕过 macOS 系统代理)
        mcpserver.settings.host = "127.0.0.1"
        mcpserver.settings.port = args.port
        server_logger.info(f"starting hello_mcp_server (transport=http port={args.port})")
        mcpserver.run(transport="streamable-http")
    else:
        server_logger.info("starting hello_mcp_server (transport=stdio)")
        mcpserver.run(transport="stdio")
