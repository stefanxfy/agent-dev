# LangGraph vs 自研：会话·上下文·记忆系统实现差异

> 基于 Claude Code 源码设计，对比 agent_core（自研 ReAct）和 langgraph_agent（LangGraph）
> 两种架构下三大子系统的实现差异
>
> 版本：v1.0 | 日期：2026-06-11

---

## 一、核心架构对比

### 1.1 自研（agent_core）

```
用户请求
    │
    ▼
┌─────────────────────────────────────────────────────────┐
│  agent_core.py（手写 ReAct 循环）                          │
│                                                          │
│  ┌──────────────────────────────────────────────────┐  │
│  │ self.history = []                                 │  │
│  │ self.session   → SessionStorage (JSONL)           │  │
│  │ self.context   → ContextManager (Token预算)        │  │
│  │ self.memory    → MemoryStore (记忆)                │  │
│  └──────────────────────────────────────────────────┘  │
│                                                          │
│  while True:                                            │
│      msg = llm.chat(messages)                          │
│      if msg.tool_calls:                                │
│          results = execute_tools(msg.tool_calls)        │
│          messages += results                            │
│      else:                                             │
│          break                                         │
└─────────────────────────────────────────────────────────┘
```

**特点**：
- 循环逻辑全手写，流程透明
- 三大系统作为 `agent_core.py` 的成员变量注入
- 所有状态手动管理（append / trim / compact / extract）

### 1.2 LangGraph（langgraph_agent）

```
用户请求
    │
    ▼
┌─────────────────────────────────────────────────────────┐
│  graph.py（StateGraph 状态机）                            │
│                                                          │
│  nodes: [llm_node, tool_node, compact_node, ...]       │
│  edges: conditional_edge（根据 state 判断路由）            │
│                                                          │
│  ┌──────────────────────────────────────────────────┐  │
│  │ state = AgentState {                             │  │
│  │   messages: BaseMessage[],    ← checkpointer持久化  │  │
│  │   turn: int,                                      │  │
│  │   system_prompt: str,                            │  │
│  │   memory: dict,                ← 记忆注入           │  │
│  │   budget: BudgetState,       ← Token预算          │  │
│  │ }                                                │  │
│  └──────────────────────────────────────────────────┘  │
│                                                          │
│  SqliteSaver / MemorySaver（自动 checkpoint）            │
└─────────────────────────────────────────────────────────┘
```

**特点**：
- 循环逻辑由 StateGraph 的 conditional edge 驱动
- 状态在节点间自动传递，无需手动管理
- Checkpointer 自动持久化每个节点的状态快照

---

## 二、会话管理实现差异

### 2.1 存储格式

| 维度 | 自研（agent_core） | LangGraph（langgraph_agent） |
|------|-------------------|---------------------------|
| **格式** | JSONL（纯文本，逐行追加） | SQLite（结构化数据库） |
| **文件** | `~/.agent_data/sessions/<uuid>.jsonl` | `sessions.db` + `checkpoints.db` |
| **并发写入** | Append-only 天然安全 | WAL 模式，`check_same_thread=False` |
| **损坏影响** | 单条 entry 损坏不影响其他 | SQLite 单文件，损坏可能影响全部 |
| **文件大小** | 线性增长，单文件可能很大 | SQLite 自动管理 page |
| **尾部读取** | `tail -c 64KB` O(1) | `SELECT * LIMIT N` 需索引 |
| **迁移** | 纯文本，容易版本控制 | 需要 `sqlite3 .dump` |

### 2.2 Session ID 管理

**自研：**

```python
# session/storage.py
class SessionStorage:
    def __init__(self, session_id=None):
        self.session_id = session_id or str(uuid.uuid4())
        self.jsonl_path = self.data_dir / f"{self.session_id}.jsonl"

    def append_entry(self, entry: dict) -> str:
        entry["uuid"] = str(uuid.uuid4())
        entry["sessionId"] = self.session_id
        entry["parentUuid"] = self._last_uuid  # parentUuid 链
        self._last_uuid = entry["uuid"]
        # JSONL 追加
        with open(self.jsonl_path, "a") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
```

**LangGraph：**

```python
# agent.py
class LangGraphAgent:
    def __init__(self, ...):
        self._thread_id = self._create_thread_in_db("新会话")  # "thread_1"

    def _create_thread_in_db(self, name: str) -> str:
        self._thread_counter += 1
        new_id = f"thread_{self._thread_counter}"
        # 写入 SQLite
        with sqlite3.connect(str(self._db_path)) as conn:
            conn.execute("""
                INSERT INTO thread_meta (thread_id, name, created_at, updated_at, message_count)
                VALUES (?, ?, ?, ?, ?)
            """, (new_id, name, time.time(), time.time(), 0))
        return new_id

    def switch_thread(self, thread_id: str):
        self._thread_id = thread_id  # 直接切换
        # LangGraph checkpointer 自动切换
```

