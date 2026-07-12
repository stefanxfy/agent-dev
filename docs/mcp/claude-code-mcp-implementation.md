# Claude Code MCP（Model Context Protocol）子系统完整实现解析

> **源码版本**：Claude Code TypeScript 源码（`src/`），MCP 相关代码集中在 `src/services/mcp/`（约 12300 行）+ 工具/UI/命令层。
> **分析目标**：把"MCP 子系统"作为一个**整体**进行代码级拆解，覆盖**配置 → 连接编排 → 传输 → 握手 → 能力协商 → 工具/资源/prompt 自动封装 → 注册进统一工具体系 → 权限/批准 → 认证 → elicitation → CLI** 全链路。
> **写作约定**：所有结论均带 `文件路径:行号` 引用，关键逻辑使用真实代码片段（```ts 代码块```）。本仓库内相对路径以 `src/` 为根。
> **范围说明**：MCP skill 提升路径依赖的 `src/skills/mcpSkills.ts` 在本源码快照中**未随包发布**（文件不存在，但引用链完整——`client.ts:119`、`useManageMCPConnections.ts:24`、`skills/mcpSkillBuilders.ts` 均 require/注册它）。该章节结论会明确标注「基于调用方引用反推」。此外，MCP 底层协议原语（PKCE、token 交换、JSON-RPC 编解码）实际实现在 `@modelcontextprotocol/sdk`，本文聚焦 Claude Code 在 SDK 之上的**编排层**。

---

## 0. 一图总览

Claude Code 的 MCP 不是一个"插件加载器"，而是一条**从声明式配置到统一工具体系**的完整流水线。使用者只写一条配置，其余全自动：

```text
┌──────────────────────────────────────────────────────────────────────────────┐
│  ① 配置层  config.ts / types.ts                                               │
│  7 种 server 类型 × 7 种 ConfigScope，合并去重，手动优先                       │
│  .mcp.json / settings.json / --mcp-config / enterprise / claude.ai / managed  │
│  getAllMcpConfigs()  config.ts        expandEnvVars() config.ts:556           │
│  isMcpServerDisabled() config.ts:1528  (disabledMcpServers / 批准状态过滤)     │
└──────────────────────────────────────────────────────────────────────────────┘
                                  ↓
┌──────────────────────────────────────────────────────────────────────────────┐
│  ② 连接编排  client.ts:getMcpToolsCommandsAndResources (client.ts:2226)       │
│  分桶：本地(stdio/sdk, batch=3) vs 远程(sse/http/ws, batch=20)               │
│  processBatched 并发连接；needs-auth 缓存命中则跳过(client.ts:2307)            │
└──────────────────────────────────────────────────────────────────────────────┘
                                  ↓
┌──────────────────────────────────────────────────────────────────────────────┐
│  ③ 建立连接 + 能力协商  connectToServer (client.ts:595, memoized LRU)          │
│  按 config.type 选 transport → new Client() → initialize() 握手               │
│  → getServerCapabilities() 协商 {tools?, prompts?, resources?, ...}           │
│  → 状态: connected | failed | needs-auth | pending | disabled                  │
└──────────────────────────────────────────────────────────────────────────────┘
                                  ↓
┌──────────────────────────────────────────────────────────────────────────────┐
│  ④ 原语发现（并行）                                                           │
│  fetchToolsForClient      client.ts:1745   tools/list                         │
│  fetchCommandsForClient   client.ts:2044   prompts/list                       │
│  fetchResourcesForClient  client.ts:2000   resources/list                     │
│  fetchMcpSkillsForClient  (feature('MCP_SKILLS')) mcpSkills.ts【缺失】         │
└──────────────────────────────────────────────────────────────────────────────┘
                                  ↓
┌──────────────────────────────────────────────────────────────────────────────┐
│  ⑤ 自动封装（模板 + override）                                                │
│  每个 MCP tool →  { ...MCPTool, name: mcp__server__tool, mcpInfo, ... }        │
│  fetchToolsForClient client.ts:1745   normalization.ts 规范化名字              │
│  resources → 注入 ListMcpResourcesTool / ReadMcpResourceTool（全局一次）       │
│  prompts  → Command(mcp__server__prompt) 或提升为 MCP skill                   │
└──────────────────────────────────────────────────────────────────────────────┘
                                  ↓
┌──────────────────────────────────────────────────────────────────────────────┐
│  ⑥ 注册进统一工具体系  AppState.tools                                         │
│  与内置 Read/Write/Bash/Grep 并列在同一张表，LLM 看到统一的工具清单             │
│  onConnectionAttempt 回调 → useManageMCPConnections → AppState                │
└──────────────────────────────────────────────────────────────────────────────┘
                                  ↓
┌──────────────────────────────────────────────────────────────────────────────┐
│  ⑦ 调用期横切                                                                 │
│  权限两层：project server 批准(utils.ts:351) + 运行时工具权限(四态)             │
│  认证：OAuth 2.1 / XAA(SEP-990) / claude.ai proxy / 15min needs-auth 缓存      │
│  elicitation：server→client 请求输入(elicitation/create)                       │
│  输出治理：token 截断(mcpValidation) + Unicode sanitize + blob 持久化          │
└──────────────────────────────────────────────────────────────────────────────┘
```

使用者心智模型：**写一条配置 → 启动时自动连上 → 握手问能力 → 拉工具清单 → 套模板克隆成 `mcp__server__tool` → 和内置工具并排丢给 LLM**。整个过程不碰代码。

---

## 1. 总体设计理念

MCP 子系统的设计由三条主线贯穿，理解这三条主线，后面所有实现细节都是它们的展开。

### 1.1 配置驱动（Declarative）

使用者唯一的输入是一条**声明式配置**——描述"server 是什么类型、怎么启动"，而不是"怎么连接、怎么封装"。配置是整个子系统的唯一真相来源（single source of truth），`getAllMcpConfigs()`（`config.ts`）把 7 个 scope 的配置合并成一张扁平表，下游所有环节（连接、封装、权限）都从这张表派生。这带来两个直接后果：

- **零胶水代码**：装一个 MCP server 不需要写任何集成代码，只需声明。
- **配置即能力边界**：`isMcpServerDisabled()`、批准状态、scope 优先级全部在配置层决定，连接层只是执行者。

### 1.2 能力协商（Capability-driven）

Claude Code 的客户端**不假设** MCP server 有什么能力，而是在握手后主动询问。`connectToServer`（`client.ts:595`）完成 `Client.initialize()` 后，立即调用 `getServerCapabilities()` 拿到 server 自报的能力集合：

```text
capabilities = { tools?, prompts?, resources?, resources.subscribe?, resources.listChanged?, ... }
```

后续每个原语发现函数都**先检查对应 capability 再发请求**：

- `fetchToolsForClient`：`if (!client.capabilities?.tools) return []`（`client.ts:1748`）
- `fetchResourcesForClient`：`capabilities?.resources` 为假直接返回 `[]`（`client.ts:2000`）
- `fetchCommandsForClient`：`capabilities?.prompts` 检查（`client.ts:2038`）

这是 MCP 协议"渐进式增强"哲学的体现：server 升级加了工具/资源，客户端不用改任何代码，下次连接自动发现。

### 1.3 模板封装（Template + Override）

MCP 工具的"自动封装"是整个子系统最精巧的设计。Claude Code **不为每个 MCP 工具写一个类**，而是定义一个通用模板 `MCPTool`（`tools/MCPTool/MCPTool.ts`），承载所有共用逻辑（渲染、权限、结果截断、并发安全判定），然后在 `fetchToolsForClient`（`client.ts:1745`）里对 server 返回的**每一个**工具，以这个模板为蓝本 `override` 出一个独立实例：

```ts
return toolsToProcess.map((tool): Tool => ({
  ...MCPTool,                                    // 复用模板
  name: buildMcpToolName(client.name, tool.name), // mcp__server__tool
  mcpInfo: { serverName: client.name, toolName: tool.name },
  async description() { return tool.description ?? '' },
  async call(...) { /* 转发到 client.callTool */ },
}))
```

这就是为什么"工具封装"本身只有几十行代码，而连接/认证/传输占了上万行——共用逻辑被收敛进单一模板，每个工具只 override 差异部分。

### 1.4 统一工具体系：MCP 工具与内置工具没有区别

封装后的 MCP 工具进入 `AppState.tools`，和内置的 `Read`/`Write`/`Bash`/`Grep` **并列在同一张表**。对 LLM 来说，`mcp__filesystem__read_file` 和 `Read` 是同类条目，走**同一个调用管道**（权限检查 → 执行 → 结果渲染）。这是"统一工具体系"的核心——外部工具被透明地融入内部工具语义。

### 1.5 两种注册范式的对比：MCP vs Skill

这是理解 Claude Code 工具体系的关键分野。同样是"把外部能力暴露给 LLM"，MCP 和 Skill 采用了**完全相反**的注册范式：

| 维度 | MCP 工具 | Skill |
|------|----------|-------|
| 注册粒度 | 每个工具 → **独立** Tool（`mcp__server__tool`） | 全部 skill → **共享一个** `Skill` Tool |
| 发现方式 | LLM 直接看到每个工具的独立 schema | LLM 看到 `Skill` 工具，skill 名单塞进它的 `prompt` |
| 调用方式 | 直接调用对应工具 | 调用 `Skill(skill="name")` 靠参数选中 |
| 执行本质 | 远程/进程外函数调用（`callTool` RPC） | prompt 展开（SKILL.md / MCP prompt 注入会话） |
| 封装位置 | `fetchToolsForClient`（`client.ts:1745`）模板 override | `SkillTool`（`tools/SkillTool/`）单工具 + 参数路由 |

MCP 是"一个 server，N 个工具，N 个 Tool 注册项"；Skill 是"N 个 skill，1 个 Tool，靠参数路由"。两者的交集在 **MCP skill**——MCP server 暴露的 prompt 被提升为 skill 后，会进入 `Skill` 工具的清单（详见第 8 章）。

### 1.6 与官方 MCP SDK 的边界：薄封装 + 厚编排

Claude Code 在 `@modelcontextprotocol/sdk` 之上做的是**薄协议封装 + 厚工程编排**：

- **SDK 负责**：传输底层（Stdio/SSE/StreamableHTTP Transport）、JSON-RPC 编解码、PKCE 计算、OAuth 状态机骨架。
- **Claude Code 负责**：多 scope 配置合并、连接并发编排、能力协商后的封装与注册、OAuth 编排（回调服务器、元数据发现、跨进程刷新锁、缓存）、XAA 跨应用授权、权限批准 UI、elicitation UI、输出 token 治理、CLI。

这条边界决定了本文的重点：SDK 内部细节一笔带过，编排层的每个决策都讲清楚为什么。

---

## 2. 配置层：`types.ts` + `config.ts`

配置层是整条流水线的入口，也是使用者唯一需要接触的层。

### 2.1 七种 server 类型：`types.ts` 的 schema

`src/services/mcp/types.ts` 用 zod 定义了所有合法的 server 配置形态。`TransportSchema`（`types.ts`）枚举了 6 种对外 transport 加 1 种内部 proxy：

```ts
export const TransportSchema = lazySchema(() =>
  z.enum(['stdio', 'sse', 'sse-ide', 'http', 'ws', 'sdk']),
)
```

每种 transport 对应一个 server config schema，最终 union 成 `McpServerConfigSchema`：

| 类型 | schema | 用途 |
|------|--------|------|
| `stdio` | `McpStdioServerConfigSchema` | spawn 本地进程，标准输入输出通信（`command`/`args`/`env`） |
| `sse` | `McpSSEServerConfigSchema` | 远程 SSE 长连接（`url`/`headers`/`headersHelper`/`oauth`） |
| `sse-ide` | `McpSSEIDEServerConfigSchema` | **内部专用**，IDE 扩展的 SSE 变体（`ideName`） |
| `http` | `McpHTTPServerConfigSchema` | Streamable HTTP（新版 MCP 流式 HTTP） |
| `ws` | `McpWebSocketServerConfigSchema` | WebSocket（`url`/`headers`） |
| `ws-ide` | `McpWebSocketIDEServerConfigSchema` | **内部专用**，IDE 扩展的 WS 变体 |
| `sdk` | `McpSdkServerConfigSchema` | 进程内 transport（SDK 嵌入场景，仅 `name`） |
| `claudeai-proxy` | `McpClaudeAIProxyServerConfigSchema` | Claude.ai 代理的远程 server（`url`/`id`） |

注意 `*-ide` 和 `claudeai-proxy` 是**内部专用类型**，使用者一般不直接配置。OAuth 配置（`McpOAuthConfigSchema`）只在 `sse`/`http` 上挂载，支持 `clientId`/`callbackPort`/`authServerMetadataUrl`/`xaa`。

`sse-ide`/`ws-ide` 的存在揭示了 IDE 集成是 MCP 的一等公民——它不是事后补丁，而是 schema 层就预留的独立 transport 类型（详见第 3、11 章）。

### 2.2 七种 ConfigScope 与优先级合并

配置可以来自 7 个层级，`ConfigScopeSchema`（`types.ts`）定义：

```ts
z.enum(['local', 'user', 'project', 'dynamic', 'enterprise', 'claudeai', 'managed'])
```

`getAllMcpConfigs()`（`config.ts`）按**优先级从高到低**合并这些来源，关键规则是**手动配置优先于自动来源**：

1. **local**：当前项目私有（不进 git），优先级最高
2. **user**：用户全局（`~/.claude`）
3. **project**：项目级 `.mcp.json`（可提交 git，团队共享）
4. **dynamic**：`--mcp-config` 命令行参数 / SDK 注入
5. **enterprise**：企业管理策略
6. **claudeai**：从 Claude.ai 同步下来的 connector
7. **managed**：受管策略（policy，最低优先级但可强制）

合并时会处理同名冲突——`config.ts` 的 `getServerSignature`（`:202`）为每个 server 计算签名用于去重，`dedupClaudeAiMcpServers`（`config.ts:289`）专门处理 claude.ai connector 与手动配置的重叠（禁用的 manual server 不参与去重签名）。

### 2.3 `.mcp.json`：团队共享的事实标准

`project` scope 的载体是项目根目录的 `.mcp.json`，格式就是 `McpJsonConfigSchema`：

```ts
export const McpJsonConfigSchema = lazySchema(() =>
  z.object({
    mcpServers: z.record(z.string(), McpServerConfigSchema()),
  }),
)
```

`writeMcpConfigToMcpJson`（`config.ts`，路径 `<cwd>/.mcp.json`，见 `:89`）负责写入。这是团队共享 MCP 配置的推荐方式——提交到 git，团队成员 clone 后只需批准即可（批准机制见第 9 章）。

### 2.4 两组极易混淆的配置 key

这是配置层最隐蔽的陷阱。MCP 有**两组名字相似但来源、用途完全不同**的 key：

| key 组 | 所属 store | 作用 | 写入点 |
|--------|-----------|------|--------|
| `enabledMcpServers` / `disabledMcpServers` | **projectConfig**（`getCurrentProjectConfig`） | 已加载 server 的运行时 enable/disable，`/mcp` 菜单 toggle 持久化 | `setMcpServerEnabled`（`config.ts:1553`） |
| `enabledMcpjsonServers` / `disabledMcpjsonServers` / `enableAllProjectMcpServers` | **localSettings**（`updateSettingsForSource('localSettings')`） | `.mcp.json` 项目级 server 的**首次信任决策**结果 | `MCPServerApprovalDialog` |

读取这两组的函数也分开：

```ts
// 运行时开关：config.ts:1528
export function isMcpServerDisabled(name: string): boolean {
  // builtin(DEFAULT_DISABLED_BUILTIN, CHICAGO_MCP): 默认禁，需在 enabledMcpServers 中才启用
  // 普通: name 在 disabledMcpServers 中即禁用
}

// 首次批准：utils.ts:351 getProjectMcpServerStatus
// → 'approved' | 'rejected' | 'pending'
```

记住这个区分：**`McpServers`（无 json）管开关，`McpjsonServers`（有 json）管首次批准**。连接层（第 4 章）会先用 `isMcpServerDisabled` 过滤，批准 UI（第 9 章）写的是 `McpjsonServers`。

