# 上下文管理系统：实现设计文档

> 基于 Claude Code `compact.ts` / `autoCompact.ts` / `prompt.ts` 源码深度分析，结合 agent-dev 项目落地实践
>
> 版本：v1.0 | 日期：2026-06-16
> 源码参考：`claude-code-analysis/src/services/compact/compact.ts` + `autoCompact.ts` + `prompt.ts`

---

## 一、设计理念：为什么需要上下文管理

### 1.1 核心问题

LLM 的上下文窗口是有限的。以 GLM-5.1 为例，窗口为 128,000 tokens。一个持续对话的 Agent 会不断积累消息：

```
Turn 1: 2 条消息 (~500 tokens)
Turn 5: 10 条消息 (~5,000 tokens)
Turn 20: 40 条消息 (~50,000 tokens)
Turn 50: 100 条消息 (~150,000 tokens) ← 超限，API 报错
```

**没有上下文管理的 Agent 就像没有垃圾回收的语言**——内存（token）只增不减，最终必然崩溃。

### 1.2 Claude Code 的设计哲学

Claude Code 的上下文管理由 `autoCompact.ts` + `compact.ts` + `prompt.ts` 三个模块协作实现，核心设计原则：

| 原则 | 含义 | 体现 |
|------|------|------|
| **自动触发** | 用户无感知，token 到阈值自动压缩 | `shouldAutoCompact()` 每轮检查 |
| **PTL 防御** | 压缩请求本身也可能超限，需要"剥洋葱"重试 | `truncateHeadForPTLRetry()` 截断 20% 最旧消息 |
| **熔断器** | 连续失败 N 次停止重试，避免死循环 | `MAX_CONSECUTIVE_AUTOCOMPACT_FAILURES = 3` |
| **防漂移** | 摘要必须逐字引用用户消息，不编造 | `<analysis>` + `<summary>` 双标签 + verbatim quotes |
| **软标记不删除** | 压缩后旧消息不物理删除，用 boundary 标记 | 可审计、可回退 |

### 1.3 agent-dev 的适配策略

agent-dev 运行在 GLM-5.1（非 Claude），需要删减 Claude 专有特性：

| Claude Code 特性 | agent-dev 处理 | 原因 |
|------------------|---------------|------|
| Forked Agent（借用 prompt cache） | 删除 | GLM 不支持 `cache_control` |
| `createPostCompactFileAttachments` | 暂不实现 | 文件重附机制复杂，用 preserved head 替代 |
| Hooks（PreCompact / SessionStart） | 删除 | agent-dev 无 hook 系统 |
| `reAppendSessionMetadata` | 暂不实现 | 标题在 `manager.py` 独立管理 |
| `partialCompactConversation` | 不实现 | agent-dev 只做全量压缩 |
| PTL 防御 | 保留 | GLM 同样会 prompt-too-long |
| `<analysis>` + `<summary>` prompt | 保留并强化 | 对齐 Claude Code 三段式设计 |
| 熔断器 | 保留 | 连续失败保护 |
| Preserved Head（最近 N 条） | **新增** | Claude Code 不保留，agent-dev 用作补偿 |

> **关键差异**：Claude Code 的 `compactConversation` 压缩后只保留 `boundary + summary + 文件重附 + hooks`，**不保留任何原始对话消息**。agent-dev 保留最近 6 条原始消息（`PRESERVED_HEAD_MESSAGES = 6`），因为 agent-dev 没有实现文件重附机制，需要原始消息保证上下文连续性。

---

## 二、模块架构

### 2.1 三模块协作

```
agent_core/context/
├── budget.py      (356 行) — 上下文预算管理器
├── compact.py     (641 行) — 压缩编排器
├── manager.py     (123 行) — 统一入口（Facade）
├── tokenizer.py   (103 行) — Token 估算器
└── test_context.py (690+ 行) — 单元测试
```

