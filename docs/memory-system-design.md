# 记忆系统完整实现方案

> 参考来源：QClaw 记忆系统 + Mem0 核心概念 + Claude Code 记忆设计  
> 项目：agent-dev（自研 Agent 框架）  
> 日期：2026-06-11  
> 状态：方案文档（待实现）

---

## 一、核心设计理念

| 设计原则 | 说明 |
|---------|------|
| **Append-only 日志** | 原始日志永不覆写，保证无损记录 |
| **三层分离** | 日常日志（原始）→ 向量索引（检索）→ MEMORY.md（精炼） |
| **变化慢的才存** | 偏好/决策/约束/教训 → 存；代码/临时状态/中间推理 → 不存 |
| **时间衰减** | 记忆有生命周期，久未访问的自动降低权重 |
| **人类审核** | LLM 提取的建议需要用户确认才能写入 MEMORY.md |
| **独立 Agent 提取** | 新建独立 Agent 做记忆提取，不污染主对话上下文 |

---

## 二、整体架构

```
┌───────────────────┐
│                    用户对话                           │
└──────────────────────┬──────────────────────────────┘
                       │
            ┌──────────┴──────────┐
            │      主 Agent 运行时  │
            │  1. 检索相关记忆       │
            │  2. 执行 ReAct 循环   │
            │  3. 返回响应          │
            └──────────┬───────────┘
                       │ 对话结束
                       ▼
            ┌──────────────────────┐
            │   记忆提取 Agent（独立）│
            │   异步后台运行          │
            │   1. 读对话历史         │
            │   2. LLM 提取关键信息  │
            │   3. 去重 + 写入存储   │
            └──────────┬───────────┘
                       │
        ┌──────────────┼──────────────┐
        │              │              │
  日常日志        向量索引        MEMORY.md
  (append)      (语义检索)      (精炼知识)
        │              │              │
        └──────────────┼──────────────┘
                       │
                 蒸馏引擎 (autoDream)
                定期压缩 → 更新 MEMORY.md
```

---

## 三、存储触发机制

### 3.1 触发时机

记忆存储发生在**对话结束后**，由独立 Agent 处理：

```
用户发送消息 → 主 Agent 响应 → 对话结束 → 异步触发记忆提取 Agent
                                              │
                              ┌───────────────┴───────────────┐
                              │          提取 Agent              │
                              │  1. 读取对话历史（独立上下文）     │
                              │  2. LLM 提取关键信息             │
                              │  3. 去重 + 写入日常日志 + 向量库  │
                              └───────────────┬───────────────┘
                                              │
                              ┌───────────────┴───────────────┐
                              │     定时蒸馏引擎（autoDream）    │
                              │     1. 读取增量日志              │
                              │     2. LLM 蒸馏精炼              │
                              │     3. dry_run → 用户确认        │
                              │     4. 写入 MEMORY.md            │
                              └───────────────────────────────┘
```

### 3.2 触发类型

| 触发类型 | 时机 | 触发者 | 结果 |
|---------|------|-------|------|
| **异步提取** | 每次对话结束后 | 记忆提取 Agent（后台线程） | 写入日常日志 + 向量库 |
| **手动标记** | 用户说"记住这个" | 主 Agent 检测关键词 | 立即触发提取 + 高置信度 |
| **定时蒸馏** | 每 24h + 会话≥5 | DistillationScheduler | 生成 MEMORY.md 建议 |
| **主动检索** | 用户问"你记得吗" / 每次 run() 前 | MemoryStore.search() | 从向量库语义检索 |

### 3.3 提取频率控制

不是每次对话都触发 LLM 提取，通过 `_should_extract()` 判断：

```python
def _should_extract(self, user_msg: str, agent_response: str) -> bool:
    """
    判断是否需要触发记忆提取 Agent
    
    触发条件（满足任一即提取）：
    1. 用户明确说"记住这个/帮我记住/记一下"
    2. 对话中出现关键词（偏好/决策/选择/拒绝/教训/经验）
    3. 对话轮次累积 ≥ 5（可能有值得记住的信息）
    """
    # 用户显式要求
    if any(kw in user_msg for kw in ["记住", "记一下", "帮我记住", "别忘了"]):
        return True
    
    # 对话内容含关键信息
    keywords = ["偏好", "决策", "选择", "拒绝", "采用", "教训", "经验", "原则"]
    if any(kw in user_msg or kw in agent_response for kw in keywords):
        return True
    
    return False
```

---

## 四、独立 Agent 记忆提取方案