`DEFAULT_DISABLED_BUILTIN`（`config.ts:1512`）是特例——Computer Use server（`feature('CHICAGO_MCP')`）默认禁用，必须显式 enable，体现了"高能力工具默认关闭"的安全姿态。

### 2.5 环境变量展开：`envExpansion.ts`

配置里允许写 `${VAR}` 和 `${VAR:-default}`，在**配置校验通过后、连接之前**展开。`expandEnvVarsInString`（`envExpansion.ts:10`）：

```ts
export function expandEnvVarsInString(value: string): {
  expanded: string
  missingVars: string[]
} {
  const expanded = value.replace(/\$\{([^}]+)\}/g, (match, varContent) => {
    const [varName, defaultValue] = varContent.split(':-', 2)
    const envValue = process.env[varName]
    if (envValue !== undefined) return envValue
    if (defaultValue !== undefined) return defaultValue
    missingVars.push(varName)
    return match  // 保留原 ${VAR} 文本，便于报错定位
  })
  return { expanded, missingVars }
}
```

调用点是 `config.ts:1327` 的 `expandEnvVars`——按 server type 分别展开 stdio 的 `command`/`args`/`env`、sse/http/ws 的 `url`/`headers`；`sse-ide`/`ws-ide`/`sdk` 不展开。**关键设计**：`missingVars` 非空时直接生成 `severity: fatal` 的 `ValidationError`（`config.ts:1333`），即缺失环境变量会**硬阻断**该 server 加载，而不是静默用空值。这避免了"token 没设 → server 用空认证连上 → 暴露未授权接口"的安全风险。

展开同样被 LSP 插件（`utils/plugins/lspPluginIntegration.ts:247`）和 MCP 插件（`utils/plugins/mcpPluginIntegration.ts:486`）复用。

### 2.6 配置层小结

配置层的三个设计决策值得铭记：

1. **类型即文档**：7 种 server 类型用 zod schema 精确定义，非法配置在校验期就被拒绝，不会带到运行时。
2. **scope 隔离 + 手动优先**：多来源合并时，使用者手写的配置永远赢，防止企业策略/claude.ai 同步覆盖用户意图。
3. **fail-fast on secrets**：环境变量缺失直接 fatal，宁可让 server 加载失败，也不让它在降级状态下静默运行。

---

## 3. 传输层：七种 transport 的实现

配置里的 `type` 字段决定走哪种 transport。`connectToServer`（`client.ts:595`）内部按 type 分发，创建对应的 SDK Transport 对象。Claude Code 直接复用 SDK 提供的 transport，但在外层包了统一的超时/代理/认证 fetch 包装器。

### 3.1 transport 的 import 与分发

`client.ts` 顶部一次性 import 所有 transport（`:9-15`）：

```ts
import { SSEClientTransport, type SSEClientTransportOptions } from '...'
import { StdioClientTransport } from '@modelcontextprotocol/sdk/client/stdio.js'
import { StreamableHTTPClientTransport, type StreamableHTTPClientTransportOptions } from '...'
import { WebSocketTransport } from '../../utils/mcpWebSocketTransport.js'
```

`isLocalMcpServer`（`client.ts:563`）是分桶的基础判据——无 `type` 或 `stdio`/`sdk` 算本地（要 spawn 进程），其余算远程。

### 3.2 stdio：本地进程

最常见的形态。`StdioClientTransport` 接收 `{ command, args, env }`，SDK 负责 spawn 子进程并通过 stdin/stdout 通信。Claude Code 层主要做两件事：

1. **环境变量展开**（第 2 章），让 `command`/`args`/`env` 支持 `${VAR}`。
2. **进程生命周期托管**：连接 drop 时清理子进程，避免僵尸进程。

stdio 是本地低并发桶的主力（batch=3），因为 spawn 进程有资源开销。

### 3.3 sse / sse-ide：Server-Sent Events 长连接

SSE 用 `SSEClientTransport`（`client.ts:673`、`:702` 实例化）。两条变体：

- **sse**：标准远程 SSE，可挂 `oauth`/`headers`/`headersHelper`，走 `wrapFetchWithTimeout`。
- **sse-ide**：IDE 扩展专用，带 `ideName`/`ideRunningInWindows`，用于 Claude Code 与 IDE（VS Code 等）之间的 MCP 通道。

SSE 的 EventSource 路径硬编码 `Accept: 'text/event-stream'`（`client.ts:667`）。注意 SSE 的 eventSourceInit.fetch **不包 timeout**（`client.ts:648`），因为 SSE 是长连接流——这正是 `wrapFetchWithTimeout` 对 GET 特殊处理的原因（见 3.8）。

### 3.4 http：Streamable HTTP（新版 MCP）

`http` 类型用 `StreamableHTTPClientTransport`（`client.ts:826`），这是 MCP 2025-03-26 规范引入的"流式 HTTP"——服务端可在单次响应里用 SSE 分片返回。它有一个**容易踩的坑**：服务端严格校验 `Accept` 头，缺失会返回 HTTP 406。常量定义在 `client.ts:471`：

```ts
const MCP_STREAMABLE_HTTP_ACCEPT = 'application/json, text/event-stream'
```

虽然 SDK 在 `StreamableHTTPClientTransport.send()` 内部会设这个头，但经过 `Headers` 对象 spread 后，某些 runtime 会丢失它。因此 Claude Code 在 `wrapFetchWithTimeout` 的最后一层**强制保证**（`client.ts:507`）：

```ts
const headers = new Headers(init?.headers)
if (!headers.has('accept')) {
  headers.set('accept', MCP_STREAMABLE_HTTP_ACCEPT)
}
```

这是典型的"防御性补丁"——明知 SDK 会设，但在最外层再保一次，消除 runtime 差异。

### 3.5 ws / ws-ide：WebSocket

WebSocket transport 是 Claude Code **自己实现**的（`utils/mcpWebSocketTransport.ts`），而非来自 SDK。`createNodeWsClient`（`client.ts:436`）创建底层 `ws.WebSocket` 客户端，支持三项企业级能力：

- **mTLS**：`getWebSocketTLSOptions()`（来自 `utils/mtls.js`，`client.ts:709`）
- **代理**：`getWebSocketProxyUrl` / `getWebSocketProxyAgent`
- **自定义 headers**

ws-ide 同样带 `ideName`/`authToken`，是 IDE 扩展的 WS 变体。Bun 的 WebSocket 支持这些选项但 DOM 类型定义没有，代码里有针对 Bun 的特殊处理（`client.ts:719` 注释）。

### 3.6 sdk：进程内 transport

`sdk` 类型用于 Claude Agent SDK 嵌入场景——server 和 client 在同一进程内，不需要网络/进程通信。两种实现：

- **InProcessTransport**（`services/mcp/InProcessTransport.ts`）：纯内存消息传递。
- **SdkControlTransport**（`services/mcp/SdkControlTransport.ts`）：SDK 控制通道。

sdk 类型有一个特殊的 `skipPrefix` 模式——当 `config.type === 'sdk'` 且环境变量 `CLAUDE_AGENT_SDK_MCP_NO_PREFIX` 为真时，MCP 工具名**不加 `mcp__` 前缀**，用原始名，目的是让 SDK MCP 工具能**按名覆盖内置工具**（`client.ts:1760`）。但权限检查仍走全限定名（`mcpInfo` 保留），防止冒充（第 9 章）。

### 3.7 claudeai-proxy：复用 Claude Code 自身认证

`claudeai-proxy` 类型最特殊——它**不跑独立的 OAuth**，而是复用 Claude Code 自身登录 claude.ai 的 OAuth token。`createClaudeAiProxyFetch`（`client.ts:372`）包装 fetch：

```ts
export function createClaudeAiProxyFetch(innerFetch: FetchLike): FetchLike {
  return async (url, init) => {
    // 读 claude.ai OAuth token → 设 Authorization: Bearer → 发请求
    // 401 时：handleOAuth401Error 判 token 是否变了，变了才重试
  }
}
```

设计动机（`client.ts:363` 注释）：Anthropic API 路径有 401 重试机制（`withRetry.ts`/`grove.ts`）处理 memoize 缓存陈旧和时钟漂移，但 claude.ai proxy 路径如果没有同样的重试，一个陈旧 token 会让**所有** claude.ai connector 集体 401 并全部卡进 15 分钟 needs-auth 缓存。`createClaudeAiProxyFetch` 的核心技巧是**快照发送时的 token**（`sentToken`，`client.ts:384`）——并发 401 时别的 connector 的 `handleOAuth401Error` 会清缓存，若重读会拿到新 token，导致本 connector 误判 same-as-keychain 跳过重试。详细的认证逻辑见第 5 章。

使用点：`connectToServer` 的 memoize callback 内 `client.ts:884`：`const fetchWithAuth = createClaudeAiProxyFetch(globalThis.fetch)`。

### 3.8 `wrapFetchWithTimeout`：统一的超时/头处理包装器

所有基于 HTTP 的 transport（sse/http/claudeai-proxy）都经过 `wrapFetchWithTimeout`（`client.ts:492`）。它做了三件事：

**① GET 不加超时，其他方法每请求 60s 超时**（`client.ts:498`）：

```ts
// GET 是长连接 SSE 流，不能加超时
if (method === 'GET') return doFetch()
// POST 等：新建 AbortController + setTimeout(MCP_REQUEST_TIMEOUT_MS)
```

这里用 `setTimeout` 而非 `AbortSignal.timeout()`，是为了避免 Bun 下信号对象的内存泄漏（`client.ts:512` 注释）。

**② parentSignal 级联**（`client.ts:525`）：外层 abort 会级联到内层请求，保证连接关闭时所有在途请求被取消。

**③ 强制 Accept 头**（`client.ts:507`）：保证 Streamable HTTP 的 `Accept` 头存在（3.4 节）。

### 3.9 超时矩阵

transport 层涉及多个超时常量，容易混淆，集中说明：

| 常量 | 行号 | 值 | 语义 |
|------|------|-----|------|
| `getConnectionTimeoutMs()` | `client.ts:456` | `MCP_TIMEOUT \|\| 30000` | 连接建交（initialize）超时，30s |
| `MCP_REQUEST_TIMEOUT_MS` | `client.ts:463` | `60000` | 单个 POST 请求超时，60s |
| `DEFAULT_MCP_TOOL_TIMEOUT_MS` | `client.ts:211` | `100_000_000`（≈27.8h） | 工具调用超时，"effectively infinite" |
| `getMcpToolTimeoutMs()` | `client.ts:224` | `MCP_TOOL_TIMEOUT \|\| DEFAULT` | 可被环境变量覆盖 |

注意工具调用默认"无限超时"——因为有些 MCP 工具（如长时间批处理） legitimately 需要跑很久，强行 60s 截断会破坏它们。连接（30s）和单请求（60s）有超时，但工具调用本身信任 server。

### 3.10 传输层小结

传输层的设计哲学是**"SDK 提供原语，Claude Code 提供工程化包装"**：7 种 transport 复用 SDK 的 5 个 + 自实现的 1 个（ws）+ 内部专用的 2 个（ide 变体），但所有 HTTP 类 transport 都套上 `wrapFetchWithTimeout` 统一治理超时与头。最值得学的是 `wrapFetchWithTimeout` 对 GET/POST 的区别对待——它精确地理解了"SSE 长连接不能加超时"这个协议语义。

---

## 4. 连接生命周期：`connectToServer` 与状态机

连接层是 MCP 子系统的心脏，它把"配置项"变成"活着的连接 + 可用的工具"。

### 4.1 `connectToServer`：memoized 的连接工厂

`connectToServer`（`client.ts:595`）是整个子系统最核心的函数，用 `memoize` 包装：

```ts
export const connectToServer = memoize(
  async (name: string, serverRef: ScopedMcpServerConfig, ...): Promise<MCPServerConnection> => {
    // 1. 选 transport（第 3 章）
    // 2. new Client({...}, { capabilities })  ← client.ts:985
    // 3. await client.initialize()  ← 握手
    // 4. client.getServerCapabilities()  ← 协商
    // 5. 返回 MCPServerConnection
  },
  ... // memoize key
)
```

memoize 的 cache key 是 `getServerCacheKey`（`client.ts:581`），保证相同配置不重复连接。`clearServerCache`（`client.ts:1648`）在配置变更、OAuth 完成后失效缓存，触发重连。

### 4.2 Client 构造与 capabilities 声明

`new Client()` 时（`client.ts:985`），Claude Code 主动声明客户端能力：

```ts
capabilities: {
  roots: {},
  elicitation: {},
}
```

注意 `elicitation` 用**空对象 `{}`** 而非 `{form:{},url:{}}`（`client.ts:996` 注释）。原因是 Java Spring AI MCP SDK 的 Elicitation 类**零字段**，对未知属性会报错——为了跨语言客户端兼容，Claude Code 故意声明空对象。这是"协议互操作性优先于表达力"的典型取舍。

### 4.3 能力协商：`getServerCapabilities`

握手成功后立即 `client.getServerCapabilities()`（`client.ts:1157`），拿到 server 自报的能力集合。日志记录（`client.ts:1176`）：

```ts
`Connection established with capabilities: ${jsonStringify({
  hasTools: !!capabilities?.tools,
  hasPrompts: !!capabilities?.prompts,
  hasResources: !!capabilities?.resources,
  hasResourceSubscribe: !!capabilities?.resources?.subscribe,
})}`
```

这个 capabilities 对象是后续所有原语发现的**开关**（第 1.2 节）。`ConnectedMCPServer` 类型（`types.ts`）把它存进连接对象：

```ts
export type ConnectedMCPServer = {
  client: Client
  name: string
  type: 'connected'
  capabilities: ServerCapabilities  // ← 协商结果
  serverInfo?: { name: string; version: string }
  instructions?: string
  config: ScopedMcpServerConfig
  cleanup: () => Promise<void>
}
```

### 4.4 五种连接状态：状态机

`MCPServerConnection`（`types.ts`）是个判别联合（discriminated union），`type` 字段区分 5 种状态：

```ts
type MCPServerConnection =
  | ConnectedMCPServer       // 连接成功，有 client + capabilities
  | FailedMCPServer          // 连接失败，带 error
  | NeedsAuthMCPServer       // 需要 OAuth 认证（401）
  | PendingMCPServer         // 正在连接/重连中
  | DisabledMCPServer        // 被用户禁用
```

状态流转由 `useManageMCPConnections`（`services/mcp/useManageMCPConnections.ts`）驱动。这套状态机直接决定了 `/mcp` 菜单的 UI 呈现和工具可用性——只有 `connected` 状态的 server 才会贡献工具。

### 4.5 批量并发连接：本地与远程分桶

`getMcpToolsCommandsAndResources`（`client.ts:2226`）在启动时批量连接所有 server，关键策略是**按本地/远程分桶 + 不同并发度**（`client.ts:2388`）：

```ts
const localServers = configEntries.filter(([_, c]) => isLocalMcpServer(c))   // stdio/sdk
const remoteServers = configEntries.filter(([_, c]) => !isLocalMcpServer(c)) // sse/http/ws

await Promise.all([
  processBatched(localServers,  getMcpServerConnectionBatchSize()         /*=3*/,  processServer),
  processBatched(remoteServers, getRemoteMcpServerConnectionBatchSize()   /*=20*/, processServer),
])
```

分桶理由（`client.ts:2388` 注释）：本地 server 要 spawn 进程，资源争抢明显，低并发（3）；远程 server 只是网络连接，高并发（20）。两个桶**并行**推进，互不阻塞——本地慢连接不会拖累远程连接。

`processBatched` 是一个通用的并发限流器，保证同一桶内最多 N 个连接在途。

### 4.6 needs-auth 缓存：避免反复探测

远程 server（sse/http/claudeai-proxy）如果返回 401，会被标记 `needs-auth` 并**缓存 15 分钟**（`MCP_AUTH_CACHE_TTL_MS`，`client.ts:257`）。在缓存有效期内，`processServer` 会**直接跳过连接**（`client.ts:2307`）：

