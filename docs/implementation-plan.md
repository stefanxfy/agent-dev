# agent-dev 实现计划（2 天版）

> 会话管理 + 上下文管理 + 记忆系统三大子系统的完整落地
>
> 版本：v2.0 | 日期：2026-06-11
> 约束：2 天完成，功能全部保留，无删减

---

## 一、交付范围

### 会话管理（8 个模块全部保留）

| 模块 | 功能 | 状态 |
|------|------|------|
| 1. 生命周期 | 创建/切换/关闭/分叉/并发 | ✅ |
| 2. 消息存储 | JSONL 持久化/渐进加载/去重 | ✅ |
| 3. 元数据 | 标题/标签/Agent类型/模式 | ✅ |
| 4. 状态恢复 | Resume/Continue/Fork 语义 | ✅ |
| 5. 进度追踪 | 实时状态/待办事项/文件历史 | ✅ |
| 6. 并发会话 | 多会话并行/命名/切换 | ✅ |
| 7. 外部持久化 | CCR 远程同步/多端同步 | ✅ |
| 8. 清理归档 | TTL/自动清理/会话归档 | ✅ |

### 上下文管理（Claude Code 原生设计全部保留）

| 功能 | 状态 |
|------|------|
| 动态窗口分配 | ✅ |
| Token 预算监控 | ✅ |
| auto-compact 触发 | ✅ |
| BASE / PARTIAL / UP_TO 三种压缩策略 | ✅ |
| 9 段式摘要格式 | ✅ |
| verbatim quotes 防漂移 | ✅ |
| 状态重建 | ✅ |
| Forked Agent 压缩 | ✅ |
| PTL 防御机制 | ✅ |

### 记忆系统（全部保留）

| 功能 | 状态 |
|------|------|
| 四类记忆分级（user/feedback/project/reference） | ✅ |
| autoDream 三重门提取 | ✅ |
| MEMORY.md 索引 | ✅ |
| 会话结束时自动提取 | ✅ |
| 新会话加载偏好到 System Prompt | ✅ |
| DailyLogger 日志 | ✅ |

---

## 二、时间分配

```
Day 1（6月12日）
├── 上午 09:00-12:00  会话管理（storage + metadata + state）
├── 下午 14:00-18:00  上下文管理（manager + chain + compact）
└── 晚上 19:00-22:00  记忆系统（extract + store + load）
                      + 集成 agent_core.py

Day 2（6月13日）
├── 上午 09:00-12:00  Resume + Fork + 并发会话
├── 下午 14:00-18:00  外部持久化 + 清理归档 + UI 集成
└── 晚上 19:00-22:00  端到端测试 + 验收 + 修 Bug
```

---

## 三、文件清单

```
agent_core/
├── session/                        # 会话管理
│   ├── __init__.py
│   ├── storage.py                 # JSONL 读写、去重、写队列、延迟创建
│   ├── metadata.py                # 标题/标签/Agent 元数据
│   ├── restore.py                 # Resume/Continue/Fork
│   ├── state.py                   # 状态机（idle/running/requires_action）
│   └── cleanup.py                 # TTL/归档/清理
│
├── context/                        # 上下文管理
│   ├── __init__.py
│   ├── manager.py                 # Token 预算、监控、触发
│   ├── chain.py                   # 消息链、parentUuid、断链检测
│   └── compact.py                 # BASE/PARTIAL/UP_TO、摘要、防漂移
│
├── memory/                         # 记忆系统
│   ├── __init__.py
│   ├── daily.py                   # DailyLogger（已有骨架）
│   ├── extract.py                  # autoDream、偏好/反馈/知识提取
│   ├── store.py                   # memory/*.md 写入
│   └── load.py                    # 加载到 System Prompt
│
├── agent_core.py                  # 修改：集成三大系统
└── app.py                         # 修改：UI 集成
```

**合计：13 个文件（3 个改造 + 10 个新建）**

---

## 四、Day 1 详细任务

### 4.1 会话管理（上午）

#### `session/storage.py`

