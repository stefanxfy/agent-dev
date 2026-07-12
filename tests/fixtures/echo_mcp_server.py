#!/usr/bin/env python
"""最小 MCP echo server（agent_core/mcp E2E 测试夹具）。

stdio 模式，提供一个 echo tool：echo(text) -> "echo: <text>"。
作为子进程由 McpManager 经 stdio_client 拉起。

用法（被测试以 subprocess 方式调用）:
    python tests/fixtures/echo_mcp_server.py
"""

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("echo")


@mcp.tool()
def echo(text: str) -> str:
    """回显输入文本。"""
    return f"echo: {text}"


if __name__ == "__main__":
    mcp.run(transport="stdio")