```ts
if (
  (config.type === 'claudeai-proxy' || config.type === 'http' || config.type === 'sse') &&
  ((await isMcpAuthCached(name)) ||
   ((config.type === 'http' || config.type === 'sse') && hasMcpDiscoveryButNoToken(name, config)))
) {
  logMCPDebug(name, `Skipping connection (cached needs-auth)`)
  onConnectionAttempt({ client: { name, type: 'needs-auth', config }, tools: [createMcpAuthTool(name, config)], commands: [] })
  return
}
```

跳过时仍注入一个 `McpAuthTool` 伪工具（第 5 章），让 LLM 知道这个 server 存在并能代用户发起认证。第二条检查 `hasMcpDiscoveryButNoToken`（`auth.ts:348`）是为了堵住 TTL 留下的窗口——没有它，每 15 分钟都会重新探测一次注定失败的 server。**XAA 是例外**：`isXaaEnabled() && config.oauth?.xaa` 时返回 false，因为 XAA 可用缓存的 id_token 静默重认证（第 5 章）。

### 4.7 重连机制

连接建立后可能因网络抖动、server 重启而断开。重连逻辑分两层：

**连接级重试**（`client.ts:1216`）：维护 `consecutiveConnectionErrors` 计数，达到 `MAX_ERRORS_BEFORE_RECONNECT = 3`（`client.ts:1228`）后手动触发 `client.close()` → 重连。`client.close()` 会 reject 所有 pending request handler（`client.ts:1234` 注释），避免 hung callTool。

**健康检查复用**（`client.ts:1688`）：`ensureConnectedClient(client)` 在工具调用前确保连接活着——健康时是 memoize 命中（no-op），`onclose` 后返回新连接。`ListMcpResourcesTool` 等（第 7 章）都通过它保证调用前连接就绪。

### 4.8 动态刷新：list_changed 通知

server 的工具/资源/prompt 清单可能在运行期变化（server 热更新）。Claude Code 通过 MCP 的 `*_list_changed` 通知**被动刷新**，而非轮询。在 `useManageMCPConnections` 注册 handler：

- **resources/list_changed**（`useManageMCPConnections.ts:705`）：仅当 `capabilities?.resources?.listChanged` 为真时注册，收到通知 → `fetchResourcesForClient.cache.delete(client.name)` 失效 LRU → 下次访问重新 `resources/list`。若 `feature('MCP_SKILLS')` 还顺带失效 skills/commands 缓存（`:718`）。
- **prompts/list_changed**（`useManageMCPConnections.ts:673`）：同理，刷新 prompt/skill 清单。

这套机制和第 7 章的 `resources/subscribe` 形成对比——Claude Code 选择 `list_changed`（拉模型）而非 `subscribe`（推模型），因为 list_changed 更简单且足够。

### 4.9 `useManageMCPConnections` 与 `MCPConnectionManager`

`useManageMCPConnections`（`services/mcp/useManageMCPConnections.ts`，1141 行）是 React hook，是连接生命周期的**编排中枢**：监听 dynamic config 变化、驱动批量连接、注册所有 notification handler、维护连接状态、暴露 `reconnect`/`toggleEnabled` 能力。

`MCPConnectionManager`（`services/mcp/MCPConnectionManager.tsx`）是薄薄的 React Context Provider，把 `useManageMCPConnections` 的 `reconnect`/`toggleEnabled` 通过 context 暴露给 UI 层（`useMcpReconnect`/`useMcpToggleEnabled`）。它本身只有 72 行，注释（`:37`）提到未来可能把这些函数挪到 app state 以消除 context。

### 4.10 连接层小结

连接层把"声明式配置"变成"活的连接"，关键设计：

1. **memoize 连接**：相同配置不重复连接，配置变更才失效重连。
2. **能力协商**：握手后问能力，绝不假设 server 有什么。
3. **本地/远程分桶并发**：尊重 spawn 与网络的开销差异。
4. **needs-auth 短路**：15 分钟缓存避免对未认证 server 的反复探测，同时保留 McpAuthTool 让 LLM 能代为认证。
5. **被动刷新**：靠 `list_changed` 通知而非轮询，及时且省资源。

---

## 5. 认证子系统：`auth.ts`（2465 行）

认证是 MCP 子系统最复杂、代码量最大的部分。远程 server（sse/http/claudeai-proxy）普遍需要认证，Claude Code 在 OAuth 2.1 基础上扩展了 XAA 跨应用授权、token 刷新并发控制、needs-auth 缓存等工程能力。

### 5.1 架构：薄 SDK + 厚编排

很多人以为 OAuth 是 Claude Code 自己实现的——其实不是。**PKCE 计算、授权码交换、token 刷新的状态机骨架全在 `@modelcontextprotocol/sdk/client/auth.js`**，Claude Code 做的是**编排**：

- 实现 SDK 的 `OAuthClientProvider` 接口（`ClaudeAuthProvider`，`auth.ts:1375`），把 verifier/token 的存取回调挂到 **keychain 安全存储**。
- 编排完整的浏览器授权流程（起回调服务器、开浏览器、收 code）。
- 处理元数据发现、失败归因、跨进程刷新锁、缓存。

import 清单（`auth.ts:1`）说明了一切：

```ts
import {
  discoverAuthorizationServerMetadata,
  discoverOAuthServerInfo,
  type OAuthClientProvider,
  auth as sdkAuth,                          // ← 授权+PKCE 全流程驱动器
  refreshAuthorization as sdkRefreshAuthorization,  // ← token 刷新驱动器
} from '@modelcontextprotocol/sdk/client/auth.js'
```

`sdkAuth` 是授权码+PKCE 全流程的驱动器（发现元数据 / DCR 注册 / 构造授权 URL / code→token 交换），`sdkRefreshAuthorization` 是刷新驱动器。Claude Code 调用它们，并用 `ClaudeAuthProvider` 提供"verifier 和 token 存哪、怎么读"的实现。

### 5.2 OAuth 2.1 主流程：`performMCPOAuthFlow`

入口 `performMCPOAuthFlow`（`auth.ts:846`），编排一次完整的浏览器授权：

**① XAA 分支短路**（`auth.ts:850`）：若 `serverConfig.oauth?.xaa` 且 `isXaaEnabled()`，转 `performMCPXaaAuth`（5.5 节）并 return。注意：xaa 配置了但环境变量未开，会**硬失败而非降级**到普通 consent 流（`auth.ts:875`，安全考量）。

**② 清旧凭据**：`clearServerTokensFromLocalStorage`（`auth.ts:899`）。

**③ 选端口**：`serverConfig.oauth?.callbackPort` 优先，否则 `findAvailablePort()`（5.7 节）。

**④ 构造 provider + 发现元数据**：`new ClaudeAuthProvider(...)`（`:977`）→ `fetchAuthServerMetadata(...)`（`:988`）→ `provider.setMetadata(metadata)`。

**⑤ 起回调服务器**：`createServer` 监听 `/callback`（`:1068`），校验 `state` 防 CSRF（`:1086`），5 分钟超时（`:1193`）。

**⑥ 发起授权**：`server.listen` 回调里调 `sdkAuth(provider, {...})`（`:1178`），期待返回 `'REDIRECT'`（拿到授权 URL）。

**⑦ code→token 交换**：浏览器完成授权后回调到 `/callback`，拿到 `authorizationCode`，再次 `sdkAuth(provider, { authorizationCode, ... })`（`:1215`），期待 `'AUTHORIZED'`。

**⑧ 失败归因**（`auth.ts:1247`）：把各种失败映射到 `MCPOAuthFlowErrorReason` 枚举：

```
cancelled | token_exchange_failed | timeout | state_mismatch | provider_denied | port_unavailable | sdk_auth_failed
```

并从 `OAuthError` 子类提取 `errorCode`/`httpStatus`。特别地，`invalid_client` + "Client not found" 会**清除存储的 clientId/clientSecret**（`:1297`）——因为 DCR（动态客户端注册）的凭据可能已失效，清掉让下次重新注册。

### 5.3 元数据发现：`fetchAuthServerMetadata`

OAuth 授权前必须先知道"授权服务器在哪、token endpoint 在哪"。`fetchAuthServerMetadata`（`auth.ts:255`）按 RFC 9728 → RFC 8414 链式发现：

1. 若配置了 `oauth.authServerMetadataUrl`，强制 `https://`（`:261`），直接 GET 该 URL，`OAuthMetadataSchema.parse`。
2. 否则调 SDK 的 `discoverOAuthServerInfo(serverUrl, ...)`（`:277`），走 **RFC 9728（Protected Resource Metadata）→ RFC 8414（Authorization Server Metadata）** 两跳。失败时降级（`:283`）。
3. 兜底：仅当 URL 有 path 时（`:293`），再试 `discoverAuthorizationServerMetadata`。

scope 解析 `getScopeFromMetadata`（`auth.ts:2444`）依次尝试 `metadata.scope` → `default_scope` → `scopes_supported.join(' ')`，非标准字段优先，兼容各种 provider。

### 5.4 token 刷新：跨进程锁 + 指数退避

token 会过期，刷新是高频操作。Claude Code 的刷新逻辑解决两个工程难题：**多进程并发刷新**和**瞬时失败重试**。

`ClaudeAuthProvider.refreshAuthorization`（`auth.ts:~2095`）：

**跨进程锁**：在 `~/.claude/mcp-refresh-<sanitizedKey>.lock` 上 `lockfile.lock`，`MAX_LOCK_RETRIES` 次重试，`ELOCKED` 时退避 `1s + random*1s`。拿锁后先 `clearKeychainCache()` 重读——若**另一进程已刷新成功**（`expiresIn > 300`），直接复用，避免重复刷新。

**指数退避**：`_doRefresh`（`auth.ts:~2155`）最多 `MAX_ATTEMPTS = 3` 次重试，退避 `1s, 2s, 4s`（`:2338`）。重试条件：timeout / `ServerError` / `TemporarilyUnavailableError` / `TooManyRequestsError`（`:2320`）。

**InvalidGrant 处理**（`:2275`）：先检查别的进程是否已刷新成功；否则 `invalidateCredentials('tokens')`——因为 invalid_grant 通常意味着 refresh token 也失效了，需要重新走完整 OAuth。

**单飞去重**（`tokens()`，`:1558`）：当 `expiresIn <= 300` 且有 refreshToken 时，启动 `_refreshInProgress` 单飞——N 个并发请求只触发一次实际刷新，其余等同一个 Promise。遥测事件 `tengu_mcp_oauth_refresh_success` / `_failure`（reason 细分 `metadata_discovery_failed`/`no_client_info`/`invalid_grant`/`transient_retries_exhausted` 等）。

### 5.5 needs-auth 缓存：15 分钟短路

第 4.6 节提到远程 server 401 后缓存 15 分钟跳过连接，这里讲实现。全在 `client.ts:257-316`：

```ts
const MCP_AUTH_CACHE_TTL_MS = 15 * 60 * 1000  // client.ts:257
// 缓存文件：<ClaudeConfigHomeDir>/mcp-needs-auth-cache.json   client.ts:261
// 数据结构：Record<string, { timestamp: number }>, key = serverId
```

- **读取（single-flight memoize）**：`getMcpAuthCache()`（`:271`）用模块级 `authCachePromise` 单飞，N 个并发 `isMcpAuthCached` 共享一次文件读。
- **查询**：`isMcpAuthCached(serverId)`（`:280`）：`Date.now() - entry.timestamp < MCP_AUTH_CACHE_TTL_MS`。
- **写入（串行化）**：`setMcpAuthCacheEntry(serverId)`（`:293`）通过 `writeChain` promise 链串行化 read-modify-write，防并发竞争；写完置 `authCachePromise = null` 失效读缓存。best-effort（`.catch(() => {})`）。
- **清除**：`clearMcpAuthCache()`（`:311`）：`authCachePromise = null` + `unlink` 文件。

**何时写**：`handleRemoteAuthFailure`（`client.ts:340`）——sse/http/claudeai-proxy 在 connect 时返回 401，发 `tengu_mcp_server_needs_auth` 事件，调 `setMcpAuthCacheEntry(name)`，返回 `{ type: 'needs-auth' }`。

**何时清**：`McpAuthTool` 的 OAuth 完成回调（`McpAuthTool.ts:139`）`clearMcpAuthCache()`，让 server 立即重新连接。

这套缓存的价值：没有它，一个需要登录的 server 每 15 分钟就会被重新探测一次（连接 → 401 → 失败），每次都是网络往返 + OAuth 发现，在 print 模式下会拖慢整个启动批次。

### 5.6 XAA（Cross-App Access / SEP-990）：一次登录，N 个 server 静默认证

XAA 是 Claude Code 对标准 OAuth 的**最重要扩展**。它的价值主张（`xaa.ts:1` 注释）：

> 一次 IdP（身份提供者）浏览器登录 → N 个 MCP server **静默认证**，绕过每个 server 各自的 OAuth consent 屏幕。

#### 两层 token exchange

XAA 用两次 RFC 标准的 token exchange 把"id_token"换成"server 的 access_token"（`xaa.ts:31`）：

```ts
const TOKEN_EXCHANGE_GRANT = 'urn:ietf:params:oauth:grant-type:token-exchange'  // RFC 8693
const JWT_BEARER_GRANT     = 'urn:ietf:params:oauth:grant-type:jwt-bearer'      // RFC 7523
const ID_JAG_TOKEN_TYPE    = 'urn:ietf:params:oauth:token-type:id-jag'
const ID_TOKEN_TYPE        = 'urn:ietf:params:oauth:token-type:id_token'
```

链路（`xaa.ts:1` 注释）：
1. **IdP 层（RFC 8693 Token Exchange）**：`id_token` → `ID-JAG`（Identity JWT Authorization Grant）。实现 `requestJwtAuthorizationGrant`（`xaa.ts:232`）。
2. **AS 层（RFC 7523 JWT Bearer Grant）**：`ID-JAG` → `access_token`。实现 `exchangeJwtAuthGrant`（`xaa.ts:336`）。

编排函数 `performCrossAppAccess`（`xaa.ts:425`）：先 `discoverProtectedResource`（RFC 9728）拿 `resource` + `authorization_servers`，遍历每个 AS，跳过不支持 `jwt-bearer` grant 的 AS，依次跑两层 exchange。

#### MCP 集成入口

`performMCPXaaAuth`（`auth.ts:663`）：
1. 从 `getXaaIdpSettings()`（用户级 `settings.xaaIdp`）拿 IdP 配置（`issuer`/`clientId`/`callbackPort`）。
2. `acquireIdpIdToken(...)`（`:731`）拿 id_token——**缓存命中则静默**，不弹浏览器。
3. `discoverOidc(idp.issuer)` 拿 IdP token endpoint。
4. `performCrossAppAccess(...)`（`:752`）跑两层 exchange。
5. token 写入与普通 OAuth **同一个 keychain 路径**（`:778`），下游 transport 的 `ClaudeAuthProvider.tokens()` 无感知——这是关键设计，XAA 对 transport 层透明。

#### 静默刷新 `xaaRefresh`

`ClaudeAuthProvider.xaaRefresh()`（`auth.ts` 内私有方法）：当无 `refreshToken` 且 token 过期/将过期时，用缓存的 id_token **重跑** `performCrossAppAccess`，无需浏览器。这让 XAA server 在 token 过期后能自动续期，不像普通 OAuth 必须重新弹 consent。

#### IdP 登录 `xaaIdpLogin.ts`

`acquireIdpIdToken`（`xaaIdpLogin.ts:400`）：缓存命中直接返回；否则 `discoverOidc` → `startAuthorization`（SDK 生成授权 URL + PKCE）→ `waitForCallback`（起本地 `/callback` 服务器，socket 绑定后才开浏览器）→ `exchangeAuthorization`（code→token，必须有 `id_token`，否则提示检查 `scope=openid`）。

开关 `isXaaEnabled()`（`xaaIdpLogin.ts:31`）=`isEnvTruthy(process.env.CLAUDE_CODE_ENABLE_XAA)`，配置从 `settings.xaaIdp` 读（env-gated）。所以 XAA 是一个**需要显式启用**的企业级能力，CLI 侧有 `claude mcp xaa setup/login/show/clear`（第 12 章）。