```
┌─────────────────────────────────────────────────────┐
│                    Agent.run()                       │
│                  agent_core.py                       │
│                                                      │
│  ┌───────────────────────────────────────────────┐  │
│  │         ContextManager (manager.py)            │  │
│  │         统一入口 / Facade                       │  │
│  │                                                │  │
│  │  check_and_compact(messages)                   │  │
│  │    ├─→ budget.should_compact()                 │  │
│  │    │     (token 用量检查)                       │  │
│  │    │                                           │  │
│  │    └─→ compactor.compact(messages)             │  │
│  │          ├─→ _preprocess (脱水)                 │  │
│  │          ├─→ _generate_summary_with_ptl        │  │
│  │          │    └─→ _call_llm_for_summary        │  │
│  │          ├─→ _extract_summary                  │  │
│  │          └─→ _build_compacted_messages         │  │
│  │                                                │  │
│  │  压缩成功 → Agent 持久化到 JSONL                │  │
│  │    └─→ _persist_compacted_messages()           │  │
│  └───────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────┘
```

### 2.2 调用时序

```
用户消息到达
    │
    ▼
Agent.run()
    │
    ├─ context_manager.check_and_compact(self.messages)
    │   │
    │   ├─ budget.should_compact() ───→ 不需要 ──→ 返回原消息
    │   │
    │   └─ 需要压缩
    │       │
    │       ├─ compactor.compact(messages)
    │       │   ├─ _preprocess (脱水)
    │       │   ├─ _generate_summary_with_ptl (LLM 摘要 + PTL 重试)
    │       │   ├─ _extract_summary (XML 标签提取)
    │       │   └─ _build_compacted_messages (组装最终消息)
    │       │
    │       └─ 返回 CompactionResult
    │
    ├─ 压缩成功？
    │   ├─ YES → self.messages = compacted
    │   │         _persist_compacted_messages()  ← 写 boundary + summary + preserved head 到 JSONL
    │   │
    │   └─ NO  → _trim_history() (旧逻辑兜底)
    │
    └─ 继续 ReAct 循环...
```

---

## 三、上下文预算管理（budget.py）

### 3.1 双模式阈值

agent-dev 支持两种压缩触发模式：

**模式 A：固定缓冲（默认）**

```
剩余 token = context_window - current_tokens
当 剩余 token < AUTOCOMPACT_BUFFER_TOKENS (13,000) 时触发压缩
```

- 对齐 Claude Code 的 `AUTOCOMPACT_BUFFER_TOKENS = 13_000`
- 13K / 128K ≈ 10% 剩余时触发

**模式 B：比例覆盖（环境变量，测试用）**

```bash
# .env
AUTOCOMPACT_PCT_OVERRIDE=25  # 剩余 ≤ 25% 时触发（即已用 ≥ 75%）
```

- 对齐 Claude Code 的 `CLAUDE_AUTOCOMPACT_PCT_OVERRIDE`
- 语义：**剩余百分比**（不是已用百分比）
- 设为 25 = 剩余 25% 时触发

### 3.2 预算状态

```python
@dataclass
class BudgetState:
    total_budget: int          # 模型上下文窗口大小
    used_tokens: int           # 当前已用 token
    remaining_tokens: int      # 剩余 token
    remaining_pct: float       # 剩余百分比
    state: str                 # "ok" | "warning" | "critical" | "overflow"
```

四档状态对应 UI 颜色：

| 状态 | 条件 | UI 颜色 | 行为 |
|------|------|---------|------|
| `ok` | 剩余 > 20K | 绿 | 正常运行 |
| `warning` | 13K < 剩余 ≤ 20K | 黄 | 显示警告 |
| `critical` | 6.5K < 剩余 ≤ 13K | 橙 | 触发自动压缩 |
| `overflow` | 剩余 ≤ 6.5K | 红 | 压缩失败兜底 |

### 3.3 熔断器

```python
MAX_CONSECUTIVE_AUTOCOMPACT_FAILURES = 3
```

连续压缩失败 3 次后停止尝试。Claude Code 用此机制避免"上下文 irrecoverably over the limit 时每轮都 hammer API"。

### 3.4 Token 估算器

agent-dev 使用轻量估算（不调 API）：

