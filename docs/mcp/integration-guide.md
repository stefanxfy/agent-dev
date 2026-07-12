# MCP 从原理到生产集成：MCP原理剖析+工业Agent实现对比+自研Agent集成实践

> **摘要**：本文系统讲解 MCP 从协议原理到生产集成的全链路。先对比 Function Call 暴露的工具生态碎片化、跨 LLM 不通、隔离差四大痛点，引出 MCP "AI 时代 USB" 的定位；再深入原语、capability 协商降级、stdio / streamable-http 传输选型与 cursor 分页。核心对照 Claude Code 与 OpenClaw 两种工业实现，再以自研 Agent 视角走通 Client 集成（McpManager 常驻 loop + 同步门面 + health 调度器 + handler 链注入）与 FastMCP Server 开发。

## 0. 引言

**MCP（Model Context Protocol）一句话**：基于 JSON-RPC 的 LLM ↔ 工具 / 数据 / 模板标准协议，client-server 架构，类似"AI 时代的 USB"。让 LLM 看到统一工具池，工具来源可以是本地进程、远程 HTTP 服务、甚至另一个 Agent。

**本文阅读指引**： 
- 协议核心（MCP 原语 + 传输层选型）
- 两种工业实现对比：Claude Code / OpenClaw
- Client 集成（自研 Agent 视角）
- Server 开发（FastMCP 实战）
- 实战工程踩坑
- 社区生态：找到合适的 MCP server 社区资源

---

## 1. MCP 与 Function Call 的区别（核心动机）

### 1.1 Function Call 是什么

LLM 原生能力：模型生成文本时，可以输出**结构化的工具调用指令**。但**各家 LLM 的格式不统一**——OpenAI 和 Anthropic Claude 的 Function Call 格式**完全不同**，这是 MCP 之前工具调用的私有化现状。

#### OpenAI Function Calling（Chat Completions API）

**请求**里声明 tools：

```json
{
  "model": "gpt-4",
  "messages": [...],
  "tools": [{
    "type": "function",
    "function": {
      "name": "get_weather",
      "description": "Get current weather",
      "parameters": {
        "type": "object",
        "properties": {"city": {"type": "string"}},
        "required": ["city"]
      }
    }
  }]
}
```

**响应**里 LLM 输出 `tool_calls`（注意 `arguments` 是 **JSON 字符串**，不是 object）：

```json
{
  "choices": [{
    "message": {
      "role": "assistant",
      "content": null,
      "tool_calls": [{
        "id": "call_abc123",
        "type": "function",
        "function": {
          "name": "get_weather",
          "arguments": "{\"city\": \"Beijing\"}"
        }
      }]
    }
  }]
}
```

#### Anthropic Claude Tool Use（Messages API）

**请求**里声明 tools（schema 字段叫 `input_schema`，无 `type: function` 包装）：

```json
{
  "model": "claude-3-5-sonnet",
  "messages": [...],
  "tools": [{
    "name": "get_weather",
    "description": "Get current weather",
    "input_schema": {
      "type": "object",
      "properties": {"city": {"type": "string"}},
      "required": ["city"]
    }
  }]
}
```

**响应**里 LLM 输出 `content` 数组里含 `tool_use` 块（注意 `input` 是 **object**，不是字符串）：

```json
{
  "content": [{
    "type": "tool_use",
    "id": "toolu_abc123",
    "name": "get_weather",
    "input": {"city": "Beijing"}
  }]
}
```

#### 关键差异

| 维度 | OpenAI | Claude |
|---|---|---|
| 参数 schema 字段 | `parameters`（嵌套在 `function` 内） | `input_schema`（顶层） |
| 工具声明标识 | `type: "function"` | 无（直接 `name` / `description`） |
| 响应字段 | `tool_calls: [...]`（单独字段） | `content: [{type: "tool_use", ...}]`（与 text 混排） |
| 参数值类型 | `arguments` 是 **JSON 字符串**（需手动 parse） | `input` 是 **object** |
| 调用 ID 前缀 | `call_xxx` | `toolu_xxx` |

**两家不通**——同一份 tool 定义 / 同一份 tool_call 解析代码，要写两套。

#### Agent 框架收到 tool_use 后

无论 OpenAI 还是 Claude 格式，Agent 框架收到 tool_use 后在代码里**硬编码**分发：

```python
if name == "get_weather":
    return await call_weather_api(**arguments)
elif name == "search_web":
    return await search_web(**arguments)
# ... 每个工具一行 if
```

**局限**：
1. 工具必须硬编码在 agent 代码里 —— 加新工具 = 发版
2. 跨 LLM 不通用 —— OpenAI / Claude / Gemini 格式各自不同，同一份 tool 定义要写 N 套；同 LLM 换模型版本也可能 break
3. 跨 agent 不通用 —— A agent 的工具集对 B agent 没用
4. 隔离性差 —— 第三方工具代码跑在 agent 进程里（崩溃 / 权限 / 资源泄漏风险）

### 1.2 MCP 解决什么

MCP 把"工具来源"从 agent 代码里**外置**为独立 server 进程/服务。LLM 看到的还是统一工具池（builtin + MCP 无区别），但工具实现可以：

- 跑在独立进程（崩溃隔离）
- 远程托管（一份实现多 agent 共享）
- 由第三方维护（filesystem / git / db / browser... 各家自己写）

Agent 框架只需做"声明式连接"——配置写一条，剩下全自动。

| 维度 | Function Call | MCP |
|---|---|---|
| 工具定义位置 | agent 代码内 | 独立 server（进程 / 网络） |
| 加新工具 | 改 agent 代码 | 加配置 / 启 server |
| 跨 agent | 不可 | 可（同一 server 多 agent 共享） |
| 隔离性 | 共享进程 | 进程 / 网络边界 |
| 协议标准化 | 各家私有 | JSON-RPC over transport |

### 1.3 为什么需要 MCP（4 个驱动力）

**① 工具生态爆炸**

filesystem / git / postgres / notion / slack / playwright... 每个工具都写一遍 agent 集成 = 灾难。**标准化 = 一次写，各家用**。

**② 跨 Agent 通用**

同一 filesystem server，Claude Code / Cursor / 自研 agent 都能用。一次实现，价值 ×N。

**③ 隔离与安全**

第三方工具代码跑在独立进程 —— 崩溃不挂 agent（stdin pipe 断就 catch），权限边界清晰（server 进程可独立 uid / 沙箱）。

**④ 渐进式增强**

server 升级加了新工具，client **不用改代码**，下次连接自动发现（capability 协商 + 重 list）。

MCP 不是 Function Call 的替代，是**生态层**——把"工具来源"标准化，让 agent 框架专注 reasoning + 调度。

### 1.4 一句话总结

> Function Call 让 LLM 能调函数；MCP 让 LLM 能调**任何地方的函数**，且加新函数不用改 agent 代码。

---

## 2. MCP 协议核心

### 2.1 协议定位

MCP = client-server 架构 + JSON-RPC 2.0，传输层无关（stdio / http / sse 都是实现细节）。

**4 个角色**：
- **Host**：跑 LLM 的进程（agent / IDE / coding agent）
- **Client**：与 server 维持 1:1 连接，维护 session
- **Server**：暴露 tools / resources / prompts 的进程或服务
- **LLM**：在 host 内，消费 client 收集来的能力描述，产出 tool_use

**关键约束**：
- 1 client ↔ 1 server（无中心 hub；多 server = 多连接）
- 每个 session 绑定创建它的 event loop

### 2.2 三类原语

| 原语 | 谁发起 | 谁消费 | 返回 | 类比 |
|---|---|---|---|---|
| **Tools** | LLM（通过 client） | server 执行 | 结果文本 | Function Call |
| **Resources** | client（用户/agent 主动） | server 提供内容 | 文件 / 二进制 | GET 端点 |
| **Prompts** | LLM（通过 client） | server 渲染模板 | 预填 messages | few-shot 模板 |

**扩展原语**（不展开）：
- **Elicitation**：server → user 主动询问（要 UI dialog 支持）
- **Sampling**：server → host 调 LLM（要 host 内 LLM 句柄）
- **Roots**：client → server 声明文件访问边界（paths 约束）

### 2.3 Capability 协商

MCP 不是"server 说有什么 client 就用什么"，而是 **initialize 后 server 声明能力，client 按需取**：

```
initialize  →  capabilities: {tools: {}, resources: {subscribe: true}, prompts: {}}
                ↓
client 解析 caps.tools → 若非 None → list_tools
client 解析 caps.resources.subscribe → 是否开 SSE 推送
```

