# ReAct 循环设计解析：agent-dev 与 Claude Code 对比分析

> 整理时间：2026-06-18

---

## 一、什么是 ReAct 循环

ReAct（Reasoning + Acting）是 Agent 系统的核心循环模式。它让 LLM 交替进行"思考"（Reasoning）和"行动"（Acting，即调用工具），形成闭环：

```
用户输入
    │
    ▼
  ┌─────────────────────────────────────────────────┐
  │              ReAct 循环（多轮 until stop）         │
  │                                                  │
  │  1. LLM 接收：system + 历史消息 + 工具 schema      │
  │  2. LLM 输出：思考 或 直接回答 或 工具调用           │
  │  3. 如果是工具调用 → 执行工具 → 结果送回 LLM       │
  │  4. 如果是直接回答 → 返回给用户，循环结束            │
  │  5. 达最大轮数 → 强制结束                         │
  └─────────────────────────────────────────────────┘
    │
    ▼
  最终回答
```

这个循环在 ReAct 论文（ICLR 2023 notable-top-5%）中被形式化为：

```
Thought → Action → Observation → Thought → ... → Final Answer
```

其中：
- **Thought**：LLM 的推理过程（"我需要查天气，所以调用 get_weather"）
- **Action**：工具调用（`get_weather(location="北京")`）
- **Observation**：工具返回结果（"北京 25°C"）
- 循环直到 LLM 产生 Final Answer

现代实现中，Function Calling API 在协议层面用 `tool_use` / `tool_result` 取代了原始 ReAct 的 stop sequence 方式，但循环逻辑本质不变。

---

## 二、agent-dev 的 ReAct 实现

### 2.1 整体架构

```
ReactAgent.run(user_message)
    │
    ▼ yield generator
    ┌─── for turn in 1..max_turns ───────────────────┐
    │                                                  │
    │  1. 检查 token 预算 → 必要时自动压缩 message 历史  │
    │  2. 拼接 messages（system + 历史 + 本轮用户消息）  │
    │  3. 调用 LLM.chat() → 收集流式 chunk              │
    │     ├─ text_delta → yield ("text", ...)          │
    │     ├─ thinking_delta → yield ("thinking", ...)   │
    │     ├─ tool_call_delta → 存入 tool_calls 列表     │
    │     └─ usage → 更新 baseline + yield              │
    │  4. tool_calls 为空？→ 最终回答，break             │
    │  5. 执行工具（串行/并行 ThreadPoolExecutor）        │
    │     ├─ yield ("tool_call", ...)                   │
    │     ├─ yield ("tool_result", ...)                  │
    │     └─ 追加 tool_result 到 messages                │
    │  6. 持久化到 session（JSONL）                      │
    │  7. 继续下一轮                                    │
    └──────────────────────────────────────────────────┘
    │
    ▼ yield ("system", "Completed")
```

### 2.2 核心代码结构

```python
# agent_core/agent_core.py:ReactAgent.run()
def run(self, user_message: str):
    # 1. 追加用户消息
    self.messages.append({"role": "user", "content": user_message})
    yield ("system", f"🔄 开始处理")

    # 2. Token 预算检查 → 自动压缩
    compacted, result = self.context_manager.check_and_compact(...)
    if result.success:
        self.messages = compacted

    for turn in range(1, self.max_turns + 1):
        yield ("system", f"🔄 Turn {turn}/{self.max_turns}")

        # 3. 拼接 messages（含 system prompt 注入）
        messages_for_llm = self._prepare_messages()

        # 4. LLM 调用（流式）
        tool_calls = []
        full_text = ""
        thinking_text = ""
        for chunk in self.llm.chat(messages_for_llm, tools):
            if chunk.text_delta:     # → yield text
            if chunk.thinking_delta: # → yield thinking
            if chunk.tool_call:      # → 收集
            if chunk.usage:          # → 更新 baseline

        # 5. 无工具 → final answer
        if not tool_calls:
            self.messages.append({"role": "assistant", "content": full_text})
            yield ("system", "✅ 回答完成")
            break

        # 6. 执行工具并追加结果到 messages
        for tc in tool_calls:
            result = self.tools.execute(tc.tool_name, tc.tool_input)
            self.messages.append(_make_tool_result_block(...))
            yield ("tool_result", {"name": tc.tool_name, "output": result})
```

### 2.3 关键设计决策

**决策 1：生成器 yield 架构**

整个 `run()` 是一个生成器（generator），将中间状态全部 yield 给 UI：
- `("text", text)` → 流式文本输出
- `("thinking", text)` → 思考过程
- `("tool_call", {...})` → 工具调用开始
- `("tool_result", {...})` → 工具执行结果
- `("usage", usage)` → Token 消耗
- `("system", msg)` → 系统状态消息

