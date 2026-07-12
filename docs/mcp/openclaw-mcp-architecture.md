# OpenClaw MCP 架构与设计解读

> 本文档基于源码逐行阅读写成。所有引用均使用 repo-root 相对路径（形如 `src/mcp/channel-bridge.ts:127`）。
> 涉及的核心/扩展边界，按 [AGENTS.md](../AGENTS.md) 与各 scoped `CLAUDE.md`/`AGENTS.md` 的规则区分：**SDK 公共接缝**（其他插件可依赖）vs **核心内部**（私有实现细节，仅供阅读理解）。
> MCP SDK 版本：`@modelcontextprotocol/sdk@1.29.0`（[package.json:1687](../package.json#L1687)）。

---

## TL;DR

OpenClaw 对 MCP（Model Context Protocol）的支持不是"加一个客户端"这么简单，而是一套**双向、多入口、跨运行时**的互操作能力。核心心智模型是：**OpenClaw 既能当 MCP server（被人连），也能当 MCP client（连别人）**，并且这套能力贯穿配置层、CLI、Gateway、嵌入式 agent runtime、以及各 provider/extension 适配器。

| 能力方向 | 角色 | 入口 | 关键文件 |
|---|---|---|---|
| 暴露 channel 会话为 MCP 对话 | **Server** (stdio) | `openclaw mcp serve` | `src/mcp/channel-server.ts`、`src/mcp/channel-bridge.ts` |
| 暴露 plugin/内置工具为 MCP 工具 | **Server** (stdio) | 独立可执行脚本 | `src/mcp/plugin-tools-serve.ts`、`src/mcp/tools-stdio-server.ts` |
| 把内部工具反向暴露给外部 client | **Server** (loopback HTTP) | 进程内惰性单例 | `src/gateway/mcp-http*.ts` |
| 连接外部 MCP server、物化进 agent | **Client** | embedded Pi / CLI runner | `src/agents/pi-bundle-mcp-*.ts`、`src/plugins/bundle-mcp.ts` |
| 统一 transport 抽象（stdio/http） | **Client** 底座 | runtime 内部 | `src/agents/mcp-transport.ts`、`src/agents/mcp-stdio-transport.ts` |
| 集中管理 MCP server 定义 | **配置面** | `openclaw mcp list/set/unset` | `src/config/mcp-config.ts`、`src/config/types.mcp.ts` |
| 对话内自助运维 MCP server | **配置面** | `/mcp` slash command | `src/auto-reply/reply/commands-mcp.ts` |
| 跨 provider 配置转译 | **适配器** | CLI runner | `src/agents/cli-runner/bundle-mcp-{claude,codex,gemini}.ts` |
| Extension 级 MCP 能力 | **适配器** | plugin 运行时 | `extensions/browser/.../chrome-mcp.ts`、`extensions/acpx/.../mcp-proxy.mjs` |

一句话总结：**MCP 在 OpenClaw 里是一组对称的 server/client 能力，沿"配置面 → 控制面 → 运行面"三层展开，用官方 SDK 承担协议语义，把跨 provider、跨传输、跨生命周期的差异收敛在各自的适配层里。**

---

## 一、整体架构

下图展示 OpenClaw 中 MCP 的全部子系统及其数据方向（→ 表示 MCP 调用方向）：

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                          外部世界（External）                                │
│                                                                             │
│   ① MCP client (Codex/Claude/脚本)      ② 外部 MCP server (uvx/远程/sse)    │
│        stdin/stdout JSON-RPC                  stdio / sse / streamable-http │
└──────────────────┬──────────────────────────────────────────┬──────────────┘
                   │ ① serve                                   │ ② 消费
                   ▼                                           ▲
┌─────────────────────────────────────────────────────────────────────────────┐
│                              OpenClaw core                                  │
│                                                                             │
│  ┌─────────────────────────── MCP Server 侧（被连）───────────────────────  │
│  │                                                                          │
│  │  [Channel MCP]              [Tools MCP]              [Loopback HTTP]    │
│  │  channel-server.ts          plugin-tools-serve.ts     gateway/mcp-http* │
│  │  channel-bridge.ts ──►Gateway    openclaw-tools-serve  (127.0.0.1 单例)  │
│  │  暴露会话/消息/审批/事件          暴露 plugin+cron 工具  反向暴露内部工具 │
│  │       stdio                          stdio                  HTTP        │
│  └────────────────────────────────────────────────────────────────────────  │
│                                                                             │
│  ┌─────────────────────────── MCP Client 侧（连人）───────────────────────  │
│  │                                                                          │
│  │  配置面:  config/mcp-config.ts  ◄── list/set/unset + /mcp slash         │
│  │            types.mcp.ts                                                  │
│  │                │                                                         │
│  │                ▼                                                         │
│  │  控制面:  plugins/bundle-mcp.ts  (读插件 .mcp.json + inline，安全合并)   │
│  │                │                                                         │
│  │                ▼                                                         │
│  │  运行面:  agents/mcp-transport*.ts (transport 工厂：stdio/http 统一)     │
│  │           agents/mcp-stdio-transport.ts (自研 StdioClientTransport)      │
│  │                │                                                         │
│  │                ▼                                                         │
│  │  运行面:  agents/pi-bundle-mcp-*.ts                                      │
│  │           runtime: connect→listAllTools→catalog→materialize→注入 LLM    │
│  │           names:   serverName__toolName 命名空间化 + 去重                 │
│  └────────────────────────────────────────────────────────────────────────  │
│                                                                             │
│  ┌─────────────────────────── 适配器层 ───────────────────────────────────  │
│  │  cli-runner/bundle-mcp-{claude,codex,gemini}.ts  转译成各家原生配置     │
│  │  extensions/browser/chrome-mcp.ts               消费 chrome-devtools-mcp │
│  │  extensions/acpx/mcp-proxy.mjs                  注入 mcpServers 给 ACP   │
│  └────────────────────────────────────────────────────────────────────────  │
└─────────────────────────────────────────────────────────────────────────────┘
```

三个关键观察：

1. **三条 server 入口，方向各不同**：Channel MCP 把"会话"暴露给外部 client；Tools MCP 把"工具"暴露给外部 client；Loopback HTTP 则是"自己当 server 给自己的 cron/codex 调"，仅绑回环地址。
2. **Client 侧是严格三段式分层**：配置解析（无副作用）→ transport 工厂（含子进程生命周期）→ SDK Client（连接/发现/调用/物化）。
3. **MCP 协议语义集中在 core + 官方 SDK**，extension 只做能力适配与生命周期管理，绝不重复造协议轮子。

---

## 二、模块分层总览

OpenClaw 把 MCP 拆成三层 + 一组横切适配器。层与层之间靠"纯数据契约"（类型、record）解耦，不靠继承体系。

| 层 | 职责 | 代表模块 | 是否含副作用 |
|---|---|---|---|
| **配置面** | MCP server 定义的持久化、归一化、读写 | `src/config/types.mcp.ts`、`src/config/mcp-config.ts`、`src/config/mcp-config-normalize.ts` | 仅文件 IO |
| **控制面** | 从插件 bundle 清单读取 + 合并 MCP 配置 | `src/plugins/bundle-mcp.ts` | 无连接 |
| **运行面（server）** | 把 OpenClaw 能力以 MCP 协议暴露 | `src/mcp/channel-*.ts`、`src/mcp/*-tools-serve.ts`、`src/gateway/mcp-http*.ts` | 进程/网络 |
| **运行面（client）** | 连外部 server、发现/物化工具、管理生命周期 | `src/agents/mcp-transport*.ts`、`src/agents/pi-bundle-mcp-*.ts` | 子进程/网络 |
| **适配器** | 跨 provider / extension 的格式转译与能力桥接 | `src/agents/cli-runner/bundle-mcp-*.ts`、`extensions/browser/...`、`extensions/acpx/...` | 视实现而定 |
| **入口/运维** | CLI 子命令 + 对话内 slash 命令 | `src/cli/mcp-cli.ts`、`src/auto-reply/reply/commands-mcp.ts` | 仅触发上层 |

贯穿全局的一条原则（来自 [AGENTS.md](../AGENTS.md) 的 Architecture 段）：**core 不绑死具体 extension**，extension 跨入 core 只能走 `openclaw/plugin-sdk/*` 或文档化的 barrel。MCP 的所有协议级实现都落在 core + 官方 SDK，extension 只做适配。

---

## 三、核心子系统详解

### 3.1 OpenClaw 作为 MCP Server —— Channel MCP（`src/mcp/channel-*.ts`）

这是 `openclaw mcp serve` 的实现，也是 OpenClaw "当 server" 最主要、最完整的入口。它把 OpenClaw 路由好的 **channel 会话**（Telegram/Discord/iMessage 等）封装成一组 MCP 工具与事件，供外部 MCP client（Codex、Claude Code、脚本）消费。

#### 模块构成

| 文件 | 行数 | 职责 |
|---|---|---|
| `src/mcp/channel-server.ts` | 110 | **组装入口**：创建 `McpServer`、装 `StdioServerTransport`、注册工具、绑定 shutdown |
| `src/mcp/channel-bridge.ts` | 560 | **核心桥接**：连 Gateway、维护事件队列、实现所有工具的业务逻辑 |
| `src/mcp/channel-tools.ts` | 189 | **工具注册**：把 8 个 MCP tool 注册到 `McpServer`，zod 校验参数 |
| `src/mcp/channel-shared.ts` | 223 | **共享类型与纯函数**：会话描述符、队列事件、过滤、审批类型 |

#### 组装流程（`channel-server.ts:28`）

`createOpenClawChannelMcpServer` 做了四件事：

1. 解析配置（`resolveMcpConfig`，缺省走 `getRuntimeConfig`）；
2. 按 `claudeChannelMode`（`off/on/auto`）算出 capabilities（`channel-tools.ts:12` 的 `getChannelMcpCapabilities`）——只有非 off 才声明 `experimental.claude/channel` 与 `claude/channel/permission` 两个实验性能力；
3. `new McpServer({name:"openclaw", version})`，并 `bridge.setServer(server)` 让 bridge 能反向推通知；
4. 注册 Claude 权限请求的 notification handler（`channel-server.ts:50`），再 `registerChannelMcpTools(server, bridge)` 挂上 8 个工具。

`serveOpenClawChannelMcp`（:73）是阻塞式生命周期函数：起 `StdioServerTransport`，挂 `stdin end/close` + `SIGINT/SIGTERM` 三路 shutdown 信号，`await server.connect(transport)` 后阻塞在 `closed` promise 上，直到 client 断开。**进程由 MCP client 拥有**——client 一断，bridge 退出。

#### 暴露的 8 个工具（`channel-tools.ts:24`）

| 工具 | 作用 | bridge 方法 |
|---|---|---|
| `conversations_list` | 列出可路由的 channel 会话 | `listConversations` → Gateway `sessions.list` |
| `conversation_get` | 按 session key 取单个会话 | `getConversation` → `sessions.describe` |
| `messages_read` | 读历史消息 | `readMessages` → `sessions.get` |
| `attachments_fetch` | 取某消息的非文本附件 | 复用 `readMessages` + `extractAttachmentsFromMessage` |
| `events_poll` | 按游标轮询队列事件 | `pollEvents`（纯内存） |
| `events_wait` | 长轮询等待下一事件 | `waitForEvent`（带超时的 promise） |
| `messages_send` | 经原路由回发消息 | `sendMessage` → Gateway `send`（带 `idempotencyKey`） |
| `permissions_list_open` / `permissions_respond` | exec/plugin 审批列表与决策 | `listPendingApprovals` / `respondToApproval` |

每个工具都返回双形态结果：`content`（给 LLM 看的文本摘要）+ `structuredContent`（给程序消费的结构化数据），见 `summarizeStructuredResult`（`channel-shared.ts:152`）。

#### 桥接核心：`OpenClawChannelBridge`（`channel-bridge.ts:42`）

Bridge 是整个 server 的"大脑"，内部状态由几个集合构成：

- `queue: QueueEvent[]` —— 内存事件队列，上限 1000（`QUEUE_LIMIT`，:40），FIFO 滑窗；
- `pendingWaiters: Set<PendingWaiter>` —— 等待 `events_wait` 的 promise 集合；
- `pendingClaudePermissions: Map` —— Claude 权限请求（短码 `yes/no <code>` 回复语义，正则 `channel-bridge.ts:39`）；
- `pendingApprovals: Map` —— exec/plugin 审批跟踪表。

**Gateway 连接生命周期**（`start()`，:83）是该模块最讲究的部分：

1. 惰性 `import` 五个 gateway 模块（`Promise.all`，:95），避免把 gateway 重依赖静态拉进 MCP 进程；
2. `resolveGatewayClientBootstrap` 解析 URL + 鉴权（token/password），申请 `READ/WRITE/APPROVALS` 三个 scope；
3. `new GatewayClient` 时挂四个回调：`onEvent`（事件入队）、`onHelloOk`（订阅 `sessions.subscribe` 并标 ready）、`onConnectError`（首连可重试则不 fail）、`onClose`（ready 前关闭才 reject）；
4. `startGatewayClientWhenEventLoopReady` 确保事件循环就绪才连，避免冷启动握手超时；
5. `await readyPromise`——ready 由 `handleHelloOk` 在订阅成功后 resolve。

**事件分发**（`handleGatewayEvent`，:422）把 5 类 gateway 事件映射成队列事件：`session.message` 走消息处理（含 Claude 权限短码匹配 + Claude channel 通知），`exec/plugin.approval.requested/resolved` 走审批跟踪。`enqueue`（:382）会同时唤醒所有匹配 filter 的 waiter——这是"一事件多订阅者"的实现。

**Claude channel 模式**（`shouldEmitClaudeChannel`，:533）只在 `mode != off && role == "user"` 时把用户消息以 `notifications/claude/channel` 推给 client，让 Claude Code 这类 client 能像收 channel 消息一样收到推送。这是 OpenClaw 对 Claude 私有扩展的定向适配，但不破坏标准 MCP（用 `experimental` capability 声明）。

#### 设计要点

- **server 与 bridge 解耦**：`McpServer`（协议）与 `OpenClawChannelBridge`（业务）分离，bridge 不直接依赖 SDK server 对象，靠 `setServer` 注入，便于测试时替换。
- **游标 + 内存队列**：live 事件不持久化，client 断开即丢失；老历史用 `messages_read` 补。这符合文档"live queue state starts when the bridge connects"的语义。
- **幂等发送**：`messages_send` 用 `randomUUID()` 作 `idempotencyKey`，防止 client 重试导致重复发送。
- **shutdown 三重保险**：stdin end、stdin close、SIGINT/SIGTERM，外加 transport onclose，确保 client 任何断开方式都能干净退出。

### 3.2 OpenClaw 作为 MCP Server —— Tools MCP（`src/mcp/*-tools-serve.ts`）

与 Channel MCP 暴露"会话"不同，这条入口暴露的是 **OpenClaw 自己的工具**——既包括插件注册的工具（如 `memory-lancedb` 的 recall/store/forget），也包括少量内置工具（如 `cron`）。它面向的场景是：ACP 会话里跑 Claude Code 时，让 Claude Code 能用上 OpenClaw 的插件工具。

#### 模块构成

| 文件 | 职责 |
|---|---|
| `src/mcp/tools-stdio-server.ts` | **通用底座**：`createToolsMcpServer` 把任意 `AnyAgentTool[]` 包成 MCP `Server`；`connectToolsMcpServerToStdio` 起stdio transport + shutdown |
| `src/mcp/plugin-tools-handlers.ts` | **handler 工厂**：`createPluginToolsMcpHandlers` 把 `AnyAgentTool` 适配成 MCP 的 `listTools`/`callTool` |
| `src/mcp/plugin-tools-serve.ts` | **plugin 工具入口**：解析插件工具策略、加载注册表、起 server |
| `src/mcp/openclaw-tools-serve.ts` | **内置工具入口**：目前仅暴露 `createCronTool()` |

#### 关键设计

1. **通用底座复用**（`tools-stdio-server.ts:9`）：`createToolsMcpServer({name, tools})` 是一个与业务无关的工厂——给它名字和工具数组，就产出一个标准 MCP server，挂 `ListToolsRequestSchema` 与 `CallToolRequestSchema` 两个 handler。两个 serve 脚本（plugin / openclaw）都只是"选工具 + 调工厂"，差异仅在工具来源。

2. **统一的前置 hook 边界**（`plugin-tools-handlers.ts:22`）：这是最关键的安全设计。`createPluginToolsMcpHandlers` 会：
   - 过滤掉 `ownerOnly` 工具（MCP 外部 client 不应触碰 owner 专属工具）；
   - 对每个工具 `wrapToolWithBeforeToolCallHook`（除非已包过），**强制让 MCP bridge 与 agent、HTTP 工具执行路径走同一套前置 hook 边界**。注释（:28）明确：ACPX MCP bridge 必须执行和 agent 一样的 pre-execution hook。

3. **工具策略解析**（`plugin-tools-serve.ts:26`）：`resolvePluginToolPolicy` 合并 `tools.profile`（工具档位）与 `tools.alsoAllow`，再叠加 sandbox policy，产出 `toolAllowlist/toolDenylist`，喂给 `ensureStandalonePluginToolRegistryLoaded` + `resolvePluginTools({suppressNameConflicts:true})`。

4. **stdout 纯净**（`tools-stdio-server.ts:26`、`plugin-tools-serve.ts:70`）：起 transport 前 `routeLogsToStderr()`，因为 MCP stdio 要求 stdout 只跑协议字节——连插件工具发现阶段的日志也得改道 stderr。

5. **JSON Schema 兜底**（`plugin-tools-handlers.ts:14`）：`resolveJsonSchemaForTool` 对没有合法 `parameters` 的工具退回 `{type:"object", properties:{}}`，保证 MCP client 永远拿到合法 schema。

#### 与 Channel MCP 的差异

| 维度 | Channel MCP | Tools MCP |
|---|---|---|
| 暴露对象 | 会话/消息/事件/审批（状态型） | 工具调用（无状态动作型） |
| server 类型 | 高层 `McpServer`（SDK 封装） | 底层 `Server`（SDK 原始类） |
| 业务承载 | `OpenClawChannelBridge`（重） | `createPluginToolsMcpHandlers`（轻） |
| 复用度 | 专用 | 工具来源可换（plugin/openclaw 共用底座） |

---

### 3.3 OpenClaw 作为 MCP Server —— Gateway Loopback HTTP（`src/gateway/mcp-http*.ts`）

这是三条 server 入口里最特殊的一条：它**不是给外部进程连的**，而是 OpenClaw **自己当 MCP server，给"同样是 OpenClaw 起的"外部 client**（如 cron 调度器、被 CLI runner 拉起的 codex）调用，让它们能用标准 MCP 协议访问 OpenClaw 内部工具。仅绑定 `127.0.0.1`，进程内惰性单例。

#### 模块分层（自底向上，极其规整）

| 文件 | 行 | 职责 |
|---|---|---|
| `mcp-http.protocol.ts` | 20 | **协议常量**：JSON-RPC 封装（`jsonRpcResult`/`jsonRpcError`）、协议版本号，零业务 |
| `mcp-http.schema.ts` | 83 | **schema 转换**：`buildMcpToolSchema` 内部 → MCP `inputSchema`，`flattenUnionSchema` 把 `anyOf/oneOf` 压平 |
| `mcp-http.runtime.ts` | 68 | **运行态缓存**：`McpLoopbackToolCache`（30s TTL）按 context 缓存工具解析 |
| `mcp-http.loopback-runtime.ts` | 46 | **进程单例状态**：持 port + 双 token，`createMcpLoopbackServerConfig` 产标准 MCP server 配置 |
| `mcp-http.request.ts` | 178 | **HTTP 守卫**：路径/方法/Origin/鉴权/body 限制 + 还原 `McpRequestContext` |
| `mcp-http.handlers.ts` | 105 | **JSON-RPC 路由**：`initialize` / `tools/list` / `tools/call` |
| `mcp-http.ts` | 245 | **主入口**：起 HTTP server、双 token、batch 处理、单例 ensure/close |

#### 请求处理数据流

```
外部 client POST /mcp  (Bearer token)
  → validateMcpLoopbackRequest        路径(仅/mcp)+方法(仅POST)+Origin(防CSRF)+双层token+1MB body
       产出 senderIsOwner             区分 owner / non-owner 调用方
  → readMcpHttpBody + JSON.parse      支持 batch（一请求多 message）
  → resolveMcpRequestContext          header → sessionKey/provider/account
  → McpLoopbackToolCache.resolve      按 context 取 scoped 工具集 (+ owner-only 策略)，30s TTL
  → handleMcpJsonRpc                  initialize | tools/list(→schema) | tools/call(→hook→execute)
  → 聚合 responses                    200(JSON-RPC) | 202(全通知)
```

#### 关键设计

1. **双 token 鉴权 + 同服务多权限**（`mcp-http.request.ts:63`）：owner token 与 non-owner token 分别表示"机主调用"与"受限调用"。`senderIsOwner` 下游驱动 `applyOwnerOnlyToolPolicy`（`runtime.ts:52`）——**同一个服务按调用方身份裁剪工具集**，而不是起多个服务。loopback-runtime 产出的配置里用 `${OPENCLAW_MCP_TOKEN}` 占位符，由调用方注入实际值。

2. **schema 联合压平**（`mcp-http.schema.ts`）：很多 MCP client 不支持 `anyOf/oneOf`，`flattenUnionSchema` 把联合类型压成单一 object，这是"适配现实中的弱 client"的典型妥协。

3. **分层缓存 + 短 TTL**：`McpLoopbackToolCache` 30s 过期，避免每请求都走完整 `resolveGatewayScopedTools`，又不会让工具集长期不一致。

4. **守卫前置、早返回**：request 层每道校验失败立即写 HTTP 状态码返回，handler 层只处理合法请求——职责边界清晰。

5. **惰性单例 + in-flight 去重**：`ensureMcpLoopbackServer` 配合 in-flight promise，避免并发触发时起多个端口。

#### 为什么需要 loopback？

外部 cron 调度器、被拉起的 codex CLI 等，需要以**标准 MCP client 身份**调用 OpenClaw 工具。loopback 让 OpenClaw 既是工具宿主义是 MCP 服务端，**无需为每个外部调用方单独实现协议适配**，且只绑回环地址保证不外暴。它和 §3.1/§3.2 的关系是"同一套底层工具、不同的传输与入口"。

### 3.4 OpenClaw 作为 MCP Client —— Transport 层（`src/agents/mcp-*.ts`）

这是 client 侧的底座。它把"用户配置里结构未知的某个 MCP server JSON"翻译成"一个统一形态的 MCP SDK `Transport` 实例 + 元数据"。**它只负责 transport 的构造与子进程生命周期，不负责连接、工具发现、调用**——那些在 §3.5 的 runtime 里由 SDK `Client` 完成。两层职责严格分离。

#### 模块构成

| 文件 | 行 | 职责 |
|---|---|---|
| `mcp-transport.ts` | 126 | **唯一对外入口** `resolveMcpTransport`：产 `ResolvedMcpTransport`（transport + 描述 + 类型 + 超时 + stderr 句柄） |
| `mcp-transport-config.ts` | 162 | **纯配置解析层**：产判别联合 `ResolvedMcpTransportConfig {kind:"stdio"\|"http"}`，无 SDK 依赖，可纯函数测 |
| `mcp-config-shared.ts` | 66 | **共享安全基座**：`isMcpConfigRecord`、`toMcpStringRecord`、`toMcpEnvRecord`（黑名单危险宿主 env）、`toMcpStringArray` |
| `mcp-stdio-transport.ts` | 160 | **自研 transport** `OpenClawStdioClientTransport`：spawn + 缓冲 + stderr + 进程树清理 |
| `mcp-stdio.ts` | 52 | stdio 启动配置解析与人类可读描述 |
| `mcp-http.ts` | 73 | http 配置解析：URL 协议校验、headers 归一化、敏感 URL 脱敏 |

#### 配置选择链（`mcp-transport-config.ts:108`）

按优先级而非字段直判，容错性强：

1. 有合法 `command` 且无 `url` → stdio（若有 `url` 同时存在则显式拒绝为 stdio，给精确 reason）；
2. 否则读 `transport` 字段，或经 `resolveOpenClawMcpTransportAlias`（`config/mcp-config-normalize.ts:17`）把旧别名 `type:"http"` 映射成 `streamable-http`；
3. 依次尝试 `streamable-http` → `sse`；都失败则 `logWarn` 并**跳过该 server**——不抛异常、不中断其余 server。

#### 自研 stdio transport 的生产化打磨（`mcp-stdio-transport.ts`）

OpenClaw 没用官方 `StdioClientTransport`，而是自研 `OpenClawStdioClientTransport`（:27），原因全在生产化诉求：

- **spawn**（`start()` :42）：`prepareOomScoreAdjustedSpawn`（Linux OOM 调分）、`detached:true`（非 win32，自成进程组便于整组清理）、`shell:false`、`stdio:["pipe","pipe","pipe"]`，合并 `getDefaultEnvironment()` 与用户 `env`；stderr 经 `PassThrough` 暴露给上层逐行 `logDebug`。
- **send**（:133）：序列化后写 stdin，用 write 回调 settle promise，处理背压（`drain`），捕获 EPIPE 避免 uncaughtException（注释 #75438）。
- **close**（:112）：`stdin.end()` → `Promise.race` 等 2s → 未死则 `killProcessTree(pid)` 再等 2s。`kill-tree.ts` 先 `SIGTERM(-pid)` 整组、宽限后 `SIGKILL`，并特判 `detached:false` 避免误杀 gateway 自身进程组（注释 #71662）。

#### 设计理念

- **统一抽象靠返回值，不靠继承**：三种 transport 不共享父类，统一于 `ResolvedMcpTransport` 这个扁平 record，上层只面向 SDK `Transport` 接口编程。
- **解析与构造分离**：`*-config.ts` 只产数据（可纯函数测），`mcp-transport.ts` 才 new 真实 `Transport`。
- **故障隔离**：单 server 失败只 warn 跳过，不影响其余 server 与 agent 主循环。

---

### 3.5 OpenClaw 作为 MCP Client —— Bundle MCP Runtime（`src/plugins/bundle-mcp.ts` + `src/agents/pi-bundle-mcp-*.ts`）

这是 client 侧的"业务大脑"。它把外部/插件 MCP server 的配置**连接、列举、物化成 agent 内部工具**，注入 LLM 的 tool 列表。方向与 §3.3 loopback 相反：OpenClaw 当 client 消费外部 server。

#### 模块分层（控制面/运行面严格二分）

| 文件 | 层 | 职责 |
|---|---|---|
| `src/plugins/bundle-mcp.ts` | **控制面** | 读插件 bundle 清单（claude/codex/cursor 三格式）的 `.mcp.json` + inline 配置，占位符展开、路径绝对化、安全文件读取、`applyMergePatch` 合并，产出纯净 `mcpServers` map |
| `pi-bundle-mcp-types.ts` | 类型契约 | `SessionMcpRuntime`、`SessionMcpRuntimeManager`、`McpToolCatalog` |
| `pi-bundle-mcp-names.ts` | **命名安全化** | server 名/工具名清洗、长度截断（server≤30, 总≤64）、冲突自动加后缀（`-2/-3`）、保留名集合 |
| `pi-bundle-mcp-runtime.ts` | **核心运行态** | `createSessionMcpRuntime` 建 client+transport、`connectWithTimeout`、分页 `listAllTools`、catalog 构建、`callTool` 透传；manager 做单例+租约+空闲回收+指纹热重载 |
| `pi-bundle-mcp-materialize.ts` | **物化层** | catalog 工具 → `AnyAgentTool[]`，绑定 `execute` 到 `callTool`，注册 plugin tool meta |
| `pi-bundle-mcp-tools.ts` | barrel | re-export |

#### "materialize（物化）"具体做什么（`materialize.ts:64`）

1. `acquireLease` 取租约 + `getCatalog` 拉目录；
2. 对 catalog 工具**按 server 名稳定排序**后逐个：`buildSafeToolName` 清洗/截断/去重，加入保留名集合防后续冲突；
3. 为每个工具构造 `AnyAgentTool`，其 `execute` 闭包调用 `runtime.callTool(serverName, toolName, input)`，`toAgentToolResult` 把 MCP `CallToolResult` 归一为 agent 结果；
4. `setPluginToolMeta` 标记来源 `bundle-mcp`；
5. 按名字稳定排序，返回 `{tools, dispose}`。

#### 运行态生命周期（`pi-bundle-mcp-runtime.ts`）

- **连接**：`new Client` → `connectWithTimeout`（:95，把 `client.connect` 包成超时 promise，默认 30s）→ `listAllTools`（:122，翻页聚合 `nextCursor`）→ catalog 缓存。
- **调用**：`callTool`（:351）经 `sessions.get(serverName)` 复用已建连 client。
- **释放**：`disposeSession`（:133）顺序：detach stderr → streamable-http 专做 `terminateSession()`（:136）→ `transport.close()` → `client.close()`。
- **manager**：单例 + 租约（`acquireLease`/`lastUsedAt`）+ 定时 `sweepIdleRuntimes`（默认 `mcp.sessionIdleTtlMs` 10min，:174）+ `configFingerprint`（sha1）变更即 dispose 重建（:483）+ in-flight map 防并发重复创建（:497）。

#### 两套入口 API（创建/运行二分）

- `createBundleMcpToolRuntime`：**一次性临时 runtime**，服务 compact 等单次场景（`compact.ts:709`），用完即销。
- `getOrCreateSessionMcpRuntime` + `materializeBundleMcpToolsForRun`：**复用会话 runtime**，服务长会话（`attempt.ts:1027`）。

#### 设计理念

- **控制面/运行面分离**：`plugins/bundle-mcp.ts`（纯配置，无连接）与 `agents/pi-bundle-mcp-*`（连接执行）严格分层，符合 [src/plugins/CLAUDE.md](../src/plugins/CLAUDE.md) 的 control-plane/runtime-plane 边界。
- **租约 + 空闲回收**：避免会话结束残留长连外部进程（这正是 `openclaw mcp` 文档"one-shot runs retire bundled MCP runtimes"的落点）。
- **配置指纹热重载**：`configFingerprint` 变化即重建，支持运行中插件配置变更。
- **命名命名空间化**：`serverName__toolName`（分隔符 `__`）+ 多级去重，保证跨多 server 工具名对 LLM provider 安全无冲突。
- **适配器模式**：`materialize` + `toAgentToolResult` 把异构 MCP 结果统一为 `AgentToolResult`，对 agent core 完全透明。

### 3.6 Extension 级 MCP 适配（acpx / browser / cli-runner）

这部分体现 OpenClaw 的边界哲学：**协议语义集中在 core + 官方 SDK，extension 只做能力适配与生命周期管理**。三个 extension 各代表一种典型集成姿态。

#### ① acpx 的 MCP Proxy —— 透明 stdio 中间人（`extensions/acpx/src/runtime-internals/mcp-proxy.mjs`）

acpx proxy **不是协议转换器**，而是一个夹在 OpenClaw 与外部 ACP（Agent Client Protocol）agent 进程之间的**透明 stdio 中间人**。唯一目的：在 ACP 会话引导请求（`session/new`/`load`/`fork`）的 JSON-RPC `params` 里**注入 `mcpServers` 配置**，把 OpenClaw 已配的 MCP server 列表传给被代理的 agent，而无需 agent 自身支持 MCP 配置。

| 文件 | 作用 |
|---|---|
| `mcp-proxy.mjs` | 可执行 Node 脚本，`--payload` 收 base64url 的 `{targetCommand, mcpServers}`，`spawn` 子进程，逐行 readline 父 stdin，命中 session 引导方法则改写注入，其余原样透传 |
| `mcp-command-line.mjs` | 纯命令行解析器 `splitCommandLine`，处理引号/转义 + 特化 Windows 路径（`.exe` 直执，`.bat/.cmd/.ps1` 须经 shell） |

**架构关系**：`mcp-proxy.mjs` 是**零依赖、零跨入**的独立 Node 脚本——既不 import `openclaw/plugin-sdk`，也不引用 MCP SDK。它与 core 的 `src/mcp/` 完全无关：core 侧实现真正的 MCP server/client 语义，acpx proxy 只做 JSON-RPC 行级改写。MCP server 由 OpenClaw 通过 `session/new` params 传给 agent，由 agent 自己拉起。符合 `extensions/acpx/CLAUDE.md` 的"薄包装、可复用逻辑放 `openclaw/acpx`"边界。

**设计理念**：极简进程级隔离——用纯 stdio 代理规避协议耦合，base64url payload 避开 argv 转义污染，把跨平台命令解析风险收敛到一个独立可单测的纯函数。

#### ② browser 的 chrome-mcp —— 消费型 client 会话（`extensions/browser/src/browser/chrome-mcp.ts`）

browser 扩展把 Chrome 浏览器能力封装成一个**长期持有、可复用的 MCP client 会话**。它以官方 SDK 的 `Client` + `StdioClientTransport` 连到子进程 `chrome-devtools-mcp`，把该 server 的工具（`list_pages`/`new_page`/`take_snapshot`/`click`/`fill`/`evaluate_script` 等）封装成 OpenClaw 浏览器抽象（`BrowserTab`/`navigate`/`screenshot`）。

| 文件 | 作用 |
|---|---|
| `chrome-mcp.ts`（~1170 行） | 核心 runtime。`sessions`/`pendingSessions` 双 Map 做**按 profile 归一化缓存**；`leaseSession` 区分常驻/临时会话；`callTool` 实现超时/abort/连接错误后的**会话驱逐+重连**，特判 `list_pages` 的 stale page 错误重连一次 |
| `chrome-mcp.snapshot.ts` | **snapshot 机制**：把 `take_snapshot` 的结构化无障碍树转成 AI 友好的 aria snapshot 文本（缩进渲染、role 过滤、去重 nth、maxChars 截断），供工具 ref 寻址 |
| `chrome-mcp.runtime.ts` | **runtime 边界闸门**（仅 5 行）：`getChromeMcpModule()` 动态 `import`，按需加载，避免重 runtime 进热路径 |

**架构关系**：`chrome-mcp.ts` 通过 `openclaw/plugin-sdk/text-runtime` 跨入 core（符合 `extensions/CLAUDE.md` 边界）。它把 MCP SDK `Client` 当**消费方**（client 角色）——真正的浏览器 MCP server 是 `chrome-devtools-mcp` 子进程。browser 扩展是"MCP 工具 → OpenClaw 浏览器抽象"的适配层，对 core 隐藏 MCP 协议细节。

**设计理念**：会话生命周期与 profile 绑定（切 profile 即驱逐旧会话）；连接健壮性优先（handshake 超时、stderr 限额、transport-identity 防并发驱逐竞态）；snapshot 是把无障碍树压成 LLM 可读且可引用（ref）的精简表示，控 token 成本。

#### ③ cli-runner 的 bundle-mcp provider 适配器（`src/agents/cli-runner/bundle-mcp-*.ts`）

当 OpenClaw 以 stdio 子进程方式拉起外部 CLI agent（claude/codex/gemini）时，需把 bundle 的 MCP server 配置**转译成各家 CLI 原生格式**——因为三家接受 MCP 配置的机制完全不同。

| 文件 | 转译目标 |
|---|---|
| `bundle-mcp.ts` | **编排器** `prepareCliBundleMcpConfig`：合并外部 `--mcp-config` + bundle + additional（`applyMergePatch`），按 mode 分派三种产物，另算 `mcpResumeHash`（对 loopback URL 端口归一化，支持 resume 判等） |
| `bundle-mcp-adapter-shared.ts` | **共性抽取**：`applyCommonServerConfig`（统一映射 command/args/env/cwd/url）、`decodeHeaderEnvPlaceholder`（解析 `Bearer ${VAR}` 占位符，三家都需要） |
| `bundle-mcp-claude.ts` | **argv 操作**：剥离既有 `--mcp-config` 后追加临时 `mcp.json` + `--strict-mcp-config` |
| `bundle-mcp-codex.ts` | **TOML inline**：headers 拆成 `http_headers`/`env_http_headers`/`bearer_token_env_var` 三态；对 openclaw loopback 强制 `default_tools_approval_mode="approve"` |
| `bundle-mcp-gemini.ts` | **JSON settings + env**：保留 `type`/`trust`，把 env 占位符就地解析（gemini 不支持环境引用语法），写 `GEMINI_CLI_SYSTEM_SETTINGS_PATH` 临时文件 |

**架构关系**：位于 `src/agents/cli-runner/`（core 内），消费 `src/plugins/bundle-mcp.ts` 的类型与 `src/agents/bundle-mcp-config.ts` 的合并逻辑。这是 core **主动适配各 CLI** 的位置——provider 差异由 adapter 吸收，不存在 core 反向依赖具体 extension 的耦合。

**设计理念**：配置形态多态（JSON 文件 / TOML inline / JSON settings+env）是各家 CLI 的不可控现实；`adapter-shared` 锁住"通用字段映射 + 占位符解码"两个不变量，让三家 adapter 只处理各自的 schema 投影与传输机制。注意 `bundle-mcp-codex.ts` 对 openclaw loopback 强制 `approve` 模式——这与 §3.3 的双 token 鉴权配合，确保内部工具被外部 CLI 调用时审批语义一致。

---

### 3.7 配置层（`src/config/*mcp*.ts`）

配置层是所有 MCP 能力的"单一事实源"。`openclaw mcp list/set/unset` 与 `/mcp` slash 命令、embedded Pi、CLI runner 全部落到这里读写 server 定义。**它只读写配置文件，不连接任何 server**。

| 文件 | 行 | 职责 |
|---|---|---|
| `types.mcp.ts` | 32 | **类型契约** `McpServerConfig`（command/args/env/cwd + url/transport/headers/超时）与 `McpConfig`（servers map + `sessionIdleTtlMs`） |
| `mcp-config.ts` | 155 | **CRUD**：`listConfiguredMcpServers` / `setConfiguredMcpServer` / `unsetConfiguredMcpServer`，走 `readSourceConfigSnapshot` + `validateConfigObjectWithPlugins` + `replaceConfigFile`（乐观锁 `baseHash`） |
| `mcp-config-normalize.ts` | 51 | **归一化**：`canonicalizeConfiguredMcpServer` 把 CLI 原生 `type:"http"` 别名映射成 `transport:"streamable-http"`；`normalizeConfiguredMcpServers` 过滤非法 record |

#### 关键设计

1. **配置契约三态对齐**（[AGENTS.md](../AGENTS.md) "Config contract"）：导出类型、schema/help、metadata、baselines、docs 必须对齐。`McpServerConfig` 是这个契约的源头，下游所有 transport 解析都以此为输入。
2. **乐观锁写**（`mcp-config.ts:86`）：`replaceConfigFile({nextConfig, baseHash})` 用读时的 hash 防止并发写覆盖——两个进程同时 `set` 不会互相踩。
3. **别名归一化**（`mcp-config-normalize.ts:17`）：`resolveOpenClawMcpTransportAlias` 把历史 `type` 字段（`http`/`sse`/`stdio`/`streamable-http`）统一映射到 canonical `transport`，保证配置面只存规范形态。
4. **写入即校验**：每次 `set`/`unset` 都过 `validateConfigObjectWithPlugins`，写坏的配置当场拒绝，不落盘。
5. **transport 归一化的双向性**：embedded Pi 直接消费 `transport` 值，而 Claude Code/Gemini 经 cli-runner adapter 还原成 CLI 原生 `type`（`http`/`sse`/`stdio`）——配置面存规范形态，适配器负责按消费方"方言"还原。

---

## 四、关键数据流（端到端）

§三 是纵向逐子系统，本节横向串起三条最能体现架构的端到端流程。

### 4.1 `openclaw mcp serve` —— OpenClaw 当 server 暴露会话

```
外部 MCP client (Codex/Claude Code)
   │  spawn `openclaw mcp serve --gateway-url ... --gateway-token ...`
   ▼
src/cli/mcp-cli.ts:31  registerMcpCli → serve
   │  resolveGatewayAuthOptions (token/password + *-file 变体)
   │  校验 --claude-channel-mode {auto,on,off}
   ▼
src/mcp/channel-server.ts:73  serveOpenClawChannelMcp
   │  createOpenClawChannelMcpServer → McpServer + Bridge
   │  new StdioServerTransport → server.connect(transport)
   ▼
src/mcp/channel-bridge.ts:83  bridge.start()
   │  惰性 import 5 个 gateway 模块
   │  resolveGatewayClientBootstrap (url + auth)
   │  new GatewayClient (READ+WRITE+APPROVALS scope)
   │  startGatewayClientWhenEventLoopReady
   ▼
Gateway onHelloOk → sessions.subscribe → bridge ready
   │
   │  client 调 conversations_list ─► Gateway sessions.list ─► toConversation 过滤
   │  client 调 messages_read     ─► Gateway sessions.get
   │  client 调 events_wait       ─► 内存队列 + pendingWaiters
   │  Gateway 推 session.message  ─► enqueue ─► 唤醒 waiter (+可选 Claude channel 通知)
   │  client 调 messages_send     ─► Gateway send (idempotencyKey)
   │  Gateway 推 exec.approval    ─► trackApproval + enqueue
   │  client 调 permissions_respond ─► Gateway exec/plugin.approval.resolve
   ▼
client 断开 (stdin end/close | SIGINT | transport onclose)
   │  shutdown → bridge.close (清 waiter/stop gateway) → server.close
```

### 4.2 embedded Pi 消费外部 MCP server —— OpenClaw 当 client 物化工具

```
用户配置 mcp.servers (config/mcp-config.ts)
   │
   ▼
plugins/bundle-mcp.ts  loadMergedBundleMcpConfig
   │  读插件 .mcp.json + inline，${CLAUDE_PLUGIN_ROOT} 展开，路径绝对化
   │  安全文件读取 (边界校验 + 拒硬链接)，applyMergePatch 合并
   │  → mcpServers map
   ▼
pi-bundle-mcp-runtime.ts:187  loadSessionMcpConfig
   │  configFingerprint = sha1(配置)   ← 热重载判据
   ▼
createSessionMcpRuntime (manager 单例 + lease)
   │  逐 server:
   │    mcp-transport-config.ts → ResolvedMcpTransportConfig (stdio|http, 纯数据)
   │    mcp-transport.ts        → resolveMcpTransport (transport 实例)
   │       stdio → OpenClawStdioClientTransport (spawn + OOM + detached)
   │       http  → SDK StreamableHTTP/SSE transport
   │    new Client + connectWithTimeout (30s)
   │    listAllTools (翻页聚合) → sanitizeServerName 去重
   │  → McpToolCatalog {servers, tools}
   ▼
pi-bundle-mcp-materialize.ts:64  materializeBundleMcpToolsForRun
   │  按 server 名稳定排序 → buildSafeToolName (清洗/截断/去重/保留名)
   │  每工具 → AnyAgentTool { execute: () => runtime.callTool(...) }
   │  toAgentToolResult 归一 + setPluginToolMeta("bundle-mcp")
   │  → {tools, dispose}
   ▼
注入 agent run 的 tool 列表 → 暴露给 LLM
   │
   │  LLM 选工具 → tool.execute → runtime.callTool(serverName, toolName, input)
   │                                     → Client.callTool → 外部 server
   ▼
会话结束 / 空闲 TTL (10min) / 指纹变更
   │  sweepIdleRuntimes → disposeSession
   │    detach stderr → (streamable: terminateSession) → transport.close → client.close
   │    stdio: killProcessTree (SIGTERM→SIGKILL 整组)
```

### 4.3 loopback 反向调用 —— OpenClaw 当 server 给自己的 client

```
cron 调度器 / 被 CLI runner 拉起的 codex（以标准 MCP client 身份）
   │  读 createMcpLoopbackServerConfig 产出的 {url, headers: ${OPENCLAW_MCP_TOKEN}}
   │  POST http://127.0.0.1:<port>/mcp  (Bearer <owner|non-owner token>)
   ▼
gateway/mcp-http.ts:219  ensureMcpLoopbackServer (惰性单例 + in-flight 去重)
   ▼
mcp-http.request.ts:63  validateMcpLoopbackRequest
   │  路径(仅/mcp) + 方法(仅POST) + Origin(防CSRF) + 双层token + 1MB body
   │  → senderIsOwner
   ▼
resolveMcpRequestContext (header → session/provider/account)
   ▼
mcp-http.runtime.ts:24  McpLoopbackToolCache.resolve
   │  按 context 取/算 scoped 工具集 (applyOwnerOnlyToolPolicy 按 senderIsOwner 裁剪)
   │  30s TTL
   ▼
mcp-http.handlers.ts:35  handleMcpJsonRpc
   │  initialize → 协议版本协商
   │  tools/list → buildMcpToolSchema (flattenUnionSchema 压平联合)
   │  tools/call → wrapToolWithBeforeToolCallHook → tool.execute
   ▼
200 (JSON-RPC) | 202 (全通知 batch)
```

三条流程共同验证了架构的一个核心主张：**同一套底层工具与会话能力，通过不同的 transport、不同的入口、不同的鉴权策略，同时支撑"对外暴露"与"对外消费"两个方向，而协议语义只在 core + 官方 SDK 里实现一次。**

---

## 五、设计理念与模式

跨子系统提炼出的横切设计原则。

### 5.1 双向对称的心智模型

OpenClaw 全篇把 MCP 当成"既能当 server 又能当 client"的对称能力来设计：同一个 `mcp` 父命令下，`serve` 是 server、`list/set/unset` 是 client 注册表；同一套内部工具，既经 stdio（Channel/Tools MCP）暴露，又经 HTTP（loopback）暴露，又经 client 侧物化注入。这种对称性让"OpenClaw 既是工具宿主也是工具消费者"成为一等公民，而不是事后补丁。

### 5.2 三段式分层：配置面 → 控制面 → 运行面

client 侧严格执行：

- **配置面**（`config/*mcp*`）：纯数据持久化，无连接，乐观锁写。
- **控制面**（`plugins/bundle-mcp`）：纯配置解析与合并，无副作用。
- **运行面**（`mcp-transport*` + `pi-bundle-mcp-*`）：连接、发现、调用、生命周期。

每层只面向"纯数据契约"对接下层，不靠继承。这是 [AGENTS.md](../AGENTS.md) "manifest-first control plane; targeted runtime loaders" 原则在 MCP 上的落地。

### 5.3 协议语义集中、差异收敛到适配层

MCP 协议语义（JSON-RPC、initialize、tools/list、tools/call、stdio/HTTP transport）只在 core + `@modelcontextprotocol/sdk` 里实现一次。所有"现实差异"——跨 provider（claude/codex/gemini 的配置格式）、跨 client（弱 schema 支持、联合类型不识别）、跨平台（Windows .bat 须 shell、Linux OOM）——都被收敛到薄薄的适配层（cli-runner adapter、schema flatten、mcp-command-line）。core 因此保持干净，第三方插件也无需重造协议轮子。

### 5.4 统一的安全边界：before-tool-call hook 三路合一

`createPluginToolsMcpHandlers`（`plugin-tools-handlers.ts:22`）和 loopback handler 都强制 `wrapToolWithBeforeToolCallHook`，**让 MCP server 路径与 agent 路径、HTTP 工具执行路径走完全相同的前置 hook 边界**。这意味着无论工具从哪条入口被调用，权限检查、审计、拦截策略都一致——不存在"MCP 绕过 hook"的特权通道。配合 loopback 的双 token（owner/non-owner）按调用方裁剪工具集，安全模型是统一的。

### 5.5 命名空间化与去重

跨多 MCP server 的工具名极易冲突。`pi-bundle-mcp-names.ts` 用 `serverName__toolName` 命名空间 + 多级去重（server 级清洗、tool 级截断、保留名集合、冲突自动加后缀 `-2/-3`），保证暴露给 LLM 的工具名对任何 provider 都安全、稳定、可复现。

### 5.6 生命周期可控：租约、空闲回收、进程树清理

外部 MCP server 往往是长连子进程，管理不当会泄漏。OpenClaw 用四道防线：

- **租约 + lastUsedAt**：runtime 按使用续命；
- **空闲 TTL**（`sessionIdleTtlMs`，默认 10min）：定时 `sweepIdleRuntimes` 回收；
- **配置指纹热重载**：`configFingerprint` 变即 dispose 重建；
- **进程树清理**：自研 `OpenClawStdioClientTransport.close` → `killProcessTree`（SIGTERM 整组 → SIGKILL），并特判避免误杀 gateway 自身进程组。

一次性入口（compact、one-shot agent）额外在 reply 完成时主动 retire runtime，避免脚本式重复调用累积子进程。

### 5.7 单例惰性初始化 + 故障隔离

loopback server、bundle runtime manager 都是进程级惰性单例 + in-flight 去重，避免并发触发产生多份。同时，单个 MCP server 解析/连接失败只 `logWarn` 跳过，不中断其余 server 与 agent 主循环——**故障被隔离在最小单元**。

### 5.8 设计模式速查

| 模式 | 落点 | 作用 |
|---|---|---|
| 适配器 | cli-runner `bundle-mcp-{claude,codex,gemini}`、browser chrome-mcp | 异构格式/能力 → 统一内部模型 |
| 工厂 | `createToolsMcpServer`、`resolveMcpTransport` | 按输入产统一产物，屏蔽构造细节 |
| 惰性单例 | `ensureMcpLoopbackServer`、bundle runtime manager | 进程唯一 + 防并发重复创建 |
| 租约/引用计数 | `acquireLease`、`lastUsedAt` + sweep | 长资源按需回收 |
| 中间人/代理 | acpx `mcp-proxy.mjs` | 不改协议、注入配置 |
| 观察者 | `pendingWaiters` + `enqueue` | 一事件多订阅者唤醒 |
| schema 驱动 | `buildMcpToolSchema`、zod 校验 | 内部模型 ↔ 协议要求隔离 |
| 控制面/运行面分离 | `plugins/bundle-mcp` vs `agents/pi-bundle-mcp-*` | 配置无副作用，执行隔离 |
| 判别联合 | `ResolvedMcpTransportConfig {kind}` | 替代继承的多态 |

---

## 六、入口与运维：CLI 与对话内 slash 命令

配置面之上还有两个"触发入口"，分别面向命令行和对话内用户。

### CLI（`src/cli/mcp-cli.ts`）

`openclaw mcp` 子命令族覆盖两个语义，对应 server/client 双角色：

| 子命令 | 角色 | 行为 |
|---|---|---|
| `serve` | **Server** | 解析 gateway 鉴权（`resolveGatewayAuthOptions`，支持 token/password 及 `*-file` 变体适配容器/CI），校验 `--claude-channel-mode`，阻塞调 `serveOpenClawChannelMcp`（§3.1） |
| `list` / `show [name]` | 配置只读 | 读 `listConfiguredMcpServers()`，支持 `--json`；`show` 无参打印全部 |
| `set <name> <json>` | 配置写 | `parseConfigValue` 把第二参数当 JSON 解析（如 `{"command":"uvx","args":["context7-mcp"]}`），写回 `setConfiguredMcpServer` |
| `unset <name>` | 配置写 | 按名删除，不存在则失败 |

设计上 `serve` 与管理命令共用一个 `mcp` 父命令，体现"既是 client 又是 server"的对称心智；所有错误走 `fail()` 统一 `exit(1)`。

### 对话内 slash 命令（`src/auto-reply/reply/commands-mcp.ts` + `mcp-commands.ts`）

`/mcp` 是 **agent 自动回复循环里给人/会话用的 slash command，不是给 LLM 的 tool**。LLM 调 MCP 工具走 §3.4/§3.5 的 transport + bundle 自动注入；`/mcp` 是用户在会话里查看/增删 server 配置的快捷入口，命中即拦截回复、不进 LLM。

- **解析**（`mcp-commands.ts:9`）：`parseMcpCommand` 复用通用 `parseStandardSetUnsetSlashCommand`，产 `McpCommand = show | set | unset | error`（`show`/`get` 同义）。
- **处理**（`commands-mcp.ts:20`）：`CommandHandler` 返回 `{shouldContinue:false, reply}` 直接终止本轮。
- **权限分层**（核心特色）：`rejectUnauthorizedCommand` → 非 owner 拒绝（但 `show` 在 internal channel 下放行只读）→ `requireCommandFlagEnabled(cfg,"mcp")` 校验开关 → 写操作额外 `requireGatewayClientScopeForInternalChannel(..., ["operator.admin"])`。
- **数据层**：同样落 `config/mcp-config.ts` 的 CRUD，输出用 `renderJsonBlock` 包 markdown code fence。

**设计理念**：配置即能力——让用户对话内自助运维 MCP server，但用多层 gate 把写权限收紧到 owner / operator.admin；与 LLM tool 通路正交，避免 LLM 自行篡改自身工具集。注意 `/mcp` 全程不触碰 transport、不建连，仅改配置；新配置在**下次** agent 会话/连接时生效。

---

## 七、使用者指南：安装一个 MCP 与发现机制

> 前面六章面向源码与架构，本节面向 **OpenClaw 使用者**：不读源码也能看懂"怎么装一个 MCP、装完 OpenClaw 怎么找到它"。

### 7.1 先分清方向：你装的 MCP 是哪一种？

OpenClaw 跟 MCP 相关的能力有两个方向，新手最容易装错方向：

| 方向 | 命令 | 含义 |
|---|---|---|
| OpenClaw **当 server** | `openclaw mcp serve` | 把 OpenClaw 的会话/工具暴露给**别的** MCP 客户端（Codex / Claude Code）用 |
| OpenClaw **当 client**（消费方） | `openclaw mcp set / list / unset` | 登记 OpenClaw agent **要调用**的外部 MCP server，让 agent 多一批工具 |

> **"给 OpenClaw 装一个 MCP" = 第二种**：登记一条 server 定义，让你的 agent 能用它的工具。本节只讲这一种。（第一种 `serve` 见 §3.1 / §4.1。）

### 7.2 核心认知：装 MCP ≠ 装包，而是登记一条"怎么连"的定义

在 OpenClaw 里装 MCP **不需要** `npm install` 之类的步骤，而是**在配置里登记一条 server 定义**（告诉 OpenClaw 怎么连它）。所有定义的单一事实源是配置文件：

```
~/.openclaw/openclaw.json        默认路径（src/config/paths.ts:144）
（可用环境变量 OPENCLAW_CONFIG_PATH 或 OPENCLAW_HOME 覆盖位置）
```

登记落到该文件的 `mcp.servers` 字段，类型契约见 §3.7 的 `McpServerConfig`。

### 7.3 三种登记方式

**方式 1：CLI（推荐）**

```bash
# stdio 类型：本地命令起子进程（最常见，如 context7、filesystem）
openclaw mcp set context7 '{"command":"uvx","args":["context7-mcp"]}'
openclaw mcp set fs '{"command":"npx","args":["-y","@modelcontextprotocol/server-filesystem","/path/to/allowed/dir"]}'

# http 类型：连远程托管服务
openclaw mcp set my-remote '{"url":"https://example.com/mcp","transport":"streamable-http","headers":{"Authorization":"Bearer xxx"}}'

openclaw mcp list                  # 查看（只读）
openclaw mcp show context7         # 看单个
openclaw mcp unset context7        # 卸载
```

`set` 的第二个参数必须是**一个 JSON 对象**（`src/cli/mcp-cli.ts` 的 `parseConfigValue` 按 JSON 解析，格式错会直接报错）。

**方式 2：直接编辑配置文件**

打开 `~/.openclaw/openclaw.json`，加 `mcp.servers`：

```json
{
  "mcp": {
    "servers": {
      "context7": { "command": "uvx", "args": ["context7-mcp"] },
      "my-remote": {
        "url": "https://example.com/mcp",
        "transport": "streamable-http",
        "headers": { "Authorization": "Bearer xxx" }
      }
    }
  }
}
```

**方式 3：对话内 slash 命令**

在 agent 会话里直接 `/mcp set ...`（需 owner / `operator.admin` 权限；普通成员只能 `/mcp show`）。命令语法与权限分层见 §六。

### 7.4 配置字段速查

| 字段 | stdio（本地命令） | http（远程） |
|---|---|---|
| `command` / `args` / `env` / `cwd` | ✅（command 必填） | — |
| `url` | — | ✅ 必填 |
| `transport` | — | `"sse"` 或 `"streamable-http"` |
| `headers` | — | ✅（鉴权放这里） |
| `connectionTimeoutMs` | 可选 | 可选 |

两个小贴士：

- 写旧别名 `type: "http"` 也会被自动归一化成 `transport: "streamable-http"`（`src/config/mcp-config-normalize.ts:17`），但建议直接写规范形态。
- 密钥传递：stdio 走 `env`（传给子进程），http 走 `headers`。

### 7.5 OpenClaw 怎么发现并加载你装的 MCP

**最关键的一点：`openclaw mcp set` 只是写配置文件——它既不连接，也不校验目标是否可达。**真正的"发现 + 加载"发生在 **agent 运行时**（你启动一个 coding / messaging agent 时），由 embedded Pi 驱动：

```
① 读 + 合并     agent 启动 → 从 mcp.servers 读你登记的定义，和插件自带 .mcp.json 合并，算配置指纹
② 逐个连接     按 command/url 判 stdio/http → 建 transport（stdio 会 spawn 子进程）→ MCP 握手（默认 30s 超时）
③ 列举工具     翻页拉出该 server 提供的全部工具
④ 命名去重     改写成「server名__工具名」并去重（多 server 同名工具不打架）
⑤ 物化注入     包装成内部工具注入 LLM —— agent「多了一批工具」
⑥ 调用        LLM 选用某工具 → 转发回原 MCP server → 取结果返回
⑦ 回收        空闲超 10 分钟（mcp.sessionIdleTtlMs）或会话结束 → 关连接、杀 stdio 子进程
```

> 这 7 步的源码细节见 §3.4（transport）、§3.5（bundle runtime）、§4.2（端到端数据流）。对使用者只需记住一句：**你登记的每个 MCP server，在 agent 启动时被连上、工具被列举改名后注入 LLM；agent 不跑就不会连。**

### 7.6 使用者须知

1. **改配置何时生效**：`set` 只改文件，**下一次 agent 会话**才加载。运行中的会话要等配置指纹变化触发重建，或重启会话。
2. **故障隔离**：某个 server 配错 / 连不上 / 命令不存在，只 `warn` 跳过它，不影响其他 server，也不拖垮 agent——装错不害怕。
3. **工具可见性**：登记的工具默认在 `coding` / `messaging` profile 下出现；`minimal` 隐藏；想彻底关掉所有 bundle MCP：`"tools": { "deny": ["bundle-mcp"] }`。
4. **stdio vs http 怎么选**：`uvx` / `npx` / `node` 在本机跑的 server → stdio；别人托管的远程服务（给一个 https URL）→ http。
5. **怎么验证真的生效**：`openclaw mcp list` 只能确认"配置写进去了"；要确认"连上 + 工具被发现"，开一个 agent 会话看它是否多出 `server名__工具名` 这批工具，或看 agent 启动日志（verbose 模式下 stderr 会打 MCP 连接信息）。

---

## 八、边界规则总结

OpenClaw 的 MCP 实现严格遵循 [AGENTS.md](../AGENTS.md) 的 Architecture 边界规则。下表把"谁依赖谁"讲清楚：

| 模块 | 可依赖 | 不可依赖 | 说明 |
|---|---|---|---|
| core `src/mcp/`、`src/agents/mcp-*`、`src/agents/pi-bundle-mcp-*`、`src/gateway/mcp-http*` | `@modelcontextprotocol/sdk`、core 内部 | 任何 `extensions/*/src/**` | 协议语义集中于此，core 不绑死具体 extension |
| `src/agents/cli-runner/bundle-mcp-*.ts` | `src/plugins/bundle-mcp.ts` 类型、core 内部 | extension 内部 | core 主动适配各 CLI，差异在此吸收 |
| `extensions/browser/.../chrome-mcp.ts` | `openclaw/plugin-sdk/text-runtime`（单向跨入 core）、MCP SDK | core `src/**` 深层、其它 extension `src/**` | client 角色，消费 `chrome-devtools-mcp` |
| `extensions/acpx/.../mcp-proxy.mjs` | **无**（零依赖 Node 脚本） | core、SDK、其它 extension | 纯 stdio 中间人，行级 JSON-RPC 改写 |
| 配置面 `src/config/*mcp*` | core config 工具 | transport/runtime | 只读写文件，不连接 |
| 控制面 `src/plugins/bundle-mcp.ts` | plugin manifest 读取工具 | transport/runtime | 只解析合并，无副作用 |

三条不可逾越的红线：

1. **extension 不直接 import core `src/**`**，只经 `openclaw/plugin-sdk/*` 跨入（browser）或保持完全独立（acpx proxy）。
2. **core 不含具体 extension 的 id / 依赖串 / 默认值**。provider 适配（claude/codex/gemini）虽在 core 的 cli-runner 内，但那是"core 适配外部 CLI"而非"core 依赖自家 extension"。
3. **新接缝必须向后兼容、文档化、版本化**（gateway protocol 变更 additive first；MCP capability 用 `experimental` 声明私有扩展如 Claude channel）。

---

## 九、附录：MCP 相关文件索引

> 仅列生产代码（`.test.ts`/`.test-support.ts`/`.e2e.test.ts` 除外），按子系统分组。

**配置面**
- `src/config/types.mcp.ts` — `McpServerConfig` / `McpConfig` 类型契约
- `src/config/mcp-config.ts` — server 定义 CRUD（list/set/unset）+ 乐观锁写
- `src/config/mcp-config-normalize.ts` — `type` 别名 → canonical `transport` 归一化

**Server 侧 · Channel MCP**
- `src/mcp/channel-server.ts` — 组装入口 + stdio 生命周期
- `src/mcp/channel-bridge.ts` — 核心桥接（Gateway 连接 + 事件队列 + 工具逻辑）
- `src/mcp/channel-tools.ts` — 8 个 MCP 工具注册
- `src/mcp/channel-shared.ts` — 共享类型与纯函数

**Server 侧 · Tools MCP**
- `src/mcp/tools-stdio-server.ts` — 通用工具型 MCP server 底座
- `src/mcp/plugin-tools-handlers.ts` — `AnyAgentTool` → MCP handler 适配（含 hook 包裹）
- `src/mcp/plugin-tools-serve.ts` — plugin 工具入口
- `src/mcp/openclaw-tools-serve.ts` — 内置工具入口（cron）

**Server 侧 · Loopback HTTP**
- `src/gateway/mcp-http.ts` — 主入口（起 HTTP server + 单例 + batch）
- `src/gateway/mcp-http.handlers.ts` — JSON-RPC 路由（initialize/list/call）
- `src/gateway/mcp-http.request.ts` — HTTP 守卫 + context 还原
- `src/gateway/mcp-http.runtime.ts` — 工具缓存（30s TTL）
- `src/gateway/mcp-http.loopback-runtime.ts` — 进程单例状态 + 配置产出
- `src/gateway/mcp-http.schema.ts` — schema 转换 + union 压平
- `src/gateway/mcp-http.protocol.ts` — JSON-RPC 协议常量

**Client 侧 · Transport**
- `src/agents/mcp-transport.ts` — 统一入口 `resolveMcpTransport`
- `src/agents/mcp-transport-config.ts` — 纯配置解析（判别联合）
- `src/agents/mcp-config-shared.ts` — 共享安全归一化工具
- `src/agents/mcp-stdio-transport.ts` — 自研 `OpenClawStdioClientTransport`
- `src/agents/mcp-stdio.ts` — stdio 启动配置描述
- `src/agents/mcp-http.ts` — http 配置解析

**Client 侧 · Bundle Runtime**
- `src/plugins/bundle-mcp.ts` — 控制面：读 + 合并 bundle MCP 配置
- `src/agents/pi-bundle-mcp-types.ts` — 类型契约
- `src/agents/pi-bundle-mcp-names.ts` — 工具名安全化与去重
- `src/agents/pi-bundle-mcp-runtime.ts` — 运行态（connect/list/call/dispose + manager）
- `src/agents/pi-bundle-mcp-materialize.ts` — 物化层（catalog → AnyAgentTool）
- `src/agents/pi-bundle-mcp-tools.ts` — barrel

**适配器 · CLI runner（跨 provider）**
- `src/agents/cli-runner/bundle-mcp.ts` — 编排器 + mode 分派 + resume hash
- `src/agents/cli-runner/bundle-mcp-adapter-shared.ts` — 共性抽取
- `src/agents/cli-runner/bundle-mcp-claude.ts` — argv + mcp.json
- `src/agents/cli-runner/bundle-mcp-codex.ts` — TOML inline
- `src/agents/cli-runner/bundle-mcp-gemini.ts` — JSON settings + env

**适配器 · Extension**
- `extensions/browser/src/browser/chrome-mcp.ts` — chrome-devtools-mcp client 会话
- `extensions/browser/src/browser/chrome-mcp.snapshot.ts` — aria snapshot 渲染
- `extensions/browser/src/browser/chrome-mcp.runtime.ts` — 按需加载闸门
- `extensions/acpx/src/runtime-internals/mcp-proxy.mjs` — stdio 中间人（注入 mcpServers）
- `extensions/acpx/src/runtime-internals/mcp-command-line.mjs` — 跨平台命令行解析

**入口与运维**
- `src/cli/mcp-cli.ts` — `openclaw mcp {serve,list,show,set,unset}`
- `src/auto-reply/reply/mcp-commands.ts` — `/mcp` 解析器
- `src/auto-reply/reply/commands-mcp.ts` — `/mcp` handler + 权限 gate

**官方文档**
- `docs/cli/mcp.md` — 面向用户的 MCP CLI 使用文档（serve + registry）

---

## 延伸阅读

- 用户向使用文档：[docs/cli/mcp.md](../docs/cli/mcp.md)
- agent 运行循环与流式：见同目录 [openclaw-react-loop-and-flows.md](./openclaw-react-loop-and-flows.md)
- 多模型/provider 适配（与 cli-runner adapter 呼应）：见 [multi-model-adaptation.md](./multi-model-adaptation.md)
- 插件系统与 manifest：见 [openclaw-skill-system.md](./openclaw-skill-system.md)

> 本文档基于 `@modelcontextprotocol/sdk@1.29.0` 时期的源码写成。若 SDK 协议版本或 transport 抽象发生不兼容变更（如新增 WebSocket transport、调整 session 语义），以 `src/agents/mcp-transport*.ts` 与 `pi-bundle-mcp-runtime.ts` 的实际实现为准。
