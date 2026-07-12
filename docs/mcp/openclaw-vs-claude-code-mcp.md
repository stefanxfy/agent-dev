# OpenClaw vs Claude Code：MCP 实现对比

> 本文对比两个 MCP（Model Context Protocol）实现在工程取舍上的异同，聚焦"为什么不同"，不重复两边源文档已有细节。
>
> **引用约定**：
> - **Claude Code 侧**结论引自 [claude-code-mcp-implementation.md](./claude-code-mcp-implementation.md)，其源码路径形如 `client.ts:595`，属 Claude Code TS 源码 `src/services/mcp/`（约 12300 行），**不在本仓库**。
> - **OpenClaw 侧**引自本仓库源码，相对路径形如 `src/mcp/channel-bridge.ts:127`，完整架构见 [openclaw-mcp-architecture.md](./openclaw-mcp-architecture.md)。
> - 两边 MCP SDK 均为 `@modelcontextprotocol/sdk`。

---

## TL;DR

一句话：**Claude Code 把 MCP 当"工具来源"（纯 client 编排器），OpenClaw 把 MCP 当"对外接缝"之一（双向协议中枢，与 channels/gateway 并列）。** 这个根本定位差异，解释了下面所有实现分歧——server 类型多寡、要不要做能力协商、认证做到什么程度、权限威胁建模针对什么。

| 维度 | Claude Code | OpenClaw |
|---|---|---|
| MCP 定位 | 纯 **client** 编排器 | **双向**：client + server |
| server 类型 | 7 种 | 2 种（stdio / http） |
| 配置来源 | 7 层 scope 合并 + `.mcp.json` 团队共享 | 单文件 `~/.openclaw/openclaw.json` |
| 能力协商 | 严格 capability-driven | 直接 `listAllTools`（不协商） |
| 连接并发 | 分桶（stdio 3 / 远程 20） | 逐个顺序 |
| 工具封装 | MCPTool 模板 spread override | materialize + execute 闭包 |
| 工具命名 | `mcp__server__tool`（前缀作权限骨架） | `serverName__toolName`（无全局前缀） |
| 认证 | OAuth 2.1 + XAA + McpAuthTool | 无自动认证（headers 手动塞 token） |
| 权限模型 | 两层（首次批准防投毒 + 运行时四态） | 统一 before-tool-call hook（三入口合一） |
| 独有 | Resources/Prompts/Skill/Elicitation/IDE transport/Channels | server 三入口/acpx 注入/跨 provider 转译 |

---

## 一、根本定位差异：client 编排器 vs 双向协议中枢

这是所有分歧的源头。两者的 MCP 数据方向完全不同：

```
Claude Code（单向，只消费）：
  外部 MCP server ──► Claude Code(client 编排) ──► LLM 工具池
                                                      │
                                                      ▼
                                                    模型调用

OpenClaw（双向，既消费也暴露）：
  外部 MCP server ──► OpenClaw(client) ──► agent runtime ──► LLM
                          ▲
                          │ server 侧三条入口
  外部 client ◄── Channel MCP(会话) / Tools MCP(工具) / Loopback HTTP(反向自用)
```

**Claude Code** 是一个 coding agent，MCP 对它的意义是"让 LLM 多一批外部工具"。所以它的 MCP 子系统是一条**单向流水线**：声明式配置 → 连接编排 → 传输 → 握手 → 能力协商 → 工具/资源/prompt 自动封装 → 注册进统一工具体系 → 权限/认证。它**没有"把自己暴露成 MCP server"的方向**（唯一的"出站"是 Channels 实验功能，把权限审批转发到手机，不是标准 MCP server 语义）。

**OpenClaw** 是一个 agent 平台 + channels 桥（Telegram/Discord/iMessage 等），MCP 对它的意义是"众多对外接缝之一"。所以它天然**双向**：

- **当 client**：把外部 MCP server 的工具物化进 agent runtime（embedded Pi），和 Claude Code 的消费方向一样；
- **当 server**：把自己路由好的 channel 会话、内置/插件工具、甚至内部 cron 工具，反向暴露成 MCP server，供 Codex / Claude Code / cron 调度器等外部 client 消费（三条入口：`src/mcp/channel-server.ts`、`src/mcp/plugin-tools-serve.ts`、`src/gateway/mcp-http.ts`）。

这个差异的直接后果：

- OpenClaw 多了整整一个"server 侧"子系统（三条入口 + Gateway 桥接 + loopback HTTP），Claude Code 完全没有；
- OpenClaw 还多了"跨 provider 配置转译"（`src/agents/cli-runner/bundle-mcp-{claude,codex,gemini}.ts`）——当它作为宿主拉起外部 CLI agent 时，要把 MCP 配置翻译成各家 CLI 原生格式。Claude Code 不需要这个，因为它自己就是终点的 client。
- 反过来，Claude Code 在"消费侧的工程化深度"上远超 OpenClaw（认证、权限、能力协商、并发编排），因为它把全部精力集中在这一个方向。

> 类比：Claude Code 是"MCP 的资深消费者"，OpenClaw 是"MCP 的路由器"。

---

## 二、配置层：7 层 scope 体系 vs 单文件 CRUD

配置层是两者差异最直观的地方，背后是**面向的使用场景不同**。