**差异**：
- 自研 UUID 随机，无顺序语义
- LangGraph 用 `thread_N` 计数器，有顺序但无 UUID 语义
- LangGraph 的 `thread_id` 直接对应 checkpointer 的 key

### 2.3 消息持久化

**自研：手动追加到 JSONL**

```python
# agent_core.py - run() 中
def run(self, user_message: str):
    self.session.append_entry({
        "type": "user",
        "role": "user",
        "content": user_message,
    })
    self.context.add_message("user", user_message)

    for chunk in self.llm.chat(messages):
        # ... 处理 chunk

    # 写回
    self.session.append_entry({
        "type": "assistant",
        "role": "assistant",
        "content": full_text,
    })
    if tool_calls:
        for tc in tool_calls:
            self.session.append_entry({...})  # tool_use + tool_result
```

**LangGraph：节点间自动传递，checkpointer 自动持久化**

```python
# nodes.py
def llm_node(state: AgentState) -> AgentState:
    messages = state["messages"]
    # system_prompt 自动注入（每次节点调用时）
    if messages and messages[0].type != "system":
        messages = [SystemMessage(content=state["system_prompt"])] + messages

    response = llm.invoke(messages)
    # 返回新 state，LangGraph 自动合并到 checkpointer
    return {"messages": messages + [response]}

def tool_node(state: AgentState) -> AgentState:
    tool_calls = state["messages"][-1].tool_calls
    results = execute_tools(tool_calls)
    return {
        "messages": state["messages"] + [
            ToolMessage(content=json.dumps(r), tool_call_id=tc.id)
            for tc, r in zip(tool_calls, results)
        ]
    }
```

**差异**：
- 自研：每一步都要手动调用 `session.append_entry()`
- LangGraph：节点返回新 state → LangGraph 自动 checkpoint，无需手动写入
- LangGraph 的持久化是**每个节点调用后自动触发**，不漏任何状态
- 自研的持久化是**手动控制**，有漏写的风险但有更大的控制权

### 2.4 Resume / Continue / Fork

**自研：读取 JSONL + 重建消息链**

```python
# session/restore.py
def resume_session(session_id: str) -> tuple[list[dict], SessionMetadata]:
    storage = SessionStorage(session_id=session_id)
    entries = storage.read_entries()

    # 断链检测：compact_boundary 处停止
    messages = []
    for entry in entries:
        if entry.get("type") == "compact-boundary":
            break
        if entry.get("parentUuid") is None:
            # 新的消息链起点
            messages = []
        messages.append(entry)

    # 从尾部恢复元数据
    tail = storage.read_tail()
    metadata = SessionMetadata.from_tail(tail)

    return messages, metadata
```

**LangGraph：checkpointer 自动恢复**

```python
# agent.py
def run(self, user_message: str):
    config = {"configurable": {"thread_id": self._thread_id}}

    # LangGraph 自动从 checkpointer 恢复 state
    # 无需手动读取文件，get_state 即可
    state = self._graph.get_state(config)
    if state and state.values.get("messages"):
        # 已有历史，继续
        initial_state = {
            "messages": state.values["messages"] + [HumanMessage(content=user_message)],
            ...
        }
    else:
        # 新会话
        initial_state = {"messages": [HumanMessage(content=user_message)], ...}

    for chunk in self._graph.stream(initial_state, config, ...):
        ...
```

**差异**：
- 自研：Resume 需要读取 JSONL 文件，手动解析 parentUuid 链，断链处停止
- LangGraph：Resume 是 `get_state(config)` 一行代码，checkpointer 完整恢复（包括所有中间状态）
- LangGraph Resume 包含**完整 state**（turn、budget 等），自研需要手动恢复所有字段

### 2.5 Fork 会话

**自研：复制 JSONL + 新 UUID**

```python
# session/restore.py
def fork_session(parent_session_id: str) -> str:
    parent = SessionStorage(session_id=parent_session_id)
    entries = parent.read_entries()

    # 生成新会话
    new_storage = SessionStorage()
    for entry in entries:
        # parentUuid 链断开？不，保留链用于追溯
        new_entry = entry.copy()
        new_entry["uuid"] = str(uuid.uuid4())
        new_entry["sessionId"] = new_storage.session_id
        # 不继承 worktree_state（危险状态）
        if new_entry.get("type") != "worktree-state":
            new_storage.append_entry(new_entry)

    return new_storage.session_id
```

**LangGraph：新建 thread_id，checkpointer 隔离**