UI 端的 `st.write_stream()` 或 Streamlit 的事件循环直接消费这些 event，无需额外的消息总线。

**决策 2：单层消息历史**

`self.messages` 是唯一的消息历史容器，所有信息（system/assistant/user/tool_result）都储存在其中。Claude Code 使用专用的 `Message` 类型系统，agent-dev 使用简单 dict。

**决策 3：压缩后的精心持久化**

压缩后，需将 boundary + summary + preserved head 按顺序写回 JSONL，确保断点续传时 history 完整。曾踩过 P0 bug：只持久化 boundary 和 summary，preserved head 六条不写盘，重启后上下文残缺。

**决策 4：Baseline 持久化**

每轮 API 返回的 `usage.input_tokens` 持久化到 jsonl `usage` 字段。F5 刷新时从最后一条恢复 baseline，避免 Token 估算跳变。

---

## 三、Claude Code 的 ReAct 实现

### 3.1 整体架构

Claude Code 的 ReAct 循环是分层设计的：

```
QueryEngine（跨 Turn 会话状态）
    │
    ├── QueryEngine.submitMessage()
    │       │
    │       │  处理用户输入 / slash command / agent 选择
    │       │  构造 system prompt（6 层叠加）
    │       │  准备 processUserInputContext（+ hooks）
    │       │       │
    │       ▼
    │   queryLoop()  ← 主循环（多轮 until stop）
    │       │
    │       ├── 每轮循环：
    │       │   1. 消息预处理（snip / microcompact / autocompact）
    │       │   2. buildPostCompactMessages（若压缩发生）
    │       │   3. blocking 检查（防止死锁）
    │       │   4. 调用 API callModel() 流式接收
    │       │   5. 收集 tool_use blocks
    │       │   6. 执行工具（toolOrchestration 管道）
    │       │   7. 继续循环 或 结束
    │       │
    │       └── 返回 Terminal { reason: 'done' | 'max_tokens' | 'blocking' }
    │
    └── QueryEngine 状态（跨多次 submitMessage 持久）
```

### 3.2 关键代码结构

**queryLoop()**（`src/query.ts`）：

```typescript
async function* queryLoop(params, consumedCommandUuids):
    // Mutable state for cross-iteration state
    let state: State = {
        messages: params.messages,
        toolUseContext: params.toolUseContext,
        turnCount: 1,
        ...
    }

    while (true) {
        // 1. 消息预处理管线
        let messagesForQuery = getMessagesAfterCompactBoundary(messages)
        messagesForQuery = await applyToolResultBudget(messagesForQuery, ...)
        messagesForQuery = snipCompactIfNeeded(messagesForQuery)
        messagesForQuery = await microcompact(messagesForQuery, ...)
        messagesForQuery = await contextCollapse.applyCollapsesIfNeeded(messagesForQuery, ...)

        // 2. 自动压缩（Forked Agent 方式）
        const { compactionResult } = await deps.autocompact(messagesForQuery, ...)
        if (compactionResult) {
            logEvent('tengu_auto_compact_succeeded', ...)
            tracking = { compacted: true, turnId: uuid(), turnCounter: 0 }
            const postCompactMessages = buildPostCompactMessages(compactionResult)
            for (const message of postCompactMessages) yield message
            messagesForQuery = postCompactMessages
        }

        // 3. Blocking 检查（防止死锁）
        if (isAtBlockingLimit && !compactionResult) {
            yield createAssistantAPIErrorMessage(PROMPT_TOO_LONG_ERROR_MESSAGE)
            return { reason: 'blocking_limit' }
        }

        // 4. API 调用（流式 + tool_use 收集）
        let assistantMessages: AssistantMessage[] = []
        let toolUseBlocks: ToolUseBlock[] = []
        for await (const message of deps.callModel({...})) {
            if (message.type === 'assistant' && has_tool_use) {
                assistantMessages.push(message)
                // 收集 tool_use blocks
            }
            yield message
        }

        // 5. 无 tool_use → 结束
        if (toolUseBlocks.length === 0) {
            // 检查是否需要 fallback / retry
            if (needsRetry) continue
            return { reason: 'done' }
        }

        // 6. 执行工具（StreamingToolExecutor / runTools）
        const toolResults = await runTools(toolUseBlocks, toolUseContext, ...)

        // 7. 更新 state 继续下一轮
        state = { ...state, messages: [...messages, ...assistantMessages, ...toolResults] }
        state.turnCount++
    }
```