### 2.1 server 类型

| | Claude Code | OpenClaw |
|---|---|---|
| 类型数 | **7 种** | **2 种** |
| 列表 | stdio / sse / sse-ide / http / ws / ws-ide / sdk / claudeai-proxy | stdio / http（transport: `sse` \| `streamable-http`） |
| 内部专用 | sse-ide / ws-ide（IDE 扩展）/ sdk（进程内）/ claudeai-proxy（复用自身登录） | 无 |

Claude Code 的 7 种里有 4 种是"特殊场景"：两种 `*-ide` 是 IDE 集成一等公民（schema 层预留）、`sdk` 是进程内 transport（供 Claude Agent SDK 嵌入）、`claudeai-proxy` 复用 Claude Code 自身登录 claude.ai 的 token。这些类型存在，是因为 Claude Code 要适配 IDE 插件、SDK 嵌入、claude.ai connector 等多种宿主形态。OpenClaw 只服务"本地命令"和"远程 HTTP"两个最常见场景，所以只有 2 种。

### 2.2 配置来源与合并

**Claude Code：7 层 ConfigScope，手动优先**

```
local(项目私有,不进 git) > user(全局) > project(.mcp.json,团队共享)
  > dynamic(--mcp-config/SDK) > enterprise(企业策略) > claude.ai(同步) > managed(受管策略)
```

`getAllMcpConfigs()`（`config.ts`）按优先级从高到低合并，核心规则是**手动配置永远赢过自动来源**——防止企业策略 / claude.ai 同步覆盖用户意图。同名冲突靠 `getServerSignature`（`config.ts:202`）算签名去重。

**OpenClaw：单一配置文件**

```
~/.openclaw/openclaw.json 的 mcp.servers 字段（src/config/paths.ts:144）
```

`openclaw mcp set/unset/list/show`（`src/cli/mcp-cli.ts`）就是对这个文件做 CRUD，带乐观锁（`baseHash` 防并发写覆盖，`src/config/mcp-config.ts:86`）。旧别名 `type:"http"` 会被归一化成 `transport:"streamable-http"`（`src/config/mcp-config-normalize.ts:17`）。

### 2.3 为什么差这么多

| 场景 | Claude Code 需要 | OpenClaw 需要 |
|---|---|---|
| 团队共享 MCP 配置 | `.mcp.json` 提交 git，成员 clone 后批准 | 一般不需要（单用户/单实例为主） |
| 企业统一推送 server | enterprise / managed scope | 不需要 |
| 从 claude.ai 同步 connector | claudeai scope | 不适用（没有对应产品） |
| 项目私有覆盖 | local scope（不进 git） | 单文件足够 |
| 环境变量 / 密钥 | `${VAR}` / `${VAR:-default}`，**缺失直接 fatal**（防降级运行） | stdio 的 `env` 字段、http 的 `headers` 手动塞 |

Claude Code 的"环境变量缺失即 fatal"（`config.ts:1333`）值得单独提——它宁可让 server 加载失败，也不让 server 在"token 没设 → 空认证连上 → 暴露未授权接口"的降级状态下静默运行。OpenClaw 没有等价的 fail-fast 机制（密钥错只会在连接时报错）。

> **小结**：配置层的复杂度正比于"要服务多少种使用场景"。Claude Code 面向"开发者团队 + 企业 + IDE + SDK + claude.ai"多场景，所以需要 7 scope；OpenClaw 面向"单用户给自己装 MCP"，一个文件 + CLI 足够。

---

## 三、Transport：种类广度 vs 单点深度

两者都遵循"薄 SDK + 厚编排"，但厚的方向不同：Claude Code 厚在"覆盖更多传输形态 + 统一治理"，OpenClaw 厚在"把 stdio 子进程做到生产级安全"。

### 3.1 种类与来源

| | Claude Code | OpenClaw |
|---|---|---|
| transport 总数 | 7（见 §2.1） | 2（stdio / http） |
| 复用 SDK | StdioClientTransport / SSEClientTransport / StreamableHTTPClientTransport | 同（http 走 SDK 的 SSE/StreamableHTTP） |
| 自研 | **WebSocket**（`utils/mcpWebSocketTransport.ts`，非 SDK 提供，支持 mTLS/代理/自定义 headers） | **stdio**（`OpenClawStdioClientTransport`，`src/agents/mcp-stdio-transport.ts`，不用官方 StdioClientTransport） |
| 统一包装器 | `wrapFetchWithTimeout`（所有 HTTP 类 transport 套一层） | 无统一 wrapper，stdio 自研、http 直用 SDK |

### 3.2 自研点的取舍差异

**Claude Code 自研 WebSocket**，是因为企业场景需要 mTLS、代理、自定义 headers（`getWebSocketTLSOptions`、`getWebSocketProxyAgent`），SDK 不提供 WS transport。

**OpenClaw 自研 stdio transport**，是为了 stdio 子进程的**生产化安全**（`src/agents/mcp-stdio-transport.ts`）：

- `prepareOomScoreAdjustedSpawn`：Linux OOM 调分，避免 MCP 子进程被优先杀掉；
- `detached:true`：自成进程组，关闭时能整组清理；
- `close()` → `killProcessTree`：`SIGTERM(-pid)` 整组 → 宽限 → `SIGKILL`，并特判 `detached:false` 避免误杀 gateway 自身进程组（`src/process/kill-tree.ts`）；
- 捕获 EPIPE 避免 uncaughtException（注释 #75438）。