```python
# agent.py
def fork_session(self, parent_thread_id: str) -> str:
    # 新建 thread_id
    new_thread_id = self._create_thread_in_db(f"Fork of {parent_thread_id}")
    # LangGraph checkpointer 自动隔离（新 thread_id 不共享历史）
    return new_thread_id
```

**差异**：
- 自研 Fork 复制消息历史到新 JSONL，parentUuid 链保留，worktree_state 不复制
- LangGraph Fork 只是新建 `thread_id`，checkpointer 各自独立，无历史共享
- **LangGraph 无法实现"Fork 继承父会话消息历史但断开 worktree_state"**——要么全有，要么全无

### 2.6 清理归档

**自研：扫描 .jsonl 文件 + TTL 策略**

```python
# session/cleanup.py
class SessionCleanup:
    def cleanup_by_ttl(self, max_age_days=30):
        cutoff = datetime.now() - timedelta(days=max_age_days)
        for f in self.data_dir.glob("*.jsonl"):
            mtime = datetime.fromtimestamp(f.stat().st_mtime)
            if mtime < cutoff:
                f.unlink()  # 删除

    def archive_session(self, session_id: str):
        src = self.data_dir / f"{session_id}.jsonl"
        dst = self.archive_dir / f"{session_id}.jsonl.gz"
        # gzip 压缩归档
        with open(src, 'rb') as fi, gzip.open(dst, 'wb') as fo:
            shutil.copyfileobj(fi, fo)
        src.unlink()
```

**LangGraph：SQLite 查询 + VACUUM**

```python
# agent.py
def cleanup_old_threads(self, max_age_days=30):
    cutoff = time.time() - max_age_days * 86400
    with sqlite3.connect(str(self._db_path)) as conn:
        old_threads = conn.execute(
            "SELECT thread_id FROM thread_meta WHERE updated_at < ?",
            (cutoff,)
        ).fetchall()
        for (tid,) in old_threads:
            self.delete_thread(tid)
    # VACUUM 回收空间
    conn.execute("VACUUM")
```

**差异**：
- 自研：文件系统操作（glob / unlink / gzip），归档可压缩到 archive 目录
- LangGraph：SQL 查询 + DELETE，SQLite 空间用 VACUUM 回收

---

## 三、上下文管理实现差异

### 3.1 Token 预算管理

**自研：独立 ContextManager 类**

```python
# context/manager.py
class ContextManager:
    def __init__(self, budget: int = 100_000):
        self.budget = budget
        self.chain = MessageChain()

    def add_message(self, role, content):
        uuid = self.chain.add(role, content)
        return uuid

    def should_compact(self) -> bool:
        used = sum(self.estimate_tokens(m) for m in self.chain.messages)
        return (self.budget - used) <= self.budget * 0.20

    def compact(self, llm_router):
        summary_text = self._generate_summary(llm_router)
        self.chain.add_summary({"summary": summary_text, "tokens": ...})
        self.chain.add_compact_boundary()
        # 清理旧消息
        self.chain.truncate_from_head()

    def estimate_tokens(self, msg: dict) -> int:
        text = msg.get("content", "")
        chinese = sum(1 for c in text if '\u4e00' <= c <= '\u9fff')
        english = len(text) - chinese
        return int(chinese * 1.4 + english * 0.25 + 10)
```

**LangGraph：写入 AgentState.budget，节点中检查**

```python
# state.py
class AgentState(TypedDict):
    messages: Annotated[list[BaseMessage], add_messages]
    turn: int
    max_turns: int
    system_prompt: str
    total_tokens: int          # 当前已用 Token
    budget_tokens: int         # 预算（默认 100_000）
    pending_summary: str | None  # 压缩后待写入的摘要
```

```python
# nodes.py - 嵌入在节点中
def llm_node(state: AgentState) -> AgentState:
    used = state.get("total_tokens", 0)
    budget = state.get("budget_tokens", 100_000)

    # 检查是否需要压缩
    if budget - used <= budget * 0.20:
        # 触发压缩边
        return {"pending_summary": "TRIGGER_COMPACT"}

    # 正常调用
    response = llm.invoke(messages)
    token_count = response.usage.total_tokens if hasattr(response, 'usage') else 0
    return {
        "messages": [response],
        "total_tokens": used + token_count,
    }
```

**差异**：
- 自研：独立的 `ContextManager` 类，与 agent_core 解耦，可单独测试
- LangGraph：Token 预算嵌入 `AgentState`，分散在节点逻辑中，需要在节点间传递
- 自研的 `should_compact()` 是显式方法调用，LangGraph 需要 conditional edge 路由

### 3.2 压缩触发

**自研：ReAct 循环中显式调用**