### 3.3 关键设计决策

**决策 1：Layered State Management**

每次循环末尾显式构造新 state：
```typescript
state = { ...state, messages: [...messages, ...assistantMessages, ...toolResults] }
turnCount
```

而不是直接 push 到 messages 数组。这让状态流转**可追踪、可测试**，每个 continue 点是显式的。

**决策 2：预处理管线流水线**

消息在进行 API 调用前经过多个阶段处理：
1. **snip**（HISTORY_SNIP feature gate）：缩小历史但保留最新上下文
2. **microcompact**：缩减重复的低价值工具调用
3. **context collapse**：投影压缩视图
4. **auto-compact**（Forked Agent）：由独立的压缩 Agent 生成摘要

每个阶段通过 feature gate 控制是否启用。这种设计保证了扩展性——新增压缩策略只需加到管线中，不影响循环主体。

**决策 3：StreamingToolExecutor vs 普通 runTools**

两种工具执行模式：
- **StreamingToolExecutor**：工具结果边执行边流式输出（对应 streaming tool execution feature）
- **runTools**（`toolOrchestration.ts`）：批量执行后一次性返回

选择在循环开始时由 `config.gates.streamingToolExecution` 决定。

**决策 4：Circuit Breaker 熔断**

连续 autocompact 失败 3 次后启动熔断器，彻底停止该会话的自动压缩，防止 API 额度浪费。

**决策 5：Message 类型系统**

Claude Code 用完整的 TypeScript 类型系统区分消息类型：
```typescript
UserMessage | AssistantMessage | ToolUseSummaryMessage | TombstoneMessage | ...
```

每种类型有各自的 `type` 字段和严格的结构定义。这与 agent-dev 的简单 dict 形成对比。

---

## 四、设计对比分析

| 维度 | agent-dev | Claude Code | 差异分析 |
|------|-----------|-------------|---------|
| **实现语言** | Python（单文件 ~800 行） | TypeScript（query.ts ~1600 行 + 多个模块） | 语言差异导致类型安全检查不同 |
| **消息表示** | 简单 dict | 严格 TypeScript 联合类型 Message | Claude Code 有完整的消息类型系统 |
| **循环方式** | 显式 for turn in range() | while(true) + state variable + return | 两者本质一致，风格不同 |
| **流式输出** | yield 元组标签给 UI 消费 | yield Message 对象（统一协议） | agent-dev 更贴近 Python 生成器哲学 |
| **工具执行** | ThreadPoolExecutor（并行） | StreamingToolExecutor / runTools 两种 | agent-dev 简单但实用 |
| **压缩策略** | Preserved Head v4（硬停止） | Forked Agent + snip + microcompact + collapse | Claude Code 更复杂但更精细 |
| **状态管理** | 直接操作 self.messages | State 对象+ 显式构造新 state | Claude Code 更可靠但更繁琐 |
| **熔断机制** | 无 | 3次失败后停 autocompact | 生产级必备 |
| **错误恢复** | 生成器双层 try/catch | fallback model + retry | Claude Code 恢复路径全面 |
| **性能监控** | 无 | queryProfiler / headlessProfiler / 遥测 | 生产级监控 |
| **持久化** | JSONL（每步写） | sessionStorage（每步写） | 理念一致 |
| **消息类型系统** | 统一 messages 数组 | sender/receiver/tool 三类 + 专用求和类型 | 系统性差异 |

### 4.1 循环控制结构的本质差异

**agent-dev 的显式 for 循环**：

```python
for turn in range(1, self.max_turns + 1):
    # 调用 LLM、执行工具、追加结果
    if not tool_calls:
        break  # 最终回答
```

特点是控制流简单直观，每个 turn 是独立的迭代，适合教学和调试。

**Claude Code 的 while(true) + state**：

```typescript
while (true) {
    let { messages, toolUseContext } = state
    // 消息预处理管线
    // API 调用
    if (toolUseBlocks.length === 0) return { reason: 'done' }
    // 执行工具
    state = { ...state, messages: [...messages, ...assistantMessages, ...toolResults] }
    state.turnCount++
}
```

特点是状态显式管理，每个 continue 点留有 audit trail。

**核心差异**：agent-dev 的 `for...break` 更加教学友好，Claude Code 的 `while...state` 更适合生产级的扩展（每个 continue 点可以独立加日志/监控/feature gate）。

### 4.2 消息预处理的管线化 vs 单点压缩

这是两者最大的工程复杂度差异：