Claude Code 的 stdio 直接用 SDK 的 `StdioClientTransport`，主要靠"连接 drop 时清理子进程"（§3.2 of CC 文档），没做到 OpenClaw 这种进程树级清理深度。

### 3.3 超时治理

Claude Code 有完整的超时矩阵（`wrapFetchWithTimeout`）：

| 常量 | 值 | 语义 |
|---|---|---|
| 连接（initialize） | 30s | 握手超时 |
| 单个 POST 请求 | 60s | 请求超时 |
| 工具调用 | ≈27.8h（"effectively infinite"） | 信任长任务，不强行截断 |
| GET（SSE 长连接） | **不加超时** | 精确理解"SSE 流不能加超时" |

OpenClaw 这边：连接 `connectWithTimeout` 默认 30s（`src/agents/pi-bundle-mcp-runtime.ts:95`），工具调用透传、无显式超时治理矩阵。**Claude Code 的超时治理更精细**，尤其是"GET 不加超时"这种对协议语义的精确把握。

> **小结**：Claude Code 广（7 种 transport + 统一超时治理），OpenClaw 深（把 stdio 一个 transport 做到进程树安全）。各自服务于自己的核心场景——CC 要兼容各种宿主/网络环境，OpenClaw 要保证本地子进程不泄漏。

---

## 四、连接 / 发现 / 加载：能力协商与并发编排

这是两者"协议纯粹度"差距最大的地方。

### 4.1 能力协商：CC 严格，OpenClaw 跳过

**Claude Code：capability-driven（渐进式增强）**

`connectToServer`（`client.ts:595`）完成 `Client.initialize()` 握手后，**立即** `getServerCapabilities()` 拿 server 自报的能力集合：

```
capabilities = { tools?, prompts?, resources?, resources.subscribe?, resources.listChanged?, ... }
```

后续每个原语发现函数**先查对应 capability 再发请求**：

- `fetchToolsForClient`：`if (!client.capabilities?.tools) return []`（`client.ts:1748`）
- `fetchResourcesForClient`：`capabilities?.resources` 为假直接返回 `[]`
- `fetchCommandsForClient`：`capabilities?.prompts` 检查

这是 MCP"渐进式增强"哲学的纯粹实现：server 升级加了工具/资源，client 不用改代码，下次连接自动发现；server 没声明的能力，client 绝不多发一个注定被拒的请求。

**OpenClaw：直接 `listAllTools`，不协商**

OpenClaw 的 client runtime（`src/agents/pi-bundle-mcp-runtime.ts`）握手后直接 `listAllTools` 翻页拉工具（`:122`），**没有 `getServerCapabilities` 这一步**。它只消费 tools，不碰 resources/prompts，所以跳过协商在功能上没损失——但少了协议合规性（对声明"无 tools 能力"的 server 也会发 `tools/list`）。

> 这是 CC 更"协议纯粹"的典型体现。OpenClaw 务实地只取自己需要的（tools），代价是放弃了渐进式增强的扩展点。

### 4.2 连接并发：分桶 vs 顺序

**Claude Code：本地/远程分桶 + 不同并发度**（`client.ts:2388`）

```
本地 (stdio/sdk)  batch=3   ← spawn 进程有资源开销，低并发
远程 (sse/http/ws) batch=20  ← 只是网络连接，高并发
两桶并行推进，互不阻塞
```

`processBatched` 是通用并发限流器。本地慢连接不拖累远程连接。

**OpenClaw：逐个顺序连接**

`createSessionMcpRuntime`（`src/agents/pi-bundle-mcp-runtime.ts`）对每个 server 顺序 `connectWithTimeout` → `listAllTools` → 建 catalog。没有分桶、没有并发限流。

> CC 的分桶策略尊重了"spawn 进程 vs 网络连接"的开销差异，是更成熟的并发设计。OpenClaw 假设 MCP server 数量少（典型几个），顺序连接可接受。

### 4.3 连接复用与刷新

| 机制 | Claude Code | OpenClaw |
|---|---|---|
| 连接复用 | `connectToServer` memoize（LRU，`client.ts:595`），相同配置不重连 | `SessionMcpRuntimeManager` 进程单例 + lease 引用计数 |
| 缓存失效 | `clearServerCache`（配置变更/OAuth 完成后，`client.ts:1648`） | 配置指纹（sha1）变更 → dispose 重建（`pi-bundle-mcp-runtime.ts:483`） |
| 动态刷新 | **被动**：`*_list_changed` 通知（`useManageMCPConnections.ts:705`），server 热更新工具时自动重拉 | **主动**：靠配置指纹，运行中 server 工具变化不会自动感知 |
| 重连 | 连续 3 次错误触发 `close()`+重连（`client.ts:1216`）；`ensureConnectedClient` 调用前健康检查 | 会话内 lease 续命；连接断开未见自动重连逻辑 |

**关键差异：动态刷新方向相反。** CC 靠 server 推来的 `list_changed` 通知被动刷新（及时、省资源），OpenClaw 靠配置指纹主动重建（只感知配置变化，**感知不到 server 自身工具清单的运行期变化**）。如果你的 MCP server 支持热更新工具，CC 能自动跟上，OpenClaw 要重启会话。