```python
# agent_core.py - run() 中
for chunk in self.llm.chat(messages):
    ...

# ReAct 循环结束后检查
if self.context.should_compact():
    self.context.compact(self.llm)
    self.session.append_entry(self.context.get_summary_entry())
```

**LangGraph：conditional edge 路由**

```python
# graph.py
def should_compact(state: AgentState) -> str:
    used = state.get("total_tokens", 0)
    budget = state.get("budget_tokens", 100_000)
    if budget - used <= budget * 0.20:
        return "compact"
    if state.get("turn", 0) >= state.get("max_turns", 10):
        return "end"
    if state.get("messages", []) and state["messages"][-1].tool_calls:
        return "tools"
    return "llm"

graph = StateGraph(AgentState)
graph.add_node("llm", llm_node)
graph.add_node("tools", tool_node)
graph.add_node("compact", compact_node)  # 新增压缩节点
graph.add_node("end", end_node)

graph.add_conditional_edges(
    "__start__",
    should_compact,
    {
        "llm": "llm",
        "tools": "tools",
        "compact": "compact",
        "end": "end",
    }
)
graph.add_conditional_edges(
    "llm",
    should_compact,  # llm 节点返回后也检查
    {...}
)
```

**差异**：
- 自研：压缩在 ReAct 循环中顺序执行，`compact()` → 写 JSONL → 继续循环
- LangGraph：压缩是一个独立的 graph **节点**，通过 conditional edge 路由进入，状态自动 checkpoint
- LangGraph 的压缩节点可以和其他节点**并行**（理论上），自研是串行的

### 3.3 压缩策略实现

**自研：独立的 compact.py**

```python
# context/compact.py
def compact_BASE(messages: list[dict]) -> dict:
    """截断早期消息 + LLM 生成摘要"""
    truncated = messages[-int(len(messages) * 0.8):]  # 保留最近 80%
    summary_text = generate_summary_via_llm(truncated)

    return {
        "type": "summary",
        "summary": summary_text,
        "truncated_count": len(messages) - len(truncated),
        "tokens_saved": estimate_tokens(messages) - estimate_tokens(truncated),
        "format": "BASE",
    }

def compact_PARTIAL(messages: list[dict]) -> dict:
    """保留首尾，中间压缩"""
    if len(messages) <= 4:
        return compact_BASE(messages)
    first = messages[:2]  # 前2条（通常 system + 第一个 user）
    last = messages[-2:]  # 后2条
    middle = messages[2:-2]
    middle_summary = generate_summary_via_llm(middle)

    return {
        "type": "summary",
        "summary": f"[保留开头]\n{format_messages(first)}\n\n[中间部分摘要]\n{middle_summary}\n\n[保留结尾]\n{format_messages(last)}",
        "format": "PARTIAL",
    }

def inject_verbatim_quotes(summary: dict, messages: list[dict]) -> dict:
    """注入 verbatim quotes 防漂移（Claude Code 机制）"""
    verbatim_quotes = [
        m for m in messages
        if m.get("role") == "user" and len(m.get("content", "")) > 50
    ][:3]  # 最多3条
    summary["verbatim_quotes"] = [
        m["content"][:200] for m in verbatim_quotes
    ]
    return summary
```

**LangGraph：compact_node 实现**

```python
# nodes.py
def compact_node(state: AgentState) -> AgentState:
    """压缩节点"""
    messages = state["messages"]

    # 根据压缩模式选择策略
    mode = state.get("compact_mode", "BASE")

    if mode == "BASE":
        truncated = messages[-int(len(messages) * 0.8):]
        summary_text = _llm_summarize(truncated)
        new_messages = _build_summary_messages(summary_text, messages)
    elif mode == "PARTIAL":
        # 保留首尾
        first = messages[:2]
        last = messages[-2:]
        middle = messages[2:-2]
        middle_summary = _llm_summarize(middle)
        new_messages = _build_partial_summary(first, middle_summary, last, messages)
    else:  # UP_TO
        target = state.get("target_tokens", 50000)
        new_messages = _compact_to_target(messages, target)

    # 注入 verbatim quotes
    new_messages = _inject_verbatim(new_messages, messages)

    return {
        "messages": new_messages,
        "pending_summary": None,
        "total_tokens": _estimate_tokens(new_messages),
    }

def _llm_summarize(messages: list[BaseMessage]) -> str:
    """调用 LLM 生成 9 段式摘要"""
    prompt = f"""请压缩以下对话，生成 9 段式摘要：

1. 会话概要（一句话）
2. 用户目标
3. 关键决策
4. 当前工作状态
5. 重要上下文
6. 文件变更
7. 待完成任务
8. 工具使用记录
9. 已知偏好/约束

对话内容：
{messages_to_text(messages)}

请按上述格式输出摘要。"""
    response = llm.invoke([HumanMessage(content=prompt)])
    return response.content
```