| 特性 | agent-dev | Claude Code |
|------|-----------|-------------|
| 压缩策略 | Preserved Head v4 | Forked Agent 生成摘要 |
| 前置处理 | check_and_compact | snip → microcompact → collapse → autocompact |
| 熔断 | 无 | ✓ 连续失败 3 次停 auto-compact |
| 保护机制 | 硬 stop + 第一个 turn 必含 | PTL fallback（剥洋葱） |
| 状态重建 | 直接替换 self.messages | buildPostCompactMessages yield 到 session |

Claude Code 的多阶段管线是为了处理生产环境的多样场景：
- **snip**：快速缩小但保留关键上下文
- **microcompact**：缩减低价值工具调用
- **context collapse**：投影压缩，无新的 API 调用
- **autocompact**：Forked Agent 生成摘要（代价最高）

agent-dev 的精简设计更适合理清概念，但生产环境需要多阶段组合。

### 4.3 工具执行的差异

| 特性 | agent-dev | Claude Code |
|------|-----------|-------------|
| 执行方式 | ThreadPoolExecutor | StreamingToolExecutor / runTools |
| 并发 | 显式并行 | 默认串行，需 isConcurrencySafe |
| 重试 | 3 次串行重试 | FallbackTriggeredError + 自动降级 |
| 权限 | 无专门系统 | ToolPermissionContext + canUseTool hook |
| 审计 | 无 | permissionDenials 收集 |

Claude Code 的工具执行是一个完整的 Runtime Pipeline（解析 → 校验 → Hook → 权限 → 执行 → 格式化 → 回流），远比 agent-dev 复杂，但这是生产环境的安全需求。

### 4.4 状态持久化的差异

两者都用了"每个操作步骤逐条写"的模式（非全量重写），但：

| 特性 | agent-dev | Claude Code |
|------|-----------|-------------|
| 存储格式 | JSONL | JSONL（sessionStorage） |
| 压缩后持久化 | boundary + summary + preserved head | buildPostCompactMessages yield |
| Baseline 保存 | usage 字段持久化到 jsonl entry | tokenCountWithEstimation 计算 |
| O(1) 读取 | read_tail(64KB) | 未深入 |
| 断链检测 | parentUuid 链 | 未深入 |

---

## 五、从 Claude Code 学到的设计模式

### 5.1 消息预处理管线

Claude Code 的管线设计值得借鉴：

```
messagesForQuery
    → applyToolResultBudget()    // 工具结果大小预算
    → snipCompactIfNeeded()      // 快速缩小
    → microcompact()              // 低价值压缩
    → contextCollapse()           // 投影压缩
    → autocompact()               // Forked Agent 摘要
    → blocking limit check        // 防止死锁
    → callModel()                 // 最终 API 调用
```

每个步骤独立、可 feature-gate、可观测。agent-dev 可以演进为类似的管线模式，将 `check_and_compact` 拆成可组合的步骤。

### 5.2 State 显式管理

agent-dev 目前直接在 `self.messages` 上操作（append/pop/替换），状态变更隐式。从 Claude Code 学到的模式：

```python
# 隐式（agent-dev 当前风格）
self.messages.append(assistant_msg)
self.messages.append(tool_result)

# 显式（Claude Code 风格）
new_messages = list(self.messages)
new_messages.extend([assistant_msg, tool_result])
self.messages = new_messages
```

显式管理的优势：
- 每个变更点可加日志/钩子
- continue 点可追踪
- 易于 undo/time travel

### 5.3 Tool 运行时协议

Claude Code 的 `Tool` 不是简单的 "函数映射"，而是一个**运行时协议对象**：

```typescript
Tool = {
    name: string
    description: string
    inputSchema: JSONSchema
    call(...) -> Promise<ToolResult>
    isConcurrencySafe() -> boolean     // 并发安全性
    isReadOnly() -> boolean            // 只读标记
    isDestructive() -> boolean         // 破坏性标记
    checkPermissions() -> Permission   // 权限检查
    validateInput() -> ValidationResult // 输入校验
    renderToolUseMessage() -> Component // UI 表现
    ...
}
```

agent-dev 的 `ToolRegistry.execute()` 只是简单函数调用，缺乏安全属性声明。生产化方向是扩展 Tool 协议。

### 5.4 System Prompt 分层管理

Claude Code 将 system prompt 拆为 6 层：

```text
1. 默认主系统提示（src/constants/prompts.ts）
2. 有效 prompt 组装器（override/custom/append）
3. 运行时上下文注入（CLAUDE.md/date/git）
4. 启动期附加指令入口
5. Prompt 缓存与失效
6. 专项 prompt（compact/sessionMemory/memories）
```

