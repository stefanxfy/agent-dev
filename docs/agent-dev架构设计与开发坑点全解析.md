# agent-dev 架构设计与开发坑点全解析

> 整理时间：2026-06-18 | 项目周期：2026-06-09 ~ 2026-06-18（约10天）

---

## 一、项目定位

从零手写一个**生产级 Agent 系统**，不走 LangChain 等框架路线。所有核心逻辑（ReAct 循环、工具系统、上下文管理、记忆系统、多 Agent）全部自研。

目标是**边开发边理解底层原理**，通过对比框架设计（LangGraph）加深对 Agent 架构的理解。

---

## 二、整体架构

```
agent-dev/
├── agent_core/          # 自研 ReAct 核心（主路线）
│   ├── agent_core.py    # ReAct 循环 + 工具执行 + 流式输出
│   ├── config.py        # LLM 配置管理 + env 变量集中（E-1）
│   ├── types.py         # MessageRole 枚举（P2-9）
│   ├── exceptions.py    # 统一异常体系 AgentError 根类（P2-10）
│   ├── context/         # 上下文管理系统
│   │   ├── budget.py    # Token 预算管理 + Baseline 持久化
│   │   ├── compact.py   # 压缩编配器（Fork + 旧模式，含 P1-5 质量检查）
│   │   ├── manager.py   # ContextManager 对外接口
│   │   └── tokenizer.py # 三层 token 计数（tiktoken + 5类启发式）
│   ├── llm/
│   │   └── router.py    # LLM 路由（OpenAI / Zhipu / Anthropic / MiniMax + P1-8/P1-9 重试）
│   ├── memory/
│   │   └── daily.py     # 每日日志 + 蒸馏引擎 autoDream（设计未实现）
│   ├── session/
│   │   ├── storage.py   # JSONL 存储 + read_tail O(1) 读取
│   │   ├── manager.py   # 会话管理（创建/切换/删除/重命名，P0-3 去重守卫 + P2-8 标题守卫）
│   │   └── restore.py   # 状态重建（parentUuid 链）
│   └── tools/
│       ├── base.py       # Tool 定义基类
│       └── builtin.py    # 内置工具（Calculator / Search）
│
├── langgraph_agent/     # LangGraph 重构版（教学对比）
│   ├── state.py          # AgentState 定义
│   ├── nodes.py         # LLM 节点 / 工具节点
│   ├── graph.py         # 图构建 + 边定义
│   └── agent.py         # LangGraph Agent 封装
│
├── web/                  # Streamlit UI（双入口）
│   ├── app.py           # 自研 ReAct UI（主入口）
│   ├── app_langgraph.py # LangGraph UI
│   └── pages/
│       ├── 00_Chat.py   # 聊天界面
│       └── 01_Session_Management.py  # 会话管理
│
└── docs/                # 文档（13份）
    ├── context-management-implementation-design.md     # 上下文管理实现（~92KB）
    ├── context-compaction-token-estimation-theory.md  # 压缩与估算理论（~33KB）
    ├── memory-system-design.md                        # 记忆系统设计（~25KB）
    ├── agent-dev-开发规则与经验汇总-2026-06-18.md     # 经验汇总（~21KB）
    └── langgraph-vs-agentcore-implementation.md       # 框架对比（~37KB）
```

### 架构原则

1. **极简依赖**：只装必须的 SDK，不装框架
2. **共用层不重复**：LLM Router 和 ToolRegistry 两个版本共用一套
3. **单 master 分支**：自研版和框架版通过独立目录和不同 UI 入口共存对比
4. **回退路径同等严谨**：主路径（tiktoken）和回退路径（5类启发式）都经校准

---

## 三、核心模块详解

### 3.1 ReAct 循环（agent_core.py）

自研 ReAct 循环是整个 Agent 的心脏，分层实现：