```python
class SimpleTokenCounter:
    def count_text(self, text: str) -> int:
        # 中文：≈ 1.4 tokens/字
        # 英文：≈ 0.25 tokens/字
        # 规则：遍历字符，ord > 127 算 1.4，否则 0.25

    def count_messages(self, messages: list[dict]) -> int:
        # 遍历 messages，累加 content 的 token 数
        # 加 role overhead（每条消息 +4 tokens）
        # 加 tool_use/tool_result block 估算
```

---

## 四、压缩编排器（compact.py）

### 4.1 压缩流程

```python
class CompactOrchestrator:
    def compact(self, messages: list[dict]) -> CompactionResult:
        # 1. 预处理（脱水）
        preprocessed = self._preprocess(messages)
        # 2. 生成摘要（含 PTL 防御）
        summary, ptl_retries = self._generate_summary_with_ptl(preprocessed)
        # 3. 组装压缩后消息
        compacted = self._build_compacted_messages(summary, messages)
        return CompactionResult(success=True, summary=summary, ...)
```

### 4.2 预处理（脱水）

`_preprocess()` 在送 LLM 之前减小消息体积：

| 操作 | 效果 | 常量 |
|------|------|------|
| 截断超长 tool_result | `content[:8000] + "...[truncated]"` | `TOOL_RESULT_TRUNCATE_CHARS = 8000` |
| 图片占位替换 | `"[image]"` 替换 base64 | — |
| 跳过 thinking block | 不送 Claude 的 thinking 给 GLM | — |

### 4.3 PTL 防御（Prompt-Too-Long 重试）

**问题**：压缩请求本身也可能 prompt-too-long（对话太长，连压缩请求都超限）。

**策略**：剥洋葱——截断最旧的 20% 消息，最多重试 3 次：

```python
def _generate_summary_with_ptl(self, messages):
    for attempt in range(MAX_PTL_RETRIES + 1):
        summary, raw = self._call_llm_for_summary(messages)
        if summary and not summary.startswith("Error: prompt too long"):
            return summary, attempt  # 成功
        # 剥掉最旧的 20%
        cut = max(1, int(len(messages) * TRUNCATE_RATIO))
        messages = messages[cut:]
    return None, MAX_PTL_RETRIES  # 彻底失败
```

| 参数 | 值 | 对齐 Claude Code |
|------|-----|-----------------|
| `MAX_PTL_RETRIES` | 3 | 对齐 |
| `TRUNCATE_RATIO` | 0.2 (20%) | 对齐 `truncateHeadForPTLRetry` |

### 4.4 压缩 Prompt（三段式设计）

完全对齐 Claude Code `prompt.ts` 的三段式结构：

**第一段：开头禁令（NO_TOOLS_PREAMBLE）**

```
CRITICAL: Respond with TEXT ONLY. Do NOT call any tools.
- Do NOT use Read, Bash, Grep, Glob, Edit, Write, or ANY other tool.
- Tool calls will be REJECTED and will waste your only turn.
- Your entire response must be plain text: an <analysis> block followed by a <summary> block.
```

**第二段：主体指令 + Few-Shot Example**

- 详细的 `<analysis>` 分析要求（逐条分析每条消息）
- 4 段 `<summary>` 结构模板（用户目标 / 关键决策 / 当前状态 / 待办事项）
- 防漂移规则（verbatim quotes、Next Step 与最近请求相关、不捡起旧任务）
- 完整的 `<example>` 展示期望输出格式

**第三段：结尾提醒（NO_TOOLS_TRAILER）**

```
REMINDER: Do NOT call any tools. Respond with plain text only —
an <analysis> block followed by a <summary> block.
Tool calls will be rejected and you will fail the task.
```

> **验证结果**：GLM-5.1 在此 prompt 下完美输出 `<analysis>` 和 `<summary>` XML 标签，4 个标签全部成对闭合，无需 fallback。

### 4.5 Summary 提取（三层 fallback）

```python
def _extract_summary(self, raw_text: str) -> str:
    # 1. 优先提取 <summary>...</summary>
    match = re.search(r'<summary>([\s\S]*?)</summary>', raw_text)
    if match:
        return match.group(1).strip()
    # 2. fallback: 提取 <analysis>...</analysis>
    match = re.search(r'<analysis>([\s\S]*?)</analysis>', raw_text)
    if match:
        return match.group(1).strip()
    # 3. fallback: 返回原文
    return raw_text.strip()
```

