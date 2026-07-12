"""
MCP Client 子包 — 让 agent_core 消费外部 MCP server 暴露的工具。

决策摘要（hard constraint）:
  #1 client-only，不做 server 侧
  #2 官方 MCP Python SDK（pin >=1.28.1,<2.0）承担 JSON-RPC / transport / 握手
  #3 MVP transport: stdio + streamable-http
  #4 命名 mcp__<server>__<tool>（与 builtin 单池混用，前缀防冒充）
  #5 能力协商: initialize → get_server_capabilities → 按 capability 发请求
  #6 category="mcp"，走 PermissionEngine 默认 ASK；支持 server 级 deny strip
  #7 配置走 settings.json mcp.servers 段
  #8 组合根建连，会话级持有，dispose 挂 ReactAgent.close()
  #9 MCP handler 同步（调 manager 同步门面），base.py 零改动
  #10 McpManager 常驻 event loop 线程 + 同步门面（session 绑定单一 loop）

公开 API: McpManager（组合根构造，挂 agent._mcp_manager，close() 调 dispose）。

参考:
  - docs/mcp/openclaw-mcp-architecture.md（双向 / 生命周期治理）
  - docs/mcp/claude-code-mcp-implementation.md（消费侧深度 / 模板封装）
  - docs/mcp/openclaw-vs-claude-code-mcp.md（取舍对比）
"""

from agent_core.mcp.manager import McpManager
import logging

# MCP SDK 内部 DEBUG log 太啰嗦(每 30s health check 一条 tools/list 噪音),
# 统一压到 WARNING。需要 SDK DEBUG 时单独 setLevel(logging.DEBUG)。
for _name in (
    "mcp.client.streamable_http",
    "mcp.client.stdio",
    "mcp.client.sse",
    "mcp.shared.session",
):
    logging.getLogger(_name).setLevel(logging.WARNING)

__all__ = ["McpManager"]