### 4.1 为什么选择独立 Agent

| 维度 | 当前 Agent 同步做 | 新建 Agent 异步做 ✅ |
|------|-------------------|---------------------|
| **上下文隔离** | ❌ 提取思考污染用户对话 | ✅ 完全隔离 |
| **安全性** | ❌ 提取结果可能泄露到对话 | ✅ 只写文件 |
| **用户体验** | ❌ 对话结束要等几秒 | ✅ 用户无感知 |
| **错误影响** | ❌ 提取失败可能打断流程 | ✅ 失败不影响主对话 |
| **Cache 共享** | ✅ 共享 Router 缓存 | ❌ 需独立初始化（成本很低） |

### 4.2 实现方式

```python
# agent_core/memory/extractor.py

import threading
from datetime import datetime
from typing import Optional
from agent_core.memory.daily import DailyLogger
from agent_core.memory.memory_store import MemoryStore
# LLMRouter 复用，不创建新的

class MemoryExtractor:
    """
    独立记忆提取 Agent
    
    设计原则：
    - 后台线程运行，不阻塞主 Agent
    - 独立 LLM Router 实例（独立上下文）
    - 提取失败只记日志，不影响主对话
    - 复用主 Agent 的 LLM Router（共享模型配置）
    """
    
    def __init__(
        self,
        router,  # 主 Agent 的 LLMRouter 实例（复用配置）
        daily_logger: DailyLogger,
        memory_store: MemoryStore,
    ):
        self.router = router  # 复用 Router（Router 内部缓存独立）
        self.daily_logger = daily_logger
        self.memory_store = memory_store
    
    def extract_async(self, user_msg: str, agent_response: str, session_id: str):
        """
        异步触发记忆提取（后台线程）
        
        调用方式：
            self.extractor.extract_async(user_msg, response, self.thread_id)
            # 立即返回，不阻塞主对话
        """
        thread = threading.Thread(
            target=self._do_extraction,
            args=(user_msg, agent_response, session_id),
            daemon=True,  # 主进程退出时自动终止
        )
        thread.start()
    
    def _do_extraction(self, user_msg: str, agent_response: str, session_id: str):
        """
        独立上下文中执行记忆提取（后台线程的入口）
        
        与主 Agent 完全隔离：
        - 独立的 LLM 调用（独立 prompt，不影响主对话上下文）
        - 独立的异常处理（失败只记日志）
        - 独立的写入操作（只写日志和向量库）
        """
        try:
            # 1. 先快速写入日常日志（无 LLM 调用）
            self.daily_logger.log(
                session_id=session_id,
                category="conversation",
                key="exchange",
                value=f"Q: {user_msg[:200]} → A: {agent_response[:200]}",
            )
            
            # 2. 判断是否需要 LLM 提取
            if not self._should_extract(user_msg, agent_response):
                return
            
            # 3. LLM 提取关键信息（独立上下文）
            extraction = self._llm_extract(user_msg, agent_response)
            
            # 4. 写入向量索引
            for item in extraction:
                self.memory_store.add_memory(
                    memory_id=f"mem_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{hash(item['text']) % 10000}",
                    text=item["text"],
                    category=item["category"],
                    confidence=1.0 if "记住" in user_msg else 0.8,
                )
            
        except Exception as e:
            # 提取失败不影响主对话，只记日志
            print(f"[MemoryExtractor] 提取失败: {e}")
    
    def _should_extract(self, user_msg: str, agent_response: str) -> bool:
        """提取频率控制"""
        if any(kw in user_msg for kw in ["记住", "记一下", "帮我记住", "别忘了"]):
            return True
        
        keywords = ["偏好", "决策", "选择", "拒绝", "采用", "教训", "经验", "原则"]
        if any(kw in user_msg or kw in agent_response for kw in keywords):
            return True
        
        return False
    
    def _llm_extract(self, user_msg: str, agent_response: str) -> list[dict]:
        """
        用 LLM 从对话中提取关键记忆
        
        关键：这个调用在独立线程中，不会污染主 Agent 的上下文
        """
        prompt = f"""
你是一个记忆提取助手。从以下对话中提取值得长期记住的信息。

提取规则：
1. 用户偏好（学习风格、技术选型、沟通方式）
2. 关键决策（架构选择、技术决策、拒绝的方案）
3. 技术细节（实现细节、Bug 修复经验、重要教训）
4. 排除：临时状态、代码细节、中间推理、已过时的信息

输出严格的 JSON 数组（不要输出其他内容）：
[
  {{"text": "记忆内容", "category": "user_preference"}},
  {{"text": "记忆内容", "category": "decision"}},
  {{"text": "记忆内容", "category": "technical"}}
]

类别只能是：user_preference / decision / technical / error

对话内容：
用户: {user_msg[:500]}
助手: {agent_response[:500]}
""".strip()
        
        response = self.router.chat(
            messages=[{"role": "user", "content": prompt}],
            provider="anthropic",  # 用 Anthropic 做提取（质量更高）
            temperature=0.1,      # 低温度保证输出稳定
        )
        
        # 解析 JSON
        import json
        try:
            # 提取 JSON 部分（可能被 ```json ``` 包裹）
            text = response.strip()
            if text.startswith("```"):
                text = text.split("\n", 1)[1]
                text = text.rsplit("```", 1)[0]
            
            memories = json.loads(text)
            return [m for m in memories if "text" in m and "category" in m]
        except json.JSONDecodeError:
            print(f"[MemoryExtractor] JSON 解析失败: {response[:100]}")
            return []
