# ReAct 严格双通道记忆提取 — 设计 spec

**作者**:Claude + xufanyun
**日期**:2026-06-22
**目标 milestone**:M9 (Day 9)
**状态**:草稿,待用户 review

---

## 1. 背景与动机

### 1.1 现状

M7 完成了"检索 + UI"接入(单向,只读不写)。
M8 完成数据生命周期。
**当前 Option C**(commit `7613ab1`)在 `ReactAgent.run()` 末尾实现了一个**简化的实时提取**:
- 同步调 LLM 解析最后一对 user/assistant
- 无视三级门,无累计阈值,无间隔节流
- 每 turn 必提,绕过 `DualChannelWriter`(M2 已实现但未使用)

### 1.2 问题

Option C **不符合** [docs/memory-system-design.md §3.3 §4.1](docs/memory-system-design.md) 的设计:
1. **缺三级门**(累计阈值、关键词过滤、LLM 评分) → LLM 调用浪费
2. **缺 A3 重启恢复** → 崩了丢 turn
3. **缺 A4 跨进程锁** → 多端并发不安全
4. **缺 A9 优雅退出** → in-flight 提取丢失
5. **缺 A10 事务** → 半写状态无回滚
6. **绕过 SM**(L3 滚动笔记)→ 已有 L3 不复用

### 1.3 目标

把 ReactAgent 的 turn-end 记忆写入路径**切换**到 `DualChannelWriter`(M2)+ `MemoryExtractor`(M3)+ `ExtractionGate`(本次新写),**严格按设计文档**实现。

**核心原则**:
- 不重新造轮子(接现成 M2/M3 实现)
- 三级门严格按 §3.3(本次用户调整版)
- 通道 A 同步、通道 B 异步(严格按 §4.1)
- UI 反馈走 sidebar 计数(不在聊天区流)

---

## 2. 范围

### 2.1 In Scope

| 范围 | 实现 |
|------|------|
| 接入 `DualChannelWriter` | 改 `ReactAgent.run()` 末尾 |
| 三级门决策树(本次用户版)| 新增 `ExtractionGate` 模块 |
| ReactAgent ↔ DualChannelWriter 适配 | 新增 `ReactMemoryBridge` 模块 |
| LLM 评分提示词(含已有记忆块)| 新增 `prompt_templates.py` |
| `web/app.py` 重新 wiring | 替换 Option C 的 extractor 注入 |
| UI sidebar 计数 | 已有,本次保持 |
| 删除 Option C 同步 hack | 删 `memory_extractor` 构造参数 + `_extract_and_write()` |
| 测试 | 新增 `tests/test_extraction_gate.py` + `tests/test_react_memory_strict.py` |
| 文档 | 更新 `docs/test_react_ui.md` |
| 设计文档 | 在 `docs/memory-system-design.md` 加 §3.3.1 用户调整版 + §4.8 通道 A WAL 行为 + §6.9 门1 周期内去重 + 短对话"记住"策略 |

### 2.2 Out of Scope