```python
class SessionStorage:
    session_id: str          # 全局唯一 UUID
    jsonl_path: Path         # ~/.agent_data/sessions/<session_id>.jsonl
    _pending: list[dict]     # 写队列，100ms 刷新
    _uuid_set: set[str]       # 内存去重

    def __init__(self, session_id=None, data_dir=None): ...
    def append_entry(self, entry: dict) -> str:     # 返回 UUID
    def read_entries(self) -> list[dict]:            # 全量读取
    def read_tail(self, kb=64) -> list[dict]:        # 尾部 64KB
    def get_entry(self, uuid: str) -> dict | None:  # UUID 查询
    def flush(self):                                  # 批量刷新
    def list_sessions(self) -> list[str]:            # 列出所有会话
    def delete_session(self, session_id):           # 删除会话
```

**实现要点**：
- 延迟创建：`_jsonl_path = None`，append 时才创建文件
- parentUuid 链：每条 entry 自动注入 `parentUuid`（上一条 UUID）
- 去重：内存维护 UUID set，append 前检查
- 写队列：100ms drain，先写临时文件再 rename（原子性）

#### `session/metadata.py`

```python
class SessionMetadata:
    title: str | None          # 用户自定义标题
    ai_title: str | None       # AI 生成标题
    tags: list[str]            # 标签
    agent_name: str             # Agent 类型
    agent_setting: str          # Agent 配置
    mode: str                   # 当前模式（plan/read/write）
    worktree_state: dict        # Git worktree 状态
    last_prompt: str            # 最后一条用户消息

    def update_title(self, title: str): ...
    def update_tag(self, tag: str): ...
    def to_entries(self) -> list[dict]:             # 转为 JSONL entry
    @classmethod
    def from_tail(cls, tail_entries: list[dict]) -> Self:  # 从尾部恢复
```

#### `session/state.py`

```python
class SessionState:
    status: Literal["idle", "running", "requires_action"]
    requires_action: RequiresActionDetails | None

    def emit_state_changed(self): ...  # SDK 事件
    def set_running(self): ...
    def set_requires_action(self, details): ...
    def set_idle(self): ...
```

### 4.2 上下文管理（下午）

#### `context/chain.py`

```python
class MessageChain:
    """
    管理消息链（parentUuid）
    - 添加消息时自动注入 parentUuid
    - 支持断链检测（parentUuid = null 时停止）
    - 支持从摘要重建链
    """
    messages: list[dict]

    def add(self, role, content, uuid=None) -> str:    # 返回 UUID
    def add_summary(self, summary: dict):             # 摘要消息
    def add_compact_boundary(self):                    # 断链标记
    def build_chain(self) -> list[dict]:               # 重建链（断链处停止）
    def get_all(self) -> list[dict]:                   # 获取全部
    def truncate_from_head(self, keep_ratio=0.8):     # 按 Token 截断
```

#### `context/manager.py`

```python
class ContextManager:
    """
    Token 预算管理 + 压缩触发
    替换 agent_core.py 中的 _trim_history
    """
    budget: int                        # 总 Token 预算
    messages: MessageChain             # 消息链

    def add_message(self, role, content): ...
    def should_compact(self) -> bool:   # 剩余 ≤ 20% 时触发
    def compact(self, llm_router):      # 调用 LLM 生成摘要
    def compact_boundary(self):         # 插入断链标记
    def compact_BASE(self, messages) -> dict:     # 截断 + 摘要
    def compact_PARTIAL(self, messages) -> dict:  # 保留首尾
    def compact_UP_TO(self, messages, target_tokens) -> dict:  # 目标 Token
    def estimate_tokens(self, text) -> int:       # 精确估算
    def format_summary_prompt(self, messages) -> str:  # 9 段式 Prompt
    def inject_verbatim_quotes(self, summary: dict, messages: list):  # 防漂移
```

#### `context/compact.py`

```python
# 压缩策略实现

def format_compact_summary(messages: list[dict]) -> str:
    """
    Claude Code 原生 9 段式摘要格式：
    1. 会话概要（一句话）
    2. 用户目标
    3. 关键决策
    4. 当前工作状态
    5. 重要上下文
    6. 文件变更
    7. 待完成任务
    8. 工具使用记录
    9. 已知偏好 / 约束
    """

def get_compact_user_summary_message(summary: str) -> dict:
    """注入 verbatim quotes 防漂移"""

NO_TOOLS_PREAMBLE = "..."
"""BASE 压缩模式下的工具说明"""
```

### 4.3 记忆系统（晚上）

#### `memory/extract.py`