```

### 4.3 与主 Agent 的集成方式

```python
# langgraph_agent/agent.py

from agent_core.memory.extractor import MemoryExtractor

class LangGraphAgent:
    def __init__(self, ...):
        # ... 原有初始化 ...
        
        # 初始化记忆系统
        self.daily_logger = DailyLogger(log_dir=".agent_data/logs")
        self.memory_store = MemoryStore(
            chroma_path=".agent_data/chroma",
            metadata_db=".agent_data/memory.db",
        )
        
        # 初始化记忆提取 Agent（独立）
        self.extractor = MemoryExtractor(
            router=self.router,  # 复用 Router
            daily_logger=self.daily_logger,
            memory_store=self.memory_store,
        )
    
    def run(self, message: str):
        # 1. 检索相关记忆（对话开始前）
        memories = self._retrieve_memories(message, top_k=3)
        memory_context = self._build_memory_context(memories)
        
        # 2. 执行 ReAct 循环（原有逻辑）
        response = self._react_loop(memory_context + message)
        
        # 3. 异步触发记忆提取（立即返回，不阻塞）
        self.extractor.extract_async(
            user_msg=message,
            agent_response=response,
            session_id=self.thread_id,
        )
        
        return response
```

---

## 五、存储流程

### 5.1 Layer 1：日常日志（Append-only）

**存储路径**：`.agent_data/logs/YYYY-MM-DD.md`

**存储格式**：
```markdown
# 日志: 2026-06-11

## [2026-06-11 16:43] Session: thread_abc123

### User Preference
- **学习风格**: 先手写原生 ReAct 理解本质，再用框架对比设计思想
  - 元数据: {"confidence": 1.0, "source": "conversation"}

### Decision
- **记忆系统设计**: 采用 QClaw 风格三层架构
  - 元数据: {"confidence": 1.0, "source": "conversation"}

### Technical
- **Token 系数**: 中文 1.4，英文 0.25
  - 元数据: {"confidence": 1.0, "source": "bug_fix"}
```

**写入时机**：每次对话结束（由提取 Agent 写入，无 LLM 调用，快速）

**代码**：`DailyLogger.log(session_id, category, key, value, metadata)`

### 5.2 Layer 2：向量索引（语义检索）

**存储路径**：`.agent_data/chroma/`（Chroma 向量数据库）+ `.agent_data/memory.db`（SQLite 元数据）

**存储结构**：
```
Chroma Collection: "memories"
├── id: "mem_20260611_164300_1234"
├── embedding: [0.123, -0.456, ...]  (384维，all-MiniLM-L6-v2)
├── document: "用户偏好：先手写原生 ReAct 理解本质"
└── metadata: {category: "user_preference", created_at: "..."}

SQLite Table: memory_meta
├── id (PK)
├── category
├── created_at
├── updated_at
├── access_count (默认0，每次检索命中+1)
└── confidence (默认1.0，每日衰减 ×0.95)
```

**写入时机**：提取 Agent 通过 LLM 提取后写入

**代码**：`MemoryStore.add_memory(memory_id, text, category, confidence)`

### 5.3 Layer 3：MEMORY.md（精炼长期记忆）

**存储路径**：`.agent_data/MEMORY.md`

**限制**：≤ 200 行 / 25KB（物理裁剪）

**存储格式**：
```markdown
# MEMORY.md - 精炼长期记忆

## 用户偏好
### 学习风格 [来源: 2026-06-10, 2026-06-11]
- 先手写原生 ReAct 理解本质，再用框架对比设计思想
- 重视底层原理，习惯多 Agent 并行深度研读

