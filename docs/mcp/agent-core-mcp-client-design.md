# agent_core MCP Client

> 让 agent_core 作为 MCP **client** 消费外部 MCP server 暴露的工具，与 builtin 工具（calc/search/bash/read）并列进同一个 `ToolRegistry`，对 LLM 透明。
>
> 实现：`agent_core/mcp/` 子包（`config` / `names` / `client` / `materialize` / `manager`）。
> 决策与设计取舍见 plan 文件 + 本文；协议层参考本目录 `openclaw-mcp-architecture.md` / `claude-code-mcp-implementation.md` / `openclaw-vs-claude-code-mcp.md`。

## 快速开始

在 `~/.agent_data/settings.json`（或环境变量 `AGENT_SETTINGS_PATH` 指向的文件）加 `mcp.servers` 段：

```json
{
  "mcp": {
    "servers": {
      "filesystem": {
        "type": "stdio",
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"]
      },
      "remote": {
        "type": "http",
        "url": "https://example.com/mcp",
        "headers": { "Authorization": "Bearer xxx" }
      }
    }
  }
}
```

启动 `streamlit run web/app.py`，agent 启动时自动连接这些 server，工具以 `mcp__<server>__<tool>` 命名注入 LLM。切会话/关闭时自动断开（kill stdio 子进程）。

> 改配置需**重启会话**生效（MVP 不做运行期热重载）。

## 配置字段

| 字段 | stdio | http | 说明 |
|---|---|---|---|
| `type` | `"stdio"` | `"http"` / `"sse"` | `sse` 归一为 http；缺 `type` 时按 `command`/`url` 推断 |
| `command` / `args` / `env` / `cwd` | ✅（command 必填） | — | stdio 子进程 |
| `url` | — | ✅ 必填 | 远程 server |
| `headers` | — | ✅ | 鉴权塞这里（MVP 无 OAuth） |
| `timeout` | — | 可选 | http 连接超时（默认 30s） |
| `enabled` | 可选 | 可选 | `false` 跳过该 server |
| `connectTimeout` | 可选 | 可选 | 连接超时（默认 30s） |

> **stdio env 提示**：`env` 不填时 SDK 用白名单环境变量（`PATH`/`HOME` 等）。server 找不到命令时，显式传 `env`（尤其 `PATH`）。

## 架构

```
LLM tool_use(mcp__echo__echo)
  → tool_chain: PermissionCheck → ToolDispatch → ToolExecute
  → ToolRegistry.execute(name, input)               [同步, worker thread]
  → ToolDef.handler(**kwargs)                        [同步闭包]
  → manager.call_tool(server, tool, args)            [同步门面]
  → run_coroutine_threadsafe(...).result(timeout)    [提交到常驻 loop]
  → [常驻 mcp_loop 线程] session.call_tool → MCP server
  ← _normalize_call_result(CallToolResult) → str
```

**核心：McpManager 常驻 event loop 线程 + 同步门面。** MCP `ClientSession` 绑定创建它的 event loop，不能跨 loop 用；而组合根是同步上下文（Streamlit）。所以 `McpManager` 启动一个 daemon 线程跑 `loop.run_forever()`，所有 server 的连接活在这个 loop，对外暴露**同步** API（`connect_all` / `call_tool` / `dispose`），内部用 `run_coroutine_threadsafe(coro, loop).result(timeout)` 同步取值（超时由 `wait_for` 真 cancel 协程）。

这带来两个好处：① materialize 出的 handler 是纯同步（调 `manager.call_tool`），**`base.py` 零改动**；② 超时比 `asyncio.run` 软超时更硬（真 cancel，不泄漏 worker thread）。

### 子包结构

| 文件 | 职责 |
|---|---|
| `config.py` | `McpServerConfig` dataclass + 从 settings.json `mcp.servers` 解析（容错） |
| `names.py` | `mcp__server__tool` 命名安全化 + `is_server_denied`（server 级 deny 匹配） |
| `client.py` | 单 server async 连接（transport 选择 + initialize + 能力协商 + list_tools） |
| `materialize.py` | MCP `Tool` → 同步 `ToolDef` + `CallToolResult` 归一 + Unicode 消毒 |
| `manager.py` | `McpManager` 常驻 loop + 同步门面 + `connect_all` 故障隔离 + `call_tool` + `dispose` |

## 命名