```
┌─────────────────────────────────────────────┐
│                   Agent                     │
│  ┌───────────────────────────────────────┐  │
│  │           _run_loop()  ← 主循环        │  │
│  │  1. 拼接消息历史                        │  │
│  │  2. 调用 LLM（流式 / 非流式）            │  │
│  │  3. 解析 tool_use / tool_result        │  │
│  │  4. 执行工具（ThreadPoolExecutor 并行）  │  │
│  │  5. 追加消息 → 继续循环                 │  │
│  │  6. 达 max_turns → 返回 final answer   │  │
│  └───────────────────────────────────────┘  │
└─────────────────────────────────────────────┘
```

**关键设计**：

- **Function Calling 完整流程**：解析 `tool_use` → 执行工具 → 追加 `tool_result` → 继续循环。Day2 代码即完整实现 Function Calling 标准流程。
- **并行工具调用**：ThreadPoolExecutor + `as_completed`，需 Prompt 明确引导"同时执行"才生效（LLM 默认串行）。
- **流式输出**：StreamWriter 注入，按 writer 输出增量 token。
- **Thinking 可视化**：检测 `chunk.thinking_delta`（仅 Claude 3.7+ 支持）。
- **错误处理**：双层（LLM 调用失败 fatal + 流式中断非 fatal）、指数退避重试（1s→2s→4s）。

### 3.2 Token 预算管理（budget.py）

Token 预算是上下文管理的核心信号，通过 `_ContextBudgetManager` 实现：

```python
class _ContextBudgetManager:
    def set_baseline(self, usage: dict)        # 记录 API 返回的 input_tokens
    def add_incremental(self, messages: list)   # 增量估算新增消息
    def estimate_tokens(self, messages)          # 当前总 token 估算
    def should_compact(self) -> bool            # 是否触发压缩
    def compact_trigger_tokens(self)             # 触发压缩的阈值（窗口 × 80%）
```

**Baseline 持久化**：每次 API 返回后，将 `usage.input_tokens` 持久化到 jsonl entry 的 `usage` 字段。Agent 重建时（Streamlit 重启/F5 刷新），从最后一条恢复 baseline，避免增量估算累积误差。

### 3.3 Preserved Head 算法（compact.py）

Preserved Head 决定压缩后保留多少条最近消息。v1→v4 演进过程揭示了真实需求：

| 版本 | 策略 | 问题 |
|------|------|------|
| v1 | 配对 + 4000 token 截断 | 灌水严重（30条保留4条） |
| v2 | drop-not-truncate，留"洞" | 用户不满：开头内容神秘消失 |
| v3 | 软 budget + 硬 max_turns | 逻辑混乱 |
| **v4（最终）** | **硬 stop，第一个 turn 必含** | ✅ 用户指令硬要求不超过 budget |

**v4 算法**（`compact.py:242-336`）：

```
从最近 turn 到最早 turn 遍历：
  if 已选够 max_turns → break（硬停）
  if 已选 ≥ 1 条 AND total + turn_tokens > max_total_tokens → break
  selected_turns.insert(0, turn)  # 倒序，最终是最近在前
```

**核心原则**：用户指令"不超过 budget"，不需要上下文连续性，第一个 turn 必含（即使灌水超 budget）。

### 3.4 Fork 压缩与 GLM Prompt Cache（compact.py）

Fork 压缩是 agent-dev 相对于 Claude Code 最重要的差异化设计，利用 GLM 的 prompt cache 机制降低压缩成本：

```
主对话 API 调用：  [system + tools + messages] → API → [response]
                                          ↑ 建立 prompt cache

Fork 压缩调用：   [system + tools + messages] → 复用 cache → [summary]
                               ↑ 完全相同的前缀 → 命中率 100%
```

**三层字节级一致性保证**：
1. `parent_system`：与主对话 system prompt 完全相同
2. `parent_tools`：与主对话 tool schema 完全相同
3. `parent_messages`：完整保留主对话所有消息（不是压缩后的精简版）

**A/B 测试结果**（2026-06-18）：

