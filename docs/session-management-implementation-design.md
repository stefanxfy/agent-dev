# Agent 会话管理系统：实现设计文档

> 基于 Claude Code 源码深度分析，结合 agent-dev 项目落地实践
>
> 版本：v1.0 | 日期：2026-06-13
> 源码参考：`claude-code-analysis/src/utils/sessionStorage.ts` + `sessionTitle.ts` + `bridge/initReplBridge.ts`

---

## 一、设计理念：为什么需要会话管理

### 1.1 核心问题

Agent 系统的会话管理不是"聊天记录存磁盘"，而是 **连续性基础设施**：

```
用户启动会话 → 做一系列操作 → 关闭终端 →
第二天回来 → 继续昨天的工作，好像没中断过
```

这要求系统能做到：
- **崩溃恢复**：进程被杀，对话数据不丢
- **断点续接**：从上次中断处精确恢复
- **分支探索**：Fork 一条会话做实验，不影响主线
- **自动元数据**：标题、标签自动生成，用户可检索

### 1.2 Claude Code 的设计哲学

Claude Code 的会话管理由 `sessionStorage.ts`（~1600行）实现，核心设计原则：

| 原则 | 含义 | 体现 |
|------|------|------|
| **Append-Only** | 文件只追加不修改，崩溃安全 | JSONL 格式，永不原地重写 |
| **信封套信纸** | 存储层管信封（Entry），业务层管信纸（message） | `message` 字段存 API 原始对象，零转换 |
| **parentUuid 森林** | 每条消息指向父消息，形成 DAG | Fork/Compact 不破坏历史链 |
| **类型即元数据** | 用 Entry type 区分数据种类 | `user`/`assistant`/`ai-title`/`custom-title`/`compact_boundary` |
| **延迟物化** | 创建 Session 时不立即创建文件 | 首条消息才 materialize，避免空文件 |
| **Tail 窗口** | 读取只看文件尾部 64KB | 快速恢复，不需要全文件扫描 |

---

## 二、存储结构

### 2.1 文件布局

```
agent-dev/
├── data/
│   └── sessions/
│       ├── {session_id}.jsonl     ← 每个会话一个 JSONL 文件
│       ├── {session_id}.jsonl
│       └── ...
```

- **Session ID**：UUID v4，全局唯一
- **文件格式**：JSONL（JSON Lines），每行一个 JSON 对象
- **写入模式**：Append-Only（只追加，永不原地修改）
- **读取模式**：Tail 窗口（默认 64KB），从文件尾部向前读

### 2.2 Entry 结构（信封套信纸）

每条 JSONL 记录是一个 **Entry**（信封），包含元数据和业务数据：

```json
{
    "uuid": "e1fd26e0-fe4d-498f-aa12-89cc43f8f06b",
    "parentUuid": "3eb53d6f-0c8f-4fa8-a0d6-2cfbb14b5be5",
    "sessionId": "dac18d8b",
    "type": "user",
    "timestamp": "2026-06-13T17:32:27.458735",
    "message": {                          ← 信纸：API 原始消息对象
        "role": "user",
        "content": "你好，我是小明"
    }
}
```

**核心字段**：

| 字段 | 类型 | 说明 |
|------|------|------|
| `uuid` | string | 本条 Entry 的唯一标识 |
| `parentUuid` | string\|null | 父消息 UUID，组成消息链（null 表示链头） |
| `sessionId` | string | 所属会话 ID |
| `type` | string | Entry 类型（见下表） |
| `timestamp` | ISO 8601 | 创建时间 |
| `message` | object\|null | API 原始消息（user/assistant 专有） |
| `**extra` | any | 顶层扩展字段（thinking, tool_logs 等） |

### 2.3 Entry 类型一览

| type | 用途 | message 字段 | 示例 |
|------|------|-------------|------|
| `user` | 用户消息 / tool_result | `{"role":"user","content":"..."}` | 纯文本消息 |
| `assistant` | AI 回复（含 tool_use） | `{"role":"assistant","content":[...]}` | 文本 + 工具调用 |
| `ai-title` | AI 自动生成的标题 | 无，用 `aiTitle` 字段 | `{"aiTitle":"用户问候","genSeq":1}` |
| `custom-title` | 用户手动命名的标题 | 无，用 `customTitle` 字段 | `{"customTitle":"我的会话"}` |
| `compact_boundary` | 上下文压缩断链标记 | 无 | 压缩后旧消息与新消息的分界 |
| `summary` | 压缩生成的摘要 | `{"role":"system","content":"..."}` | 上下文压缩后的摘要消息 |