**差异**：
- 自研：压缩是 ContextManager 的方法，接收 `list[dict]`，输出 dict（summary entry）
- LangGraph：压缩是 StateGraph 的节点，接收/返回 `AgentState`，消息格式是 `BaseMessage`
- 自研的压缩输出直接写 JSONL，LangGraph 的压缩输出通过 `add_messages` reducer 合并到 state
- LangGraph 的 `add_messages` reducer 会**追加**消息，不会自动去重或处理 summary 语义

### 3.4 消息链（parentUuid）

**自研：每条 entry 带 parentUuid**

```python
# context/chain.py
class MessageChain:
    def add(self, role, content) -> str:
        uuid = str(uuid.uuid4())
        entry = {
            "uuid": uuid,
            "parentUuid": self._last_uuid,  # 指向上一条
            "sessionId": self.session_id,
            "role": role,
            "content": content,
            "timestamp": datetime.now().isoformat(),
        }
        self.messages.append(entry)
        self._last_uuid = uuid
        return uuid

    def add_compact_boundary(self):
        """压缩边界：断开 parentUuid 链"""
        self.messages.append({
            "uuid": str(uuid.uuid4()),
            "parentUuid": None,  # 断链标记
            "type": "compact-boundary",
            "sessionId": self.session_id,
        })
        self._last_uuid = None  # 重置，下一条消息将是新的链起点
```

**LangGraph：无 parentUuid，依赖 checkpoint 快照**

```python
# LangGraph 的消息链不是链式的，是全量快照
# 每个 checkpoint = AgentState 的完整快照
# 压缩 = 生成 summary message 并替换旧消息

# nodes.py
def compact_node(state: AgentState) -> AgentState:
    # 生成摘要
    summary = _llm_summarize(state["messages"])
    # 构造摘要消息（作为普通 AIMessage）
    summary_msg = AIMessage(
        content=f"[压缩摘要]\n{summary}",
        id=f"summary-{uuid.uuid4().hex[:8]}"
    )
    # 保留最近的 N 条消息
    keep_count = 4
    new_messages = [summary_msg] + state["messages"][-keep_count:]
    return {"messages": new_messages}

# 问题：没有 parentUuid，无法区分"压缩边界"和"普通消息"
# 只能通过 message.id 前缀判断（约定俗成，非协议）
```

**差异**：
- 自研：`parentUuid` 是**显式链式结构**，每条消息指向上一条，断链处 `parentUuid = null`
- LangGraph：**无链式结构**，只有 `BaseMessage.id`（随机 UUID），压缩边界无法精确标识
- 自研可以从 JSONL 精确重建"哪条消息在压缩前，哪条在压缩后"
- LangGraph 的 checkpoint 是**完整 state 快照**，resume 时恢复整个 state，无法做到"断链处停止"

### 3.5 压缩边界语义

| 语义 | 自研实现 | LangGraph 实现 |
|------|---------|--------------|
| 标识压缩边界 | `{type: "compact-boundary", parentUuid: null}` | 无等价物，只能约定 `message.id.startswith("summary-")` |
| 断链处停止读取 | Resume 遇到 `parentUuid = null` 停止 | 无断链语义，Resume 全量恢复 |
| 摘要 + 后续消息 | 摘要 + 后续消息 = 完整上下文 | 摘要替换旧消息，后续消息在摘要之后 |
| 压缩历史可追溯 | JSONL 中可见所有压缩记录 | checkpointer 只保留最新快照，历史不可见 |
| Fork 时压缩边界 | 继承压缩边界 entry | 无压缩边界概念 |

---

## 四、记忆系统实现差异

### 4.1 记忆提取

**自研：独立 MemoryExtractor 类，接收 list[dict]**

```python
# memory/extract.py
class MemoryExtractor:
    """从会话消息中提取四类记忆"""
    SYSTEM_PROMPTS = {
        "user": """你是一个记忆提取助手。
从对话中提取用户偏好，格式如下：
## 用户偏好
- 偏好: <描述>""",
        "feedback": """你是一个记忆提取助手。
从对话中提取反馈和纠正，格式如下：
## 反馈纠正
- 纠正: <描述>""",
        "project": """你是一个记忆提取助手。
从对话中提取项目知识，格式如下：
## 项目知识
- 知识: <描述>""",
    }

    def extract(self, messages: list[dict]) -> dict[str, list[str]]:
        texts = [m.get("content", "") for m in messages if isinstance(m.get("content"), str)]
        combined = "\n\n".join(texts[-20:])  # 最近20条

        return {
            "user": self._extract_via_llm(combined, "user"),
            "feedback": self._extract_via_llm(combined, "feedback"),
            "project": self._extract_via_llm(combined, "project"),
        }

    def _extract_via_llm(self, text: str, category: str) -> list[str]:
        prompt = self.SYSTEM_PROMPTS[category] + f"\n\n对话内容：\n{text}"
        response = self.llm.invoke([HumanMessage(content=prompt)])
        return self._parse_extraction(response.content, category)
```