### 4.4 needs-auth 短路（CC 独有）

CC 对远程 server 返回 401 时标记 `needs-auth` 并缓存 15 分钟（`client.ts:257`），缓存期内直接跳过连接（`:2307`），但会注入一个 `McpAuthTool` "伪工具"让 LLM 知道这个 server 存在、能代用户发起认证。OpenClaw 没有等价机制——认证失败的 server 只会在连接时报错跳过。

> **小结**：连接/发现/加载这一层，CC 全面更成熟——能力协商、分桶并发、被动刷新、needs-auth 短路、健康检查重连，都是 OpenClaw 没有的。OpenClaw 的优势在"配置指纹热重载"（支持运行中改插件配置）和"lease + 空闲回收"（默认 10min TTL，`sessionIdleTtlMs`），后者 CC 用 memoize LRU 间接实现。

---

## 五、工具封装与命名：模板 override vs 物化闭包

两者都把"server 返回的原始工具"转成"自家工具体系的一员"，但范式相反：CC 是 OOP 的模板 spread，OpenClaw 是函数式的构造+闭包。

### 5.1 封装范式

**Claude Code：MCPTool 模板 spread override**

CC 定义一个通用模板 `MCPTool`（`tools/MCPTool/MCPTool.ts`），承载所有共用逻辑（渲染、权限、结果截断 100k 字符、并发安全判定、`mapToolResultToToolResultBlockParam`）。然后在 `fetchToolsForClient`（`client.ts:1745`）里对每个工具以模板为蓝本 override：

```ts
return toolsToProcess.map((tool): Tool => ({
  ...MCPTool,                                          // ① 复用模板全部共用逻辑
  name: buildMcpToolName(client.name, tool.name),      // ② 真实名
  mcpInfo: { serverName: client.name, toolName: tool.name },  // ③ 反向追溯
  async description() { return tool.description ?? '' },      // ④ 用 server 的描述
  async call(...) { /* 转发到 client.callTool */ },
}))
```

共用逻辑收敛进单一模板，每个工具只覆盖差异——这就是"封装只有几十行，连接/认证占了上万行"的原因。

**OpenClaw：materialize + execute 闭包**

OpenClaw 的 `materializeBundleMcpToolsForRun`（`src/agents/pi-bundle-mcp-materialize.ts:64`）对 catalog 工具逐个构造 `AnyAgentTool`，`execute` 是一个闭包，绑定到 `runtime.callTool`：

```ts
// 概念示意
toolsToProcess.map((tool) => ({
  name: buildSafeToolName(serverName, tool.name),   // serverName__toolName
  description: tool.description,
  inputSchema: tool.inputSchema,
  async execute(input) {
    return toAgentToolResult(await runtime.callTool(serverName, tool.name, input))
  },
}))
```

并用 `setPluginToolMeta` 标记来源 `bundle-mcp`。没有"共用模板基座"，共用逻辑（结果归一、meta 标记）直接写在 materialize 函数里。

> **范式差异**：CC 是"共用基座 + 差异覆盖"（OOP 继承的 spread 版），OpenClaw 是"逐个构造 + 闭包绑定"（函数式注入）。CC 的模板法在"共用逻辑复杂"时更优雅（渲染/截断/权限都在一处），OpenClaw 的闭包法在"逻辑简单"时更直接。

### 5.2 命名：都用了 `server__tool`，但前缀语义不同

| | Claude Code | OpenClaw |
|---|---|---|
| 工具名 | `mcp__<server>__<tool>` | `<serverName>__<toolName>` |
| 全局前缀 | 有 `mcp__` | 无 |
| 字符规范化 | `normalizeNameForMCP`：非法字符 → `_`，正则 `^[a-zA-Z0-9_-]{1,64}$` | `buildSafeToolName`：清洗 + 截断（server≤30, 总≤64） |
| 冲突处理 | server 签名去重（`getServerSignature`） | 冲突自动加后缀 `-2/-3` + 保留名集合（`pi-bundle-mcp-names.ts`） |
| 反向追溯 | `mcpInfo: {serverName, toolName}` | 工具名本身编码了 server（`serverName__` 前缀） |

**关键差异在 `mcp__` 前缀**。CC 的前缀是**权限骨架**（见 §六）——它把 MCP 工具和内置工具（`Read`/`Write`/`Bash`）放在**同一张表**里，必须用前缀区分，否则一个 MCP 工具可能冒充内置 `Write` 绕过 deny 规则。OpenClaw 不加全局前缀，因为它的 MCP 工具直接进 agent tool 列表，**不与"内置工具"混在同一池**需要防冒充——server 名前缀已经足够命名空间隔离。

还有一个 CC 独有的微妙设计：`sdk` 类型 + `CLAUDE_AGENT_SDK_MCP_NO_PREFIX` 时工具名**不加 `mcp__` 前缀**，目的是让 SDK MCP 工具能按名覆盖内置工具；但权限检查仍走全限定名（`mcpInfo` 保留），防冒充。这种"显示名可无前缀、权限名永远全限定"的双轨，OpenClaw 没有。

### 5.3 安全前置：Unicode 消毒（CC 独有）