```python
class MemoryExtractor:
    """
    从会话消息中提取四类记忆
    autoDream 三重门：时间≥24h + 会话≥5 + 无锁
    """
    def __init__(self, session_id: str): ...
    def should_extract(self) -> bool:  # 三重门检查
    def extract(self, messages: list[dict]) -> dict[str, list[str]]:
        """
        返回:
        {
            "user": [...],      # 用户偏好
            "feedback": [...],  # 反馈纠正
            "project": [...],   # 项目知识
            "reference": [...],  # 参考资料
        }
        """
    def extract_user_preferences(self, messages) -> list[str]: ...
    def extract_feedback(self, messages) -> list[str]: ...
    def extract_project_knowledge(self, messages) -> list[str]: ...
    def extract_references(self, messages) -> list[str]: ...
```

#### `memory/store.py`

```python
class MemoryStore:
    """
    memory/*.md 持久化
    """
    def __init__(self, project_slug: str): ...
    def save(self, category: str, memories: list[str]): ...
    def append(self, category: str, memory: str): ...
    def load(self, category: str) -> list[str]: ...
    def load_all(self) -> dict[str, list[str]]: ...
    def update_memory_index(self):  # 更新 MEMORY.md 索引（限 200 行）
```

#### `memory/load.py`

```python
def load_memories_for_session(project_slug: str) -> str:
    """
    加载所有记忆，格式化为 System Prompt 片段
    """
    return """
## 【记忆 - 用户偏好】
{user_memories}

## 【记忆 - 项目知识】
{project_memories}
...
"""

def inject_into_system_prompt(base_prompt: str, memories: str) -> str:
    """注入到 System Prompt"""
```

### 4.4 集成 agent_core.py

```python
# agent_core.py 修改点

class ReactAgent:
    def __init__(self, ...):
        # 新增
        self.session = SessionStorage()
        self.context = ContextManager(budget=max_context_tokens)
        self.memory_store = MemoryStore(project_slug)
        self.memory_loader = load_memories_for_session
        # 加载跨会话记忆
        self.system_prompt = inject_into_system_prompt(
            self.system_prompt,
            self.memory_loader(project_slug)
        )

    def run(self, user_message: str):
        # 1. 写入 SessionStorage
        self.session.append_entry({"role": "user", "content": user_message})
        # 2. 加入 ContextManager
        self.context.add_message("user", user_message)
        # 3. 检查是否需要压缩
        if self.context.should_compact():
            self.context.compact(self.llm)  # 生成摘要
            # 写回 SessionStorage
            self.session.append_entry(self.context.get_summary_entry())
        # 4. 正常 ReAct 循环...

    def finalize(self):
        """会话结束时调用"""
        # 1. 提取记忆
        extractor = MemoryExtractor(self.session.session_id)
        if extractor.should_extract():
            memories = extractor.extract(self.context.messages)
            self.memory_store.save_all(memories)
        # 2. 刷新写队列
        self.session.flush()
        # 3. 写入元数据
        self.session.append_metadata(self.metadata.to_entries())
```

---

## 五、Day 2 详细任务

### 5.1 Resume + Fork

#### `session/restore.py`

```python
def resume_session(session_id: str) -> tuple[list[dict], SessionMetadata]:
    """
    Resume 流程：
    1. 读取 JSONL 全部消息
    2. 断链检测（compact_boundary 处停止）
    3. 重建消息链
    4. 恢复元数据（从尾部）
    5. 返回 (messages, metadata)
    """

def fork_session(parent_session_id: str) -> str:
    """
    Fork 流程：
    1. 读取父会话消息（断链处停止）
    2. 生成新 Session ID
    3. 写入新 JSONL（parentUuid 链不变）
    4. 不继承 worktree_state（危险状态）
    """

def continue_session(session_id: str) -> tuple[list[dict], SessionMetadata]:
    """
    Continue（增量加载）：
    - 已知本地已有会话，读取尾部增量
    """
```

### 5.2 进度追踪

```python
# session/progress.py（新建）

class ProgressTracker:
    """
    追踪文件变更、待办事项、工具使用记录
    """
    file_history: list[FileChange]
    todos: list[str]
    tool_usage: dict[str, int]

    def record_file_change(self, path: str, change_type: str): ...
    def record_todo(self, todo: str): ...
    def record_tool_use(self, tool_name: str): ...
    def to_snapshot(self) -> dict:  # 转为 JSONL entry
    def load_from_snapshot(self, snapshot: dict): ...
```