**LangGraph：嵌入 end_node 或 finalize_node**

```python
# nodes.py
def memory_extract_node(state: AgentState) -> AgentState:
    """会话结束时提取记忆"""
    messages = state["messages"]

    # 三重门检查
    if not _should_extract(messages):
        return state

    # 提取四类记忆
    texts = "\n\n".join([m.content for m in messages if hasattr(m, "content")])
    memory_categories = ["user", "feedback", "project", "reference"]
    extracted = {}

    for cat in memory_categories:
        prompt = MEMORY_EXTRACT_PROMPTS[cat].format(text=texts)
        response = llm.invoke([HumanMessage(content=prompt)])
        extracted[cat] = _parse_extraction(response.content, cat)

    # 写入记忆文件
    store = MemoryStore(project_slug=state.get("project_slug", "default"))
    for cat, items in extracted.items():
        store.append(cat, items)

    return {"memory_extracted": True, **state}
```

**差异**：
- 自研：独立类，`agent_core.finalize()` 中显式调用，注入时机完全可控
- LangGraph：作为 StateGraph 的**节点**，通过 conditional edge 路由进入，时机由 graph 控制
- 自研的提取时机是"会话结束"，LangGraph 的提取时机由 `should_end(state)` 决定

### 4.2 记忆存储

**自研：JSONL/JSON + JSONL append**

```python
# memory/store.py
class MemoryStore:
    def __init__(self, project_slug: str):
        self.memory_dir = Path(f".agent_data/projects/{project_slug}/memory")
        self.memory_dir.mkdir(parents=True, exist_ok=True)

    def append(self, category: str, memories: list[str]):
        path = self.memory_dir / f"{category}.md"
        now = datetime.now().strftime("%Y-%m-%d %H:%M")

        lines = [f"\n## [{now}]"]
        for m in memories:
            lines.append(f"- {m}")

        with open(path, "a") as f:
            f.write("\n".join(lines) + "\n")

    def load(self, category: str) -> list[str]:
        path = self.memory_dir / f"{category}.md"
        if not path.exists():
            return []
        return self._parse_memory_file(path.read_text())

    def _parse_memory_file(self, content: str) -> list[str]:
        """解析 Markdown 记忆文件"""
        items = []
        for line in content.split("\n"):
            if line.strip().startswith("- "):
                items.append(line.strip()[2:])
        return items
```

**LangGraph：同样文件系统操作，无差异**

```python
# LangGraph 版本的 MemoryStore 与自研完全相同
# 差异在于注入时机和调用路径
```

**差异**：存储层无本质差异，都是文件系统操作。差异在于调用路径（自研在 `finalize()` 中，LangGraph 在 `memory_extract_node` 中）。

### 4.3 记忆加载到 System Prompt

**自研：启动时注入**

```python
# agent_core.py - ReactAgent.__init__
def __init__(self, ...):
    project_slug = self._detect_project_slug()
    memories = load_memories(project_slug)

    self.system_prompt = inject_into_system_prompt(
        self.llm.config.system_prompt,
        memories
    )

def inject_into_system_prompt(base: str, memories: str) -> str:
    if not memories:
        return base
    return base + "\n\n" + MEMORY_SECTION.format(memories=memories)

MEMORY_SECTION = """
## 【跨会话记忆】
以下是你之前从该用户学到的偏好和知识：

{memories}

请在后续对话中遵循这些偏好。
"""
```

**LangGraph：写入 AgentState.system_prompt**

```python
# state.py
class AgentState(TypedDict):
    system_prompt: str  # 包含记忆的完整 system_prompt
    memory_loaded: bool  # 标记是否已加载

# nodes.py
def memory_load_node(state: AgentState) -> AgentState:
    if state.get("memory_loaded"):
        return state

    memories = load_memories(state.get("project_slug", "default"))
    enriched_prompt = inject_into_system_prompt(
        state["system_prompt"],
        memories
    )
    return {
        "system_prompt": enriched_prompt,
        "memory_loaded": True,
    }

# graph.py
graph = StateGraph(AgentState)
graph.add_node("memory_load", memory_load_node)
graph.add_node("llm", llm_node)
# 每次会话开始先加载记忆
graph.add_edge("memory_load", "llm")
```