### 2.4 Claude Code 风格的消息存储

agent-dev 采用与 Claude Code 完全一致的消息存储格式——**tool_use 和 tool_result 内嵌在 content 数组中**，而不是作为独立 Entry：

**一次完整的工具调用流程（旧 vs 新）**：

```
旧格式（7条独立 Entry）：
  user → assistant(text) → tool_use → tool_use → tool_result → tool_result → assistant(final)

新格式 Claude Code 风格（4条 Entry）：
  user → assistant(text + tool_use×2) → user(tool_result×2) → assistant(final)
```

**实际 JSONL 示例**：

```jsonl
{"uuid":"...", "type":"user", "message":{"role":"user","content":"计算 123*456"}}
{"uuid":"...", "type":"assistant", "message":{"role":"assistant","content":[
    {"type":"text","text":"好的，让我来计算"},
    {"type":"tool_use","id":"call_abc","name":"calc","input":{"expression":"123*456"}}
]}}
{"uuid":"...", "type":"user", "message":{"role":"user","content":[
    {"type":"tool_result","tool_use_id":"call_abc","content":"56088.0"}
]}}
{"uuid":"...", "type":"assistant", "message":{"role":"assistant","content":"结果是 56088"}}
```

**优势**：
- `get_messages_for_llm()` 直接取 `message` 字段传给 LLM API，**零格式转换**
- 减少条目数（7→4），降低 JSONL 文件体积
- 与 Claude Code 存储格式完全对齐，方便对照学习

---

## 三、模块架构

### 3.1 整体架构

```
SessionManager (Facade)
├── storage: SessionStorage          # JSONL 持久化（append-only / parentUuid 链 / 写队列）
├── metadata: SessionMetadata        # 元数据管理（标题 / 标签 / Agent类型 / 模式）
├── state: SessionState              # 状态机（idle / running / requires_action）
├── progress: ProgressTracker        # 进度追踪（文件变更 / 待办 / Turn 统计）
├── cleanup: SessionCleanup          # 清理归档（TTL / 归档 / 磁盘统计）
├── restore: resume_session / ...    # 会话恢复（Resume / Continue / Fork）
└── title: TitleState 状态机         # 标题生成（两阶段 + genSeq 防乱序）
```

### 3.2 模块职责边界

| 子系统 | 管"什么" | 不管"什么" |
|--------|---------|-----------|
| **会话管理** | Session ID、元数据、JSONL 持久化、Resume/Fork | 消息链内容、Token 预算 |
| **上下文管理** | 消息链、Token 预算、压缩 | 跨会话知识 |
| **记忆系统** | 跨会话知识（memory.md） | 单次会话的消息链 |

### 3.3 SessionStorage 写入管线

```
调用方 → append_entry(type, message, parent_uuid, **extra)
                    ↓
            _make_entry()  ← 构造 Entry dict + 注册 UUID 到内存索引
                    ↓
            _pending.append(entry)  ← 加入写队列
                    ↓
            flush() / _flush_loop()  ← 批量写入磁盘（原子性）
```

- **写队列**：先入内存 `_pending` 列表，批量刷盘减少 I/O
- **UUID 去重**：`_uuid_set` 记录所有已分配的 UUID，防止重复
- **自动链接**：不传 `parent_uuid` 时自动链到最新 Entry

### 3.4 parentUuid 森林结构

```
Entry A (parentUuid=None)     ← 消息链头
  └── Entry B (parentUuid=A)
        └── Entry C (parentUuid=B)
              └── Entry D (parentUuid=C)

compact_boundary (parentUuid=None)  ← 压缩断链，新链头
  └── Entry E (parentUuid=None)     ← 压缩后的新消息链
        └── Entry F (parentUuid=E)
```

- 正常消息形成单链（A→B→C→D）
- 压缩时插入 `compact_boundary`，旧链断开，新链从 E 开始
- Fork 时复制父会话的全部消息，分配新 UUID，独立演进

---

## 四、会话恢复：Resume / Continue / Fork

### 4.1 三种恢复语义