| 指标 | A 原模式 | B Fork 模式 |
|------|---------|------------|
| cached_tokens | 1,984（7%） | 25,664（100%）|
| 总 tokens | 27,279 | 26,527 |

**限制**：冷启动（无缓存）时，Fork 模式 token 消耗反而更高，需靠主对话首次 API 调用自动建立缓存。

### 3.5 三层 Token 计数（tokenizer.py）

```
┌──────────────────────────────────────┐
│  SimpleTokenCounter.count(text)      │
├──────────────────────────────────────┤
│ L1: tiktoken o200k_base（优先）       │  精确，GLM-4 偏差 +27%~+40%
│ L2: 5类启发式（回退）                 │  经 tiktoken 校准的近似
│ L3: 字面长度（兜底）                  │  完全无法估算时的保底
└──────────────────────────────────────┘
```

**5 类启发式回退**（经 tiktoken 对照校准）：

| 类别 | 比率 | 说明 |
|------|------|------|
| 中文 | 0.45 tok/字 | 实测偏差 ±15% |
| 英文 | 0.22 tok/字 | 含空格 |
| 代码 | 0.33 tok/字 | 特殊符号密集 |
| 数字 | 0.45 tok/字 | 数字与标点混合 |
| 其他 | max(0, total - 4类) | 边界字符 |

**设计原则**：回退路径与主路径同等严谨，不能因为"反正不走"就放水。所有常量经 tiktoken 对照验证。

### 3.6 LLM 路由（router.py）

支持多 Provider 统一路由，核心接口：

```python
class LLMRouter:
    def chat(messages, tools=None, system_prompt_override=None,
             tool_choice=None, stream=True, **kwargs) -> Iterator[Chunk]
    def count_messages(messages) -> int
    def get_model_config(model) -> dict
```

**Provider 支持**：
- OpenAI（GPT-4o 等）
- Zhipu（GLM-5.1，工具调用 + prompt cache）
- Anthropic（Claude 3.7+，thinking blocks）
- MiniMax（偶有 400 错误）

**system_prompt 传递规范**：
- **Anthropic**：必须作为顶层 `system` 参数传递，不能混入 messages
- **OpenAI / Zhipu**：可用 `role=system` 放在 messages 开头

**tool_choice 透传**：支持将 `tool_choice` 透传到具体 Provider（已修复 router.py 层透传，commit bc86c89）。

**P1-8 / P1-9 错误处理与重试**（2026-06-18）：

`_stream_with_retry()` 统一包装层处理两类错误：

1. **HTTP 错误分类**（`_classify_http_error`）：
   - `4xx (除 408/429)` → 不重试，直接抛（401/403/404/422 都是不可恢复）
   - `408 / 429 / 5xx` → 重试，指数退避
   - **小厂商 400 软重试**（P1-8）：MiniMax 等偶发 400（可能是格式问题），仅 1 次重试

2. **Stream 中断检测**（`_is_stream_interruption_error`，P1-9）：
   - `IncompleteRead` / `ChunkedEncodingError` / `ProtocolError`
   - `ConnectionResetError` / `BrokenPipeError`
   - GLM-5.1 实测 9-112s 延迟抖动，偶发 stream 截断
   - 重试上限 2 次（`LLM_MAX_STREAM_RETRY`）

3. **env 变量集中管理**（E-1）：`MAX_STREAM_RETRY` / `MAX_REQUEST_RETRY` / `RETRY_BACKOFF_BASE` 现在可通过 `LLM_MAX_STREAM_RETRY` 等 env 覆盖，从 `config.llm_max_stream_retry` 读取。

### 3.7 会话管理（session/）

JSONL 存储设计，避免 SQLite/JSON 的跨平台和并发问题：

```
data/sessions/
├── {session_id}.jsonl      # 每条消息一个 JSON Lines
│   ├── parent_uuid          # 父消息 UUID（断链检测）
│   ├── chunk.usage          # API usage 统计（input/output/cached）
│   ├── turn_count           # turn 编号
│   └── tool_calls           # 工具调用记录
└── sessions.db             # 元数据（会话列表）

O(1) 读取最后一条：tail -c 65536 {session_id}.jsonl
```

