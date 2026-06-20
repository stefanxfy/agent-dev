# 上下文压缩与 Token 估算:原理深度解析

> 本文档从第一性原理出发,解释 Agent 系统中"上下文压缩"的本质,以及 Claude Code 和 agent-dev 各自的 Token 估算方法。是 `context-management-implementation-design.md` 的理论补充篇。

---

## 目录

1. [背景:为什么需要上下文压缩](#1-背景为什么需要上下文压缩)
2. [压缩的本质:不是"压缩文件",而是"压缩历史"](#2-压缩的本质不是压缩文件而是压缩历史)
3. [Token 估算的根本问题:你永远不知道 API 收到了多少](#3-token-估算的根本问题你永远不知道-api-收到了多少)
4. [Claude Code 的三层 Token 估算架构](#4-claude-code-的三层-token-估算架构)
5. [agent-dev 的 Token 估算实现(已对齐 Claude Code)](#5-agent-dev-的-token-估算实现已对齐-claude-code)
6. [增量估算:API usage 基准的核心实现](#6-增量估算api-usage-基准的核心实现)
7. [对比总结与已完成的优化](#7-对比总结与已完成的优化)

---

## 1. 背景:为什么需要上下文压缩

### 1.1 问题定义

LLM 有固定的上下文窗口(Context Window),比如:

| 模型 | Context Window |
|---|---|
| Claude Sonnet 4 | 200K tokens |
| GLM-5.1 | 128K tokens |
| GPT-4o | 128K tokens |

一个持续对话的 Agent,**每轮都会把之前的所有消息带上**,所以上下文会持续增长:

```
第1轮:system(500t) + user_msg(100t)           = 600t
第2轮:system(500t) + user_msg(100t) + asst(200t) + user_msg(100t) = 900t
第3轮:...                                     = 1200t
...
第N轮:接近 200K,触发窗口上限 → LLM 拒绝响应
```

### 1.2 解决方案:压缩历史

在上下文逼近上限之前,把"中间的历史消息"替换成一个简短的 summary,腾出空间:

```
压缩前(22条,108K tokens):
  [system] + [msg1] + [msg2] + ... + [msg21] + [当前用户消息]

压缩后(8条,6K tokens):
  [system] + [summary of msg1..msg21] + [msg16..msg21] + [当前用户消息]
```

**关键**:压缩不是删除--是**用更少的 tokens 保留同等的信息密度**。

---

## 2. 压缩的本质:不是"压缩文件",而是"压缩历史"

### 2.1 常见误解

> ❌ "压缩就是把消息打包成 zip"
>
> ❌ "压缩是在发送前把上下文压缩一下"
>
> ❌ "压缩的是下一次窗口的上下文"

### 2.2 准确的说法

**压缩改写的,是"消息列表"这个数据结构**。

在 Agent 内部,`self.messages` 是一个 Python list,每条消息是一个 dict:

```python
self.messages = [
    {"role": "system", "content": "You are a helpful assistant..."},
    {"role": "user", "content": "帮我查一下天气"},
    {"role": "assistant", "content": "好的,你想查哪个城市?"},
    {"role": "user", "content": "深圳"},
    {"role": "assistant", "content": "..."},  # tool_use: search_weather
    ...
]
```

**压缩做的事**:把 `self.messages` 的中间部分(旧消息)替换成 summary,让 list 变短。

```python
# 压缩前:self.messages 有 22 条
self.messages = [msg0, msg1, msg2, ..., msg21]

# 压缩后:self.messages 变成 8 条
self.messages = [
    msg0,                                          # system prompt
    {"role": "user", "content": "[Previous conversation summarized]\n\n..."},  # summary
    msg16, msg17, msg18, msg19, msg20, msg21,      # 最近 6 条
]
```

**下一轮 LLM 调用时**,发给 API 的就是压缩后的 `self.messages`--更短,但信息密度更高。

### 2.3 压缩的时间点

```
用户消息到达
    ↓
self.messages.append(user_message)
    ↓
触发检查:_check_budget() → used_tokens > compact_threshold?
    ↓
是 → 调 compact(messages) → 返回 compressed_messages
    ↓
self.messages = compressed_messages   ← 改写消息列表
    ↓
继续这一轮的 LLM 调用(用压缩后的 messages)
```

**所以压缩发生在"这一轮对话进行过程中",压缩的对象是"这一轮之前的历史消息"**。

---

## 3. Token 估算的根本问题:你永远不知道 API 收到了多少

### 3.1 问题

要触发压缩,你需要知道"当前上下文有多大"。但:

**你手算的 token 数 ≠ API 实际收到的 token 数**

原因:

| 原因 | 说明 |
|---|---|
| 缓存命中 | API 可能把 system prompt 等内容缓存在 KV-Cache 中,`cache_read_input_tokens` 是从缓存读取的部分。这部分**占据了 context window 空间**(占位),但**不计入计费账单**(免费)。你的手算会重复计算它,导致高估约 10-50%。高估不是问题,反而让压缩触发更保守。 |
| 消息去重 | 同名工具定义只发一次,你的手算会算多次 |
| 多模态 blocks | 图片按像素计费,不是按 base64 字符数 |
| thinking blocks | API 可能不把 thinking 计入输入(取决于配置) |

### 3.2 两种思路

**思路 A:累加(错误)**

```python
# 记录每条消息的 tokens,像银行记账
total = 0
for msg in messages:
    total += estimate_tokens(msg)  # 每次都重新估算
```

问题:误差会累积。第 100 轮时,估算误差可能达到几千 tokens。

**思路 B:实时窗口(正确)**

```python
# 直接问 API:"你上次收到了多少 tokens?"
last_input_tokens = last_api_response.usage.input_tokens

# 只估算"上次调用之后新增的消息"
new_messages = messages[last_message_index + 1:]
estimated_new = rough_estimate(new_messages)

current_context_size = last_input_tokens + estimated_new
```

问题:需要把 API 的 `usage` 信息存下来,并在下一次估算时可用。

### 3.3 Claude Code 的选择

Claude Code 用的是**思路 B(实时窗口)**,并且做了三层 fallback:

1. **优先**:用上次 API 响应的 `usage.input_tokens`(最准确)
2. **降级**:调一次 Haiku 模型拿 `usage`(较准确,但花钱)
3. **兜底**:字符级粗略估算(不准确,但免费)

---

## 4. Claude Code 的三层 Token 估算架构

> 源码位置:`claude-code-analysis/src/services/tokenEstimation.ts` + `tokens.ts`

### 4.1 L1:精确计算(`countTokensWithAPI`)

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

**特点**:
- 调 Anthropic SDK 专用接口,**不消耗输出 tokens**(只计数,不生成)
- 支持 Bedrock / Vertex / thinking blocks
- 包装了 `withTokenCountVCR()` 录播缓存(相同 messages 不重复计费)

**什么时候用**:压缩前精确计算(需要准确数字来决定是否触发压缩)。

### 4.2 L2:Haiku fallback(`countTokensViaHaikuFallback`)

```typescript
export async function countTokensViaHaikuFallback(
  messages: MessageParam[],
  tools: ToolUnion[],
): Promise<number | null> {
  const model = isVertexGlobalEndpoint || isBedrockWithThinking
    ? getDefaultSonnetModel()   // 特殊情况用 Sonnet
    : getSmallFastModel()       // 默认用 Haiku(便宜)

  const response = await anthropic.beta.messages.create({
    model,
    max_tokens: containsThinking ? 2048 : 1,  // 不需要输出
    messages,
    tools,
  })

  return inputTokens + cacheCreationTokens + cacheReadTokens
}
```

**特点**:
- 用一次完整的 `messages.create()` 拿 `usage`
- Haiku 便宜(约 Claude Sonnet 的 1/20 成本)
- 特殊情况(Vertex global / Bedrock+thinking)降级用 Sonnet

**什么时候用**:L1 失败(比如网络错误、API 不支持 countTokens)。

### 4.3 L3:粗略估算(`roughTokenCountEstimation`)

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

对各类 content block 分别处理:

| Block 类型 | 估算方式 |
|---|---|
| `text` | `roughTokenCountEstimation(block.text)`(默认 4 字符/token) |
| `image` / `document` | **固定返回 2000**(与 API 实际收费对齐) |
| `tool_use` | `roughTokenCountEstimation(name + JSON.stringify(input))` |
| `thinking` | `roughTokenCountEstimation(block.thinking)` |
| `tool_result` | 递归估算 content(可能是 text/image 混合) |

**什么时候用**:高频调用场景(比如每轮都检查阈值),不能每次都调 API。

### 4.4 核心函数:`tokenCountWithEstimation()`

这是 Claude Code **实际用来检查阈值**的函数:

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

**核心思想**:

```
当前上下文大小 ≈
  上次 API 响应的 input_tokens(权威数字)
  + 上次响应之后新增消息的粗略估算(这些还没经过 API)
```

**为什么不用累加?**

因为 context window 是"当前 API 请求的实际 token 数",不是"历史所有消息的 token 和"--缓存命中(`cache_read_input_tokens`)不计入预算检查,你的累加会算多。

---

## 5. agent-dev 的 Token 估算实现(已对齐 Claude Code)

> 源码位置:`agent_core/context/tokenizer.py` + `budget.py` + `manager.py` + `agent_core.py`
>
> **以下三个优化已在 2026-06-16 实现**,对应 commit `637b31f`,107/107 测试全过。

### 5.1 `SimpleTokenCounter`（v2 — tiktoken 精确优先，启发式回退）

**文件**：`agent_core/context/tokenizer.py`

> **2026-06-18 升级至 v2**：集成 tiktoken o200k_base 精确计数（主路径），启发式扩展为 5 类字符独立统计。中文偏差从 +210~290% 降至 ±15%。

```python
class SimpleTokenCounter:
    ROLE_OVERHEAD = 5       # GLM API 实测校准（原 10）
    TOOL_CALL_FIXED = 50
    TOOL_RESULT_FIXED = 20

    # 启发式回退常量（GLM-4 delta method 实测校准 2026-06-18）
    _FALLBACK_CHINESE_RATIO = 0.45   # 中文（原 1.4，+210~290%）
    _FALLBACK_ENGLISH_RATIO = 0.22   # 英文（原 0.25）
    _FALLBACK_CODE_RATIO = 0.33      # 代码（新增，原走英文低估 -35%）
    _FALLBACK_DIGITS_RATIO = 0.45    # 数字（新增，原走英文低估 -61%）

    def __init__(self, model: str | None = None):
        self._encoder = self._init_tiktoken()  # None if unavailable

    def count(self, text: str) -> int:
        if not text:
            return 0
        if self._encoder is not None:
            try:
                return len(self._encoder.encode(text))  # 精确计数
            except Exception:
                pass
        return self._heuristic_count(text)              # 回退

    def _heuristic_count(self, text: str) -> int:
        """5 类字符独立统计"""
        chinese = len(re.findall(r'[\u4e00-\u9fff]', text))
        code    = len(re.findall(r'[{}\[\]()\s#_=:;,.+/*\\|\'"<>@&^-]', text))
        digits  = len(re.findall(r'\d', text))
        english = len(re.findall(r'[a-zA-Z]', text))
        matched = chinese + code + digits + english
        other   = max(0, len(text) - matched)

        return int(
            chinese * self._FALLBACK_CHINESE_RATIO +
            english * self._FALLBACK_ENGLISH_RATIO +
            code    * self._FALLBACK_CODE_RATIO +
            digits  * self._FALLBACK_DIGITS_RATIO +
            other   * self._FALLBACK_ENGLISH_RATIO
        )
```

**关键特性**：
- **tiktoken 优先**：`count()` 优先用 `self._encoder.encode(text)` 精确计数（o200k_base 与 GLM tokenizer 高度一致）
- **启发式降级 5 类**：中文/英文/代码/数字/其他，全部基于 GLM-4 delta method 实测校准
- `image`/`document` blocks 固定 2000 tokens
- `thinking` block 不计入（API 层自动过滤）
- `ROLE_OVERHEAD` 从 10 降至 5（GLM API 实测）

**校准方法**：

GLM-4-flash delta method —— 发送单条 controlled message，API 返回 `prompt_tokens`，扣除 system token 得到纯内容 token 数，除以字符数得到实测比率。

**偏差对比**：

| 文本类型 | GLM 实测(tok/char) | 旧启发式(v1) | 新 tiktoken(v2) | 新启发式(回退) |
|---------|-------------------|-------------|----------------|---------------|
| 中文对话 | 0.45 | +236% | +27% | ±15% |
| 中文技术 | 0.36 | +290% | +40% | +25% |
| 英文日常 | 0.25 | ~0% | <5% | -12% |
| 英文技术 | 0.15 | +67% | <5% | +47% |
| Python 代码 | 0.33 | -24% | <5% | ±0% |
| JSON 数据 | 0.34 | -26% | <5% | -3% |
| 变长数字串 | 0.56 | -61% | <5% | -20% |
| 单字重复 | 0.33 | -24% | <5% | +36% |
- 工具调用/结果:固定开销 + 内容粗估

### 5.2 `ContextBudgetManager`:增量估算 + 阈值判断

**文件**:`agent_core/context/budget.py`

核心是 `_estimate_used_tokens()` 方法,实现**增量优先、全量兜底**:

```python
class ContextBudgetManager:
    # 新增字段:增量基准(对齐 Claude Code tokenCountWithEstimation)
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
        # 压缩/session切换后,基准失效
        self._baseline_valid = False

    def _estimate_used_tokens(self, messages: list[dict]) -> int:
        msg_count = len(messages)

        # 增量路径:三个条件全满足
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
                # 无新增 → 直接用 API 数字(比粗略估算更准)
                return self._baseline_tokens

        # 全量兜底:首次启动 / 压缩后 / session 切换
        return self.token_counter.count_messages(messages)
```

**增量估算的直觉**:

```
第 N-1 轮结束时:
    _baseline_tokens = 48000   (API 说"这轮收到了 48000 tokens")
    _baseline_msg_count = 12   (当时有 12 条消息)

第 N 轮开始:
    messages = [12条旧消息, assistant回复, 用户新消息] = 15 条
    new_messages = messages[12:] = [assistant回复, 用户新消息] = 3 条
    result = 48000 + count_messages(3条新增)
```

### 5.3 `agent_core.py`:usage 捕获并回传

**文件**:`agent_core/agent_core.py`

#### 流式捕获 + 持久化

```python
for chunk in llm_stream:
    # ... 文本/thinking/tool_call 处理 ...

    # Token 消耗 → 转发给 UI + 回传给 context manager
    if chunk.usage:
        self._last_turn_usage = chunk.usage    # 保存供持久化
        yield ("usage", chunk.usage)          # → UI 面板展示

        # 对齐 Claude Code tokenCountWithEstimation
        if self.context_manager:
            self.context_manager.set_baseline(
                chunk.usage.input_tokens,      # API 权威数字
                len(self.messages),             # 当前消息数
            )

# 流式结束后,持久化到 jsonl entry
self.storage.add_assistant_message(
    content=final_text,
    usage=asdict(self._last_turn_usage) if self._last_turn_usage else None,
)
```

#### Agent 重建时恢复 baseline

```python
def _restore_usage_baseline(self):
    """从 jsonl 最后一条带 usage 的 entry 恢复 baseline"""
    # Step 1: O(1) read_tail(64KB)
    tail_entries = storage.read_tail(kb=64)
    for entry in reversed(tail_entries):
        usage = entry.get("usage")
        if usage and usage.get("input_tokens"):
            # entry 是 assistant,它的 input_tokens 不含自己
            # 刷新后 self.messages 包含这条,所以 -1
            msg_count = len(self.messages) - 1
            self.context_manager.set_baseline(
                usage["input_tokens"],
                msg_count,
            )
            return

    # Step 2: Fallback O(n) - 极端场景(灌水 entry > 64KB)
    all_entries = storage.read_entries()
    # ... 同样逻辑 ...
```

**`len(messages) - 1` 的语义**:最后一条带 usage 的 jsonl entry 一定是 assistant 消息。它的 `input_tokens` 是 API 在 assistant append **之前**捕获的,不含 assistant 自己。刷新后 `self.messages` 包含这条 assistant,所以 `baseline_msg_count = len - 1`,增量估算会补上最后这 1 条 assistant 的 token。

**双重用途**:`chunk.usage` 同时驱动三个消费者:
1. `yield ("usage", chunk.usage)` → Streamlit UI Token 面板
2. `set_baseline(...)` → ContextBudgetManager 增量基准
3. `asdict(usage)` → JSONL entry 持久化(供 F5 刷新恢复)

### 5.4 `manager.py`:压缩成功后自动失效

**文件**:`agent_core/context/manager.py`

```python
def check_and_compact(self, messages: list[dict]) -> CompactionResult | None:
    result = self.budget.should_compact(messages)

    if result.trigger_compact:
        compaction_result = self.compactor.compact(...)
        if compaction_result.success:
            # 压缩改写了消息列表,旧基准失效
            self.budget.invalidate_baseline()
            self.budget.record_compaction(...)
```

**为什么压缩后必须失效?**

```
压缩前:12 条消息 → API input_tokens = 48000
压缩后:[system, summary, 最新6条] = 8 条

_baseline_msg_count = 12 > len(messages) = 8
→ 条件 _baseline_msg_count <= msg_count 不满足
→ 自动走全量路径 → 重建基准
```

这个机制**不需要显式判断"是否刚压缩过"**,条件检查自动触发降级。

### 5.5 对比表(优化后)

| 维度 | Claude Code | agent-dev |
|---|---|---|
| API usage 捕获 | ✅ | ✅ `agent_core.py` |
| API usage 回传 budget manager | ✅ | ✅ `set_baseline()` |
| 增量估算 | ✅ 基准 + 新消息 | ✅ `_estimate_used_tokens()` |
| 全量兜底 | ✅ 无基准时 | ✅ `_baseline_valid=False` 时 |
| image/document 固定 2000 | ✅ | ✅ `tokenizer.py` |
| thinking 不计入 | ✅ | ✅ `tokenizer.py` |
| 压缩后基准失效 | ✅ | ✅ `invalidate_baseline()` |
| L1 精确计数 API | ✅ `countTokensWithAPI()` | ❌(GLM 无此接口)|
## 6. 增量估算:API usage 基准的核心实现

### 6.1 为什么需要增量估算?

全量遍历的问题:**每次检查阈值都要遍历所有消息**。

```
第1轮:遍历 2 条消息     → 快
第10轮:遍历 20 条消息   → 快
第100轮:遍历 200 条消息  → 可观开销
第1000轮:遍历 2000 条消息 → 显著延迟
```

增量估算的改进:**基准部分是 O(1)**(直接用上次 API 数字),只有新增部分是 O(ΔN)(新增消息数,通常很小)。

### 6.2 增量估算的时间线

```
第 N-1 轮结束:
  LLM API 响应完成
  → usage.input_tokens = 48000
  → set_baseline(48000, msg_count=12)

第 N 轮开始(用户发了新消息):
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
| **压缩成功** | ✅ `invalidate_baseline()` | 消息列表被改写(旧消息换成 summary) |
| **Session 切换** | ✅ 设计已预留 | 不同 session 的 usage 不能共用 |
| **Agent 重建(F5 刷新)** | ❌ 不失效(从 jsonl 恢复)| `_restore_usage_baseline()` 从最后一条带 usage 的 entry 恢复 |
| **API 调用失败** | ❌ 不调 | 消息列表没变,旧基准仍有效 |
| **第一次启动** | ❌(初始就是 False) | 还没调用过 LLM,没有基准 |
| **老 jsonl 无 usage 字段** | ❌(恢复失败,走全量)| Day 7 之前的 jsonl entry 没有 usage,`_restore_usage_baseline` 找不到 |

### 6.5 增量 vs 全量:完整时序

```
Session 启动
    │
    ├─ 第1轮检查 ──────────────────────────────────────────→ 全量(无基准)
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
    ├─ 压缩后第1轮检查 ───────────────────────────────────→ 全量(基准已失效)
    │   (消息列表被改写了,旧的 msg_count 对不上)               │
    │                                                          │
    └─ 压缩后 LLM 响应 ──→ set_baseline(new_tokens, new_count) → 重建基准
                                                              │
                                                  下一批增量轮次...
```

### 6.6 为什么压缩后必须失效?

**关键**:`set_baseline` 捕获的是"当时那条消息列表"发给 API 后,API 说"我收到了多少 tokens"。

压缩改变了消息列表的结构:
- 压缩前:12 条原始消息 → API 说 48000 tokens
- 压缩后:8 条消息(system + summary + 6条)→ API 会说完全不同的数字

```
_baseline_msg_count = 12(压缩前)
len(messages) = 8(压缩后)

12 > 8 → 条件 _baseline_msg_count <= msg_count 不满足
→ 自动走全量路径 → 重新建立基准
```

这个机制**不需要显式判断"是否刚压缩过"**--条件检查自动降级。

### 6.7 Baseline 持久化与恢复(2026-06-17 新增)

**问题**:F5 刷新(Agent 重建)后 `_baseline_valid = False`,全量估算不计 cache 命中,数字偏高 10-50%。

**解决**:`chunk.usage` 持久化到 jsonl entry,Agent 重建时从历史恢复。

#### JSONL entry 结构

```json
{
  "type": "assistant",
  "message": {"role": "assistant", "content": "..."},
  "usage": {
    "input_tokens": 391,
    "output_tokens": 194,
    "thinking_tokens": 68,
    "cached_tokens": 256
  }
}
```

#### 恢复流程

```
Agent.__init__()
    │
    ├─ _restore_usage_baseline()
    │   ├─ Step 1: storage.read_tail(kb=64)        ← O(1) seek+read 末尾 64KB
    │   │   └─ reversed 遍历找最后一条带 usage 的 entry
    │   │      └─ 找到 → set_baseline(input_tokens, len(messages) - 1) → return
    │   │
    │   └─ Step 2: storage.read_entries()           ← O(n) fallback(极端场景)
    │       └─ 同样逻辑,遍历全部 entry
    │
    ├─ 找到 → _baseline_valid = True(增量路径生效)
    └─ 找不到 → _baseline_valid = False(全量兑底,老 jsonl 场景)
```

#### `len(messages) - 1` 的含义

最后一条带 usage 的 entry 一定是 assistant 消息。它的 `input_tokens` 是 API 在 assistant `append` **之前**捕获的:

```
chunk.usage 到达时:self.messages = [user, tools, tool_result]  ← 3 条
                     ↑ API 说 input_tokens=391

assistant append 后:self.messages = [user, tools, tool_result, assistant]  ← 4 条

刷新后恢复:self.messages = [user, tools, tool_result, assistant]  ← 4 条
           msg_count = len(self.messages) - 1 = 3
           baseline = (391, 3)
           增量 = 391 + count(msgs[3:]) = 391 + 174 = 565 ✅
```

#### 性能对比

| 方法 | 1522KB / 2000 条文件 | 倍数 |
|------|---------------------|------|
| O(1) `read_tail(64KB)` | 0.23 ms/次 | - |
| O(1) `list_sessions` | 0.51 ms/次 | - |
| O(n) `read_entries` | 7.53 ms/次 | 33x 慢 |

**read_tail 实现原理**:`seek(filesize - 64KB)` → `read(64KB)` → `splitlines()` → `json.loads()` 逐条解析。不读全文件,只读末尾窗口。64KB 窗口在极端灌水场景(单条 entry > 64KB)可能 miss,此时 fallback 到 O(n)。

## 7. 对比总结与已完成的优化

### 7.1 Token 估算方法对比

| 方法 | 准确度 | 速度 | 成本 | agent-dev 状态 |
|---|---|---|---|---|
| **全量粗略估算** | ±20% | 慢(O(N)) | 免费 | ✅ 兜底路径 |
| **增量估算**(Claude Code) | 基准±0%,新增±20% | 快(O(1)+O(ΔN)) | 免费 | ✅ 已实现(含 jsonl 持久化恢复) |
| **精确 API 计数**(L1) | ±0% | 慢(API 调用) | 有成本 | ❌ GLM 无此接口 |
| **Haiku fallback**(L2) | ±1% | 慢(API 调用) | 低成本 | ❌ 不需要 |

### 7.2 agent-dev 已完成的优化

| 优化 | 文件 | 核心改动 | 对齐 Claude Code |
|---|---|---|---|
| **P0: API usage 回传** | `agent_core.py` | `chunk.usage.input_tokens` → `set_baseline()` | ✅ |
| **P1: 增量估算** | `budget.py` | `set_baseline()` + `_estimate_used_tokens()` | ✅ |
| **P1: 增量兜底** | `budget.py` | 基准失效 → 全量估算 | ✅ |
| **P1: 压缩后失效** | `manager.py` | 压缩成功 → `invalidate_baseline()` | ✅ |
| **P2: image/doc 固定 2000** | `tokenizer.py` | image/document blocks → 固定 2000 tokens | ✅ |
| **P2: thinking 不计入** | `tokenizer.py` | thinking block → `+0` | ✅ |
| **P3: Usage 持久化** | `agent_core.py` + `storage.py` | `asdict(usage)` 写入 jsonl entry | ✅ |
| **P4: O(1) Baseline 恢复** | `agent_core.py` | `read_tail(64KB)` → fallback `read_entries` | ✅ |
| **P5: len-1 精对齐** | `agent_core.py` | `baseline_msg_count = len(messages) - 1` | ✅ |
| **cache.json 删除** | `storage.py` + `web/app.py` | 删除 sidecar 文件类型(-226行) | - |
| **Preserved Head v4** | `compact.py` | 硬 stop budget + max_turns + 第一个 turn 必含 | - |

### 7.3 仍需关注的优化方向

| 优化 | 状态 | 说明 |
|---|---|---|
| **L1 精确计数** | ❌ 待探索 | GLM API 是否有 `count_tokens` 端点待验证 |
| **image/document 脱水** | 🔶 部分 | `tokenizer.py` 已固定 2000,`compact.py` 的 `_preprocess()` 已替换占位符 |
| **文件重附机制** | ❌ 暂无 | agent-dev 未实现,无需 `stripReinjectedAttachments` |

### 7.4 测试覆盖

全量 **152/152 测试通过**(2026-06-18):

```
TestIncrementalEstimation(6个) - test_context.py
├── test_budget_set_baseline_incremental        # 增量路径正确
├── test_budget_set_baseline_full_fallback       # 基准无效时全量
├── test_budget_invalidate_baseline              # 失效后走全量
├── test_budget_invalidate_after_compaction       # 压缩后失效(自动)
├── test_budget_set_baseline_zero_ignored         # input_tokens<=0 忽略
└── test_budget_incremental_no_new_messages      # 无新增时直接用基准

TestImageDocumentTokenEstimation(3个) - test_context.py
├── test_image_block_2000_tokens                 # image 固定 2000
├── test_document_block_2000_tokens              # document 固定 2000
└── test_plain_text_unaffected                   # 纯文本不受影响

TestUsageBaselineRestore(8个) - test_usage_baseline_restore.py
├── test_usage_persisted_to_jsonl_entry          # usage 写入 jsonl
├── test_baseline_restored_from_jsonl_usage      # 从 jsonl 恢复 baseline
├── test_old_session_without_usage_fallback      # 老 jsonl 无 usage fallback
├── test_baseline_with_incremental_new_messages  # 恢复后增量估算
├── test_compact_invalidates_baseline            # 压缩后失效
├── test_restore_baseline_o1_via_tail_window     # O(1) read_tail 恢复
├── test_restore_baseline_fallback_when_tail_misses  # O(n) fallback
└── test_restore_baseline_no_usage_fallback      # 无 usage 时全量兜底

TestPreservedHeadBuilder(10个) - test_context.py
├── test_pair_user_assistant_into_turns          # 消息配对
├── test_pair_consecutive_users_separate_turns   # 连续 user 分开
├── test_empty_messages_returns_empty            # 空消息
├── test_normal_short_dialogue_all_preserved     # 短对话全保留
├── test_max_turns_hard_cap_stops_iteration      # max_turns 硬停
├── test_continuity_no_gaps                      # 连续性检查
├── test_orphan_assistant_turn_handled           # 孤儿 assistant
├── test_max_total_tokens_is_hard_limit          # v4 硬 stop
├── test_oversized_turn_excluded_by_budget       # 灌水剔除
└── test_compacted_uses_budget_strategy          # 端到端

TestUnifiedCompactPrompt(4个) - test_context.py
├── test_unified_instruction_source              # 统一指令源
├── test_fork_prompt_deterministic               # Fork prompt 确定性
├── test_fork_prompt_no_conversation_text        # Fork 不嵌对话
└── test_old_prompt_has_conversation             # 旧模式嵌对话
## 附录 A:压缩前后 Token 数变化示例

假设一个真实场景:

```
初始状态:
  - system prompt: 500 tokens
  - 历史消息: 21 条,共 107,500 tokens
  - 总计: 108,000 tokens(逼近 128K 上限)

压缩后:
  - system prompt: 500 tokens
  - summary: 500 tokens(原 21 条消息的摘要)
  - 最近 6 条消息: 5,000 tokens
  - 总计: 6,000 tokens(节省 102,000 tokens)

压缩效果:
  - Token 节省率: 94.4%
  - 信息保留: 摘要 + 最近 6 条(足够 LLM 继续对话)
```

---

## 附录 B:常见误区澄清

### 误区 1:"压缩是把消息打包成 zip"

**错**。压缩是**用 LLM 生成摘要**,替换掉原始消息。不是无损压缩,而是**有损压缩**(丢失细节,保留核心信息)。

### 误区 2:"压缩是在发送前临时做的"

**错**。压缩是**永久改写** `self.messages`。压缩后的消息列表会持久化到 JSONL,重启后仍然有效。

### 误区 3:"Token 估算越精确越好"

**不完全对**。压缩触发阈值是 `context_window * 0.75`(75%),有 ±20% 误差不会导致误触发。过度追求精确反而增加复杂度和成本。Claude Code 用三层架构,是因为它的上下文窗口更大(200K),且面向用户生产环境,误差容忍度更低。

### 误区 4:直接用手算 token 数累加来判断 context 压力

**错**。手算 token 数有两个根本性问题:

**1 缓存命中导致高估**:`cache_read_input_tokens` 是从 KV-Cache 读取的部分,它已经占据了 context window 空间(占位),但 API 不收钱(免费)。你用手算会把这部分重复计算,导致高估 10-50%。

> **关键区分**:`cache_read_input_tokens` 在三个维度有不同的含义:
> - **context window 空间**:✅ 算占用(50K + 130K = 180K 都占空间)
> - **计算成本**:❌ 不算(50K 不需要 GPU 重新算)
> - **计费账单**:❌ 不算(免费午餐)

所以高估不是 bug,是安全余量。Claude Code 直接用 API 返回的 `input_tokens` 数字,而不是自己累加,就是避免这个问题。

**2 累加 vs 实时窗口**:即使不考虑缓存,手算也是"历史所有消息的 token 和",而 context window 是"当前这一次 API 请求的大小"。两者不是同一个东西。

---

## 附录 C:参考资料

- Claude Code 源码:`claude-code-analysis/src/services/tokenEstimation.ts`
- Claude Code 源码:`claude-code-analysis/src/services/tokens.ts`
- Claude Code 源码:`claude-code-analysis/src/services/compact/compact.ts`
- agent-dev 实现:`agent_core/context/tokenizer.py`
- agent-dev 实现:`agent_core/context/budget.py`
- agent-dev 实现:`agent_core/context/compact.py`
- 设计文档:`docs/context-management-implementation-design.md`