CC 在封装**之前**对 server 返回的工具数据做 `recursivelySanitizeUnicode`（`client.ts`，§6.2）——递归清理不可见字符、RTL 覆盖符、混淆字符，防恶意 server 通过工具名/描述里的特殊 Unicode 实施 prompt injection 或视觉欺骗。

OpenClaw 的 materialize 流程**没有等价的 Unicode 消毒**（至少源码和文档未见）。这在"消费不可信第三方 MCP server"时是 CC 的一个安全优势。

> **小结**：封装范式 OOP vs 函数式，命名都用 `server__tool` 但 CC 多一个作权限骨架的 `mcp__` 前缀，CC 还多一道 Unicode 防 prompt injection。两者去重策略不同：CC 靠 server 签名，OpenClaw 靠冲突后缀 + 保留名。

---

## 六、权限与安全：威胁建模决定模型形态

这一层最能体现"为什么不同"——两者的权限模型针对**完全不同的威胁**。

### 6.1 威胁建模

| 威胁 | Claude Code 针对 | OpenClaw 针对 |
|---|---|---|
| 仓库投毒 | ✅ `.mcp.json` 提交 git，恶意仓库可能藏危险 server | ❌ 不面临（单用户平台，不团队共享配置） |
| 工具冒充内置 | ✅ MCP 工具冒充 `Write` 绕过 deny | ❌ 不面临（MCP 工具与内置工具不混池） |
| 多入口权限不一致 | — | ✅ agent 路径 / MCP server 路径 / loopback 路径调用工具，权限必须一致 |
| 外部 client 越权 | — | ✅ loopback 被 cron/codex 调用时按调用方裁剪工具 |

### 6.2 Claude Code：两层独立 + 命名驱动

**层 A——project server 首次批准（防仓库投毒）**

`.mcp.json` 里的 server 首次出现时，`getProjectMcpServerStatus`（`utils.ts:351`）返回 `pending`，弹 `MCPServerApprovalDialog`（yes / yes_all / no）。最精妙的是 bypass 模式（`utils.ts:386`）：自动批准条件**故意不读 projectSettings 的实际值**——防止仓库通过 projectSettings 配置代用户同意自己的 server。这是"不信任仓库自身声明"的安全姿态。

**层 B——运行时工具调用权限（复用通用流水线）**

每次调用 MCP 工具时，`checkPermissions`（`client.ts:1814`）返回 `passthrough`，交给与 `Bash`/`Write` 共用的**通用四态流水线**（allow / deny / ask / passthrough）。默认走 `ask` → 交互式对话框（accept-once / accept-session / reject）。

**`mcp__` 前缀是权限骨架**：

- 工具池级 strip（`tools.ts:253`）：组装工具池时，命中 deny 规则的工具**在模型看到列表前就被剔除**；一条 `mcp__server1` deny → 整个 server1 工具消失。
- 全限定名匹配（`mcpStringUtils.ts:60`）：`getToolNameForPermissionCheck` **始终返回全限定名**，即使 `skipPrefix` 让显示名是裸 `Write`，权限检查仍用 `mcp__sdkserver__Write`，防冒充。

**两组 key 不混淆**：`McpjsonServers`（localSettings，首次批准）vs `McpServers`（projectConfig，运行时开关）。

### 6.3 OpenClaw：统一 hook 边界 + loopback 双 token

OpenClaw 没有首次批准、没有工具池级 strip，它的安全重点是**"多入口权限一致"**：

**before-tool-call hook 三路合一**（`src/mcp/plugin-tools-handlers.ts:22`）

无论工具从哪条入口被调用，都强制 `wrapToolWithBeforeToolCallHook`：

- agent 路径（embedded Pi 直接调）
- MCP server 路径（Tools MCP / loopback HTTP 暴露的工具被外部 client 调）
- HTTP 工具执行路径

三条入口走**完全相同的前置 hook 边界**——不存在"MCP 绕过 hook"的特权通道。这是 OpenClaw 安全模型的核心。

**loopback 双 token**（`src/gateway/mcp-http.request.ts:63`）

OpenClaw 独有的"自己当 server 给自己调"场景：loopback HTTP server 用 owner / non-owner 双 token 鉴权，`senderIsOwner` 下游驱动 `applyOwnerOnlyToolPolicy`——**同一个服务按调用方身份裁剪工具集**，而不是起多个服务。codex / cron 调用时如果是 non-owner token，owner-only 工具自动隐藏。

**`/mcp` slash 命令多层 gate**（`src/auto-reply/reply/commands-mcp.ts`）

对话内运维 MCP server 的权限链：owner 校验 → `requireCommandFlagEnabled(cfg,"mcp")` → 写操作额外要 `operator.admin` scope。

### 6.4 对比

| | Claude Code | OpenClaw |
|---|---|---|
| 首次批准（防投毒） | ✅ 两态/三态 UI + bypass 不读 projectSettings | ❌ 无 |
| 运行时权限 | 通用四态流水线（与 Bash/Write 共用） | before-tool-call hook（与 agent/HTTP 路径共用） |
| 工具池级剔除 | ✅ deny 规则 strip 整 server | ❌ 无（靠 profile + `tools.deny:["bundle-mcp"]` 整体开关） |
| 命名防冒充 | ✅ 全限定名匹配 | ❌ 不需要（不混池） |
| 多入口一致 | — | ✅ 三路 hook 合一 |
| 按调用方裁剪 | — | ✅ loopback 双 token |
| Unicode 防注入 | ✅ 封装前消毒 | ❌ 无 |