`mcp__<server>__<tool>`（对齐 Claude Code）。server/tool 名清洗非法字符为 `_`（`my-server` → `my_server`）。前缀是**安全骨架**：builtin 和 MCP 工具单池混用，前缀保证 MCP 工具不能冒充 builtin（如 `Bash`）绕过 deny 规则。

## 权限

- MCP 工具 `category="mcp"`，走 PermissionEngine 默认 **ASK**（首次调用弹窗，用户 allow 后持久化）
- **server 级 deny strip**：`permissions.deny` 写 `"mcp__fs"` / `"mcp__fs*"` / `"mcp__fs(*)"` → 整个 `fs` server 的工具不注册（LLM 看不到）
- 单工具 deny：`"mcp__fs__read_file"` → 注册但 PermissionEngine 命中 DENY（走既有权限路径）

## 生命周期

1. 组合根 `web/app.py:get_agent()` 建立 `McpManager` → `connect_all` → 注册工具到 `registry`
2. manager 经 `st.session_state.mcp_manager` 单例化（防 Streamlit rerun 泄漏 loop 线程）
3. `agent._mcp_manager` 持引用
4. `ReactAgent.close()` 调 `manager.dispose()`（停 loop + 关所有连接 + kill stdio 子进程）；web 层 3 处销毁点（新建/切换/重建会话）全覆盖
5. `atexit` 兜底（进程被 kill / session_state 丢失时）

## 故障隔离

单 server 连接失败（命令不存在/超时/握手失败）只 `warn` 跳过，不阻断其他 server，不阻断 agent 启动。整个 MCP 注入段也包 try/except，失败降级为无 MCP。

## 调试

`AGENT_LOG_MCP=DEBUG` 开启 🔌 子 logger，`grep "🔌"` 还原 MCP 连接/协商/调用链路。

## 扩展原语（Phase 2：resources / prompts / roots / list_changed）

Phase 2 在 tools 之外补了 4 个原语/能力（elicitation 暂未做）。

### roots（client → server 声明可访问目录）
client 在 initialize 时声明 roots capability，server 据此知道 client 能访问哪些目录。
- 配置：`settings.json` 的 `mcp.roots`（list of `{uri, name}` 或字符串路径）；缺省用当前工作目录。
  ```json
  { "mcp": { "roots": [{"uri": "file:///tmp", "name": "tmp"}, "/home"] } }
  ```
- 声明后，filesystem 等 server 会用 roots（而非 args 的目录）。

### resources（client → server 拉取可读数据）
两个**全局工具**（多 server 共享，参数 `server` 路由，对齐 Claude Code）：
- `list_mcp_resources(server?)` — 列出 resources（每行 `[server] uri — desc`）
- `read_mcp_resource(server, uri)` — 读 resource 内容（text 直返，blob 占位）

### prompts（段注入 + 工具取内容，仿 skill）
- `McpPromptsHandler`（inputs_chain）把 server 的 prompt 名单注入 system prompt（`<available_mcp_prompts>`）
- `get_mcp_prompt(server, name, arguments)` 工具取渲染后内容（动态）

### list_changed（server → client 推通知，动态刷新）
server 声明 `listChanged` 能力并主动发 `tools/list_changed` 等通知时，client 的 message_handler
收到后：重新 list/materialize → `on_tools_changed` 回调（组合根：`unregister_by_prefix` + register 新 +
失效 `run_state.tool_schemas`）。下次 turn LLM 看到新工具集。

> 需 server 主动支持（FastMCP 默认不动态改 tool 列表，故需支持热更新的 server 才能触发）。

## 已知限制

- 无 OAuth 认证生态（PKCE / token 刷新 / needs-auth 缓存）—— 远程 server 用 `headers` 塞静态 token
- 仅 stdio + streamable-http transport（无 ws / sse-ide / sdk / claudeai-proxy）
- 无运行期**配置**热重载（改 server 配置需重启会话；但 server 主动发 list_changed 通知时会动态刷新工具/资源/提示）
- **elicitation 未做**（server→client 反向要用户输入；未来若做需加 `AWAITING_ELICITATION` phase + streamlit 表单 dialog，复用 permission 的 SM 暂停零件）
- sampling / logging callback / resource subscribe 细粒度更新未做
- MCP 工具不响应 `_cancel_event`（一次性 RPC，无法中途取消；靠超时兜底）