| 操作 | 语义 | 实现方式 |
|------|------|---------|
| **Resume** | 从断点恢复，只加载摘要 + 最新消息 | 从尾部扫描，找到 `compact_boundary`，加载之后的 Entry |
| **Continue** | 继续完整会话，加载全部历史 | 从文件头读取所有 Entry |
| **Fork** | 分叉创建新会话，复制父会话消息 | 深拷贝父会话消息，分配新 Session ID 和新 UUID |

### 4.2 Resume 的断链逻辑

当上下文超过 Token 预算时，系统执行压缩：
1. 将旧消息总结为一条 `summary` Entry
2. 插入一条 `compact_boundary` Entry 标记断点
3. 后续新消息从断点开始新链

Resume 时只读取 `compact_boundary` 之后的 Entry + summary，避免加载全部历史。

---

## 五、标题生成系统

### 5.1 设计灵感：Claude Code 的三层标题机制

Claude Code（源码：`utils/sessionTitle.ts` + `bridge/initReplBridge.ts`）使用三层标题生成：

```
第一层：即时派生（deriveTitle）        ← 0延迟，纯本地计算
    ↓ 异步
第二层：AI 生成（generateSessionTitle）  ← Haiku 模型，3-7词
    ↓ 用户操作
第三层：用户自定义（/rename）           ← 永久锁定
```

**Claude Code 的触发逻辑**（`onUserMessage` 回调）：

```typescript
// initReplBridge.ts 简化版
let userMessageCount = 0
let genSeq = 0

const onUserMessage = (text: string): boolean => {
    if (hasExplicitTitle) return true          // 用户改过，永不覆盖

    userMessageCount++

    if (userMessageCount === 1 && !hasTitle) {
        // 第1条：即时占位 + 异步 AI 生成
        const placeholder = deriveTitle(text)
        patch(placeholder)
        generateAndPatch(text)
    } else if (userMessageCount === 3) {
        // 第3条：用完整对话重新生成（覆盖第1条的粗略标题）
        const input = extractConversationText(getMessagesAfterCompactBoundary(msgs))
        generateAndPatch(input)
    }

    return userMessageCount >= 3  // 第3条后锁定
}
```

**Claude Code 的 genSeq 防乱序**：

```typescript
const generateAndPatch = (input: string): void => {
    const gen = ++genSeq  // 递增序号

    void generateSessionTitle(input, AbortSignal.timeout(15_000)).then(generated => {
        // 只有最新序号的结果才写入
        if (generated && gen === genSeq && !getCurrentSessionTitle(getSessionId())) {
            patch(generated)
        }
    })
}
```

第1条的 Haiku 请求可能比第3条的晚返回（网络延迟），genSeq 确保旧请求的结果被丢弃。

### 5.2 Claude Code 的标题 Prompt（完整原文）

```
Generate a concise, sentence-case title (3-7 words) that captures the main
topic or goal of this coding session. The title should be clear enough that
the user recognizes the session in a list. Use sentence case: capitalize only
the first word and proper nouns.

Return JSON with a single "title" field.

Good examples:
{"title": "Fix login button on mobile"}
{"title": "Add OAuth authentication"}
{"title": "Debug failing CI tests"}
{"title": "Refactor API client error handling"}

Bad (too vague): {"title": "Code changes"}
Bad (too long): {"title": "Investigate and fix the issue where the login button does not respond on mobile devices"}
Bad (wrong case): {"title": "Fix Login Button On Mobile"}
```

**Prompt 设计要点**：
1. **明确目标**："captures the main topic or goal"（概括主题或目标，不是提取关键词）
2. **格式约束**：3-7 词、sentence-case
3. **好坏示例**：4个好示例 + 3个坏示例（太模糊/太长/大小写错误）
4. **JSON Schema**：用 `outputFormat: json_schema` 约束输出结构

### 5.3 Claude Code 的 extractConversationText（输入构造）

```typescript
// sessionTitle.ts
const MAX_CONVERSATION_TEXT = 1000

export function extractConversationText(messages: Message[]): string {
    const parts: string[] = []
    for (const msg of messages) {
        if (msg.type !== 'user' && msg.type !== 'assistant') continue
        if ('isMeta' in msg && msg.isMeta) continue           // 跳过元数据消息
        const content = msg.message.content
        if (typeof content === 'string') {
            parts.push(content)
        } else if (Array.isArray(content)) {
            for (const block of content) {
                if (block.type === 'text') parts.push(block.text)
            }
        }
    }
    const text = parts.join('\n')
    return text.length > MAX_CONVERSATION_TEXT
        ? text.slice(-MAX_CONVERSATION_TEXT)   // 截取最后1000字符
        : text
}
```