agent-dev 目前只有 `self.system_prompt = config.system_prompt` 加简单拼接。生产化方向是分层管理。

---

## 六、ReAct 循环演进路线（从 prototype 到 production）

### Phase 1：核心循环（agent-dev 当前状态）

```
✓ 完整的 ReAct 循环（Thought → Action → Observation）
✓ 流式输出（text/thinking/tool_call/tool_result）
✓ 简单的 Token 预算管理
✓ 压缩（Preserved Head v4 + Fork 模式）
✓ 基本持久化（JSONL）
✓ 工具并行执行
```

### Phase 2：生产化（推荐演进方向）

```
□ 消息预处理管线（snip → microcompact → autocompact）
□ State 显式管理（关闭隐式 mutation）
□ Circuit Breaker 熔断
□ Tool 运行时协议（安全属性声明）
□ System Prompt 分层管理
□ 错误恢复路径完善（fallback model + retry）
□ 监控与遥测
```

### Phase 3：高级特性（长期演进方向）

```
□ Streaming Tool Execution（工具结果边执行边流式）
□ Context Collapse（投影压缩）
□ Task Budget（API 级别的 token 预算分配）
□ 消息审计日志
□ Forked Agent 压缩的 cache 预热策略
```

---

## 七、关键坑点回顾

| 坑点 | 影响 | 根因 | 修复 |
|------|------|------|------|
| **压缩后 preserved head 不持久化** | 重启后上下文残缺 | `_persist_compacted_messages` 只写 boundary+summary | 补全 preserved head 的逐条写入 |
| **F5 刷新 Token 跳变** | 侧边栏数字从 64K 跳到 90K | baseline 因 agent 重建归零 | usage 持久化 + O(1) read_tail 恢复 |
| **同一变量名承载不同类型**（turn_thinking） | app.py crash | dict 和 int 共用变量名 | 改为 turn_thinking_tokens |
| **Streamlit 热重载日志重复** | 每行日志输出两次 | handler 在热重载时重复添加 | isinstance 检测已存在 handler |
| **Fork 压缩单元测试盲区** | conversation_text NameError 漏检 | mock 掉 LLM 调用绕过真实路径 | 需端到端测试（浏览器实测） |
| **GLM 消息格式差异** | tool_result 格式不符导致循环中断 | 不同 Provider 输出结构不同 | router 层统一格式 |

---

## 八、结论

### 核心差异

1. **agent-dev** 是"教学正确"的实现——代码简洁、可读性强、概念清晰，适合理解 ReAct 循环的本质
2. **Claude Code** 是"生产正确"的实现——分层架构、显式状态管理、多阶段预处理、完整的错误恢复

### 关键洞察

- **ReAct 循环本身的代码量不到 200 行**（agent-dev 核心循环仅 ~160 行）。真正复杂的是外围系统：Token 管理、压缩、持久化、工具执行管线、错误恢复。
- **Function Calling 在协议层面取代了 stop sequence**，但循环逻辑（Thought → Action → Observation）30 年不变的图灵机模式本质没有变。
- **生产级 Agent 的 80% 代码在非循环逻辑上**：压缩、持久化、错误处理、安全、监控——这些才是工程化落地的门槛。
- **从教学到生产的最大跨越**不是循环逻辑本身，而是"如果 API 返回异常怎么办"的无数个分支处理。

### 源码路径

| 实现 | 关键文件 |
|------|----------|
| agent-dev | `agent_core/agent_core.py`（~800 行，`run()` 为核心生成器） |
| Claude Code | `src/query.ts`（~1600 行，`queryLoop()` 为核心循环） |
| Claude Code | `src/QueryEngine.ts`（~1300 行，跨 Turn 会话管理） |
| Claude Code | `src/services/tools/toolOrchestration.ts`（工具执行编排） |
| Claude Code | `src/services/compact/autoCompact.ts`（自动压缩策略） |
| Claude Code | `src/services/compact/compact.ts`（Forked Agent 压缩实现） |

### 参考对比

| 实现 | 规模 | 语言 | 压缩策略 | 工具执行 | 状态管理 | 持久化 |
|------|------|------|----------|----------|----------|--------|
| agent-dev | ~800 行（核心） | Python | Preserved Head v4 | ThreadPoolExecutor | 隐式 self.messages | JSONL |
| Claude Code | ~3000 行（核心） | TypeScript | Forked Agent + snip + MC | StreamingToolExecutor / runTools | 显式 State 对象 | sessionStorage |
| LangGraph | 框架级别 | Python | 内置 | 通过 Node 编排 | 声明式 StateGraph | SqliteSaver |