> **小结**：CC 的权限是"**对抗外部不可信输入**"（仓库投毒、恶意 server、工具冒充），所以模型细、有首次批准、有工具池 strip。OpenClaw 的权限是"**保证自家多入口语义一致**"（agent/MCP/loopback 三条路同等待遇 + 按调用方裁剪），所以模型是统一的 hook 边界。两者都对，因为威胁不同。

---

## 七、认证：完整 OAuth 生态 vs 手动塞 token

这是两者**功能差距最大**的一层。Claude Code 有一个 2465 行的完整认证子系统（`auth.ts`），OpenClaw 的 MCP client 基本没有自动认证。

### 7.1 Claude Code 的认证生态

CC 支持 4 种认证形态，覆盖从"手动配 token"到"一次登录 N 个 server 静默认证"的全谱：

| 形态 | 机制 | 场景 |
|---|---|---|
| **OAuth 2.1** | `performMCPOAuthFlow`（`auth.ts`）：PKCE + 元数据发现（`fetchAuthServerMetadata`）+ 跨进程刷新锁 + 指数退避 | 标准 OAuth MCP server |
| **XAA（SEP-990）** | Cross-App Access：一次登录，N 个 server 用缓存的 id_token 静默认证 | 企业多 server 统一登录 |
| **claude.ai proxy** | `createClaudeAiProxyFetch`（`client.ts:372`）：复用 Claude Code 自身登录 claude.ai 的 token，不跑独立 OAuth | claude.ai connector |
| **手动 headers** | sse/http 的 `headers`/`headersHelper` | 简单 token 场景 |

两个 CC 独有的精巧设计：

1. **`McpAuthTool` 伪工具**（§5.7 of CC 文档）：当 server 处于 `needs-auth` 状态，CC 注入一个"伪工具"进 LLM 工具池，让 **LLM 在会话里代用户触发认证流程**。这让"未认证的 server"对 LLM 仍是可见、可交互的，而不是静默消失。

2. **needs-auth 15min 缓存 + token 快照**（§5.5 / §3.7）：401 后缓存 15min 避免反复探测；`createClaudeAiProxyFetch` 快照发送时的 token（`sentToken`），避免并发 401 时别的 connector 清缓存导致本 connector 误判跳过重试——这是对并发竞态的精细处理。

### 7.2 OpenClaw 的认证现状

OpenClaw 的 MCP client（`src/agents/pi-bundle-mcp-runtime.ts` + `src/agents/mcp-*.ts`）**没有 OAuth 流程**：

- stdio server：靠 `env` 字段传 API key 给子进程；
- http server：靠 `headers` 字段手动塞 `Authorization: Bearer xxx`；
- 没有 token 刷新、没有元数据发现、没有 McpAuthTool 等价物、没有 needs-auth 缓存。

认证失败的 server 只会在连接时报错，被故障隔离逻辑 `warn` 跳过。

### 7.3 实际影响

| 场景 | Claude Code | OpenClaw |
|---|---|---|
| 本地 stdio server（uvx/npx） | 都没问题（不需认证） | 都没问题 |
| 远程 server + 静态 token | `headers` 配 token | `headers` 配 token |
| 远程 server + OAuth | 全自动（PKCE + 刷新 + 缓存） | **不支持**，得自己想办法拿 token 塞 headers |
| 多个 OAuth server | XAA 一次登录静默认证 | 每个 server 独立手动配 |

> **小结**：如果只用本地 stdio server 或静态 token 的远程 server，两者体验相当。一旦涉及 OAuth（很多现代托管 MCP server 需要），CC 体验碾压，OpenClaw 目前是空白。这是 OpenClaw MCP 最值得补强的方向之一（见 §十）。

---

## 八、独有能力：各自不重叠的版图

把两边"对方没有的"列出来，能看清各自的侧重。

### 8.1 Claude Code 独有（消费侧深度）

| 能力 | 说明 |
|---|---|
| **Resources 子系统** | `ListMcpResourcesTool` / `ReadMcpResourceTool` 两个全局工具，LRU 缓存，blob 持久化（§7 of CC 文档） |
| **Prompts → Command** | server 的 `prompts/list` 转成 `mcp__server__prompt` 命令（§8.1） |
| **MCP Skill 提升** | MCP prompt 可提升为 skill，进 `Skill` 工具清单（feature flag `MCP_SKILLS`，§8.3） |
| **Elicitation** | `elicitation/create`——server 主动向 client 请求输入（填表单/URL 认证），让工具从"单向执行"变"可交互"（§10） |
| **IDE transport** | sse-ide / ws-ide，IDE 集成一等公民 |
| **sdk 进程内 transport** | InProcessTransport / SdkControlTransport + skipPrefix 覆盖内置工具 |
| **claudeai-proxy** | 复用自身登录 |
| **Channels（experimental）** | 把权限审批转发到手机（Telegram/iMessage/Discord），与本地对话框竞速——**注意：这不是标准 MCP server 语义**，是 CC 特有的"消息通道"功能 |
| **完整认证生态** | 见 §七 |
| **能力协商 + 分桶并发 + 被动刷新** | 见 §四 |