**关键设计**：截取**尾部** 1000 字符（不是前部），因为最近的对话最能代表当前主题。

### 5.4 Claude Code 的 saveAiGeneratedTitle（存储设计）

```typescript
// sessionStorage.ts 第 2667 行
export function saveAiGeneratedTitle(sessionId: UUID, aiTitle: string): void {
    appendEntryToFile(getTranscriptPathForSession(sessionId), {
        type: 'ai-title',
        aiTitle,
        sessionId,
    })
}
```

**为什么用独立的 `ai-title` 类型而不是复用 `custom-title`**（源码注释原文）：

1. **读取优先级**：读取时先查 `customTitle`，没有才查 `aiTitle`，用户改名永远优先
2. **Resume 安全**：`restoreSessionMetadata` 只缓存 `custom-title`，不缓存 `ai-title`，避免旧 AI 标题覆盖会话中途的用户改名
3. **CAS 语义**：AI 可以覆盖自己的旧 AI 标题，但永远不能覆盖用户标题
4. **指标区分**：AI 标题不触发 `tengu_session_renamed` 事件
5. **自然淘汰**：`ai-title` 不会被重写到文件尾部，自然被挤出 64KB tail 窗口

### 5.5 agent-dev 实现

#### TitleState 状态机

```python
class TitleState(Enum):
    NEED_TITLE   = "need_title"   # 无标题，等待生成
    AI_PENDING   = "ai_pending"   # AI 请求已发出，未返回
    AI_SET       = "ai_set"       # 第1条 AI 标题已设置（允许第3条重新生成）
    USER_SET     = "user_set"     # 用户手动改过，永久锁定
    FINALIZED    = "finalized"    # 第3条后锁定，不再自动生成
```

**状态转移图**：

```
NEED_TITLE ──(消息1)──→ AI_PENDING ──(AI返回)──→ AI_SET ──(消息3)──→ AI_PENDING ──(AI返回)──→ FINALIZED
                                                                            │                         │
                                                                     (用户改名)               (用户改名)
                                                                            ↓                         ↓
                                                                        USER_SET ←────────────────── USER_SET
```

#### 触发策略

| 时机 | 动作 | 输入 |
|------|------|------|
| 第1条用户消息 | `_derive_title()` 即时占位 + 异步 AI 生成 | 单条消息文本 |
| 第3条用户消息 | 异步 AI 重新生成（覆盖第1条） | 完整对话文本（`_extract_conversation_text`） |
| 第4条起 | 不触发 | — |
| 用户手动改名 | 写入 `custom-title`，永久锁定 | — |

#### agent-dev 标题 Prompt（中文适配版）

```python
TITLE_SYSTEM_PROMPT = (
    "生成一个简洁的会话标题（3-7个词），要求：\n"
    "1. 准确概括对话的主题或目标\n"
    "2. 只返回标题文本，不要引号、不要解释、不要多余内容\n"
    "3. 使用自然的中文表达\n"
    "\n"
    "好的示例：\n"
    "- 用户问候和自我介绍\n"
    "- 并行执行三个计算和搜索任务\n"
    "- 调试登录按钮无响应问题\n"
    "- 重构API客户端错误处理\n"
    "\n"
    "差的示例：\n"
    "- 问候时刻（太模糊，没有信息量）\n"
    "- 三个任务执行（太简略，缺少具体内容）\n"
    "- 代码修改（太泛，无法区分会话）\n"
    "- 调查并修复移动设备上登录按钮无响应的问题（太长）"
)
```

#### genSeq 防乱序实现

```python
async def _generate_ai_title(self, input_text: str, gen_seq: int) -> None:
    title = await asyncio.wait_for(
        self._call_llm_for_title(input_text),
        timeout=15.0,
    )
    if not title:
        return

    # 只有最新 genSeq 的结果才写入
    if gen_seq != self._gen_seq:
        return  # 旧请求晚返回，丢弃

    if self._title_state == TitleState.USER_SET:
        return  # 用户已改名，不覆盖

    self._save_ai_title(title, gen_seq)
```

#### 重启恢复（_restore_title_state）

从 JSONL tail 扫描，重建三个状态：