**read_tail O(1) 恢复**：从 jsonl 文件末尾 64KB 读取，通过 `last_agent_config` 缓存四个关键字段（provider/model/system_prompt/max_turns）实现 Agent 重建，无需全量解析。

**parentUuid 链**：每条消息记录父消息 UUID，类比 Git parent 引用，实现精确状态重建和断链检测。

---

## 四、开发过程中遇到的坑

### 4.1 Streamlit 热重载导致日志重复

**现象**：Streamlit 热重载（代码修改触发）后，同一个 handler 被添加两次，导致日志输出重复。

**根因**：`logging.root.addHandler()` 在每次 import 时调用，Streamlit 热重载触发模块重载，handler 累积。

**修复**：用 `isinstance(h, logging.StreamHandler)` 检测已存在的 handler，防止重复添加。

```python
if not any(isinstance(h, logging.StreamHandler) for h in logging.root.handlers):
    logging.root.addHandler(handler)
```

**教训**：热重载环境（Streamlit / Jupyter）中的模块级副作用（handler、缓存）需要显式防护。

---

### 4.2 UI 变量名冲突：thinking_text / turn_thinking

**现象**：`app.py` line 883 NameError `thinking_text`，紧接着 line 881 AttributeError `int.values`。

**根因**：`turn_thinking` 变量名同时承载两种不同类型：
- **dict 类型**：来自 `chunk.thinking` 的完整思考内容（用于 Thinking 可视化）
- **int 类型**：来自 token 计数的思考 token 数（用于侧边栏显示）

两段代码路径都叫 `turn_thinking`，P5 重构遗留，未覆盖代码（pytest 不跑 Streamlit）。

**修复**：
- 拼 string 版本：`f"思考 ({turn_thinking}s)"` 解决 dict+string 拼串
- 最终改名：`turn_thinking` → `turn_thinking_tokens`（明确 int 类型）

**教训**：不同数据类型不要复用同一个变量名；Streamlit UI 代码需要端到端测试覆盖。

---

### 4.3 F5 刷新 Token 数字跳变

**现象**：浏览器 F5 刷新后，侧边栏 Token 数字从 64,967 跳到 90,268（+25,301，+38.9%）。

**根因链**：

```
F5 刷新 → Agent 重建 → _baseline_valid 归零
        → 从增量估算（API 真实 input_tokens）
          切换到全量估算（SimpleTokenCounter 字面累加）
        → 差额 = GLM cache 命中省的部分
```

**诊断过程**（2026-06-17 22:11-23:35）：
- 22:11 用户报 bug，开始诊断
- 22:17 提出 4 方案（检测折算 / UI 双显示 / 压缩路径 / baseline 持久化）
- 22:49 选方案 B（usage 持久化）
- 23:06 发现性能问题（cache.json O(n) 读取，7.5ms vs 0.23ms）
- 23:20 最终方案：删 cache.json，改用 read_tail O(1)

**修复**：`_restore_usage_baseline()` 两步走：
1. O(1)：从 jsonl tail 64KB 读取最后一条 entry 的 `chunk.usage`
2. O(n) 兜底：全量扫描（仅极端大消息场景触发）

**关键修复**：用 `len(self.messages) - 1` 而非 `len(self.messages)` 计算 baseline_msg_count，因为第 4 条 final answer 还未 append 时 len 多计 1，API 只看到前 3 条。

---

### 4.4 Preserved Head v2 "drop not truncate" 用户不满

**现象**：v2 算法丢弃超预算消息但不截断，留下"空洞"，用户反馈开头内容神秘消失。

**根因**：设计时假设上下文连续性重要，但实际用户对话中，开头通常是关键指令和设定。