CC 独有能力集中在"**把 MCP server 的全部原语（tools/resources/prompts）都消费透彻，并做到协议合规与认证完整**"。

### 8.2 OpenClaw 独有（双向 + 跨 runtime）

| 能力 | 说明 |
|---|---|
| **Channel MCP server** | `src/mcp/channel-server.ts`——把 channel 会话（消息/事件/审批）暴露成 MCP server，供外部 client 消费 |
| **Tools MCP server** | `src/mcp/plugin-tools-serve.ts`——把 plugin/内置工具（如 cron）暴露成 MCP server |
| **Loopback HTTP MCP server** | `src/gateway/mcp-http*.ts`——反向自用，让 cron/codex 以标准 MCP client 身份调内部工具，仅绑 127.0.0.1，双 token |
| **跨 provider 配置转译** | `src/agents/cli-runner/bundle-mcp-{claude,codex,gemini}.ts`——当宿主拉起外部 CLI agent 时，把 MCP 配置翻译成各家原生格式（argv/TOML/JSON settings） |
| **acpx proxy 注入** | `extensions/acpx/.../mcp-proxy.mjs`——stdio 中间人，在 ACP session 引导请求里注入 `mcpServers`，零依赖独立脚本 |
| **stdio 进程树安全** | `OpenClawStdioClientTransport` + `killProcessTree`（OOM 调分/detached/进程组清理/EPIPE 防护，§三） |
| **配置指纹热重载** | 运行中改插件配置即 dispose 重建（§4.3） |
| **lease + 空闲回收** | `sessionIdleTtlMs` 默认 10min，避免会话结束残留长连子进程 |
| **plugin bundle `.mcp.json` 合并** | 从插件清单读 MCP 配置，安全文件读取 + 合并（`src/plugins/bundle-mcp.ts`） |

OpenClaw 独有能力集中在"**双向互操作 + 跨 runtime/provider 桥接 + 子进程生命周期治理**"——这些是"平台/路由器"定位才需要的。

### 8.3 版图对比图

```
                  消费侧深度（client）
                         ▲
                         │
        Claude Code ●────┤
                         │
                         │
         ────────────────┼────────────────► 双向/桥接广度（platform）
                         │
                         │      ● OpenClaw
                         │
```

两者几乎不重叠——CC 占据"消费侧深度"象限，OpenClaw 占据"双向桥接"象限。这正是定位差异的直接投射。

---

## 九、设计哲学：为什么不同

把前八节的差异收拢，两者的设计哲学可以归结为两条不同的主线。

### 9.1 Claude Code：协议纯粹 + 消费极致

CC 的每一步都贯彻 MCP"渐进式增强"哲学，并把"消费侧工程化"做到极致：

- **协议纯粹**：能力协商（不假设 server 有什么）、`list_changed` 被动刷新（不轮询）、Resources/Prompts/Elicitation 全原语消费、elicitation capability 故意声明空对象兼容 Java SDK——处处体现"协议说什么就听什么，不偷懒"。
- **消费极致**：模板封装让零胶水成为可能、统一工具体系让 MCP 工具与内置工具平权、OAuth 全生态让远程 server 开箱即用、分桶并发尊重 spawn/网络开销差异、needs-auth 缓存避免无谓探测。

一句话：**CC 是"MCP 客户端的工程教科书"——协议合规、认证完整、并发优雅、封装精巧。**

### 9.2 OpenClaw：双向对称 + 边界收敛

OpenClaw 的每一步都贯彻"双向对称"和"差异收敛到适配层"：

- **双向对称**：同一套底层工具与会话能力，通过 stdio/HTTP 不同 transport、server/client 不同角色同时支撑"对外暴露"与"对外消费"。`openclaw mcp` 一个父命令下，`serve` 是 server、`set/list/unset` 是 client——心智模型对称。
- **边界收敛**：协议语义只在 core + 官方 SDK 实现一次，所有现实差异（跨 provider 配置格式、跨平台命令解析、跨调用方权限）都收敛到薄薄的适配层（cli-runner adapter / mcp-command-line / 双 token）。core 因此保持干净，extension 只做适配，绝不重造协议轮子。

一句话：**OpenClaw 是"MCP 路由器"——双向打通、多 runtime 桥接、把异构差异藏进适配层。**

### 9.3 为什么会这样

根本原因在 §一已经点明：**定位不同**。

- CC 是一个 **coding agent**，MCP 是它的"工具来源"。它要把"用上外部工具"这件事做到极致顺滑（认证、权限、并发、协议合规），所以深耕消费侧。
- OpenClaw 是一个 **agent 平台 + channels 桥**，MCP 是它"众多对外接缝之一"。它要解决的是"如何让 OpenClaw 的能力既被外部 MCP client 消费、又能消费外部 MCP server、还能跨 runtime/provider 桥接"，所以必须双向 + 重适配。

**没有谁对谁错，只有各自服务于自己的核心场景。** CC 不需要 server 侧（它不是平台），OpenClaw 不需要 OAuth 全生态（它的 MCP client 主要连本地 stdio server 和可信配置的远程 server）。

---

## 十、互相借鉴的启示