### 5.7 `McpAuthTool`：让 LLM 代用户认证的"伪工具"

当一个 server 处于 `needs-auth` 状态，它的真实工具还没加载。但 Claude Code 仍要让 LLM **知道这个 server 存在**，并能在对话中代用户发起认证——这就是 `McpAuthTool` 的作用。

`createMcpAuthTool(serverName, config)`（`McpAuthTool.ts:49`）构造一个 `Tool`：

```ts
{
  name: buildMcpToolName(serverName, 'authenticate'),  // mcp__<server>__authenticate
  isMcp: true,
  mcpInfo: { serverName, toolName: 'authenticate' },
  inputSchema: z.object({}),  // 无需输入
  async call(...) {
    // sse/http: performMCPOAuthFlow(..., { skipBrowserOpen: true })
    // claudeai-proxy / 非 sse-http: 返回 status: 'unsupported'，让用户去 /mcp
  }
}
```

**为什么叫"伪工具"**（`McpAuthTool.ts:37` 注释）：它**冒充**一个该 server 的"真实工具"（带 `mcp__<server>__` 前缀），让模型以为该 server 已连接。一旦 OAuth 完成、`reconnectMcpServerImpl` 跑完，真实工具被 swap 进 `AppState.mcp.tools`，这个伪工具因为**共享 `mcp__<server>__` 前缀**，被 `reject(tools, t => t.name?.startsWith(prefix))` 自动清除。

**注入点**：第 4.6 节跳过连接时、以及 `client.ts:2326` 连接结果为 `needs-auth` 时，都 `tools: [createMcpAuthTool(name, config)]`。

**调用时的后台续接**（`McpAuthTool.ts:137`）：OAuth 完成 → `clearMcpAuthCache()` → `reconnectMcpServerImpl` → 用前缀替换把真实 tools/commands/resources swap 进 appState。

这个设计的精妙之处：用工具体系的命名约定（前缀）作为"伪工具自动退场"的机制，不需要额外的注册/注销逻辑。

### 5.8 `createClaudeAiProxyFetch`（认证视角）

第 3.7 节从传输角度介绍了它，这里补充**认证视角**的关键技巧。

claude.ai proxy 复用 Claude Code 自身登录 claude.ai 的 token（`getClaudeAIOAuthTokens().accessToken`），不跑独立 OAuth。问题是：多个 claude.ai connector 并发请求时，如果 token 过期，它们会**同时**收到 401，同时触发 `handleOAuth401Error`，同时清 memoize 缓存并刷新——导致连锁风暴，且所有 connector 都卡进 15 分钟 needs-auth 缓存。

`createClaudeAiProxyFetch`（`client.ts:372`）的解法是**快照发送时的 token**（`sentToken`，`:384`）：

```ts
async function doRequest() {
  await checkAndRefreshOAuthTokenIfNeeded()
  const sentToken = getClaudeAIOAuthTokens().accessToken  // ← 快照这一刻的 token
  headers.set('Authorization', `Bearer ${sentToken}`)
  return innerFetch(url, { ...init, headers })
}
const response = await doRequest()
if (response.status !== 401) return response
// 401: 只有 token 真的变了(keychain 更新/force-refresh 成功)才重试
if (handleOAuth401Error(sentToken)) return doRequest()  // 重试一次
return response
```

关键在 `handleOAuth401Error(sentToken)` 对比的是**发送时快照的 token** 而非重新读取——若重读会拿到别的 connector 刚刷新的新 token，判定 `same-as-keychain` 为 false 跳过重试，导致这个 connector 永远卡 401。注释（`:384`）明确类比 `bridgeApi.ts withOAuthRetry`。

### 5.9 OAuth 回调端口：`oauthPort.ts`

OAuth 需要本地回调服务器监听一个端口。`oauthPort.ts`（78 行）负责选端口，从 `auth.ts` 抽出来是为了**打破 `auth.ts ↔ xaaIdpLogin.ts` 的循环依赖**（`:1` 注释）。

端口范围（`oauthPort.ts:8`）：

```ts
const REDIRECT_PORT_RANGE =
  getPlatform() === 'windows'
    ? { min: 39152, max: 49151 }   // Windows 避开 IANA 动态端口段 49152-65535
    : { min: 49152, max: 65535 }
const REDIRECT_PORT_FALLBACK = 3118
```

`findAvailablePort`（`:36`）：配置端口（`MCP_OAUTH_CALLBACK_PORT`）优先；否则在范围内**随机选**（随机而非顺序，`:48` 注释说是为安全），用"试 bind"探测（`createServer`→`listen`→成功则 `close` 返回），最多 `min(range, 100)` 次；全失败试 fallback 3118；仍失败抛 `'No available ports for OAuth redirect'`。

`buildRedirectUri`（`:21`）固定 `http://localhost:${port}/callback`，引用 RFC 8252 §7.3：loopback redirect URI 只要 path 匹配，任意端口都算匹配。

### 5.10 边界细节：协议合规的几个关键决策

**Step-up 认证**（`wrapFetchWithStepUpDetection`，`auth.ts:1353`）：响应 403 且 `WWW-Authenticate` 含 `insufficient_scope` 时，正则提取 scope，标记 `markStepUpPending(scope)`。`tokens()` 据此**省略 refresh_token**（`:1545`），强制 SDK 走 PKCE 重新授权——因为 RFC 6749 §6 禁止通过 refresh 提升 scope。

**CIMD（SEP-991）**（`ClaudeAuthProvider.clientMetadataUrl`，`:1431`）：AS 声明 `client_id_metadata_document_supported: true` 时，用 URL 作为 client_id 跳过 DCR；可用 `MCP_OAUTH_CLIENT_METADATA_URL` 覆盖。

**keychain 4096 字节限制**（`saveDiscoveryState`，`:2200` 注释）：macOS `security -i` 的 stdin 行有 4096 字节限制，所以只持久化 URL 不存完整 metadata blob，否则两个 OAuth MCP server 的元数据会撑爆（#30337）。

**token 撤销**（RFC 7009）：`revokeToken` / `revokeServerTokens` / `clearServerTokensFromLocalStorage` 在删除 server 时清理凭据。

`clientMetadata` 声明 `token_endpoint_auth_method: 'none'`（public client）、`grant_types: ['authorization_code', 'refresh_token']`（`:1406`）——标准 OAuth 2.1 public client 配置。

### 5.11 认证子系统小结

认证层的设计可以概括为"**把 OAuth 的协议正确性交给 SDK，把工程的并发/缓存/安全/兼容性留给自己**"：

1. **薄 SDK**：PKCE、code 交换、刷新状态机用 SDK 的，Claude Code 只提供 keychain 存取回调。
2. **厚编排**：浏览器流程、元数据发现、跨进程刷新锁、15 分钟 needs-auth 缓存、失败归因。
3. **XAA 是杀手锏**：一次 IdP 登录解决 N 个 server 的静默认证，企业场景的核心价值。
4. **伪工具机制**：用命名前缀让 McpAuthTool 自动退场，是工具体系复用的典范。
5. **协议合规细节**：step-up scope、CIMD、keychain 限制、RFC 7009 撤销——每个边界都有对应的 RFC 约束驱动。

---

## 6. 工具发现与自动封装：`fetchToolsForClient`

这是整个 MCP 子系统的**价值落地点**——server 暴露的工具，经过这一步变成 Claude Code 工具体系里的一员。第 1.3 节说的"模板封装"在这里展开。

### 6.1 发现：`tools/list` + capability 探测

`fetchToolsForClient`（`client.ts:1745`）是 memoized（LRU）的工具发现函数，第一步就是 capability 探测：

```ts
export const fetchToolsForClient = memoizeWithLRU(
  async (client: MCPServerConnection): Promise<Tool[]> => {
    if (client.type !== 'connected') return []
    try {
      if (!client.capabilities?.tools) {   // ← 第 1.2 节的能力协商
        return []
      }
      const result = await client.client.request(
        { method: 'tools/list' },
        ListToolsResultSchema,
      ) as ListToolsResult
      // ...
    }
  })
```

`capabilities?.tools` 为假直接返回空数组——即使发 `tools/list` 也只会被拒，提前短路省一次往返。

### 6.2 Unicode sanitization：防 prompt injection 的第一道关

拿到 server 返回的工具列表后，第一件事不是封装，而是**消毒**：

```ts
const toolsToProcess = recursivelySanitizeUnicode(result.tools)
```

`recursivelySanitizeUnicode` 递归清理工具数据里的 Unicode 异常（不可见字符、RTL 覆盖符、混淆字符等）。这是安全防线——恶意 server 可能通过工具名/描述里的特殊 Unicode 字符实施 prompt injection 或视觉欺骗。在封装**之前**消毒，保证进入工具体系的都是干净数据。

### 6.3 核心：模板 override

对消毒后的每个工具，以 `MCPTool`（`tools/MCPTool/MCPTool.ts`）为模板 override。先看模板本身——它是一个**通用基座**，所有字段都标注"运行时被覆盖"：

```ts
// tools/MCPTool/MCPTool.ts
export const MCPTool = buildTool({
  isMcp: true,
  isOpenWorld() { return false },
  name: 'mcp',                              // ← 运行时覆盖为 mcp__server__tool
  maxResultSizeChars: 100_000,
  async description() { return DESCRIPTION },  // ← 运行时覆盖
  async prompt() { return PROMPT },             // ← 运行时覆盖
  inputSchema: z.object({}).passthrough(),      // MCP 工具自带 schema,允许任意输入
  async call() { return { data: '' } },         // ← 运行时覆盖为真实 callTool
  async checkPermissions() { return { behavior: 'passthrough', message: 'MCPTool requires permission.' } },
  // 渲染、截断、mapToolResultToToolResultBlockParam 等共用逻辑留在模板
})
```

`inputSchema` 用 `z.object({}).passthrough()` 是关键——MCP 工具**各自定义 schema**，模板不能用具体 schema 限制输入，必须放行。

然后在 `fetchToolsForClient`（`client.ts:1760`）里 override：

```ts
return toolsToProcess.map((tool): Tool => {
  const fullyQualifiedName = buildMcpToolName(client.name, tool.name)
  return {
    ...MCPTool,                                    // ① 复用模板全部共用逻辑
    name: skipPrefix ? tool.name : fullyQualifiedName,  // ② 真实名
    mcpInfo: { serverName: client.name, toolName: tool.name },  // ③ 反向追溯
    isMcp: true,
    searchHint: typeof tool._meta?.['anthropic/searchHint'] === 'string'
      ? tool._meta['anthropic/searchHint'].replace(/\s+/g, ' ').trim() || undefined
      : undefined,
    alwaysLoad: tool._meta?.['anthropic/alwaysLoad'] === true,
    async description() { return tool.description ?? '' },  // ④ 用 server 的描述
    async prompt() {
      const desc = tool.description ?? ''
      return desc.length > MAX_MCP_DESCRIPTION_LENGTH          // 2048
        ? desc.slice(0, MAX_MCP_DESCRIPTION_LENGTH) + '… [truncated]'
        : desc
    },
    isConcurrencySafe() { /* 判定是否可并发 */ },
    // call() 转发到 client.callTool（在别处绑定）
  }
})
```

四点 override 逐一看：

**① 复用模板**：`...MCPTool` spread 拿到渲染、权限、截断、`mapToolResultToToolResultBlockParam` 等所有共用逻辑。这是"模板封装"的物理实现。

**② 真实名**：`buildMcpToolName(client.name, tool.name)` = `mcp__<normalizedServer>__<normalizedTool>`。`skipPrefix`（SDK + `CLAUDE_AGENT_SDK_MCP_NO_PREFIX`）时用原始名，让 SDK MCP 工具能覆盖内置工具。

**③ `mcpInfo` 反向追溯**：`{ serverName, toolName }` 记录原始信息，是权限检查（第 9 章）、Agent 透传（`AgentTool.tsx:397`）、ToolSearch（`ToolSearchTool.ts:138`）的反查依据。即使 `skipPrefix` 把名字无前缀化了，`mcpInfo` 仍保留，权限检查仍走全限定名（防冒充）。

**④ 描述与截断**：用 server 的 `description`，超过 `MAX_MCP_DESCRIPTION_LENGTH = 2048`（`client.ts:218`）截断。`searchHint` 折叠空白（防 `_meta` 里的换行污染 deferred-tool 列表）。

### 6.4 名称规范化：`normalization.ts` + `mcpStringUtils.ts`

工具名必须满足 API 约束 `^[a-zA-Z0-9_-]{1,64}$`。`normalizeNameForMCP`（`normalization.ts`）把非法字符替换为 `_`：

```ts
export function normalizeNameForMCP(name: string): string {
  let normalized = name.replace(/[^a-zA-Z0-9_-]/g, '_')
  // claude.ai server 名特殊处理:折叠连续 _ + 去首尾 _,防干扰 __ 分隔符
  if (name.startsWith('claude.ai ')) {
    normalized = normalized.replace(/_+/g, '_').replace(/^_|_$/g, '')
  }
  return normalized
}
```

`mcpStringUtils.ts` 提供名字相关的全套工具：

| 函数 | 行号 | 作用 |
|------|------|------|
| `buildMcpToolName(server, tool)` | `:50` | 拼 `mcp__<server>__<tool>` |
| `getMcpPrefix(server)` | `:39` | 拼 `mcp__<server>__`（用于前缀匹配） |
| `mcpInfoFromString` | `:19` | 反解析 `mcp__server__tool` → `{serverName, toolName}`（已知限制：server 名含 `__` 会误解析） |
| `getToolNameForPermissionCheck` | `:60` | **始终返回全限定名**做权限匹配，即使 display name 无前缀化 |

`getToolNameForPermissionCheck` 是安全关键——它保证权限检查永远基于全限定名，防止 MCP 工具通过 `skipPrefix` 冒充内置工具名（如 "Write"）绕过 deny 规则。

### 6.5 工具调用：转发到 `callTool`

override 的 `call()` 最终绑定到 `client.callTool`（`client.ts:3092`）。调用时：

1. `ensureConnectedClient(client)`（`client.ts:1688`）确保连接活着（memoize 命中则 no-op）。
2. 发 `tools/call` JSON-RPC（`client.ts:3092`）。
3. 结果经 `processMcpToolResult`（`client.ts:2630`）规范化：文本直接返回，二进制 blob 持久化（第 7 章），大文本持久化（`persistId = mcp-<server>-<tool>-<ts>`，`client.ts:2769`）。

超时用 `getMcpToolTimeoutMs()`（默认"无限"，第 3.9 节）。

### 6.6 `_meta`：server 向客户端传递的元数据

MCP 工具的 `_meta` 字段是 server 给客户端的"带外信息"。Claude Code 识别两个 Anthropic 扩展字段：

- `anthropic/searchHint`：搜索提示，帮 ToolSearch 匹配。
- `anthropic/alwaysLoad`：标记为"总是加载"（区别于 deferred/lazy 工具）。

这是 MCP `_meta` 机制的实例——协议留了扩展口，Anthropic 用它传递平台特定提示。

### 6.7 自动封装小结

这一章是"为什么使用者零胶水"的根本原因。设计要点：

1. **模板承载共用**：渲染/权限/截断收敛进单一 `MCPTool`，每个工具只 override 差异。
2. **`mcpInfo` 双向追溯**：名字规范化给 LLM 看，`mcpInfo` 给系统用，权限始终基于全限定名。
3. **消毒先行**：Unicode sanitization 在封装前，防 prompt injection。
4. **`_meta` 扩展**：协议留口，平台填字段。

---

## 7. Resources 子系统

Resources 是 MCP 的第三种原语（除 tools/prompts 外），代表"server 提供的可读数据"（文件、数据库行、配置等）。Claude Code 的 resources 实现有一个反直觉的设计：**不是每个 resource 一个工具**。

### 7.1 两个通用工具：全局注入一次