> **Claude Code 对比**：`formatCompactSummary()` 同样用 regex 提取，且缺标签时不报错不重试——优雅降级。agent-dev 的三层 fallback 与此一致。

### 4.6 压缩后消息组装

```python
def _build_compacted_messages(self, summary: str, original: list[dict]) -> list[dict]:
    # 结构: [system] + [summary] + [preserved head]
    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": f"[Previous conversation summarized]\n\n{summary}"},
        *original[-PRESERVED_HEAD_MESSAGES:],  # 最近 6 条
    ]
```

| 组件 | 来源 | 对齐 Claude Code |
|------|------|-----------------|
| system | Agent 的 `self.system_prompt` | 对齐（Claude Code 同样动态注入） |
| summary user msg | `"[Previous conversation summarized]\n\n{summary}"` | 对齐 `getCompactUserSummaryMessage()` |
| preserved head | `original[-6:]` | Claude Code 不保留（有文件重附替代） |

### 4.7 DEBUG 日志

压缩全链路 DEBUG 日志，清晰展示每一步状态：

```
[Compact START] messages=22, tokens_before=108,755
  First msg: user content=帮我计算 123*456...
  Last msg: user content=继续...
[Preprocess] 22 -> 22 messages
[Compact DONE] 108,755 -> 8,420 tokens (freed 100,335), PTL retries: 0, 2340ms, preserved_head=6
  Final structure: [system] + [summary] + [6 preserved head]
```

---

## 五、压缩持久化（agent_core.py）

### 5.1 设计原则

压缩后的消息必须同步持久化到 JSONL 文件，否则重启后上下文残缺。

**核心原则：单一构造点**

对齐 Claude Code 的 `buildPostCompactMessages()`（compact.ts:325-338）：

```typescript
// Claude Code: 单点构造保证完整性
export function buildPostCompactMessages(result: CompactionResult): Message[] {
  return [
    result.boundaryMarker,
    ...result.summaryMessages,
    ...(result.messagesToKeep ?? []),
    ...result.attachments,
    ...result.hookResults,
  ]
}
```

agent-dev 对应实现为 `_persist_compacted_messages()`：

```python
def _persist_compacted_messages(self, compacted, compact_result):
    """对齐 Claude Code buildPostCompactMessages 单一构造点"""
    storage = self._session_manager.storage

    # 1. boundary（parent 链到最后一条旧消息）
    storage.add_compact_boundary(
        trigger="auto",
        pre_tokens=compact_result.tokens_before,
        messages_summarized=len(compacted) - 1,
    )

    # 2. summary（parent 链到 boundary）
    storage.add_summary(
        summary=compact_result.summary,
        tokens_saved=compact_result.tokens_freed,
    )

    # 3. preserved head（逐条写入，跳过 system 和 summary）
    for msg in compacted[1:]:
        if role not in ("user", "assistant"):
            continue
        if content.startswith("[Previous conversation summarized]"):
            continue
        storage.append_entry(entry_type=role, message=msg)

    # 4. flush 确保落盘
    storage.flush()

    # 5. 同步 manager._last_uuid（防 parent 错位）
    self._session_manager._last_uuid = storage.last_uuid
```

### 5.2 压缩后 JSONL 结构

```
压缩前：22 条主链 entry（#1-#22）

压缩后新增 9 条 entry（#23-#31）：
  #23  BOUNDARY    (type=system, subtype=compact_boundary, parent=#22)
  #24  SUMMARY     (type=user, isCompactSummary=True, parent=#23)
  #25  preserved[0]  (type=user, parent=#24)
  #26  preserved[1]  (type=assistant, parent=#25)
  #27  preserved[2]  (type=user, parent=#26)
  ...
  #30  preserved[5]  (type=assistant, parent=#29)
  #31  后续对话     (type=assistant, parent=#30) <- 正确链到 preserved[5]
```

### 5.3 关键设计决策

**为什么用 `storage.append_entry` 而非 `manager.add_user_message`**：