**差异**：
- 自研：System Prompt 在 `__init__` 中构造一次，固定不变
- LangGraph：System Prompt 在 `memory_load_node` 节点中动态构造，**每个会话开始时执行一次**
- LangGraph 的记忆加载是 graph 的第一个节点，自研的注入是构造时的静态字符串

### 4.4 autoDream 三重门

**自研：独立的 should_extract 检查**

```python
# memory/extract.py
class MemoryExtractor:
    def should_extract(self) -> bool:
        """三重门检查"""
        age_ok = self._check_age()         # ≥24h 未提取
        session_ok = self._check_session_count()  # ≥5 次会话
        lock_ok = not self._is_locked()    # 无锁

        return age_ok and session_ok and lock_ok

    def _check_age(self) -> bool:
        last_file = self.memory_dir / ".last_extraction"
        if not last_file.exists():
            return True
        last = datetime.fromisoformat(last_file.read_text())
        return datetime.now() - last >= timedelta(hours=24)

    def _check_session_count(self) -> bool:
        count_file = self.memory_dir / ".session_count"
        if not count_file.exists():
            return False
        return int(count_file.read_text()) >= 5

    def _is_locked(self) -> bool:
        return (self.memory_dir / ".lock").exists()
```

**LangGraph：嵌入状态检查**

```python
# nodes.py
def memory_extract_node(state: AgentState) -> AgentState:
    # 三重门
    age_ok = _check_extraction_age(state) >= 24  # 小时
    session_ok = state.get("session_count", 0) >= 5
    lock_ok = not state.get("memory_lock", False)

    if not (age_ok and session_ok and lock_ok):
        return state  # 不提取

    # 执行提取...
    return {"memory_extracted": True, "last_extraction": time.time(), **state}
```

**差异**：逻辑相同，实现位置不同（自研在类方法中，LangGraph 在节点中）。

---

## 五、三大系统对比总表

### 会话管理

| 功能 | 自研 | LangGraph |
|------|------|----------|
| JSONL / SQLite | JSONL ✅ | SQLite ✅ |
| parentUuid 链 | ✅ 完整实现 | ❌ 无 |
| 延迟创建文件 | ✅ | ❌ SQLite 无 |
| append-only | ✅ | ❌ WAL 模式 |
| 写队列 + 批量刷新 | ✅ | ❌ checkpointer 自动 |
| Session ID 管理 | UUID | `thread_N` |
| Resume | 读取 JSONL + 重建链 | `get_state(config)` 一行 |
| Continue | 增量加载尾部 | checkpointer 自动 |
| Fork | 复制消息 + 断开 worktree_state | 新建 thread_id（全有/全无） |
| 并发会话列表 | 扫描 .jsonl | SQLite SELECT |
| 切换会话 | `switch_session()` | `self._thread_id = tid` |
| TTL 清理 | glob + unlink | SQL DELETE + VACUUM |
| 归档 | gzip 压缩 | ❌ 无归档 |
| 元数据持久化 | JSONL entry | SQLite 表 |
| 原子性 | rename 临时文件 | SQLite 事务 |

### 上下文管理

| 功能 | 自研 | LangGraph |
|------|------|----------|
| Token 预算监控 | `ContextManager` 类 | `AgentState.total_tokens` |
| 精确 Token 估算 | 中文 1.4 / 英文 0.25 | 同 |
| 压缩触发 | `should_compact()` 方法 | `conditional_edge` 路由 |
| BASE 压缩 | ✅ `compact.py` | ✅ `compact_node` |
| PARTIAL 压缩 | ✅ | ✅ |
| UP_TO 压缩 | ✅ | ✅ |
| 9 段式摘要格式 | ✅ | ✅ |
| verbatim quotes | ✅ | ✅ |
| 消息链（parentUuid） | ✅ 显式链 | ❌ 无 |
| 断链检测 | ✅ `parentUuid = null` | ❌ 无等价物 |
| 状态重建 | 摘要 + 后续精确重建 | 全量 snapshot |
| Fork 继承历史 | ✅ 精确控制 | ❌ 全有/全无 |
| 与 ReAct 集成 | 替换 `_trim_history()` | 新增节点 + conditional edge |

### 记忆系统

| 功能 | 自研 | LangGraph |
|------|------|----------|
| 四类记忆提取 | ✅ `MemoryExtractor` | ✅ `memory_extract_node` |
| 偏好提取 | ✅ | ✅ |
| 反馈纠正提取 | ✅ | ✅ |
| 项目知识提取 | ✅ | ✅ |
| 参考资料提取 | ✅ | ✅ |
| 记忆文件写入 | `memory/*.md` | 同 |
| 增量追加 | ✅ append | ✅ append |
| MEMORY.md 索引 | ✅ | ✅ |
| 加载到 System Prompt | `__init__` 静态注入 | `memory_load_node` 动态注入 |
| autoDream 三重门 | ✅ | ✅ |
| 会话结束触发 | `finalize()` | `memory_extract_node` |

