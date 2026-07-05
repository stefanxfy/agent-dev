# OpenClaw ReAct 循环与横切关注点解读

> openclaw 源码在本机的路径：/Users/fanyunxu/Desktop/myproject/ailearning/openclaw-2026.5.7/
>本文档基于源码逐行阅读写成。所有引用均使用 repo-root 相对路径（形如 `src/agents/pi-embedded-runner/run.ts:326`）。
> 涉及的核心/扩展边界，按 [AGENTS.md](../AGENTS.md) 与 [src/plugins/CLAUDE.md](../src/plugins/CLAUDE.md) 的规则区分：**核心内部**（私有实现细节，仅供阅读理解）vs **SDK 公共接缝**（其他插件可依赖）。

---

## TL;DR

OpenClaw 把 "嵌入式 ReAct 循环" 实现为 **`runEmbeddedPiAgent` 单一驱动函数 + `runEmbeddedAttemptWithBackend` 单次 attempt 后端**。一次 attempt ≈ 一次 Thought→Action→Observation 迭代；run.ts 的 `while(true)` 是上层重试/降级循环。

横切关注点（tool policy、before/after tool call、compaction、loop detection、context guard、prompt cache、payload redaction 等）以 **4 类形式** 注入：

| 注入形式 | 典型例子 |
|---|---|
| **工具拦截器**（前置） | [src/agents/pi-tools.before-tool-call.ts](src/agents/pi-tools.before-tool-call.ts) |
| **事件订阅**（后置） | [src/agents/pi-embedded-subscribe.handlers.tools.ts](src/agents/pi-embedded-subscribe.handlers.tools.ts) |
| **生命周期 hook** | [src/agents/pi-hooks/](src/agents/pi-hooks/) + `run.ts` 内 `runOwnsCompactionBeforeHook/AfterHook` |
| **策略/状态层** | [src/agents/tool-policy.ts](src/agents/tool-policy.ts)、[src/agents/tool-loop-detection.ts](src/agents/tool-loop-detection.ts)、[src/agents/context-window-guard.ts](src/agents/context-window-guard.ts) |

状态通过 **两层显式容器** 维持：run 闭包变量（attempt 间） + `EmbeddedPiSubscribeState`（attempt 内）+ `SessionManager`（持久化）。

---

## 一、整体架构

```
runEmbeddedPiAgent (run.ts:326)                     ← 上层 while(true)
  │
  ├─ 模型解析 (model.ts:300+)
  ├─ 状态准备 / 闭包变量
  │
  └─ while (runLoopIterations < MAX_RUN_LOOP_ITERATIONS)
       │
       ├─ prompt 准备 + runtime plan 构造
       ├─ attemptAbortController 中继
       │
       ├─► runEmbeddedAttemptWithBackend          ← 单次 ReAct 迭代
       │    │
       │    ├─ subscribeEmbeddedPiSession
       │    │   └─ createEmbeddedPiSessionEventHandler
       │    │       ├─ handleMessageStart/Update/End
       │    │       ├─ handleToolExecutionStart/Update/End
       │    │       ├─ handleCompactionStart/End
       │    │       └─ handleAgentStart/End
       │    │
       │    ├─ 模型流式响应
       │    │   │
       │    │   ├─ message_start/update/end ─► handleMessage*
       │    │   │
       │    │   └─ tool_call ─► handleToolExecutionStart
       │    │       │
       │    │       ├─► runBeforeToolCallHook     ◄── 工具拦截
       │    │       │   ├─ loop detection
       │    │       │   ├─ trusted policy
       │    │       │   ├─ plugin hook runner
       │    │       │   └─ approval gate (requestPluginToolApproval)
       │    │       │
       │    │       ├─► tool.execute
       │    │       │   └─ exec/sandbox/message 工具的内部审批
       │    │       │
       │    │       └─► handleToolExecutionEnd
       │    │           ├─► after_tool_call hook
       │    │           └─► emitToolResultOutput
       │    │
       │    └─ 返回 attempt 结果
       │
       ├─ attempt 结果解释
       │   ├─ post-compaction 循环保护
       │   ├─ idle-timeout 熔断
       │   ├─ timeout 触发 compaction
       │   ├─ context overflow 恢复
       │   ├─ assistant 端 failover
       │   ├─ payload 构造
       │   ├─ 各种 retry (planning / reasoning / empty / silent / compaction)
       │   └─ 终态返回 EmbeddedPiRunResult
       │
       └─ continue / break
```

---

## 二、主 ReAct 循环：`runEmbeddedPiAgent`

### 2.1 入口与基本结构