| 方式 | 优点 | 缺点 |
|------|------|------|
| `manager.add_user_message` | 自动更新 `_last_uuid` | 触发标题生成 LLM 调用（不必要） |
| `storage.append_entry` | 纯写入，不触发 hook | 不自动更新 `manager._last_uuid` |

选择 `storage.append_entry` + 手动同步 `_last_uuid`，避免不必要的标题 LLM 调用。

**为什么手动同步 `manager._last_uuid`**（P1 修复）：

`storage.append_entry` 只更新 `storage._last_uuid`，不更新 `manager._last_uuid`。后续对话 `manager.add_assistant_message` 会用 `manager._last_uuid` 作 parent，链到压缩前的旧消息（错位）。

```python
# P1 修复（commit 2e0a1d6）
self._session_manager._last_uuid = storage.last_uuid
```

---

## 六、Boundary 标记与会话恢复

### 6.1 Boundary 结构

```json
{
    "uuid": "b6327c04",
    "parentUuid": "95b96bbd",
    "sessionId": "7f071c62",
    "type": "system",
    "subtype": "compact_boundary",
    "compactMetadata": {
        "trigger": "auto",
        "preTokens": 108785,
        "messagesSummarized": 6
    },
    "timestamp": "2026-06-15T22:54:30.123456",
    "message": {}
}
```

### 6.2 Boundary 的三个核心作用

1. **时间分水岭**：boundary 之前是"旧对话"（已被压缩），之后是"新对话"
2. **防止 LLM 看到超长上下文**：`get_messages_for_llm(stop_at_boundary=True)` 只读 boundary 之后
3. **状态恢复点**：重启时从 boundary 之后恢复当前工作上下文

### 6.3 会话恢复流程

```python
# 启动时加载（agent_core.py:201）
self.messages = self._session_manager.get_messages_for_llm()
# stop_at_boundary=True（默认），只读最后一个 boundary 之后的消息

# 切换会话时（manager.py:switch）
messages = storage.get_messages(stop_at_boundary=False)
# stop_at_boundary=False，保留完整历史用于 UI 展示
```

**`get_messages(stop_at_boundary=True)` 实现**：

```python
def get_messages(self, stop_at_boundary=True):
    entries = self.read_entries()
    if stop_at_boundary:
        # 反向扫描找最后一个 compact_boundary
        for i in range(len(entries) - 1, -1, -1):
            if self._is_boundary(entries[i]):
                entries = entries[i + 1:]  # 只取 boundary 之后
                break
    # 过滤 metadata types，保留 user/assistant/system
    return [e["message"] for e in entries if e["type"] in ("user", "assistant", "system")]
```

### 6.4 为什么是软标记（不物理删除）

| 方案 | 优点 | 缺点 |
|------|------|------|
| 物理删除旧消息 | 文件小 | 不可审计、不可回退 |
| 软标记 boundary | 可审计、可回退 | 文件持续增长 |

agent-dev 选择软标记 + `recover_uncompressed.py` 恢复工具，对齐 Claude Code 的 append-only 原则。

---

## 七、消息架构重构

### 7.1 双写问题根因

**重构前（commit 3089a29 之前）**：

```
Agent 实例
- self.history    <- 内存中的消息列表（真相源 A）
- _session_manager
  - storage
    - JSONL   <- 磁盘上的消息列表（真相源 B）
```

**问题**：
- 压缩后 `self.history` 被替换（8 条新消息），但 JSONL 是 append-only（22 条旧消息仍在）
- `_save_to_session()` 遍历 `self.history` 重写磁盘 -> 消息翻倍
- 双写双源，必然不一致

### 7.2 重构方案（commit 3089a29）

**对齐 Claude Code 的单一真相源**：

```
Agent 实例
- self.messages   <- 内存中的消息列表（唯一真相源，从 JSONL 加载）
  - _session_manager
    - storage
      - JSONL  <- 磁盘持久化（append-only，每条消息实时写入）
```

| 改动 | 说明 |
|------|------|
| `self.history` -> `self.messages` | 对齐 Claude Code `messages: Message[]` |
| 删除 `_save_to_session()` | 消除双写根因 |
| `load_history()` -> `load_messages()` | 语义更清晰 |
| 压缩后直接调 `_persist_compacted_messages()` | 单点构造，不再遍历重写 |