`client.ts:2342` 连接成功后，只有当**某个 server** 支持 resources 且尚未注入时，才把两个工具**一次性**加入全局工具集：

```ts
const supportsResources = !!client.capabilities?.resources
// ...
if (supportsResources && !resourceToolsAdded) {
  // 注入 ListMcpResourcesTool + ReadMcpResourceTool
  resourceToolsAdded = true  // ← 全局 flag，只注入一次
}
```

也就是说，无论有多少个支持 resources 的 server，全局永远只有**两个**资源工具：`ListMcpResources` 和 `ReadMcpResource`。它们通过参数（`server`/`uri`）路由到具体 server 的具体 resource，而不是为每个 resource 生成独立工具。

这与工具（tools）的封装方式（每个 tool 一个 `mcp__server__tool`）形成鲜明对比——resources 数量可能极大（一个文件系统 server 可能有上万个文件 resource），逐个生成工具会撑爆 LLM 的工具表，所以用"两个通用工具 + 参数路由"。

### 7.2 `ListMcpResourcesTool`：列出所有资源

`tools/ListMcpResourcesTool/ListMcpResourcesTool.ts:66`：

```ts
async call({ server }) {
  // 对每个 connected client 调 ensureConnectedClient + fetchResourcesForClient(fresh)
  // Promise.all 并发，单个失败不拖垮整体
  // 返回扁平化 {uri, name, mimeType, description, server}[]
}
```

注释（`:79`）：`fetchResourcesForClient` 是 LRU 缓存（按 server name），启动预取已热，在 `onclose` 和 `resources/list_changed` 通知时失效。`server` 参数可选，用于过滤单个 server。

### 7.3 `ReadMcpResourceTool`：读取资源 + blob 持久化

`tools/ReadMcpResourceTool/ReadMcpResourceTool.ts:80`：

```ts
async call({ server, uri }) {
  // 校验 client 存在、type==='connected'、capabilities?.resources
  const connectedClient = await ensureConnectedClient(client)
  const result = await connectedClient.client.request(
    { method: 'resources/read', params: { uri } },
    ReadResourceResultSchema,
  )
  // 对每个 content:
  //   有 text → 直接返回
  //   是 base64 blob → 生成 persistId，调 persistBinaryContent，返回 blobSavedTo
}
```

**blob 持久化**（`:106`）：二进制 resource（图片、PDF 等）不能塞进 LLM 文本上下文，所以落盘到 `getToolResultsDir()`，返回文件路径：

```ts
const persistId = 'mcp-resource-' + Date.now() + '-' + i + '-' + rand  // :114
persistBinaryContent(Buffer.from(blob, 'base64'), mimeType, persistId)
// 返回 { uri, mimeType, blobSavedTo, text: getBinaryBlobSavedMessage(...) }
```

`persistBinaryContent`（`utils/mcpOutputStorage.ts:148`）按 mimeType 选扩展名（`extensionForMimeType`，`:66`，支持 pdf/json/csv/txt/html/md/zip/docx/xlsx/pptx/mp3/wav/mp4/png/jpg/gif/webp/svg，未知→`bin`），写到 `${persistId}.${ext}`。

### 7.4 `fetchResourcesForClient`：LRU 缓存

`client.ts:2000`（`memoizeWithLRU`，key = `client.name`）：

```ts
if (!client.capabilities?.resources) return []
const result = await client.client.request(
  { method: 'resources/list' }, ListResourcesResultSchema,
)
return result.resources.map(r => ({ ...r, server: client.name }))
```

每条 resource 打上 `server` 标签，`ListMcpResourcesTool` 据此过滤。

### 7.5 动态刷新：`list_changed` 而非 `subscribe`（重要事实）

MCP resources 有两种动态更新机制：`resources/subscribe`（推模型，server 主动推送变化）和 `resources/list_changed`（拉模型，server 通知"清单变了"，客户端重新 list）。

**Claude Code 只用后者**。证据：

- **subscribe 只探测不调用**：`client.ts:1179` 连接后日志记录 `hasResourceSubscribe: !!capabilities?.resources?.subscribe`，但全代码库 grep `resources/subscribe` / `subscribeToResource` **零调用命中**。订阅能力仅作日志展示。
- **实际用 list_changed**：`useManageMCPConnections.ts:705`，条件 `capabilities?.resources?.listChanged`，收到通知 → `fetchResourcesForClient.cache.delete(client.name)` 失效 LRU → 下次访问重新 `resources/list`。

设计理由：`list_changed` 是粗粒度的"清单变了"信号，实现简单（失效缓存重新拉），对 Claude Code 的资源使用模式（按需读）足够；`subscribe` 的细粒度推送在"LLM 偶尔读资源"的场景下收益不大，反而增加连接状态复杂度。

### 7.6 工具结果的二进制持久化（区别于 resource）

除了 `ReadMcpResource` 的 resource blob，**工具调用结果**返回的二进制也单独持久化。`persistBlobToTextBlock`（`client.ts:2598`）：

```ts
const persistId = 'mcp-' + normalizeNameForMCP(serverName) + '-blob-' + Date.now() + '-' + rand  // :2604
persistBinaryContent(...)
```

注意三套 persistId 前缀的区别：

| 来源 | persistId 前缀 | 位置 |
|------|---------------|------|
| `ReadMcpResource` 读 resource | `mcp-resource-...` | `ReadMcpResourceTool.ts:114` |
| 工具调用返回二进制 blob | `mcp-<server>-blob-...` | `client.ts:2604` |
| 工具调用返回大文本 | `mcp-<server>-<tool>-<ts>` | `client.ts:2769` |

前缀区分让落盘文件可追溯来源。

### 7.7 Resources 小结

Resources 子系统的核心设计是**"两个通用工具 + 参数路由"替代"逐 resource 工具"**——这是对 resources 可能海量特性的务实妥协。配合 LRU 缓存 + `list_changed` 被动刷新，在"按需读取"场景下实现低开销、高及时性。

---

## 8. Prompts 与 MCP Skill

Prompts 是 MCP 的第二种原语，代表"server 提供的可复用提示模板"。在 Claude Code 里，prompts 有两条命运：要么作为普通 prompt（仅 `/mcp` 菜单可见），要么被"提升"为 MCP skill（进 `Skill` 工具、`/skills` 可见）。

### 8.1 `prompts/list` → Command 转换

`fetchCommandsForClient`（`client.ts:2044`）拉取 server 的 prompts 并转成 Claude Code 的 `Command` 格式：

```ts
const result = await client.client.request(
  { method: 'prompts/list' }, ListPromptsResultSchema,
)
if (!result.prompts) return []
const promptsToProcess = recursivelySanitizeUnicode(result.prompts)  // 同样先消毒

return promptsToProcess.map(prompt => {
  const argNames = Object.values(prompt.arguments ?? {}).map(k => k.name)
  return {
    type: 'prompt' as const,
    name: 'mcp__' + normalizeNameForMCP(client.name) + '__' + prompt.name,
    description: prompt.description ?? '',
    hasUserSpecifiedDescription: !!prompt.description,
    contentLength: 0,                    // ← 动态内容,无静态长度
    isEnabled: () => true,
    isMcp: true,                         // ← 标记为 MCP 来源
    source: 'mcp',
    userFacingName() { return `${client.name}:${prompt.name} (MCP)` },
    argNames,
    async getPromptForCommand(args) {    // ← 动态拉取
      const result = await connectedClient.client.getPrompt({
        name: prompt.name,
        arguments: zipObject(argNames, argsArray),
      })
      // transformResultContent 后返回
    },
  }
})
```

注意这条转换路径设置的是 `isMcp: true`、`source: 'mcp'`，但**没有**设置 `loadedFrom: 'mcp'`——这是它和 MCP skill 的关键区别（8.2 节）。

`contentLength: 0` 和"动态 `getPromptForCommand`"是 MCP prompt 与文件系统 skill 的本质差异：prompt 内容不存在本地，每次调用实时 `client.getPrompt` RPC 拉取。

### 8.2 MCP prompt vs MCP skill：`loadedFrom === 'mcp'` 分界

`utils.ts:78` 有一段决定性注释，直接说明两者的分界：

```ts
/**
 * Filters MCP **prompts** (not skills) by server. Used by the `/mcp` menu
 * capabilities display — skills are a separate feature shown in `/skills`,
 * so they mustn't inflate the "prompts" capability badge.
 *
 * The distinguisher is `loadedFrom === 'mcp'`: MCP skills set it, MCP
 * prompts don't (they use `isMcp: true` instead).
 */
```

也就是说：

- **MCP prompt**：`isMcp: true`，`loadedFrom` 为空。只在 `/mcp` 菜单的 prompt 能力徽章里显示，**不进** `Skill` 工具清单。
- **MCP skill**：`loadedFrom: 'mcp'`。进 `/skills` 菜单和 `Skill` 工具清单。

这就是为什么 `SkillTool.getAllCommands`（`SkillTool.ts:73`）精确过滤 `cmd.type === 'prompt' && cmd.loadedFrom === 'mcp'`——只有被标记为 skill 的 MCP prompt 才进 `Skill` 工具。

`LoadedFrom` 类型（`loadSkillsDir.ts:67`）把 `'mcp'` 列为独立来源：

```ts
export type LoadedFrom =
  | 'commands_DEPRECATED' | 'skills' | 'plugin' | 'managed' | 'bundled' | 'mcp'
```

### 8.3 MCP skill 提升：`fetchMcpSkillsForClient` + feature flag

把 MCP prompt "提升"为 MCP skill，需要走另一条路径，受开关控制（`client.ts:117`）：

```ts
const fetchMcpSkillsForClient = feature('MCP_SKILLS')
  ? (require('../../skills/mcpSkills.js') as typeof import('../../skills/mcpSkills.js'))
      .fetchMcpSkillsForClient
  : undefined
```

只有 `feature('MCP_SKILLS')` 开启时，`fetchMcpSkillsForClient` 才会把 server 的 prompt 用 `createSkillCommand`（`loadSkillsDir.ts:270`）包成 `loadedFrom: 'mcp'` 的 skill。

> ⚠️ **范围说明**：本源码快照中 `src/skills/mcpSkills.ts` **文件不存在**（`ls` 确认 No such file，全盘 `find` 未命中），但引用链完整——`client.ts:119`、`useManageMCPConnections.ts:24`、`skills/mcpSkillBuilders.ts`（为其注册 builder）都 require/引用它。这是 feature-gated 的实验性能力，本版本要么未启用、要么该文件被剥离。因此：**在当前这份代码里，MCP server 的 prompt 默认只是 prompt，不会自动变成 skill**。下述结论基于调用方引用反推。

### 8.4 `mcpSkillBuilders`：写一次注册表解循环依赖

`skills/mcpSkillBuilders.ts` 是一个**精巧的依赖环破解**。问题：`mcpSkills.ts`（提升 MCP prompt 为 skill）需要 `loadSkillsDir.ts` 的 `createSkillCommand`/`parseSkillFrontmatterFields`，但 `loadSkillsDir.ts` 又传递依赖到 `client.ts`，而 `client.ts` 依赖 `mcpSkills.ts`——形成环。

解法是**写一次注册表**（`mcpSkillBuilders.ts`）：

```ts
export type MCPSkillBuilders = {
  createSkillCommand: typeof createSkillCommand
  parseSkillFrontmatterFields: typeof parseSkillFrontmatterFields
}
let builders: MCPSkillBuilders | null = null
export function registerMCPSkillBuilders(b: MCPSkillBuilders): void { builders = b }
export function getMCPSkillBuilders(): MCPSkillBuilders {
  if (!builders) throw new Error('MCP skill builders not registered...')
  return builders
}
```

`loadSkillsDir.ts` 模块初始化时（`mcpSkillBuilders.ts` 注释说明它在启动时经 `commands.ts` 的静态 import 被求值）调 `registerMCPSkillBuilders` 把这两个函数注册进去；`mcpSkills.ts` 通过 `getMCPSkillBuilders()` 取用。这个叶子模块不 import 任何实现，只持有类型，从而打破环。

注释还提到一个失败方案：`await import(variable)` 动态 import 在 Bun 打包的二进制里会失败（specifier 解析到 `/$bunfs/root/...`），所以必须用这种静态注册表。

### 8.5 动态 vs 静态：MCP skill 与文件 skill 的本质差异

三者最终都汇入 `Skill` 工具（统一终点），但加载层完全不同：

| 维度 | 文件系统 skill | MCP prompt | MCP skill |
|------|---------------|-----------|-----------|
| 来源 | `SKILL.md` 文件 | server `prompts/list` | server `prompts/list`（提升路径） |
| `loadedFrom` | `skills`/`bundled`/`plugin`/`managed` | 无 | `mcp` |
| 内容 | **静态**（本地文本） | **动态**（`getPrompt` RPC） | **动态** |
| 加载时机 | 启动时扫目录 | server 连上后 | server 连上后 + feature flag |
| feature gate | 无 | 无 | `feature('MCP_SKILLS')` |
| `contentLength` | 实际字节数 | `0` | `0` |
| 进 `Skill` 工具 | 是 | 否 | 是 |
| `/skills` 可见 | 是 | 否 | 是 |

对 LLM 来说，本地 `SKILL.md` skill 和 MCP skill 是同一个 `Skill` 工具下的同类条目，调用方式完全一致（`Skill(skill="...")`）。区别全在加载层——这正是"统一终点，差异源头"的设计。

### 8.6 Prompts 与 MCP Skill 小结

这一章揭示了 MCP 与 Skill 两个子系统的**交汇点**。设计要点：

1. **`loadedFrom === 'mcp'` 是唯一分界**：同一个 MCP prompt，设了这个标记就是 skill，否则只是 prompt。
2. **feature gate 隔离实验**：MCP skill 提升是实验功能，用 `feature('MCP_SKILLS')` + 独立文件（`mcpSkills.ts`）隔离，不影响主流程。
3. **写一次注册表破解依赖环**：`mcpSkillBuilders.ts` 是模块化设计的典范——用最小代价（一个叶子模块）解决复杂依赖环。
4. **动态内容是 MCP skill 的本质优势**：server 改了 prompt，下次调用立即生效，文件 skill 做不到。

---

## 9. 权限与批准：两层模型

MCP 的权限治理是**两层独立**的模型，理解这个分层是避免混淆的关键。两层的触发时机、数据存储、UI 都不同。

### 9.1 层 A：project MCP server 的首次批准

当一个 `.mcp.json` 里的 server **首次出现**（团队 clone 了带 `.mcp.json` 的仓库），Claude Code 不会自动信任它——因为 `.mcp.json` 是提交到 git 的，恶意仓库可能在里面藏一个危险 server。首次批准机制就是为了防这个威胁。

**状态判定** `getProjectMcpServerStatus`（`utils.ts:351`）返回三态：

```ts
// rejected: server 名(经 normalizeNameForMCP)在 settings.disabledMcpjsonServers 中  (utils.ts:359)
// approved: 在 enabledMcpjsonServers 中,或 enableAllProjectMcpServers === true        (utils.ts:367)
// 其余 → 'pending'
```

**bypass 模式的安全细节**（`utils.ts:386`）：在跳过权限提示的模式下，自动批准条件是 `hasSkipDangerousModePermissionPrompt() && isSettingSourceEnabled('projectSettings')`——**故意不读 projectSettings 的实际值**，防止仓库通过 projectSettings 配置代用户同意自己的 server。非交互模式同理（`:398`）。这是"不信任仓库自身声明"的安全姿态。

**触发入口** `handleMcpjsonServerApprovals`（`services/mcpServerApproval.tsx:15`）：取 `getMcpConfigsByScope('project')` 中 `status === 'pending'` 的 server，1 个渲染 `MCPServerApprovalDialog`，多个渲染 `MCPServerMultiselectDialog`。

**批准选项** `MCPServerApprovalDialog`（`components/MCPServerApprovalDialog.tsx:24`）三个值：

| 值 | label | 写入 |
|----|-------|------|
| `yes` | "Use this MCP server" | `localSettings.enabledMcpjsonServers`（`:28`） |
| `yes_all` | "Use this and all future MCP servers in this project" | 同 yes + `enableAllProjectMcpServers = true`（`:35`） |
| `no` | "Continue without using this MCP server" | `localSettings.disabledMcpjsonServers`（`:43`）；`onCancel` 也映射为 `no`（`:66`） |