```python
def _restore_title_state(self):
    entries = self.storage.read_tail(kb=64)

    custom_title = None
    ai_title = None
    user_msg_count = 0
    max_gen_seq = 0

    for entry in reversed(entries):
        etype = entry.get("type")
        if etype == "custom-title" and custom_title is None:
            custom_title = entry
        elif etype == "ai-title" and ai_title is None:
            ai_title = entry
        elif etype == "user":
            content = entry.get("message", {}).get("content", "")
            if isinstance(content, str) and content:
                user_msg_count += 1

    # 恢复计数器
    self._user_msg_count = user_msg_count
    self._gen_seq = max(entry.get("genSeq", 1) for ... in ai_title_entries)

    # 决策优先级：custom-title > ai-title(genSeq≥2) > ai-title(genSeq=1) > NEED_TITLE
    if custom_title:
        self._title_state = TitleState.USER_SET
    elif ai_title:
        if ai_title.get("genSeq", 1) >= 2:
            self._title_state = TitleState.FINALIZED
        else:
            self._title_state = TitleState.AI_SET
    else:
        self._title_state = TitleState.NEED_TITLE
```

**恢复要点**：
- `genSeq=1`（仅首轮生成）→ `AI_SET`（允许第3条重新生成）
- `genSeq≥2`（已重新生成过）→ `FINALIZED`（锁定）
- `_user_msg_count` 从 JSONL 统计纯用户消息恢复（排除 tool_result）
- `_gen_seq` 取所有 ai-title 条目中的最大 genSeq 恢复

---

## 六、Streamlit 集成的特殊考量

### 6.1 脚本重跑模型

Streamlit 每次用户交互（点按钮、输入文本）都会**重跑整个脚本**。这意味着：

- `get_agent()` 可能在每次交互时被重新调用
- 新 `ReactAgent` → 新 `SessionManager` → `_restore_title_state()` 被触发
- 任何不持久化的内存状态（`_user_msg_count`、`_gen_seq`、`_title_state`）都会丢失

**解决方案**：所有标题相关状态都从 JSONL 恢复（见上文 `_restore_title_state`）。

### 6.2 Session ID 持久化

Streamlit 的 `st.session_state` 在 F5 刷新后丢失。使用 URL query param 持久化：

```python
# 从 URL 恢复 session_id
if "session" in st.query_params:
    st.session_state.chat_session_id = st.query_params["session"]
```

---

## 七、与 Claude Code 的对照分析

### 7.1 设计差异

| 维度 | Claude Code | agent-dev |
|------|-------------|-----------|
| **存储格式** | JSONL | JSONL ✅ 一致 |
| **Entry 结构** | uuid/parentUuid/sessionId/type/timestamp/message | ✅ 完全一致 |
| **tool_use 存储** | content 数组内嵌 | ✅ 一致（commit `0a5bba5` 重构） |
| **标题模型** | Haiku（Anthropic） | GLM-4-flash（智谱） |
| **标题存储** | ai-title / custom-title 双 Entry 类型 | ✅ 一致 |
| **genSeq 防乱序** | ✅ 闭包变量 | ✅ 实例变量 + JSONL 恢复 |
| **deriveTitle 存 JSONL** | ❌ 不存（发远程 API） | ❌ 不存（只在内存缓存） |
| **64KB Tail + Head 双窗口** | ✅ tail 优先，head 回退 | ✅ 一致（commit `114043f`） |
| **reAppendSessionMetadata** | ✅ resume 时 custom-title 重写到尾部 | ✅ 一致（commit `114043f`） |
| **sessions-index.json** | ✅ 缓存加速层 | ❌ 未实现（直接扫描目录） |
| **last-prompt Entry** | ✅ 每轮覆盖，200字截断 | ❌ 未实现 |
| **task-summary Entry** | ✅ `claude ps` 命令用 | ❌ 未实现 |

### 7.2 Claude Code 设计精髓总结

1. **Append-Only 是基石**：所有操作都是追加，永不修改已有数据。崩溃安全、可回溯、无锁
2. **类型即元数据**：用 Entry type 区分数据种类，避免一个字段存多种语义
3. **信封套信纸**：存储层只管 Entry 信封，message 字段是只读的 API 原始对象
4. **Tail 窗口 + 自然淘汰**：旧数据被挤出窗口自然消失，不需要主动清理
5. **双 Entry 标题类型**：ai-title 和 custom-title 分开存储，类型即优先级
6. **genSeq 防乱序**：异步请求可能乱序返回，序号确保只有最新结果生效
7. **fire-and-forget**：标题生成在后台进行，永不阻塞主流程
8. **两层触发 + 永久锁定**：第1条生成占位，第3条用完整对话重新生成，用户改名后永久锁定