- 文件：[src/agents/pi-embedded-runner/run.ts](src/agents/pi-embedded-runner/run.ts)
- 入口函数：`runEmbeddedPiAgent(params: RunEmbeddedPiAgentParams): Promise<EmbeddedPiRunResult>`（[run.ts:326](src/agents/pi-embedded-runner/run.ts#L326)）
- 双名导出：[src/agents/pi-embedded-runner.ts:8-11](src/agents/pi-embedded-runner.ts#L8-L11)

### 2.2 attempt 循环的完整执行顺序

观察 [run.ts:1017-2860](src/agents/pi-embedded-runner/run.ts#L1017-L2860) 的 `while(true)`：

#### Step 1. 重试上限检查

[run.ts:1018-1051](src/agents/pi-embedded-runner/run.ts#L1018-L1051)：

```
runLoopIterations >= MAX_RUN_LOOP_ITERATIONS
  → resolveRunFailoverDecision({ stage: "retry_limit" })
  → handleRetryLimitExhaustion
  → 返回
```

#### Step 2. prompt 准备

[run.ts:1058-1074](src/agents/pi-embedded-runner/run.ts#L1058-L1074)：

合并 `nextAttemptPromptOverride`（来自 compaction 续接等场景）、`scrubAnthropicRefusalMagic`、`ackExecutionFastPathInstruction`、`planningOnlyRetryInstruction` 等追加指令。注入到 `runtimePlan = buildAgentRuntimePlan({...})`。

#### Step 3. abort 中继

[run.ts:1105-1115](src/agents/pi-embedded-runner/run.ts#L1105-L1115)：

新建 `attemptAbortController`，把 `params.abortSignal` 透传并允许其 abort 当前 attempt。

#### Step 4. 执行 attempt

[run.ts:1116-1233](src/agents/pi-embedded-runner/run.ts#L1116-L1233)：`runEmbeddedAttemptWithBackend({...})` 内含完整 prompt → 流式响应 → 工具执行 → 事件订阅流程。

`onToolOutcome` 回调直接接到 `observePostCompactionToolOutcome`（[run.ts:1184](src/agents/pi-embedded-runner/run.ts#L1184)），实现 post-compaction 死循环检测。

#### Step 5. post-attempt 解释与重试决策

[run.ts:1248-2820](src/agents/pi-embedded-runner/run.ts#L1248-L2820) 按以下顺序分类处理 attempt 结果：

| 阶段 | 行号 | 处理函数 |
|---|---|---|
| post-compaction 循环保护 abort | [1235-1245](src/agents/pi-embedded-runner/run.ts#L1235-L1245) | `postCompactionGuard.observe` + abort |
| idle-timeout 熔断 | [1290-1334](src/agents/pi-embedded-runner/run.ts#L1290-L1334) | `stepIdleTimeoutBreaker` |
| **timeout 触发 compaction** | [1417-1524](src/agents/pi-embedded-runner/run.ts#L1417-L1524) | `tokenUsedRatio > 0.65 && !timedOutDuringCompaction && !timedOutDuringToolExecution` → `contextEngine.compact({ trigger: "timeout_recovery" })` |
| **context overflow 恢复** | [1526-1812](src/agents/pi-embedded-runner/run.ts#L1526-L1812) | `contextEngine.compact({ trigger: "overflow" })` × 3 → `truncateOversizedToolResultsInSession` |
| prompt 错误处理 | [1814-2035](src/agents/pi-embedded-runner/run.ts#L1814-L2035) | `coerceToFailoverError` + auth profile rotation + fallback model |
| assistant 端 failover | [2037-2202](src/agents/pi-embedded-runner/run.ts#L2037-L2202) | `handleAssistantFailover` (rate limit / auth / billing / idle) |
| payload 构造 | [2230-2249](src/agents/pi-embedded-runner/run.ts#L2230-L2249) | `buildEmbeddedRunPayloads` + `mergeAttemptToolMediaPayloads` |
| 各种 retry | [2255-2646](src/agents/pi-embedded-runner/run.ts#L2255-L2646) | `planningOnlyRetry` / `reasoningOnlyRetry` / `emptyResponseRetry` / `compactionContinuationRetry` / `silentErrorRetry`（ollama/glm-5.1 stopReason=error） |
| 终态返回 | [2707-2820](src/agents/pi-embedded-runner/run.ts#L2707-L2820) | `EmbeddedPiRunResult` + `markAuthProfileGood`/`Used` + `attempt.setTerminalLifecycleMeta?.()` |

#### Step 6. 清理

[run.ts:2822-2860](src/agents/pi-embedded-runner/run.ts#L2822-L2860)：

- `forgetPromptBuildDrainCacheForRun`
- `stopRuntimeAuthRefreshTimer`
- `runAgentCleanupStep` 安全 dispose `contextEngine`
- 可选 `retireSessionMcpRuntime`

### 2.3 关键横切变量（"loop state"）

attempt 间持久化状态保存在 `run.ts` 的闭包变量中：

```ts
// 模型 / provider
provider, modelId, runtimeModel, effectiveModel, thinkLevel
// run.ts:430-431, 527-536, 675-676, 730-731

// auth
profileIndex, lastProfileId, runtimeAuthState, apiKeyInfo
// run.ts:672-680, 711-725

// session
activeSessionId, activeSessionFile
// run.ts:836-837 — 每次 compaction 后通过 adoptCompactionTranscript 切换 (line 938-949)

// compaction
autoCompactionCount, lastCompactionTokensAfter
// run.ts:775-776

// replay
accumulatedReplayState = createEmbeddedRunReplayState()
// run.ts:1014

// 重试计数器
planningOnlyRetryAttempts
reasoningOnlyRetryAttempts
emptyResponseRetryAttempts
compactionContinuationRetryAttempts
sameModelIdleTimeoutRetries
emptyErrorRetries
overflowCompactionAttempts
timeoutCompactionAttempts
overloadProfileRotations
rateLimitProfileRotations
// run.ts:768-833

// post-compaction 保护
postCompactionGuard       // 死循环检测
postCompactionAbortController
// run.ts:798-812, 1106, 1239-1241

// compaction 续接
nextAttemptPromptOverride
suppressNextUserMessagePersistence
// run.ts:850-853, 1062-1063, 1716-1717
```

### 2.4 attempt 后端

`runEmbeddedAttemptWithBackend`（[src/agents/pi-embedded-runner/run/backend.ts](src/agents/pi-embedded-runner/run/backend.ts)，由 [run.ts:111](src/agents/pi-embedded-runner/run.ts#L111) 导入）封装真正的一次 prompt+stream+tool 调用。

---

## 三、事件订阅层

### 3.1 模块组成

| 模块 | 文件 |
|---|---|
| 入口函数 `subscribeEmbeddedPiSession` | [src/agents/pi-embedded-subscribe.ts:117](src/agents/pi-embedded-subscribe.ts#L117) |
| 入参类型 `SubscribeEmbeddedPiSessionParams` | [src/agents/pi-embedded-subscribe.types.ts:20-65](src/agents/pi-embedded-subscribe.types.ts#L20-L65) |
| 事件处理工厂 `createEmbeddedPiSessionEventHandler` | [src/agents/pi-embedded-subscribe.handlers.ts:23-135](src/agents/pi-embedded-subscribe.handlers.ts#L23-L135) |
| 状态类型 `EmbeddedPiSubscribeState` | [src/agents/pi-embedded-subscribe.handlers.types.ts:30-109](src/agents/pi-embedded-subscribe.handlers.types.ts#L30-L109) |
| 事件合约 `EmbeddedPiSubscribeEvent` | [src/agents/pi-embedded-subscribe.handlers.types.ts:232-235](src/agents/pi-embedded-subscribe.handlers.types.ts#L232-L235) |

### 3.2 事件类型与处理函数

[handlers.ts:76-132](src/agents/pi-embedded-subscribe.handlers.ts#L76-L132) 的 switch：

| 事件 | 处理函数 | 文件 |
|---|---|---|
| `message_start` | `handleMessageStart` | [handlers.messages.ts](src/agents/pi-embedded-subscribe.handlers.messages.ts) |
| `message_update` | `handleMessageUpdate` | 同上 |
| `message_end` | `handleMessageEnd` | 同上 |
| `tool_execution_start` | `handleToolExecutionStart` | [handlers.tools.ts:604+](src/agents/pi-embedded-subscribe.handlers.tools.ts#L604) |
| `tool_execution_update` | `handleToolExecutionUpdate` | 同上 |
| `tool_execution_end` | `handleToolExecutionEnd`（detach） | [handlers.tools.ts:1186-1227](src/agents/pi-embedded-subscribe.handlers.tools.ts#L1186-L1227) |
| `agent_start` | `handleAgentStart` | [handlers.lifecycle.ts:24-38](src/agents/pi-embedded-subscribe.handlers.lifecycle.ts#L24-L38) |
| `compaction_start` | `handleCompactionStart` | [handlers.compaction.ts:7-40](src/agents/pi-embedded-subscribe.handlers.compaction.ts#L7-L40) |
| `compaction_end` | `handleCompactionEnd` | [handlers.compaction.ts:42-110](src/agents/pi-embedded-subscribe.handlers.compaction.ts#L42-L110) |
| `agent_end` | `handleAgentEnd` | [handlers.lifecycle.ts:40+](src/agents/pi-embedded-subscribe.handlers.lifecycle.ts#L40) |

### 3.3 关键事件载荷

#### `compaction_start`

[handlers.compaction.ts:7-40](src/agents/pi-embedded-subscribe.handlers.compaction.ts#L7-L40)：

```
compaction_start
  → state.compactionInFlight = true
  → state.livenessState = "paused"
  → ensureCompactionPromise
  → emit before_compaction 插件 hook
```

#### `compaction_end`

[handlers.compaction.ts:42-110](src/agents/pi-embedded-subscribe.handlers.compaction.ts#L42-L110)：

```
compaction_end
  → state.compactionInFlight = false
  → incrementCompactionCount / noteCompactionTokensAfter
  → willRetry=false 时发 after_compaction 插件 hook
```

#### `tool_execution_end`

[handlers.tools.ts:1186-1227](src/agents/pi-embedded-subscribe.handlers.tools.ts#L1186-L1227)：

```
tool_execution_end
  → emitToolResultOutput
  → if (hookRunnerAfter?.hasHooks("after_tool_call"))
       构造 PluginHookAfterToolCallEvent
       → runAfterToolCall (fire-and-forget)
```

#### `agent_end`

[handlers.lifecycle.ts:40-67](src/agents/pi-embedded-subscribe.handlers.lifecycle.ts#L40-L67)：

```
agent_end
  → 计算 replayInvalid / livenessState ("blocked"/"abandoned"/"paused"/"working")
  → 通过 ctx.params.onAgentEvent 抛出
```

### 3.4 事件链序列化

`scheduleEvent`（[handlers.ts:26-73](src/agents/pi-embedded-subscribe.handlers.ts#L26-L73)）把事件串成 `pendingEventChain`，保证 handler 顺序执行；`tool_execution_end` 标记 `detach: true` 不阻塞下一事件。

---

## 四、钩子系统

### 4.1 全局钩子触发点

| 钩子 | 触发位置 |
|---|---|
| `before_agent_reply` | [run.ts:453-473](src/agents/pi-embedded-runner/run.ts#L453-L473)（cron 触发短路） |
| `before_compaction` | [handlers.compaction.ts:24-38](src/agents/pi-embedded-subscribe.handlers.compaction.ts#L24-L38) + [run.ts:971-986](src/agents/pi-embedded-runner/run.ts#L971-L986) `runOwnsCompactionBeforeHook` |
| `after_compaction` | [handlers.compaction.ts:93-108](src/agents/pi-embedded-subscribe.handlers.compaction.ts#L93-L108) + [run.ts:987-1012](src/agents/pi-embedded-runner/run.ts#L987-L1012) `runOwnsCompactionAfterHook` |
| `before_tool_call` | [pi-tools.before-tool-call.ts:418-618](src/agents/pi-tools.before-tool-call.ts#L418-L618) `runBeforeToolCallHook` |
| `after_tool_call` | [handlers.tools.ts:1196-1226](src/agents/pi-embedded-subscribe.handlers.tools.ts#L1196-L1226) |

hook 运行器来自 `getGlobalHookRunner()`（[src/plugins/hook-runner-global.ts](src/plugins/hook-runner-global.ts)）。

### 4.2 `before_tool_call` 的完整流程

[pi-tools.before-tool-call.ts:418-618](src/agents/pi-tools.before-tool-call.ts#L418-L618)：

入参：

```ts
{
  toolName: string;
  params: unknown;
  toolCallId: string;
  ctx: HookContext;
  signal: AbortSignal;
  approvalMode: "auto" | "approve-required";
}

type HookContext = {
  agentId, config, sessionKey, sessionId,
  runId, trace, loopDetection, onToolOutcome
}; // line 41-51
```

返回 `HookOutcome` 判别联合（line 56-63）：

```ts
type HookOutcome =
  | { blocked: true;  kind?: "veto" | "failure"; deniedReason?: "plugin-before-tool-call" | "plugin-approval" | "tool-loop"; reason: string; params?: unknown }
  | { blocked: false; params: unknown }
```

执行顺序（line 429-617）：

```
1. tool loop detection
   ├─ detectToolCallLoop
   └─ recordToolCall

2. trusted plugin policy
   ├─ runTrustedToolPolicies
   ├─ 返回 block → 直接拒绝
   └─ 返回 requireApproval → requestPluginToolApproval

3. global hook runner
   └─ hookRunner.runBeforeToolCall

4. 错误路径
   ├─ BeforeToolCallBlockedError 抛回
   └─ kind: "failure", deniedReason: "plugin-before-tool-call" 的 HookOutcome
```

包装器：`wrapToolWithBeforeToolCallHook(tool, ctx)`（[pi-tools.before-tool-call.ts:620-756](src/agents/pi-tools.before-tool-call.ts#L620-L756)）。

`toToolDefinitions` 适配器（[pi-tool-definition-adapter.ts:226-291](src/agents/pi-tool-definition-adapter.ts#L226-L291)）在 `execute` 内嵌入了相同流程的 fallback 路径（line 240-256）；客户端工具变体 `toClientToolDefinitions`（[pi-tool-definition-adapter.ts:327-389](src/agents/pi-tool-definition-adapter.ts#L327-L389)）调 `runBeforeToolCallHook` 并把 `outcome.params` 写回 client tool recorder。

### 4.3 `after_tool_call` 的细节

[handlers.tools.ts:1196-1226](src/agents/pi-embedded-subscribe.handlers.tools.ts#L1196-L1226)：

```ts
type PluginHookAfterToolCallEvent = {
  toolName: string;
  params: unknown;
  runId: string;
  toolCallId: string;
  result: unknown;
  error?: unknown;
  durationMs: number;
};
```

- 通过 `consumeAdjustedParamsForToolCall(toolCallId, runId)` 拿到 `before_tool_call` 调整后的参数。
- `durationMs` 来自 `startData.startTime`。
- `hookRunnerAfter.runAfterToolCall(hookEvent, ctx).catch(...)` — fire-and-forget。

### 4.4 `before_compaction` / `after_compaction`

- `compaction_start` 时发 `before_compaction`（[handlers.compaction.ts:24-38](src/agents/pi-embedded-subscribe.handlers.compaction.ts#L24-L38)）。
- `compaction_end` 在 `willRetry=false` 时发 `after_compaction`（line 93-108）。
- `run.ts` 中 `runOwnsCompactionBeforeHook`/`runOwnsCompactionAfterHook`（line 971-1012）用于"context engine 拥有 compaction"时同步触发钩子，保证 subscribers（memory extensions / usage trackers）仍被通知。

### 4.5 SDK 形式钩子

[src/agents/pi-hooks/](src/agents/pi-hooks/) 目录暴露 SDK 形式的 hook：

| 文件 | 用途 |
|---|---|
| [pi-hooks/compaction-safeguard.ts](src/agents/pi-hooks/compaction-safeguard.ts) | 基于 `ExtensionAPI` 的 SDK hook |
| [pi-hooks/compaction-safeguard-runtime.ts](src/agents/pi-hooks/compaction-safeguard-runtime.ts) | `getCompactionSafeguardRuntime()` lazy 加载 |
| [pi-hooks/compaction-safeguard-quality.ts](src/agents/pi-hooks/compaction-safeguard-quality.ts) | 结构化摘要质量 |
| [pi-hooks/compaction-instructions.ts](src/agents/pi-hooks/compaction-instructions.ts) | `resolveCompactionInstructions` — 三层 precedence: event → runtime → DEFAULT_COMPACTION_INSTRUCTIONS |
| [pi-hooks/context-pruning.ts](src/agents/pi-hooks/context-pruning.ts) | 上下文裁剪 |

统一接口（来自 [src/plugins/hooks.ts](src/plugins/hooks.ts)）：

```ts
interface HookRunner {
  hasHooks(name: string): boolean;
  runBeforeToolCall(event, ctx): Promise<HookOutcome>;
  runAfterToolCall(event, ctx): Promise<void>;
  runBeforeAgentReply(event, ctx): Promise<void>;
  runBeforeCompaction(event, ctx): Promise<void>;
  runAfterCompaction(event, ctx): Promise<void>;
}
```

---

## 五、工具授权 / 审批

### 5.1 exec 工具的两阶段协议

入参类型：[bash-tools.exec-approval-request.ts:9-28](src/agents/bash-tools.exec-approval-request.ts#L9-L28)：

```ts
type RequestExecApprovalDecisionParams = {
  id: string;
  command: string;
  commandArgv: string[];
  systemRunPlan: SystemRunPlan;
  env: NodeJS.ProcessEnv;
  cwd: string;
  host: "gateway" | "node";
  security: ExecSecurity;
  ask: ExecAsk;
  warningText: string;
  agentId: string;
  sessionKey: string;
  turnSource*: string;
};
```

注册决策：`ExecApprovalRegistration = { id, expiresAtMs, finalDecision? }`（line 85-89）。

决策枚举：`"allow-once" | "allow-always" | "deny" | "timeout" | "cancelled"`（[infra/exec-approvals.ts](src/infra/exec-approvals.ts)）。

#### 协议流程

```ts
// bash-tools.exec-approval-request.ts:91-110
registerExecApprovalRequest(params) {
  return callGatewayTool(
    "exec.approval.request",
    { timeoutMs: DEFAULT_APPROVAL_REQUEST_TIMEOUT_MS },
    params,
    { expectFinal: false }
  );
}

// line 112- waitForExecApprovalDecision
waitForExecApprovalDecision(id) {
  return callGatewayTool("exec.approval.waitDecision", ...);
}

// bash-tools.exec-approval-followup.ts 对应的 approveExecCommand 触发 follow-up
```

#### 调用位置

[bash-tools.exec.ts](src/agents/bash-tools.exec.ts) 中：
- 行 1059、1072、1160、1351-1359、1394-1400、1499、1533-1538 — 集中引用审批字段
- `approvalId` / `approvalRunningNoticeMs` / `approvalWarningText` / `approvalFollowup*`
- 加载 `loadExecApprovals()` 决定默认 `security`/`ask`

### 5.2 插件审批（两阶段协议）

[pi-tools.before-tool-call.ts:145-316](src/agents/pi-tools.before-tool-call.ts#L145-L316) 的 `requestPluginToolApproval`：

```ts
requestPluginToolApproval(ctx, hookCtx) {
  // 第一阶段
  callGatewayTool("plugin.approval.request", {
    pluginId, title, description, severity,
    toolName, toolCallId, agentId, sessionKey,
    timeoutMs, twoPhase: true
  });

  // 第二阶段
  callGatewayTool("plugin.approval.waitDecision", ...)
    .then(mapPluginApprovalDecision)  // PluginApprovalResolutions
    .catch(handleAbort);              // abort 监听器抢先返回 reason: "Approval cancelled (run aborted)"
}
```

`PluginApprovalResolutions = ALLOW_ONCE | ALLOW_ALWAYS | DENY | TIMEOUT | CANCELLED`（来自 [src/plugins/types.js](src/plugins/types.js)）。

### 5.3 owner-only 工具策略

[src/agents/tool-policy.ts:59-79](src/agents/tool-policy.ts#L59-L79)：

```ts
applyOwnerOnlyToolPolicy(tools, senderIsOwner, ownerOnlyToolAllowlist)
```

- 对 `ownerOnly` 工具做 sender 授权过滤。
- 非 owner 直接被 `wrapOwnerOnlyToolExecution` 替换为抛错（[tool-policy.ts:23-33](src/agents/tool-policy.ts#L23-L33)）。
- `OwnerOnlyToolApprovalClass = "control_plane" | "exec_capable" | "interactive"`（[tool-policy.ts:20](src/agents/tool-policy.ts#L20)）。

### 5.4 工具策略管线

| 文件 | 职责 |
|---|---|
| [src/agents/tool-policy.ts](src/agents/tool-policy.ts) | owner-only 过滤 |
| [src/agents/tool-policy-pipeline.ts](src/agents/tool-policy-pipeline.ts) | 策略管线（让上层按顺序叠加 allow/block/transform） |
| [src/agents/sandbox-tool-policy.ts](src/agents/sandbox-tool-policy.ts) | 沙箱内工具策略 |
| [src/agents/tool-policy-shared.ts](src/agents/tool-policy-shared.ts) | `expandToolGroups` / `normalizeToolName` / `resolveToolProfilePolicy` / `TOOL_GROUPS` |
| [src/agents/effective-tool-policy.ts](src/agents/effective-tool-policy.ts) | 综合 config + sandbox + ownerOnly + allowlist |

### 5.5 exec vs plugin 审批对比

| 维度 | exec 工具 | 插件工具 |
|---|---|---|
| 触发器 | exec 工具内部需要外部执行 | plugin hook 返回 `requireApproval` |
| 网关方法 | `exec.approval.request` / `exec.approval.waitDecision` | `plugin.approval.request` / `plugin.approval.waitDecision` |
| 调用点 | [bash-tools.exec.ts](src/agents/bash-tools.exec.ts) | [pi-tools.before-tool-call.ts:145-316](src/agents/pi-tools.before-tool-call.ts#L145-L316) |
| 决策映射 | `"allow-once" \| "allow-always" \| "deny" \| "timeout" \| "cancelled"` | `PluginApprovalResolutions.ALLOW_ONCE \| ALLOW_ALWAYS \| DENY \| TIMEOUT \| CANCELLED` |

两者都通过 `callGatewayTool` 网关 RPC；abort 监听器保证运行 abort 时能快速取消等待。

---

## 六、上下文压缩（Compaction）

### 6.1 抽象位置

| 模块 | 文件 |
|---|---|
| 顶层 | [src/agents/compaction.ts](src/agents/compaction.ts)（`BASE_CHUNK_RATIO=0.4`、`MIN_CHUNK_RATIO=0.15`、`SAFETY_MARGIN=1.2`） |
| Hook 集成 | [src/agents/pi-hooks/compaction-safeguard.ts](src/agents/pi-hooks/compaction-safeguard.ts) |
| Lazy 加载 | [src/agents/pi-hooks/compaction-safeguard-runtime.ts](src/agents/pi-hooks/compaction-safeguard-runtime.ts) |
| 运行时入口 | `compactEmbeddedPiSession`（[src/agents/pi-embedded-runner/compact.queued.ts](src/agents/pi-embedded-runner/compact.queued.ts)，从 [pi-embedded-runner.ts:3](src/agents/pi-embedded-runner.ts#L3) 导出） |
| 安全超时 | `compactWithSafetyTimeout`（[src/agents/pi-embedded-runner/compaction-safety-timeout.ts:26](src/agents/pi-embedded-runner/compaction-safety-timeout.ts#L26)），`EMBEDDED_COMPACTION_TIMEOUT_MS=900_000`（15 分钟） |
| Identifier preservation | `IDENTIFIER_PRESERVATION_INSTRUCTIONS`（[compaction.ts:39-41](src/agents/compaction.ts#L39-L41)），策略 `AgentCompactionIdentifierPolicy = "strict" \| "off" \| "custom"` |
| 摘要 fallback | `DEFAULT_SUMMARY_FALLBACK = "No prior history."`（[compaction.ts:23](src/agents/compaction.ts#L23)）；`summarizeInStages`、`computeAdaptiveChunkRatio` |
| 摘要指令 | `resolveCompactionInstructions`（[pi-hooks/compaction-instructions.ts:48-57](src/agents/pi-hooks/compaction-instructions.ts#L48-L57)）— 三层 precedence: event → runtime → DEFAULT_COMPACTION_INSTRUCTIONS |
| 引擎入口 | `contextEngine = await resolveContextEngine(...)`（[run.ts:928-932](src/agents/pi-embedded-runner/run.ts#L928-L932)），`ownsCompaction` 决定是否由引擎自身处理 |
| Post-compaction 副作用 | `runPostCompactionSideEffects`（[src/agents/pi-embedded-runner/compaction-hooks.ts](src/agents/pi-embedded-runner/compaction-hooks.ts)）— 在 [run.ts:1506-1512](src/agents/pi-embedded-runner/run.ts#L1506-L1512) 调用 |

### 6.2 attempt 内的两个触发点

#### timeout 触发

[run.ts:1417-1524](src/agents/pi-embedded-runner/run.ts#L1417-L1524)：

```
tokenUsedRatio > 0.65
&& !timedOutDuringCompaction
&& !timedOutDuringToolExecution
  → contextEngine.compact({ trigger: "timeout_recovery" })
  → 最多 MAX_TIMEOUT_COMPACTION_ATTEMPTS=2 次
```

#### context overflow 触发

[run.ts:1526-1812](src/agents/pi-embedded-runner/run.ts#L1526-L1812)：

```
先尝试 MAX_OVERFLOW_COMPACTION_ATTEMPTS=3 次 explicit contextEngine.compact({ trigger: "overflow" })
再尝试 truncateOversizedToolResultsInSession
```

每次 compaction 成功后 `postCompactionGuard.armPostCompaction()`（[run.ts:1516](src/agents/pi-embedded-runner/run.ts#L1516)、[1706](src/agents/pi-embedded-runner/run.ts#L1706)、[2482](src/agents/pi-embedded-runner/run.ts#L2482)），并在 attempt 之间通过 `continue` 重试同一 prompt 或切换为 `MID_TURN_PRECHECK_CONTINUATION_PROMPT`（line 178-179）。

### 6.3 状态保存

```ts
// run.ts:836-841
activeSessionId / activeSessionFile
  → adoptCompactionTranscript (line 938-949) 后更新
  → 对应 SessionManager 的真实文件路径

// run.ts:775-776, 1335-1342, 1499-1504, 1672-1678
autoCompactionCount / lastCompactionTokensAfter
  → 在 attempt 之间的闭包中保留

// pi-embedded-subscribe.handlers.compaction.ts:54-69
incrementCompactionCount
  → 持久化到 session store (reconcileSessionStoreCompactionCountAfterSuccess)
```

---

## 七、工具结果净化

### 7.1 工具结果守卫（持久化层）

| 文件 | 职责 |
|---|---|
| [src/agents/session-tool-result-guard.ts](src/agents/session-tool-result-guard.ts) | `installSessionToolResultGuard` |
| [src/agents/session-tool-result-guard-wrapper.ts](src/agents/session-tool-result-guard-wrapper.ts) | 包装器，通过 `redactSensitiveText`（来自 `../logging/redact.js`）递归 redact transcript content，按 `cfg?.logging?.redactSensitive` 决定模式 |
| 类型扩展 `GuardedSessionManager` | 注入 `flushPendingToolResults` / `clearPendingToolResults` |

### 7.2 Payload 级别 redact

[src/agents/payload-redaction.ts](src/agents/payload-redaction.ts)：

- `redactSensitivePayloadString` 把 `Authorization: Bearer xxxxx` / JWT / Cookie 替换为 `<redacted>`（line 16-48）。
- `isCredentialFieldName` / `shouldRedactImageData` 判别凭证字段（line 24-70）。
- `visitDiagnosticPayload`（line 76-）递归走 diagnostic payload，命中 `image/*` 数据的 base64 替换为 `<redacted>`（line 64-70），并用 SHA-256 digest 记录。

### 7.3 Prompt 字符串净化

[src/agents/sanitize-for-prompt.ts](src/agents/sanitize-for-prompt.ts)：

```ts
// 剥除 Unicode Cc/Cf + U+2028/2029
sanitizeForPromptLiteral(value: string): string;
// OC-19 威胁模型注释（line 2-15）

// 把不信任文本包裹在 <untrusted-text> 标签内并 &lt;/&gt; 转义
wrapUntrustedPromptDataBlock({ label, text, maxChars }): string;
```

### 7.4 Tool result 上下文守卫

| 文件 | 职责 |
|---|---|
| [src/agents/tool-result-context-guard.ts](src/agents/tool-result-context-guard.ts) | 工具结果占用的上下文比例守卫 |
| [src/agents/tool-result-truncation.ts](src/agents/tool-result-truncation.ts) | `truncateOversizedToolResultsInSession` / `sessionLikelyHasOversizedToolResults` / `resolveLiveToolResultMaxChars`。[run.ts:1680-1702](src/agents/pi-embedded-runner/run.ts#L1680-L1702) 在 overflow 恢复路径使用 |

---

## 八、循环检测

### 8.1 抽象

[src/agents/tool-loop-detection.ts](src/agents/tool-loop-detection.ts)：

```ts
type LoopDetectorKind =
  | "generic_repeat"
  | "unknown_tool_repeat"
  | "known_poll_no_progress"
  | "global_circuit_breaker"
  | "ping_pong";

type LoopDetectionResult =
  | { stuck: false }
  | { stuck: true; level: "warning" | "critical"; detector: LoopDetectorKind; count: number; message: string; pairedToolName?: string; warningKey?: string };

const TOOL_CALL_HISTORY_SIZE = 30;
const WARNING_THRESHOLD = 10;
const UNKNOWN_TOOL_THRESHOLD = 10;
const CRITICAL_THRESHOLD = 20;
const GLOBAL_CIRCUIT_BREAKER_THRESHOLD = 30;
```

`resolveLoopDetectionConfig(config?)`（line 85+）归一化用户配置。

### 8.2 注入路径

#### 通用 loop detection

[pi-tools.before-tool-call.ts:429-484](src/agents/pi-tools.before-tool-call.ts#L429-L484)：

```ts
detectToolCallLoop(...) // → LoopDetectionResult
recordToolCall(...)      // 记录到 history

// 命中 critical 时返回
{ blocked: true, deniedReason: "tool-loop", kind: "veto" }
```

#### Post-compaction 死循环

[pi-embedded-runner/post-compaction-loop-guard.ts](src/agents/pi-embedded-runner/post-compaction-loop-guard.ts)（由 [run.ts:96-100](src/agents/pi-embedded-runner/run.ts#L96-L100) import）：

```ts
createPostCompactionLoopGuard()
  → postCompactionGuard.observe(observation)
  → verdict.shouldAbort 时 attempt 终止
```

[run.ts:1184](src/agents/pi-embedded-runner/run.ts#L1184) 的 `onToolOutcome` 把每个 tool outcome 接入 `postCompactionGuard`。

#### 清理超时

[src/agents/run-cleanup-timeout.ts](src/agents/run-cleanup-timeout.ts)：

```ts
const AGENT_CLEANUP_STEP_TIMEOUT_MS = 10_000;

runAgentCleanupStep(...) // 用 withTimeout 包装清理步骤
// run.ts:2825-2859 调用
```

### 8.3 计数器（loop state）

```ts
// pi-tools.before-tool-call.ts:41-51
type HookContext = {
  agentId, config, sessionKey, sessionId,
  runId, trace,
  loopDetection,    // ← 挂在 HookContext 上，跟 sessionKey 关联
  onToolOutcome
};

// pi-tools.before-tool-call.ts:437
scope = args.ctx.runId ? { runId: args.ctx.runId } : undefined;
// 隔离单次 run 的 loop 记录
```

---

## 九、上下文窗口守卫

### 9.1 抽象

[src/agents/context-window-guard.ts](src/agents/context-window-guard.ts)：

```ts
const CONTEXT_WINDOW_HARD_MIN_TOKENS = 4_000;
const CONTEXT_WINDOW_WARN_BELOW_TOKENS = 8_000;
const CONTEXT_WINDOW_HARD_MIN_RATIO = 0.1;
const CONTEXT_WINDOW_WARN_BELOW_RATIO = 0.2;

type ContextWindowSource = "model" | "modelsConfig" | "agentContextTokens" | "default";

type ContextWindowInfo = { tokens: number; referenceTokens?: number; source: ContextWindowSource };

type ContextWindowGuardResult = ContextWindowInfo & {
  hardMinTokens: number;
  warnBelowTokens: number;
  shouldWarn: boolean;
  shouldBlock: boolean;
};

resolveContextWindowInfo({ cfg, provider, modelId, modelContextTokens?, modelContextWindow?, defaultTokens })
  → 先查 modelsConfig，再查 model/modelsConfig，最后 default
  → 按 agents.defaults.contextTokens cap
```

### 9.2 使用

- 通过 `ctxInfo = resolvedRuntimeModel.ctxInfo`（[run.ts:535](src/agents/pi-embedded-runner/run.ts#L535)）注入。
- [run.ts:1418-1424](src/agents/pi-embedded-runner/run.ts#L1418-L1424)：

```ts
tokenUsedRatio = lastTurnPromptTokens / ctxInfo.tokens
// 0.65 阈值触发 timeout compaction
```

- [run.ts:1725-1766](src/agents/pi-embedded-runner/run.ts#L1725-L1766)：在 `truncateOversizedToolResultsInSession` 路径中重复使用。

---

## 十、内部事件 / 事件总线

### 10.1 契约

[src/agents/internal-event-contract.ts](src/agents/internal-event-contract.ts)：

```ts
const AGENT_INTERNAL_EVENT_TYPE_TASK_COMPLETION = "task_completion";
const AGENT_INTERNAL_EVENT_SOURCES = ["subagent", "cron", "video_generation", "music_generation"];
const AGENT_INTERNAL_EVENT_STATUSES = ["ok", "timeout", "error", "unknown"];

type AgentInternalEventSource = ...;
type AgentInternalEventStatus = ...;
```

### 10.2 事件载荷

[src/agents/internal-events.ts](src/agents/internal-events.ts)：

```ts
type AgentTaskCompletionInternalEvent = {
  type: "task_completion";
  source: AgentInternalEventSource;
  childSessionKey: string;
  childSessionId?: string;
  announceType: string;
  taskLabel: string;
  status: AgentInternalEventStatus;
  statusLabel: string;
  result: string;
  mediaUrls?: string[];
  statsLine?: string;
  replyInstruction: string;
};

type AgentInternalEvent = AgentTaskCompletionInternalEvent;

formatTaskCompletionEvent(event) {
  // 注入 <<<BEGIN_UNTRUSTED_CHILD_RESULT>>> 哨兵并标明 "untrusted content"
}

formatTaskCompletionEventForPlainPrompt(event) {
  // 给非 markdown 通道用
}
```

通过 `escapeInternalRuntimeContextDelimiters`（[src/agents/internal-runtime-context.ts](src/agents/internal-runtime-context.ts)）防止事件字段破坏 prompt 模板，分隔符为 `INTERNAL_RUNTIME_CONTEXT_BEGIN/END`。

### 10.3 与 prompt 构造的关系

```ts
// pi-embedded-subscribe.types.ts:64
type SubscribeEmbeddedPiSessionParams = {
  ...
  internalEvents?: AgentInternalEvent[];
  ...
};

// pi-embedded-subscribe.ts:91-113, 122
pendingToolMediaUrls = collectPendingMediaFromInternalEvents(events)
// 用于一次性把 child task 媒体合并到当前回复
```

---

## 十一、Prompt 缓存稳定性

### 11.1 抽象

[src/agents/prompt-cache-stability.ts](src/agents/prompt-cache-stability.ts)：

```ts
// 替换 CRLF → LF、删除行尾空白、整体 trim
normalizeStructuredPromptSection(text: string): string;

// lowercase、trim、按字典序排序、去重
normalizePromptCapabilityIds(capabilities: string[]): string[];
// 目的：让系统 prompt 中的能力列表可稳定 prefix-cache
```

### 11.2 相关文件（per-provider cache hooks）

| 文件 | 职责 |
|---|---|
| [src/agents/context-cache.ts](src/agents/context-cache.ts) | 上下文缓存总入口 |
| [src/agents/pi-embedded-runner/google-prompt-cache.ts](src/agents/pi-embedded-runner/google-prompt-cache.ts) | Google prompt cache |
| [src/agents/pi-embedded-runner/anthropic-cache-control-payload.ts](src/agents/pi-embedded-runner/anthropic-cache-control-payload.ts) | Anthropic cache control |
| [src/agents/pi-embedded-runner/cache-ttl.ts](src/agents/pi-embedded-runner/cache-ttl.ts) | 缓存 TTL |
| [src/agents/pi-embedded-runner/prompt-cache-retention.ts](src/agents/pi-embedded-runner/prompt-cache-retention.ts) | 缓存保留策略 |
| [src/agents/pi-embedded-runner/anthropic-family-cache-semantics.ts](src/agents/pi-embedded-runner/anthropic-family-cache-semantics.ts) | Anthropic 族缓存语义 |
| [src/agents/pi-embedded-runner/prompt-cache-observability.ts](src/agents/pi-embedded-runner/prompt-cache-observability.ts) | 缓存可观测性 |

每个 provider 在 attempt 中以 stream wrappers / payload hooks 注入 cache control。

### 11.3 调用栈

- 在 attempt 的 `runtimePlan = buildAgentRuntimePlan({...})`（[run.ts:1079-1098](src/agents/pi-embedded-runner/run.ts#L1079-L1098)）之后由 provider 特定的 stream wrapper 应用。
- `attempt.promptCache`（[run.ts:1467](src/agents/pi-embedded-runner/run.ts#L1467)、[1625](src/agents/pi-embedded-runner/run.ts#L1625)）作为 `runtimeContext.promptCache` 注入 compaction 引擎。

---

## 十二、取消 / Abort

### 12.1 Run-level 生命周期

[src/agents/pi-embedded-runner/runs.ts](src/agents/pi-embedded-runner/runs.ts) 暴露：

| 函数 | 行号 | 用途 |
|---|---|---|
| `queueEmbeddedPiMessage` | 66-92 | 在 `handle.isStreaming()` 且 `!handle.isCompacting()` 时排队 steering 消息 |
| `abortEmbeddedPiRun` | 100-161 | 单 session abort / compacting-only / 所有 run abort |
| `abortAndDrainEmbeddedPiRun` | 329-344 | abort + 等待 settle |
| `requestEmbeddedRunModelSwitch` | 212-234 | 在 run 中途切模型 |
| `consumeEmbeddedRunModelSwitch` | 236-248 | 消费模型切换请求 |
| `waitForActiveEmbeddedRuns` | 256-284 | 等待所有 run 结束 |
| `waitForEmbeddedPiRunEnd` | 286-321 | 等待单 session run 结束（默认 15s） |
| `forceClearEmbeddedPiRun` | 410-428 | 卡死恢复 |
| `setActiveEmbeddedRun` / `clearActiveEmbeddedRun` / `updateActiveEmbeddedRunSnapshot` | 359-407 | 维护 ACTIVE_* map |

底层状态来自 [run-state.ts](src/agents/pi-embedded-runner/run-state.ts)：

```ts
const ACTIVE_EMBEDDED_RUNS: Map<sessionId, EmbeddedRunHandle>;
const ACTIVE_EMBEDDED_RUN_SESSION_IDS_BY_KEY: Map<sessionKey, sessionId>;
const ACTIVE_EMBEDDED_RUN_SNAPSHOTS: Map<sessionId, RunSnapshot>;
const EMBEDDED_RUN_MODEL_SWITCH_REQUESTS: Map<sessionId, EmbeddedRunModelSwitchRequest>;
const EMBEDDED_RUN_WAITERS: Map<sessionId, Waiter[]>;
```

### 12.2 Attempt-level abort 桥接

[run.ts:361-375](src/agents/pi-embedded-runner/run.ts#L361-L375)：

```ts
throwIfAborted() // 在 attempt 边界处检查 params.abortSignal，抛 AbortError
```

[run.ts:1105-1115](src/agents/pi-embedded-runner/run.ts#L1105-L1115)：

```ts
attemptAbortController + relayParentAbort 监听器
// 把外层 abortSignal 透传到 attempt
```

[run.ts:1234-1245](src/agents/pi-embedded-runner/run.ts#L1234-L1245)：

```ts
attempt.catch((err) => throw postCompactionAbortError ?? err)
// post-compaction 死循环 abort 优先于原始错误

attempt.finally(...)
  // 清理 listener + 重置 postCompactionAbortController
```

### 12.3 Live model switch

[src/agents/live-model-switch.ts](src/agents/live-model-switch.ts)：

```ts
shouldSwitchToLiveModel()  // 检查 liveModelSwitchPending=true 且当前模型 != 持久化选择
clearLiveModelSwitchPending()  // 清理 stale flag
```

当用户在 run 中切模型，`requestedSelection` 在 [run.ts:1392-1413](src/agents/pi-embedded-runner/run.ts#L1392-L1413) 被检查，命中 `canRestartForLiveSwitch`（[run.ts:1374-1379](src/agents/pi-embedded-runner/run.ts#L1374-L1379) 的"无 messaging / 无 tool / 无 assistant text"判定）时抛 `LiveSessionModelSwitchError`，外层 wrap 做重启。

---

## 十三、一次完整 ReAct 轮的端到端顺序

下面以 happy path（成功完成，无 compaction、无重试）为例，把 attempt 内部的事件订阅、横切 hook、tool 拦截器完整串起来。

### Step 1. 入口

```ts
runEmbeddedPiAgent(params) // run.ts:326
  → enqueueSession(...) → enqueueGlobal(...)
  → runLoopIterations = 0
```

### Step 2. 前置阶段（attempt 之前）

- `backfillSessionKey`（[run.ts:331-339](src/agents/pi-embedded-runner/run.ts#L331-L339)）
- workspace 解析 / runtime plugins 加载 / 模型解析（[run.ts:403-537](src/agents/pi-embedded-runner/run.ts#L403-L537)）
- `runOwnsCompactionBeforeHook`（如果需要）— 由 [run.ts:971-986](src/agents/pi-embedded-runner/run.ts#L971-L986) 提供函数
- 启动 stage tracker（[run.ts:384-401](src/agents/pi-embedded-runner/run.ts#L384-L401)）

### Step 3. prompt 构造

[run.ts:1058-1074](src/agents/pi-embedded-runner/run.ts#L1058-L1074)：

```ts
基础 prompt + 各种 retry instruction
  → 注入到 runtimePlan = buildAgentRuntimePlan({...})
```

### Step 4. runtime plan 构造

[run.ts:1079-1098](src/agents/pi-embedded-runner/run.ts#L1079-L1098)。

### Step 5. 执行 attempt

`runEmbeddedAttemptWithBackend`（[run.ts:1116-1233](src/agents/pi-embedded-runner/run.ts#L1116-L1233)）：

```
subscribeEmbeddedPiSession
  └─ createEmbeddedPiSessionEventHandler
      └─ 把事件链化（pendingEventChain）

模型流式响应
  ├─ message_start/update/end ─► handleMessage* (handlers.messages.ts)
  └─ tool_call ─► handleToolExecutionStart (handlers.tools.ts:604+)
       └─ 记录 start time、args、toolMetaById、emit tool/command item events

  执行 tool.execute：
    ├─► runBeforeToolCallHook (pi-tools.before-tool-call.ts:418)
    │   ├─ 1. loop detection (detectToolCallLoop / recordToolCall)
    │   ├─ 2. trusted policy (runTrustedToolPolicies)
    │   ├─ 3. plugin hook runner (hookRunner.runBeforeToolCall)
    │   └─ 4. approval gate (requestPluginToolApproval)
    │       └─ 第一阶段: plugin.approval.request
    │       └─ 第二阶段: plugin.approval.waitDecision
    │
    ├─► emit tool.execution.started diagnostic event
    │
    ├─► tool.execute
    │   └─ exec/sandbox/message 工具的内部审批
    │       ├─ exec 工具 → registerExecApprovalRequest + waitForExecApprovalDecision
    │       └─ owner-only 工具 → applyOwnerOnlyToolPolicy + wrapOwnerOnlyToolExecution
    │
    └─► handleToolExecutionEnd (handlers.tools.ts:1186-1227)
        ├─► emitToolResultOutput
        └─► after_tool_call hook (fire-and-forget)
```

### Step 6. 结果解释 & 终止

[run.ts:1248-2820](src/agents/pi-embedded-runner/run.ts#L1248-L2820) 依次：

1. post-compaction 循环保护 abort 检查
2. idle-timeout 熔断
3. overload/backoff
4. context overflow 恢复（可能触发 compaction）
5. assistant failover
6. payload 构造
7. 各种 retry 解析
8. 最终 `EmbeddedPiRunResult`

### Step 7. 清理

[run.ts:2822-2860](src/agents/pi-embedded-runner/run.ts#L2822-L2860)：

- context engine dispose / bundle-mcp retire

---

## 十四、新增横切关注点应插入的位置

| 关注点类型 | 应插入文件 | 关键钩子/扩展点 |
|---|---|---|
| 通用同步拦截（前置） | [src/agents/pi-tools.before-tool-call.ts](src/agents/pi-tools.before-tool-call.ts) | `runBeforeToolCallHook` (line 418) — 顺序：loop detection → trusted policy → global hook → approval gate |
| 工具执行后副作用（后置） | [src/agents/pi-embedded-subscribe.handlers.tools.ts](src/agents/pi-embedded-subscribe.handlers.tools.ts) | `handleToolExecutionEnd` 末尾 (line 1186-1226) — `after_tool_call` 触发点 |
| 上下文压缩副作用 | [src/agents/pi-hooks/compaction-safeguard.ts](src/agents/pi-hooks/compaction-safeguard.ts) | `ExtensionAPI` 注册；或 [run.ts:971-1012](src/agents/pi-embedded-runner/run.ts#L971-L1012) 的 `runOwnsCompactionBeforeHook` / `runOwnsCompactionAfterHook` |
| 工具授权策略 | [src/agents/tool-policy.ts](src/agents/tool-policy.ts) (line 23-79) + [src/agents/tool-policy-pipeline.ts](src/agents/tool-policy-pipeline.ts) | `applyOwnerOnlyToolPolicy` / pipeline |
| 工具结果持久化 | [src/agents/session-tool-result-guard.ts](src/agents/session-tool-result-guard.ts) + [src/agents/session-tool-result-guard-wrapper.ts](src/agents/session-tool-result-guard-wrapper.ts) | `installSessionToolResultGuard` |
| Prompt 内净化 | [src/agents/sanitize-for-prompt.ts](src/agents/sanitize-for-prompt.ts) | `sanitizeForPromptLiteral` / `wrapUntrustedPromptDataBlock` |
| Payload 级别净化 | [src/agents/payload-redaction.ts](src/agents/payload-redaction.ts) | `redactSensitivePayloadString` / `visitDiagnosticPayload` |
| 循环检测 | [src/agents/tool-loop-detection.ts](src/agents/tool-loop-detection.ts) + [src/agents/pi-tools.before-tool-call.ts:429-484](src/agents/pi-tools.before-tool-call.ts#L429-L484) | `detectToolCallLoop` / `recordToolCall` |
| Post-compaction 死循环 | [src/agents/pi-embedded-runner/post-compaction-loop-guard.ts](src/agents/pi-embedded-runner/post-compaction-loop-guard.ts) | `createPostCompactionLoopGuard`，`onToolOutcome` 已在 [run.ts:1184](src/agents/pi-embedded-runner/run.ts#L1184) 接入 |
| Context window 决策 | [src/agents/context-window-guard.ts](src/agents/context-window-guard.ts) | `resolveContextWindowInfo`，下游在 [run.ts:535](src/agents/pi-embedded-runner/run.ts#L535)、[1418-1424](src/agents/pi-embedded-runner/run.ts#L1418-L1424)、[1725-1766](src/agents/pi-embedded-runner/run.ts#L1725-L1766) 使用 |
| Prompt cache 稳定性 | [src/agents/prompt-cache-stability.ts](src/agents/prompt-cache-stability.ts) | `normalizeStructuredPromptSection` / `normalizePromptCapabilityIds` |
| Run-level abort | [src/agents/pi-embedded-runner/runs.ts](src/agents/pi-embedded-runner/runs.ts) (line 100-161) | `abortEmbeddedPiRun` + `run-state.ts` |
| Attempt-level abort | [src/agents/pi-embedded-runner/run.ts:361-375](src/agents/pi-embedded-runner/run.ts#L361-L375), [1105-1115](src/agents/pi-embedded-runner/run.ts#L1105-L1115) | `throwIfAborted` + `relayParentAbort` |
| 内部事件 | [src/agents/internal-events.ts](src/agents/internal-events.ts) | `AgentTaskCompletionInternalEvent` 注入 prompt 模板 |
| Cleanup timeout | [src/agents/run-cleanup-timeout.ts](src/agents/run-cleanup-timeout.ts) | `runAgentCleanupStep` |

---

## 十五、状态如何跨横切关注点被保留

OpenClaw 通过 **两层显式状态容器** 来在 attempt 之间 / 跨关注点维持对话连续性：

### 1. Run-loop 闭包变量（attempt 间持久化）

[src/agents/pi-embedded-runner/run.ts:430-840](src/agents/pi-embedded-runner/run.ts#L430-L840)：

```ts
// 模型 / provider
provider, modelId, runtimeModel, effectiveModel, thinkLevel

// auth
profileIndex, lastProfileId, runtimeAuthState, apiKeyInfo

// session
activeSessionId, activeSessionFile  // adoptCompactionTranscript 后切换

// compaction
autoCompactionCount, lastCompactionTokensAfter

// replay
accumulatedReplayState = createEmbeddedRunReplayState()

// 重试计数器
planningOnlyRetryAttempts, reasoningOnlyRetryAttempts, emptyResponseRetryAttempts,
compactionContinuationRetryAttempts, sameModelIdleTimeoutRetries,
emptyErrorRetries, overflowCompactionAttempts, timeoutCompactionAttempts,
overloadProfileRotations, rateLimitProfileRotations

// post-compaction 保护
postCompactionGuard, postCompactionAbortController

// compaction 续接
nextAttemptPromptOverride, suppressNextUserMessagePersistence
```

### 2. EmbeddedPiSubscribeState（attempt 内派生状态）

[src/agents/pi-embedded-subscribe.handlers.types.ts:30-109](src/agents/pi-embedded-subscribe.handlers.types.ts#L30-L109)：

```ts
type EmbeddedPiSubscribeState = {
  assistantTexts: string[];
  toolMetas: Map<string, ToolMeta>;
  toolMetaById: Map<string, ToolMeta>;
  itemActiveIds: Set<string>;
  itemStarted: Map<string, ...>;
  itemCompleted: Map<string, ...>;
  compactionInFlight: boolean;
  replayState: ReplayState;
  livenessState: "blocked" | "abandoned" | "paused" | "working";
  // ...各种 messaging tool pending maps
  pendingToolMediaUrls: string[];
};
```

### 3. 持久化层

```ts
// SessionManager（@mariozechner/pi-coding-agent）持有 message transcript
//   由 installSessionToolResultGuard 注入到 SessionManager
//   session-tool-result-guard-wrapper.ts

// authStore（run.ts:539-543, 622-633）
//   保存 profile 使用/失败状态

// usageAccumulator（run.ts:773, 1281）
//   跨 attempt 累计 token

// diag 日志（runs.ts:15-19）
//   记录 run state 变化
```

### 4. 参数流

每次 attempt 把 `accumulatedReplayState` 透传到 `initialReplayState`（[run.ts:1177](src/agents/pi-embedded-runner/run.ts#L1177)），把 `lastAssistant` / `messagesSnapshot` 等结果从 attempt 返回，闭包变量把它们再合并回去。

通过这套分层状态结构，所有"横切关注点"在 attempt 边界都能从同一个会话真实状态中读取、修改、写入，而不需要另起并行结构。

---

## 十六、设计原则总结

1. **attempt = ReAct 单次迭代**，`runEmbeddedAttemptWithBackend` 封装完整 Thought→Action→Observation；上层 `while(true)` 只做"重试 / 降级 / 终止"决策。
2. **横切关注点 = 4 类形式**：工具拦截 / 事件订阅 / 生命周期 hook / 策略层。每一类有明确的插入点（见第十四节）。
3. **两阶段审批协议**统一：`request` + `waitDecision`，通过网关 RPC；exec 工具和插件工具对称设计。
4. **判别联合 (discriminated unions)** 风格贯穿：
   - `HookOutcome = { blocked: true; kind?; deniedReason?; reason; params? } | { blocked: false; params }`
   - `LoopDetectionResult = { stuck: false } | { stuck: true; level; detector; count; ... }`
   - `ContextWindowInfo = { tokens; referenceTokens?; source: "model" | "modelsConfig" | "agentContextTokens" | "default" }`
   - `AgentInternalEvent = AgentTaskCompletionInternalEvent`
5. **事件链序列化**：`pendingEventChain` 保证 handler 顺序执行；`tool_execution_end` 标记 `detach: true` 不阻塞下一事件。
6. **Compaction 双触发**：timeout（0.65 比例）vs overflow（明确错误），分别走不同恢复路径；post-compaction 死循环由 `postCompactionGuard` 防御。
7. **Prompt cache 稳定性靠文本规范化**：`normalizeStructuredPromptSection` + `normalizePromptCapabilityIds` 让系统 prompt 可稳定 prefix-cache。
8. **状态分层**：run 闭包（attempt 间） + `EmbeddedPiSubscribeState`（attempt 内） + `SessionManager`（持久化）三层隔离，互不污染。

---

**核心抽象一句话总结**：OpenClaw 的 ReAct 循环 = **run.ts 的 `while(true)` + attempt 后端的单次迭代**；横切关注点以 **"工具拦截 + 事件订阅 + 生命周期 hook + 策略层"** 四种形式注入到 attempt 内部；状态通过 **"run 闭包变量 + EmbeddedPiSubscribeState + SessionManager"** 三层容器维持。所有新增流程都应在第十四节表格中找到对应插入点。