#!/usr/bin/env python
"""Phase 2 测试夹具 MCP server：暴露 tools + resources + prompts。

供 test_mcp_primitives_e2e.py 连接验证 resources/prompts 消费端。
作为子进程由 McpManager 经 stdio_client 拉起。

用法（被测试以 subprocess 方式调用）:
    python tests/fixtures/primitives_mcp_server.py
"""

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("test-primitives")


@mcp.tool()
def echo(text: str) -> str:
    """回显输入文本。"""
    return f"echo: {text}"


@mcp.resource("config://app")
def app_config() -> str:
    """静态 resource：应用配置。"""
    return "key=value\nversion=1.0"


@mcp.prompt()
def review(code: str) -> str:
    """代码审查 prompt（参数：code）。"""
    return f"请审查以下代码:\n{code}"


@mcp.prompt()
def greet(name: str, lang: str = "zh") -> str:
    """问候 prompt。"""
    return f"hello {name} ({lang})"


if __name__ == "__main__":
    mcp.run(transport="stdio")