---

## 八、待补齐功能（按优先级）

| 优先级 | 功能 | 说明 |
|--------|------|------|
| P0 | F5 刷新后历史消息显示 | 修复 web/app.py 的消息加载逻辑 |
| P1 | `last-prompt` Entry | 最近用户输入（200字截断），用于会话列表预览 |
| P2 | 上下文管理模块 | Token 预算 + 动态压缩 + summary Entry |
| P2 | `task-summary` Entry | 周期性任务摘要，用于会话列表展示 |
| P3 | `tag` / `agent-name` | 会话标签和 Agent 类型标记 |
| P3 | `sessions-index.json` | 缓存加速层，避免每次扫描目录 |
| P3 | `file-history-snapshot` | 文件变更快照，用于进度追踪 |

---

## 九、测试覆盖

当前 18 个测试覆盖：

| 测试 | 覆盖范围 |
|------|---------|
| `test_parent_uuid_chain` | 消息链完整性 |
| `test_compact_boundary` | 压缩断链 |
| `test_resume_and_continue` | Resume/Continue 语义 |
| `test_fork` | Fork 分支 |
| `test_metadata` | 元数据读写 |
| `test_state_machine` | 状态机 |
| `test_progress_tracker` | 进度追踪 |
| `test_jsonl_storage_details` | JSONL 存储细节 |
| `test_list_and_delete` | 列表/删除 |
| `test_cleanup` | 清理归档 |
| `test_integration_full_workflow` | 端到端集成 |
| `test_title_state_machine` | 标题状态机 |
| `test_title_user_rename` | 用户改名锁定 |
| `test_title_restore_on_resume` | 重启恢复（AI_SET / FINALIZED / USER_SET） |
| `test_title_user_msg_count_restore` | _user_msg_count 恢复 |
| `test_title_derive_title` | 即时占位 |
| `test_title_genseq_out_of_order` | genSeq 防乱序 |
| `test_title_entry_format` | Entry 格式 |

---

## 附录 A：Claude Code 源码索引

| 功能 | 源码文件 | 关键函数 |
|------|---------|---------|
| 标题生成 | `utils/sessionTitle.ts` | `generateSessionTitle()`, `extractConversationText()` |
| 标题触发 | `bridge/initReplBridge.ts` | `onUserMessage()`, `generateAndPatch()`, `deriveTitle()` |
| 标题存储 | `utils/sessionStorage.ts` | `saveAiGeneratedTitle()`, `reAppendSessionMetadata()` |
| 会话列表 | `utils/sessionStoragePortable.ts` | `listSessionsImpl()`, `readLiteMetadata()` |
| 会话恢复 | `sessionRestore.ts` | `restoreSession()`, `resetSessionFilePointer()` |
| 压缩 | `utils/compaction.ts` | `compactConversation()`, `createCompactSummary()` |

> ⚠️ 源码路径：`/Users/fanyunxu/Desktop/myproject/ailearning/claude-code-analysis/`（完整可读版，非 minified）

---

## 附录 B：agent-dev 代码索引

| 功能 | 文件 | 关键类/函数 |
|------|------|------------|
| 存储层 | `agent_core/session/storage.py` | `SessionStorage`, `append_entry()`, `read_tail()`, `flush()` |
| 管理层 | `agent_core/session/manager.py` | `SessionManager`, `TitleState`, `_on_user_message()`, `_restore_title_state()` |
| 元数据 | `agent_core/session/metadata.py` | `SessionMetadata`, `update_ai_title()`, `update_title()` |
| 状态机 | `agent_core/session/state.py` | `SessionState` |
| 进度 | `agent_core/session/progress.py` | `ProgressTracker` |
| 恢复 | `agent_core/session/restore.py` | `resume_session()`, `continue_session()`, `fork_session()` |
| 清理 | `agent_core/session/cleanup.py` | `SessionCleanup` |
| 测试 | `agent_core/session/test_session.py` | 18 个测试 |
| UI 集成 | `web/app.py` | `get_agent()`, 会话管理侧边栏 |