| 不做 | 原因 |
|------|------|
| autoDream 蒸馏(§七)| M5 已有 `DistillationScheduler` 框架,本次不接 UI |
| `/remember` slash command | 设计文档无此概念,本次不做 |
| 通道 A 同步 LLM 提取(违反不变量 #3)| 不做 |
| 关键词强意图短路(违背设计 §3.3 严格性)| 不做 |
| 改 SM(L3 滚动笔记)逻辑 | SM 已实现,各管各的,本次不动 |
| 改 daily log 格式 | 保持 JSONL 简化实现 |

---

## 3. 架构总览

```
ReactAgent.run(prompt)
    │
    ├─ ① 主 ReAct 循环(LLM ←→ 工具)        ← 现有,不动
    │
    └─ ② turn-end(响应流结束)                ← 本次改造
        │
        ├─ 通道 A 同步(无 LLM)
        │   └─► DualChannelWriter.channel_a_inline_write(user_msg, resp, turn_index)
        │       ├─► 写 ~/.agent_data/logs/<session_id>.jsonl
        │       └─► 推 daily_cursor(同事务)
        │
        └─ 通道 B 异步(LLM,后台 executor)
            │
            └─► ExtractionGate.should_extract(ctx)        ← ★ 本次新写
                ├─ 门1: cumulative_tokens >= 10K  OR  tool_calls >= 10
                │       (会话级累计;达到后清零,新一轮累计)
                │
                ├─ 门2: 关键词命中 ≥1 (16 个关键词)
                │
                └─ 门3: LLM 评分 confidence >= 0.6
                        (提示词含 <existing_memories_in_this_period>)
                │
                └─► DualChannelWriter.channel_b_background_extract(turns, llm_extractor)
                    └─► ThreadPoolExecutor 异步
                        ├─► MemoryExtractor.process(candidates)   ← M3 已有
                        ├─► MemoryStore.write()  per-file         ← M3 已有
                        └─► VectorStore.add(embedding) + 推 extract_cursor
```

---

## 4. 关键概念

### 4.1 通道 A vs 通道 B 职责分工

| 写什么 | 写哪里 | 谁写 | 同步/异步 | 触发 |
|--------|--------|------|----------|------|
| turn 原文 | `~/.agent_data/logs/<session_id>.jsonl` | **A** | **同步** | turn-end 必写 |
| daily_cursor | MetaDB(SQLite) | **A** | 同步 | 写完 JSONL 后 |
| 结构化 memory | `~/.agent_data/memory/<type>/<hash>.md` | **B** | **异步** | 门1 OR 门2 + 门3 过 |
| Chroma 向量 | `~/.agent_data/vector_store/chroma/` | **B** | 异步 | 同上 |
| extract_cursor | MetaDB | **B** | 异步 | 写盘+向量化成功后 |

### 4.2 A 详细职责(WAL 层)

**通道 A 唯一职责**:把 turn 原文实时持久化到 JSONL(类似数据库 WAL)。

```python
# 写入位置
log_path = memory_store.root.parent / "logs" / f"{session_id}.jsonl"

# 写入字段(每行 JSON)
entry = {
    "turn_index": turn_index,
    "user_msg": user_msg,
    "assistant_resp": assistant_resp,
    "ts": time.time(),
}

# 必须 f.flush() + os.fsync()(强制落盘)
```

**A 必不做**:
- ❌ 不调 LLM(不变量 #3)
- ❌ 不解析内容
- ❌ 不向量化
- ❌ 不写 `~/.agent_data/memory/<type>/` 下任何文件
- ❌ 不写 vector store
- ❌ 不读 daily log(只 append)

**和 SessionManager 的关系**:
- SessionManager 写 `data/sessions/<id>.jsonl`(主对话流,粒度细到每条 message)
- 通道 A 写 `~/.agent_data/logs/<id>.jsonl`(记忆子系统,粒度粗到每 turn 一行)
- 两者刻意分开,免得记忆写入挂掉影响主对话上下文

### 4.3 B 详细职责(结构化层)

**通道 B 唯一职责**:从对话原文生成结构化记忆条目 + 向量索引。

```python
# 触发流程(异步)
# 1. 调 LLM 评分 + 提取(一次合并调用,§3.3 L1)
# 2. MemoryExtractor.process(): 校验 / 合并 / 密钥过滤
# 3. MemoryStore.write(): 写 ~/.agent_data/memory/<type>/<hash>.md
# 4. ChromaVectorStore.add(): 写 ~/.agent_data/vector_store/chroma/<id>
# 5. 推进 extract_cursor 到 daily_cursor + 1
```

**B 必不做**:
- ❌ 不写 daily log
- ❌ 不推进 daily_cursor
- ❌ 不接受 user 实时查询(查询是 MemoryRetriever.search())

### 4.4 B 的触发决策树(本次用户调整版)

```
B 触发决策树:
│
├─ 门1(累计型): cumulative_tokens >= 10K  OR  tool_calls >= 10  ?
│   ├─ 是 → 进入门3 LLM 评分
│   │       (跑完 B 后,cumulative_tokens / tool_calls 清零,开始新一轮累计)
│   └─ 否 ↓
│
├─ 门2(事件型): 16 个关键词中 ≥1 命中?
│   ├─ 是 → 进入门3 LLM 评分
│   │       (不清零,门2 触发后保留累计计数,允许多次连续触发)
│   └─ 否 → SKIP, reason: "no_trigger"
│
└─ 门3(质量门): LLM 评分 confidence >= 0.6 ?
    ├─ 是 → 提交 B
    └─ 否 → SKIP, reason: "low_confidence(0.XX)"
```

**关键设计点**:
- **门1 OR 门2**:任一达到就调 LLM
- **门1 触发后清零**:`cumulative_tokens=0, cumulative_tool_calls=0`,开始新一轮累计
- **门2 触发后不清零**:关键词型可以连续多次触发(只要每次都命中关键词 + LLM 评分过)
- **会话级累计**:每次 session 开始清零 `cumulative_tokens`

### 4.5 关键词列表(16 个)

```python
KEYWORDS = [
    # 显式要求
    "记住", "记一下", "帮我记住", "别忘了",
    # 决策类
    "偏好", "决策", "选择", "拒绝", "采用",
    # 反思类
    "教训", "经验", "原则",
    # 习惯类
    "总是", "从不", "永远", "习惯",
]
```

### 4.6 LLM 评分提示词(去重策略)

**门1 触发时,提示词包含"本周期已提取的记忆"**,让 LLM 自己去重(避免代码层去重导致上下文断层):

```xml
<existing_memories_in_this_period>
[user] 习惯用 uv (turn 5)
[project] 项目叫 agent-dev (turn 8)
</existing_memories_in_this_period>

<conversation>
[turn 6] ...
[turn 7] ...
[turn 8] ...
[turn 9] ...
[turn 10] ...
</conversation>

请基于以上评估,提取"本周期内"的新记忆(避免和已提取的重复)
```

**关键**:
- **门1 周期起点** = `gate1_period_start_turn`(本次新维护的状态)
- 每次门1 跑完后,该变量更新为 `current_turn + 1`
- 拼提示词时,查询 `MemoryStore.list_by_session(session_id, since_turn=gate1_period_start_turn)`

### 4.7 SM(L3) vs B(通道 B)的区别

| 维度 | B 记忆提取 | SM 摘要提取 |
|------|----------|----------|
| 目标产物 | 结构化记忆条目 | 滚动笔记 markdown |
| 写入位置 | `memory/{user,feedback,project,reference}/` | `memory/daily/...` |
| 消费者 | MemoryRetriever(检索) | /compact 压缩 |
| 是否入库 Chroma | ✅ 是 | ❌ 否 |
| 触发频率 | N turn 1 次(门过)| **每 turn 1 次**(启用后) |
| LLM 调用 | 0\~1/turn | 1/turn(启用后) |
| 当前状态 | ❌ Option C 绕过 | ✅ 已有(各管各的,本次不动) |

**两者并行不冲突**:A 持久化原文 / SM 滚动笔记(给压缩用)/ B 结构化记忆(给检索用)。

---

## 5. 组件 / 模块

### 5.1 新增

#### 5.1.1 `agent_core/memory/extraction_gate.py`

```python
@dataclass
class TurnContext:
    cumulative_tokens: int
    cumulative_tool_calls: int
    last_messages: list[dict]      # 给门2 关键词检测用
    gate1_period_start_turn: int  # 给 LLM 评分拼"已提过的"用

@dataclass
class Decision:
    should_extract: bool
    reason: str
    confidence: float
    candidates: list[ExtractionCandidate]
    via_gate1: bool                # 哪个门触发的(给 bridge 决定是否清零)

class ExtractionGate:
    MIN_TOKENS_TO_INIT = 10_000
    MIN_TOOL_CALLS = 10
    MIN_CONFIDENCE = 0.6
    KEYWORDS = [...16 个...]

    def should_extract(self, ctx: TurnContext) -> Decision: ...
    def _keyword_filter(self, last_messages) -> bool: ...
    def _llm_score_and_extract(self, ctx: TurnContext) -> ExtractResult: ...
```

#### 5.1.2 `agent_core/memory/react_memory_bridge.py`

```python
class ReactMemoryBridge:
    """
    适配层:把 ReactAgent.run() 的同步 generator 风格
    翻译成 DualChannelWriter 的 async future 风格
    """

    def __init__(self, dual_channel, gate, embed_fn, ...): ...

    def on_turn_end(
        self,
        user_msg: str,
        assistant_resp: str,
        turn_index: int,
        input_tokens: int,
        output_tokens: int,
        tool_calls_in_turn: int,
        last_messages: list[dict],
        recent_turns: list[TurnMessage],  # 供 LLM 评分用
    ) -> Iterator[MemoryEvent]: ...

    def recover_state(self) -> None:
        """A3 启动恢复:从 extract_cursor 恢复 gate1_period_start_turn"""

    def shutdown(self, timeout: float = 30.0) -> bool: ...
```

#### 5.1.3 `agent_core/memory/prompt_templates.py`

```python
EXTRACT_PROMPT_TEMPLATE = """分析以下对话,同时判断两件事:
1. 是否包含"值得长期记住"的新信息
2. 如果是,提取为结构化记忆

[... 完整模板见 §3.3 L1 + L9 修复 ...]
"""

EXTRACT_SYSTEM_PROMPT = """你是结构化记忆提取助手. 严格按 schema 输出 JSON."""
```

### 5.2 改造

#### 5.2.1 `agent_core/agent_core.py`

- **删**:`memory_extractor` / `memory_embed_fn` 构造参数
- **删**:`_extract_and_write()` 方法整段
- **删**:`run()` 末尾的 yield "🧠 正在提取" 段
- **加**:`react_memory_bridge: Optional[ReactMemoryBridge]` 构造参数
- **改**:`run()` 末尾:遍历 `bridge.on_turn_end(...)` 收 MemoryEvent,yield `("memory_event", event)`

#### 5.2.2 `web/app.py`

- **删**:`MemoryExtractor` / `make_embed_fn` 注入逻辑
- **改**:`get_agent()`:构造 `DualChannelWriter` + `ExtractionGate` + `ReactMemoryBridge`

### 5.3 行为对比

| 场景 | Option C 现状 | 严格实现后 |
|------|------------|----------|
| 短对话(累计 < 10K)| 强行每 turn 提 | 门1 不达;门2 不命中 → SKIP |
| 关键词命中"记住" | 必提 | 门2 命中 → 调 LLM 评分 |
| 累计 ≥ 10K | 强行提 | 门1 过 → 调 LLM 评分 |
| 门1 跑过 | 继续提 | **清零累计**,从 0 重新开始 |
| 进程崩溃 | 提取一半的 turn 丢 | A3 恢复,cursor 持久化 |
| LLM 调用次数/turn | 1(必) | **0\~1**(门3 才调) |
| 短对话"记住"不响应 | 不存在(强制提)| **接受**(不违背设计 §3.3 严格性) |

---

## 6. 数据契约

### 6.1 TurnContext

```python
@dataclass
class TurnContext:
    session_id: str
    cumulative_tokens: int           # 当前 session 累计 input + output
    cumulative_tool_calls: int       # 当前 session 累计 tool 调用次数
    last_messages: list[dict]        # 最近 4-6 条 {role, content}
    gate1_period_start_turn: int     # 当前门1 周期起点(给 LLM 评分拼"已提过的"用)
```

### 6.2 Decision

```python
@dataclass
class Decision:
    should_extract: bool
    reason: str                      # skip: no_trigger / skip: low_confidence(0.XX) / extract
    confidence: float
    candidates: list[ExtractionCandidate]
    via_gate1: bool                  # 门1 触发的(给 bridge 决定是否清零累计)
```

### 6.3 MemoryEvent(streamed to UI)

```python
class MemoryEventKind(str, Enum):
    CHANNEL_A_OK = "channel_a_ok"          # daily log 写完
    GATE_SKIP = "gate_skip"                 # 三级门拒绝
    GATE_PASS = "gate_pass"                 # 门过,提交后台
    EXTRACT_DISPATCHED = "extract_dispatched"  # 后台任务已派发
    EXTRACT_DONE = "extract_done"           # 后台提取完成(N 条)
    EXTRACT_ERROR = "extract_error"         # 后台失败

@dataclass
class MemoryEvent:
    kind: MemoryEventKind
    turn_index: int
    reason: Optional[str] = None
    candidates_count: int = 0
```

---

## 7. 错误处理

| 错误 | 处理 |
|------|------|
| **L1 解析失败**(LLM 没返 JSON) | MemoryEvent.EXTRACT_ERROR, reason=parse_error,日志存 raw,continues |
| **L7 source_quote 缺失** | 单条 candidate 拒,不阻断其他 |
| **L8 secret 命中** | 单条 candidate 拒,日志 warn |
| **channel_a 写盘失败** | **不推进 daily_cursor**,pending_writes 保留,启动重试 |
| **channel_b 写盘失败** | **不推进 extract_cursor**,pending_writes 保留,启动重试 |
| **embed_fn 失败** | DualChannelWriter 抛 EmbeddingError(M2 行为),bridge 捕获 → EXTRACT_ERROR,不写盘 |
| **跨进程锁 5s 没拿到** | LockBusy, EXTRACT_ERROR 日志, session 下次 turn 再试 |
| **进程退出时 in-flight** | atexit graceful shutdown 30s 超时(M2 A9) |
| **LLM 评分 confidence < 0.6** | SKIP, 不写盘, 不算入 extract_cursor 推进 |
| **门1 触发后 LLM 评分 < 0.6** | **不清零**累计(因为没真提) |

---

## 8. 测试

### 8.1 单元测试 `tests/test_extraction_gate.py`

- `test_below_10k_no_keyword` — 累计 < 10K + 无关键词 → SKIP
- `test_above_10k_no_keyword` — 累计 ≥ 10K + 无关键词 → 调 LLM 评分
- `test_below_10k_with_keyword` — 累计 < 10K + 关键词命中 → 调 LLM 评分(门2 主导)
- `test_above_10k_with_keyword` — 累计 ≥ 10K + 关键词命中 → 调 LLM 评分(门1 主导)
- `test_low_confidence` — LLM 评分 < 0.6 → SKIP, reason=low_confidence
- `test_high_confidence` — LLM 评分 ≥ 0.6 → extract + candidates
- `test_keyword_list_complete` — 16 个关键词都在列表里
- `test_turn_count_clears_after_gate1` — 验证累计清零逻辑(模拟)

### 8.2 集成测试 `tests/test_react_memory_strict.py`

- `test_channel_a_writes_daily_log` — turn 末尾 `~/.agent_data/logs/<session>.jsonl` 有 1 行
- `test_channel_b_writes_memory_files` — 门1 触发后 `~/.agent_data/memory/<type>/<hash>.md` 有新文件
- `test_gate1_clears_counter` — 门1 跑完后 `cumulative_tokens=0`
- `test_gate2_does_not_clear` — 门2 跑完后 `cumulative_tokens` 保留
- `test_concurrent_session_isolation` — 多 session 并行不串
- `test_crash_recovery` — kill 进程后重启, pending_writes 重做
- `test_prompt_has_existing_memories_block` — 门1 触发的 LLM 调用 prompt 含 `<existing_memories_in_this_period>`
- `test_dedup_via_prompt` — 同周期内重复内容,LLM 评分 0.3 → SKIP

### 8.3 回归

- M2 dual_channel_writer 已有测试不破
- M3 extractor 已有测试不破
- M7 memory_status 行为不破

---

## 9. 边界 / 风险

| 风险 | 缓解 |
|------|------|
| **LLM 评分不稳定**(confidence 0.5/0.7 跳变) | 0.6 阈值是经验值;可在 `ExtractionConfig` 调 |
| **关键词列表不全** | "我倾向 X"不命中;但这是设计本意,做严格 |
| **会话累计跨 session 误差** | 会话级累计,session 切换清零;接受 |
| **门1 跑得过于频繁**(每 10K 一次)| 跑完清零 → 实际约 10-15 turn 跑 1 次 |
| **LLM 评分去重不彻底** | MemoryStore.write 的 `item_hash` 兜底(已有)|
| **pending_writes 启动恢复** | 沿用 M2 A10,recover_pending() 接口 |
| **L3 SM 和 B 重复消耗 LLM** | 不去重;B 主要给"检索",SM 主要给"压缩",职责正交 |
| **短对话"记住"不响应** | 接受(违背设计);不修决策树 |

---

## 10. UI 行为

### 10.1 sidebar `🧠 Memory 状态`(已有,本次增强)

```
┌─ 🧠 Memory 状态 ──────────────┐
│ 启用状态: ✅ 后台异步提取      │
│                               │
│ 本会话累计: 3,500 tokens     │
│ 本会话 tool: 0               │
│                               │
│ Searches: 4(检索次数)         │
│ Total Hits: 2(命中数)         │
│                               │
│ Daily Log: ~/.agent_data/logs/<id>.jsonl (5 lines) │
│ Memory: ~/.agent_data/memory/ (6 files)          │
└───────────────────────────────┘
```

### 10.2 聊天区(无新增消息)

**严格实现后,聊天区不再显示 "🧠 正在提取" / "✅ 已写入"**:
- 通道 A 同步写完 daily log(无 LLM,无消息)
- 通道 B 异步提交后台,不阻塞 generator(无消息)
- 用户感知:对话流畅,**不被打断**
- 通过 sidebar 计数器 + logs 看到后台进度

### 10.3 短对话"记住"策略

**本设计不提供"短对话立刻提取"的快捷路径**:
- 短对话(累计 < 10K)且无关键词命中 → SKIP
- 用户说"记住"但无关键词命中 → SKIP
- 这是设计 §3.3 严格性的体现,不修

**用户行为预期**:
- 长对话(累计 ≥ 10K)自动周期提取
- 关键词命中(记住/偏好/习惯...)自动事件提取
- 想立刻记住:用长一点的对话,或包含关键词
- 不提供"绕过门"的 UI 开关

---

## 11. 实施计划概要

### 11.1 阶段 1:核心模块(无 ReactAgent 接入)

1. 新建 `agent_core/memory/extraction_gate.py`
2. 新建 `agent_core/memory/prompt_templates.py`
3. 单元测试 `tests/test_extraction_gate.py`

### 11.2 阶段 2:Bridge + DualChannelWriter 接入

1. 新建 `agent_core/memory/react_memory_bridge.py`
2. 改 `agent_core/agent_core.py` 删 Option C 接入 bridge
3. 集成测试 `tests/test_react_memory_strict.py`

### 11.3 阶段 3:UI + 文档

1. 改 `web/app.py` wiring
2. 更新 `docs/test_react_ui.md`
3. 更新 `docs/memory-system-design.md`(已在本次 commit 完成 §3.3.1 + §4.8 + §6.9)

---

## 12. 验收标准

### 12.1 功能验收

- [ ] 累计 < 10K + 无关键词:不写盘
- [ ] 累计 ≥ 10K:调 LLM 评分,≥ 0.6 写盘
- [ ] 关键词命中:调 LLM 评分
- [ ] 门1 跑完:累计清零
- [ ] 门2 跑完:累计不清零
- [ ] 进程崩溃重启:cursor 恢复,pending_writes 续做
- [ ] UI 聊天区不显示 "🧠" 同步消息
- [ ] sidebar 计数器递增
- [ ] 无 toggle 设计,短对话不响应(接受)

### 12.2 不变量验收(对齐 §4.5)

- [ ] #3 通道 A 不调 LLM
- [ ] #4 通道 B 推进 extract_cursor 前必须成功写入
- [ ] #5 L7 source_quote 必填
- [ ] #6 L8 secret 过滤
- [ ] #7 project/reference 含 `**Why:**` 段

### 12.3 测试验收

- [ ] `pytest tests/test_extraction_gate.py -v` 全过
- [ ] `pytest tests/test_react_memory_strict.py -v` 全过
- [ ] 回归 M2/M3/M7 测试不破