### 7.3 消息缓存删除（commit ec774cc）

**重构前**：`SessionManager` 维护 `_message_cache`（内存缓存），与 JSONL 磁盘数据双源。

**问题**：`add_assistant_with_tools` / `add_tool_results` / `add_summary` / `add_compact_boundary` 都没同步 `_message_cache`，导致缓存不完整。

**重构后**：删除 `_message_cache` 全层 + `include_pending` 参数。JSONL 是唯一真相源，`get_messages()` 每次从磁盘读。

---

## 八、辅助工具

### 8.1 recover_uncompressed.py（会话恢复）

**用途**：从压缩状态回退到未压缩时刻。

```bash
# 按 session_id 恢复
python3 scripts/recover_uncompressed.py 7f071c62

# 干跑（不写盘）
python3 scripts/recover_uncompressed.py 7f071c62 --dry-run
```

**行为**：
1. 找最后一个 `compact_boundary`
2. 备份原文件到 `.recovery-backup`
3. 截断到 boundary 之前（保留压缩前的所有 entry）
4. 验证：打印恢复前后的 entry 数和 LLM 加载视图

**为什么有效**：append-only 设计保证旧消息不物理删除，boundary 之前的 entry 完整保留，截断操作完全可逆。

### 8.2 verify_summary.py（摘要验证）

**用途**：验证压缩生成的 summary 是否符合 prompt 要求。

```bash
# 验证单个 session
python3 scripts/verify_summary.py 7f071c62

# 验证所有 session
python3 scripts/verify_summary.py --all

# 严格模式（要求 XML 标签）
python3 scripts/verify_summary.py 7f071c62 --strict
```

**8 项检查**：
1. 前缀 `[Previous conversation summarized]\n\n`
2. `<analysis>` 标签存在
3. `</analysis>` 闭合
4. `<summary>` 标签存在
5. `</summary>` 闭合
6. 4 段结构（用户目标/关键决策/当前状态/待办事项）
7. 逐字引用（防漂移规则）
8. 摘要长度（200-800 字符）

---

## 九、日志系统

### 9.1 分级设计

| 级别 | 内容 | 数量 |
|------|------|------|
| **INFO** | Session loaded / Context compacted（关键生命周期事件） | 2 |
| **DEBUG** | LLM 发送/返回详情、文本输出、思考过程、工具调用、session saved | 10+ |
| **WARNING** | 保存失败、压缩失败、flush 失败 | 5 |
| **ERROR** | LLM 调用异常 | 2 |

### 9.2 日志配置

**单一 handler 来源**（修复重复输出）：

```python
# agent_core.py — 不再自建 handler
_logger = logging.getLogger("react_agent")
_logger.setLevel(logging.DEBUG)  # 自己放开 DEBUG，由 root handler level 控制输出

# web/app.py — 统一配置 root handler
logging.basicConfig(
    level=_log_level,  # 默认 INFO，--log-level=DEBUG 可切换
    format='[%(asctime)s] [%(levelname)s] %(name)s: %(message)s',
    datefmt='%H:%M:%S',
)
```

### 9.3 Streamlit 集成

```bash
# 默认 INFO
python3 -m streamlit run web/app.py

# DEBUG 模式（看全量细节）
python3 -m streamlit run web/app.py -- --log-level=DEBUG
```

DEBUG 模式下自动安静第三方库（httpx/openai/anthropic/watchdog/git）。

---

## 十、与 Claude Code 的对照分析

### 10.1 架构对照