**v3 修复**：第一个 turn 必含（不丢弃），后续 hard stop。但 v3 有逻辑混乱问题（软 budget + 硬 max_turns 混合）。

**v4 最终解**：硬 stop，第一个 turn 必含。理由：用户指令硬要求不超过 budget，不需要上下文连续性。

**教训**：压缩算法不能假设用户偏好，必须明确用户意图。用户对"内容消失"的容忍度远低于"token 超出预算"。

---

### 4.5 cache.json O(n) 性能陷阱

**现象**：读取 session_state 缓存（cache.json）耗时 7.5ms，全量解析所有历史消息。

**根因**：cache.json 存储全量消息列表，每次读取需要解析所有历史，n = 消息总数。

**修复**：完全删除 cache.json，改用 jsonl tail 读取 + `last_agent_config` 缓存关键字段。性能提升 33 倍（7.5ms → 0.23ms）。

**删除过程**（2026-06-17 23:08-23:20）：
- 删除 3 个相关方法
- 删除 sidebar 显示逻辑
- 删除测试文件相关用例
- 总计 -269 行

**教训**：sidecar 文件设计要考虑 O(n) 读取成本，极端场景（数千条消息）会放大性能问题。

---

### 4.6 GLM-5.1 工具调用消息格式 Bug

**现象**：GLM 模型返回的 `tool_result` 消息格式与 OpenAI 标准不同，导致 ReAct 循环中断。

**根因**：GLM 的 tool_result 消息中，结果可能以不同结构返回（取决于 Provider 实现）。

**Day2 修复**：在 `agent_core.py` 中识别 `tool_result` 消息格式，统一处理后再追加到消息历史。

**教训**：多 Provider 路由时，消息格式归一化必须在 router 层处理，不能假设各 Provider 输出格式一致。

---

### 4.7 LangGraph 节点函数签名类型错误

**现象**：`add_node` 触发 UserWarning："expected RunnableConfig, got dict"。

**根因**：节点函数 `config` 参数类型注解写成了 `dict`，LangGraph 期望 `RunnableConfig`。

**修复**：改为 `from langgraph.checkpoint import RunnableConfig` 导入并使用正确类型注解。

**教训**：LangGraph 对类型注解要求严格，函数签名必须与框架期望一致。类型错误不一定导致运行时崩溃，但会触发 Warning。

---

### 4.8 Parallel Tool Calls 需要 Prompt 明确引导

**现象**：实现了 ThreadPoolExecutor 并行执行，但 LLM 仍然串行调用工具。

**根因**：并行执行是代码层面的实现，需要 Prompt 层面明确告诉 LLM"同时执行以下任务"，LLM 默认倾向于串行（因为大多数 API 调用存在因果依赖）。

**修复**：在 System Prompt 中添加并行执行引导，或在用户消息中明确说"同时执行"。

**教训**：LLM 的行为受 Prompt 约束，代码能力（并行执行）≠ LLM 行为（串行执行）。两者需要匹配。

---

### 4.9 GLM-5.1 不支持 Claude thinking blocks API

**现象**：Anthropic Claude 3.7+ 支持 thinking blocks，`chunk.thinking_delta` 可以拿到增量思考 token。但 GLM-5.1 没有这个 API。

**设计决策**：通过 Prompt 让 GLM 输出【思考】标记，在 UI 上用思考样式展示。虽然不是原生 thinking API，但用户体验接近。

**教训**：不同 Provider 的 API 能力差异很大，跨 Provider 开发时需要为每个能力准备降级方案。

---

### 4.10 Fork 压缩路径单元测试不覆盖真实 LLM

**现象**：`conversation_text` NameError bug 被漏掉，因为 Fork 压缩路径的单元测试不调用真实 LLM。

**根因**：Fork 压缩依赖 GLM API，单元测试 mock 掉了 LLM 调用，绕过了真实代码路径。