多 server 批准 `MCPServerMultiselectDialog`（`components/MCPServerMultiselectDialog.tsx:25`）用 `partition` 拆选中/未选中，分别写 enabled/disabled；ESC 全部 reject（`:56`）。

注意：这层写的都是 **localSettings** 的 `enabledMcpjsonServers`/`disabledMcpjsonServers`/`enableAllProjectMcpServers`，与第 2.4 节的 projectConfig `enabled/disabledMcpServers`（运行时开关）是**两组不同的 key**。

### 9.2 层 B：运行时工具调用权限

每次 LLM 实际调用某个 MCP 工具时，还要过一道运行时权限。MCP 工具的 `checkPermissions`（`client.ts:1814`）：

```ts
async checkPermissions(input, context): Promise<PermissionDecision> {
  return {
    behavior: 'passthrough',                       // ← 不自带决策,交给通用流水线
    message: 'MCPTool requires permission.',
    addRules: [{                                    // ← 建议持久化规则
      ruleContent: fullyQualifiedName,              //    规则名 = mcp__server__tool
      behavior: 'allow',
      destination: 'localSettings',
    }],
  }
}
```

`passthrough` 表示 MCP 工具不自带权限决策，而是交给 Claude Code 的**通用权限流水线**（与 Bash/Write 共用）。通用流水线是四态判定：`allow` / `deny` / `ask` / `passthrough`（见 `permissions.ts` 与 `interactiveHandler.ts`）。MCP 工具默认走 `ask` → 进入交互式权限对话框。

**对话框选项**（`components/permissions/FilePermissionDialog/permissionOptions.tsx:41`）：

| 选项 | 语义 | 持久化 |
|------|------|--------|
| `accept-once` | 本次放行 | 不持久化（`:86`） |
| `accept-session` | 会话内不再问 | 写 **session 内存** allow 规则，非持久化（`:143`） |
| `reject` | 拒绝 | —（`:167`） |

> **重要澄清**：本代码库**没有字面量 `'always'` 状态**。等价物是 `accept-session`（会话内免问）以及通过 `addRules` suggestion 写入 settings 的持久化 allow 规则（`interactiveHandler.ts:154` 的 `onAllow` + `PermissionContext.ts:139` 的 `persistPermissions`）。channel 回复按 `allow`/`deny` 二态处理（`interactiveHandler.ts:376`），`permanent: false`。

层 B 的批准结果可以通过 `addRules`（层 A 的 `checkPermissions` 已带 suggestion）持久化到 localSettings，下次同 server 同工具自动 allow。

### 9.3 完整权限链：调用一次 MCP 工具的全过程

把两层串起来，调用一次 MCP 工具的完整权限链：

```text
1. .mcp.json server 首次出现
   → getProjectMcpServerStatus === 'pending'
   → handleMcpjsonServerApprovals 弹 MCPServerApprovalDialog (yes/yes_all/no)
   → 写 localSettings enabled/disabledMcpjsonServers                     【层 A】

2. server 加载
   → getAllMcpConfigs / client.ts 经 isMcpServerDisabled (读 projectConfig disabledMcpServers) 过滤
   → 禁用者不连接

3. 工具池组装
   → filterToolsByDenyRules (tools.ts:262) 用 mcp__server server 级 deny 规则 strip 整 server 工具  【9.4】

4. 单次调用
   → MCP tool checkPermissions 返回 passthrough (client.ts:1814)
   → hasPermissionsToUseTool 判 allow/deny/ask
   → ask 走 interactiveHandler → 对话框 accept-once/accept-session/reject
   → (+ 可选 channel relay 竞速,见 9.6)
   → onAllow/onReject 回写 permission rules                              【层 B】
```

### 9.4 `mcp__` 前缀与 server 级 strip

`mcp__server__tool` 命名约定在权限层有双重作用。

**工具池级 strip**（`tools.ts:253` 的 `filterToolsByDenyRules`）：组装给模型的工具池时，对每个工具调 `getDenyRuleForTool`。命中 deny 规则的工具**在模型看到工具列表前就被剔除**，而不是调用时才拦截。这意味着一条 `mcp__server1` 的 deny 规则会让整个 server1 的工具**从工具池消失**。

**server 级规则匹配**（`permissions.ts:238` 的 `toolMatchesRule`）：

```ts
// :251  始终用全限定名做匹配
const nameForRuleMatch = getToolNameForPermissionCheck(tool)
// :254  直接全名匹配
if (ruleName === nameForRuleMatch) return true
// :258  server 级规则:规则形如 mcp__server1 或 mcp__server1__*
const { serverName: ruleServer, toolName: ruleTool } = mcpInfoFromString(ruleName)
const { serverName: toolServer } = mcpInfoFromString(nameForRuleMatch)
if (ruleServer === toolServer && (ruleTool === undefined || ruleTool === '*')) {
  return true  // 一条 mcp__server1 deny → strip server1 所有工具
}
```

**`getToolNameForPermissionCheck` 始终返回全限定名**（`mcpStringUtils.ts:60`）是安全关键——即使 `skipPrefix` 模式让工具的 display name 是无前缀的 "Write"（来自某 SDK MCP server），权限检查仍用 `mcp__sdkserver__Write` 全限定名匹配，防止 MCP 工具冒充内置 `Write` 绕过 deny 规则。

### 9.5 disable/启用 server：`isMcpServerDisabled`

运行时 enable/disable（`/mcp` 菜单 toggle）走的是另一组 key。`isMcpServerDisabled`（`config.ts:1528`）：

- **builtin**（`DEFAULT_DISABLED_BUILTIN`，CHICAGO_MCP 的 computer-use server，`:1512`）：默认禁，必须在 `enabledMcpServers` 中才启用。
- **普通**：name 在 `disabledMcpServers` 中即禁用。

`setMcpServerEnabled`（`config.ts:1553`）经 `saveCurrentProjectConfig` 写 projectConfig，builtin 改 `enabledMcpServers`，普通改 `disabledMcpServers`（enabled=true 即移除）。builtin 状态切换记 `tengu_builtin_mcp_toggle` 遥测。

连接层体现：`client.ts:2237` 连接前分区，`isMcpServerDisabled` 的 server 不进 batch，直接 `onConnectionAttempt({ type: 'disabled' })`，不发 HTTP/spawn。

### 9.6 Channels 功能澄清（⚠️ 非普通 MCP 权限）

`services/mcp/` 下有 `channelAllowlist.ts` / `channelPermissions.ts` / `channelNotification.ts` 三个文件，名字容易让人以为是"普通 MCP 工具的白名单/权限"。**它们不是**。它们是 **"Channels" 功能**——把 Telegram/iMessage/Discord 等 MCP server 当作**消息通道**，把 Claude Code 的权限审批提示**转发到手机**等渠道，与本地对话框/bridge/hook/classifier **竞速**，先返回者胜（`channelPermissions.ts:1` 注释）。

三个文件分工：

| 文件 | 作用 | 开关 |
|------|------|------|
| `channelAllowlist.ts` | **谁能当 channel**：只有 `{marketplace, plugin}` 在已批准 channel plugin 清单内的 plugin channel server 才允许注册通知 handler。`isChannelAllowlisted`（`:67`）是**纯函数预过滤，非安全边界**（`:59` 注释） | GrowthBook `tengu_harbor_ledger`（`:37`）；企业可用 managed `allowedChannelPlugins` 替换 |
| `channelPermissions.ts` | **channel 能不能转发权限审批 + 如何收发**：本地弹权限对话框时，同时把 prompt 发到手机，竞速。`createChannelPermissionCallbacks`（`:209`）持 `pending: Map<requestId, resolver>`；`shortRequestId`（`:140`）生成 5 字母短 ID（FNV-1a 哈希 + 脏词屏蔽）；回复正则 `^\s*(y\|yes\|n\|no)\s+([a-km-z]{5})\s*$`（`:75`） | GrowthBook `tengu_harbor_permissions`（`:36`），独立于 `tengu_harbor` |
| `channelNotification.ts` | **三条 notification 协议**：`notifications/claude/channel`（server→CC 入站消息，`:37`）、`notifications/claude/channel/permission`（server→CC 权限回复，`:62`）、`notifications/claude/channel/permission_request`（CC→server 出站请求，`:85`）。`gateChannelServer`（`:191`）串联 capability → flag → OAuth → org policy → session allowlist | — |

**关键结论**：Channels 是一个受 GrowthBook flag（`tengu_harbor` 系列）控制的 **experimental 功能**，与"普通 MCP 工具的权限检查"是两回事。普通 MCP 工具的权限走的是第 9.2 节的通用四态流水线 + `mcp__` 前缀规则。文档读者不要把 `channelPermissions` 误认为"MCP 工具权限管理器"。

`channelPermissions` 的竞速逻辑（`interactiveHandler.ts:316`）：feature gate（`KAIROS`/`KAIROS_CHANNELS`）+ channelCallbacks 存在 + tool 不需用户交互 → 生成 `shortRequestId` → `filterPermissionRelayClients` 过滤客户端（需声明 `claude/channel` **和** `claude/channel/permission` 两个 experimental capability）→ 对每个发 `CHANNEL_PERMISSION_REQUEST_METHOD` notification（fire-and-forget），`input_preview` 截到 200 字符（`channelPermissions.ts:160`）。

### 9.7 权限层小结

权限层的核心是**两层独立 + 命名约定驱动**：

1. **层 A（首次批准）防仓库投毒**：`.mcp.json` 是 git 提交的，首次必须用户同意；bypass 模式故意不读 projectSettings 防仓库自证。
2. **层 B（运行时权限）复用通用流水线**：MCP 工具 `passthrough` 到与 Bash/Write 共用的四态判定。
3. **`mcp__` 前缀是权限的骨架**：server 级规则靠它 strip 整 server 工具；全限定名匹配防冒充。
4. **两组 key 不混淆**：`McpjsonServers`（localSettings，首次批准）vs `McpServers`（projectConfig，运行时开关）。
5. **Channels 是另一回事**：消息通道转发审批，experimental，别和工具权限搞混。

---

## 10. Elicitation：server→client 请求输入

Elicitation 是 MCP 一个相对新的原语——允许 **server 主动向 client（用户）请求输入**。典型场景：server 执行工具时需要用户确认一个选项、填一个表单、或完成一个 URL 认证流程。这让 MCP 工具从"单向执行"变成"可交互"。

### 10.1 协议：`elicitation/create`

协议方法是 `elicitation/create`（MCP 规范）。Claude Code 用 SDK 的 `ElicitRequestSchema` 注册 handler（`elicitationHandler.ts:5`），而非手写字符串。完成通知（URL 模式）用 `ElicitationCompleteNotificationSchema`（`:175`）。

结果动作 `ElicitResult.action ∈ {'accept', 'decline', 'cancel'}`（`elicitationHandler.ts:40`），`accept` 带 `content`。

### 10.2 capability 声明

如第 4.2 节所述，Client 构造时声明 `capabilities: { elicitation: {} }`（`client.ts:985`）。注意用**空对象**而非 `{form:{},url:{}}`——为兼容 Java Spring AI MCP SDK 的零字段 Elicitation 类（`:996` 注释）。client 必须先声明这个 capability，否则 `setRequestHandler` 会抛错。

### 10.3 REPL 路径：`registerElicitationHandler`

主路径在 `elicitationHandler.ts:68` 的 `registerElicitationHandler`，注册 `client.setRequestHandler(ElicitRequestSchema, ...)`。流程：

```text
server 发 elicitation/create
  → runElicitationHooks(...)  (elicitationHandler.ts:214)
      ↳ hook 可直接返回 ElicitResult 提前短路 (:96)
  → 否则 push 进 AppState.elicitation.queue (:127)
      事件带 serverName/requestId/params/signal/waitingState/respond/onWaitingDismiss
      respond 是闭包,调用时 resolve 一个 Promise (:114-153)
  → await response (:154)
  → runElicitationResultHooks(...) (:159, :264)
      ↳ hook 可改写结果或降级为 decline
  → 返回 ElicitResult 给 server
```

**signal abort**（`:115`）：`extra.signal` abort 时自动返回 `{action: 'cancel'}`。

**URL 模式完成通知**（`:175`）：额外注册 `ElicitationCompleteNotificationSchema` handler，收到 server 的完成通知时，在 queue 中把对应事件打 `completed: true`，`findElicitationInQueue` 按 `serverName + elicitationId` 定位（`:54`）。

**错误兜底**（`:167`）：catch 返回 `{action: 'cancel'}`；client 无 elicitation capability 时静默 return（`:208`）。

### 10.4 初始化期占位 handler

在 `registerElicitationHandler` 被 `useManageMCPConnections` 覆盖之前有一个时间窗口。`client.ts:1188` 注册一个返回 `{action: 'cancel'}` 的**默认 handler**，避免初始化期的 elicitation 无响应。这是典型的"先占位、后覆盖"防御性编程。

### 10.5 Print/SDK 路径：错误码 -32042

非 REPL（print/SDK）模式没有交互 UI，走另一条路径（`client.ts:2803`）：当工具调用返回 JSON-RPC 错误 `-32042` 且带 `elicitations` 数组时，逐个处理 URL elicitation（打印 URL / 调 `handleElicitation` 回调 / 入 queue），完成后**重试工具调用**。REPL 模式在此处把 URL elicitation 也 push 进同一个 `elicitation.queue`（`client.ts:2966`）。

错误码 `-32042` 是 MCP 规范定义的 "Server doesn't support a required feature, needs elicitation" 语义。

### 10.6 UI：`ElicitationDialog`

`components/mcp/ElicitationDialog.tsx` 按 `event.params.mode` 分派：

- **`mode === 'url'`** → `<ElicitationURLDialog>`（`:121`）：**两阶段**——consent（用户确认要打开 URL）→ waiting（等待 server 的完成通知）。
- **否则** → `<ElicitationFormDialog>`（`:133`）：渲染 `requestedSchema` 字段表单（文本/枚举/多选/日期时间等），校验用 `utils/mcp/elicitationValidation.ts`。

用户操作回调 `onResponse(action, content?)`：`accept`（带 content）、`decline`、`cancel`。两个 dialog 都用 `useRegisterOverlay('elicitation')` / `('elicitation-url')` 管理覆盖层，`useNotifyAfterTimeout` 防止用户错过提示。

### 10.7 queue → UI 连接

`screens/REPL.tsx:4721`：`focusedInputDialog === 'elicitation'` 时渲染 `ElicitationDialog`，`event` 取 `elicitation.queue[0]`。`onResponse` 调 `currentRequest.respond({action, content})`（`:4725`）resolve Promise；URL accept 不立即出队（保留 phase-2 waiting，`:4730`），其余 `slice(1)` 出队。

### 10.8 Hook 集成

Elicitation 在两个点集成 hook（可被用户配置拦截/改写）：

- **请求前** `runElicitationHooks`（`:214`）：hook 可直接返回 `ElicitResult` 短路，跳过 UI。
- **结果后** `runElicitationResultHooks`（`:264`）：hook 可改写或降级结果（如强制 `decline`）。

这让企业可以用 hook 策略统一管控 elicitation（如自动拒绝某些 server 的表单请求）。

### 10.9 Elicitation 小结

Elicitation 把 MCP 工具从"单向调用"升级为"可交互对话"。设计要点：

1. **两条路径覆盖两种模式**：REPL 走 queue + UI 弹窗，print/SDK 走错误码 -32042 + URL。
2. **capability 空对象兼容**：跨语言 SDK 兼容性的典型取舍。
3. **hook 双点拦截**：请求前可短路，结果后可改写，企业可管控。
4. **占位 handler 防初始化期空窗**：防御性编程的细节。

---

## 11. 官方 Registry 与安全治理

MCP server 是外部代码，天然需要多层安全治理。这一章讲 Claude Code 在"信任判定、动态 header、输出治理"上的三道防线。