### 技术选型偏好 [来源: 2026-06-08]
- 拒绝 Docker 沙箱（学习场景需要完全控制）
- 优先本地方案（Chroma > Pinecone，SQLite > PostgreSQL）

## 关键决策
### Agent 架构 [来源: 2026-06-08, 2026-06-11]
- Stage 1: 自研 ReAct 循环（Day 1-2 完成）
- Stage 2: LangGraph 重构（Day 3-4 完成）
- Stage 3: 记忆系统（Day 5 进行中）
- Stage 4: 非 Docker 原生沙箱（待开始）

## 技术细节
### LangGraph Checkpointer [来源: 2026-06-11]
- 使用 SqliteSaver.from_conn_string() 实现持久化
- 刷新页面后从 SQLite 恢复会话历史
```

**写入时机**：蒸馏引擎生成建议 + 用户确认后写入

**编辑方式**：用户可直接手动编辑 MEMORY.md（格式：标题 + 内容 + [来源: 日期]）

---

## 六、检索流程

### 6.1 检索时机

| 检索类型 | 时机 | 触发者 | 结果 |
|---------|------|-------|------|
| **上下文注入** | 每次 `agent.run()` 开始前 | Agent 内部逻辑 | 相关记忆注入 system prompt |
| **主动检索** | 用户问"你记得吗" | memory_search 工具 | 语义搜索返回结果 |
| **蒸馏参考** | 蒸馏引擎运行时 | distiller.distill() | 读取近期日志 |

### 6.2 检索流程图

```
用户消息: "我的学习风格是什么？"
        ↓
    Agent.run()
        ↓
┌─────────────────────────────────┐
│  1. 语义搜索（MemoryStore）      │
│     query = "学习风格 偏好"     │
│     top_k = 3                   │
└────────────┬────────────────────┘
             ↓
┌─────────────────────────────────┐
│  2. 多维度排序                    │
│     semantic(0.6) + time(0.3)   │
│     + access(0.1) × confidence  │
└────────────┬────────────────────┘
             ↓
┌─────────────────────────────────┐
│  3. 构建 memory_context          │
│  [相关记忆]                     │
│  - 先手写原生 ReAct 理解本质... │
│  - 重视底层原理，多 Agent 研读  │
└────────────┬────────────────────┘
             ↓
┌─────────────────────────────────┐
│  4. 注入 system prompt          │
│  system_prompt += memory_context│
└────────────┬────────────────────┘
             ↓
         Agent 响应
```

### 6.3 检索排序公式

```
final_score = (semantic_score × 0.6 + time_score × 0.3 + access_score × 0.1) × confidence

其中：
- semantic_score = 1 - cosine_distance（Chroma 返回）
- time_score = max(0.3, 1.0 - age_days / 30)
  - 30天内：1.0（完全可信）
  - 30-90天：0.7（可能已变）
  - 90天+：0.3（大概率过时）
- access_score = min(1.0, access_count × 0.1)
  - 每次被检索命中：confidence += 0.1
- confidence：初始 1.0，每日衰减 ×0.95
```

### 6.4 检索代码

```python
def _retrieve_memories(self, query: str, top_k: int = 3) -> list[dict]:
    """语义检索相关记忆"""
    results = self.memory_store.search(
        query=query,
        top_k=top_k,
        time_decay_days=30,
    )
    
    # 更新访问次数（被命中 → 权重提升）
    for mem in results:
        self.memory_store.update_access(mem["id"])
    
    return results


def _build_memory_context(self, memories: list[dict]) -> str:
    """将检索结果构建为 system prompt 片段"""
    if not memories:
        return ""
    
    lines = ["[相关记忆]"]
    for mem in memories:
        lines.append(f"- {mem['text']}")
    
    return "\n".join(lines) + "\n\n"
```

---

## 七、蒸馏流程（autoDream）

### 7.1 触发条件（三重门）

```python
def should_distill(self, session_count: int) -> bool:
    """三重门检查"""
    # 门1：时间≥24h（距离上次蒸馏至少 24 小时）
    last = self._get_last_distill_time()
    if last and (datetime.now() - last).total_seconds() < 86400:
        return False
    
    # 门2：会话≥5（有足够新内容）
    if session_count < 5:
        return False
    
    # 门3：无锁（没有正在运行的蒸馏进程）
    if self.lock_path.exists():
        return False
    
    return True