| 维度 | Claude Code | agent-dev | 一致性 |
|------|-------------|-----------|--------|
| 触发方式 | 自动 + 手动 | 自动 + 手动 | 一致 |
| 预算检查 | `shouldAutoCompact()` | `budget.should_compact()` | 一致 |
| PTL 防御 | `truncateHeadForPTLRetry()` | `_generate_summary_with_ptl()` | 一致 |
| 熔断器 | `MAX_CONSECUTIVE_AUTOCOMPACT_FAILURES=3` | 同 | 一致 |
| Prompt 设计 | 三段式（开头 + 主体 + 结尾） | 同 | 一致 |
| XML 标签 | `<analysis>` + `<summary>` | 同 | 一致 |
| 标签提取 | `formatCompactSummary()` regex | `_extract_summary()` 三层 fallback | 一致 |
| 标签缺失处理 | 不报错，优雅降级 | 同 | 一致 |
| 压缩后消息 | boundary + summary + 文件重附 + hooks | boundary + summary + preserved head | 不同方案 |
| preserved head | 不保留 | 保留最近 6 条 | agent-dev 独有 |
| 文件重附 | `createPostCompactFileAttachments` | 不实现 | 暂不做 |
| Hooks | PreCompact / SessionStart / PostCompact | 不实现 | 暂不做 |
| Forked Agent | 借用 prompt cache | 删除 | GLM 不支持 |
| Boundary 持久化 | query.ts yield loop | `_persist_compacted_messages()` | 一致 |
| 消息架构 | MessageState 单例 | `self.messages` + JSONL | 一致 |

### 10.2 Preserved Head 的取舍

Claude Code 不保留任何原始对话消息，依赖三个补偿机制：

1. **文件重附**（`createPostCompactFileAttachments`）：把 Read 过的文件内容重新注入
2. **Plan 重附**（`createPlanAttachmentIfNeeded`）：恢复当前计划
3. **Skill 重附**（`createSkillAttachmentIfNeeded`）：恢复已调用的 Skill

agent-dev 没有这三个机制，用 `PRESERVED_HEAD_MESSAGES = 6`（约 2 轮完整对话）做简单补偿：

- **优点**：实现简单，多轮连续对话不断片
- **代价**：压缩省下的 token 被 6 条消息占去一部分
- **未来方向**：实现文件重附后可减少到 2-3 条或完全删除

---

## 十一、关键教训

### 11.1 P0：preserved head 未落盘

**现象**：压缩后内存 8 条消息正常，重启后只剩 summary + 后续，6 条 preserved head 永久丢失。

**根因**：`_persist_compacted_messages` 只调了 `add_compact_boundary` + `add_summary`，漏写 preserved head 循环。

**修复**：commit `a032609`，补上 preserved head 逐条 `append_entry`。

**教训**：分散调多个方法容易漏，应该用单一构造点（对齐 `buildPostCompactMessages`）。

### 11.2 P1：parent 错位

**现象**：压缩后第一条后续对话的 parentUuid 指向 boundary 之前的旧消息，而不是 preserved head 最后一条。

**根因**：`storage.append_entry` 只更新 `storage._last_uuid`，不更新 `manager._last_uuid`。

**修复**：commit `2e0a1d6`，`_persist_compacted_messages` 末尾同步 `manager._last_uuid = storage.last_uuid`。

**教训**：绕过高阶方法用底层 API 时，必须手动同步所有依赖高阶方法维护的状态。

### 11.3 P0：restore.py 不识别新格式 boundary

**现象**：重启后恢复的消息包含压缩前的全部旧消息，boundary 标记失效。

**根因**：`_is_compact_boundary()` 只识别旧格式（`type=compact-boundary`），不识别新格式（`type=system + subtype=compact_boundary`）。

**修复**：commit `090623a`，增强 `_is_compact_boundary()` 支持两种格式。

**教训**：格式迁移期间必须兼容新旧两种格式，直到所有旧数据被清理。

### 11.4 重复日志

**现象**：每条日志输出两次，格式不同。

**根因**：`agent_core.py` 自建 `StreamHandler` + root logger 的 `basicConfig` handler = 两个 handler。Python logging 的 `propagate=True`（默认）导致消息同时输出到两个 handler。

**修复**：删除 `agent_core.py` 的自建 handler，统一走 root handler。

**教训**：子 logger 不要自建 handler，让 root handler 统一管理格式和级别。

---

## 十二、测试覆盖

### 12.1 测试统计

```
98 passed in 0.58s
```

| 测试文件 | 测试数 | 覆盖范围 |
|---------|--------|---------|
| `test_context.py` | ~30 | CompactOrchestrator / ContextBudgetManager / TokenCounter |
| `test_session.py` | ~68 | SessionStorage / SessionManager / 持久化 / 恢复 / Boundary |

