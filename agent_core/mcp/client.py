"""
MCP 单 server async 连接（运行面，在 McpManager 常驻 loop 内执行）。

职责:
  - connect_server(name, config): async context manager
    open transport（stdio 2-tuple / http 3-tuple）→ ClientSession → initialize
    → get_server_capabilities() → 先查 caps.tools 再 list_tools → yield handle
  - _negotiate_and_list(session, timeout): 能力协商 + list（抽出来便于单测）
  - 任何一步失败 → 抛异常（由 manager 捕获做故障隔离）

SDK API（v1.28.1）:
  stdio_client(StdioServerParameters) as (read, write)               # 2-tuple
  streamablehttp_client(url, headers, timeout, sse_read_timeout) as  # 3-tuple
      (read, write, get_session_id_callback)                          # 第 3 个忽略
  ClientSession(read, write, read_timeout_seconds=timedelta(...))     # 会话级读超时
  session.initialize()                                                # initialize 不单独接受 timeout
  session.get_server_capabilities()  ← 方法，非属性，返 ServerCapabilities | None
  caps.tools is not None 才 list_tools()                              # 能力协商

对齐决策 #5: 能力协商（对齐 Claude Code: initialize → 协商 → 按 capability 发请求）。

生命周期要点（R2）: transport + session 的 async with 嵌套在 connect_server 内，
调用方（manager._connect_one）必须在 `async with connect_server(...) as handle:` 块内
使用 handle.session；块退出 → session 关 → transport 关。
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, AsyncIterator

logger = logging.getLogger("agent_core.mcp")  # 🔌


@dataclass
class McpServerHandle:
    """已连接的 server 句柄。

    session 的生命周期由 connect_server 的 async with 持有——调用方必须在
    `async with connect_server(...) as handle:` 块内使用 handle.session，
    块退出即关闭（manager._connect_one 在块内长持有 + await dispose_event）。
    """

    name: str
    session: Any       # mcp.ClientSession
    capabilities: Any  # types.ServerCapabilities | None
    tools: list        # list[types.Tool]
    resources: list = field(default_factory=list)   # list[types.Resource]（Phase 2 Step 2 填）
    prompts: list = field(default_factory=list)     # list[types.Prompt]（Phase 2 Step 3 填）


@asynccontextmanager
async def _open_transport(config) -> AsyncIterator[tuple[Any, Any]]:
    """open transport，yield (read, write)。统一 stdio 2-tuple 与 http 3-tuple。"""
    if config.kind == "stdio":
        from mcp import StdioServerParameters
        from mcp.client.stdio import stdio_client

        params = StdioServerParameters(
            command=config.command,
            args=list(config.args),
            env=config.env,    # None → SDK 用默认白名单 env（R8：server 缺 PATH 时需显式传）
            cwd=config.cwd,
        )
        async with stdio_client(params) as (read, write):
            yield read, write

    elif config.kind == "http":
        import httpx
        from mcp.client.streamable_http import streamable_http_client

        # 关键：trust_env=False 跳过 macOS 系统代理 / HTTP_PROXY 环境变量
        # ——否则 httpx 默认 trust_env=True，会被 scutil 配的 127.0.0.1:7890 代理劫持，
        # POST localhost 拿 502 Bad Gateway（curl/requests 走 PAC 例外不走代理，所以正常）。
        # fix: 自己构造 httpx.AsyncClient，timeout/headers/auth 走 config 注入
        # → streamable_http_client(新 API)接受 http_client= 预构造 client。
        # 用新 API 替代 deprecated streamablehttp_client，保留向后行为（trust_env=False）。
        from mcp.shared._httpx_utils import MCP_DEFAULT_TIMEOUT, MCP_DEFAULT_SSE_READ_TIMEOUT

        client_kwargs: dict = {"trust_env": False, "follow_redirects": True}
        if config.headers:
            client_kwargs["headers"] = config.headers
        # auth 省略：MVP 用 headers 塞静态 token（决策 #6：不做 OAuth）
        client_kwargs["timeout"] = httpx.Timeout(
            config.timeout or MCP_DEFAULT_TIMEOUT,
            read=config.sse_read_timeout or MCP_DEFAULT_SSE_READ_TIMEOUT,
        )
        async with httpx.AsyncClient(**client_kwargs) as http_client:
            async with streamable_http_client(
                config.url,
                http_client=http_client,
            ) as (read, write, _get_session_id):  # 3-tuple（R4：第 3 个忽略）
                yield read, write

    else:
        raise ValueError(f"unknown mcp transport kind: {config.kind}")


async def _negotiate_and_list(session: Any, list_timeout: float) -> tuple[Any, list]:
    """
    能力协商 + list_tools（对齐决策 #5：先查 caps.tools 再 list）。

    Returns:
        (capabilities, tools)。caps.tools 为 None/缺 → tools=[]，不发 list_tools
        （省一次注定被拒的往返，协议合规）。
    """
    caps = session.get_server_capabilities()
    tools: list = []
    if caps is not None and getattr(caps, "tools", None) is not None:
        tools = (await asyncio.wait_for(session.list_tools(), timeout=list_timeout)).tools
    return caps, tools


async def _list_resources_if_capable(session: Any, caps: Any, list_timeout: float) -> list:
    """caps.resources 非空时分页 list_resources（循环 nextCursor 聚合）；否则返空（协议合规）。"""
    if caps is None or getattr(caps, "resources", None) is None:
        return []
    resources: list = []
    cursor = None
    while True:
        kw = {"cursor": cursor} if cursor else {}
        result = await asyncio.wait_for(session.list_resources(**kw), timeout=list_timeout)
        resources.extend(result.resources or [])
        cursor = getattr(result, "nextCursor", None)
        if not cursor:
            break
    return resources


async def _list_prompts_if_capable(session: Any, caps: Any, list_timeout: float) -> list:
    """caps.prompts 非空时分页 list_prompts；否则返空（协议合规）。"""
    if caps is None or getattr(caps, "prompts", None) is None:
        return []
    prompts: list = []
    cursor = None
    while True:
        kw = {"cursor": cursor} if cursor else {}
        result = await asyncio.wait_for(session.list_prompts(**kw), timeout=list_timeout)
        prompts.extend(result.prompts or [])
        cursor = getattr(result, "nextCursor", None)
        if not cursor:
            break
    return prompts


@asynccontextmanager
async def connect_server(
    name: str,
    config: Any,
    *,
    init_timeout: float = 30.0,
    list_timeout: float = 30.0,
    read_timeout: float = 60.0,
    roots_callback: Any = None,
    message_handler: Any = None,
) -> AsyncIterator[McpServerHandle]:
    """
    连接单个 MCP server，yield McpServerHandle（session + capabilities + tools）。

    全流程: open transport → ClientSession → initialize(超时) → 能力协商 → list_tools(超时)。
    session/transport 生命周期由本 cm 持有，调用方须在 `async with` 块内使用 handle.session。

    roots_callback/message_handler: 传给 ClientSession —— 非空即声明 roots capability +
      收 ServerNotification（list_changed 等）。elicitation_callback 故意不传（P5 跳过）。

    任何一步失败 → 抛异常（由 manager 捕获做故障隔离；R3 initialize 卡死靠 wait_for 超时）。
    """
    from mcp import ClientSession

    async with _open_transport(config) as (read, write):
        async with ClientSession(
            read, write,
            read_timeout_seconds=timedelta(seconds=read_timeout),
            list_roots_callback=roots_callback,
            message_handler=message_handler,
        ) as session:
            await asyncio.wait_for(session.initialize(), timeout=init_timeout)
            caps, tools = await _negotiate_and_list(session, list_timeout)
            resources = await _list_resources_if_capable(session, caps, list_timeout)
            prompts = await _list_prompts_if_capable(session, caps, list_timeout)
            handle = McpServerHandle(
                name=name, session=session, capabilities=caps,
                tools=tools, resources=resources, prompts=prompts,
            )
            logger.debug(
                "🔌 mcp server '%s' connected: tools=%d resources=%d prompts=%d",
                name, len(tools), len(resources), len(prompts),
            )
            yield handle
