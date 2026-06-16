# 上下文压缩与 Token 估算：原理深度解析

> 本文档从第一性原理出发，解释 Agent 系统中"上下文压缩"的本质，以及 Claude Code 和 agent-dev 各自的 Token 估算方法。是 `context-management-implementation-design.md` 的理论补充篇。

---

## 目录

1. [背景：为什么需要上下文压缩](#1-背景为什么需要上下文压缩)
2. [压缩的本质：不是"压缩文件"，而是"压缩历史"](#2-压缩的本质不是压缩文件而是压缩历史)
3. [Token 估算的根本问题：你永远不知道 API 收到了多少](#3-token-估算的根本问题你永远不知道-api-收到了多少)
4. [Claude Code 的三层 Token 估算架构](#4-claude-code-的三层-token-估算架构)
5. [agent-dev 的 Token 估算现状](#5-agent-dev-的-token-估算现状)
6. [增量估算：用 API usage 作基准的核心思想](#6-增量估算用-api-usage-作基准的核心思想)
7. [对比总结与优化建议](#7-对比总结与优化建议)

---

## 1. 背景：为什么需要上下文压缩

### 1.1 问题定义

LLM 有固定的上下文窗口（Context Window），比如：

| 模型 | Context Window |
|---|---|
| Claude Sonnet 4 | 200K tokens |
| GLM-5.1 | 128K tokens |
| GPT-4o | 128K tokens |

一个持续对话的 Agent，**每轮都会把之前的所有消息带上**，所以上下文会持续增长：

```
第1轮：system(500t) + user_msg(100t)           = 600t
第2轮：system(500t) + user_msg(100t) + asst(200t) + user_msg(100t) = 900t
第3轮：...                                     = 1200t
...
第N轮：接近 200K，触发窗口上限 → LLM 拒绝响应
```

### 1.2 解决方案：压缩历史

在上下文逼近上限之前，把"中间的历史消息"替换成一个简短的 summary，腾出空间：

```
压缩前（22条，108K tokens）：
  [system] + [msg1] + [msg2] + ... + [msg21] + [当前用户消息]

压缩后（8条，6K tokens）：
  [system] + [summary of msg1..msg21] + [msg16..msg21] + [当前用户消息]
```

**关键**：压缩不是删除——是**用更少的 tokens 保留同等的信息密度**。

---

## 2. 压缩的本质：不是"压缩文件"，而是"压缩历史"

### 2.1 常见误解

> ❌ "压缩就是把消息打包成 zip"
>
> ❌ "压缩是在发送前把上下文压缩一下"
>
> ❌ "压缩的是下一次窗口的上下文"

### 2.2 准确的说法

**压缩改写的，是"消息列表"这个数据结构**。

在 Agent 内部，`self.messages` 是一个 Python list，每条消息是一个 dict：

```python
self.messages = [
    {"role": "system", "content": "You are a helpful assistant..."},
    {"role": "user", "content": "帮我查一下天气"},
    {"role": "assistant", "content": "好的，你想查哪个城市？"},
    {"role": "user", "content": "深圳"},
    {"role": "assistant", "content": "..."},  # tool_use: search_weather
    ...
]
```

**压缩做的事**：把 `self.messages` 的中间部分（旧消息）替换成 summary，让 list 变短。

```python
# 压缩前：self.messages 有 22 条
self.messages = [msg0, msg1, msg2, ..., msg21]

# 压缩后：self.messages 变成 8 条
self.messages = [
    msg0,                                          # system prompt
    {"role": "user", "content": "[Previous conversation summarized]\n\n..."},  # summary
    msg16, msg17, msg18, msg19, msg20, msg21,      # 最近 6 条
]
```

**下一轮 LLM 调用时**，发给 API 的就是压缩后的 `self.messages`——更短，但信息密度更高。

### 2.3 压缩的时间点

```
用户消息到达
    ↓
self.messages.append(user_message)
    ↓
触发检查：_check_budget() → used_tokens > compact_threshold?
    ↓
是 → 调 compact(messages) → 返回 compressed_messages
    ↓
self.messages = compressed_messages   ← 改写消息列表
    ↓
继续这一轮的 LLM 调用（用压缩后的 messages）
```

**所以压缩发生在"这一轮对话进行过程中"，压缩的对象是"这一轮之前的历史消息"**。

---

## 3. Token 估算的根本问题：你永远不知道 API 收到了多少

### 3.1 问题

要触发压缩，你需要知道"当前上下文有多大"。但：

**你手算的 token 数 ≠ API 实际收到的 token 数**

原因：

| 原因 | 说明 |
|---|---|
| 缓存命中 | API 可能缓存了 system prompt，`cache_read_input_tokens` 不计入计费，但你的手算会重复计算 |
| 消息去重 | 同名工具定义只发一次，你的手算会算多次 |
| 多模态 blocks | 图片按像素计费，不是按 base64 字符数 |
| thinking blocks | API 可能不把 thinking 计入输入（取决于配置） |

### 3.2 两种思路

**思路 A：累加（错误）**

```python
# 记录每条消息的 tokens，像银行记账
total = 0
for msg in messages:
    total += estimate_tokens(msg)  # 每次都重新估算
```

问题：误差会累积。第 100 轮时，估算误差可能达到几千 tokens。

**思路 B：实时窗口（正确）**

```python
# 直接问 API："你上次收到了多少 tokens？"
last_input_tokens = last_api_response.usage.input_tokens

# 只估算"上次调用之后新增的消息"
new_messages = messages[last_message_index + 1:]
estimated_new = rough_estimate(new_messages)

current_context_size = last_input_tokens + estimated_new
```

问题：需要把 API 的 `usage` 信息存下来，并在下一次估算时可用。

### 3.3 Claude Code 的选择

Claude Code 用的是**思路 B（实时窗口）**，并且做了三层 fallback：

1. **优先**：用上次 API 响应的 `usage.input_tokens`（最准确）
2. **降级**：调一次 Haiku 模型拿 `usage`（较准确，但花钱）
3. **兜底**：字符级粗略估算（不准确，但免费）

---

## 4. Claude Code 的三层 Token 估算架构

> 源码位置：`claude-code-analysis/src/services/tokenEstimation.ts` + `tokens.ts`

### 4.1 L1：精确计算（`countTokensWithAPI`）

```typescript
export async function countTokensWithAPI(
  messages: MessageParam[],
  tools: ToolUnion[],
): Promise<number | null> {
  const response = await anthropic.beta.messages.countTokens({
    model,
    messages,
    tools,
    betas,
    ...(containsThinking && {
      thinking: { type: "enabled", budget_tokens: 1024 }
    }),
  })
  return response.input_tokens
}
```

**特点**：
- 调 Anthropic SDK 专用接口，**不消耗输出 tokens**（只计数，不生成）
- 支持 Bedrock / Vertex / thinking blocks
- 包装了 `withTokenCountVCR()` 录播缓存（相同 messages 不重复计费）

**什么时候用**：压缩前精确计算（需要准确数字来决定是否触发压缩）。

### 4.2 L2：Haiku fallback（`countTokensViaHaikuFallback`）

```typescript
export async function countTokensViaHaikuFallback(
  messages: MessageParam[],
  tools: ToolUnion[],
): Promise<number | null> {
  const model = isVertexGlobalEndpoint || isBedrockWithThinking
    ? getDefaultSonnetModel()   // 特殊情况用 Sonnet
    : getSmallFastModel()       // 默认用 Haiku（便宜）

  const response = await anthropic.beta.messages.create({
    model,
    max_tokens: containsThinking ? 2048 : 1,  // 不需要输出
    messages,
    tools,
  })

  return inputTokens + cacheCreationTokens + cacheReadTokens
}
```

**特点**：
- 用一次完整的 `messages.create()` 拿 `usage`
- Haiku 便宜（约 Claude Sonnet 的 1/20 成本）
- 特殊情况（Vertex global / Bedrock+thinking）降级用 Sonnet

**什么时候用**：L1 失败（比如网络错误、API 不支持 countTokens）。

### 4.3 L3：粗略估算（`roughTokenCountEstimation`）

```typescript
export function roughTokenCountEstimation(
  content: string,
  bytesPerToken: number = 4,
): number {
  return Math.round(content.length / bytesPerToken)
}

export function bytesPerTokenForFileType(fileExtension: string): number {
  switch (fileExtension) {
    case 'json':
    case 'jsonl':
    case 'jsonc':
      return 2  // JSON 的 {}[]:, 单字符 token 密度高
    default:
      return 4
  }
}
```

对各类 content block 分别处理：

| Block 类型 | 估算方式 |
|---|---|
| `text` | `roughTokenCountEstimation(block.text)`（默认 4 字符/token） |
| `image` / `document` | **固定返回 2000**（与 API 实际收费对齐） |
| `tool_use` | `roughTokenCountEstimation(name + JSON.stringify(input))` |
| `thinking` | `roughTokenCountEstimation(block.thinking)` |
| `tool_result` | 递归估算 content（可能是 text/image 混合） |

**什么时候用**：高频调用场景（比如每轮都检查阈值），不能每次都调 API。

### 4.4 核心函数：`tokenCountWithEstimation()`

这是 Claude Code **实际用来检查阈值**的函数：

```typescript
export function tokenCountWithEstimation(
  messages: readonly Message[],
): number {
  // 1. 找到最后一条有 usage 的 assistant message
  let i = messages.length - 1
  while (i >= 0) {
    const usage = getTokenUsage(messages[i])
    if (usage) {
      // 2. 处理并行工具调用的 split records
      const responseId = getAssistantMessageId(messages[i])
      if (responseId) {
        let j = i - 1
        while (j >= 0) {
          if (getAssistantMessageId(messages[j]) === responseId) {
            i = j  // 锚点到第一条 split
          }
          j--
        }
      }

      // 3. 基准 + 基准之后的新消息粗略估算
      return (
        getTokenCountFromUsage(usage) +
        roughTokenCountEstimationForMessages(messages.slice(i + 1))
      )
    }
    i--
  }

  // 4. 完全没找到 usage → 全部粗略估算
  return roughTokenCountEstimationForMessages(messages)
}
```

**核心思想**：

```
当前上下文大小 ≈
  上次 API 响应的 input_tokens（权威数字）
  + 上次响应之后新增消息的粗略估算（这些还没经过 API）
```

**为什么不用累加？**

因为 context window 是"当前 API 请求的实际 token 数"，不是"历史所有消息的 token 和"——缓存命中（`cache_read_input_tokens`）不计入预算检查，你的累加会算多。

---

## 5. agent-dev 的 Token 估算现状

### 5.1 当前实现：`SimpleTokenCounter`

> 源码位置：`agent_core/context/tokenizer.py`

```python
class SimpleTokenCounter:
    CHINESE_RATIO = 1.4   # 中文 tokens/字
    ENGLISH_RATIO = 0.25  # 英文 tokens/字符
    ROLE_OVERHEAD = 10    # 每条消息固定开销
    TOOL_CALL_FIXED = 50  # tool_use block 固定开销
    TOOL_RESULT_FIXED = 20  # tool_result block 固定开销

    def count_messages(self, messages: list[dict]) -> int:
        total = 0
        for msg in messages:
            total += self.ROLE_OVERHEAD
            if isinstance(content, str):
                total += self.count(content)
            elif isinstance(content, list):
                for block in content:
                    if block.type == "tool_use":
                        total += self.TOOL_CALL_FIXED
                        total += self.count(json.dumps(block.input))
                    elif block.type == "tool_result":
                        total += self.TOOL_RESULT_FIXED
                        total += self.count(block.content)
                    # ...
        return total
```

**特点**：
- 每次检查阈值时，**全量遍历所有消息**
- 用字符比例估算（中文 1.4t/字，英文 0.25t/字符）
- 没有用 API 响应的 `usage` 信息

### 5.2 与 Claude Code 的对比

| 维度 | Claude Code | agent-dev |
|---|---|---|
| API usage 捕获 | ✅ `app.py` 捕获了 `usage` | ✅ `app.py` 捕获了 `usage` |
| API usage 回传 budget manager | ✅ | ❌（只用于 UI 展示） |
| 增量估算 | ✅ 基准 + 新消息 | ❌ 每次全量 |
| 精确计数 API | ✅ `countTokensWithAPI()` | ❌ |
| 粗略估算 | ✅ 字符级 | ✅ 字符级（中文 1.4t/字） |

### 5.3 为什么当前实现"够用"？

1. **压缩触发不频繁**：只有逼近阈值时才触发，估算误差 ±20% 不会导致误触发
2. **GLM-5.1 上下文窗口较小**（128K），压缩触发更频繁，但误差影响不大
3. **实现简单**：不需要维护"上次 usage"状态，不需要处理缓存逻辑

---

## 6. 增量估算：用 API usage 作基准的核心思想

### 6.1 为什么需要增量估算？

当前 agent-dev 的问题是：**每次检查阈值都要全量遍历所有消息**。

```
第1轮：遍历 2 条消息   → 快
第10轮：遍历 20 条消息  → 快
第100轮：遍历 200 条消息 → 慢（200 条 * 每条估算 = 可观开销）
第1000轮：遍历 2000 条消息 → 更慢
```

如果用了增量估算，无论第几轮，**基准部分是 O(1)**（直接用上次 API 数字），只有新增部分是 O(N)（N = 上次调用之后新增的消息数，通常很小）。

### 6.2 增量估算的时间线

```
第 N-1 轮结束：
  LLM 最终调用完成
  → usage.input_tokens = 8,200
  → 记录：_baseline_tokens = 8,200
  → 记录：_baseline_message_index = 最后一条 assistant 消息的索引

第 N 轮开始（用户发了一条新消息）：
  self.messages = [
    ...压缩后的历史（已被 _baseline 覆盖）,
    第 N-1 轮 assistant 回复,
    用户新消息,
  ]
  → 需要估算的部分 = 第 N-1 轮 assistant 回复 + 用户新消息
  → current ≈ _baseline_tokens + rough_estimate(新增部分)
```

### 6.3 实现增量估算需要改什么？

**`ContextBudgetManager` 需要新增**：

```python
class ContextBudgetManager:
    def __init__(self, ...):
        # ...现有字段...
        self._baseline_tokens: int = 0          # 上次 API 响应的 input_tokens
        self._baseline_message_count: int = 0   # 上次 API 响应时的消息数

    def set_baseline(self, usage: UsageStats) -> None:
        """从第 N-1 轮 LLM 响应中捕获 usage，设置基准"""
        self._baseline_tokens = usage.input_tokens
        self._baseline_message_count = len(self.messages)  # 需要外部传入

    def _check_budget_incremental(self, messages: list[dict]) -> tuple[bool, str]:
        """增量估算当前上下文大小"""
        new_messages = messages[self._baseline_message_count:]
        estimated_new = self.token_counter.count_messages(new_messages)

        current = self._baseline_tokens + estimated_new
        # ...后续判断逻辑...
```

**`app.py` 需要改**：

```python
# 在 Streamlit 主循环中，每次捕获到 usage 时：
if msg_type == "usage":
    stats["input"] += content.input_tokens
    stats["output"] += content.output_tokens
    # 新增：回传给 context manager
    agent.context_manager.set_baseline(content.input_tokens, len(agent.messages))
```

### 6.4 边界情况

| 情况 | 处理方式 |
|---|---|
| 第一次调用（没有 baseline） | 降级为全量估算 |
| 压缩发生后（消息列表被改写） | 重置 baseline（因为消息列表变了，之前的 baseline 无效） |
| API 调用失败（没有 usage） | 保持上一次 baseline，或用全量估算 |
| 切换 session | 重置 baseline（不同 session 的 usage 不能共用） |

---

## 7. 对比总结与优化建议

### 7.1 Token 估算方法对比

| 方法 | 准确度 | 速度 | 成本 | 适用场景 |
|---|---|---|---|---|
| **全量粗略估算**（agent-dev 当前） | ±20% | 慢（O(N)） | 免费 | 开发阶段、消息量少 |
| **增量估算**（Claude Code） | ±5% | 快（O(1)+O(ΔN)） | 免费 | 生产环境、消息量多 |
| **精确 API 计数**（L1） | ±0% | 慢（API 调用） | 有成本 | 压缩前精确判断 |
| **Haiku fallback**（L2） | ±1% | 慢（API 调用） | 低成本 | L1 失败时的降级 |

### 7.2 agent-dev 优化建议

**优先级 P0：捕获并回传 API usage**

```python
# agent_core.py 的 run() 方法中，捕获 usage 后：
if chunk.usage:
    self._last_usage = chunk.usage
    if self.context_manager:
        self.context_manager.set_baseline(chunk.usage, len(self.messages))
```

**优先级 P1：实现增量估算**

参考 Claude Code 的 `tokenCountWithEstimation()`，在 `ContextBudgetManager` 中实现：
- `set_baseline(usage, message_count)`
- `_check_budget_incremental(messages)`

**优先级 P2：调 GLM API 的 countTokens 接口（如果有）**

GLM API 是否支持 `count_tokens` 端点需要验证。如果支持，可以实现 L1 精确计数。

**优先级 P3：预处理（脱水）增强**

参考 Claude Code 的 `stripImagesFromMessages()` 和 `stripReinjectedAttachments()`：
- 如果 agent-dev 未来支持图片，需要过滤 image/document blocks
- 如果实现了文件重附机制，需要防止文件内容雪崩

---

## 附录 A：压缩前后 Token 数变化示例

假设一个真实场景：

```
初始状态：
  - system prompt: 500 tokens
  - 历史消息: 21 条，共 107,500 tokens
  - 总计: 108,000 tokens（逼近 128K 上限）

压缩后：
  - system prompt: 500 tokens
  - summary: 500 tokens（原 21 条消息的摘要）
  - 最近 6 条消息: 5,000 tokens
  - 总计: 6,000 tokens（节省 102,000 tokens）

压缩效果：
  - Token 节省率: 94.4%
  - 信息保留: 摘要 + 最近 6 条（足够 LLM 继续对话）
```

---

## 附录 B：常见误区澄清

### 误区 1："压缩是把消息打包成 zip"

**错**。压缩是**用 LLM 生成摘要**，替换掉原始消息。不是无损压缩，而是**有损压缩**（丢失细节，保留核心信息）。

### 误区 2："压缩是在发送前临时做的"

**错**。压缩是**永久改写** `self.messages`。压缩后的消息列表会持久化到 JSONL，重启后仍然有效。

### 误区 3："Token 估算越精确越好"

**不完全对**。压缩触发阈值是 `context_window * 0.75`（75%），有 ±20% 误差不会导致误触发。过度追求精确反而增加复杂度和成本。Claude Code 用三层架构，是因为它的上下文窗口更大（200K），且面向用户生产环境，误差容忍度更低。

### 误区 4："API usage.input_tokens 就是全部历史消息的 token 和"

**错**。`usage.input_tokens` 是**当前请求的实际大小**，可能包含缓存命中（`cache_read_input_tokens`），这部分不计入计费，但你的手算会重复计算。这就是为什么 Claude Code 直接用 API 数字，而不是自己累加。

---

## 附录 C：参考资料

- Claude Code 源码：`claude-code-analysis/src/services/tokenEstimation.ts`
- Claude Code 源码：`claude-code-analysis/src/services/tokens.ts`
- Claude Code 源码：`claude-code-analysis/src/services/compact/compact.ts`
- agent-dev 实现：`agent_core/context/tokenizer.py`
- agent-dev 实现：`agent_core/context/budget.py`
- agent-dev 实现：`agent_core/context/compact.py`
- 设计文档：`docs/context-management-implementation-design.md`