---

## 六、关键权衡

### 6.1 parentUuid 链 vs Checkpoint 快照

这是两种架构的**根本性差异**：

```
自研 parentUuid 链：
  entry_1 ──parentUuid──▶ entry_2 ──parentUuid──▶ entry_3
                                              │
                   ┌─compact-boundary──▶ entry_4 ──parentUuid──▶ entry_5
                   │parentUuid=null（断链）
                   ▼
  重建：从 entry_4 开始（断链处停止）

LangGraph Checkpoint 快照：
  checkpoint_A = {messages: [msg1, msg2, msg3]}
  checkpoint_B = {messages: [msg1, msg2, msg3, msg4, msg5]}  ← 全量替换
  checkpoint_C = {messages: [summary, msg4, msg5]}             ← 全量替换
                  ↑ 无法区分 msg4 是压缩前还是压缩后的

Resume checkpoint_C：恢复 [summary, msg4, msg5]，无法知道 msg4 在压缩前是否存在
```

**影响**：
- 自研可以精确回答"这条消息是压缩前还是压缩后的"
- LangGraph 的 checkpoint 是幂等的全量快照，无法追溯中间状态

### 6.2 控制权 vs 便利性

| 维度 | 自研 | LangGraph |
|------|------|----------|
| 学习成本 | 低（纯 Python） | 高（StateGraph / conditional_edge / reducer） |
| Debug 难度 | 低（线性流程） | 高（状态机 + checkpoint 隐式行为） |
| 新增节点 | 直接加代码 | 需要修改 graph |
| 压缩时机 | 显式 `if should_compact()` | 隐式 conditional_edge 路由 |
| 状态可见性 | 全在内存/文件 | checkpoint 隐藏了中间状态 |
| 迁移成本 | 高（已有 JSONL） | 低（已有 SQLite） |

### 6.3 Fork 语义差异

| 需求 | 自研 | LangGraph |
|------|------|----------|
| 继承父会话消息历史 | ✅ 复制 JSONL entry | ✅ 新 thread_id 无历史 |
| 断开 worktree_state | ✅ 过滤 entry | ❌ 无 worktree_state 概念 |
| 保留压缩边界 | ✅ 复制 compact-boundary | ❌ 无压缩边界 |
| 继承 parentUuid 链 | ✅ 保留 | ❌ 无链 |
| 独立演进 | ✅ 两边各自 append | ✅ 两边独立 checkpoint |

> **结论：LangGraph 的 Fork 是"干净的新会话"，不是"父会话的分支"。如果需要 Git 式的分支语义，必须用自研。**

---

## 七、实现文件差异

### 自研（agent_core）

```
agent_core/
├── session/
│   ├── storage.py      # JSONL 操作，~200行
│   ├── metadata.py     # 元数据，~100行
│   ├── state.py       # 状态机，~80行
│   ├── restore.py     # Resume/Fork，~150行
│   └── cleanup.py     # TTL/归档，~80行
├── context/
│   ├── manager.py      # Token预算，~150行
│   ├── chain.py       # 消息链，~100行
│   └── compact.py     # 压缩策略，~200行
├── memory/
│   ├── extract.py      # 提取，~150行
│   ├── store.py       # 存储，~80行
│   └── load.py        # 加载，~50行
├── agent_core.py      # 修改：集成三大系统
└── app.py             # 修改：UI集成

合计：~1340行
```

### LangGraph（langgraph_agent）

```
langgraph_agent/
├── agent.py            # 修改：集成记忆系统，~50行
├── graph.py           # 修改：新增compact_node，~30行
├── nodes.py           # 修改：新增compact/memory节点，~200行
├── state.py           # 修改：新增budget/memory字段，~20行
└── memory/
    ├── extract.py      # 提取，~150行（与自研共享逻辑）
    ├── store.py       # 存储，~80行（与自研共享逻辑）
    └── load.py        # 加载，~50行（与自研共享逻辑）

合计：~580行（大幅减少，因为持久化和循环由框架提供）
```

---

## 八、结论

| 选择 | 理由 |
|------|------|
| **选自研** | 需要精确 parentUuid 链、压缩边界语义、PARTIAL 压缩、分支 Fork 语义 |
| **选 LangGraph** | 快速交付、需要 SQLite 持久化、能接受全量快照而非链式压缩 |
| **两者都用** | 自研用于 agent_core，LangGraph 用于 langgraph_agent，对比学习 |

> 两种实现的功能完全对齐，底层实现完全不同，互为备份。