### 5.3 并发会话

```python
def list_concurrent_sessions(data_dir: str) -> list[SessionSummary]:
    """
    列出所有会话（名称/时间/摘要）
    """

def switch_session(session_id: str) -> SessionStorage:
    """
    切换当前会话
    """
```

### 5.4 外部持久化

```python
# session/remote.py（新建，可选实现）

class RemoteSessionSync:
    """
    CCR 远程同步接口
    （若 2 天内无法完成，推迟到后续迭代）
    """
    def push(self, session_id: str): ...
    def pull(self, session_id: str): ...
    def list_remote(self) -> list[SessionSummary]: ...
```

### 5.5 清理归档

```python
# session/cleanup.py

class SessionCleanup:
    """
    TTL + 归档策略
    """
    def cleanup_by_ttl(self, max_age_days=30): ...
    def archive_session(self, session_id: str): ...
    def get_storage_stats(self) -> dict: ...
```

### 5.6 UI 集成

```python
# app.py 修改

# 侧边栏：会话列表
# st.sidebar: list_concurrent_sessions()
# st.sidebar: switch_session()

# 会话元数据面板
# st: title, tags, mode, status

# 进度追踪面板
# st: file_history, todos, tool_usage

# 会话结束时触发 finalize()
```

---

## 六、并行开发策略

**Day 1 按模块分配 3 个 sub-agent 并行编写：**

| Agent | 负责文件 | 依赖 |
|-------|---------|------|
| Agent-1 | `session/` 全部（storage + metadata + state + cleanup + restore + progress） | 无 |
| Agent-2 | `context/` 全部（manager + chain + compact） | 无 |
| Agent-3 | `memory/` 全部（extract + store + load），`session/progress.py` | 无 |

**主控（我）：**
- Day 1 上午：协调 sub-agent 创建，审查输出
- Day 1 下午：编写集成层 `agent_core.py` 改动
- Day 2：编写 `session/remote.py`、UI 集成、端到端测试

---

## 七、验收标准

### Day 1 结束前

- [ ] `SessionStorage.append_entry()` + `read_entries()` + `read_tail()` 测试通过
- [ ] `ContextManager.should_compact()` + `compact()` 生成摘要写入 JSONL
- [ ] `MessageChain.parentUuid` 链正确生成
- [ ] `MemoryExtractor.extract()` 提取四类记忆
- [ ] `MemoryStore.save()` + `load()` 读写 `memory/*.md`
- [ ] `inject_into_system_prompt()` 正确注入到 System Prompt
- [ ] `agent_core.py` 集成三大系统，端到端 ReAct 循环可跑

### Day 2 结束前

- [ ] `resume_session()` 从 JSONL 恢复消息链
- [ ] `fork_session()` 新会话复制消息（不含危险状态）
- [ ] `continue_session()` 增量加载尾部
- [ ] `ProgressTracker` 记录文件变更 + 待办
- [ ] `list_concurrent_sessions()` + `switch_session()` 并发切换
- [ ] `SessionCleanup` TTL 清理
- [ ] Streamlit UI 会话列表 + 切换器
- [ ] 会话结束 `finalize()` 自动提取记忆
- [ ] 新会话验证旧偏好生效

---

## 八、技术约定

### Token 估算

```python
def estimate_tokens(text: str) -> int:
    chinese = sum(1 for c in text if '\u4e00' <= c <= '\u9fff')
    english = len(text) - chinese
    return int(chinese * 1.4 + english * 0.25 + 10)  # overhead
```

### JSONL Entry 规范

所有 Entry 必须包含：
```python
{
    "uuid": str,           # 全局唯一
    "parentUuid": str | None,  # 父消息 UUID
    "sessionId": str,      # 所属会话
    "timestamp": str,      # ISO 8601
}
```

### 压缩触发阈值

```python
COMPACT_THRESHOLD_RATIO = 0.20  # 剩余 ≤ 20% 时触发
KEEP_RATIO = 0.80               # 截断后保留最近 80%
```

---

> 文档版本：v2.0
> 创建时间：2026-06-11
> 适用项目：agent-dev