### 12.2 关键测试

```python
# 压缩持久化完整性
test_persist_compacted_writes_all_messages
test_persist_compacted_skips_system_and_summary
test_persist_compacted_no_session_manager
test_persist_compacted_syncs_manager_last_uuid

# Boundary 格式兼容
test_resume_with_new_format_boundary_and_summary
test_resume_without_summary_still_works

# 会话恢复工具
test_recover_uncompressed_script
test_recover_uncompressed_cli

# P1/P2 回归
test_no_message_cache_attribute
test_list_sessions_uses_from_tail
test_daily_logger_thread_safety
test_search_raises_network_errors
```

---

## 附录 A：Claude Code 源码索引

| 文件 | 行数 | 功能 |
|------|------|------|
| `src/services/compact/compact.ts` | ~1600 | 核心压缩逻辑 |
| `src/services/compact/autoCompact.ts` | ~350 | 自动触发 + 熔断器 |
| `src/services/compact/prompt.ts` | ~400 | 压缩 prompt（三段式） |
| `src/services/compact/sessionMemoryCompact.ts` | ~600 | Session Memory 压缩 |
| `src/utils/sessionStorage.ts` | ~5100 | JSONL 持久化 |
| `src/utils/context.ts` | — | 上下文工具函数 |

## 附录 B：agent-dev 代码索引

| 文件 | 行数 | 功能 |
|------|------|------|
| `agent_core/context/budget.py` | 356 | 上下文预算管理 |
| `agent_core/context/compact.py` | 641 | 压缩编排器 |
| `agent_core/context/manager.py` | 123 | 统一入口 |
| `agent_core/context/tokenizer.py` | 103 | Token 估算 |
| `agent_core/context/test_context.py` | 690+ | 单元测试 |
| `agent_core/agent_core.py` | 652 | Agent 主体（含 `_persist_compacted_messages`） |
| `agent_core/session/storage.py` | 767 | JSONL 持久化 |
| `agent_core/session/manager.py` | 926 | Session 管理 |
| `agent_core/session/restore.py` | — | 会话恢复（含 boundary 识别） |
| `scripts/recover_uncompressed.py` | 267 | 压缩回退工具 |
| `scripts/verify_summary.py` | 310 | 摘要验证 |

## 附录 C：Commit 历史（2026-06-15）

```
0771daf feat(compact): 完全对齐 Claude Code prompt.ts 设计
de7d56d feat(compact): 加 DEBUG 日志清晰看到压缩过程
2ce83d9 feat(compact): Option B+C 强化 prompt 严格使用 XML 标签 + few-shot
bfd0677 feat(scripts): 添加 verify_summary.py 验证工具
2e0a1d6 fix(agent_core): _persist_compacted_messages 同步 manager._last_uuid
efaa2f6 chore: 忽略 recovery-backup 备份文件
457bc0f chore: 从 git 移除误提交的 recovery-backup
a2abf66 feat(scripts): 添加 recover_uncompressed.py 恢复工具
a032609 fix(agent_core): 压缩后 preserved head 6 条消息必须落盘
88c28c5 fix(session): _get_last_uuid 跳过元数据 entry 修复断链
ec774cc fix: 修复 P1/P2 全部遗留问题
090623a fix: 修复 3089a29 refactor 留下的两个回归
299cc59 cleanup: remove backup files
3089a29 refactor: 对齐 Claude Code 消息架构 + 压缩持久化
333f178 refactor(context): AUTOCOMPACT_PCT_OVERRIDE 改为剩余百分比语义
73680ff feat(context): 双模式阈值 — 固定缓冲+比例覆盖
3054b6e refactor: 移除重复的 Token 消耗面板
1fd8bd7 feat: Streamlit UI 集成上下文预算面板
be19094 feat: 上下文管理系统 Phase 1 — ContextBudgetManager + CompactOrchestrator
```

## 变更历史

| 版本 | 日期 | 变更 |
|------|------|------|
| v1.0 | 2026-06-16 | 初始版本：完整记录上下文管理系统设计与实现 |