### 11.1 官方 Registry：`officialRegistry.ts`

Claude Code 维护一份"官方 MCP server"清单，用于在某些场景给予不同的信任/呈现。`prefetchOfficialMcpUrls`（`officialRegistry.ts:33`）启动时 fire-and-forget 拉取：

```ts
const response = await axios.get<RegistryResponse>(
  'https://api.anthropic.com/mcp-registry/v0/servers?version=latest&visibility=commercial',
  { timeout: 5000 },
)
```

取每个 entry 的 `server.remotes[].url`，经 `normalizeUrl`（去 query、去尾 `/`，`:19`）后塞进 `officialUrls: Set<string>`。

查询 `isOfficialMcpUrl(normalizedUrl)`（`:66`）：

```ts
export function isOfficialMcpUrl(normalizedUrl: string): boolean {
  return officialUrls?.has(normalizedUrl) ?? false  // ← fail-closed
}
```

`officialUrls` 未加载时 **fail-closed 返回 false**（`:62` 注释）——宁可都不认作 official，也不在 registry 未就绪时误判。

**关闭开关**：`process.env.CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC`（`:34`）为真时跳过拉取——这是企业/离线环境的"禁止非必要网络请求"开关。失败仅 debug 日志，不抛（`:55`）。

### 11.2 `headersHelper`：动态获取 header（git credential-helper 风格）

有些 MCP server 的认证 header 是**动态**的（如定期刷新的 token），不适合写死在配置里。`headersHelper` 机制允许配置一个**外部脚本**，每次连接前执行它拿 header——灵感来自 git 的 credential-helper。

配置（`McpSSEServerConfigSchema` / `McpHTTPServerConfigSchema` 的 `headersHelper` 字段）：

```json
{ "type": "http", "url": "...", "headersHelper": "/usr/local/bin/my-mcp-auth.sh" }
```

`getMcpHeadersFromHelper`（`headersHelper.ts:32`）：

```ts
// 若 config.headersHelper 存在,execFileNoThrowWithCwd(shell, 10s 超时)执行
// 传入环境变量:
//   CLAUDE_CODE_MCP_SERVER_NAME = <server 名>
//   CLAUDE_CODE_MCP_SERVER_URL  = <server url>     ← 让一个脚本服务多 server
// stdout 须为 JSON object,所有 value 须为 string
// 否则返回 null(不阻断连接)
```

**安全检查**（`:42`）：若 config 来自 **project/local scope 且非交互模式**，必须先 `checkHasTrustDialogAccepted()`，否则拒绝执行并记 `tengu_mcp_headersHelper_missing_trust` 事件。这防止仓库通过 `.mcp.json` 的 `headersHelper` 字段诱导用户执行任意脚本——必须先信任仓库。

合并 `getMcpServerHeaders`（`:125`）：`staticHeaders`（config.headers）与 dynamic 合并，**dynamic 覆盖 static**。这让用户可以静态写部分 header（如 `X-Tenant`），动态取敏感 header（如 `Authorization`）。

### 11.3 `mcpValidation`：输出 token 截断

MCP 工具的输出可能极大（一个查询返回百万行），直接塞进 LLM 上下文会撑爆。`mcpValidation.ts` 负责 token 治理。

**预算** `getMaxMcpOutputTokens`（`:26`）优先级：

```text
env MAX_MCP_OUTPUT_TOKENS  →  GrowthBook tengu_satin_quoll.mcp_tool  →  默认 25000
```

**截断判定** `mcpContentNeedsTruncation`（`:151`）用**两阶段**避免每次都精确计 token（昂贵）：

1. **启发式估算**（`getContentSizeEstimate`，`:59`）：文本按字符估算，image 按 1600 token。与阈值 `MCP_TOKEN_COUNT_THRESHOLD_FACTOR = 0.5`（预算的一半）比较。
2. 超过启发式阈值才调 `countMessagesTokensWithAPI` 精确计 token。

**截断执行** `truncateMcpContent` / `truncateMcpContentIfNeeded`（`:180`）：超限时截断字符串或 content blocks；**image 会尝试 `compressImageBlock` 压缩**塞进剩余预算（`:113`）；追加截断提示语（`:81`）。

这个两阶段设计很务实——大多数工具输出远小于预算，启发式估算就够了，只有接近阈值的才付出精确计 token 的成本。

### 11.4 超时矩阵（汇总）

第 3.9 节已列出 transport 层超时，这里汇总 MCP 全链路的超时/限制，便于全局把握：

| 维度 | 值 | 来源 |
|------|-----|------|
| 连接建交（initialize） | 30s | `getConnectionTimeoutMs` `client.ts:456` |
| 单个 HTTP 请求（POST） | 60s | `MCP_REQUEST_TIMEOUT_MS` `client.ts:463` |
| 工具调用 | ≈无限（100M ms） | `DEFAULT_MCP_TOOL_TIMEOUT_MS` `client.ts:211` |
| OAuth 回调等待 | 5min | `performMCPOAuthFlow` `auth.ts:1193` |
| needs-auth 缓存 | 15min | `MCP_AUTH_CACHE_TTL_MS` `client.ts:257` |
| token 即将过期阈值 | expiresIn ≤ 300s | `tokens()` `auth.ts:1558` |
| headersHelper 执行 | 10s | `headersHelper.ts:32` |
| 工具描述最大长度 | 2048 字符 | `MAX_MCP_DESCRIPTION_LENGTH` `client.ts:218` |
| 工具结果 token 预算 | 25000（默认） | `mcpValidation.ts:26` |

规律：**连接/请求严格超时，工具调用信任 server**。因为连接失败要快速反馈，但工具可能 legitimately 跑很久。

### 11.5 安全治理小结

三道防线的设计逻辑：

1. **Registry 判定信任梯度**：official server 给不同呈现，fail-closed 保证未就绪时不误判。
2. **headersHelper 动态凭据**：git credential-helper 风格，project scope 强制 trust 防仓库投毒。
3. **输出 token 治理**：两阶段估算 + image 压缩，防撑爆上下文。
4. **连接严格、工具宽松**：超时矩阵反映"快速失败 vs 信任长任务"的平衡。

---

## 12. CLI 命令体系：`claude mcp ...`

使用者主要通过 `claude mcp` 子命令管理 MCP server。命令在 `main.tsx:3894` 用 commander 注册，handler 实现在 `cli/handlers/mcp.tsx`、`commands/mcp/addCommand.ts`、`commands/mcp/xaaIdpCommand.ts`。

### 12.1 子命令清单

| 子命令 | 注册 | handler | 作用 |
|--------|------|---------|------|
| `serve` | `main.tsx:3895` | `mcpServeHandler`（`cli/handlers/mcp.tsx:42`） | **Claude Code 自身作为 MCP server** 启动。`setup(cwd,...)` 后动态 import `entrypoints/mcp.ts` 的 `startMCPServer`。flag `-d/--debug`、`--verbose` |
| `add <name> <commandOrUrl> [args...]` | `addCommand.ts:35` | `addCommand.ts:81` | 添加 server（见 12.2） |
| `add-json <name> <json>` | `main.tsx:3936` | `mcpAddJsonHandler`（`:286`） | 用 JSON 字符串直接加 server，`-s/--scope`、`--client-secret` |
| `add-from-claude-desktop` | `main.tsx:3945` | `mcpAddFromDesktopHandler`（`:317`） | 从 Claude Desktop 配置导入（仅 Mac/WSL），弹 `MCPServerDesktopImportDialog` 选 |
| `list` | `main.tsx:3924` | `mcpListHandler`（`:144`） | 列出所有 server 并**并发健康检查** |
| `get <name>` | `main.tsx:3930` | `mcpGetHandler`（`:193`） | 显示某 server 详情：scope、status、type/url/headers/oauth |
| `remove <name>` | `main.tsx:3916` | `mcpRemoveHandler`（`:74`） | 删除 server，`-s/--scope`；多 scope 时提示指定 |
| `reset-project-choices` | `main.tsx:3953` | `mcpResetChoicesHandler`（`:352`） | 重置当前项目 `.mcp.json` 的批准选择（见 12.4） |
| `xaa`（条件 `isXaaEnabled()`） | `main.tsx:3913` | `registerMcpXaaIdpCommand`（`xaaIdpCommand.ts:24`） | XAA IdP 连接管理（见 12.5） |

### 12.2 `add`：核心添加命令

`registerMcpAddCommand`（`addCommand.ts:35`）注册，flag 丰富：

```bash
claude mcp add <name> <commandOrUrl> [args...] \
  -s/--scope <local|user|project>   # 默认 local
  -t/--transport <stdio|sse|http>   # 自动推断
  -e/--env KEY=VALUE                # 可重复
  -H/--header "Key: Value"          # 可重复
  --client-id <id>                  # OAuth
  --client-secret <secret>          # OAuth
  --callback-port <port>            # OAuth 回调端口
  --xaa                             # 隐藏 flag,需 isXaaEnabled
```

按 transport 分别构造配置对象（`addCommand.ts:147`）：

- URL 输入 + http/sse → `{ type: 'sse'|'http', url, headers, oauth }`
- 命令输入 → `{ type: 'stdio', command, args, env }`

然后 `addMcpConfig(name, config, scope)` 写入对应 scope 的文件。

**误用防护**（`:128`）：`looksLikeUrl` 检测——若用户把 URL 当 stdio 命令传（如 `claude mcp add foo https://...`），警告提示改用 `-t http`。

### 12.3 `list` 的并发健康检查

`mcpListHandler`（`cli/handlers/mcp.tsx:144`）不只列配置，还对每个 server 做**实时健康检查**。用 `pMap` 并发，`concurrency: getMcpServerConnectionBatchSize()`（`:163`，默认 3）——复用连接层的本地并发度。最后 `gracefulShutdown(0)` 清理连接，避免孤儿子进程（`:188` 注释：`process.exit` 会 bypass cleanup handlers）。

`checkMcpServerHealth`（`:26`）封装健康检查：

```ts
await connectToServer(name, server)
// 按 result.type 返回:
//   'connected'  → '✓ Connected'
//   'needs-auth' → '! Needs authentication'
//   其他         → '✗ Failed to connect'
//   抛异常       → '✗ Connection error'
```

被 `list` 和 `get` 复用。

### 12.4 `reset-project-choices`（注意命令名）

`mcpResetChoicesHandler`（`:352`）清空当前项目 `.mcp.json` 的批准结果：`enabledMcpjsonServers` / `disabledMcpjsonServers` / `enableAllProjectMcpServers` 全部清空，让所有 project server 回到 `'pending'` 重新问。

> **注意命令名是 `reset-project-choices`，不是 `reset-choices`**——这是文档中常见笔误点。

### 12.5 `xaa`：XAA IdP 管理（条件命令）

仅在 `isXaaEnabled()`（`CLAUDE_CODE_ENABLE_XAA`）为真时注册（`main.tsx:3913`）。用户级配置，子命令（`xaaIdpCommand.ts`）：

| 子命令 | 行号 | 作用 |
|--------|------|------|
| `xaa setup` | `:29` | 配置 IdP（issuer/clientId/callbackPort），写 `settings.xaaIdp` |
| `xaa login` | `:150` | OIDC 浏览器登录缓存 id_token，或 `--id-token` 直接注入 JWT |
| `xaa show` | `:219` | 展示当前 XAA 配置 |
| `xaa clear` | `:243` | 清配置 + keychain |

这是第 5.6 节 XAA 的 CLI 入口——`setup` + `login` 一次，之后所有 XAA-enabled server 静默认证。

### 12.6 `/mcp` slash command（会话内）

不同于 CLI 子命令，`/mcp` 是**会话内 slash command**（`commands/mcp/mcp.tsx:63`）：

```ts
call(onDone, _context, args) {
  // args[0] === 'no-redirect' → <MCPSettings>（测试用）
  // 'reconnect <server>'      → <MCPReconnect>
  // 'enable'/'disable [target]' → <MCPToggle>（target 默认 'all'）
  // 否则 base → <MCPSettings>
}
```

`MCPToggle` 的 `target` 默认 `'all'`，按 action 启用/禁用指定或全部 server（非 ide）。toggle 调 `setMcpServerEnabled`（第 9.5 节）写 projectConfig，效果持久化。

### 12.7 CLI 体系小结

CLI 命令是配置层（第 2 章）的薄封装——每个 `add`/`remove` 最终都调 `addMcpConfig`/`removeMcpConfig` 写配置文件，`list`/`get` 复用连接层的 `connectToServer`。设计上把"配置编辑"和"连接验证"分离：CLI 管配置，连接层管生命周期。

`serve` 是特殊的——它让 Claude Code **反向**作为 MCP server 暴露给别的 MCP client，体现了 MCP 协议的双向性。

---

## 13. 关键模块索引与数据流时序

### 13.1 文件清单

**配置与类型**（`services/mcp/`）

| 文件 | 行数 | 职责 |
|------|------|------|
| `types.ts` | 258 | 全部 zod schema + 连接状态判别联合 + MCPCliState |
| `config.ts` | 1578 | 多 scope 合并、`.mcp.json` 读写、enable/disable、env 展开 |
| `normalization.ts` | 23 | 名称规范化 `normalizeNameForMCP` |
| `mcpStringUtils.ts` | 106 | `buildMcpToolName`/`getMcpPrefix`/`mcpInfoFromString`/`getToolNameForPermissionCheck` |
| `envExpansion.ts` | 38 | `${VAR}`/`${VAR:-default}` 展开 |
| `headersHelper.ts` | 138 | 外部脚本动态取 header |
| `officialRegistry.ts` | 72 | 官方 MCP registry 查询 |

**连接与封装核心**（`services/mcp/`）

| 文件 | 行数 | 职责 |
|------|------|------|
| `client.ts` | 3348 | **核心引擎**：`connectToServer`/`fetchToolsForClient`/`fetchCommandsForClient`/`fetchResourcesForClient`/`getMcpToolsCommandsAndResources`/`wrapFetchWithTimeout`/认证缓存 |
| `useManageMCPConnections.ts` | 1141 | React hook，连接生命周期编排中枢 |
| `MCPConnectionManager.tsx` | 72 | React Context Provider |
| `InProcessTransport.ts` | 63 | sdk 类型的进程内 transport |
| `SdkControlTransport.ts` | 136 | SDK 控制通道 transport |

**认证**（`services/mcp/`）

| 文件 | 行数 | 职责 |
|------|------|------|
| `auth.ts` | 2465 | OAuth 2.1 全编排、`ClaudeAuthProvider`、刷新锁、needs-auth 缓存 |
| `xaa.ts` | 511 | XAA 两层 token exchange |
| `xaaIdpLogin.ts` | 487 | XAA IdP 登录、id_token 缓存 |
| `oauthPort.ts` | 78 | OAuth 回调端口选择 |

**权限 / Channels / Elicitation**（`services/mcp/`）

| 文件 | 行数 | 职责 |
|------|------|------|
| `utils.ts` | 575 | `getProjectMcpServerStatus`/`filterMcpPromptsByServer`/各种过滤 |
| `channelPermissions.ts` | 240 | Channels 权限转发（experimental） |
| `channelAllowlist.ts` | 76 | Channels 白名单 |
| `channelNotification.ts` | 316 | Channels 三条 notification 协议 |
| `elicitationHandler.ts` | 313 | elicitation/create 处理 |

**工具层**（`tools/`）

| 目录 | 职责 |
|------|------|
| `MCPTool/` | 工具模板基座 + 渲染 + 折叠分类 |
| `McpAuthTool/` | needs-auth 伪工具 |
| `ReadMcpResourceTool/` | 读 MCP resource + blob 持久化 |
| `ListMcpResourcesTool/` | 列 MCP resources |

**辅助**（`utils/`、`skills/`、`components/`）

| 文件 | 职责 |
|------|------|
| `utils/mcpValidation.ts` | 输出 token 截断 |
| `utils/mcpOutputStorage.ts` | blob 持久化落盘 |
| `utils/mcpWebSocketTransport.ts` | WebSocket transport |
| `skills/mcpSkillBuilders.ts` | 写一次注册表（解依赖环） |
| `components/MCPServerApprovalDialog.tsx` 等 | 批准/多选/导入 UI |
| `components/mcp/ElicitationDialog.tsx` | elicitation 表单/URL UI |