### 2.3b 能力协商的降级与渐进式增强

协商不只是"握手一步"，还有两段**后续行为**会决定 client 实现复杂度。

#### ① 降级：server 不声明某 capability 时 client 怎么办

server 可能在 `capabilities` 里**完全不声明某原语**（如老版本 server 不实现 resources）。client 端必须**不发起对应 list 原语**——发了一定被拒，且浪费往返。

**项目实际实现**（[client.py:111-147](agent_core/mcp/client.py#L111)）：

```python
async def _negotiate_and_list(session, list_timeout):
    caps = session.get_server_capabilities()
    tools = []
    # caps.tools is None → server 没声明 tools 能力 → 直接返空，不发 list_tools
    if caps is not None and getattr(caps, "tools", None) is not None:
        tools = (await asyncio.wait_for(session.list_tools(), timeout=list_timeout)).tools
    return caps, tools

# resources / prompts 同模式：capability 缺 → 返空 list，不发 list_*
```

**为什么重要**：server 升级加了 tools 能力，client **不用改代码**，下次连接自动发现；server 没声明的能力，client 绝不多发一个注定被拒的请求——这就是 MCP "渐进式增强"哲学的工程落地。

#### ② 渐进式增强：server 运行期新增 capability 怎么感知

协商发生在 `initialize`，是**连接时一次性的**。server 运行期**新加** capability（如运行时挂载新资源池），client 怎么感知？

| 机制 | 协议方法 | 项目处理 |
|---|---|---|
| **list_changed 通知**（已实现原语） | `notifications/tools/list_changed` 等 | [manager.py:412-420](agent_core/mcp/manager.py#L412) 收到后调 `_refresh` 重新 list |
| **新原语新 capability**（连接时未声明） | 无通知，只能下次连接感知 | client 不主动感知；用户调一次会触发 `ensure_connected_client` 重建 |

**关键约束**：list_changed 只覆盖**已声明原语的增减**（"我有 tools，现在 tools 变了"）。如果 server 运行期**首次声明**新 capability（如第一次暴露 resources），必须**断开重连**才能让 client 协商到。

**生产建议**：依赖运行期新增 capability 的设计是脆弱的——更稳的做法是 server 启动时**全量声明**所有会暴露的 capability，运行期只用 list_changed 通知增删具体项。

### 2.4 JSON-RPC 帧结构

MCP 帧 = 标准 JSON-RPC 2.0 + 固定 method 命名空间：

**Request（client → server）**：
```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "method": "tools/call",
  "params": {"name": "add", "arguments": {"a": 1, "b": 2}}
}
```

**Response（server → client）**：
```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "result": {"content": [{"type": "text", "text": "3"}], "isError": false}
}
```

**Notification（无 id，无响应）**：
```json
{
  "jsonrpc": "2.0",
  "method": "notifications/tools/list_changed"
}
```

**Method 命名空间**：
- 会话级：`initialize` / `ping`
- 数据平面：`tools/list` / `tools/call` / `resources/list` / `resources/read` / `resources/subscribe` / `prompts/list` / `prompts/get`
- 推送：`notifications/*`（server → client 单向通知，如 `notifications/tools/list_changed`）

---

## 3. 传输协议

MCP 协议层（JSON-RPC）独立于传输层。MCP 已知 / 潜在的传输协议有：

**官方（生产可用）**：
- **stdio**（3.1）：本地进程，stdin/stdout pipe（一行一帧，LF 分隔）
- **streamable-http**（3.2，**当前推荐**）：HTTP POST + GET SSE

**官方（legacy，已废弃）**：
- **sse**：HTTP GET 一直开着，client→server 也走 POST 到同 URL。已被 streamable-http 取代——新项目别用

**社区偶有讨论（非稳定）**：
- **WebSocket**：MCP spec 提到作为**未来扩展可能**（"可扩展支持 WebSocket 等其他传输方式"），但截至本文写作时**无稳定 SDK 实现**，不建议生产使用

**本章重点**：**stdio**（3.1）和 **streamable-http**（3.2）。

### 3.1 stdio（本地进程）

**模型**：client 启动 server 子进程，通过 stdin/stdout 交换 JSON-RPC 帧（一行一帧，LF 分隔）。

**适用**：
- 本地工具（filesystem / shell / 本地 db）
- 安全敏感场景（server 在受控进程内）
- 一次性任务（短生命周期）

**配置**：
```json
{
  "mcpServers": {
    "filesystem": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"],
      "env": {"PATH": "/usr/local/bin:/usr/bin"}
    }
  }
}
```

**优劣**：
- ✅ 零网络配置
- ✅ 进程隔离天然（子进程崩不挂 host）
- ❌ 每连接 = 1 进程，长时间占用内存
- ❌ Windows 下 subprocess 传参有引号坑（实测遇到过）

### 3.2 streamable-http（HTTP + SSE，**当前推荐**）

**模型**：HTTP POST 承载 client→server 请求；server→client 响应 + 通知走 **GET SSE** 长连接。

**适用**：
- 远程托管（云上 / 集群 / 多 agent 共享）
- 长生命周期 server
- 需要 HTTP 鉴权（Bearer / OAuth headers）
- 多租户场景

**配置**：
```json
{
  "mcpServers": {
    "remote-fs": {
      "url": "https://mcp.example.com/fs",
      "headers": {"Authorization": "Bearer xxx"}
    }
  }
}
```

**优劣**：
- ✅ 多客户端共享一份 server
- ✅ 横向扩展（server 集群）
- ✅ 标准 HTTP 鉴权
- ❌ 网络抖动 / 代理劫持（macOS 系统代理，见 7.x）
- ❌ SSE 静默断（SDK bug，7.1 详述）

---

### 3.3 数据平面扩展：cursor 分页 + resources/subscribe 推送

协议层除传输外还有两个**数据平面扩展**，会影响 client 实现复杂度。

#### 3.3.1 Cursor 分页（list 原语）

`tools/list` / `resources/list` / `prompts/list` 三种 list 原语都可能**分页返回**——server 在结果里带 `nextCursor` 字段，client 必须循环取尽才能拿全。

```json
// 第一页请求
{"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}

// 第一页响应（nextCursor 非空 → 还有更多）
{
  "result": {
    "tools": [...10 个...],
    "nextCursor": "page-2-token"
  }
}

// 第二页请求（带 cursor）
{"jsonrpc": "2.0", "id": 2, "method": "tools/list",
 "params": {"cursor": "page-2-token"}}

// 第二页响应（nextCursor 为空/缺 → 终止）
{"result": {"tools": [...5 个...], "nextCursor": null}}
```

#### 3.3.2 resources/subscribe 推送

server 可以对单个 resource 声明 `subscribe: true` capability，client 发 `resources/subscribe` 订阅后，server 在该 resource 内容变化时主动推 `notifications/resources/updated` 通知。

```
client                              server
  │  resources/subscribe(uri=X) ──► │
  │ ◄─────────────────────────────  │ (ack)
  │                                 │
  │        ... X 内容变化 ...        │
  │                                 │
  │ ◄── notifications/resources/updated {uri: X} ──│
```

```python
elif isinstance(notif, types.ResourceUpdatedNotification):
    logger.debug(
        "🔌 mcp server '%s' resource updated: %s（MVP 不做 subscribe 细粒度刷新）",
        name, getattr(getattr(notif, "params", None), "uri", "?"),
    )
```

**生产建议**：需要"resource 变了自动同步"时，要么由 client 周期性 re-list（轮询），要么升级到订阅模式——但订阅模式要小心断连重订阅的复杂度。

---

## 4. 两种工业实现对比（Claude Code vs OpenClaw）

### 4.1 Claude Code：纯 client 编排器

**定位**：Anthropic 官方 coding agent。**只做 client，不暴露 MCP 服务**——它本身是一个消费侧。


**架构特点**：
- 多 server 编排：每个 server 一个 mcpClient 子类，统一接入 IDE 工具池
- 权限极细：每个 MCP tool 走 PermissionEngine ASK（用户粒度）
- 重连策略偏被动：HTTP 连接级连续错误达 `MAX_ERRORS_BEFORE_RECONNECT = 3`（`client.ts:1228`）后手动 `close()` + 重连，stdio 不重连（实测权衡）

> **源码时效说明**：本文对 Claude Code 的分析参考自 2026 年 4 月网络上暴露出的源码。**现发行的 Claude Code 是否自己也充当 Server 角色（如 OpenClaw 那样双向），未做深入研究**——本文不排除此可能，"只做 client"的判断以当时源码为准。

### 4.2 OpenClaw：双向协议中枢

**定位**：agent 平台 + channels 桥。**既当 client 也当 server**——这是它和 Claude Code 最大的不同。

**双向含义**：
- **作为 client**：连外部 MCP server（filesystem / db / 浏览器 / 自定义业务 server）
- **作为 server**：把自身 agent 能力暴露为 MCP server，让其他 agent 反向调用（"agent as a tool"）

**架构特点**：
- **生命周期治理更细致**：连接池、限流、断路器——因为 server 角色要求高可用
- **channels 桥**：通过 MCP 把 IM 平台（Telegram / Slack / Discord）抽象为统一通道
- **Roots 用 channel 路径**：每个对话 session 都有自己的文件访问边界

### 4.3 MCP 支持能力详细对比

下面从 **14 个维度** 系统对比二者在 MCP 支持上的差异——这是本文的核心对比表，建议作为选型参考。

| 维度 | Claude Code | OpenClaw |
|---|---|---|
| **角色定位** | client only | **双向**（client + server） |
| **语言 / 规模** | TypeScript ~12300 行 | TypeScript（更大） |
| **原语：Tools** | ✅ 完整消费 | ✅ 暴露 + 消费 |
| **原语：Resources** | ✅ 完整消费 | ✅ 暴露 + 消费 |
| **原语：Prompts** | ✅ 完整消费 | ✅ 暴露 + 消费 |
| **原语：Elicitation** | ✅ 完整消费（声明 `elicitation:{}` capability + `registerElicitationHandler` 收 `elicitation/create`） | ✅ 支持 |
| **原语：Sampling** | ✅ 支持 | ✅ 支持 |
| **Roots** | cwd（声明项目根，capability `roots:{}`） | channel 路径（每个 session 独立） |
| **list_changed** | ✅ 动态刷新（被动） | ✅ 动态刷新 |
| **传输协议** | **7 种**：stdio / sse / sse-ide / http / ws / ws-ide / sdk / claudeai-proxy | stdio + http（主用） |
| **session 管理** | 多 client 编排（Promise 链） | **连接池 + 限流 + 断路器** |
| **重连 / 健康** | http 连接级错误达 `MAX_ERRORS_BEFORE_RECONNECT = 3` 后 close + 重连（`client.ts:1216,1228`），stdio 不重连 | 连接池主动感知（被动 + 主动混合） |
| **权限模型** | **极细**（每 tool 都 ASK 用户） | 中等（按 channel 隔离） |
| **server 暴露（agent as tool）** | ❌ 完全不做 | ✅ 大量做（核心能力） |
| **跨 agent / 跨进程共享 server** | ❌ 仅单 agent | ✅ 多 agent + channels 桥 |
| **生态定位** | IDE / CLI 单兵作战 | **平台**（channels + agent + server 三位一体） |

### 4.4 共识与分歧

虽然走的是两条不同的设计路线，但二者在 **3 个点上完全共识**——可以作为"MCP client 实现的事实标准"参考。

**共识**（都不约而同这么做）：
- 都用 `mcp__<server>__<tool>` 命名（防 builtin 冒充，命名空间隔离）
- 都做 capability 协商（不假定 server 有什么，按需取）
- 都尊重 list_changed（运行时工具集变更，刷新 ToolRegistry）

**分歧**（设计取舍不同）：
- **核心定位**：Claude Code 走"agent 即产品"路线（一个 agent 满足一个用户）；OpenClaw 走"agent 即平台"路线（一个 agent 服务多个用户/渠道）。这决定了 server 角色是否要做、连接池是否要建、权限粒度多细
- **重连模型**：Claude Code 偏被动（HTTP `MAX_ERRORS_BEFORE_RECONNECT = 3` 是底线，stdio 不重连）；OpenClaw 连接池主动感知（更适合 server 角色的高可用要求）
- **权限粒度**：Claude Code 极细（每 tool 都询问用户）；OpenClaw 按 channel 隔离（一次授权，整个 channel 内的工具通用）
- **生命周期复杂度**：Claude Code 单进程多 server 编排（Promise 链清晰）；OpenClaw 连接池 + 限流 + 断路器（生产级可靠性，但实现复杂度高）
- **双向 vs 单向**：Claude Code 完全不做 server 暴露（scope 收敛）；OpenClaw 把双向作为核心能力（agent as tool 是产品差异化）

**给读者的选型参考**：
- 做 **IDE / CLI / 单 agent 工具** → Claude Code 路线（轻量、聚焦、消费侧做透）
- 做 **平台 / 多 agent 协作 / channels 桥** → OpenClaw 路线（双向、连接池、生产级）
- 做 **自研 Agent 集成 MCP** → 参考 Claude Code 的消费侧深度 + OpenClaw 的生命周期治理（Chapter 5 自研 Agent 正是这套组合）

---

## 5. AI Agent MCP Client 集成（自研 Agent 视角）

> 实现参考自 Claude code 和 openclaw，从 使用者角度出发，一个MCP Server 是怎么安装给AI Agent的？Agent是如何识别这个MCP Server的？再到LLM 问答时 如何调用这个MCP Server的？

### 5.1 一个最小 demo（FastMCP + SDK 端到端 Hello）

为了直观，先看一个**最小可跑**的 demo（FastMCP 写 server + 官方 MCP SDK 写 client）：

**`server.py`** —— 用 FastMCP 写一个暴露 `add` 工具的 server：

```python
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("hello")

@mcp.tool()
def add(a: int, b: int) -> int:
    """两数相加。"""
    return a + b

if __name__ == "__main__":
    mcp.run(transport="stdio")   # 走 stdin/stdout pipe
```

**`client.py`** —— 用 SDK 写一个 client，发现并调用 `add`：

```python
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
import asyncio

async def main():
    # ① 启 server 子进程，通过 stdin/stdout pipe 通信
    params = StdioServerParameters(command="python", args=["server.py"])
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            # ② 握手（initialize）+ capability 协商
            await session.initialize()

            # ③ 发现 server 暴露的工具
            tools = (await session.list_tools()).tools
            print("Tools:", [t.name for t in tools])

            # ④ 实际调用
            result = await session.call_tool("add", {"a": 1, "b": 2})
            print("add(1, 2) =", result.content[0].text)

asyncio.run(main())
```

**跑**：

```bash
$ python client.py
Tools: ['add']
add(1, 2) = 3
```

### 5.2 自研AI Agent 集成 MCP Client 端到端流程

```
┌──────────────────────────────────────────────────────────────────────┐
│ 【阶段 A：使用者安装 MCP Server】                                     │
│                                                                       │
│   $ npm install -g @modelcontextprotocol/server-filesystem           │
│        ↓                                                              │
│   settings.json 加配置:                                               │
│   {                                                                   │
│     "mcp": { "servers": { "filesystem": { "command": "npx ... " }}}  │
│   }                                                                   │
└──────────────────────────────────────────────────────────────────────┘
                                ↓
┌──────────────────────────────────────────────────────────────────────┐
│ 【阶段 B：Agent 启动 → 接入】                                         │
│                                                                       │
│   McpManager 启动                                                     │
│     ├─ load_mcp_config(settings.json) → [server configs]              │
│     ├─ server 级 deny strip                                           │
│     ├─ 启常驻 mcp_loop 线程（daemon）                                 │
│     ├─ spawn per-server 长连接 task                                │
│     │    ├─ stdio: spawn subprocess + pipe                            │
│     │    └─ http:  建 HTTP session                                    │
│     ├─ initialize + capability 协商                                   │
│     ├─ list_tools → [Tool, Tool, ...]                                 │
│     └─ materialize → [ToolDef, ToolDef, ...]                          │
│           └─ ToolDef.handler = 闭包 → manager.call_tool               │
│                                                                       │
│   register 到 ToolRegistry:                                           │
│     ToolRegistry                                                      │
│     ├── Read        (builtin)                                         │
│     ├── Bash        (builtin)                                         │
│     ├── mcp__filesystem__read_file     (← 来自 filesystem server)     │
│     ├── mcp__filesystem__write_file    (← 同上)                       │
│     └── mcp__remote_db__query          (← 来自 remote-db server)     │
└──────────────────────────────────────────────────────────────────────┘
                                ↓
┌──────────────────────────────────────────────────────────────────────┐
│ 【阶段 C：LLM 看到 tools → 发起 tool_use → Server 执行 → 回填结果】    │
│                                                                       │
│   LLM 请求:                                                           │
│     system_prompt + tools=[Read, Bash, mcp__filesystem__read_file,    │
│                              mcp__filesystem__write_file,             │
│                              mcp__remote_db__query]                   │
│                                                                       │
│   LLM 返回 tool_use:                                                  │
│     { name: "mcp__filesystem__read_file",                             │
│       input: { "path": "/tmp/x.txt" } }                               │
│                                                                       │
│   tool_chain 路由:                                                    │
│     PermissionEngine.check → ToolRegistry.execute → handler 闭包      │
│                                                                       │
│   handler 闭包 → manager.call_tool("filesystem", "read_file", args)  │
│                                                                       │
│   mcp_loop 线程:                                                      │
│     await session.call_tool("read_file", args)                        │
│       → server subprocess / HTTP 端执行                                │
│       → 返回 CallToolResult                                            │
│       → 归一化字符串                                                   │
│                                                                       │
│   tool_result 注入 agent.messages → LLM 下一轮                        │
└──────────────────────────────────────────────────────────────────────┘
```

**三阶段一句话总结**：
- **A 安装**：用户在 settings.json 声明 server
- **B 接入**：Agent 启动时把 server 暴露的工具**注入** ToolRegistry
- **C 调用**：LLM 看到的 tool 名是 `mcp__<server>__<tool>`，调用走和 builtin 工具**完全相同的** tool_chain 路径

### 5.3 模块组成（功能模块 + 关系）

```
                              ┌─────────────────────────┐
                              │  settings.json          │
                              │  (mcp.servers 配置)     │
                              └────────────┬────────────┘
                                           │ load
                                           ▼
┌─────────────────────────────────────────────────────────────────────┐
│                    McpManager（组合根）                              │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐ │
│  │  Config     │  │   Client    │  │ Materializer│  │  Health     │ │
│  │  Parser     │  │   Connect   │  │ MCP→ToolDef │  │  Scheduler  │ │
│  └──────┬──────┘  └──────┬──────┘  └──────┬──────┘  └──────┬──────┘ │
│         │                │                │                │        │
│         ▼                ▼                ▼                ▼        │
│  ┌───────────────────────────────────────────────────────────────┐  │
│  │              常驻 mcp_loop 线程（daemon）                      │  │
│  │   ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────┐    │  │
│  │   │ server-A │  │ server-B │  │ server-C │  │  ...     │    │  │
│  │   │ _keep_   │  │ _keep_   │  │ _keep_   │  │          │    │  │
│  │   │ alive    │  │ alive    │  │ alive    │  │          │    │  │
│  │   └────┬─────┘  └────┬─────┘  └────┬─────┘  └──────────┘    │  │
│  └───────┼──────────────┼──────────────┼────────────────────────┘  │
└──────────┼──────────────┼──────────────┼───────────────────────────┘
           │              │              │
           ▼              ▼              ▼
     ┌──────────┐   ┌──────────┐   ┌──────────┐
     │ server-A │   │ server-B │   │ server-C │
     │ subprocess   │   HTTP    │   HTTP     │
     └──────────┘   └──────────┘   └──────────┘
```

**5 个功能模块**：

| 模块 | 职责 | 输入 | 输出 |
|---|---|---|---|
| **Config Parser** | 解析 settings.json | settings.json | `[server configs]` |
| **Client Connect** | 单 server async 连接 + initialize + list_tools | server config | `[MCP Tool]` |
| **Materializer** | MCP Tool → ToolDef（含 handler 闭包 + 归一化） | `[MCP Tool]` | `[ToolDef]` |
| **Health Scheduler** | 周期 probe + 自动重连 + list_changed 响应 | `{status, last_check_at, last_error}` | 健康状态 + 工具集变更通知 |
| **McpManager（组合根）** | 同步门面 + 常驻 loop + 跨 server 协调 + 生命周期管理 | 所有模块 | 对外 API（connect_all / call_tool / dispose） |

**模块关系**（数据流向）：

```
settings.json ──► Config Parser ──► Client Connect ──► Materializer ──► ToolRegistry
                       │                  │                  │
                       └──────────────────┴──────────────────┘
                                          │
                                   Health Scheduler
                                  （周期驱动 + 状态查询）
```

**ToolRegistry 注入链路**（关键）：
```
MCP Tool (from server)
  → Materializer 包装为 ToolDef (handler=闭包)
    → ToolRegistry.register
      → 下次 ToolRegistry.list_schemas() 时被 LLM 看到
        → LLM 返回 tool_use(name="mcp__<server>__<tool>")
          → ToolRegistry.execute 路由到 handler
            → handler 闭包 → manager.call_tool
```

**对 LLM 完全透明**：MCP 工具和 builtin 工具在 ToolRegistry 里**没有任何区分**——LLM 看到的工具列表、发出的 tool_use、收到的 tool_result 都用同一套 schema。区别只在 tool 名前缀 `mcp__`。

### 5.4 接入阶段：Server 安装 → 注入 ToolRegistry

**这一阶段的目标**：把"外部 MCP Server 的工具"变成"ToolRegistry 里的 ToolDef"。

```
[settings.json 配置]
       ↓ Config Parser
[server configs 列表]
       ↓ server 级 deny strip（用户配的 deny rule 过滤）
[enabled server configs]
       ↓ 启常驻 mcp_loop 线程
[mcp_loop alive]
       ↓ 并行 spawn per-server task
[每个 server 一个长连接 task]
       ├─ stdio: spawn subprocess (stdin/stdout pipe)
       └─ http:  建 HTTP session
       ↓
[async with connect_server]
       ├─ transport.open()
       ├─ ClientSession(read, write)
       ├─ await session.initialize()
       ├─ caps = session.get_server_capabilities()
       └─ if caps.tools: await session.list_tools() → [MCP Tool]
       ↓
[Materializer]
       ├─ 每个 MCP Tool → ToolDef
       ├─ ToolDef.name = "mcp__<server>__<tool>"（安全化）
       ├─ ToolDef.category = "mcp"
       └─ ToolDef.handler = 闭包 → manager.call_tool(server, tool, args)
       ↓
[ToolRegistry.register(td) 每个 ToolDef]
       ↓
[工具池扩展完成]
```

**关键代码片段（Materializer 的 handler 闭包）**：

```python
# 每个 MCP Tool 被包装为一个同步 ToolDef，handler 闭包是关键：
def make_handler(manager, server, tool):
    def handler(**kwargs):
        return manager.call_tool(server, tool, kwargs)   # 同步门面
    return handler

# ToolDef 物化后：
td = ToolDef(
    name="mcp__filesystem__read_file",
    description="Read a file from filesystem server",
    parameters={"type": "object", "properties": {"path": {"type": "string"}}, ...},
    handler=make_handler(mgr, "filesystem", "read_file"),  # ← 闭包绑定
    category="mcp",
)
tool_registry.register(td)
```

**命名与权限**：

```
ToolRegistry 工具池
├── Read                  (builtin)
├── Bash                  (builtin)
├── mcp__filesystem__read_file    ← MCP 工具
├── mcp__filesystem__write_file
└── mcp__remote_db__query

命名规则：mcp__<server>__<tool>（与 builtin 单池混用，前缀防 spoofing）
权限模型：所有 MCP tool category="mcp"，PermissionEngine 默认 ASK
server 级 deny strip：用户配 Bash(mcp__*) 或 mcp__dangerous__* → 该 server 全工具不注册
双重保险：组合根构造时 strip（第二道）+ manager 内置第一道 strip
```

**对 LLM 完全透明**：接入阶段结束后，LLM 看到的 tools 列表会通过 `ToolRegistry.list_schemas()` 自动包含 MCP 工具。**LLM 不需要任何特殊配置**——它只看到一个统一工具池。

### 5.5 调用阶段：LLM tool_use → Server 执行

**这一阶段的目标**：LLM 发出的 `tool_use(name="mcp__...")` 被正确路由到对应 server 并取回结果。

```
[LLM 看到 tools 列表（包含 builtin + mcp__*）]
       ↓ LLM 推理后返回 tool_use
[tool_use: { name: "mcp__filesystem__read_file", input: {"path": "/tmp/x.txt"} }]
       ↓
[agent tool_chain 收到 tool_use]
       ↓ PermissionEngine.check(tool_def, input)
       ↓   • MCP tool 默认 ASK
       ↓   • 若 ASK → 等待用户确认（已有 UI）
       ↓   • 若 ALLOW / DENY → 继续 / 返回 error
       ↓
[ToolRegistry.execute("mcp__filesystem__read_file", {"path": "/tmp/x.txt"})]
       ↓ 找到 ToolDef，调用 td.handler(**kwargs)
[handler 闭包 → manager.call_tool("filesystem", "read_file", {"path": "/tmp/x.txt"})]
       ↓ 跨线程（主线程 → mcp_loop 线程）
[await session.call_tool("read_file", {"path": "/tmp/x.txt"})]
       ↓
[MCP Server 执行]
       ├─ stdio: subprocess 读 stdin JSON-RPC 帧 → 执行 → 写 stdout
       └─ http:  HTTP POST /tools/call → server 执行 → 返回 CallToolResult
       ↓
[CallToolResult 归一化]
       ├─ 遍历 content: text / image / audio / resource
       ├─ isError=true → 标记 error
       ├─ structuredContent → 追加 JSON
       └─ _sanitize_unicode（控制字符转义，防 prompt injection）
       ↓
[归一化字符串]
       ↓
[ToolRegistry 返回 {"status": "success", "output": "..."}]
       ↓
[tool_chain 拼成 tool_result 消息，注入 agent.messages]
       ↓
[LLM 下一轮看到 tool_result → 继续推理]
```

**关键代码片段（manager.call_tool 同步门面）**：

```python
# 同步门面：主线程调用，跨线程到 mcp_loop 取结果
def call_tool(
    self,
    server: str,
    tool: str,
    arguments: dict,
    *,
    timeout: Optional[float] = None,   # 缺省 = self._call_timeout (60s)
) -> str:
    """call_tool → 跨线程 run_coroutine_threadsafe 到 mcp_loop；返回归一化字符串。
    异常: server 未连接 → RuntimeError；超时 → TimeoutError。
    """
    fut = asyncio.run_coroutine_threadsafe(
        self._async_call_tool(server, tool, arguments),
        self._loop,                # 常驻 mcp_loop 线程的 event loop
    )
    return fut.result(timeout=timeout)   # 超时 = 真取消（cancel 协程）
```

**对调用方完全透明**：tool_chain 不需要知道这个 tool 是 builtin 还是 MCP——它只调 `ToolRegistry.execute(name, args)`。区别只在 ToolRegistry 内部的 handler 闭包指向哪里（builtin → 本地函数；MCP → manager.call_tool）。

### 5.5b Handler 链注入段（MCP 数据如何进 system prompt / tool schemas）

§5.4-§5.5 讲了"工具怎么进 ToolRegistry + 怎么被调"——但 MCP 数据**还有第二条路径**进 LLM：`prompts` 名单段注入到 `system_prompt`、`tools` schemas 注入到 LLM 请求。这条路径走的是 `turn_chain` 的 handler 链，不在 ToolRegistry 里。

#### 注入链路

```
              ┌─────────────────────────────────────────────┐
              │  agent builder（builder.py:97,101）          │
              │  注册到 inputs_chain（按顺序）：             │
              │   • MemoryRetrievalHandler                   │
              │   • SystemPromptHandler                      │
              │   • ToolsSchemaPrepareHandler                │
              │   • McpPromptsHandler       ← MCP prompts 注入 │
              └────────────────┬────────────────────────────┘
                               │ 每个 turn 跑一次
                               ▼
   ┌──────────────────┐    ┌──────────────────────┐
   │ mcp_loop 线程     │    │ 主线程 turn_chain    │
   │ McpManager       │    │                      │
   │ registered_      │    │ McpPromptsHandler   │
   │ prompts()        │───►│   ↓                 │
   │ → {server:[...]} │    │ render_mcp_prompts_ │
   └──────────────────┘    │   section(...)       │
                           │   ↓                 │
                           │ ctx.run_state.append│
                           │   _system(section)  │
                           └─────────┬───────────┘
                                     ▼
                           ┌──────────────────────┐
                           │ run_state.system_    │
                           │   prompt 累积        │
                           │ （含 skills 段 +     │
                           │   mcp prompts 段）   │
                           └─────────┬───────────┘
                                     ▼
                           ┌──────────────────────┐
                           │ ToolsSchemaPrepare   │
                           │ Handler（chain 末位）│
                           │   ↓                 │
                           │ 按 provider 格式     │
                           │ 组装 tool_schemas    │
                           │ （builtin + mcp__*） │
                           └─────────┬───────────┘
                                     ▼
                                LLM 请求
```

#### 与 §5.5 调用阶段的分工

- **§5.5 调用阶段**：讲 tool_use 怎么**被路由到 server**（ToolRegistry.execute → handler 闭包 → manager.call_tool）
- **§5.5b Handler 链**：讲 MCP 数据怎么**进 LLM 请求**（prompts 段、tools schemas）
- 两条路径**互不依赖**——prompts 段不影响工具调用，工具调用也不依赖 prompts 段是否注入

**为什么是独立小节**：不熟悉 handler chain 的读者按 §5.4-§5.5 流程图跑起来，会发现 system prompt 里多了 `<available_mcp_prompts>` 段但找不到出处——它就在这里。

### 5.6 同步门面（核心架构：Producer–Consumer 视角）

**核心关系**：主线程 = **Producer**（生产 tool 调用任务），mcp_loop 线程 = **Consumer**（执行任务）。它们通过 **跨线程 Future** 通信——主线程提交任务后阻塞等结果，mcp_loop 异步执行并写回 Future。

```
                Producer                          Consumer
            ┌─────────────┐                  ┌─────────────────┐
            │  主线程      │                  │  mcp_loop 线程   │
            │ (Streamlit / │                  │  (常驻 daemon)  │
            │  agent.run)  │                  │                 │
            └──────┬──────┘                  └────────┬────────┘
                   │                                   │
   ① 构造 task ─────┤                                   │
   (async coro)      │                                   │
                   │   ② 跨线程投递 task                  │
                   │   (run_coroutine_threadsafe)        │
                   ├──────────────────────────────────►│
                   │                                   │
                   │   ┌──── Future (跨线程句柄) ────┐  │
                   │   │ 主线程持有引用              │  │
                   │   │ mcp_loop 写回结果           │  │
                   │   └─────────────────────────────┘  │
                   │                                   │
                   │   ③ mcp_loop 调度协程                │
                   │                                   │──┐
                   │                                   │  │ await session.call_tool(...)
                   │                                   │  │     │
                   │                                   │  │     ▼
                   │                                   │  │   [MCP Server]
                   │                                   │  │     │
                   │                                   │◄─┘  ◄──┘
                   │                                   │
                   │   ④ fut.set_result(...)           │
                   │◄──────────────────────────────────┤
                   │                                   │
   ⑤ fut.result(timeout) 阻塞取结果                    │
                   │                                   │
                   ▼                                   │
            ⑥ 归一化字符串返回                           │
```

**5 步交互流程**：

1. **Producer 构造 task**：构造异步协程 `_async_call_tool(server, tool, args)`
2. **Producer 跨线程投递**：`run_coroutine_threadsafe(coro, loop)` —— 投递给 Consumer，拿到跨线程 `Future`
3. **Consumer 执行**：loop 内调度 coro，`await session.call_tool(...)` 真正执行
4. **Consumer 写回**：执行完调 `future.set_result(...)`，写回结果
5. **Producer 阻塞取结果**：`future.result(timeout=60)` 阻塞等结果（带超时 / 背压）

**关键设计点**：

- **Future 是跨线程桥梁**：Producer 持有 Future 引用，Consumer 写回结果——这就是"任务队列"
- **Producer 阻塞取结果**（`fut.result(timeout)`）—— 这是背压机制：主线程不会无限堆积 task，超过 timeout 抛 `TimeoutError`
- **Consumer 持续运行** —— mcp_loop 是 daemon 线程 + `loop.run_forever()`，一直在那等任务
- **双层 timeout 兜底 = 真取消** —— 项目实际用的是 **两层** 取消机制（`manager.py:_run`）：
  ```python
  async def _wrap():
      task = asyncio.ensure_future(coro)
      try:
          # 内层：asyncio.wait_for 触发 task.cancel()（真取消正在执行的协程）
          return await asyncio.wait_for(task, timeout=timeout)
      except asyncio.TimeoutError:
          task.cancel()    # 显式 cancel 兜底（wait_for 已自动 cancel，这里双保险）
          raise TimeoutError(f"MCP 操作超时({timeout}s)") from None

  fut = asyncio.run_coroutine_threadsafe(_wrap(), self._loop)
  # 外层：fut.result 等 thread-safe Future，比 timeout 多 2s 兜底
  return fut.result(timeout=timeout + 2)
  ```
  - 内层 `asyncio.wait_for(task, timeout)` 是**真取消**：超时会 cancel 协程。
  - 外层 `fut.result(timeout=timeout+2)` 多 2s 宽限，主要兜住"协程 cancel 信号已发出但协程本身收尾（finally 等）拖时间"的边界——正常情况由内层先超时，外层基本不触发。

**为什么必须常驻 Consumer（mcp_loop）**：
MCP `ClientSession` 创建时绑定 event loop，session 不能跨 loop 用。`asyncio.run` 临时 loop 会在 loop 退出后让 session 失效（实测 worker 泄漏）。所以 Consumer 必须长存——这也正是"task 队列"能成立的前提。

### 5.7 生命周期管理

```
          ┌─────────┐
          │ created │  McpManager(server_configs)
          └────┬────┘
               │ connect_all() 同步阻塞直到首轮 ready / 失败
               ▼
     ┌──────────────────┐
     │  connecting      │  per-server 长连接 task spawn
     │  (transient)     │
     └────┬─────────────┘
          │
          ▼
   ┌──────────────┐    health-check probe 失败
   │  connected   ├─────────────────────────┐
   │              │◄────────────────────────┤
   └──────┬───────┘  probe 成功              │
          │                                  │
          │ server kill                      │
          ▼                                  ▼
   ┌──────────────┐                  ┌──────────────┐
   │ disconnected │                  │  connecting  │
   │ (transient)  │                  │  (retry)     │
   └──────┬───────┘                  └──────────────┘
          │
          │ dispose()  / atexit
          ▼
     ┌─────────┐
     │ disposed │  不可逆，mgr 不再可用
     └─────────┘
```

**关键 API**：
- `connect_all()`：阻塞同步；返回 `dict[server_name, list[ToolDef]]`；per-server 故障隔离
- `call_tool(server, tool, args, timeout=60)`：同步；超时抛 `TimeoutError`；返回归一化字符串
- `dispose()`：幂等；set dispose_events + stop loop + join thread
- `atexit.register(self.dispose)`：进程退出兜底

### 5.8 健康检查调度器（时序图）

```
mcp_loop 线程：

[scheduler]──sleep(30s)──► 取 top-K 最久未检测
                                 │
                                 ▼  并行
                ┌──── probe(fs) ────┐  ┌──── probe(remote-db) ───┐
                │ list_tools+timeout│  │ list_tools+timeout      │
                └───────┬───────────┘  └──────────┬──────────────┘
                        │                         │
                  成功/失败                   成功/失败
                        │                         │
                        ▼                         ▼
            health[fs] =                     health[remote-db] =
            {connected, ts, None}          {disconnected, ts, "timeout"}
                        │                         │
                        └────────────┬────────────┘
                                     │
                                     ▼
                          get_health() 快照
                                     │
                                     ▼
                          [sidebar MCP Servers]
                          🟢 filesystem   connected
                          🔴 remote-db    disconnected
```

**关键设计**：
- **主动 probe**：用 `list_tools`（轻量 RPC）做心跳，失败即视为断连
- **disconnected 自动重连**：状态切到 disconnected 后，调度器异步发起重连，不阻塞下一轮 probe
- **top-K 而不是全量**：默认 concurrency=5，按"最久未检测"优先，错开不批量（避免 spike）
- **状态快照**：`{status, last_check_at, last_error}` 三字段，GIL 下主线程读安全

**环境变量**：
- `MCP_HEALTH_CHECK_INTERVAL`（默认 30 秒）
- `MCP_HEALTH_CHECK_CONCURRENCY`（默认 5）

### 5.9 故障隔离（三层边界）

```
┌──────────────────────────────────────────────┐
│ 第一层：per-server 隔离（接入阶段）           │
│   • 每个 server 独立 try/except               │
│   • 一个失败只 warn + skip，不影响其他        │
└──────────────────┬───────────────────────────┘
                   ▼
┌──────────────────────────────────────────────┐
│ 第二层：单 server 内部隔离（连接生命周期内）   │
│   • transport → session → list_tools          │
│     → materialize 任一步失败只 warn 该 server │
└──────────────────┬───────────────────────────┘
                   ▼
┌──────────────────────────────────────────────┐
│ 第三层：call_tool 隔离（tool 调用阶段）       │
│   • 调 MCP tool 抛异常 → 捕获                 │
│   • 返回 {"status": "error", "output": "..."} │
│   • agent 不挂，继续下一轮                    │
└──────────────────────────────────────────────┘
```

**关键不变式**：任何一层的失败都**不向上传播**——要么 skip、要么降级、要么返回 error，永远不让整个 agent 挂掉。

### 5.10 配置（settings.json）

```json
{
  "mcp": {
    "roots": [{"uri": "file:///Users/me/projects", "name": "projects"}],
    "servers": {
      "filesystem": {
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"],
        "env": {"PATH": "/usr/local/bin:/usr/bin"},
        "enabled": true,
        "connect_timeout": 30
      },
      "remote-db": {
        "type": "http",
        "url": "https://mcp.example.com/db",
        "headers": {"Authorization": "Bearer xxx"}
      }
    }
  }
}
```

**schema 字段**：
- `type`：stdio / http / sse（缺省按 `command`/`url` 自动推断；`sse` 归一化为 http 走 streamable-http transport；别名 `transport` 也接受）
- `enabled`：true / false（缺省 true）
- `connect_timeout`：init + list_tools 总超时（缺省 30s）
- `roots`：client 级，非 per-server（缺省 cwd）

完整 server 级字段（每个 server 一份）

| JSON 字段 | 类型 | 适用 transport | 缺省 | 说明 |
|---|---|---|---|---|
| `type` | string | 通用 | 按 `command`/`url` 推断 | `stdio` / `http` / `sse`；`sse` 走 streamable-http；别名 `transport` |
| `command` | string | stdio | 必填 | 子进程可执行文件 |
| `args` | list[string] | stdio | `[]` | 子进程参数 |
| `env` | object | stdio | `None`（SDK 用默认白名单 env） | 显式传 `PATH` 等避免 server 缺环境变量 |
| `cwd` | string | stdio | `None` | 子进程工作目录 |
| `url` | string | http | 必填 | HTTP 端点 URL |
| `headers` | object | http | `None` | 自定义 HTTP 头（如 `Authorization`） |
| `timeout` | number | http | MCP SDK 默认 | HTTP 总超时（秒） |
| `sse_read_timeout` / `sseReadTimeout` | number | http | MCP SDK 默认 | GET SSE 长连接读超时 |
| `enabled` | bool | 通用 | `true` | `false` → 该 server 跳过连接（仍参与 health 监控全集） |
| `connect_timeout` / `connectTimeout` | number | 通用 | `30.0` | init + list_tools 总超时（秒） |

#### client 级字段（顶在 `mcp` 下，非 per-server）

| JSON 字段 | 类型 | 缺省 | 说明 |
|---|---|---|---|
| `roots` | list | `cwd`（当前工作目录） | client 声明给 server 的可访问根目录；支持 `{"uri": "file://...", "name": "..."}` 对象或 `"file://..."` 字符串简写 |

### 5.11 跨会话复用 mgr（对比图）

```
【原架构：mgr 跟 agent 生命周期】
切会话
  ├─► agent.close() 顺带 mgr.dispose()
  ├─► 所有 MCP 连接断
  └─► 新会话 connect_all 全重连（健康 server 也重连）❌ 浪费

【新架构：mgr 跨会话复用】
切会话
  ├─► agent.close() 只关 agent 自己资源
  ├─► mgr 在 st.session_state 中保持
  ├─► 新会话从 mgr.registered_tools() 重 register 到新 registry
  └─► 健康连接保持，调度器持续维护 ✅
```

**关键改动**：
- `agent.close()` 去掉 `mgr.dispose()`（mgr 不再跟 agent 生命周期）
- mgr 存 `st.session_state.mcp_manager` 跨会话保持
- agent 重建时复用 mgr + 从 `registered_tools()` 重 register
- mgr 真销毁时机：进程退出（atexit）+ mcp config 变化（暂未做热更新）

---

## 6. MCP Server 开发流程（FastMCP 实战）

### 6.1 为什么用 FastMCP

官方 Python SDK 提供两层 API：
- **底层**：`mcp.server.Server` + 手写 JSON-RPC handler（繁琐）
- **高层 FastMCP**：装饰器风格，自动处理序列化 / transport / capability 声明

### 6.2 三大装饰器

```python
from mcp.server.fastmcp import FastMCP, Context

mcp = FastMCP("hello-mcp")

# ── Tool：可被 LLM 调用 ──
@mcp.tool()
async def add(a: int, b: int) -> str:
    """两数相加，返回 JSON 字符串。"""
    return json.dumps({"sum": a + b})

# ── Resource：可被 client 主动读取 ──
@mcp.resource("config://{key}")
def get_config(key: str) -> str:
    """读某项配置。"""
    return CONFIG_MAP.get(key, "unknown")

# ── Prompt：模板，渲染成 messages ──
@mcp.prompt()
def review_code(code: str, language: str = "python") -> str:
    """构造 code review 请求。"""
    return f"Please review this {language} code:\n\n```\n{code}\n```"
```

**关键点**：
- `description` / `name` 默认从 docstring + 函数名推断；显式更稳
- `Context` 参数（可选）：拿 session 句柄，可调 `ctx.session.list_roots()` / `ctx.report_progress()` 等
- **type hints 必须**（FastMCP 据此生成 JSON Schema）

### 6.3 transport 选择

```python
if __name__ == "__main__":
    import sys
    if "--transport=http" in sys.argv:
        port = int(sys.argv[sys.argv.index("--port") + 1]) if "--port" in sys.argv else 8765
        mcp.run(transport="streamable-http", host="127.0.0.1", port=port)
    else:
        mcp.run(transport="stdio")   # 默认
```

**CLI flag 驱动** vs **环境变量驱动**：CLI 灵活（同一文件可 stdio / http 切换），env 适合生产部署。

### 6.4 错误处理

**isError 标记**：tool 返回错误时，client 应知道结果是错误而非正常输出。

```python
@mcp.tool()
async def read_file(path: str) -> str:
    """读文件，失败抛 FileNotFoundError → isError=True。"""
    if not os.path.exists(path):
        # FastMCP 会把抛出的异常转成 isError=True 的 CallToolResult
        raise FileNotFoundError(f"not found: {path}")
    return open(path).read()
```

**client 侧处理**：`_normalize_call_result` 检查 `result.isError`，True 则在 ToolRegistry 标 error（不抛）。

**structured content**：tool 可返回结构化数据，client 解析更稳：

```python
@mcp.tool()
async def add(a: int, b: int) -> dict:
    """返回结构化数据，FastMCP 转成 structuredContent 字段。"""
    return {"sum": a + b}   # → result.structuredContent = {"sum": 3}
```

### 6.5 list_changed 通知（动态增删）

**FastMCP 默认不动态改**：FastMCP 设计上是"启动时确定工具集"，运行时通过装饰器增删工具**不会自动 emit** `notifications/tools/list_changed`——这是 FastMCP 自身的实现限制（不是协议限制）。如果需要运行时改工具集 + 推通知到 client，必须走更底层的 SDK API。

**⚠️ 不要直接操作 `_tool_manager`**：`mcp._tool_manager._tools` 是 FastMCP 私有 API，版本升级极易 break；即使能增删，也不保证触发 list_changed 通知。**生产代码不要这样写**。

```python
# ❌ 不可靠：操作私有 API + 不可控的通知时机
@mcp.tool()
async def manage_dynamic_tool(action: str, name: str) -> str:
    if action == "remove":
        mcp._tool_manager._tools.pop(name, None)   # 私有 API + 不一定 emit
        return f"removed: {name}"

# ✅ 推荐：重启 server 让 client 重连时重新 list_tools
# 或：用底层 SDK（mcp.server.Server）自己实现 _tool_manager + 自己 send_notification
```

**调试验证**：动态改工具集后，用 [MCP Inspector](#83-调试工具mcp-inspector) 连 server 看是否真发了 `notifications/tools/list_changed`——别只靠文档承诺。

### 6.6 部署形态

| 形态 | 适用 | 命令 |
|---|---|---|
| **Subprocess** | 本地工具 / CLI 一次性 | `npx -y @modelcontextprotocol/server-fs /tmp` |
| **HTTP server** | 远程共享 / 多 agent | `python server.py --transport=http --port=8765` |
| **Multi-tenant** | 生产平台 | 多个 HTTP 端 + 路由层 |

### 6.7 server 端调试实战技巧

开发 server 时最容易栽在"看不见的错误"上——下面是三个最常见坑。

#### ① `@mcp.tool()` 必须写 type hints

FastMCP 用函数签名（参数 + 返回值 type hints）**生成 JSON Schema 喂给 LLM**。**没写 type hints → schema 是空 `{}` → LLM 看不到任何参数信息**，调用必失败。

```python
# ❌ 错：没 type hints → schema 为空
@mcp.tool()
async def search_files(query, path):  # ← LLM 看不到 query/path 是什么类型
    return find(query, path)

# ✅ 对：显式 type hints → schema 完整
@mcp.tool()
async def search_files(query: str, path: str) -> list[dict]:
    """按文件名搜索。"""
    return find(query, path)
```

**调试信号**：用 [MCP Inspector](#83-调试工具mcp-inspector) 连 server，在 Tools 页看每个 tool 的 `inputSchema`。如果是空 `{}`，就是缺 type hints。

#### ② stdio 默认吞 stderr，server 报错看不见

stdio transport 把 stderr **默认丢弃**（只把 stdin/stdout 留给 JSON-RPC 帧）。server 内部崩了、Python traceback 全在 stderr —— client 那边完全看不到，只能看到 session 异常。

```bash
# 调试时手动把 stderr 引到日志文件
python server.py 2> /tmp/mcp-server.err
# 或：开发期用 Inspector 启动（它会捕获 stderr 在 UI）
npx @modelcontextprotocol/inspector python server.py
```

#### ③ HTTP transport 跨域 / 鉴权调试

HTTP server 启动后 client 连不上，先排查三件：

```bash
# 1. 端口监听了吗？
lsof -iTCP:8765 -sTCP:LISTEN

# 2. 手动 curl 看 endpoint（streamable-http 走 POST + 必带 Accept: text/event-stream）
curl -i -X POST http://127.0.0.1:8765/mcp \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'

# 3. macOS 用户确认系统代理是否劫持（见 §7.3）
scutil --proxy | grep HTTPProxy
```

---

## 7. 工程踩坑


### 7.1 被动断连检测失效（SDK GET SSE 静默 return）

**现象**：
- server kill 后，client 不感知，session 残留
- **`registered_tools()` 仍返回该 server 的工具 → ToolRegistry 留着"幽灵工具"**
- LLM 看到的 tool 列表里还有这些工具 → LLM 可能调它 → 傻等超时
- call_tool → 返回 error / 超时（用户体验差，且报错信息混淆："工具不可用" vs "server 不可用"）

**根因**（两层叠加）：

**第一层 — SDK 层（被动检测失效）**：MCP Python SDK v1.28.1 的 `streamable_http_client`（新 API，旧名 `streamablehttp_client` 已 deprecated）内部 GET SSE 流断开时，**重连 2 次后静默 `return`**（不抛异常），导致外层 `async with connect_server(...)` 不退出。

```python
# .venv/.../mcp/client/streamable_http.py 内部（伪代码，对照 line 292-294）
MAX_RECONNECTION_ATTEMPTS = 2
while attempt < MAX_RECONNECTION_ATTEMPTS:
    try:
        ...                # GET SSE
    except Exception as exc:
        attempt += 1

if attempt >= MAX_RECONNECTION_ATTEMPTS:
    return   # 静默退出，不抛 — 这是个 SDK bug
```

**第二层 — Agent 层（ToolRegistry 不自动清理）**：`_servers[name]` 还持有 session 对象 → `registered_tools()` 仍返回该 server 的 tool_defs。ToolRegistry 是**被动数据源**——只读 manager 暴露的工具列表，一旦连接时 tools 已注册，**没有反向信号**告诉 ToolRegistry "请移除"。

```
ToolRegistry（断连后状态）
├── Read                (builtin)
├── Bash                (builtin)
├── mcp__fs__read_file  ← 幽灵工具（server 已死，但仍在这里）❌
├── mcp__fs__write_file ← 幽灵工具 ❌
└── mcp__remote__query  ← 幽灵工具 ❌
```

**为什么两层会叠加成"幽灵工具"**：
- 旧架构（Phase 4 前）只有被动检测 → SDK 静默 return + ToolRegistry 不动 = **幽灵工具永远残留**，直到 streamlit 重启
- Phase 4 加 retry loop，但被动异常不触发 → 仍然残留
- 必须**主动**检测 + **主动**清理，二者缺一不可

**解法**（两个动作配合）：

**1. 主动 health-check 调度器**：
- 每 30s 周期 probe（`list_tools` with timeout）
- 失败 → teardown_server（pop + 取消长连接 task）
- disconnected → 异步 spawn 重连（不阻塞）

**2. unregister_by_prefix**：
- probe 失败时调 `on_tools_changed` 回调
- 回调里 `registry.unregister_by_prefix(server_prefix(name))` —— 注销该 server 的所有 tools
- `run_state.tool_schemas = None` —— 让下次 LLM 请求时 ToolsSchemaPrepareHandler 重填

**完整链路图**：

```
断连瞬间
   ↓
【窗口期 ≤ 30s】LLM 仍可能调幽灵工具
   ↓ ToolRegistry.execute → handler 闭包 → manager.call_tool
   ↓ session.call_tool 傻等 → 超时 → 返回 error（不挂 agent）
   ↓
【health-check scheduler 30s 一轮】
   ↓ probe: list_tools with timeout
   ↓ 超时
   ↓ teardown_server + unregister_by_prefix("mcp__fs__")
   ↓ run_state.tool_schemas = None
   ↓
【下次 LLM 请求】
   ↓ ToolsSchemaPrepareHandler 重填 schemas
   ↓ mcp__fs__* 不再出现 ✅
```

**教训**：
- MCP SDK 的 streamable_http transport 在断连时**不保证通知**
- ToolRegistry **不能假设"已注册的工具可用"**——必须主动维护一致性
- 生产 client 必须**主动检测 + 主动清理**，二者缺一不可
- 窗口期内 LLM 仍可能调幽灵工具——这是已知 trade-off，靠 ToolRegistry.execute 的异常隔离兜底

### 7.2 跨线程 callback（Streamlit 主线程 + mcp loop 线程）

**现象**：list_changed 通知 / 健康状态变更 → manager 调 `on_tools_changed` 回调 → 在 mcp loop 线程内执行 → `st.session_state.get(...)` 返 None（ScriptRunContext 跨线程失效）。

**根因**：Streamlit 的 `ScriptRunContext` 是 thread-local，run 脚本只在主线程有 context。其他线程访问 `st.session_state` 是 no-op（拿不到当前 rerun 的 session）。

**解法**：manager 不感知 streamlit；回调里**动态查 `st.session_state.get("agent")`**（不闭包捕获）：

```python
def _on_tools_changed(server_name):
    # 不捕获 agent/registry，每次动态查
    agent = st.session_state.get("agent")
    if agent is None:
        return
    registry = agent.tools
    # ... unregister + register + 失效 tool_schemas
```

**教训**：跨线程回调必须**无闭包外部依赖**——manager 只调 callback，callback 自己解决状态查找。

### 7.3 系统代理劫持（macOS scutil HTTPProxy → httpx trust_env）

**现象**：本地启动 HTTP MCP server（`http://127.0.0.1:8765`），client POST → **502 Bad Gateway**。

**根因**：macOS 系统代理（`scutil --proxy` 显示 `127.0.0.1:7890`）把 localhost 请求劫持到本地代理。`httpx` 默认 `trust_env=True`，读 `HTTP_PROXY` 环境变量 → localhost 被劫持。

**解法**：构造 httpx client 时显式 `trust_env=False`：

```python
self._http_client = httpx.AsyncClient(
    base_url=url,
    timeout=httpx.Timeout(timeout, read=sse_read_timeout),
    trust_env=False,   # 关键：不读系统代理
)
```
**教训**：跨平台 HTTP client 行为不一致（macOS 强系统代理），本地连接强制不走系统代理。
---

### 7.4 message_handler 自死锁陷阱

**现象**：handler 里 `await session.list_tools()` 后**永远卡住**——list_changed 通知收不到，连接看似活着但实际无响应。

**根因**：`message_handler` 跑在 `ClientSession._receive_loop`（**同一个 asyncio task**）。如果 handler 内部 `await` 一个需要 send_request 的 RPC（如 `list_tools`），response 必须经 `_receive_loop` 自己处理才能回来——但 `_receive_loop` 正卡在 `await handler(message)` 上，**自死锁**：response 永远进不来，handler 永远不返回。

```
ClientSession 内部 task 状态（死锁时）：

  _receive_loop (single task)
    │
    ├─ 收到 ServerNotification (ToolListChangedNotification)
    │   ↓
    ├─ 调用 message_handler(message)        ← await 这儿
    │   ↓                                     ↓
    │   handler 内 await session.list_tools() ← 需要 send_request
    │   ↓                                     ↓
    │   send_request 发出去 + 等 response      ↓
    │   ↓                                     ↓
    │   response 必须经 _receive_loop 自己读   ↓
    │   ↓                                     ↓
    │   ← 这里永远卡住 ────────────────  _receive_loop 还在等 handler 返回
    │
    └─ 整个 session 僵死
```

**解法**：handler 立即返回 + `asyncio.create_task` 把处理逻辑抛到独立 task。独立 task 跑 `await list_tools` 时，response 可被 `_receive_loop` 正常接收（因为它没被 handler 占着）。

```python
# ❌ 自死锁：handler 内 await 任何 send_request 类 RPC
async def _handler_bad(message):
    if isinstance(message, ToolListChangedNotification):
        result = await session.list_tools()   # 死锁
        # 永远等不到 response

# ✅ 正确：handler 立即返回，处理逻辑抛到独立 task
async def _handler_good(message):
    if isinstance(message, types.ServerNotification):
        asyncio.create_task(self._handle_notification(name, message.root))
    # 立刻返回，_receive_loop 继续收下一条
```

**项目实际实现**：`_make_message_handler` 严格按这个范式写——handler 内只判类型 + `create_task`，**绝不 await**。`_handle_notification` 在独立 task 内才 `await list_tools` / `list_resources` / `list_prompts`。

**教训**：
- 任何 `message_handler` 实现的"看起来无害"的 `await` 都可能是死锁源
- 项目里能踩到这个的入口只有一处：自己扩展 `message_handler`。**抄现有 _make_message_handler 的 create_task 范式**是最安全的做法
- 调试信号：handler 内 await 后连接不再响应任何 RPC，且 `list_changed` 通知丢失——基本就是这个死锁

---
## 8. MCP 开源社区与生态


### 8.1 三个核心资源库

#### ① Awesome MCP Servers

- **链接**：https://github.com/punkpeye/awesome-mcp-servers
- **定位**：社区维护的 MCP 服务器精选列表
- **特点**：包含各种第三方服务器，按功能分类，易于查找

#### ② MCP Servers Website

- **链接**：https://mcpservers.org/
- **定位**：官方 MCP 服务器目录网站
- **特点**：提供搜索和筛选功能，包含使用说明和示例

#### ③ Official MCP Servers

- **链接**：https://github.com/modelcontextprotocol/servers
- **定位**：Anthropic 官方维护的服务器
- **特点**：质量最高、文档最完善，包含常用服务的实现


### 8.2 官方 server 覆盖场景（基于 ③）

`@modelcontextprotocol/server-*` 系列（npm 包，Anthropic 官方维护）：

| 包 | 能力 |
|---|---|
| `server-filesystem` | 文件读写（带 paths 白名单） |
| `server-git` | git 操作（commit / log / diff） |
| `server-github` | GitHub API（issue / PR / workflow） |
| `server-gitlab` | GitLab API |
| `server-postgres` | Postgres 查询（schema 感知） |
| `server-sqlite` | SQLite 查询 |
| `server-slack` | Slack 频道 / 消息 |
| `server-google-drive` | Google Drive 文件 |
| `server-puppeteer` | 浏览器自动化 |
| `server-fetch` | HTTP fetch（带 HTML→markdown 转换） |
| `server-everything` | 测试 / demo（暴露所有原语 + list_changed） |

**特点**：stdio 为主，统一 `command + args + env` 配置即用。**先查官方 list，没合适的再自己写 server**。

### 8.3 调试工具：MCP Inspector

官方交互式调试 GUI，启动一个 server 可手动测 tools / resources / prompts。

- **npm**：`@modelcontextprotocol/inspector`
- **用法**：`npx @modelcontextprotocol/inspector python hello_mcp_server.py`
- **价值**：开发 server 时必装；不用写测试就能手动验每个原语