**教训**：
1. 单元测试 mock 是双刃剑：保证测试稳定性，但也可能漏掉真实路径 bug。
2. Fork 压缩的 cache 命中验证只能在浏览器实测，无法在单元测试中验证。
3. 对关键路径（压缩/LLM 调用）需要设计端到端测试，即使慢也要跑。

---

### 4.11 jsonl 存储并发写问题

**现象**：多个进程同时 append jsonl 可能导致 Entry 交错（Entry N 和 Entry N+1 内容混合）。

**根因**：jsonl 是追加写文件，多进程并发时没有原子性保证。

**缓解**：会话锁（Session-level lock）保证同一会话同时只有一个写操作。未来可考虑 WAL 模式或进程锁。

---

### 4.12 chat API 流式中断导致状态不一致

**现象**：流式 API 返回过程中网络中断，已 append 的消息不完整。

**根因**：流式响应是增量写入，发生中断时只写了部分内容。

**修复**：使用双缓冲（先收集完整响应，校验后再一次性 append）。若中断发生在校验前，丢弃不完整 Entry。

---

## 五、设计原则总结

### 5.1 核心原则

| 原则 | 描述 |
|------|------|
| **极简依赖** | 只装必须的 SDK，框架能不用就不用 |
| **自研优先** | 核心逻辑（ReAct/工具/压缩/记忆）全部自研 |
| **对比学习** | 自研版和 LangGraph 版共存，通过对比理解框架设计 |
| **数据说话** | 所有设计决策基于实测（A/B 测试 > 理论推演） |
| **用户意图** | 压缩/缓存等行为优先满足用户显式指令 |
| **同等严谨** | 回退路径与主路径同样严格，不放水 |
| **不编造** | 不确定就说不知道，不凭印象编 |

### 5.2 上下文管理原则

| 原则 | 描述 |
|------|------|
| **Baseline 持久化** | API input_tokens 持久化，F5 刷新不跳变 |
| **硬 stop** | Preserved Head 严格控制预算，不软超 |
| **Fork 缓存** | 压缩复用主对话前缀，GLM 100% 命中 |
| **O(1) 读取** | read_tail 64KB 窗口 + last_agent_config |
| **灌水防御** | 从 max_tokens 源头限制，不靠检测 |

### 5.3 工程原则

| 原则 | 描述 |
|------|------|
| **同名不混类型** | 不同数据类型不共用变量名 |
| **热重载安全** | 模块级副作用（handler/缓存）需防护 |
| **类型注解正确** | LangGraph/其他框架的类型要求严格遵守 |
| **端到端测试** | 关键路径（压缩/LLM）不能只靠 mock |
| **日志防重复** | isinstance 检测已有 handler |
| **session_state 重建** | 配置变更必须显式触发 Agent 重建 |

---

## 六、四阶段学习路径

| 阶段 | 内容 | 状态 |
|------|------|------|
| Stage 1 | 自研 ReAct 循环（Day1-2） | ✅ 完成 |
| Stage 2 | LangGraph 框架重构对比（Day4） | ✅ 完成 |
| Stage 3 | 记忆系统（设计完成，代码未实现） | ⏳ 待实现 |
| Stage 4 | 非 Docker 原生沙箱 | ⏳ 待实现 |

---

## 七、关键文件索引

| 文件 | 作用 |
|------|------|
| `agent_core/agent_core.py` | ReAct 循环核心，~600 行 |
| `agent_core/context/compact.py` | 压缩编配器，Preserved Head v4 |
| `agent_core/context/budget.py` | Token 预算管理，Baseline 持久化 |
| `agent_core/context/tokenizer.py` | 三层 token 计数 |
| `agent_core/llm/router.py` | 多 Provider 路由 |
| `agent_core/session/storage.py` | JSONL 存储，read_tail O(1) |
| `web/app.py` | Streamlit 主 UI |
| `docs/context-management-implementation-design.md` | 上下文管理完整文档 |
| `docs/memory-system-design.md` | 记忆系统设计文档 |
| `docs/langgraph-vs-agentcore-implementation.md` | 框架对比文档 |