如果两边要互相学习，最值得动的几处：

### 10.1 OpenClaw 可以向 CC 借鉴

| 借鉴点 | 价值 | 难度 |
|---|---|---|
| **能力协商**（`getServerCapabilities` 后再发原语请求） | 协议合规 + 省往返 + 为未来消费 resources/prompts 留扩展点 | 低（runtime 加一步） |
| **OAuth 认证**（至少 PKCE + token 刷新） | 解锁大量现代托管 MCP server | 高（需回调服务器、元数据发现、token 存储） |
| **`list_changed` 被动刷新** | 感知 server 运行期工具变化，不必重启会话 | 中（runtime 注册 notification handler） |
| **Unicode sanitization** | 防 prompt injection（消费不可信 server 时） | 低（materialize 前加一道递归消毒） |
| **分桶并发连接** | server 数量多时启动更快 | 中（按 stdio/http 分桶 + 限流器） |
| **环境变量 fail-fast**（缺失即 fatal） | 避免降级运行暴露未授权接口 | 低（config 校验加检查） |

最高优先级是**能力协商**和**OAuth**——前者是协议合规的基础，后者是功能版图最大的空白。

### 10.2 CC 可以向 OpenClaw 借鉴

| 借鉴点 | 价值 | 难度 |
|---|---|---|
| **stdio 进程树清理**（OOM 调分 + detached + killProcessTree） | 避免僵尸/孤儿子进程，Linux OOM 场景更稳 | 中（替换官方 StdioClientTransport 或包装） |
| **配置指纹热重载** | 运行中改配置即重建连接，不必重启 | 中（需要 dispose/recreate 生命周期） |
| **lease + 空闲回收**（TTL eviction） | 长会话不累积闲置连接 | 中（在 memoize LRU 之上加 TTL sweep） |
| **"server 侧"思路**（把自身能力暴露为 MCP） | 让 CC 自己也能被别的 client 编排 | 高（架构方向不同，需评估） |

最实用的是**进程树清理**和**空闲回收**——CC 的 memoize LRU 只在配置变更时失效，长时间不用的连接不会被主动回收。

### 10.3 一个有趣的镜像

两者有一个几乎对称的设计：**让外部能力透明融入内部语义**。

- CC：MCP 工具经模板封装后，与内置 `Read`/`Write`/`Bash` **并列在同一张工具表**，LLM 看到统一清单（§1.4 of CC 文档）。
- OpenClaw：外部 MCP 工具经 materialize 后，与 agent 内部工具**统一成 `AnyAgentTool`**，注入 LLM（§3.5 of OpenClaw 文档）；反过来，内部工具经 Tools MCP / loopback **统一成 MCP 协议**，暴露给外部 client。

这是"统一抽象"理念的殊途同归——无论方向，都要把异构外部能力收敛进一套内部模型。

---

## 附：速查对照表

| 维度 | Claude Code | OpenClaw | 差异根因 |
|---|---|---|---|
| 定位 | client 编排器 | 双向协议中枢 | 产品形态（agent vs platform） |
| server 类型 | 7 | 2 | 宿主形态多寡 |
| 配置来源 | 7 scope 合并 | 单文件 CRUD | 团队/企业 vs 单用户 |
| 能力协商 | 严格 | 跳过 | 协议纯粹度 vs 务实 |
| 连接并发 | 分桶 3/20 | 顺序 | server 规模假设 |
| 动态刷新 | list_changed 被动 | 配置指纹主动 | 协议利用 vs 配置驱动 |
| 工具封装 | 模板 spread | materialize 闭包 | OOP vs 函数式 |
| 工具命名 | mcp__server__tool | serverName__toolName | 是否与内置工具混池 |
| 认证 | OAuth 全生态 | 手动 headers | 远程 server 占比 |
| 权限模型 | 两层（防投毒+四态） | 统一 hook（三入口合一） | 威胁建模不同 |
| 进程安全 | SDK 默认 | 自研进程树清理 | 本地 stdio 依赖度 |
| 独有版图 | Resources/Prompts/Skill/Elicitation/IDE/Channels | server 三入口/跨 provider 转译/acpx 注入 | 消费深度 vs 桥接广度 |

---

## 延伸阅读

- Claude Code MCP 全量源码解析：[claude-code-mcp-implementation.md](./claude-code-mcp-implementation.md)
- OpenClaw MCP 架构与设计：[openclaw-mcp-architecture.md](./openclaw-mcp-architecture.md)（含 §七 使用者安装指南）
- OpenClaw agent 运行循环：[openclaw-react-loop-and-flows.md](./openclaw-react-loop-and-flows.md)
- OpenClaw 多模型/provider 适配：[multi-model-adaptation.md](./multi-model-adaptation.md)

> 本文基于两份源码快照时期的实现写成。Claude Code 侧引自 `src/services/mcp/`（约 12300 行），OpenClaw 侧引自本仓库 `src/mcp/`、`src/agents/mcp-*.ts`、`src/agents/pi-bundle-mcp-*.ts`、`src/gateway/mcp-http*.ts`、`src/agents/cli-runner/bundle-mcp-*.ts`。若任一侧协议支持或架构发生不兼容变更（如 OpenClaw 补齐 OAuth、CC 增加 server 侧），以各自源码为准。
