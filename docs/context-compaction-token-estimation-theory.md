# 上下文压缩与 Token 估算：原理深度解析

> 本文档从第一性原理出发，解释 Agent 系统中"上下文压缩"的本质，以及 Claude Code 和 agent-dev 各自的 Token 估算方法。是 `context-management-implementation-design.md` 的理论补充篇。

---

## 目录

1. [背景：为什么需要上下文压缩](#1-背景为什么需要上下文压缩)
2. [压缩的本质：不是"压缩文件"，而是"压缩历史"](#2-压缩的本质不是压缩文件而是压缩历史)
3. [Token 估算的根本问题：你永远不知道 API 收到了多少](#3-token-估算的根本问题你永远不知道-api-收到了多少)
4. [Claude Code 的三层 Token 估算架构](#4-claude-code-的三层-token-估算架构)
5. [agent-dev 的 Token 估算实现（已对齐 Claude Code）](#5-agent-dev-的-token-估算实现已对齐-claude-code)
6. [增量估算：API usage 基准的核心实现](#6-增量估算api-usage-基准的核心实现)
7. [对比总结与已完成的优化](#7-对比总结与已完成的优化)

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
| 缓存命中 | API 可能把 system prompt 等内容缓存在 KV-Cache 中，`cache_read_input_tokens` 是从缓存读取的部分。这部分**占据了 context window 空间**（占位），但**不计入计费账单**（免费）。你的手算会重复计算它，导致高估约 10-50%。高估不是问题，反而让压缩触发更保守。 |
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

## 5. agent-dev 的 Token 估算实现（已对齐 Claude Code）

> 源码位置：`agent_core/context/tokenizer.py` + `budget.py` + `manager.py` + `agent_core.py`
>
> **以下三个优化已在 2026-06-16 实现**，对应 commit `637b31f`，107/107 测试全过。

### 5.1 `SimpleTokenCounter`：粗略估算核心

**文件**：`agent_core/context/tokenizer.py`

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
            content = msg.get("content", "")

            if isinstance(content, str):
                total += self.count(content)          # 字符比例估算

            elif isinstance(content, list):
                for block in content:
                    block_type = block.get("type", "text")

                    if block_type == "text":
                        total += self.count(block.get("text", ""))

                    elif block_type == "tool_use":
                        total += self.TOOL_CALL_FIXED
                        total += self.count(json.dumps(block.get("input", {})))

                    elif block_type == "tool_result":
                        total += self.TOOL_RESULT_FIXED
                        total += self.count(block.get("content", ""))

                    elif block_type == "thinking":
                        pass                          # 不计入（占输出 token，不占输入）

                    elif block_type in ("image", "document"):
                        # 固定 2000 tokens（对齐 Claude Code）
                        # 之前用 str(block) 算 base64，会高估 10-50 倍
                        total += 2000

                    else:
                        total += self.count(str(block))  # 兜底
        return total
```

**关键特性**：
- `image`/`document` blocks 固定 2000 tokens（之前用 `str(block)` 算 base64，高估 10-50 倍）
- `thinking` block 不计入（API 层自动过滤，不占输入 token）
- 工具调用/结果：固定开销 + 内容粗估

### 5.2 `ContextBudgetManager`：增量估算 + 阈值判断

**文件**：`agent_core/context/budget.py`

核心是 `_estimate_used_tokens()` 方法，实现**增量优先、全量兜底**：

```python
class ContextBudgetManager:
    # 新增字段：增量基准（对齐 Claude Code tokenCountWithEstimation）
    _baseline_tokens: int = 0      # 上次 API 响应的 input_tokens
    _baseline_msg_count: int = 0    # 上次 API 响应时的消息数
    _baseline_valid: bool = False   # 基准是否有效

    def set_baseline(self, input_tokens: int, message_count: int) -> None:
        if input_tokens <= 0:
            return
        self._baseline_tokens = input_tokens
        self._baseline_msg_count = message_count
        self._baseline_valid = True

    def invalidate_baseline(self) -> None:
        # 压缩/session切换后，基准失效
        self._baseline_valid = False

    def _estimate_used_tokens(self, messages: list[dict]) -> int:
        msg_count = len(messages)

        # 增量路径：三个条件全满足
        if (
            self._baseline_valid
            and self._baseline_msg_count > 0
            and self._baseline_msg_count <= msg_count
        ):
            new_messages = messages[self._baseline_msg_count:]
            if new_messages:
                estimated_new = self.token_counter.count_messages(new_messages)
                return self._baseline_tokens + estimated_new
            else:
                # 无新增 → 直接用 API 数字（比粗略估算更准）
                return self._baseline_tokens

        # 全量兜底：首次启动 / 压缩后 / session 切换
        return self.token_counter.count_messages(messages)
```

**增量估算的直觉**：

```
第 N-1 轮结束时：
    _baseline_tokens = 48000   （API 说"这轮收到了 48000 tokens"）
    _baseline_msg_count = 12   （当时有 12 条消息）

第 N 轮开始：
    messages = [12条旧消息, assistant回复, 用户新消息] = 15 条
    new_messages = messages[12:] = [assistant回复, 用户新消息] = 3 条
    result = 48000 + count_messages(3条新增)
```

### 5.3 `agent_core.py`：usage 捕获并回传

**文件**：`agent_core/agent_core.py` 第 386-391 行

```python
for chunk in llm_stream:
    # ... 文本/thinking/tool_call 处理 ...

    # Token 消耗 → 转发给 UI + 回传给 context manager
    if chunk.usage:
        yield ("usage", chunk.usage)          # → UI 面板展示

        # 对齐 Claude Code tokenCountWithEstimation
        if self.context_manager:
            self.context_manager.set_baseline(
                chunk.usage.input_tokens,      # API 权威数字
                len(self.messages),             # 当前消息数
            )
```

**双重用途**：`chunk.usage` 同时驱动两个消费者：
1. `yield ("usage", chunk.usage)` → Streamlit UI Token 面板
2. `set_baseline(...)` → ContextBudgetManager 增量基准

### 5.4 `manager.py`：压缩成功后自动失效

**文件**：`agent_core/context/manager.py`

```python
def check_and_compact(self, messages: list[dict]) -> CompactionResult | None:
    result = self.budget.should_compact(messages)

    if result.trigger_compact:
        compaction_result = self.compactor.compact(...)
        if compaction_result.success:
            # 压缩改写了消息列表，旧基准失效
            self.budget.invalidate_baseline()
            self.budget.record_compaction(...)
```

**为什么压缩后必须失效？**

```
压缩前：12 条消息 → API input_tokens = 48000
压缩后：[system, summary, 最新6条] = 8 条

_baseline_msg_count = 12 > len(messages) = 8
→ 条件 _baseline_msg_count <= msg_count 不满足
→ 自动走全量路径 → 重建基准
```

这个机制**不需要显式判断"是否刚压缩过"**，条件检查自动触发降级。

### 5.5 对比表（优化后）

| 维度 | Claude Code | agent-dev |
|---|---|---|
| API usage 捕获 | ✅ | ✅ `agent_core.py` |
| API usage 回传 budget manager | ✅ | ✅ `set_baseline()` |
| 增量估算 | ✅ 基准 + 新消息 | ✅ `_estimate_used_tokens()` |
| 全量兜底 | ✅ 无基准时 | ✅ `_baseline_valid=False` 时 |
| image/document 固定 2000 | ✅ | ✅ `tokenizer.py` |
| thinking 不计入 | ✅ | ✅ `tokenizer.py` |
| 压缩后基准失效 | ✅ | ✅ `invalidate_baseline()` |
| L1 精确计数 API | ✅ `countTokensWithAPI()` | ❌（GLM 无此接口）|
## 6. 增量估算：API usage 基准的核心实现

### 6.1 为什么需要增量估算？

全量遍历的问题：**每次检查阈值都要遍历所有消息**。

```
第1轮：遍历 2 条消息     → 快
第10轮：遍历 20 条消息   → 快
第100轮：遍历 200 条消息  → 可观开销
第1000轮：遍历 2000 条消息 → 显著延迟
```

增量估算的改进：**基准部分是 O(1)**（直接用上次 API 数字），只有新增部分是 O(ΔN)（新增消息数，通常很小）。

### 6.2 增量估算的时间线

```
第 N-1 轮结束：
  LLM API 响应完成
  → usage.input_tokens = 48000
  → set_baseline(48000, msg_count=12)

第 N 轮开始（用户发了新消息）：
  messages = [12条旧消息, assistant回复, 用户新消息] = 14 条
  new_messages = messages[12:] = [assistant回复, 用户新消息]
  current ≈ 48000 + count_messages(2条新增)
```

### 6.3 四个核心字段

| 字段 | 类型 | 含义 |
|---|---|---|
| `_baseline_tokens` | `int` | 上次 API 响应的 `input_tokens` |
| `_baseline_msg_count` | `int` | 捕获基准时的消息条数 |
| `_baseline_valid` | `bool` | 基准是否有效 |
| `consecutive_failures` | `int` | 熔断计数器 |

### 6.4 基准失效时机

| 事件 | 是否失效 | 原因 |
|---|---|---|
| **压缩成功** | ✅ `invalidate_baseline()` | 消息列表被改写（旧消息换成 summary） |
| **Session 切换** | ✅ 设计已预留 | 不同 session 的 usage 不能共用 |
| **API 调用失败** | ❌ 不调 | 消息列表没变，旧基准仍有效 |
| **第一次启动** | ❌（初始就是 False） | 还没调用过 LLM，没有基准 |

### 6.5 增量 vs 全量：完整时序

```
Session 启动
    │
    ├─ 第1轮检查 ──────────────────────────────────────────→ 全量（无基准）
    │                                                          │
    ├─ 第1轮 LLM 响应 ──→ set_baseline(input_tokens, msg_count)
    │                                                          │
    ├─ 第2轮检查 ──────────────────────────────────────────→ 增量
    │   (baseline=48K + count(new_msgs))                       │
    │                                                          │
    ├─ 第N轮 LLM 响应 ──→ set_baseline(new_tokens, new_count)
    │                                                          │
    │    ... 增量若干轮 ...
    │                                                          │
    ├─ 触发压缩 ─────────→ invalidate_baseline()
    │                                                          │
    ├─ 压缩后第1轮检查 ───────────────────────────────────→ 全量（基准已失效）
    │   (消息列表被改写了，旧的 msg_count 对不上)               │
    │                                                          │
    └─ 压缩后 LLM 响应 ──→ set_baseline(new_tokens, new_count) → 重建基准
                                                              │
                                                  下一批增量轮次...
```

### 6.6 为什么压缩后必须失效？

**关键**：`set_baseline` 捕获的是"当时那条消息列表"发给 API 后，API 说"我收到了多少 tokens"。

压缩改变了消息列表的结构：
- 压缩前：12 条原始消息 → API 说 48000 tokens
- 压缩后：8 条消息（system + summary + 6条）→ API 会说完全不同的数字

```
_baseline_msg_count = 12（压缩前）
len(messages) = 8（压缩后）

12 > 8 → 条件 _baseline_msg_count <= msg_count 不满足
→ 自动走全量路径 → 重新建立基准
```

这个机制**不需要显式判断"是否刚压缩过"**——条件检查自动降级。
## 7. 对比总结与已完成的优化

### 7.1 Token 估算方法对比

| 方法 | 准确度 | 速度 | 成本 | agent-dev 状态 |
|---|---|---|---|---|
| **全量粗略估算** | ±20% | 慢（O(N)） | 免费 | ✅ 兜底路径 |
| **增量估算**（Claude Code） | 基准±0%，新增±20% | 快（O(1)+O(ΔN)） | 免费 | ✅ 已实现 |
| **精确 API 计数**（L1） | ±0% | 慢（API 调用） | 有成本 | ❌ GLM 无此接口 |
| **Haiku fallback**（L2） | ±1% | 慢（API 调用） | 低成本 | ❌ 不需要 |

### 7.2 agent-dev 已完成的优化（commit `637b31f`）

| 优化 | 文件 | 核心改动 | 对齐 Claude Code |
|---|---|---|---|
| **P0: API usage 回传** | `agent_core.py` | `chunk.usage.input_tokens` → `set_baseline()` | ✅ |
| **P1: 增量估算** | `budget.py` | `set_baseline()` + `_estimate_used_tokens()` | ✅ |
| **P1: 增量兜底** | `budget.py` | 基准失效 → 全量估算 | ✅ |
| **P1: 压缩后失效** | `manager.py` | 压缩成功 → `invalidate_baseline()` | ✅ |
| **P2: image/doc 固定 2000** | `tokenizer.py` | image/document blocks → 固定 2000 tokens | ✅ |
| **P2: thinking 不计入** | `tokenizer.py` | thinking block → `+0` | ✅ |

### 7.3 仍需关注的优化方向

| 优化 | 状态 | 说明 |
|---|---|---|
| **L1 精确计数** | ❌ 待探索 | GLM API 是否有 `count_tokens` 端点待验证 |
| **image/document 脱水** | 🔶 部分 | `tokenizer.py` 已固定 2000，`compact.py` 的 `_preprocess()` 已替换占位符 |
| **文件重附机制** | ❌ 暂无 | agent-dev 未实现，无需 `stripReinjectedAttachments` |

### 7.4 测试覆盖

新增 **9 个测试**（`agent_core/context/test_context.py`），107/107 全过：

```
TestIncrementalEstimation（6个）
├── test_budget_set_baseline_incremental        # 增量路径正确
├── test_budget_set_baseline_full_fallback       # 基准无效时全量
├── test_budget_invalidate_baseline              # 失效后走全量
├── test_budget_invalidate_after_compaction       # 压缩后失效（自动）
├── test_budget_set_baseline_zero_ignored         # input_tokens<=0 忽略
└── test_budget_incremental_no_new_messages      # 无新增时直接用基准

TestImageDocumentTokenEstimation（3个）
├── test_image_block_2000_tokens                 # image 固定 2000
├── test_document_block_2000_tokens              # document 固定 2000
└── test_plain_text_unaffected                   # 纯文本不受影响
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

### 误区 4：直接用手算 token 数累加来判断 context 压力

**错**。手算 token 数有两个根本性问题：

**① 缓存命中导致高估**：`cache_read_input_tokens` 是从 KV-Cache 读取的部分，它已经占据了 context window 空间（占位），但 API 不收钱（免费）。你用手算会把这部分重复计算，导致高估 10-50%。

> **关键区分**：`cache_read_input_tokens` 在三个维度有不同的含义：
> - **context window 空间**：✅ 算占用（50K + 130K = 180K 都占空间）
> - **计算成本**：❌ 不算（50K 不需要 GPU 重新算）
> - **计费账单**：❌ 不算（免费午餐）

所以高估不是 bug，是安全余量。Claude Code 直接用 API 返回的 `input_tokens` 数字，而不是自己累加，就是避免这个问题。

**② 累加 vs 实时窗口**：即使不考虑缓存，手算也是"历史所有消息的 token 和"，而 context window 是"当前这一次 API 请求的大小"。两者不是同一个东西。

---

## 附录 C：参考资料

- Claude Code 源码：`claude-code-analysis/src/services/tokenEstimation.ts`
- Claude Code 源码：`claude-code-analysis/src/services/tokens.ts`
- Claude Code 源码：`claude-code-analysis/src/services/compact/compact.ts`
- agent-dev 实现：`agent_core/context/tokenizer.py`
- agent-dev 实现：`agent_core/context/budget.py`
- agent-dev 实现：`agent_core/context/compact.py`
- 设计文档：`docs/context-management-implementation-design.md`