```

### 7.2 蒸馏流程图

```
DistillationScheduler（每小时检查一次）
        ↓
    should_distill() == True ?
        ↓ 是
┌─────────────────────────────────┐
│  1. 加锁                         │
│     创建 .agent_data/.distill_lock│
└────────────┬────────────────────┘
             ↓
┌─────────────────────────────────┐
│  2. 读取增量日志                  │
│     上次蒸馏后的新日志内容        │
└────────────┬────────────────────┘
             ↓
┌─────────────────────────────────┐
│  3. LLM 提取精炼信息             │
│     生成 MEMORY.md 格式建议      │
└────────────┬────────────────────┘
             ↓
┌─────────────────────────────────┐
│  4. 去重 + 合并                  │
│     检查建议是否已存在于 MEMORY.md│
└────────────┬────────────────────┘
             ↓
┌─────────────────────────────────┐
│  5. dry_run=True → 返回建议     │
│     通知用户确认                 │
│     用户确认 → dry_run=False    │
│     写入 MEMORY.md               │
└────────────┬────────────────────┘
             ↓
┌─────────────────────────────────┐
│  6. 释放锁 + 更新蒸馏时间        │
└─────────────────────────────────┘
```

### 7.3 蒸馏 Prompt

```
从以下对话日志中提取值得长期记住的信息。

提取规则：
1. 用户偏好（学习风格、技术选型偏好、沟通方式）
2. 关键决策（架构选择、技术决策、拒绝的方案）
3. 技术细节（实现细节、Bug 修复经验、重要教训）
4. 排除：临时状态、代码细节、中间推理、已过时的信息

输出格式（Markdown）：
## 用户偏好
### 标题 [来源: 日期]
- 内容

## 关键决策
### 标题 [来源: 日期]
- 内容

日志内容：
---
（增量日志内容）
---
```

---

## 八、文件结构总览

```
agent-dev/
├── agent_core/
│   ├── memory/
│   │   ├── __init__.py          ← 包入口（导出所有组件）
│   │   ├── daily.py             ← DailyLogger（日常日志，append-only）
│   │   ├── memory_store.py      ← MemoryStore（向量索引 + 语义搜索 + 时间衰减）
│   │   ├── extractor.py         ← MemoryExtractor（独立提取 Agent，异步后台）
│   │   ├── distiller.py         ← MemoryDistiller（蒸馏引擎，autoDream）
│   │   └── scheduler.py         ← DistillationScheduler（定时调度，三重门）
│   │
│   └── langgraph_agent/
│       └── agent.py              ← 集成记忆系统（检索 + 异步提取）
│
├── .agent_data/
│   ├── logs/                    ← 日常日志（append-only，永不覆写）
│   │   ├── 2026-06-11.md
│   │   ├── 2026-06-10.md
│   │   └── ...
│   ├── chroma/                  ← Chroma 向量数据库
│   ├── memory.db                ← SQLite 元数据索引（时间/访问/置信度）
│   ├── MEMORY.md                ← 精炼长期记忆（≤200行/25KB）
│   ├── .distill_lock            ← 蒸馏锁文件（防并发）
│   └── .last_distill            ← 上次蒸馏时间戳
│
└── web/
    └── app_langgraph.py         ← UI：记忆管理按钮（查看/触发蒸馏/手动编辑）
```

---

## 九、依赖清单

```txt
# 核心依赖（必须）
chromadb>=0.4.0
sentence-transformers>=2.2.0

# 已有依赖（无需额外安装）
sqlite3         ← Python 内置
threading       ← Python 内置
json            ← Python 内置
```

安装命令：
```bash
pip install chromadb sentence-transformers
```

---

## 十、实施计划（Day 5）

| 时间 | 任务 | 产出文件 |
|------|------|---------|
| **09:00-10:00** | DailyLogger（日常日志） | `agent_core/memory/daily.py` |
| **10:00-11:30** | MemoryStore（Chroma + SQLite + 时间衰减） | `agent_core/memory/memory_store.py` |
| **11:30-13:00** | MemoryExtractor（独立提取 Agent） | `agent_core/memory/extractor.py` |
| **13:30-14:30** | MemoryDistiller（蒸馏引擎） | `agent_core/memory/distiller.py` |
| **14:30-15:30** | DistillationScheduler（定时调度） | `agent_core/memory/scheduler.py` |
| **15:30-16:30** | 集成到 LangGraph Agent | 修改 `langgraph_agent/agent.py` |
| **16:30-17:30** | 端到端测试 + UI 集成 | 测试 + 修改 `app_langgraph.py` |