### 13.2 时序图 1：启动连接

```text
main.tsx 启动
  │
  ▼
getMcpToolsCommandsAndResources(onConnectionAttempt, mcpConfigs)   client.ts:2226
  │
  ├─ getAllMcpConfigs().servers          合并 7 scope,手动优先
  │   └─ expandEnvVars (config.ts:556)    ${VAR} 展开,缺失 fatal
  │
  ├─ 分区: disabled (isMcpServerDisabled) → 直接 onConnectionAttempt({type:'disabled'})
  │
  ├─ 分桶: localServers (stdio/sdk) / remoteServers (sse/http/ws)
  │
  └─ Promise.all([
       processBatched(local,  batch=3, processServer),     client.ts:2391
       processBatched(remote, batch=20, processServer),
     ])
            │
            ▼  processServer(name, config):
            ├─ needs-auth 缓存命中? (isMcpAuthCached / hasMcpDiscoveryButNoToken)
            │     └─ 是 → onConnectionAttempt({type:'needs-auth', tools:[McpAuthTool]})  return
            │
            ├─ connectToServer(name, config) [memoized]    client.ts:595
            │     ├─ 按 config.type 选 transport (第 3 章)
            │     ├─ new Client({capabilities:{roots:{},elicitation:{}}})   client.ts:985
            │     ├─ await client.initialize()              握手
            │     └─ client.getServerCapabilities()         协商 {tools?,prompts?,resources?}
            │
            ├─ 连接结果分流:
            │   ├─ connected → 并行发现:
            │   │     ├─ fetchToolsForClient     (tools/list)        client.ts:1745
            │   │     │     └─ 每个 tool → {...MCPTool, name:mcp__s__t, ...} override
            │   │     ├─ fetchCommandsForClient  (prompts/list)      client.ts:2044
            │   │     ├─ fetchMcpSkillsForClient (feature('MCP_SKILLS'))
            │   │     └─ fetchResourcesForClient (resources/list)    client.ts:2000
            │   │           └─ supportsResources && !resourceToolsAdded
            │   │              → 注入 ListMcpResources/ReadMcpResource (全局一次)
            │   ├─ needs-auth → tools:[McpAuthTool]
            │   ├─ failed    → {type:'failed', error}
            │   └─ ...
            │
            └─ onConnectionAttempt({client, tools, commands, resources})
                  │
                  ▼
            useManageMCPConnections → AppState.tools (与内置工具并列)
                  │
                  ▼
            注册 notification handler (list_changed → 失效 LRU)
```

### 13.3 时序图 2：工具调用

```text
LLM 决定调用 mcp__filesystem__read_file({path})
  │
  ▼
工具池组装阶段(给 LLM 之前):
  filterToolsByDenyRules (tools.ts:262)
    └─ server 级 mcp__filesystem deny? → 整 server 工具剔除
  │
  ▼
调用阶段:
  MCP tool.call(input, context)
    ├─ checkPermissions → {behavior:'passthrough', addRules:[{mcp__filesystem__read_file, allow}]}
    │     │
    │     ▼  hasPermissionsToUseTool (通用四态)
    │     ├─ allow  → 继续
    │     ├─ deny   → 拒绝
    │     └─ ask    → interactiveHandler → 对话框
    │                    ├─ accept-once / accept-session / reject
    │                    └─ (+ channel relay 竞速, experimental)
    │
    ├─ ensureConnectedClient(client)            client.ts:1688  (memoize 命中 no-op)
    │
    ├─ client.callTool({name, arguments})        client.ts:3092  (timeout: getMcpToolTimeoutMs ≈无限)
    │
    └─ processMcpToolResult                       client.ts:2630
          ├─ 文本 → 直接返回
          ├─ 二进制 blob → persistBlobToTextBlock (mcp-<server>-blob-...)   client.ts:2598
          └─ 大文本 → persist (mcp-<server>-<tool>-<ts>)                    client.ts:2769
                │
                ▼
          mcpContentNeedsTruncation / truncateMcpContentIfNeeded  (25000 token 预算)
                │
                ▼
          渲染结果 (复用 MCPTool 模板的 renderToolResultMessage)
```

### 13.4 时序图 3：OAuth 认证（needs-auth → 连上）

```text
启动时: server X 连接 → 401
  └─ handleRemoteAuthFailure (client.ts:340)
        └─ setMcpAuthCacheEntry(X) → needs-auth-cache.json (15min TTL)
        └─ onConnectionAttempt({type:'needs-auth', tools:[McpAuthTool(X)]})
              └─ LLM 看到 mcp__X__authenticate 工具
  │
  ▼  (LLM 调用 mcp__X__authenticate, 或用户 /mcp 手动认证)
McpAuthTool.call (McpAuthTool.ts:85)
  └─ performMCPOAuthFlow(X, {skipBrowserOpen:true})     auth.ts:846
        ├─ XAA 分支? (oauth.xaa && isXaaEnabled) → performMCPXaaAuth (静默, 5.6)
        ├─ findAvailablePort()                         oauthPort.ts:36
        ├─ new ClaudeAuthProvider                      auth.ts:977
        ├─ fetchAuthServerMetadata (RFC 9728→8414)     auth.ts:255
        ├─ 起 /callback 服务器 (state 防 CSRF, 5min 超时)   auth.ts:1068
        ├─ sdkAuth(provider) → 'REDIRECT' (拿授权 URL)
        ├─ 浏览器完成授权 → 回调 → 拿 authorizationCode
        ├─ sdkAuth(provider, {authorizationCode}) → 'AUTHORIZED' (code→token)
        └─ token 存 keychain (mcpOAuth[serverKey])
              │
              ▼  OAuth 成功回调 (McpAuthTool.ts:137)
        clearMcpAuthCache()              清 15min 缓存
        reconnectMcpServerImpl(X)        重连
              │
              ▼
        connectToServer(X) 重跑 → connected
        fetchToolsForClient → 真实工具 (mcp__X__<tool>)
              │
              ▼
        前缀替换 swap: 真实工具进 AppState.mcp.tools,
                       McpAuthTool (mcp__X__authenticate) 自动清除 (共享 mcp__X__ 前缀)
```

---

## 14. 设计理念总结

回望整个 MCP 子系统，可以提炼出六条贯穿始终的设计理念。

### 14.1 配置驱动：声明式优于命令式

使用者唯一的输入是一条声明式配置。配置是唯一真相来源，下游所有环节从它派生。这带来零胶水、可版本控制（`.mcp.json` 入 git）、可审计的好处。`getAllMcpConfigs` 把 7 个 scope 合并成一张表，是"声明式"的物理实现。

### 14.2 能力协商：不假设，主动问

客户端绝不假设 server 有什么能力，握手后 `getServerCapabilities()` 主动问。每个原语发现函数先查 capability 再发请求。这让 server 可以渐进增强（升级加工具），客户端零改动自动发现。这是 MCP 协议"渐进式增强"哲学的体现，也是应对"server 能力千差万别"的正确姿态。

### 14.3 模板封装：一个蓝本克隆 N 个

不为每个 MCP 工具写类，而是定义通用 `MCPTool` 模板承载共用逻辑，`fetchToolsForClient` 对每个工具 override 差异。这就是为什么"工具封装"只有几十行，而连接/认证占上万行——复杂度被收敛到该收敛的地方。`mcpInfo` 反向追溯 + 全限定名权限匹配，是这套封装的安全底座。

### 14.4 安全默认：多层防御 + 最小信任

- **首次批准防仓库投毒**：`.mcp.json` 是 git 提交的，首次必须用户同意；bypass 模式故意不读 projectSettings。
- **Unicode sanitization**：封装前消毒，防 prompt injection。
- **全限定名权限**：即使 `skipPrefix` 让 display name 无前缀，权限检查仍走全限定名防冒充。
- **needs-auth 缓存**：避免反复探测未授权 server。
- **headersHelper 强制 trust**：防仓库诱导执行脚本。
- **Registry fail-closed**：未就绪时不误判 official。

每一层都假设"上一层可能被绕过"，形成纵深防御。

### 14.5 薄 SDK 厚编排：协议正确性交给 SDK，工程留给自己

PKCE、code 交换、JSON-RPC、transport 底层用 SDK；浏览器流程、元数据发现、跨进程刷新锁、缓存、XAA、权限 UI、输出治理留给自己。这条边界让 Claude Code 既能跟进协议演进（SDK 升级即获得新 transport/原语），又能做 SDK 不该做的工程编排。

### 14.6 与 Skill 的统一与分野

MCP 工具和 Skill 是"把外部能力暴露给 LLM"的两种范式，注册方式相反（逐工具 vs 单工具多参数），但**终点统一**——都在 `AppState.tools` 这张表里，都走同一个调用管道。MCP skill（server prompt 提升）是两者的交汇点，用 `loadedFrom === 'mcp'` 标记，进 `Skill` 工具清单。这种"差异源头、统一终点"的设计，让 LLM 无需区分能力来源。

---

## 附录 A：MCP 协议原语与 Claude Code 映射

| MCP 原语 | JSON-RPC 方法 | Claude Code 映射 | 关键代码 |
|----------|--------------|------------------|----------|
| **tools** | `tools/list`、`tools/call` | 每个 tool → 独立 `mcp__server__tool` Tool | `fetchToolsForClient` `client.ts:1745` |
| **prompts** | `prompts/list`、`prompts/get` | → Command；提升后 → MCP skill（`Skill` 工具） | `fetchCommandsForClient` `client.ts:2044` |
| **resources** | `resources/list`、`resources/read` | 两个通用工具（`ListMcpResources`/`ReadMcpResource`）参数路由 | `client.ts:2000`、`tools/ReadMcpResourceTool/` |
| **resources/subscribe** | `resources/subscribe` | **只探测不使用**（日志记录），实际靠 `resources/list_changed` | `client.ts:1179`（探测）、`useManageMCPConnections.ts:705`（实际） |
| **elicitation** | `elicitation/create` | queue + UI 弹窗（REPL）/ 错误码 -32042（print） | `elicitationHandler.ts:68`、`client.ts:2803` |
| **roots** | `roots/list` | client 声明 capability（空 `{}`），提供工作区根 | `client.ts:985` |
| **logging** | `notifications/message` | server 日志转发 | （logging handler） |
| **completion** | `completion/complete` | prompt 参数补全 | （completion handler） |
| **list_changed** | `tools/list_changed`、`prompts/list_changed`、`resources/list_changed` | 失效 LRU 缓存，被动刷新 | `useManageMCPConnections.ts:673/705` |
| **experimental** | `notifications/claude/channel*` | Channels 功能（消息通道转发审批） | `channelNotification.ts:37/62/85` |

---

## 附录 B：与 Skill 子系统的对照

本附录承接 [docs/claude-code-skill-system-implementation.md](claude-code-skill-system-implementation.md) §9，把 MCP 与 Skill 两个子系统并列对比。

| 维度 | MCP 工具 | 文件系统 Skill | MCP Skill |
|------|----------|---------------|-----------|
| **注册范式** | 逐工具独立 Tool | 单 `Skill` Tool + 参数 | 单 `Skill` Tool + 参数 |
| **封装位置** | `fetchToolsForClient`（`client.ts:1745`） | `createSkillCommand`（`loadSkillsDir.ts:270`） | `fetchMcpSkillsForClient`（`mcpSkills.ts`，缺失） |
| **来源** | server `tools/list` | `.claude/skills/<n>/SKILL.md` | server `prompts/list`（提升） |
| **`loadedFrom`** | （Tool，无此字段） | `skills`/`bundled`/`plugin`/`managed` | `mcp` |
| **内容** | 远程函数调用 | 静态本地文本 | 动态 server 拉 |
| **执行本质** | `callTool` RPC | prompt 展开 | prompt 展开（`getPrompt` RPC） |
| **发现时机** | server 连上后 | 启动扫目录 | server 连上后 + feature flag |
| **能力探测** | `capabilities.tools` | 无（文件即能力） | `capabilities.prompts` + `feature('MCP_SKILLS')` |
| **`Skill` 工具可见** | 否（独立 Tool） | 是 | 是 |
| **依赖环处理** | 不涉及 | 不涉及 | `mcpSkillBuilders.ts` 写一次注册表 |

**交汇点**：MCP skill 是唯一同时属于两个子系统的实体——它来自 MCP（server prompt），但终点在 Skill（`Skill` 工具）。`SkillTool.getAllCommands`（`SkillTool.ts:73`）合并本地 + MCP skill 的逻辑，是两个子系统的接合处。

---

## 附录 C：配置字段速查

MCP 配置涉及多组易混字段，按下表速查：

### server 配置字段（`McpServerConfig`，写进配置文件）

| 字段 | 适用类型 | 作用 |
|------|---------|------|
| `type` | 全部 | transport 类型（stdio/sse/sse-ide/http/ws/sdk/claudeai-proxy） |
| `command`/`args`/`env` | stdio | spawn 命令及环境 |
| `url` | sse/http/ws | 远程地址 |
| `headers` | sse/http/ws | 静态 HTTP header |
| `headersHelper` | sse/http | 外部脚本动态取 header |
| `oauth.clientId` | sse/http | OAuth client id |
| `oauth.callbackPort` | sse/http | OAuth 回调端口 |
| `oauth.authServerMetadataUrl` | sse/http | 授权服务器元数据 URL（须 https） |
| `oauth.xaa` | sse/http | 启用 XAA（SEP-990） |
| `ideName`/`ideRunningInWindows` | sse-ide/ws-ide | IDE 标识 |
| `name` | sdk | 进程内 server 名 |
| `url`/`id` | claudeai-proxy | Claude.ai 代理 |

### 运行时状态字段（不同 store，勿混）

| 字段组 | store | 写入点 | 作用 |
|--------|-------|--------|------|
| `enabledMcpServers`/`disabledMcpServers` | projectConfig | `setMcpServerEnabled` `config.ts:1553` | `/mcp` toggle 运行时开关 |
| `enabledMcpjsonServers`/`disabledMcpjsonServers`/`enableAllProjectMcpServers` | localSettings | `MCPServerApprovalDialog` | `.mcp.json` server 首次批准 |
| `xaaIdp` | userSettings | `claude mcp xaa setup` | XAA IdP 配置（issuer/clientId/callbackPort） |

### 环境变量

| 变量 | 作用 | 默认 |
|------|------|------|
| `MCP_TIMEOUT` | 连接建交超时（ms） | 30000 |
| `MCP_TOOL_TIMEOUT` | 工具调用超时（ms） | 100000000（≈无限） |
| `MCP_REQUEST_TIMEOUT_MS`（内部常量） | 单请求超时 | 60000 |
| `MCP_SERVER_CONNECTION_BATCH_SIZE` | 本地连接并发 | 3 |
| `MCP_REMOTE_SERVER_CONNECTION_BATCH_SIZE` | 远程连接并发 | 20 |
| `MCP_OAUTH_CALLBACK_PORT` | OAuth 回调固定端口 | 随机 |
| `MAX_MCP_OUTPUT_TOKENS` | 工具输出 token 上限 | 25000 |
| `CLAUDE_CODE_ENABLE_XAA` | 启用 XAA | 关 |
| `CLAUDE_AGENT_SDK_MCP_NO_PREFIX` | SDK MCP 工具去前缀（可覆盖内置） | 关 |
| `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC` | 关闭非必要网络（含 registry） | 关 |
| `SLASH_COMMAND_TOOL_CHAR_BUDGET` | `Skill` 工具清单字符预算 | 1% context |
| `MCP_OAUTH_CLIENT_METADATA_URL` | CIMD client metadata URL | — |

---

> **文档版本**：基于当前 `src/` 快照。`src/skills/mcpSkills.ts` 未随包发布，第 8 章相关结论已标注「基于调用方引用反推」。MCP 底层协议原语（PKCE、token 交换、JSON-RPC）实现在 `@modelcontextprotocol/sdk`，本文聚焦 Claude Code 编排层。
