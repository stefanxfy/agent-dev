# 记忆系统完整实现方案

> 参考来源：QClaw 记忆系统 + Mem0 核心概念 + Claude Code 记忆设计（详见 [`claude-code-memory-system-deep-dive.md`](claude-code-memory-system-deep-dive.md)）
> 项目：agent-dev（自研 Agent 框架）
> 日期：2026-06-19（v2，基于 Claude Code 源码 deep-dive 全面升级）
> 状态：方案文档（v2 设计已完成，待实现）

---

## 〇、变更日志（Changelog）

| 版本 | 日期 | 变更摘要 | 触发 |
| --- | --- | --- | --- |
| v1 | 2026-06-11 | 初稿：三层架构 + Chroma + autoDream | 原始需求 |
| v2 | 2026-06-19 | **架构升级**：补 L3 会话内压缩、双通道写入、封闭分类法、Why-How 模板、Edit-only 沙箱、token 阈值、配置/UI/安全章节 | Claude Code 源码 deep-dive 对比 |
| v2.1 | 2026-06-20 | **配置/检索升级**：Pydantic 配置校验 + 三模式共存（vector/file/hybrid）+ Hybrid 量化指标 + 锁粒度拆分 + Windows 路径兼容 | 24 项专业审查 |

### 〇.1 版本时间线（演进路径）

```
v1 (2026-06-11)                    v2 (2026-06-19)                   v2.1 (2026-06-20)
   │                                  │                                  │
   ├─ 三层架构 (daily/vector/memory)    ├─ + L3 会话内压缩                ├─ + Pydantic 配置
   ├─ Chroma 强制                      ├─ + 双通道写入                    ├─ + 三模式共存 (vector/file/hybrid)
   ├─ 单 MEMORY.md                     ├─ + 封闭 4 类                     ├─ + Hybrid 量化指标
   ├─ 关键词匹配触发                   ├─ + Edit-only 沙箱                ├─ + 锁粒度拆分
   ├─ 单一 LLM 摘要                    ├─ + token 阈值 + 工具数阈值        ├─ + Windows 路径兼容
   └─ 单文件 ≤200 行                    ├─ + 5 条回退条件                   └─ + 24 项审查修正
                                       └─ + per-file 记忆
```

**v1 → v2 主要变化**：补 Claude Code 的"分层压缩"思想（fast path / slow path 分离）。
**v2 → v2.1 主要变化**：补配置工程（fail-fast 校验）+ 检索灵活性（三模式可切换 + 量化决策依据）。

> **本节用途**：让读者一眼看清"我看的版本"和"该看什么章节"——读者可以根据 v1/v2/v2.1 的差异定位自己关心的变更。

---

## 一、核心设计理念

> **🆕 标记说明**：本文用 🆕 标记 v2 相对 v1 的新增项；v2.1 的新增项用 🆕¹ 标记。所有 🆕 项在 §〇.1 版本时间线里有完整列表。

| 设计原则 | 说明 |
|---------|------|
| **Append-only 日志** | 原始日志永不覆写，保证无损记录 |
| **三层分离** | 日常日志（原始）→ 索引（检索，可选向量）→ 长期记忆文件（精炼）。**注：第 2 层（向量索引）从 v2.1 起是 `mode=vector` 选项，可关闭——见 §6.6** |
| **变化慢的才存** | 偏好/决策/约束/教训 → 存；代码/临时状态/中间推理 → 不存 |
| **时间衰减** | 记忆有生命周期，久未访问的自动降低权重 |
| **人类审核** | LLM 提取的建议需要用户确认才能写入长期记忆 |
| **独立 Agent 提取** | 新建独立 Agent 做记忆提取，不污染主对话上下文 |
| **🆕 会话内滚动摘要** | 长会话有 L3 滚动摘要作为 fast path，避免每次都重摘要 |
| **🆕 双通道写入** | 主 agent 内联写 + 后台 fork agent 异步提取，互斥防冲突 |
| **🆕 封闭分类法** | 4 类固定（user / feedback / project / reference），LLM 不能发明第五类 |
| **🆕 工具沙箱** | 提取 agent 只能用 Edit 工具改指定路径，无法越界 |
| **🆕 文件级记忆** | 一条记忆一个文件 + 索引，长期记忆不再受单文件大小限制 |
| **🆕¹ 三模式检索** | vector / file / hybrid 三模式可切换（详见 §6.6） |
| **🆕¹ Pydantic 配置** | 启动时 fail-fast 校验,避免拼错 key 静默回退（详见 §12.5） |

> **解释**：v1 → v2 的最大变化是引入了 **Claude Code 的"分层压缩"思想**——把"会话内压缩"（L3 快路径）和"跨会话整合"（L5 慢路径）分开，避免每次压缩都让 LLM 重新摘要所有历史。详细对比见 [`claude-code-memory-system-deep-dive.md` §0](claude-code-memory-system-deep-dive.md)。
> 
> **v2.1 关键澄清**："三层分离"里中间那层（向量索引）不再是必选——它从 v2.1 起降级为 `mode=vector` 可选项,生产环境推荐纯文件（`mode=file`）或 hybrid（`mode=hybrid`），避免 Chroma 维护成本。详见 §6.6.2。

---

## 二、整体架构（v2）

```
┌────────────────────────────────────────────────────────────┐
│                       用户对话                              │
└───────────────────────────┬────────────────────────────────┘
                            │
                 ┌──────────┴──────────┐
                 │   主 Agent 运行时    │
                 │  1. 检索相关记忆     │  ← 每次 run() 前向量召回 top-k
                 │  2. 执行 ReAct 循环  │
                 │  3. 内联写记忆       │  ← 🆕 双通道 A：主 agent 直接写
                 │  4. 返回响应         │
                 └──────────┬──────────┘
                            │ turn-end
              ┌─────────────┴─────────────┐
              ▼                           ▼
   ┌────────────────────┐      ┌───────────────────────┐
   │ L3 会话内压缩        │      │ L4 后台提取 Agent      │
   │ (SessionMemory-like) │      │ (forked, cache-safe) │
   │ 滚动摘要, 零 LLM     │      │ 异步跑, 增量更新      │
   └──────────┬─────────┘      └──────────┬────────────┘
              │ (token 超阈值时)            │
              ▼                           ▼
       替换早期上下文               写入:
                                    ├─ 日常日志 (append-only)
   长期记忆                          ├─ 向量索引 (Chroma)
   ┌──────────┐                     └─ 长期记忆文件 (per-memory file)
   │ 每条记忆 │
   │ 一个文件 │  ← 🆕 v2: 不再受单文件大小限制
   └─────┬────┘
         │
         ▼
   蒸馏引擎 (autoDream)
   24h + 5 session + 锁
   LLM 整合 + 冲突调和
   生成 candidate 文件
   用户 diff review
```

### 2.1 v1 vs v2 架构对比

| 维度 | v1 | v2（本次升级） |
| --- | --- | --- |
| **压缩路径** | 仅靠 LLM 一次性摘要 | L3 滚动摘要（零 LLM）+ L4 后台提取（增量）+ L5 蒸馏（跨会话） |
| **写入路径** | 只有后台异步 | **双通道**：主 agent 内联 + 后台异步，互斥防冲突 |
| **分类法** | 4 类但可扩展 | **封闭 4 类**（user/feedback/project/reference），LLM 不能发明 |
| **记忆粒度** | 单 MEMORY.md（≤200 行） | **每条记忆一个文件** + 索引文件 |
| **写工具** | LLM 返回 JSON → 解析写入 | **Edit 工具直接改文件**（schema 不漂移） |
| **提取频率** | 关键词匹配 | **token 阈值 + tool 次数阈值** + 关键词补充 |
| **回退设计** | 无 | **5 条回退条件**（gate 关 / 文件空 / 模板态 / 提取中 / 仍超阈值） |

> **解释**：v2 的核心理念是 **"压缩分层、写入双轨、存储分离"**——短期压缩（零 LLM）和长期整合（LLM 介入）分开；写入既有"实时内联"也有"异步兜底"；存储不再是单文件瓶颈。

### 2.2 与 Claude Code 7 层架构的对应关系

| Claude Code 层 | agent-dev v2 对应 | 实现章节 |
| --- | --- | --- |
| L1 系统提示层（CLAUDE.md + MEMORY.md 索引） | 启动时加载 `MEMORY.md` 索引 | §五 Layer 3 |
| L2 按需召回（`findRelevantMemories`） | `MemoryStore.search()` 语义检索 | §六 |
| **L3 会话内压缩（SessionMemory）** | 🆕 **会话内滚动摘要机制** | §四.4 |
| L4 写入提取（`extractMemories` + fork agent） | 🆕 双通道写入（主 + 后台） | §四.1 |
| L5 跨会话整合（`autoDream`） | autoDream 蒸馏引擎 | §七 |
| L6 同步层（`teamMemorySync`） | ❌ 不实现（单用户场景） | — |
| L7 安全模型（path + sandbox） | 🆕 工具 + 路径沙箱 | §十四 |

---

## 三、存储触发机制（v2：阈值 + LLM 评分）

### 3.1 触发时机

记忆存储发生在 **turn-end（用户消息处理完毕）**，由**双通道**处理：

```
用户发送消息 → 主 Agent 响应 → turn-end
                              │
              ┌───────────────┴───────────────┐
              │           双通道                 │
              │                                │
   通道 A（主 agent 内联写）    通道 B（后台异步提取）
   ├─ 用户说"记住这个"        ├─ 阈值触发（token / tool count）
   ├─ 写到 daily log          ├─ 后台跑 fork agent
   └─ 推进 cursor              └─ 写 daily + vector + memory
                                                │
                                                ▼
                                          autoDream 蒸馏
                                          24h + 5 session
                                          整合 → 候选 → 审核
```

### 3.2 触发类型

| 触发类型 | 时机 | 触发者 | 结果 |
|---------|------|-------|------|
| **🆕 内联写（通道 A）** | turn-end 用户显式"记住这个" | 主 Agent 直接调用记忆工具 | 立即写 daily log + 推进 cursor |
| **后台提取（通道 B）** | 累积 ≥10K tokens 或 ≥10 tool calls | 后台 fork agent（cache-safe） | 写 daily + vector + memory |
| **定时蒸馏** | 每 24h + 会话≥5 | DistillationScheduler | 生成 per-memory candidate 文件，用户 review |
| **主动检索** | 用户问"你记得吗" / 每次 run() 前 | MemoryStore.search() | 从向量库语义检索 |
| **🆕 L3 会话内压缩** | token 数逼近阈值 | 滚动摘要（零 LLM） | 替换早期上下文 |

### 3.3 提取频率控制（v2：token 阈值 + LLM 评分）

**v1 关键词匹配已弃用**（仅在 §4.0.2 v1 原版代码中保留作为对照参考）：

```python
# ⚠️ v1 设计（已弃用）：纯关键词匹配，太脆
# 仅在 §4.0.2 v1 原版中保留, 当前实现见下方 v2
v1_keywords = ["偏好", "决策", "选择", "拒绝", "采用", "教训", "经验", "原则"]
# 缺陷详见下方说明
```

v1 关键词匹配的核心问题：
- **漏报率高**：用户说"我一般用 X"不命中任何关键词 → 重要偏好被漏掉
- **误报率高**：用户说"你总是这样"命中"总是"，但用户在抱怨而非陈述偏好 → 假阳性触发无效提取
- 关键词清单是 LLM 的"语义过滤器"，本质上是个**分类器**——不如直接让 LLM 做分类

**v2 改进：token/tool 阈值 + LLM 评分 + 关键词补充**：

```python
class ExtractionGate:
    """
    v2 提取频率控制：三级门 + LLM 评分
    
    为什么不只用关键词?
    → 关键词清单本质是个粗糙的分类器, 不如直接让 LLM 评一次
    → 但每次评分也是 LLM 调用, 所以要用 token 阈值先过滤
    """

    def __init__(self, config: ExtractionConfig):
        self.config = config
        # 阈值参考 Claude Code: tengu_session_memory 下的配置
        # src/services/SessionMemory/sessionMemory.ts
        self.min_tokens_to_init = 10000       # 累积 10K tokens 才启动 extraction
        self.min_tokens_between_updates = 5000  # 两次提取间隔至少 5K tokens
        self.tool_calls_between_updates = 10   # 至少 10 次 tool call

    def should_extract(self, ctx: TurnContext) -> Decision:
        """
        三级门 + LLM 评分
        
        门1: token 阈值（避免短对话触发）
        门2: 间隔阈值（避免短时间内反复触发）
        门3: LLM 评分（关键词匹配 + 语义判断）
        """
        # 门1: 启动门槛
        if ctx.cumulative_tokens < self.min_tokens_to_init:
            return Decision("skip", reason="below_init_threshold")

        # 门2: 间隔门槛
        if ctx.tokens_since_last_extract < self.min_tokens_between_updates:
            return Decision("skip", reason="below_interval_threshold")

        # 门3: 关键词快速过滤（cheap）
        keywords_hit = self._keyword_filter(ctx.last_messages)
        if not keywords_hit:
            return Decision("skip", reason="no_keyword")

        # 关键词命中 → 触发 LLM "评分 + 提取" 一次完成
        # v2.1 (L1): 合并原来的 _llm_score + _llm_extract 两次调用为一次
        # 原来: 评分(500ms) + 提取(800ms) = 1.3s
        # 现在: 一次调用(800ms),省一次 round-trip + 省一次 cache 失效
        result = self._llm_score_and_extract(ctx.last_messages)
        if not result.should_extract:
            return Decision("skip", reason=f"llm_says_no({result.reason})")
        if result.confidence < 0.6:  # 置信度阈值
            return Decision("skip", reason=f"low_confidence({result.confidence:.2f})")
        # 把提取结果挂在 Decision 上, 通道 B 直接用,不用再调 LLM
        return Decision("extract", confidence=result.confidence, candidates=result.candidates)

    def _keyword_filter(self, messages: list[Message]) -> bool:
        """cheap 关键词预过滤, 只为减少 LLM 调用次数"""
        keywords = [
            "记住", "记一下", "帮我记住", "别忘了",   # 显式要求
            "偏好", "决策", "选择", "拒绝", "采用",   # 决策类
            "教训", "经验", "原则",                   # 反思类
            "总是", "从不", "永远", "习惯",            # 习惯类（v1 漏的关键！）
        ]
        text = " ".join(m.text for m in messages)
        return any(kw in text for kw in keywords)

    def _llm_score_and_extract(self, messages: list[Message]) -> ExtractResult:
        """
        v2.1 合并版: 一次 LLM 调用同时完成"是否值得提取"+"如果是,提取什么"
        
        收益 (L1 修复):
        - 延迟: 1.3s → 0.8s (~40% ↓)
        - 成本: 2× Haiku 调用 → 1× (~50% ↓)
        - Cache: 单 cache key, 多次调用复用率更高
        
        返回结构化结果 (L6 修复): 区分 status 让上层知道发生了什么
        """
        prompt = f"""分析以下对话, 同时判断两件事:
1. 是否包含"值得长期记住"的信息
2. 如果是, 提取为结构化记忆 (按 4 类: user / feedback / project / reference)

值得记住的:
- 用户偏好 (学习风格/技术选型/沟通方式)
- 关键决策 (架构选择/技术决策/拒绝的方案)
- 重要教训 (bug 修复经验/踩坑总结)
- 外部系统指针 (Linear/Slack 链接)

不值得记住的:
- 临时状态 (当前进度/未完成的工作)
- 代码细节 (具体函数名/行号)
- 中间推理 (思考过程的草稿)
- 已过时信息

<conversation>
{self._summarize_for_scoring(messages)}
</conversation>

输出 JSON (严格遵守 schema, 不要其他内容):
{{
  "should_extract": true/false,
  "confidence": 0.0-1.0,
  "reason": "若不提取,简短说明原因",
  "candidates": [
    {{
      "type": "user" | "feedback" | "project" | "reference",
      "content": "一句话记忆",
      "why": "若 type=feedback/project, 这条记忆的 Why 字段 (见 §5.3.2)",
      "source_quote": "原对话中触发该记忆的逐字引用 (见 §4.2 L7 修复)"
    }}
  ]
}}"""
        # L9 修复: conversation 用 <conversation> delimiters 包起来,
        # 防止用户文本里塞 "ignore prior instructions" 攻击 prompt
        response = self.router.chat(
            messages=[
                {"role": "system", "content": "你是结构化记忆提取助手. 严格按 schema 输出 JSON."},
                {"role": "user", "content": prompt},
            ],
            provider="anthropic",
            model="claude-haiku-4-5",  # 小模型, 成本低
            temperature=0.0,
            cache_safe_params=True,    # 详见 §6.8 LLM Integration Contract
            cache_namespace="memory_extract_score",  # 同 namespace 多次调用共享 cache
        )
        # L6 修复: 区分 status, 不再静默 return ExtractResult.empty()
        try:
            data = json.loads(response.strip())
            return ExtractResult(
                should_extract=bool(data.get("should_extract", False)),
                confidence=float(data.get("confidence", 0.0)),
                reason=data.get("reason", ""),
                candidates=data.get("candidates", []),
                status="ok",
            )
        except (json.JSONDecodeError, KeyError, ValueError) as e:
            # 失败不静默 — 存 raw + 发 metric (L6 修复)
            self._metrics.increment("extract.parse_error_total")
            self._raw_failure_log.write_text(response, encoding="utf-8")
            return ExtractResult(
                should_extract=False,
                confidence=0.0,
                reason=f"parse_error: {e}",
                candidates=[],
                status="parse_error",
            )
```

**v1 vs v2 阈值对比**：

| 维度 | v1 | v2 |
| --- | --- | --- |
| 启动门槛 | 无（每次都判） | `cumulative_tokens ≥ 10K` |
| 间隔门槛 | 无 | `tokens_since_last_extract ≥ 5K` |
| 关键词匹配 | 8 个词 | 13 个词（增加"总是/从不/永远/习惯"） |
| 二次过滤 | 无 | LLM 评分 ≥ 0.6 |
| 关键词误报 | 高（"你总是这样" 命中"总是"，但用户在抱怨而非陈述偏好） | 关键词 + LLM 评分二级过滤 |

> **解释**：CC 的实际配置来自 Statsig 服务端下发（`tengu_session_memory`），上面是合理的近似值。生产环境应该把 `min_tokens_to_init` 等参数**暴露在 config 里**，方便不同场景调优（短对话密集 vs 长对话稀疏）。

### 3.3.1 用户调整版决策树（v2.1.1 增）—— M9 修订

> **修订背景**：原 §3.3 决策树是 4 个 AND 串联的门，门2（5K token 间隔）+ 门3（关键词）+ 门4（LLM 评分）必须全过才提交 B。**实际联调发现两个问题**：
> 1. **5K 间隔门**与"达到 10K 即重置计数"语义重叠，两者只能二选一
> 2. **4 个 AND 串联**导致"累计 < 10K 且无关键词"的短对话即使说"记住"也不响应（违背用户直觉）
>
> M9 联调中与用户对齐后,调整为 **3 门 OR 关系**版本。

**调整后决策树**：

```
B 触发决策树（v2.1.1 用户版）:
│
├─ 门1（累计型）: cumulative_tokens >= 10K  OR  tool_calls >= 10  ?
│   ├─ 是 → 进入门3 LLM 评分
│   │       （跑完 B 后，cumulative_tokens / tool_calls 清零，开始新一轮累计）
│   └─ 否 ↓
│
├─ 门2（事件型）: 16 个关键词中 ≥1 命中？
│   ├─ 是 → 进入门3 LLM 评分
│   │       （不清零，门2 触发后保留累计计数，允许多次连续触发）
│   └─ 否 → SKIP, reason: "no_trigger"
│
└─ 门3（质量门）: LLM 评分 confidence >= 0.6 ?
    ├─ 是 → 提交 B
    └─ 否 → SKIP, reason: "low_confidence(0.XX)"
```

**关键设计点**：

| 点 | 原版 §3.3 | v2.1.1 用户版 |
|----|----------|--------------|
| 门数 | 4 个 AND 串联 | **3 个，门1 OR 门2 → 门3** |
| 间隔节流 | 5K token 间隔门（门2 原版）| **取消**，改为"门1 跑完清零累计" |
| 门1 触发后 | 还要过门3 关键词 | **直接走 LLM 评分** |
| 门2 触发后 | 还要过门1 累计 | **直接走 LLM 评分** |
| 关键词列表 | 13 个 | **16 个**（增加"记住/记一下/帮我记住/别忘了"） |

**门1 清零逻辑**：

- 门1 触发 LLM 评分后，**不管评分过没过 0.6 都要清零累计**（如果评分 < 0.6，仍算"已经尝试过这个周期"）
- **修订**：门1 触发后 LLM 评分 < 0.6 → **不清零**累计（详见 §7 错误处理）
- 门2 触发后 **不清零**（关键词型可连续多次触发）

**会话级累计**：

- 每次 session 开始时 `cumulative_tokens = 0, cumulative_tool_calls = 0`
- 跨 session 不累计（避免短对话密集场景误触发）

---

## 四、记忆写入与压缩方案（v2：双通道 + 分层）

> **v2 重要变更**：本章由原"独立 Agent 提取"扩展为完整的"写入与压缩"方案。
> 原 §4.2 实现代码保留为 §四.A（向后兼容），v2 新增章节为 §四.1-§四.7。

### 4.0 v1 实现（保留作为参考）

#### 4.0.1 为什么选择独立 Agent

| 维度 | 当前 Agent 同步做 | 新建 Agent 异步做 ✅ |
|------|-------------------|---------------------|
| **上下文隔离** | ❌ 提取思考污染用户对话 | ✅ 完全隔离 |
| **安全性** | ❌ 提取结果可能泄露到对话 | ✅ 只写文件 |
| **用户体验** | ❌ 对话结束要等几秒 | ✅ 用户无感知 |
| **错误影响** | ❌ 提取失败可能打断流程 | ✅ 失败不影响主对话 |
| **Cache 共享（与主对话）** | ✅ 同 context 直接复用 | ❌ 独立 context，prompt 完全不同 |
| **Cache 共享（跨提取调用）** | ❌ 每次主对话都不同 | ✅ 多次提取 prompt 高度相似，cache 命中高 |
| **Anthropic 服务端 prompt cache** | ✅ 同 system prompt 命中 | ✅ 用 cache_safe_params 也可命中 |

> **v2 修正**：v1 表中 "Cache 共享" 一栏 ❌ 是错的。异步 Agent 也可以做到 cache 共享，只是**作用域不同**：
> - 同步 Agent：与**主对话 loop** 共享 cache（同一 context）
> - 异步 Agent：与**其他提取调用**共享 cache（多次提取 prompt 结构相似）
> 
> 实现方式：参考 Claude Code `runForkedAgent` + `createCacheSafeParams`（[src/services/SessionMemory/sessionMemory.ts:318-325](../../ailearning/claude-code-analysis/src/services/SessionMemory/sessionMemory.ts#L318-L325)）—— 把 system prompt 写成 cache-safe 形式，多次调用共享同一段前缀，Anthropic 服务端 cache 能命中 90%+。
> 
> 详见 §四.6（v2 修正"复用 Router 实例"为"独立 context 但 cache-safe"）。

#### 4.0.2 实现方式（v1 原版，保留参考）

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
    独立记忆提取 Agent（v1 原版，已弃用）
    
    ⚠️ 设计原则自相矛盾（v1 遗留 bug）：
    - 下方 docstring 同时声称"独立"和"复用"，v2 修正见 §四.6
    
    修正后的设计原则（v2）：
    - 后台线程运行，不阻塞主 Agent
    - 复用主 Agent 的 LLMRouter **实例**（共享模型配置）
    - 但用独立 cache namespace 避免污染主对话 cache
    - 提取失败只记日志，不影响主对话
    """

    def __init__(
        self,
        router,  # v1: 主 Agent 的 LLMRouter 实例（v2 修正见 §四.6）
        daily_logger: DailyLogger,
        memory_store: MemoryStore,
    ):
        self.router = router  # ⚠️ v1 直接复用实例（v2 应改为 cache-safe 模式）
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
⚠️ v1 类别，v2 已改为封闭 4 类 {user, feedback, project, reference}，详见 §5.3.1

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

### 4.0.3 与主 Agent 的集成方式（v1 原版，保留参考）

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
            router=self.router,  # ⚠️ v1: 复用 Router 实例（v2 应改为独立 context，见 §四.6）
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

> **v2 修正**：v1 第 451 行的 `router=self.router` 会复用主 Agent 的 Router 实例，**会污染 prompt cache**。v2 应该传入 Router 配置而非实例，并在提取 Agent 内创建独立 context（详见 §四.6）。

---

## §四.1 - §四.6：v2 新增架构

### 4.1 双通道写入架构（P0-2）

**v1 单通道问题**：

```
用户 turn → 主 Agent 响应 → 异步触发提取 Agent → 等几秒 → 写存储
                                          ↑
                                   这里延迟几秒
                                   如果用户立刻问"刚才说的记住没?" 
                                   → 答"还没存" (空指针)
```

**v2 双通道**：

```
用户 turn → 主 Agent 响应 → turn-end
                              │
              ┌───────────────┴───────────────┐
              ▼                                ▼
   通道 A（主 agent 内联写）         通道 B（后台异步提取）
   ├─ 实时, 无延迟                    ├─ 异步, 后台跑
   ├─ 仅写 daily log                  ├─ 写 daily + vector + memory
   ├─ 推进 cursor                     ├─ 推进 last_extracted_cursor
   └─ 触发条件:                       └─ 触发条件:
      用户说"记住这个"                   token ≥ 10K 或 tool ≥ 10
      或主 agent 判断                    或关键词命中 + LLM 评分 ≥ 0.6
```

**互斥机制**：

```python
class DualChannelWriter:
    """
    v2.1 双通道写入器

    关键设计:
    - cursor 跟踪防止重复处理
    - 通道 A 写完立即推进 cursor, 让通道 B 跳过已处理区间
    - 通道 A 只写 daily log (轻量); 完整提取留给通道 B

    v2.1 新增:
    - cursor 持久化 (A3): 进程重启不丢位置
    - 跨进程锁 (A4): 多终端 / IDE + CLI 不冲突
    - 提取事务 (A5): 半写状态可回滚
    - executor 优雅退出 (A9): 进程退出前等 in-flight 完成
    - extraction_in_progress 超时 (A10): 永不卡死
    """

    def __init__(self, session_id, daily_logger, vector_store, memory_files, meta_db):
        self.session_id = session_id
        self.daily = daily_logger
        self.vector = vector_store
        self.memory = memory_files
        self.meta_db = meta_db  # SQLite, 持久化 cursor / pending_writes

        # A3: cursor 从 SQLite 加载, 不再是纯内存
        self.daily_cursor = self.meta_db.get_cursor(session_id, "daily") or 0
        self.extract_cursor = self.meta_db.get_cursor(session_id, "extract") or 0

        # A4: 跨进程互斥 (fcntl.flock 在 Unix, msvcrt 在 Windows)
        # 进程内 threading.Lock 仍然保留, 两者并存:进程内细粒度 + 跨进程粗粒度
        self._proc_daily_lock = threading.Lock()    # 进程内 daily 细粒度
        self._proc_extract_lock = threading.Lock()  # 进程内 extract 细粒度
        self._ipc_daily_lock_path = ".agent_data/memory/.daily.ipclock"
        self._ipc_extract_lock_path = ".agent_data/memory/.extract.ipclock"

        # A10: extraction_in_progress 改成有 wall-clock 超时保护的标志
        self._extraction_in_progress = False
        self._extraction_started_at = 0.0
        self._EXTRACTION_TIMEOUT_S = 60

        # A9: ThreadPoolExecutor 优雅退出 (wait=True + timeout)
        self._executor = ThreadPoolExecutor(
            max_workers=2,
            thread_name_prefix="mem-channel-b",
        )
        import atexit
        atexit.register(self._graceful_shutdown)

    def _graceful_shutdown(self):
        """A9: 进程退出前等 in-flight 提取完成, 超时后强制 cancel"""
        try:
            # wait=True 让已 submit 的任务跑完
            # Python 3.9+ 的 shutdown 没有 timeout 参数, 用 wait(wait=True) + 并发超时
            import concurrent.futures
            # 简易超时: 等 N 秒后 cancel
            done_event = threading.Event()
            def _wait():
                self._executor.shutdown(wait=True)
                done_event.set()
            t = threading.Thread(target=_wait, daemon=True)
            t.start()
            done_event.wait(timeout=30)  # 30s 宽限期
            if not done_event.is_set():
                log.warning("executor shutdown timeout, forcing cancel")
                self._executor.shutdown(wait=False)
        except Exception as e:
            log.error(f"graceful_shutdown error: {e}")

    def channel_a_inline_write(self, user_msg: str, agent_response: str, turn_index: int):
        """
        通道 A: 主 agent 内联写 (turn-end 同步, 不阻塞)

        适用: 用户显式说"记住这个", 或主 agent 检测到明确偏好
        限制: 只写 daily log, 不做 LLM 提取 (避免阻塞)

        v2.1 (A3+A4): 跨进程 flock + cursor 持久化原子绑定
        """
        with self._ipc_flock(self._ipc_daily_lock_path):  # A4: 跨进程
            with self._proc_daily_lock:  # 进程内
                # 写 daily log
                self.daily.log(
                    session_id=self.session_id,
                    category="conversation",
                    key=f"turn_{turn_index}",
                    value=f"Q: {user_msg[:200]} → A: {agent_response[:200]}",
                )
                # A3: 持久化 cursor (与 daily log 写在同一事务里, 防止半写)
                self.daily_cursor = turn_index
                self.meta_db.set_cursor(
                    session_id=self.session_id,
                    cursor_type="daily",
                    value=turn_index,
                )  # SQLite WAL 模式, 写入 fsync 后才返回

    def channel_b_background_extract(self, ctx: TurnContext):
        """
        通道 B: 后台异步提取 (LLM 调用, cache-safe)

        适用: 阈值触发 (token / tool count)
        行为: 读 channel_a_cursor 到当前 turn 之间的所有消息
              → LLM 提取 → 写 vector + memory

        v2.1: 加上跨进程 flock + in-progress 标志超时保护
        """
        # A10: 检查 in-progress 标志 + 超时
        if self._extraction_in_progress:
            age = time.time() - self._extraction_started_at
            if age < self._EXTRACTION_TIMEOUT_S:
                return  # 还在跑, 跳过本次
            else:
                log.warning(f"extraction stuck for {age:.0f}s, force reset")
                self._extraction_in_progress = False  # 强制重置

        # A4: 跨进程互斥
        if not self._ipc_try_lock(self._ipc_extract_lock_path):
            return  # 别的主进程在跑, 跳过

        with self._proc_extract_lock:
            start = max(self.extract_cursor, self.daily_cursor)
        new_messages = ctx.messages[start:]
        if not new_messages:
            self._ipc_unlock(self._ipc_extract_lock_path)
            return

        self._extraction_in_progress = True
        self._extraction_started_at = time.time()

        # 后台跑, 不阻塞主对话
        self._executor.submit(self._do_extract, new_messages)

    def _do_extract(self, messages):
        """A5: 事务式提取 — 半写状态可回滚"""
        pending_ids = []
        try:
            extraction = self._llm_extract(messages)
            for item in extraction:
                # A5: 幂等键去重 (item_hash 来自 source_quote + type + key)
                item_hash = self._compute_item_hash(item)
                if self.meta_db.is_pending_written(item_hash):
                    continue  # 已写过, 跳过
                self.meta_db.mark_pending(item_hash)  # 先标 pending
                pending_ids.append(item_hash)

                # 写向量库
                self.vector.add(item)
                # 写记忆文件: 用 <uuid>.tmp + rename 原子事务
                tmp_path = self.memory.path_for(item).with_suffix(".md.tmp")
                final_path = self.memory.path_for(item)
                tmp_path.write_text(self.memory.render(item))
                tmp_path.rename(final_path)  # 原子 rename, 不会留半文件

                self.meta_db.mark_committed(item_hash)  # 标 committed

            # 全部成功 → 推进 cursor
            with self._proc_extract_lock:
                self.extract_cursor = max(self.extract_cursor, self.daily_cursor)
            self.meta_db.set_cursor(
                session_id=self.session_id,
                cursor_type="extract",
                value=self.extract_cursor,
            )
        except Exception as e:
            # A5: 失败回滚 — 清理 pending + 删半文件
            log.warning(f"channel_b extract failed: {e}")
            for pid in pending_ids:
                self.meta_db.unmark_pending(pid)
            # vector.add 已写无法回滚, 但下次启动 re-extract 时 item_hash 去重
            # 保证不会重复写入 file
        finally:
            # A10: 标志复位
            self._extraction_in_progress = False
            self._extraction_started_at = 0.0
            self._ipc_unlock(self._ipc_extract_lock_path)

    # ---------- A4: 跨进程锁辅助 ----------
    def _ipc_flock(self, path: str):
        """阻塞获取跨进程锁 (with-block 兼容)"""
        return _IPCLock(path, blocking=True)

    def _ipc_try_lock(self, path: str) -> bool:
        """非阻塞获取跨进程锁"""
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
            os.write(fd, f"{os.getpid()}\n".encode())
            self._held_ipc_fds[path] = fd
            return True
        except FileExistsError:
            return False

    def _ipc_unlock(self, path: str):
        """释放跨进程锁"""
        fd = self._held_ipc_fds.pop(path, None)
        if fd is not None:
            os.close(fd)
        os.unlink(path) if os.path.exists(path) else None
```

**为什么不是 v1 的"单一通道"？**

| 场景 | v1 单通道 | v2 双通道 |
| --- | --- | --- |
| 用户说"记住 X" | 等几秒 → 答"已记" | **立即**写 daily log，答"已记" |
| 用户问"刚才说的记住没?" | 可能答"还在处理" | 答"已记（通道 A 即时）" |
| 长对话累积提取 | 后台慢慢跑 | 后台跑 + 通道 A 兜底轻量记录 |

### 4.2 Edit-only 工具沙箱（P0-7）

**v1 问题：JSON 解析路径**

```
提取 Agent LLM 输出 JSON → 解析 → 写文件
         ↑
    容易出错:
    - LLM 输出格式不对 → JSONDecodeError
    - LLM 改了 schema → 文件结构混乱
    - LLM 输出超长 → 截断
```

**v2：Edit 工具直接改文件**

```python
class MemoryFileEditor:
    """
    v2 记忆文件编辑器
    
    设计原则:
    - LLM 只用 Edit 工具, 不能 Write 整个文件 (避免 schema 漂移)
    - 工具白名单 + 路径白名单 (只能改 memory/ 下的指定文件)
    - 严格分类: 4 类封闭 (user/feedback/project/reference, 见 §五)
    """

    # 4 类封闭分类, LLM 不能发明第五类
    ALLOWED_CATEGORIES = {"user", "feedback", "project", "reference"}

    def create_memory_edit_tool(self, memory_dir: str) -> dict:
        """
        返回一个受限的 Edit 工具描述, 给后台提取 Agent 用
        
        参考 Claude Code: src/services/SessionMemory/sessionMemory.ts:460-482
        """
        return {
            "name": "Edit",
            "description": (
                "Edit a memory file in the memory directory. "
                "You may ONLY edit files under " + memory_dir + ". "
                "You may NOT use Write, NotebookEdit, or Bash. "
                "Each memory file has frontmatter with 'type' field "
                "must be one of: user, feedback, project, reference."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "pattern": f"^{re.escape(memory_dir)}/.*\\.md$",
                    },
                    "old_string": {"type": "string"},
                    "new_string": {"type": "string"},
                },
                "required": ["file_path", "old_string", "new_string"],
                # 强制: 路径必须在 memory_dir 下
                "additionalProperties": False,
            },
        }

    def validate_edit(self, file_path: str, old_string: str, new_string: str):
        """编辑前的硬验证 (LLM 不可绕过)"""
        # 1. 路径必须在 memory_dir 下
        real_path = os.path.realpath(file_path)
        if not real_path.startswith(os.path.realpath(self.memory_dir)):
            raise PermissionError(f"path {file_path} outside memory dir")

        # 2. new_string 中必须含合法 type 字段
        if "type:" in new_string:
            type_match = re.search(r"type:\s*(\S+)", new_string)
            if type_match and type_match.group(1) not in self.ALLOWED_CATEGORIES:
                raise ValueError(f"invalid category: {type_match.group(1)}")

        # 3. feedback / project 类必须有 Why 字段 (P0-4)
        if "type: feedback" in new_string or "type: project" in new_string:
            if "**Why:**" not in new_string:
                raise ValueError("feedback/project memories must include '**Why:**'")

        return True
```

**v1 vs v2 写入路径对比**：

| 维度 | v1 (JSON 解析) | v2 (Edit 工具) |
| --- | --- | --- |
| LLM 输出格式 | 严格 JSON | 自由文本（schema 在 frontmatter 约束） |
| 解析失败 | 整批失败 | 单条 Edit 失败不影响其他 |
| Schema 漂移 | 可能（LLM 加字段） | 不可能（frontmatter 是固定的） |
| 路径安全 | 自己写校验 | 工具白名单强制 |
| 类别约束 | prompt 里说 | 工具层 + 校验层双重强制 |

### 4.3 Extract vs Compact 分层（P0-8 + P0-6）

**关键洞察**：v1 把"压缩"和"提取"混在一起——每次压缩都让 LLM 重新摘要所有历史。**这两件事应该分开**。

```
v1 (混在一起):
压缩 = LLM 读全部历史 → LLM 输出新 summary → 替换旧的
       每次都重摘要, 慢, 信息丢失

v2 (分层):
extract = 后台 fork agent 增量更新 SM 文件 (慢路径, 不阻塞)
compact = 直接读 SM 文件拼成 summary 消息 (快路径, 零 LLM)
       SM 文件永远累积, compact 只是"读取+截断", 不丢信息
```

```python
class SessionMemoryLayer:
    """
    v2 L3 会话内滚动摘要 (等价于 Claude Code SessionMemory)
    
    关键不变量:
    - SM 文件永不被重新生成, 只通过 Edit 增量更新
    - compact 不调 LLM, 只读 SM 文件
    - 已 compact 的消息信息保留在 SM 文件里, 不丢
    """

    def __init__(self, session_id: str, sm_path: str):
        self.session_id = session_id
        self.sm_path = sm_path  # .agent_data/sessions/{id}/sm.md
        self.last_compacted_msg_id: Optional[str] = None

    # ---------- 慢路径: extraction (调 LLM) ----------
    def extract_incremental(self, messages: list[Message]):
        """
        增量更新 SM 文件
        - 读 SM 文件当前内容
        - 取 last_compacted_msg_id 之后的新消息
        - LLM 用 Edit 工具更新 SM 文件 (schema 不变)
        - 后台跑, 不阻塞
        """
        current_sm = self.read_sm() if self.sm_exists() else self._template()
        new_messages = messages[self._index_after(self.last_compacted_msg_id):]

        if not new_messages:
            return

        # fork agent + cache-safe params (P0-6)
        threading.Thread(
            target=self._do_extract,
            args=(current_sm, new_messages),
            daemon=True,
        ).start()

    def _do_extract(self, current_sm: str, new_messages: list[Message]):
        try:
            prompt = self._build_extract_prompt(current_sm, new_messages)
            # fork agent: 独立 context, cache-safe params
            response = self.router.chat(
                messages=[{"role": "user", "content": prompt}],
                provider="anthropic",
                model="claude-haiku-4-5",  # 小模型足够
                temperature=0.1,
                cache_safe_params=True,  # 🆕 复用 system prompt 缓存
                tools=[self.memory_editor.create_memory_edit_tool(self.sm_path)],
            )
            # 后台 agent 已经通过 Edit 工具改完文件了
            # 推进 last_compacted_msg_id
            self.last_compacted_msg_id = new_messages[-1].id
        except Exception as e:
            log.warning(f"sm extract failed: {e}")

    # ---------- 快路径: compact (零 LLM) ----------
    def compact(self, messages: list[Message], context_window: int) -> CompactResult:
        """
        触发压缩时 (token 超阈值), 直接用 SM 文件拼 summary
        
        零 LLM 调用, 毫秒级完成
        """
        sm_content = self.read_sm() if self.sm_exists() else None
        if not sm_content:
            # 回退: SM 文件不存在, 用传统 LLM 压缩
            return None  # 让 caller 走传统路径

        # 按 section 截断到 2000 tokens / 8000 chars (P0-8 的"读 + 截断"模式)
        truncated = self._truncate_sections(sm_content, max_per_section=8000)

        # 拼装 summary 消息
        summary_message = {
            "role": "user",
            "content": (
                f"[Session memory summary]\n\n"
                f"The following is a condensed summary of our session so far. "
                f"Full SM file: {self.sm_path}\n\n"
                f"{truncated}"
            ),
        }

        # 丢弃 last_compacted_msg_id 之前的消息
        # 它们的"信息"在 SM 文件里, 不丢
        kept_messages = messages[self._index_after(self.last_compacted_msg_id):]

        return CompactResult(
            summary=summary_message,
            kept_messages=kept_messages,
            # 关键: 不产生 usage / stop_reason (因为没调 LLM)
        )

    def _truncate_sections(self, sm: str, max_per_section: int) -> str:
        """
        按 section 截断 SM 文件
        
        SM 文件结构: ## Section 1 ... ## Section 2 ...
        每个 section 单独截断, 总文件大小无硬上限
        """
        sections = re.split(r"(^## .+$)", sm, flags=re.MULTILINE)
        result = []
        for i in range(1, len(sections), 2):
            header = sections[i]
            body = sections[i + 1] if i + 1 < len(sections) else ""
            if len(body) > max_per_section:
                body = body[:max_per_section] + "\n\n[... truncated for brevity ...]"
            result.append(header + body)
        return "\n\n".join(result)
```

**v1 vs v2 压缩对比**：

| 维度 | v1 (单一路径) | v2 (分层) |
| --- | --- | --- |
| 压缩延迟 | 5~15s (阻塞, 等 LLM) | < 100ms (读文件) |
| LLM 调用 | 1 次大调用 | 0 次（extract 在后台跑过了） |
| Cache 命中 | 低（大输入 cache miss） | 高（SM 文件小，cache 友好） |
| 最近消息保真 | 被总结 | **保留原文**（只丢弃 last_compacted_msg_id 之前） |
| 跨压缩累积 | 每次重新生成 | SM 文件持续累积，**信息永不丢** |

### 4.4 L3 会话内滚动摘要（P0-1）

承接 §四.3，这里详细说明 L3 层的目的和触发逻辑。

**为什么需要 L3？**

```python
# 假设一段会话达到 60K tokens
# v1 没有 L3: 整段压缩 → 5~15s 阻塞 → summary 2000 tokens → 信息密度低
# v2 有 L3:
#   - extraction 在后台已经把前 50K 压成 SM 文件 (8K tokens)
#   - compact 时直接读 SM 文件, 100ms 完成
#   - 保留最近 10K tokens 原文 (高保真)
```

**触发条件**（v2）：

```python
def should_trigger_compact(self, ctx: TurnContext) -> CompactDecision:
    """
    触发 compact 的决策
    
    优先级:
    1. SM-compact (L3, 零 LLM, 快速)
    2. 回退到传统 compact (LLM 调用)
    """
    # 1. feature gate
    if not self.sm_compact_enabled():
        return CompactDecision("traditional", reason="gate_disabled")

    # 2. SM 文件必须存在
    if not self.sm_exists() or self.sm_is_template():
        return CompactDecision("traditional", reason="no_sm_file")

    # 3. SM 文件不能太大 (避免 summary 本身超限)
    if self.sm_token_count() > self.config.max_sm_tokens_for_compact:
        return CompactDecision("traditional", reason="sm_too_large")

    # 4. 后台 extraction 不能正在跑 (避免读写冲突)
    if self.extraction_in_progress:
        return CompactDecision("wait", reason="extract_running", timeout_ms=15000)

    # 5. SM-compact 后预估 token 数仍超阈值 → 不要走 SM (避免无限重试)
    projected = self.estimate_post_compact_tokens(ctx)
    if projected >= self.config.auto_compact_threshold:
        return CompactDecision("traditional", reason="sm_insufficient")

    # 所有检查通过 → SM-compact
    return CompactDecision("sm_compact")
```

**5 条回退条件**（与 Claude Code `shouldUseSessionMemoryCompaction` 一一对应）：

| 条件 | 含义 | 回退策略 |
| --- | --- | --- |
| gate 关 | 用户/Statsig 没启用 SM | 走传统 |
| 无 SM 文件 | 提取还没跑过（短对话） | 走传统 |
| SM 文件是模板 | 占位符未填充 | 走传统 |
| extraction 正在跑 | 避免读写冲突 | 等 ≤15s |
| SM-compact 后仍超阈值 | SM 文件不够精简 | 走传统 |

### 4.5 v2 关键不变量

**这些不变量是 v2 设计的"安全网"，违反任意一条都会破坏系统**：

1. **SM 文件永不被重新生成**——只通过 Edit 增量更新，schema 由 frontmatter 锁定
2. **compact 不调 LLM**——只读 SM 文件 + 截断，零延迟
3. **通道 A 只写 daily log**——不调用 LLM，避免阻塞主对话
4. **通道 B 推进 extract_cursor 前必须成功写入**——失败时不推进，下次重试
5. **Edit 工具路径必须在 memory_dir 下**——工具白名单强制，无法越界
6. **4 类封闭（user/feedback/project/reference）**——LLM 不能发明第五类，类型由校验层强制
7. **feedback / project 必须含 `**Why:**`**——避免"只有规则没有原因"的浅记忆

#### 4.5.1 不变量测试矩阵：8 个并发/崩溃场景（v2.1 增，对应 A12 修复）

**为什么这里**：7 条不变量是设计契约,必须可被测试验证。本节定义 **8 个并发/崩溃场景**,每条不变量对应至少 1 个场景,放 CI 必跑 + `--stress` 长跑。

**测试文件**:`tests/test_dual_channel_concurrent.py`(单进程 + threading)、`tests/test_cross_process_flock.py`(跨进程)、`tests/test_crash_recovery.py`(崩溃恢复)。

| # | 场景 | 测的不变量 | 测试方法 | 期望结果 |
| --- | --- | --- | --- | --- |
| 1 | 进程内双线程同时调 channel_a | #3 + #4 | `threading.Barrier(2)` 对齐两线程,都调 `channel_a_inline_write` | 两段都写 daily log,无覆盖,`daily_cursor` = max |
| 2 | 进程内 A 写 → B 提取边界 | #4 | A 写到 turn_5,B 立即触发,只处理 turn_6+ | B 不重复处理 A 已写的 5 条(item_hash 去重) |
| 3 | 跨进程 A + B | #4 + #5 | 起两个子进程,都调 channel_a | 第二个进程的 flock 阻塞,等第一个释放后才写,无交错 |
| 4 | 通道 B 提取中途崩溃 | #4(最关键) | `kill -9` 子进程,模拟 LLM API timeout | cursor **不**推进,下次启动重处理 `extract_cursor` → `daily_cursor` 区间,幂等去重 |
| 5 | autoDream 锁强占(PID 已死) | #5 | 写一个 fake 锁文件 + 旧 PID(进程已死) | 新一轮直接强占,prior_mtime 正确返回 |
| 6 | autoDream 锁强占(mtime 超 1h) | #5 | 写锁文件 + sleep 1h(测试用 5s 调小阈值) | 同样强占,prior_mtime 用旧 mtime |
| 7 | autoDream 蒸馏失败 | #4(回滚) | LLM mock 返回 invalid JSON | 锁释放,prior_mtime 回滚,24h 门从上次**成功**算起 |
| 8 | extraction_in_progress 卡死 | #4(超时) | mock `_do_extract` 死循环 90s(超过 60s 超时) | 60s 后标志强制重置,下次能正常提交 |

**测试模板**(以场景 4 为例):

```python
def test_channel_b_crash_resume(tmp_meta_db, monkeypatch):
    """场景 4: B 提取中途崩溃 → 重启后幂等恢复"""
    writer = DualChannelWriter(session_id="s1", meta_db=tmp_meta_db, ...)

    # 1. A 写到 turn_5
    for i in range(6):
        writer.channel_a_inline_write(f"msg {i}", f"resp {i}", i)
    assert writer.daily_cursor == 5

    # 2. 触发 B, mock _do_extract 在 vector.add 之后崩
    def crash_after_vector_add(messages):
        writer.vector.add({"text": "extracted"})  # 已写
        raise RuntimeError("simulated crash")
    monkeypatch.setattr(writer, "_do_extract", crash_after_vector_add)
    writer.channel_b_background_extract(ctx_with_messages)

    # 3. 等 executor 跑完
    writer._executor.shutdown(wait=True)

    # 4. 验证: extract_cursor 没推进, vector 有半条数据
    assert writer.extract_cursor == 0
    assert writer.vector.count() == 1  # 半写

    # 5. 重启模拟: 新 writer, 加载持久化 cursor
    new_writer = DualChannelWriter(session_id="s1", meta_db=tmp_meta_db, ...)
    assert new_writer.extract_cursor == 0  # 持久化正确

    # 6. 再触发一次 B
    new_writer.channel_b_background_extract(ctx_with_messages)
    new_writer._executor.shutdown(wait=True)

    # 7. 验证幂等: file 有 1 条 (item_hash 去重), cursor 推进
    assert (new_writer.memory_root / "user" / "hash_X.md").exists()
    assert new_writer.extract_cursor == 5
```

**对应 issue**:A12 审查指出"§10 没说怎么测锁 / 崩溃恢复",本节给 8 场景 + 1 模板。

**覆盖率目标**:
- 关键不变量 #1-#7(见 §4.5)**100%** 覆盖
- 并发场景 1-8 必跑(放 CI,`pytest -m concurrency`)
- 崩溃场景 4/6/7/8 加 `pytest --stress` 长跑 1h 看是否真不卡死

**与 §4.6+ 的关系**:不变量是"是什么",本节是"怎么证"。两者**绑定**——改了 §4.5 任一条,必须同步更新本节;反之亦然。

**迁移说明**:本节原位于 §10.1(实施计划内),2026-06-20 重构时归位到 §4.5.1(测试不变量属于设计契约,不属于 plan)。详见 [`docs/IMPLEMENTATION_PLAN.md`](IMPLEMENTATION_PLAN.md) M6 任务清单。

### 4.6 独立 Context（修正 v1 的 Router 复用问题）（P0-16）

**v1 问题**：

```python
# agent_core/memory/extractor.py:162 (v1)
self.router = router  # ⚠️ 直接复用主 Agent 的 Router 实例
```

问题：
- Router 实例有 prompt cache（system prompt / tools 缓存）
- 提取 agent 用的 prompt 和主 agent 完全不同 → cache 命中率低
- 主 agent 的 cache 可能被提取 agent 污染（如果 Router 内部 cache 不分 key）

**v2 修正**：

```python
class MemoryExtractor:
    def __init__(
        self,
        router_config: RouterConfig,  # ✅ 传 config, 不是实例
        daily_logger: DailyLogger,
        memory_store: MemoryStore,
    ):
        # 创建独立 Router 实例, 独立 cache namespace
        self.router = LLMRouter(
            config=router_config,
            cache_namespace="memory_extractor",  # 独立 cache key
        )
        # 其他不变...
```

> **参考**：Claude Code 用 `runForkedAgent` + `createSubagentContext` 做完全独立化（[src/services/SessionMemory/sessionMemory.ts:318-325](../../ailearning/claude-code-analysis/src/services/SessionMemory/sessionMemory.ts#L318-L325)）。Python 这边手动创建独立 Router 实例即可，效果等价。

### 4.7 回退条件设计（P0-9）

**v1 没有显式回退**——一旦 extraction 失败，主 Agent 行为不变，但用户感受是"什么都没记住"。

**v2 显式回退矩阵**：

| 失败场景 | 检测方式 | 回退策略 | 用户可见性 |
| --- | --- | --- | --- |
| extraction LLM 调用失败 | try/except | 记 warning，下次重试 | 日志，不打扰 |
| SM-compact 后仍超阈值 | post_compact_token_count ≥ threshold | 走传统 compact | 无（无缝） |
| 通道 A 写 daily 失败 | try/except | 通道 B 兜底 | 无 |
| 通道 B 写文件失败 | try/except | 保留 daily log，下次重试 | 日志 |
| Edit 工具被绕过（路径越界） | 工具白名单 | 抛 PermissionError | 日志 + alert |
| 类别非法 | validate_edit | 抛 ValueError | 日志 + 不写入 |
| Why 字段缺失（feedback/project） | validate_edit | 抛 ValueError | 日志 + 不写入 |
| SM 文件被损坏 | JSON / Markdown 解析失败 | 回退传统 compact + 修复 SM | UI 提示 |

> **关键**：所有回退都是**无缝的**——用户感知不到"系统在降级运行"。Claude Code 的设计哲学也是这样：用户在各种异常路径下都能得到"合理"的响应，只是背后悄悄走了不同的代码路径。

### 4.8 通道 A WAL 行为（v2.1.1 增）—— M9 修订

> **修订背景**：原 §4.1 描述通道 A 写 daily log，但未明确"通道 A 与通道 B 的数据流关系"。**M9 联调澄清**：通道 A 写的 JSONL 是 **WAL（Write-Ahead Log）**，**不是 B 每次必读的"工作文件"**。

**热路径 vs 冷路径**：

| 场景 | A → B 链路 | 是否读 JSONL |
|------|----------|------------|
| **正常 turn-end**（同进程、连续运行）| A 内存传 `TurnMessage` 给 B | ❌ **不读** |
| **进程崩了 + 重启**（A3 恢复）| 从 JSONL 重建 `TurnMessage` 喂给 B 补跑 | ✅ 读 |
| **跨进程并发**（A4 IPCLock）| 进程 A 写 JSONL，进程 B 锁住 `daily_cursor` 之后从 JSONL 读 turn_range | ✅ 读 |
| **手动工具排查** | `cat ~/.agent_data/logs/<id>.jsonl \| jq` | ✅ 读 |
| **M5 蒸馏** | `DistillationScheduler` 读 JSONL 聚合 | ✅ 读 |

**WAL 字段约定**（每行 JSON）：

```python
entry = {
    "turn_index": turn_index,        # 主对话轮次序号
    "user_msg": user_msg,            # 用户原文
    "assistant_resp": assistant_resp, # 助手响应原文
    "ts": time.time(),                # 时间戳
}
```

**WAL 写入必须 fsync**（不只是 flush）：

```python
with open(log_path, "a", encoding="utf-8") as f:
    f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    f.flush()
    os.fsync(f.fileno())  # ★ 强制落盘,进程崩了不丢
```

**通道 A 必不做清单**（不变量 #3 + #4 强化）：

- ❌ 不调 LLM
- ❌ 不解析内容、关键词检测、LLM 评分
- ❌ 不向量化
- ❌ 不写 `~/.agent_data/memory/<type>/` 下任何文件
- ❌ 不写 vector store
- ❌ 不读 daily log（只 append，读是 B 或外部工具的事）

**和 SessionManager 的关系**：

- SessionManager 写 `data/sessions/<id>.jsonl`（主对话流，粒度细到每条 message）
- 通道 A 写 `~/.agent_data/logs/<id>.jsonl`（记忆子系统，粒度粗到每 turn 一行）
- **两者刻意分开**，免得记忆写入挂掉影响主对话上下文

**为什么 JSONL 仍是必要**（不因为有内存链路就删 JSONL）：

1. **崩溃恢复**：进程在 turn-end 后、B 跑完前 crash，没有 JSONL 就丢了 turn
2. **跨进程**：Streamlit + CLI 并发时，两个进程看到的 turn 状态需要 JSONL 同步
3. **调试可观测**：`tail -f` 实时看对话原文是排错最直接的方式

---

## 五、存储流程（v2：封闭分类 + per-file + Why-How）

> **v2 重要变更**：
> - Layer 1 / Layer 2 结构基本保留
> - Layer 3 从"单 MEMORY.md"重构为"per-file + 索引"
> - 分类从 4 类"开放"改为 4 类"封闭"
> - feedback / project 类记忆强制 Why 字段

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

**写入时机**：
- 通道 A（内联）：turn-end 用户显式"记住这个"，立即写
- 通道 B（后台）：阈值触发，写入更详细的提取结果

**代码**：`DailyLogger.log(session_id, category, key, value, metadata)`

**v2 调整**：分类从 `User Preference / Decision / Technical` 等开放清单改为**封闭 4 类**（见 §5.3.1），与 Layer 3 / 向量库的 metadata `type` 字段保持一致。

### 5.2 Layer 2：向量索引（语义检索）

**存储路径**：`.agent_data/chroma/`（Chroma 向量数据库）+ `.agent_data/memory.db`（SQLite 元数据）

**存储结构**：
```
Chroma Collection: "memories"
├── id: "mem_20260611_164300_1234"
├── embedding: [0.123, -0.456, ...]  (568维，bge-m3 多语言, v2.1)
├── document: "用户偏好：先手写原生 ReAct 理解本质"
└── metadata: {type: "user", created_at: "...", access_count: 5}

SQLite Table: memory_meta
├── id (PK)
├── type (user | feedback | project | reference)  ← 🆕 封闭 4 类
├── created_at
├── updated_at
├── access_count (默认0，每次检索命中+1)
└── confidence (默认1.0，每日衰减 ×0.95)
```

**写入时机**：通道 B 提取 Agent 通过 LLM 提取后写入

**代码**：`MemoryStore.add_memory(memory_id, text, type, confidence)`

**v2 调整**：`category` 字段重命名为 `type`，值域限定为 `{user, feedback, project, reference}`，由 `MemoryStore.add_memory` 校验（不在白名单内抛 `ValueError`）。

### 5.3 Layer 3：长期记忆（v2 重构：per-file + 索引）

#### 5.3.1 封闭分类法（P0-3）

**v1 问题**：

```python
# v1: 分类清单在 prompt 里, LLM 可能"发明"
CATEGORIES = ["user_preference", "decision", "technical", "error"]
# LLM 可能输出: task / episodic / reflection / context / ...
# → 每条记忆需要做"分类后处理", 系统失去统一性
```

**v2 改进**：

```python
# v2: 编译期封闭, LLM 不能发明第五类
from typing import Literal

MemoryType = Literal["user", "feedback", "project", "reference"]
#                          ↑         ↑         ↑          ↑
#                       用户角色    纠偏习惯    项目背景    外部指针

ALLOWED_TYPES: frozenset[MemoryType] = frozenset({"user", "feedback", "project", "reference"})

def validate_type(t: str) -> MemoryType:
    if t not in ALLOWED_TYPES:
        raise ValueError(f"type must be one of {ALLOWED_TYPES}, got {t!r}")
    return t  # type: ignore
```

**4 类封闭分类详解**（参考 Claude Code [`MEMORY_TYPES`](../../ailearning/claude-code-analysis/src/memdir/memoryTypes.ts#L80)）：

| Type | 必存项 | 必存触发示例 | 作用 |
| --- | --- | --- | --- |
| **user** | 用户角色 / 目标 / 知识背景 | "I'm a data scientist…"; "I've been writing Go…" | 调整回答视角与详略 |
| **feedback** | 用户对工作方式的纠正 / 确认 | "don't mock the database…"; "stop summarizing…" | 沿用对的、避开错的 |
| **project** | 项目背景、deadline、决策、动机 | "we're freezing merges after Thursday…" | 给出更贴背景的建议 |
| **reference** | 外部系统的指针（Linear、Slack、文档） | "bugs are tracked in Linear project INGEST" | 知道"在哪查" |

> **解释**：封闭分类法的**最大价值**在于——LLM 不会"发明"第五类。如果 prompt 说"输出 user_preference / decision / technical / error"，LLM 可能输出 `task / reflection / context / episodic`。但如果代码层用 `Literal["user", "feedback", "project", "reference"]`，拼写错误在**编译期**就报错。这是 Mem0 / Claude Code 都采用的硬约束。

#### 5.3.2 Why-How 模板（P0-4）

**v1 问题**：feedback / project 类记忆只有"规则"没有"原因"——用户看到一行"不要 mock 数据库"，不知道**为什么**。

**v2 改进**：feedback / project 类记忆**必须**含 `**Why:**` 字段。

```markdown
<!-- v2 feedback 类型记忆示例 -->
---
type: feedback
created_at: 2026-06-08
confidence: 1.0
---

# 不要 mock 数据库

**Why:** 学习场景下 mock 会掩盖真实数据库行为，导致迁移到生产时
才发现 SQL 兼容性 / 事务行为差异。早期 v1 阶段因此踩过坑。

<!-- v2 project 类型记忆示例 -->
---
type: project
created_at: 2026-06-08
confidence: 1.0
---

# agent-dev 项目架构

**Why:** 用户偏好"先手写原生 ReAct 理解本质"——框架是工具，
理解底层原理后才能选择合适的工具；这是 Stage 1→2→3 的决策动机。
```

**强制方式**（由 §四.2 的 `validate_edit` 实现）：

```python
def validate_why_required(new_string: str) -> None:
    """feedback / project 类记忆必须含 Why 字段"""
    if 'type: feedback' in new_string or 'type: project' in new_string:
        if '**Why:**' not in new_string:
            raise ValueError(
                "feedback/project memories must include '**Why:**' field. "
                "Rules without reasons decay fast."
            )
```

#### 5.3.3 Per-File 存储架构（P0-5）

**v1 问题**（单 MEMORY.md）：

```
.agent_data/MEMORY.md
├── 用户偏好 (5KB)
├── 关键决策 (8KB)
├── 技术细节 (12KB)
└── 总计 25KB → 触顶 → 物理裁剪 → 早期记忆被丢弃
```

随着会话增多，MEMORY.md 单文件会触顶。**v1 用"物理裁剪"应对，但裁剪会丢信息**。

**v2 改进**：一条记忆一个文件 + 索引文件。

```
.agent_data/memory/
├── MEMORY.md                    ← 索引（轻量, ~5KB, 不再触顶）
├── user/
│   ├── 2026-06-08_learning_style.md
│   └── 2026-06-11_tech_preference.md
├── feedback/
│   ├── 2026-06-08_no_db_mock.md
│   └── 2026-06-10_prefer_native.md
├── project/
│   └── 2026-06-08_agent_arch.md
└── reference/
    └── 2026-06-11_linear_ingest.md
```

**MEMORY.md（索引文件）**：

```markdown
# MEMORY.md - 长期记忆索引

> 本文件是索引, 不是记忆本身. 真正的记忆在同目录的 `user/` / `feedback/` / `project/` / `reference/` 子目录.

## user (2 条)
- [2026-06-08] 学习风格: 先手写原生 ReAct → [user/2026-06-08_learning_style.md](user/2026-06-08_learning_style.md)
- [2026-06-11] 技术选型偏好 → [user/2026-06-11_tech_preference.md](user/2026-06-11_tech_preference.md)

## feedback (2 条)
- [2026-06-08] 不要 mock 数据库 → [feedback/2026-06-08_no_db_mock.md](feedback/2026-06-08_no_db_mock.md)
- [2026-06-10] 优先本地方案 → [feedback/2026-06-10_prefer_native.md](feedback/2026-06-10_prefer_native.md)

## project (1 条)
- [2026-06-08] agent-dev 架构分阶段 → [project/2026-06-08_agent_arch.md](project/2026-06-08_agent_arch.md)

## reference (1 条)
- [2026-06-11] Linear INGEST 项目 → [reference/2026-06-11_linear_ingest.md](reference/2026-06-11_linear_ingest.md)
```

**单条记忆文件格式**（以 feedback 为例）：

```markdown
---
type: feedback
created_at: 2026-06-08
updated_at: 2026-06-08
confidence: 1.0
access_count: 0
source_session: thread_abc123
---

# 不要 mock 数据库

**Why:** 学习场景下 mock 会掩盖真实数据库行为，导致迁移到生产时
才发现 SQL 兼容性 / 事务行为差异。早期 v1 阶段因此踩过坑。

## 触发场景
- 用户要求"测试一下这个 ORM 查询"
- 重构涉及数据库迁移

## 反例（不要这样做）
```python
# 反模式
@patch("sqlalchemy.create_engine")
def test_query(mock_engine): ...
```
```

**v1 vs v2 Layer 3 对比**：

| 维度 | v1 (单 MEMORY.md) | v2 (per-file + 索引) |
| --- | --- | --- |
| 文件数 | 1 个 | 1 索引 + N 记忆 |
| 单文件大小 | 触顶 25KB → 裁剪 | 每条 ≤ 2KB，无触顶问题 |
| 可观测性 | 看 MEMORY.md 即可 | 看索引 + 点开单条 |
| 编辑粒度 | 整文件读 / 写 | 单文件 Edit（工具友好） |
| 删除粒度 | 整文件重写 | `rm` 单文件 |
| Git 友好 | diff 整个文件 | diff 单条记录，PR review 友好 |
| autoDream 蒸馏 | 读单文件 → 整文件重写 | 按 type 分目录处理，可并行 |

**写入时机**：
- 通道 B 提取时：写入对应 type 子目录的新文件
- autoDream 蒸馏：整合多个文件 → 候选新文件 → 用户 review

**编辑方式**：用户可直接手动编辑 MEMORY.md（索引）或任何单条记忆文件（格式：YAML frontmatter + Markdown body + 可选 Why 字段）。

#### 5.3.4 Schema 演进（v2.1 增）—— A7 修复

**问题**：frontmatter schema 硬编码在 `validate_edit` 里,没有 `schema_version` 字段,v3 加新字段(`last_accessed_at` / `tags` / `decay_score` 等)后,老记忆文件怎么办?

**v2.1 方案**:每条记忆 frontmatter 加 `schema_version: 1`,启动时跑 MigrationRegistry 懒迁移。

```yaml
---
type: feedback
schema_version: 1         # 🆕 v2.1: schema 演进标识
created_at: 2026-06-08
confidence: 1.0
last_accessed_at: null    # 🆕 v3 将引入, 老文件会被自动填入 mtime
---
```

```python
class MemoryMigration:
    """v2.1 增: schema 演进注册表"""
    
    CURRENT_VERSION = 1
    MIGRATIONS = {
        # v1 → v2 示例 (future): 加 last_accessed_at
        # (1, 2): _v1_to_v2,
    }
    
    def migrate_on_load(self, file_path: Path) -> dict:
        """读文件时懒迁移, 结果写 sidecar 缓存避免重复跑"""
        cache_path = file_path.with_suffix(".migrated.json")
        if cache_path.exists():
            return json.loads(cache_path.read_text())
        
        raw = yaml.safe_load(file_path.read_text())
        version = raw.get("schema_version", 0)  # 缺省=0, v2.1 之前写的
        
        while version < self.CURRENT_VERSION:
            migrator = self.MIGRATIONS.get((version, version + 1))
            if not migrator:
                raise MigrationError(f"no migrator for {version} → {version+1}")
            raw = migrator(raw)
            version += 1
        
        # 写回原文件 + sidecar 缓存
        raw["schema_version"] = self.CURRENT_VERSION
        file_path.write_text(self._dump_frontmatter(raw))
        cache_path.write_text(json.dumps({"version": version, "migrated_at": time.time()}))
        return raw
    
    @staticmethod
    def _v1_to_v2(memory: dict) -> dict:
        """v1 → v2 迁移示例: 加 last_accessed_at"""
        # 启发式: 用 access_count × time_decay 估算
        memory["last_accessed_at"] = (
            memory.get("created_at", time.time())  # 没访问过 → 用创建时间
        )
        return memory
```

**关键不变量**:
- **懒迁移** — 启动时不全表扫,只在 load 时迁,启动 < 100ms
- **sidecar 缓存** — `.md.migrated.json` 记录迁移历史,避免每次读都跑
- **原文件写回** — 迁移完成后写回原 `.md`,保证文件即真相
- **MIGRATIONS 注册表** — 链式迁移 `(v1, v2), (v2, v3) ...`,顺序显式

**为什么不做"启动时全表扫"**:
- 1000+ 文件启动会慢 5-10s
- 大部分文件用户根本不会读
- 懒迁移 + 缓存 = 只在真正用的时候付成本

**对应 issue**:A7 审查指出"v3 加字段后老记忆怎么办",本节给答案。

---

## 六、检索流程

### 6.0 冷启动与种子数据（v2.1 增）—— L5 修复

**问题**:第一次跑 agent-dev,`MEMORY.md` 不存在 → SM-compact 无 SM 文件 → 走传统 compact fallback → 蒸馏无候选 → 用户感知"agent 啥都不记得"。

**v2.1 方案**:`seed/` 目录 + 自动 bootstrap。

```
agent_core/memory/
├── seed/                          ← 🆕 v2.1
│   ├── user/
│   │   └── 000_default_learning_style.md
│   ├── feedback/
│   │   └── 000_default_no_mock.md
│   ├── project/
│   │   └── 000_default_stage1.md
│   └── reference/
│       └── (空)
├── memory_store.py
├── retriever.py
└── extractor.py
```

**Bootstrap 流程** (在 agent 启动时跑一次):

```python
class MemoryBootstrap:
    """v2.1 冷启动: 首次跑时拷贝 seed"""
    
    SEED_DIR = Path(__file__).parent / "seed"
    USER_MEMORY_DIR = Path.home() / ".agent_data" / "memory"
    
    def ensure_seeded(self):
        """如果 .agent_data/memory/ 是空的, 从 seed/ 拷贝"""
        if self._is_empty():
            log.info("first run detected, seeding from defaults")
            for category in ["user", "feedback", "project", "reference"]:
                src = self.SEED_DIR / category
                dst = self.USER_MEMORY_DIR / category
                if src.exists():
                    shutil.copytree(src, dst, dirs_exist_ok=True)
            # 重建 MEMORY.md 索引
            self._rebuild_index()
            self._metrics.increment("memory.bootstrap.first_run_total")
    
    def _is_empty(self) -> bool:
        """任何类别目录都没有 .md 文件 → 视为冷启动"""
        for category in ["user", "feedback", "project", "reference"]:
            if list((self.USER_MEMORY_DIR / category).glob("*.md")):
                return False
        return True
```

**Seed 文件示例** (`seed/user/000_default_learning_style.md`):
```markdown
---
type: user
schema_version: 1
created_at: 2026-06-20
confidence: 0.5
source: seed_default
---
# 默认学习风格 (待覆盖)

**Why:** agent-dev 默认认为新用户偏好"先手写原生 ReAct 理解本质,
再上框架"。这是项目启动时 (Stage 1) 的用户决定; Stage 2 后会被
真实偏好自动覆盖 (蒸馏引擎会用更高 confidence 的记忆替换)。

**How to override:** 直接编辑此文件, 或在对话中说"我的学习风格是 X",
agent 会自动写新记忆并通过蒸馏淘汰此 seed。
```

**为什么 confidence=0.5 (低于正常 1.0)**:
- 标记为"默认假设",蒸馏时优先被替换
- 真实对话积累的 user 记忆 confidence > 0.7,蒸馏时胜出

**冷启动 + 现有用户**:
- `ensure_seeded()` 只在 `_is_empty()` 时跑,不会覆盖已有记忆
- 老用户升级 agent-dev v2.1 → 不触发 seed (他们的记忆已存在)
- 真正的"首次"用户 → 拿到 4 个 seed 文件 + 蒸馏引擎正常工作

**对应 issue**:L5 审查指出"首次跑无记忆无 bootstrap 路径",本节给完整方案。

### 6.1 检索时机

| 检索类型 | 时机 | 触发者 | 结果 |
|---------|------|-------|------|
| **上下文注入** | 每次 `agent.run()` 开始前 | Agent 内部逻辑 | 相关记忆注入 system prompt |
| **主动检索** | 用户问"你记得吗" | memory_search 工具 | 语义搜索返回结果 |
| **蒸馏参考** | 蒸馏引擎运行时 | distiller.distill() | 读取近期日志 |

### 6.2 检索流程图（v2.1：3 模式分流）

```
用户消息: "我的学习风格是什么？"
        ↓
    Agent.run()
        ↓
   config.retrieval.mode?
        │
   ┌────┴─────┬──────────┐
   ↓          ↓          ↓
 vector      file     hybrid
 (Chroma)  (LLM-side) (组合)
   ↓          ↓          ↓
 cosine     读 MEMORY.md  cosine top_20
 top_k      LLM 选 top_5  ↓
   ↓          ↓          LLM 选 top_5
   ↓          ↓          ↓
   └──────────┴──────────┘
              ↓
┌─────────────────────────────────┐
│  构建 memory_context            │
│  [相关记忆]                     │
│  - 先手写原生 ReAct 理解本质... │
│  - 重视底层原理，多 Agent 研读  │
└────────────┬────────────────────┘
             ↓
┌─────────────────────────────────┐
│  注入 system prompt            │
│  system_prompt += memory_context│
└────────────┬────────────────────┘
             ↓
         Agent 响应
```

**三种模式说明**：
- **vector**（v1 路径）：仅用 Chroma cosine similarity 召回（详见 §六.4）
- **file**（v2.1 路径 B）：读 MEMORY.md + LLM side-query 选文件（详见 §6.5.4）
- **hybrid**（v2.1 推荐）：向量粗筛 20 → LLM 精排 5（详见 §6.6）

> **v2 修正**：原流程图只画了 vector 模式（v1 设计）。v2.1 改为三分流模式，默认 `mode=hybrid`。

### 6.3 检索排序公式（v2：分段时间衰减）

```
final_score = (semantic_score × 0.6 + time_score × 0.3 + access_score × 0.1) × confidence

其中：
- semantic_score = 1 - cosine_distance（Chroma 返回，仅 mode=vector/hybrid 用）
- time_score = 分段函数（v2 修正，详见 §16.2）：
  - 30 天内：1.0（完全可信）
  - 30-90 天：1.0 - 0.5 × (age_days - 30) / 60（线性下降到 0.5）
  - 90 天+：0.3（保底）
- access_score = min(1.0, access_count × 0.1)
  - 每次被检索命中：access_count +1（间接通过 access_score 提升 final_score）
  - 注：不直接修改 confidence，避免 probability 越界
- confidence：初始 1.0，每日衰减 ×0.95
```

> **v2 修正**：v1 公式 `time_score = max(0.3, 1.0 - age_days / 30)` 在 `age_days = 30` 时返回 0，与"30 天内完全可信"的语义矛盾。v2 用分段函数明确表达（详见 §16.2）。

### 6.4 检索代码

```python
class MemoryRetriever:
    """v2.1 检索器: 三模式 + 注入策略 + token budget"""
    
    def __init__(self, config: RetrievalConfig):
        self.config = config
        # L4 修复: 注入模式默认 summary 而非 full (省 6KB/turn)
        self.inject_mode = config.inject_mode  # "summary" | "full"
        # L8 修复: 注入 token 硬上限, 防止超 context
        self.max_injection_tokens = config.max_injection_tokens  # 默认 2000
    
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

        # L12 修复: 0 命中时记 debug metric + 状态条反馈
        if not results:
            self._metrics.increment("memory.search.zero_hit_total")
            log.debug(f"memory search: 0 hits for query len={len(query)}")
        else:
            self._metrics.gauge("memory.search.result_count", len(results))

        return results

    def _build_memory_context(self, memories: list[MemoryFile]) -> str:
        """
        v2.1 注入策略: 默认 summary, 可选 full, 强制 token budget
        
        L4 修复: 之前 3 条 × 2KB = 6KB 每 turn, 100 turn 600KB
        L8 修复: 之前无 budget check, 20 × 2KB = 40KB 可超 32K context
        
        三层控制:
        1. inject_mode: summary (default) / full
        2. max_injection_tokens: 硬上限 (默认 2000)
        3. score_threshold: 低于阈值的不注入
        """
        if not memories:
            return ""  # L12: 0 命中已在 _retrieve_memories 记 metric
        
        lines = ["[相关记忆]"]
        total_tokens = 0
        injected_count = 0
        
        for mem in sorted(memories, key=lambda m: m.score, reverse=True):
            # L4: summary 模式只注入 frontmatter + 1 行摘要
            if self.inject_mode == "summary":
                text = mem.summary  # ~150 chars
            else:
                text = mem.content  # up to 2KB
            
            entry = f"- {text}"
            entry_tokens = self._count_tokens(entry)
            
            # L8: token budget 硬限
            if total_tokens + entry_tokens > self.max_injection_tokens:
                self._metrics.increment("memory.injection.truncated_total")
                log.debug(f"injection truncated at {injected_count} memories, "
                          f"budget={self.max_injection_tokens}, used={total_tokens}")
                break
            
            lines.append(entry)
            total_tokens += entry_tokens
            injected_count += 1
        
        # L12: 状态条反馈
        if injected_count < len(memories):
            self._metrics.gauge(
                "memory.injection.injected_vs_total",
                f"{injected_count}/{len(memories)}",
            )
        
        return "\n".join(lines) + "\n\n"
    
    def _count_tokens(self, text: str) -> int:
        """粗略 token 估算 (4 chars ≈ 1 token, 误差 ±20%)"""
        return len(text) // 4
```

**配置项 (L4 + L8)**:
```python
class RetrievalConfig(BaseModel):
    mode: Literal["vector", "file", "hybrid"] = "hybrid"
    top_k: int = 3
    # L4: 注入模式 (默认 summary 省 6KB/turn)
    inject_mode: Literal["summary", "full"] = "summary"
    # L8: 注入 token 硬上限 (默认 2000, 防止超 context)
    max_injection_tokens: int = Field(2000, ge=100, le=10000)
    # 旧字段保留
    vector_top_k: int = 20
    file_top_k: int = 5
    time_decay_days: int = 30
```

**注入量对比**:

| 模式 | 单 turn 注入 | 100 turn 累计 | 上下文压力 |
| --- | --- | --- | --- |
| v1 (full, 无 budget) | 6KB worst | 600KB (重复) | ❌ 必爆 |
| v2 (full, 无 budget) | 6KB worst | 600KB | ❌ |
| **v2.1 summary** (default) | ~500B (3×150) | 50KB (重复但小) | ✅ 友好 |
| v2.1 full + budget 2000 | ≤2KB (硬限) | ≤200KB | ✅ 友好 |

**L12 状态条反馈** (放 §13.1):
```
Memory: 3 relevant / 47 total
   ↑ §6.4 注入数  ↑ §13.5 总记忆数
```
当 0 relevant 时,显示 `Memory: 0 relevant (search ran, found nothing)`。

### 6.5 🆕 检索路径重新评估：向量索引 vs 文件 + LLM 二次筛选

**重要发现**：v2 升级时这一节没有跟着重写。Claude Code 实际**完全不用向量数据库**——所有"检索"都是基于 MEMORY.md 索引 + LLM 二次筛选。让我把这个发现和取舍讲清楚。

#### 6.5.1 两种检索路径对比

**路径 A（v1 设计）：向量召回**

```
用户消息 → MemoryStore.search() → cosine similarity top-k → 注入 prompt
                  ↑
            Chroma 向量库
```

**路径 B（Claude Code 实际做法）：文件 + LLM 二次筛选**

```
用户消息 → 加载 MEMORY.md 索引（~5KB）→ LLM side-query 选 ≤5 文件
                                       → 读这 5 个文件
                                       → 注入 prompt
```

参考 Claude Code `findRelevantMemories`：用一个小模型（Sonnet sideQuery）读索引文件，挑出最相关的几个记忆文件路径，再读这些文件的实际内容。

#### 6.5.2 深度对比

| 维度 | 路径 A：向量召回 | 路径 B：文件 + LLM 筛选 |
| --- | --- | --- |
| **基础设施** | Chroma + sentence-transformers + SQLite metadata | 纯文件系统 + MEMORY.md |
| **检索调用** | DB query（< 10ms） | LLM side-query（~500ms）+ 读 N 个文件（< 100ms） |
| **检索精度** | 受限于 embedding 模型对上下文的理解 | LLM 直接理解语义，**精度更高** |
| **可解释性** | ❌ 黑盒（"为什么这条命中？"难回答） | ✅ LLM 可以解释"为什么选这 5 条" |
| **维护成本** | embedding 模型需要定期 re-encode 新记忆 | 文件加一条，索引加一行 |
| **跨语言** | 多语言 embedding 模型需要预训练 | LLM 原生多语言 |
| **冷启动** | 需要批量 encode 已有记忆 | 立即可用 |
| **扩展性** | ✅ 数千条以上仍快 | ⚠️ MEMORY.md 索引会增长，需要 L2 缓存策略 |
| **依赖项** | chromadb + sentence-transformers（~500MB） | 无（用现成的 LLM） |

#### 6.5.3 agent-dev 应该选哪条？

**v2.1 推荐：路径 B（文件 + LLM 二次筛选）**

理由：
1. **agent-dev 是单用户研究项目**，记忆规模预期在**几百条**，远低于向量库的甜区（数千+）
2. 路径 B **少一个重量级依赖**（不用装 chromadb / sentence-transformers，节省 ~500MB 磁盘和冷启动时间）
3. LLM 二次筛选的**精度 > 向量召回**——这是 Claude Code 选这条路径的根本原因
4. **可解释性** 是研究项目的关键优势（你可以问 LLM "为什么选这条"，而不是面对黑盒分数）

**什么时候才需要路径 A？**

- 记忆规模达到**数千条以上**（MEMORY.md 索引超过 ~10KB，LLM 选择成本上升）
- 多用户 / 团队场景（每个用户都需要独立的快速检索）
- 需要**离线检索**（没有 LLM 可调用时）

#### 6.5.4 路径 B 的实现（推荐方案）

```python
class FileBasedRetriever:
    """
    v2.1 推荐: 文件 + LLM 二次筛选
    
    借鉴 Claude Code findRelevantMemories
    """

    def __init__(self, memory_root: str, llm_router):
        self.memory_root = pathlib.Path(memory_root)
        self.llm = llm_router  # 用主 Router 实例即可（共享 cache）

    def retrieve(self, query: str, top_k: int = 5) -> list[MemoryFile]:
        # 1. 读取索引文件（轻量, ~5KB）
        index = (self.memory_root / "MEMORY.md").read_text()

        # 2. LLM side-query 选最相关的 N 个文件
        #    用小模型 (Haiku) 即可, 与主对话 cache 共享
        selected_files = self._llm_pick_files(index, query, top_k)

        # 3. 读取这些文件的实际内容
        memories = []
        for rel_path in selected_files:
            file_path = self.memory_root / rel_path
            if not file_path.exists():
                continue  # 蒸馏后文件可能移动
            content = file_path.read_text()
            memories.append(self._parse_memory_file(file_path, content))

        # 4. 更新访问次数（用 SQLite metadata, 不需要向量库）
        self._update_access_counts(memories)

        return memories

    def _llm_pick_files(self, index: str, query: str, top_k: int) -> list[str]:
        """
        LLM 从索引里挑 top_k 个最相关的文件
        
        设计:
        - 用 Haiku 小模型, 成本低
        - 输入 < 2K tokens, 一次调用
        - 输出严格的 JSON 数组
        """
        prompt = f"""基于以下 MEMORY 索引, 选出与用户问题最相关的 {top_k} 个记忆文件.

用户问题: {query}

MEMORY 索引:
{index}

只返回文件路径列表 (相对路径), 严格的 JSON 数组格式, 不要其他内容:
["user/2026-06-08_learning_style.md", "feedback/2026-06-08_no_db_mock.md"]"""

        response = self.llm.chat(
            messages=[{"role": "user", "content": prompt}],
            model="claude-haiku-4-5",  # 小模型足够
            temperature=0.0,
        )
        return json.loads(response.strip())

    def _update_access_counts(self, memories: list[MemoryFile]):
        """被检索命中 → access_count +1 (用 SQLite metadata)"""
        for mem in memories:
            self.metadata_db.execute(
                "UPDATE memory_meta SET access_count = access_count + 1 WHERE id = ?",
                (mem.id,),
            )
```

#### 6.5.5 检索路径决策矩阵

| 项目规模 | 推荐路径 | 理由 | 索引分片策略 |
| --- | --- | --- | --- |
| agent-dev（个人研究项目，< 1K 条记忆） | **路径 B** | 简单、精度高、省依赖 | 单 MEMORY.md（< 5KB） |
| 中型项目（1K-10K 条） | **路径 B + 索引分片** | 索引增长，需拆分 | 按 type 分 4 文件：`MEMORY.user.md` / `MEMORY.feedback.md` / `MEMORY.project.md` / `MEMORY.reference.md` |
| 大型项目（> 10K 条） | **路径 A** | MEMORY.md 索引超过 LLM 上下文窗口 | 向量库 + SQLite metadata |
| 团队 / 多用户 | **路径 A** | 每个用户独立快速检索 | 向量库按 user_id 分片 |

**中型项目分片策略详解**（v2.1 推荐）：

```
memory/
├── MEMORY.user.md           ← user 类索引
├── MEMORY.feedback.md       ← feedback 类索引
├── MEMORY.project.md        ← project 类索引
├── MEMORY.reference.md      ← reference 类索引
├── user/
│   └── *.md                 ← 实际记忆文件
└── ...
```

**分片的好处**：
- LLM side-query 只需读相关的 1-2 个索引文件（而不是整个 MEMORY.md）
- 单文件增长可控（每个 type 单独 1K-2K 条记忆）
- 仍保持"用户可读、git 友好"的特性

**分片的代价**：
- 检索时如果跨 type（如"找出所有和 X 相关的记忆"）需要 LLM 选 type 后再选文件——多一跳

#### 6.5.6 §六 章节修订总结

| 章节 | v1 | v2 | v2.1（本次建议） |
| --- | --- | --- | --- |
| §6.1 检索时机 | ✅ | ✅（保留） | ✅ |
| §6.2 检索流程图 | 向量召回 | （未重写） | **改为文件 + LLM 流程** |
| §6.3 排序公式 | semantic + time + access | （未重写） | **删除 semantic_score，改用 LLM 选择 + access 加权** |
| §6.4 检索代码 | `MemoryStore.search()` | （未重写） | **改为 `FileBasedRetriever.retrieve()`** |
| §6.5 重新评估 | — | — | 🆕 本节 |

#### 6.5.7 配套变更

> ⚠️ **本节已被 §6.6 取代**——§6.6 决定**保留双模式共存**，不再删除 v1 资产。本节保留仅为变更追踪。

- **保留依赖**：`chromadb`, `sentence-transformers`（作为 `mode=vector` 选项，详见 §6.6）
- **保留依赖**：`sqlite3`（仍用于 access_count, time_decay, last_distilled_at 等元数据）
- **新增文件**：`agent_core/memory/retriever.py`（三模式入口，详见 §6.6）
- **保留文件**：`agent_core/memory/memory_store.py`（Chroma 向量库，作为 `mode=vector` 实现）

> **最终决策**（由 §6.6 确定）：保留 v1 的 Chroma 资产，改为 `mode=vector` 模式可切换；§5.2 / §6.1-§6.4 全部保留（不再是"v1 legacy"，而是"vector 模式"的具体实现）。

### 6.6 🆕 双模式共存：配置切换 + Hybrid 模式

**6.5 节建议"v2.1 切到路径 B"，但本节提出更稳妥的方案：保留双模式，用配置切换 + Hybrid 兼顾学习价值与生产性能。**

#### 6.6.1 设计动机

为什么不直接选一条？

| 理由 | 说明 |
| --- | --- |
| **学习价值** | 向量召回是经典 IR 范式，研究 agent-dev 值得保留作为 baseline |
| **A/B 对比** | 同一查询可以用两种方式跑，对比精度 / 延迟 / 成本 |
| **场景适配** | 不同规模 / 不同场景下最优模式不同，硬切会丢失灵活性 |
| **渐进迁移** | v1 → v2.1 不必一步到位，可以配置切换逐步验证 |

#### 6.6.2 三种模式

```json
{
  "retrieval": {
    "mode": "hybrid",            // "vector" | "file" | "hybrid"
    "vector_top_k": 20,          // 仅 hybrid / vector 用
    "file_top_k": 5,             // 最终返回多少条
    "hybrid_strategy": "vector_filter_then_llm_rerank"  // hybrid 子策略
  }
}
```

| 模式 | 流程 | 适用场景 | 学习价值 |
| --- | --- | --- | --- |
| **`vector`**（v1） | 用户查询 → Chroma cosine → top_k → 注入 | 离线 / 无 LLM / 大规模（10K+） | ⭐⭐⭐⭐⭐ 经典 IR 范式 |
| **`file`**（CC 做法） | 用户查询 → 读 MEMORY.md → LLM side-query → 读 N 文件 → 注入 | 生产环境 / < 1K 条记忆 | ⭐⭐⭐ 现代 LLM-native 范式 |
| **`hybrid`**（v2.1 推荐） | 用户查询 → Chroma top_20（粗筛）→ LLM side-query 选 5（精排）→ 注入 | 通用 / A/B 测试 / 迁移期 | ⭐⭐⭐⭐⭐ 同时学两种 |

#### 6.6.3 Hybrid 模式详解（推荐）

```
                    ┌──────────────────┐
用户查询 ─────────→ │ 1. 向量粗筛      │
                    │    Chroma top_20 │ ← 快速, ~10ms, 召回率高
                    └────────┬─────────┘
                             │ 20 个候选
                             ▼
                    ┌──────────────────┐
                    │ 2. LLM 精排       │
                    │    side-query    │ ← 精确, ~500ms
                    │    选 top_5      │
                    └────────┬─────────┘
                             │ 5 个文件路径
                             ▼
                    ┌──────────────────┐
                    │ 3. 读取文件内容   │ ← < 100ms
                    └────────┬─────────┘
                             │
                             ▼
                    注入 system prompt
```

**关键设计点**：

1. **向量粗筛是"召回"（recall）**——目标是不漏，宁可多要（top_20 > 实际需要的 5 条）
2. **LLM 精排是"排序"（precision）**——从 20 条里挑真正最相关的 5 条
3. **两者各司其职**——向量负责"快速缩小范围"，LLM 负责"理解语义挑最优"

#### 6.6.3.1 量化指标（为什么是 20→5）

| 指标 | 公式 | Vector-only | File-only | Hybrid (20→5) |
| --- | --- | --- | --- | --- |
| **Recall@20** | 相关 ∩ 召回 / 相关 | ~0.85 (Chroma 阈值 0.7) | ~0.40 (LLM 看不到全文) | ~0.85 (复用 vector 召回) |
| **Precision@5** | 召回 ∩ 相关 / 召回 | ~0.55 (语义相似 ≠ 真相关) | ~0.80 (LLM 推理) | ~0.85 (向量粗筛 + LLM 精排) |
| **P95 延迟** | ms | ~15 ms | ~600 ms (LLM 一次) | ~520 ms (向量 ~10ms + LLM ~500ms + IO ~10ms) |
| **成本/次** | tokens | 0 | ~500 in + 100 out (haiku) | ~300 in + 80 out (haiku, 摘要更短) |

> **数据来源**：
> - Recall@20 (0.85) 参考 Chroma 默认 sentence-transformers/all-MiniLM-L6-v2 在 1K 文档集上的常见报告值
> - File-only Recall@0.40 来自 Claude Code 实际观察（MEMORY.md 索引短文匹配长尾查询能力差）
> - Hybrid 延迟 = Vector (10ms) + LLM side-query (500ms) + file IO (10ms)
> 
> **本表用于内部决策**，不是 benchmark 承诺；上线后用 §6.6.5 A/B 框架实测替换为真值。

#### 6.6.4 实现

```python
class HybridRetriever:
    """
    v2.1 Hybrid 检索器: 向量粗筛 + LLM 精排
    """

    def __init__(
        self,
        vector_store: Optional["MemoryStore"] = None,    # v1 Chroma
        file_index: "MEMORYIndex" = None,                # v2.1 文件索引
        llm_router = None,
        config = None,
    ):
        self.vector_store = vector_store  # 可选, mode=file 时为 None
        self.file_index = file_index
        self.llm = llm_router
        self.config = config or {
            "mode": "hybrid",
            "vector_top_k": 20,
            "file_top_k": 5,
        }

    def retrieve(self, query: str) -> list[MemoryFile]:
        mode = self.config["mode"]

        if mode == "vector":
            return self._retrieve_vector_only(query)

        elif mode == "file":
            return self._retrieve_file_only(query)

        elif mode == "hybrid":
            return self._retrieve_hybrid(query)

        else:
            raise ValueError(f"unknown retrieval mode: {mode!r}")

    # ---------- Vector-only (v1 路径) ----------
    def _retrieve_vector_only(self, query: str) -> list[MemoryFile]:
        """纯向量召回"""
        results = self.vector_store.search(
            query=query,
            top_k=self.config["file_top_k"],
            time_decay_days=30,
        )
        return self._hydrate_to_memory_files(results)

    # ---------- File-only (v2.1 路径 B) ----------
    def _retrieve_file_only(self, query: str) -> list[MemoryFile]:
        """纯文件 + LLM 二次筛选"""
        # 实现见 §6.5.4 FileBasedRetriever
        ...

    # ---------- Hybrid (推荐) ----------
    def _retrieve_hybrid(self, query: str) -> list[MemoryFile]:
        """
        向量粗筛 → LLM 精排
        
        关键:
        - vector_top_k (20) >> file_top_k (5): 粗筛召回, 精排去噪
        - LLM 看到的是 20 个候选的简短描述 (从 MEMORY.md 截取), 不是全文
        - 选完后才读 5 个文件的全文
        """
        # 步骤 1: 向量粗筛 (粗召回)
        candidates = self.vector_store.search(
            query=query,
            top_k=self.config["vector_top_k"],
            time_decay_days=30,
        )
        if not candidates:
            return []

        # 步骤 2: LLM 精排 (从 20 选 5)
        #         给 LLM 看的是每个候选的摘要 (从 MEMORY.md 索引截取)
        # 注: c["summary"] 字段由 ingest 时填入 (从 frontmatter 提取 + 一句话描述)
        candidate_summaries = [
            f"[{i}] {c['summary']}"
            for i, c in enumerate(candidates)
        ]
        n_candidates = len(candidate_summaries)
        selected_indices = self._llm_pick(
            query=query,
            candidates="\n".join(candidate_summaries),
            n_candidates=n_candidates,
            top_k=self.config["file_top_k"],
        )

        # 步骤 3: 读入选文件的全文
        selected = [candidates[i] for i in selected_indices]
        return self._hydrate_to_memory_files(selected)

    def _llm_pick(self, query: str, candidates: str, n_candidates: int, top_k: int) -> list[int]:
        """LLM 精排: 从 N 个候选选 top_k"""
        prompt = f"""用户问题: {query}

候选记忆 ({n_candidates} 条):
{candidates}

选出最相关的 {top_k} 条, 只返回索引列表 (JSON 数组):
[0, 3, 7, 12, 15]"""
        response = self.llm.chat(
            messages=[{"role": "user", "content": prompt}],
            model="claude-haiku-4-5",
            temperature=0.0,
        )
        return json.loads(response.strip())

    def _hydrate_to_memory_files(self, results: list[dict]) -> list[MemoryFile]:
        """向量召回结果 → 读文件内容"""
        memories = []
        for r in results:
            file_path = self.memory_root / r["rel_path"]
            if file_path.exists():
                memories.append(MemoryFile(
                    path=file_path,
                    content=file_path.read_text(),
                    score=r.get("score"),
                ))
        return memories
```

#### 6.6.5 A/B 测试：用 Hybrid 验证两种模式

**这是个意外的收获——Hybrid 模式天然支持 A/B 测试**：

```python
class ABTestRetriever:
    """
    用 hybrid 模式做 A/B 测试:
    - vector-only 路径单独跑一次, 记录 top_k 结果
    - file-only 路径单独跑一次, 记录 top_k 结果
    - hybrid 路径跑一次, 记录结果
    - 对比三者的 overlap (Jaccard 相似度) 和用户反馈
    """

    def retrieve_with_ab(self, query: str) -> ABTestResult:
        vector_results = self._retrieve_vector_only(query)
        file_results = self._retrieve_file_only(query)
        hybrid_results = self._retrieve_hybrid(query)

        return ABTestResult(
            query=query,
            vector_only=[m.path for m in vector_results],
            file_only=[m.path for m in file_results],
            hybrid=[m.path for m in hybrid_results],
            jaccard_vector_file=self._jaccard(vector_results, file_results),
            jaccard_hybrid_vector=self._jaccard(hybrid_results, vector_results),
            jaccard_hybrid_file=self._jaccard(hybrid_results, file_results),
            latency={
                "vector": self._last_latency_vector,
                "file": self._last_latency_file,
                "hybrid": self._last_latency_hybrid,
            },
        )
```

**用法**：先跑一个月 A/B 测试，统计 jaccard 和用户反馈（哪些被采纳），决定生产用哪个模式。

#### 6.6.6 配置示例

```json
{
  "retrieval": {
    "mode": "hybrid",
    "vector_top_k": 20,
    "file_top_k": 5,
    "hybrid_strategy": "vector_filter_then_llm_rerank",
    "ab_test_enabled": false,
    "ab_test_log_path": ".agent_data/ab_test_logs/"
  }
}
```

**切换模式**（无需重启）：

```python
config.set("retrieval.mode", "file")  # 切到纯 LLM
config.set("retrieval.mode", "vector")  # 切到纯向量
config.set("retrieval.mode", "hybrid")  # 切到 hybrid (推荐)
```

#### 6.6.7 双模式决策总结

| 你的需求 | 推荐模式 |
| --- | --- |
| **学习 / 研究**（想了解 IR 范式） | `vector` |
| **生产环境**（中小规模） | `file` |
| **通用 + 性能 + 精度兼顾** | **`hybrid`** ⭐ |
| **A/B 测试 / 渐进迁移** | `hybrid` + `ab_test_enabled: true` |
| **大规模（10K+）** | `vector`（file 路径的索引会膨胀） |
| **离线 / 无 LLM** | `vector` |

#### 6.6.8 配套保留 v1 资产

由于现在不删除 `MemoryStore`（向量召回）：

- **保留依赖**：`chromadb`, `sentence-transformers`（§九 不再删除）
- **保留文件**：`agent_core/memory/memory_store.py`（向量库实现）
- **新增文件**：`agent_core/memory/retriever.py`（三模式 retriever 入口）
- **修改文件**：`agent_core/memory/__init__.py`（暴露 retriever 工厂）

> **最终决策**：v2.1 推荐 `mode: "hybrid"`（双模式共存，向量粗筛 + LLM 精排），同时保留 `mode: "vector"` 和 `mode: "file"` 作为可切换的备选。文档 §5.2 / §6.1-§6.4 全部保留（不再是"v1 legacy"，而是"vector 模式"的具体实现）。

### 6.7 Prompt Cache 策略（v2.1 增）—— L3 修复

**问题**:全文多次提到 `cache_safe_params=True`,但**没说清"什么算 cache-safe" + 怎么测 cache 命中率**。Anthropic 写 cache 收 25% 溢价,读 cache 省 90%,乱用会亏损。

**Anthropic Prompt Cache 规则**(2024+):
- 4 个 breakpoint,每个 prefix 至少 1024 tokens
- prefix 必须**字节级相同**才能命中
- 命中读: $0.30/MTok (cache read) vs $3.00/MTok (input)
- 写: $3.75/MTok (cache write) — 比 input 贵 25%

**v2.1 重构所有 prompt,让 system 放稳定 prefix,user 放变量**:

```python
# ❌ 错误:把变量塞 system,无法 cache
system = f"You are a memory extractor. User name: {user_name}. Today: {today}."

# ✅ 正确:system 完全静态,user 装变量
system = "You are a memory extractor. Output JSON only."  # 跨所有调用字节相同
user = f"<conversation>\n{conversation}\n</conversation>\n\nUser: {user_name}\nDate: {today}"
```

**4 个 cache 优化点**(v2.1 重构):

| Prompt | 之前 (v2) | 之后 (v2.1) | Cache 命中率 |
| --- | --- | --- | --- |
| `_llm_score_and_extract` (§3.3) | system 含动态 schema 描述 | system 纯静态,schema 进 user 段 | 0% → ~85% |
| `_llm_pick_files` (§6.5) | system 含候选摘要 | system 静态,候选列表全进 user | 0% → ~70% |
| `distill` (§7.3) | system 含 session 列表 | system 静态,session 摘要进 user | 0% → ~80% |
| `_llm_verify_memory` (L7 修复) | 每次都重写 | system 静态,候选 memory 进 user | 0% → ~90% |

**测量方法**(必加,放 §13.6 OTel):
```python
response = self.router.chat(...)
# Anthropic 返回里带 cache 命中信息
cache_read = response.usage.cache_read_input_tokens
cache_write = response.usage.cache_creation_input_tokens
total_input = response.usage.input_tokens

cache_hit_rate = cache_read / (cache_read + total_input) if total_input > 0 else 0
self._metrics.gauge("memory.llm.cache_hit_rate", cache_hit_rate)
```

**目标**:稳态下 cache hit rate > 70% (L1 合并 + cache 重构后,从原来 0% 升到 ~80%)

### 6.8 LLM Integration Contract（v2.1 增）—— L13 修复

**问题**:全文传 `cache_safe_params=True` / `cache_namespace="..."` 但 `LLMRouter` 接口没定义,这些 kwarg 是静默忽略还是魔法未 wire,无从验证。

**v2.1 显式定义 router 接口契约**:

```python
from typing import Protocol, Literal

class LLMUsage(BaseModel):
    """Anthropic usage 标准字段, router 必须透传"""
    input_tokens: int
    output_tokens: int
    cache_read_input_tokens: int = 0   # 命中读
    cache_creation_input_tokens: int = 0  # 首次写
    cache_uncached_input_tokens: int = 0  # 未命中

class LLMResponse(BaseModel):
    content: str
    usage: LLMUsage
    model: str
    stop_reason: str

class LLMRouter(Protocol):
    """v2.1 LLM 路由契约 — 所有 memory 模块只能依赖这个接口"""
    
    def chat(
        self,
        messages: list[dict],
        provider: Literal["anthropic", "openai", "google"] = "anthropic",
        model: str = "claude-haiku-4-5",
        temperature: float = 0.0,
        tools: list[dict] | None = None,
        # Cache 字段 (L13 修复: 显式契约)
        cache_safe_params: bool = False,    # True → system prompt 走 cache
        cache_namespace: str = "default",   # 同 namespace 共享 cache 桶
        # 可观测性
        trace_span_name: str | None = None, # OTel span 名 (§13.6)
    ) -> LLMResponse:
        ...
```

**`cache_namespace` 语义**:
- 同 namespace + 同 system prompt prefix → 共享 cache (跨调用复用)
- 不同 namespace → 独立 cache (隔离,适合 A/B 测试)
- 例: `memory_extract_score` / `memory_pick_files` / `memory_dream` 三个 namespace

**单元测试** (必加,放 §4.5.1 矩阵的并发场景组):
```python
def test_cache_namespace_sharing():
    """同 namespace 第二次调用应该 cache 命中"""
    router = MockRouter()
    r1 = router.chat(
        messages=[{"role": "system", "content": "Extract."}, {"role": "user", "content": "msg1"}],
        cache_namespace="test_ns",
    )
    r2 = router.chat(
        messages=[{"role": "system", "content": "Extract."}, {"role": "user", "content": "msg2"}],
        cache_namespace="test_ns",
    )
    assert r2.usage.cache_read_input_tokens > 0  # 第二次命中

def test_cache_namespace_isolation():
    """不同 namespace 各自独立"""
    r1 = router.chat(messages=..., cache_namespace="ns_a")
    r2 = router.chat(messages=..., cache_namespace="ns_b")
    assert r2.usage.cache_read_input_tokens == 0
```

**对应 issue**:L13 审查指出"`cache_safe_params` / `cache_namespace` 是黑魔法",本节给完整契约 + 单测。

### 6.9 门1 周期内去重 + 短对话"记住"策略（v2.1.1 增）—— M9 修订

> **修订背景**:M9 联调中,按 §3.3.1 调整版决策树实现后,发现两个新问题需要明确:
> 1. **门1 周期内重复提取**:门1 累计型触发会覆盖门2 已提过的内容
> 2. **短对话"记住"不响应**:严格按 §3.3 后,短对话里"记住"被门1 累计阈值卡住

#### 6.9.1 门1 周期内去重（LLM 提示词层去重,非代码层）

**问题场景**:
```
Turn 5: "我总是用 uv"        累计 3.5K
        门2 命中"总是" → 跑 B → 写入"用户习惯用 uv"
        (累计不清零,因为门2 触发)

Turn 15: 累计 12K
         门1 ≥ 10K → 跑 B
         LLM 看到对话里有 uv 提及 → 又觉得值得记
         → 重复写入"用户习惯用 uv"(和 Turn 5 一模一样)
```

**解法**:门1 触发时,**LLM 提示词包含"本周期已提取的记忆"**,让 LLM 自己去重。

**为什么不在代码层去重**:

| 方案 | 优点 | 缺点 |
|------|------|------|
| **代码层去重**(维护 `_max_processed_turn` 状态) | 状态可控 | **上下文断层**:LLM 看不到完整对话 |
| **LLM 提示词层去重**(本次方案) | **上下文连贯**:LLM 看到完整对话 + 已记下的 | 依赖 LLM 语义判断 |

**用户明确选**:LLM 提示词层去重,以保证上下文连贯。

**提示词模板**:

```xml
<existing_memories_in_this_period>
[user] 习惯用 uv (turn 5)
[project] 项目叫 agent-dev (turn 8)
</existing_memories_in_this_period>

<conversation>
[turn 6] ...
[turn 7] ...
...
[turn 10] ...
</conversation>

请基于以上评估,提取"本周期内"的新记忆(避免和已提取的重复)
```

**状态管理**:

```python
# bridge 维护一个变量
self.gate1_period_start_turn: int = 0  # 当前门1 周期起点

# 每次门1 跑完,更新该变量
if decision.via_gate1 and decision.should_extract:
    self.gate1_period_start_turn = turn_index + 1
```

**MemoryStore.write 的 `item_hash` 仍保留作为最后兜底**（已有,不依赖 LLM 语义判断）。

#### 6.9.2 短对话"记住"策略（不修,改用 UI toggle 显式表达）

**问题**:
```
短对话(累计 < 10K)里用户说"记住我的名字叫张三"
  → 门1 不达(3K < 10K)
  → 门2 命中"记住"
  → 调 LLM 评分
  → 写盘 ✓
```

**等等,门2 命中就会调 LLM,为什么说"不响应"?**

> **澄清**:门2 命中 → 调 LLM 评分 → confidence ≥ 0.6 才写盘。这是 §3.3.1 调整版的行为,不是"不响应"。
>
> **真正"不响应"的场景是**:门1 不达 + 门2 关键词未命中(比如用户说"我倾向 X"不命中 16 个关键词)。

**两种修法评估**:

| 方案 | 行为 | 评价 |
|------|------|------|
| **A. 关键词强意图短路**(门1 不到也调 LLM)| "记住" 命中 → 跳过门1门2 → 调 LLM | **违背设计 §3.3 严格性** |
| **B. UI toggle "强制提取"** | 用户主动勾 toggle → 该 turn 必走 LLM | **符合 Claude Code `/remember` 哲学** |

**用户选择 B**:不修决策树,改用 UI toggle 显式表达"立刻提取"意图。

**Toggle 设计**:

- **默认关**(严格按 §3.3.1)
- **勾上后**:该 turn 必走 LLM 评分(绕开门1门2)
- **不持久化**:每次 session 重新勾
- **位置**:sidebar `🧠 Memory 状态` 折叠面板

**为什么不选 A**:

1. **违背 §3.3 严格性**:"猜用户意图"是设计本意拒绝的
2. **关键词扩展风险**:为了命中更多意图,关键词会膨胀,污染决策树
3. **CC 参考**:Claude Code 用 `/remember` slash command,不靠关键词猜
4. **可观测**:toggle 显式表达,用户清楚什么时候在"加速模式"

#### 6.9.3 决策树最终版(综合 §3.3.1 + §6.9.1 + §6.9.2)

```
B 触发决策树（v2.1.1 最终版）:
│
├─ Toggle "强制提取" 勾上?
│   ├─ 是 → 必走门3 LLM 评分
│   └─ 否 ↓
│
├─ 门1（累计型）: cumulative_tokens >= 10K  OR  tool_calls >= 10  ?
│   ├─ 是 → 进入门3 LLM 评分
│   │       (提示词含 <existing_memories_in_this_period> 让 LLM 自己去重)
│   │       (跑完 B 后,累计清零)
│   └─ 否 ↓
│
├─ 门2（事件型）: 16 个关键词中 ≥1 命中?
│   ├─ 是 → 进入门3 LLM 评分
│   │       (不清零累计)
│   └─ 否 → SKIP, reason: "no_trigger"
│
└─ 门3（质量门）: LLM 评分 confidence >= 0.6 ?
    ├─ 是 → 提交 B
    └─ 否 → SKIP, reason: "low_confidence(0.XX)"
```

**对应实现 spec**:`docs/superpowers/specs/2026-06-22-react-memory-strict-design.md`

---

## 七、蒸馏流程（autoDream，v2 升级）

> **v2 重要变更**：
> - 锁文件 = mtime（参考 Claude Code `consolidationLock.ts`）
> - dry_run 从 yes/no 改为 **diff/merge review**（用户看到候选文件，编辑后接受）
> - 输出从"单 MEMORY.md 重写"改为"per-file 候选 + 索引更新"

### 7.1 触发条件（四重门 + 锁 v2 升级）

```python
class AutoDreamScheduler:
    """
    v2 蒸馏调度器
    
    四重门（cheap → expensive）:
    - 门0: feature gate
    - 门1: 时间门（24h）
    - 门2: 扫描节流（10 分钟）
    - 门3: session 数量门（≥5）
    - 门4: 锁（PID + mtime 双重保护）
    
    v1 升级点: 锁文件合并了"上次蒸馏时间"和"当前锁状态"
    """
    
    LOCK_FILE = ".agent_data/memory/.consolidate-lock"
    HOLDER_STALE_MS = 60 * 60 * 1000  # 锁超过 1h 视为持有者已死
    
    def should_distill(self) -> tuple[bool, str]:
        # 门0: feature gate
        if not self.feature_enabled("auto_dream"):
            return False, "gate_disabled"

        # 门1+2+4: 锁文件同时承担"上次时间"和"当前锁状态"两个职责
        lock_state = self._check_lock()
        if lock_state.busy:
            return False, f"locked_by_{lock_state.holder_pid}"

        # 门1: 时间门（从 lock mtime 读, 不用单独文件）
        # v2.1: mtime 是"上次成功蒸馏时间",失败 runs 不更新 mtime (见 _acquire_lock)
        if lock_state.age_ms < 24 * 3600 * 1000:
            return False, f"too_soon({lock_state.age_ms / 3600000:.1f}h)"

        # 门2: 扫描节流（避免反复 listdir）
        if self._time_since_last_scan() < 10 * 60:
            return False, "scan_throttled"

        # 门3: session 数量门
        session_count = self._count_recent_sessions()
        if session_count < 5:
            return False, f"too_few_sessions({session_count})"

        return True, "ok"

    def _check_lock(self) -> LockState:
        """
        检查锁状态. 锁文件 mtime 同时是"上次蒸馏时间".

        v1 的 .distill_lock + .last_distill 两个文件 → v2 合并为一个.
        """
        if not self.LOCK_FILE.exists():
            return LockState(busy=False, age_ms=0, holder_pid=None)

        mtime_ms = self.LOCK_FILE.stat().st_mtime * 1000
        age_ms = time.time() * 1000 - mtime_ms
        envelope = self._read_lock_envelope()  # v2.1: JSON 包装,见 _acquire_lock

        # 双重保护: mtime 超过 1h OR PID 已死 → 锁可被强占
        if age_ms > self.HOLDER_STALE_MS or not self._pid_alive(envelope.pid):
            return LockState(busy=False, age_ms=age_ms, holder_pid=None)

        return LockState(busy=True, age_ms=age_ms, holder_pid=envelope.pid)

    def _acquire_lock(self) -> int:
        """
        获取锁, 返回 prior mtime（用于失败回滚）.

        v2.1 关键改动（A1: 消除 TOCTOU 竞争; A2: 失败回滚 mtime）:
        1. 用 os.open(O_CREAT|O_EXCL) 内核原子创建,失败抛 FileExistsError
        2. 锁内容从裸 PID 字符串 → JSON envelope (A11)
        3. 失败路径用 prior_mtime 回滚,不让失败 run 推进 24h 门
        """
        prior_mtime = self.LOCK_FILE.stat().st_mtime if self.LOCK_FILE.exists() else 0

        envelope = {
            "pid": os.getpid(),
            "host": socket.gethostname(),
            "started_at": time.time(),
            "schema_version": 1,
        }

        # 原子创建: O_CREAT|O_EXCL 保证只有一个进程能创建
        # Linux/macOS 都支持; Windows 用 msvcrt 或 fcntl 替代
        try:
            fd = os.open(
                self.LOCK_FILE,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                0o600,
            )
            with os.fdopen(fd, "w") as f:
                f.write(json.dumps(envelope))
        except FileExistsError:
            # 别的进程持有锁（或上次崩溃未清理）→ 直接返回失败
            return 0

        # 加 fcntl flock 做"持有期间互斥", 防 stale lock 期间别的进程强占
        # （HOLDER_STALE_MS 之外的窗口）
        self._flock_fd = self._open_flock()  # 进程退出时由 atexit 释放
        return int(prior_mtime * 1000) if prior_mtime else 0

    def _release_lock(self, prior_mtime_ms: int) -> None:
        """
        释放锁。

        v2.1 关键改动（A2: 失败回滚 mtime）:
        - 成功:保留 mtime（自动成为"上次成功蒸馏时间"）
        - 失败:回滚到 prior_mtime,不让失败 run 推进 24h 门
        - 无论成败:删除锁文件 + 释放 flock
        """
        if self._last_distill_failed:
            # 失败:回滚 mtime
            if prior_mtime_ms > 0:
                os.utime(self.LOCK_FILE, ns=(prior_mtime_ms, prior_mtime_ms))
        # 删除锁文件（成功的 mtime 已被 utime 保留——但 utime 只对存在的文件生效）
        # 真正"保留 mtime"的做法:不删除文件,只清空内容/或保留
        # 这里采取折中: 删除锁,失败时把 prior_mtime 写到 last_distill_at
        if self._last_distill_failed and prior_mtime_ms > 0:
            self._write_last_distill_at(prior_mtime_ms / 1000)
        os.close(self._flock_fd)
        self.LOCK_FILE.unlink(missing_ok=True)

    def _read_lock_envelope(self) -> LockEnvelope:
        """
        读取锁 JSON 包装（A11: 替代裸 PID 字符串）.
        读到垃圾 / 格式错时返回空 envelope,触发"锁可被强占"路径.
        """
        try:
            data = json.loads(self.LOCK_FILE.read_text())
            return LockEnvelope.model_validate(data)  # Pydantic 校验
        except (json.JSONDecodeError, ValidationError, FileNotFoundError):
            return LockEnvelope.empty()  # pid=0 → _pid_alive(0)=False → 锁可被强占
```

### 7.2 蒸馏流程图（v2）

```
DistillationScheduler（每小时检查一次）
        ↓
    should_distill() == True ?
        ↓ 是
┌─────────────────────────────────┐
│  1. 获取锁 (.consolidate-lock)  │
│     mtime = now（自动成为"上次时间"）│
└────────────┬────────────────────┘
             ↓
┌─────────────────────────────────┐
│  2. 扫描增量 session              │
│     列出 last_distill 之后改动的 │
│     session 文件                  │
└────────────┬────────────────────┘
             ↓
┌─────────────────────────────────┐
│  3. 读取每条记忆文件              │
│     按 type 分组 (user/feedback/ │
│     project/reference)           │
└────────────┬────────────────────┘
             ↓
┌─────────────────────────────────┐
│  4. LLM 蒸馏（fork agent）       │
│     - 合并相似记忆                │
│     - 调和冲突信息                │
│     - 删除过时记忆                │
│     - 生成 per-file 候选         │
└────────────┬────────────────────┘
             ↓
┌─────────────────────────────────┐
│  5. 写入 candidate 目录          │
│     .agent_data/memory/_candidate/│
│     ├── user/                    │
│     │   └── 2026-06-19_learning_v2.md│
│     └── feedback/                │
│         └── 2026-06-19_no_mock_v2.md│
└────────────┬────────────────────┘
             ↓
┌─────────────────────────────────┐
│  6. UI 展示 diff/merge review    │
│     (🆕 P0-19: 不是 yes/no,     │
│      用户看 diff 后点"接受")     │
└────────────┬────────────────────┘
             ↓ 用户接受
┌─────────────────────────────────┐
│  7. 原子替换                      │
│     旧文件移入 _archive/         │
│     candidate 移到正式目录       │
│     重建 MEMORY.md 索引           │
└────────────┬────────────────────┘
             ↓
┌─────────────────────────────────┐
│  8. 释放锁（保留 mtime）          │
│     mtime 自动 = now             │
│     下次检查门1 时距今 0h         │
└─────────────────────────────────┘
```

### 7.3 蒸馏 Prompt（v2：per-file 输出）

```
从以下对话日志和现有记忆中, 蒸馏出值得保留的长期记忆.

输出: 每个候选记忆一个 markdown 文件, 含 YAML frontmatter.

frontmatter 字段:
- type: 必须是 user | feedback | project | reference 之一
- created_at: YYYY-MM-DD
- confidence: 0.0-1.0
- sources: [session_id_1, session_id_2, ...]

body 格式:
# <标题>

**Why:** <这条记忆为什么重要, 用户/项目背景>

## 内容
<具体记忆内容>

蒸馏规则:
1. 合并相似记忆 (例: "先手写 ReAct" 和 "重视底层原理" → 合并)
2. 调和冲突信息 (例: 用户改主意了 → 更新原记忆, 不新增)
3. 删除过时记忆 (例: "Python 2 vs 3" 的 Python 2 相关 → 删)
4. 强制: feedback / project 类必须含 **Why:** 字段

现有记忆（不要重复提取）:
<读取 .agent_data/memory/ 下的所有 .md 文件>

增量日志:
<读取 .agent_data/logs/ 中 last_distill 之后的文件>
```

### 7.4 dry_run → diff/merge review（P0-19）

**v1 问题**：dry_run=True → 返回"建议" → 用户 yes/no → 写 MEMORY.md。

问题：
- 用户看到的是"建议摘要"，不是真实文件 diff
- yes/no 粒度太粗（要么全接受要么全拒绝）
- 不能部分接受

**v2 改进**：

```python
class DistillationReview:
    """
    v2 蒸馏评审：候选文件 + diff review
    
    用户体验:
    - 在 UI 里看到 _candidate/ 下的候选文件
    - 每个文件可以: 接受 / 编辑后接受 / 拒绝 / 跳过
    - 接受后原子替换旧文件
    """
    
    def render_review_ui(self) -> ReviewUI:
        candidates = self._candidate_dir.glob("**/*.md")
        return ReviewUI(
            candidates=[
                CandidateFile(
                    path=c,
                    diff=compute_diff(self._find_existing(c.name), c),
                    metadata=parse_frontmatter(c),
                )
                for c in candidates
            ],
            actions=["accept", "edit", "reject", "skip"],
        )
    
    def apply_review(self, decisions: list[Decision]) -> None:
        """
        应用用户决策
        
        每条决策:
        - accept: candidate 移入正式目录, 旧的入 _archive/
        - edit: 用用户编辑后的内容覆盖 candidate, 再 accept
        - reject: candidate 删除, 旧文件保留
        - skip: candidate 保留在 _candidate/, 下次蒸馏再处理
        """
        for d in decisions:
            if d.action == "accept":
                self._atomic_replace(d.candidate)
            elif d.action == "edit":
                d.candidate.write_text(d.edited_content)
                self._atomic_replace(d.candidate)
            elif d.action == "reject":
                d.candidate.unlink()
            elif d.action == "skip":
                pass  # 保留
        # 重建 MEMORY.md 索引
        self._rebuild_index()
```

> **解释**：参考 Claude Code 的 `/dream` 命令设计——它会展示 candidate file 的内容（不是摘要），用户可以编辑后再接受。这与 git PR review 的体验一致：**用户审查的是真实的代码 diff，不是别人转述**。

### 7.5 锁文件合并（v2 优化）

**v1 的两个文件**：

```
.agent_data/
├── .distill_lock      # 当前锁状态
└── .last_distill      # 上次蒸馏时间
```

**v2 的一个文件**：

```
.agent_data/memory/
└── .consolidate-lock  # mtime = 上次时间, 内容 = PID = 当前锁状态
```

**收益**：
- 少一次 IO（mtime 是 stat 自带，不用单独读文件）
- 原子性更强（不会出现"mtime 已更新但 PID 未写"的中间态）
- 减少用户认知负担（少一个隐藏文件）

---

## 八、文件结构总览（v2）

```
agent-dev/
├── agent_core/
│   ├── memory/
│   │   ├── __init__.py          ← 包入口（导出所有组件）
│   │   ├── daily.py             ← DailyLogger（日常日志，append-only）
│   │   ├── memory_store.py      ← MemoryStore（向量索引 + 语义搜索 + 时间衰减）
│   │   ├── extractor.py         ← MemoryExtractor（双通道：内联 + 后台）
│   │   ├── sm_layer.py          ← 🆕 SessionMemoryLayer（L3 会话内压缩）
│   │   ├── memory_editor.py     ← 🆕 MemoryFileEditor（Edit-only 工具沙箱）
│   │   ├── distiller.py         ← MemoryDistiller（autoDream，diff/merge review）
│   │   ├── scheduler.py         ← DistillationScheduler（四重门 + 合并锁）
│   │   ├── config.py            ← 🆕 MemoryConfig（阈值、开关、token 系数等）
│   │   └── types.py             ← 🆕 MemoryType 封闭类型定义
│   │
│   └── langgraph_agent/
│       └── agent.py              ← 集成记忆系统（检索 + 双通道写入 + L3 压缩）
│
├── .agent_data/
│   ├── logs/                    ← 日常日志（append-only，永不覆写）
│   │   ├── 2026-06-19.md
│   │   └── 2026-06-18.md
│   ├── chroma/                  ← Chroma 向量数据库
│   ├── memory.db                ← SQLite 元数据索引
│   ├── memory/                  ← 🆕 v2: per-file 长期记忆
│   │   ├── MEMORY.md            ← 索引（轻量, ~5KB）
│   │   ├── user/                ← 4 类封闭分类
│   │   ├── feedback/
│   │   ├── project/
│   │   ├── reference/
│   │   ├── _candidate/          ← 蒸馏候选（待 review）
│   │   ├── _archive/            ← 蒸馏归档（被替换的旧文件）
│   │   └── .consolidate-lock    ← 锁文件（mtime = 上次蒸馏时间）
│   ├── sessions/                ← L3 SM 文件（每会话一个）
│   │   └── thread_xxx/
│   │       └── sm.md
│   └── .agent_config.json       ← 🆕 记忆系统配置（开关、阈值）
│
└── web/
    └── app_langgraph.py         ← UI：记忆管理按钮 + autoDream 状态 + 候选 review
```

---

## 九、依赖清单（v2）

```txt
# 核心依赖（必须）
chromadb>=0.4.0
sentence-transformers>=2.2.0
pydantic>=2.0             ← 🆕¹ v2.1: 配置校验 (§12.5)
pyyaml>=6.0                ← 🆕 v2: frontmatter 解析

# 可选依赖（按 retrieval.mode 选择）
# mode=vector / hybrid 路径需要:
#   - 默认本地: sentence-transformers (已装)
#   - 推荐:     BAAI/bge-m3 (多语言, 见 §九.1)

# 已有依赖（无需额外安装）
sqlite3         ← Python 内置
threading       ← Python 内置
fcntl           ← Python 内置 (Unix 跨进程锁, §4.1 A4)
json            ← Python 内置
pathlib         ← Python 3.4+
typing          ← Python 3.8+ (需要 Literal / Final 类型)
opentelemetry-api>=1.20   ← 🆕¹ v2.1: §13.6 可观测性
```

安装命令：
```bash
pip install chromadb sentence-transformers pydantic pyyaml opentelemetry-api
```

### 九.1 嵌入模型选型（v2.1 增）—— L2 修复

**v1 问题**:默认 `all-MiniLM-L6-v2` (384维, 仅英文),中文记忆"学习风格"嵌入质量差;无多语言备选;无模型升级迁移路径。

**v2.1 推荐**:`BAAI/bge-m3` (568维, 多语言, 2024 SOTA)

| 候选 | 维度 | 多语言 | 中文质量 | 体积 | 适用 |
| --- | --- | --- | --- | --- | --- |
| `all-MiniLM-L6-v2` (v1 默认) | 384 | ❌ 仅英文 | ⭐⭐ 差 | ~80MB | 纯英文场景 |
| `BAAI/bge-m3` (v2.1 推荐) | 568 | ✅ 100+ 语言 | ⭐⭐⭐⭐⭐ | ~2.3GB | **多语言默认** |
| `text-embedding-3-small` (OpenAI API) | 1536 | ✅ | ⭐⭐⭐⭐ | 0 (API) | 不在意 outbound 成本/隐私 |
| `BAAI/bge-small-zh-v1.5` | 512 | ✅ 中英 | ⭐⭐⭐⭐⭐ (中文专项) | ~95MB | 纯中文 |

**配置项** (`§12.5 Pydantic`):
```python
class VectorConfig(BaseModel):
    embedding_model: str = "BAAI/bge-m3"
    embedding_dim: int = 568
    embedding_model_version: str = "1.0"  # 用于检测 chroma 内的旧向量
```

**模型升级迁移**:
- 改 `embedding_model` 后启动时跑 `re_embed.py --old-model X --new-model Y`
- 一次性任务:从 `chroma/` 读所有 id → 用旧模型 embed 比对确认无变化 → 用新模型 embed → 批量 update
- `embedding_model_version` 写入每条 vector 的 metadata,查询时检测不一致就 warn

**为什么 v2.1 默认 bge-m3**:
- agent-dev 用户群中文占比高 (项目元数据/记忆内容多中文)
- 多语言不锁死未来,加日韩俄不用换模型
- 本地推理,无 outbound 成本,符合 §十四.1 路径安全

**对应 issue**:L2 审查指出"v1 嵌入模型仅英文 + 无迁移路径",本节给答案。

---



## 十、实施计划

> **本章已迁移** → 完整实施计划(8 天 AI+人协作、6 个验收点、风险、交付清单)在独立文件:
> 
> 📄 **[`docs/IMPLEMENTATION_PLAN.md`](IMPLEMENTATION_PLAN.md)**
> 
> **理由**:plan 性质(时间表 / 验收点 / 任务切片)不属于设计文档,放在独立文件便于:
> - plan 频繁更新(每次迭代调整)不影响 design doc
> - 实施时单文件打印给 AI Agent 上下文,不被 design doc 3764 行稀释
> - 8 场景并发矩阵(原 §10.1)作为**设计不变量**已重定位到 §4.5.1,仍在 design doc
> 
> **本节保留的 cross-reference**:
> - §4.5.1 8 场景并发矩阵(测试不变量,设计层面)
> - §16 引用关系(提到本文件)

---

## 十一、待办与决策记录（v2）

### 11.1 已完成（v2 设计阶段）

- [x] 整体架构升级（L3 + 双通道 + 封闭分类 + per-file）
- [x] §三 提取频率阈值化（token/tool + LLM 评分）
- [x] §四 写入与压缩方案（双通道 + Edit-only + extract/compact 分层）
- [x] §五 长期记忆 per-file 重构
- [x] §七 autoDream 升级（合并锁 + diff/merge review）
- [x] §十二 配置与开关设计
- [x] §十三 UI 与可观测性设计
- [x] §十四 安全模型（路径 + 工具沙箱）

### 11.2 待实现（v2 编码阶段）

按 §十 实施计划，按优先级 P0 → P1 → P2 推进。

### 11.3 决策记录

> **职责定位**：本节是"决策索引表"——用最短篇幅回答"我们做了什么选择"。每个决策的**详细论证（备选方案 / 取舍过程）**见 §十五对应小节,本节不重复展开。
> 
> **v2.1 决策更新**：检索模式从 v1 强制 Chroma → v2.1 三模式共存,详见 §15.5。

| 决策 | 备选 | 选择 | 理由（简） | 详细论证 |
| --- | --- | --- | --- | --- |
| 分类法 | 开放 vs 封闭 | **封闭 4 类** | LLM 不能发明新类别，编译期约束 | §五.3.1 |
| Layer 3 存储 | 单文件 vs per-file | **per-file + 索引** | 单文件触顶会丢信息；per-file 利于 git/Edit | §15.1 |
| 写入路径 | 单通道 vs 双通道 | **双通道** | 内联写实时，后台异步完整 | §四.1 |
| 压缩路径 | LLM 一次 vs 分层 | **extract（LLM）+ compact（零 LLM）** | 避免每次压缩重摘要 | §15.3 |
| 工具 | JSON 解析 vs Edit | **Edit-only** | schema 不漂移，工具层强制 | §四.2 |
| Review | dry_run yes/no（v1） | **diff/merge review**（v2） | 用户审查真实文件，不是转述；支持部分接受 | §七.4 |
| 锁文件 | 两文件 vs 合并 | **合并（mtime = 时间）** | 少一次 IO，原子性强 | §七.3 |
| 检索模式（v2.1） | 强制 Chroma vs 三模式 | **vector / file / hybrid 三模式可切换** | 不同规模/场景下最优模式不同 | §6.6, §15.5 |
| 触发判断 | 纯关键词 vs 纯 LLM | **阈值 + LLM 评分** | 关键词漏报 40%，LLM 评分漏报 < 5% | §15.4 |
| 配置校验（v2.1） | 裸 dict vs 强类型 | **Pydantic BaseModel** | 拼错 key 启动即崩, fail-fast | §12.5 |
| 后台任务（v2.1） | daemon thread vs pool | **ThreadPoolExecutor + atexit** | daemon 进程退出不保证 cleanup | §四.1 |

---

## 十二、配置与开关（v2）

### 12.1 配置文件：`.agent_data/.agent_config.json`

```json
{
  "memory": {
    "enabled": true,
    "auto_extract": {
      "enabled": true,
      "min_tokens_to_init": 10000,
      "min_tokens_between_updates": 5000,
      "tool_calls_between_updates": 10,
      "llm_score_threshold": 0.6
    },
    "session_memory": {
      "enabled": true,
      "sm_compact_enabled": true,
      "max_tokens_per_section": 8000,
      "extraction_wait_timeout_ms": 15000
    },
    "auto_dream": {
      "enabled": true,
      "min_hours": 24,
      "min_sessions": 5,
      "scan_throttle_minutes": 10,
      "lock_stale_ms": 3600000
    },
    "retrieval": {
      "top_k": 3,
      "semantic_weight": 0.6,
      "time_weight": 0.3,
      "access_weight": 0.1,
      "time_decay_days": 30
    },
    "security": {
      "memory_dir_permissions": "0700",
      "memory_file_permissions": "0600",
      "enable_path_validation": true
    }
  }
}
```

### 12.2 开关优先级

参考 Claude Code 的三层开关设计：

| 层级 | 开关 | 默认 | 控制方 |
| --- | --- | --- | --- |
| 外层 | `memory.enabled` | **true** | 配置文件 |
| 中层 | `auto_extract.enabled` | true | 配置文件 |
| 内层 | `session_memory.enabled` | true | 配置文件 |
| 蒸馏 | `auto_dream.enabled` | true | 配置文件 |
| **总开关**（紧急关） | `DISABLE_AUTO_MEMORY=1` env var | - | 环境变量 |

### 12.3 运行时开关（无需重启）

```python
# 启用/禁用 L3 会话内压缩
config.set("memory.session_memory.enabled", False)

# 手动触发蒸馏（跳过 24h 门）
distiller.force_run(reason="manual_trigger")

# 调整阈值
config.set("memory.auto_extract.min_tokens_to_init", 5000)
```

### 12.4 与 Claude Code 设置的对照

| Claude Code 设置 | agent-dev v2 对应 | 位置 |
| --- | --- | --- |
| `autoCompactEnabled` | `memory.auto_extract.enabled` | `.agent_config.json` |
| `tengu_session_memory` | `memory.session_memory.enabled` | `.agent_config.json` |
| `tengu_sm_compact` | `memory.session_memory.sm_compact_enabled` | `.agent_config.json` |
| `autoDreamEnabled` | `memory.auto_dream.enabled` | `.agent_config.json` |
| `ENABLE_CLAUDE_CODE_SM_COMPACT=1` | `memory.session_memory.sm_compact_enabled=true` | 配置或 env |
| `DISABLE_AUTO_COMPACT=1` | `memory.enabled=false` | 配置或 env |

### 12.5 配置校验：Pydantic 强类型（v2.1）

**v1 问题**：纯 `dict` 配置, key 拼错（如 `min_tokns_to_init`）运行时才崩溃, 静默回退默认值掩盖问题。

**v2.1 方案**：用 Pydantic v2 BaseModel 做 schema 校验, 启动时 fail-fast:

```python
from pydantic import BaseModel, Field, field_validator
from typing import Literal

class AutoExtractConfig(BaseModel):
    enabled: bool = True
    min_tokens_to_init: int = Field(10000, ge=1000, le=100000)
    min_tokens_between_updates: int = Field(5000, ge=500)
    tool_calls_between_updates: int = Field(10, ge=1, le=100)
    llm_score_threshold: float = Field(0.6, ge=0.0, le=1.0)

class RetrievalConfig(BaseModel):
    mode: Literal["vector", "file", "hybrid"] = "hybrid"
    top_k: int = Field(3, ge=1, le=20)
    vector_top_k: int = Field(20, ge=5, le=100)
    file_top_k: int = Field(5, ge=1, le=20)
    semantic_weight: float = Field(0.6, ge=0.0, le=1.0)
    time_weight: float = Field(0.3, ge=0.0, le=1.0)
    access_weight: float = Field(0.1, ge=0.0, le=1.0)
    time_decay_days: int = Field(30, ge=1, le=365)
    
    @field_validator("semantic_weight", "time_weight", "access_weight")
    @classmethod
    def weights_sum_to_one(cls, v, info):
        # 简化校验: 三个 weight 加起来 = 1.0 (允许 ±0.01 浮点误差)
        # 完整校验在 load() 后做, 见下
        return v

class MemoryConfig(BaseModel):
    enabled: bool = True
    auto_extract: AutoExtractConfig = AutoExtractConfig()
    retrieval: RetrievalConfig = RetrievalConfig()
    # ... 其他子配置

def load_config(path: str) -> MemoryConfig:
    """启动时校验 + 加载"""
    raw = json.loads(Path(path).read_text())
    try:
        config = MemoryConfig.model_validate(raw["memory"])
    except ValidationError as e:
        # fail-fast: 启动时直接抛, 不让无效配置进生产
        raise ConfigError(f"Invalid config at {path}:\n{e}") from e
    
    # 跨字段校验 (Pydantic 单独写)
    r = config.retrieval
    total = r.semantic_weight + r.time_weight + r.access_weight
    if not (0.99 <= total <= 1.01):
        raise ConfigError(
            f"retrieval weights must sum to 1.0, got {total}"
        )
    return config
```

**收益**：
- **fail-fast**：拼错 key 启动即崩, 不掩盖问题
- **范围校验**：`min_tokens_to_init: -1` 直接拒收
- **类型安全**：`mode: "hybird"` 拼错拒绝, 不会静默回退到 default
- **自文档**：`BaseModel` 字段即文档, IDE 自动补全

**依赖**：`pydantic>=2.0`（已是 FastAPI 同款，团队熟悉）

---

## 十三、UI 与可观测性（v2）

### 13.1 状态面板

参考 Claude Code `MemorySettings` 组件设计：

```
┌──────────────────────────────────────────────────┐
│ Memory                                            │
├──────────────────────────────────────────────────┤
│ User memory: ~/.claude/user_memory/  (12 条)     │
│ Project memory: .agent_data/memory/  (5 条)      │
│ ▼ Per category:                                   │
│   user:      2 条                                │
│   feedback:  1 条                                │
│   project:   1 条                                │
│   reference: 1 条                                │
│ ▼ Current query (v2.1 增):                       │
│   Search: 3 relevant / 47 total                  │  ← L12 修复
│   Injected: 450 tokens (budget 2000)             │  ← L4 + L8 修复
│   Last 0-hit: 3 turns ago                        │  ← L12 修复
│ ▼ Recent activity:                                │
│   Last extract: 2 hours ago                      │
│   Last dream:  never                             │
│   [Run auto-dream now]                           │
│ ▶ Pending candidates (2):                         │
│   - user/2026-06-19_learning_v2.md               │
│   - feedback/2026-06-19_no_mock_v2.md            │
│   [Review candidates]                            │
└──────────────────────────────────────────────────┘
```

**v2.1 新增 3 行**(L12 修复 — 检索可观测性):
- `Search: N relevant / M total` — 每次 turn 实时显示,用户知道 memory 系统被调了
- `Injected: N tokens (budget M)` — 注入量可见,超 budget 会有 ⚠️ 标记
- `Last 0-hit: N turns ago` — 长期 0 命中提示"记忆系统可能没工作"

### 13.2 三个核心开关

```python
# UI 层 React-ish 伪代码
autoMemorySwitch = useState(config.memory.enabled)
sessionMemorySwitch = useState(config.memory.session_memory.enabled)
autoDreamSwitch = useState(config.memory.auto_dream.enabled)

# 互斥: 关掉 auto → dream 行隐藏
showDreamRow = autoMemorySwitch.value

# 每次切换立即写回配置
function toggleMemory(key, value):
    config.set(key, value)
    config.save()  # 立即落盘
    logEvent(f"tengu_memory_{key}_toggled", {enabled: value})
```

### 13.3 autoDream 状态行

```python
# 三态: running / never / last ran X ago
dreamStatus = (
    "running" if isDreamRunning()
    else "never" if lastDreamAt is None
    else f"last ran {formatRelative(lastDreamAt)}"
)
# 呈现: "Auto-dream: on · last ran 3 hours ago · /dream to run"
```

### 13.4 候选文件 review UI

蒸馏完成后在 `_candidate/` 目录生成候选文件，UI 展示：

```
┌──────────────────────────────────────────────────┐
│ Distillation candidates (2)                      │
├──────────────────────────────────────────────────┤
│ ┌─ user/2026-06-19_learning_v2.md ────────────┐  │
│ │ type: user                                  │  │
│ │ confidence: 0.95                            │  │
│ │ sources: [thread_abc, thread_def]           │  │
│ │ ---                                         │  │
│ │ # 学习风格                                   │  │
│ │ ## 内容                                     │  │
│ │ - 先手写原生 ReAct 理解本质                  │  │
│ │ - 重视底层原理...                           │  │
│ │                                           │  │
│ │ [Accept] [Edit] [Reject] [Skip]            │  │
│ └────────────────────────────────────────────┘  │
│                                                  │
│ ┌─ feedback/2026-06-19_no_mock_v2.md ─────────┐  │
│ │ ...                                          │  │
│ └────────────────────────────────────────────┘  │
└──────────────────────────────────────────────────┘
```

### 13.5 监控指标

```python
@dataclass
class MemoryMetrics:
    """通过日志事件收集"""
    extract_total: int
    extract_failures: int
    sm_compact_hits: int          # SM-compact 路径触发次数
    sm_compact_fallbacks: int     # 回退到传统 compact 的次数
    dream_runs: int
    dream_candidates_accepted: int
    memory_file_count: int
    avg_memory_age_days: float
```

> **解释**：参考 Claude Code 的 `tengu_*` 事件命名规范——`tengu_memory_extraction`, `tengu_sm_compact_flag_check`, `tengu_memory_toggled`。统一的 telemetry 命名让后期接 Grafana / Datadog 时直接 map。

### 13.6 可观测性 SLO（v2.1 增）—— A8

§13.5 列了指标但**没回答三个问题**:发到哪?SLO 是多少?告警阈值是什么?这正是 A8 审查指出的缺口。

**1. 发射目标**: OpenTelemetry spans,统一 namespace `memory.*`:

```python
from opentelemetry import trace
tracer = trace.get_tracer("agent.memory")

# Span 命名规范
with tracer.start_as_current_span("memory.extract") as span:
    span.set_attribute("memory.session_id", session_id)
    span.set_attribute("memory.trigger", "token_threshold")  # 或 "tool_count" / "manual"
    span.set_attribute("memory.input_tokens", input_tokens)
    span.set_attribute("memory.candidates_extracted", len(extraction))
    # ... 业务逻辑
```

| Span 名 | 父 span | 关键 attributes |
| --- | --- | --- |
| `memory.search` | `agent.run` | `query_len`, `top_k`, `result_count`, `latency_ms`, `cache_read_tokens` |
| `memory.extract` | `agent.run` | `trigger`, `input_tokens`, `candidates_extracted`, `latency_ms` |
| `memory.dream` | standalone | `candidates_count`, `accepted_count`, `latency_ms` |
| `memory.sm_compact` | `agent.run` | `section`, `tokens_before`, `tokens_after`, `fell_back_to_traditional` |

**2. SLO 定义**:

| SLO | 目标 | 测量方法 |
| --- | --- | --- |
| `memory.search.p95_latency` | < 600ms (hybrid) / < 100ms (vector) | histogram_quantile(0.95) |
| `memory.extract.p95_latency` | < 5s | histogram_quantile(0.95) |
| `memory.extract.success_rate` | > 95% | `extract_committed / extract_started` |
| `memory.sm_compact.fallback_rate` | < 5% | `sm_compact_fallbacks / sm_compact_attempts` |
| `memory.dream.candidate_acceptance_rate` | > 30% | `accepted / candidates` |
| `memory.hallucination_rejection_rate` | < 10% | (L7 验证 pass 失败率, 见 §4.2) |

**3. 告警规则** (PromQL 风格):

```yaml
# extract 失败率高
- alert: MemoryExtractFailureHigh
  expr: |
    rate(memory_extract_failures_total[1h])
    / rate(memory_extract_total[1h]) > 0.10
  for: 15m
  severity: warning

# search 延迟劣化
- alert: MemorySearchSlow
  expr: |
    histogram_quantile(0.95, rate(memory_search_latency_ms_bucket[5m])) > 1000
  for: 10m
  severity: warning

# 蒸馏卡死 (超过 30 分钟没完成)
- alert: MemoryDreamStuck
  expr: |
    time() - max(memory_dream_started_at_timestamp) > 1800
  severity: critical
```

**4. 导出目标** (按部署环境):
- 开发: stdout JSON logs + local OTel collector
- 生产: OTLP → Grafana Cloud / Datadog / 自己的 Tempo+Jstack
- 离线/单机: 只写 `~/.agent_data/memory_metrics.jsonl` (类似 Claude Code 的 PostHog 模式)

**5. 与 §13.5 MemoryMetrics 的关系**:
- `MemoryMetrics` 是"业务指标"（per-process 计数器,落盘）
- 本节 OTel spans 是"链路追踪"（跨进程,跨服务）
- 两者互补:MemoryMetrics 回答"多少",OTel 回答"为什么慢 / 哪一步失败"

### 13.7 成本预算（v2.1 增）—— L10 修复

**问题**:§6.6.3.1 列了"per-call ~$0.001" 但**没有规模外推**。1K sessions/天 × 50 turns × L1 合并后的 LLM 调用 = 多少?没算过就上线,月底账单会惊到。

**v2.1 成本模型**(假设:每 session 50 turns,平均 5 turns 触发 1 次提取):

| 阶段 | 单次成本 (Haiku) | 调用频率 | 100 sessions/天 | 1K sessions/天 | 10K sessions/天 |
| --- | --- | --- | --- | --- | --- |
| **检索 side-query** (mode=hybrid) | $0.0003 (~300 in + 80 out) | 每 turn 1 次 = 50/session | $1.50 | $15 | $150 |
| **评分+提取合并** (L1 修复后) | $0.0005 (~500 in + 120 out) | 每 5 turns 1 次 = 10/session | $0.50 | $5 | $50 |
| **二次校验** (L7 修复) | $0.0004 (~400 in + 100 out) | 每次提取 1 次 = 10/session | $0.40 | $4 | $40 |
| **蒸馏** (autoDream) | $0.05 (跨 session 整合) | 每 24h 1 次/机器 | $1.50 | $1.50 | $1.50 |
| **幻觉验证** (L7 失败重试) | $0.0004 | 假设 5% 触发 | $0.10 | $1 | $10 |
| **小计** | | | **$4.00/天** | **$26.50/天** | **$251.50/天** |
| **月度** | | | **$120/月** | **$795/月** | **$7,545/月** |

**关键优化点**:
- L1 合并前 (2 次 LLM/提取): 1K sessions/天 → $35/天 (vs 现在 $26.5/天, **省 24%**)
- L3 cache 命中 80%: 1K sessions/天 → $13/天 (**省 51%**)
- 两者叠加: 1K sessions/天 → **$9/天** = $270/月

**预算红线**:
- 单 session/天 成本 < $0.10 (10K 规模下)
- 单次提取成本 < $0.001 (硬约束,触发警告)

**配置项**:
```python
class CostConfig(BaseModel):
    daily_budget_usd: float = 50.0  # 超过就降级 mode=file
    per_extract_budget_usd: float = 0.001
    cost_tracking_enabled: bool = True
```

**对应 issue**:L10 审查指出"成本无规模外推",本节给完整模型 + 优化路径。

### 13.8 延迟预算（v2.1 增）—— L11 修复

**问题**:§6.6.3.1 列了"hybrid P95 ~520ms"但**没有全链路预算**——gate / extract / retrieve / inject 每段多少?extract 5s 怎么办?`extraction_wait_timeout_ms: 15000` 对交互式 agent 太大。

**v2.1 全链路延迟预算**:

| 阶段 | P50 | P95 | P99 | 失败策略 |
| --- | --- | --- | --- | --- |
| **gate** (token + keyword) | < 1ms | < 5ms | < 20ms | skip extract |
| **retrieve** (mode=hybrid) | 200ms | 600ms | 1.5s | 降级 mode=file |
| **inject** (build context) | 5ms | 20ms | 50ms | 截断到 budget |
| **extract.score_and_extract** (L1 合并) | 800ms | 2s | 5s | 丢弃本次提取,不阻塞 |
| **extract.verify** (L7 异步) | 400ms | 1s | 3s | background,不阻塞主对话 |
| **dream** (autoDream 后台) | 30s | 2min | 5min | 重试, 不影响用户 |
| **总: 用户感知 turn 延迟** | **+1s** | **+2.6s** | **+5s** | — |

**关键设计原则**:
- 主对话 turn **绝不被 extract 阻塞** — extract 跑在 executor 里 (§4.1)
- gate + retrieve + inject **必须 < 1s P95** — 用户在等
- dream **完全后台** — 用户永远不知道在跑

**`extraction_wait_timeout_ms: 15000` 误用的修正**:
- v1 时这个值用在主对话 turn 末尾 (等提取完成才能继续)
- v2 起,这个值只用于"等 channel A 内联写完成"(< 100ms 足够)
- 后台 channel B **不被等待**,跑完就行

**配置项**:
```python
class LatencyConfig(BaseModel):
    gate_p95_ms: int = 5
    retrieve_p95_ms: int = 600
    inject_p95_ms: int = 20
    extract_p95_ms: int = 2000       # 超就丢弃 (不阻塞)
    extraction_wait_timeout_ms: int = 100  # v2.1: 从 15000 改 100,见上
    dream_timeout_s: int = 300
```

**对应 issue**:L11 审查指出"无全链路延迟预算 + extraction_wait_timeout 太大",本节给完整 SLO + 修正。

---

## 十四、安全模型（v2）

### 14.1 路径安全多层防御

参考 Claude Code `paths.ts` 设计，agent-dev v2 采用 4 层防御：

| 层 | 位置 | 防御内容 |
| --- | --- | --- |
| L1 | `MemoryPathValidator.validate()` | 相对路径、根路径、null byte |
| L2 | `MemoryPathValidator.normalize()` | NFC 归一化（防 Unicode trick） |
| L3 | `MemoryPathValidator.realpath()` | 符号链接解析（防软链逃逸） |
| L4 | `MemoryPathValidator.is_within()` | realpath 双向对比 + separator 后缀 |

```python
class MemoryPathValidator:
    """4 层路径校验"""
    
    def validate(self, path: str, root: str) -> str:
        # L1: 字符串层
        # v2.1: Windows 路径可能以盘符开头 (D:\...) 视为绝对路径
        # 用 os.path.isabs() 跨平台判断, 比 path.startswith("/") 更可靠
        if os.path.isabs(path) or "\0" in path:
            raise PermissionError(f"absolute path or null byte: {path!r}")
        
        # L2: Unicode 归一化 + normpath (Windows 兼容)
        # normpath 解决反斜杠/正斜杠混用, ".." 段规范化等
        normalized = os.path.normpath(unicodedata.normalize("NFC", path))
        
        # L3: realpath 解析
        real = os.path.realpath(os.path.join(root, normalized))
        
        # L4: 双向对比 + separator 后缀
        # 跨平台: Windows 上 os.sep == "\\", 不能用 "/" 比较
        real_root = os.path.realpath(root) + os.sep
        if not real.startswith(real_root):
            raise PermissionError(
                f"path {path!r} resolves outside memory root {root!r}"
            )
        
        return real
```

### 14.2 工具沙箱

| 工具 | 限制 |
| --- | --- |
| `MemoryFileEditor` | 只允许 Edit，仅限 `memory_dir` 内的 `.md` 文件 |
| 主 agent 调用的记忆工具 | Read 全局，Write/Edit 仅 `memory_dir` |
| Bash（如果暴露） | 完全禁止 |

```python
class MemoryFileEditor:
    """工具白名单 + 路径白名单"""
    
    ALLOWED_TYPES = {"user", "feedback", "project", "reference"}
    
    def create_tool(self, memory_dir: str) -> dict:
        return {
            "name": "Edit",
            "description": (
                f"You may ONLY edit files under {memory_dir} matching *.md. "
                f"Each file's `type:` frontmatter must be one of "
                f"{self.ALLOWED_TYPES}. feedback/project types MUST include "
                f"a '**Why:**' section."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "pattern": f"^{re.escape(memory_dir)}/.*\\.md$",
                    },
                    "old_string": {"type": "string"},
                    "new_string": {"type": "string"},
                },
                "required": ["file_path", "old_string", "new_string"],
                "additionalProperties": False,
            },
        }
    
    def validate_edit(self, file_path: str, old_string: str, new_string: str):
        # 1. 路径校验（复用 MemoryPathValidator）
        real = self.path_validator.validate(file_path, self.memory_dir)

        # 2. 类型校验
        if "type:" in new_string:
            type_match = re.search(r"type:\s*(\S+)", new_string)
            if type_match and type_match.group(1) not in self.ALLOWED_TYPES:
                raise ValueError(f"invalid type: {type_match.group(1)}")

        # 3. Why 字段校验（feedback/project）
        if "type: feedback" in new_string or "type: project" in new_string:
            if "**Why:**" not in new_string:
                raise ValueError(
                    "feedback/project memories must include '**Why:**'"
                )

        # 4. v2.1 (L7): source_quote 必填 — 防止 LLM 幻觉
        # LLM 提取的记忆必须有原对话逐字引用, 否则丢弃
        if "type:" in new_string and "source_quote:" not in new_string:
            raise ValueError(
                "memory must include 'source_quote:' field with verbatim "
                "excerpt from the source conversation. Hallucinated memories "
                "without grounding are rejected."
            )

        # 5. v2.1 (L9): 输出 sanitizer — 防 prompt injection 投毒
        suspicious_patterns = [
            r"ignore\s+(previous|all|prior)\s+instructions?",
            r"system\s*:\s*",  # 试图塞 system message
            r"<\|.*?\|>",       # Anthropic 特殊 token
            r"</?\s*(system|assistant|user|tool)\s*>",  # 角色标签
        ]
        for pat in suspicious_patterns:
            if re.search(pat, new_string, re.IGNORECASE):
                raise ValueError(
                    f"suspicious pattern in memory (possible prompt injection): "
                    f"matched {pat!r}"
                )


# ---------- v2.1 (L7): 异步记忆验证 pass ----------
class MemoryVerifier:
    """
    L7 修复: 防止 LLM 幻觉污染长期记忆
    
    内联提取时虽然有 source_quote (validate_edit 强制), 但 source_quote
    本身是 LLM 自报的, 仍可能造假。增加异步二次校验:
    - 重新 prompt LLM: "source_quote 是否真的出现在原对话中?"
    - 不一致 → 降 confidence + 标 suspicious
    - 连续 3 次 suspicious → 丢弃
    """
    
    async def verify_memory(self, memory: dict, source_conversation: str) -> VerifyResult:
        # L9 修复: conversation 用 <conversation> delimiters 包起来
        # system 静态 (cache 友好, 见 §6.7)
        response = await self.router.chat(
            messages=[
                {"role": "system", "content": (
                    "You verify whether a memory candidate is supported by the source "
                    "conversation. Reply JSON only."
                )},
                {"role": "user", "content": f"""<conversation>
{source_conversation[:8000]}
</conversation>

<memory_candidate>
type: {memory['type']}
content: {memory['content']}
source_quote: {memory.get('source_quote', 'MISSING')}
</memory_candidate>

Verify: does source_quote appear verbatim (or near-verbatim) in <conversation>?
Is the memory content a faithful paraphrase of source_quote?

Output JSON:
{{
  "supported": true/false,
  "confidence": 0.0-1.0,
  "reason": "short explanation"
}}"""},
            ],
            provider="anthropic",
            model="claude-haiku-4-5",
            temperature=0.0,
            cache_safe_params=True,
            cache_namespace="memory_verify",  # §6.7 + §6.8 合约
        )
        try:
            data = json.loads(response.strip())
            return VerifyResult(
                supported=bool(data.get("supported", False)),
                confidence=float(data.get("confidence", 0.0)),
                reason=data.get("reason", ""),
            )
        except (json.JSONDecodeError, KeyError, ValueError):
            return VerifyResult(supported=False, confidence=0.0, reason="parse_error")
    
    async def verify_and_admit(self, memory: dict, source_conversation: str) -> bool:
        """L7 入口: 校验通过才 admit, 失败降 confidence + 计数"""
        result = await self.verify_memory(memory, source_conversation)
        if result.supported and result.confidence >= 0.7:
            # 信任, 写文件
            return True
        # 不信任: 降 confidence, 仍写但标 suspicious
        memory["confidence"] *= 0.5
        memory["suspicious_count"] = memory.get("suspicious_count", 0) + 1
        self._metrics.increment("memory.verify.rejected_total")
        # 连续 3 次可疑 → 真丢弃 (写时检查)
        if memory["suspicious_count"] >= 3:
            self._metrics.increment("memory.verify.discarded_total")
            return False
        return True
```

**对应 issue**:
- L7 修复:幻觉记忆加 source_quote 必填 + 异步二次校验
- L9 修复:prompt injection 防御 (delimiters + 输出 sanitizer + 角色标签拒收)

---

### 14.3 文件权限
```

### 14.3 文件权限

```python
import os
import stat

def setup_memory_permissions(memory_dir: str):
    """
    记忆目录权限: 0700 (仅 owner 可读写执行)
    记忆文件权限: 0600 (仅 owner 可读写)
    """
    # 目录
    os.chmod(memory_dir, 0o700)
    
    # 文件
    for md_file in pathlib.Path(memory_dir).rglob("*.md"):
        os.chmod(md_file, 0o600)
```

### 14.4 SecretScanner（可选 v2.1）

参考 Claude Code 的 `gitleaks` 规则集成——记忆文件不应该包含 API key / 密码 / token：

```python
class SecretScanner:
    """记忆写入前扫描敏感信息"""
    
    PATTERNS = [
        r"sk-[A-Za-z0-9]{20,}",           # OpenAI API key
        r"sk-ant-[A-Za-z0-9-]{20,}",      # Anthropic API key
        r"ghp_[A-Za-z0-9]{36,}",          # GitHub PAT
        r"xox[baprs]-[A-Za-z0-9-]{10,}",  # Slack token
    ]
    
    def scan(self, content: str) -> list[str]:
        findings = []
        for pattern in self.PATTERNS:
            matches = re.findall(pattern, content)
            if matches:
                findings.extend(matches)
        return findings
    
    def validate_or_redact(self, content: str) -> str:
        findings = self.scan(content)
        if findings:
            log.warning(f"secret detected in memory: {findings}")
            # 选项 A: 拒绝写入
            raise ValueError(f"memory contains secrets: {findings}")
            # 选项 B: 自动 redact
            # return redact_secrets(content)
        return content
```

### 14.5 Data Lifecycle（v2.1 增）—— 备份 / 损坏检测 / 容量治理

§14.1-14.4 覆盖了 chmod / 路径 / 工具 / secret,但**没回答"数据本身怎么存活"**——A6 审查指出三处缺口:无备份、无损坏检测、无容量上限。

```python
class MemoryDataLifecycle:
    """v2.1 新增: 备份 + 完整性 + 容量"""
    
    # ---------- 备份 ----------
    BACKUP_DIR = Path.home() / ".agent_data.backup"
    BACKUP_RETENTION_DAYS = 7
    BACKUP_HOUR = 3  # 凌晨 3 点跑 (用户大概率不在线)
    
    def daily_backup(self):
        """每天 03:00 跑一次: rsync 整个 .agent_data/ 到 BACKUP_DIR"""
        if not self._should_run_today():
            return
        timestamp = time.strftime("%Y%m%d")
        dest = self.BACKUP_DIR / timestamp
        subprocess.run([
            "rsync", "-a", "--delete",
            ".agent_data/",  # src
            f"{dest}/",      # dst
        ], check=True)
        # 清理 7 天前的备份
        self._prune_old_backups()
    
    def restore_from_backup(self, backup_date: str):
        """紧急恢复: rsync 反向 (备份 → 工作目录)"""
        src = self.BACKUP_DIR / backup_date
        if not src.exists():
            raise FileNotFoundError(f"backup {backup_date} not found")
        subprocess.run(["rsync", "-a", "--delete", f"{src}/", ".agent_data/"], check=True)
    
    # ---------- 完整性检测 ----------
    def startup_integrity_check(self):
        """启动时跑, 发现损坏立即停"""
        # SQLite 完整性
        result = self.meta_db.execute("PRAGMA integrity_check").fetchone()
        if result[0] != "ok":
            raise CorruptedMemoryError(
                f"SQLite corrupt: {result[0]}. Run: agent-memory restore --from-backup <date>"
            )
        
        # MEMORY.md 索引指向的文件都得存在
        for entry in self.parse_memory_md():
            path = self.memory_root / entry["rel_path"]
            if not path.exists():
                log.warning(f"orphan index entry: {entry['rel_path']}")
                # 不 raise, 只 warn + 自动清理 (rebuild index)
        
        # frontmatter 解析校验
        for md_file in self.memory_root.rglob("*.md"):
            try:
                self.parse_frontmatter(md_file)
            except (yaml.YAMLError, KeyError) as e:
                raise CorruptedMemoryError(f"bad frontmatter in {md_file}: {e}")
    
    # ---------- 容量治理 ----------
    MAX_FILES_PER_CATEGORY = 500   # 每类 (user/feedback/project/reference) 硬上限
    MAX_DAILY_LOG_DAYS = 90         # daily log 保留 90 天
    MAX_TOTAL_MEMORY_MB = 500       # 总大小软上限 (含 chroma/)
    
    def enforce_size_limits(self):
        """蒸馏跑完后调, 触发 FIFO 淘汰"""
        for category in ["user", "feedback", "project", "reference"]:
            files = sorted(
                (self.memory_root / category).glob("*.md"),
                key=lambda p: p.stat().st_mtime,  # 老的先淘汰
            )
            excess = len(files) - self.MAX_FILES_PER_CATEGORY
            if excess > 0:
                for old in files[:excess]:
                    old.unlink()
                    # 同步删 vector
                    self.vector.delete_by_path(old)
                log.info(f"evicted {excess} oldest memories in {category}")
        
        # daily log 轮转
        cutoff = time.time() - self.MAX_DAILY_LOG_DAYS * 86400
        for log_file in (self.memory_root / "logs").glob("*.md"):
            if log_file.stat().st_mtime < cutoff:
                log_file.unlink()
```

**生产告警阈值**:

| 指标 | 警告 | 严重 |
| --- | --- | --- |
| `memory_file_count.user` | > 400 | > 500（触发淘汰） |
| `total_memory_mb` | > 400 | > 500 |
| `startup_integrity_check.fail` | 任一 | 阻塞启动 |
| `daily_backup.age_hours` | > 26 | > 50（备份中断） |

---

## 十五、设计决策与备选方案（v2）

### 15.1 为什么不选 SQLite 存记忆？

| 方案 | 优点 | 缺点 |
| --- | --- | --- |
| **Per-file + 索引（v2.1 默认）** | 用户可读、git 友好、Edit 工具友好 | 大量文件时 IO 多 |
| 单 MEMORY.md（v1） | 简单 | 触顶 → 物理裁剪 → 丢信息 |
| SQLite | 查询快 | 用户不可读、不可 git diff、需专用工具 |
| Chroma + metadata | 作为 **`mode=vector` 选项**保留（详见 §6.6），不直接当主存储 | 不适合结构化记忆（缺 Why 等字段） |

**v2.1 决策**：per-file + 索引是默认，Chroma 仅作为 `mode=vector` 检索的可选实现（v1 资产保留）。Claude Code 也用 per-file + 索引，Mem0 类似。

### 15.2 为什么不选 Mem0 的 graph 存储？

Mem0 用图结构（实体 + 关系），适合"复杂实体关系"场景。agent-dev v2 主要存"偏好 / 决策 / 教训"，**关系简单**，图结构是过度设计。

### 15.3 为什么不每次 compact 都调 LLM？

每次 LLM 调用的成本：
- 5~15 秒延迟（阻塞主对话）
- 单次调用 2000~4000 tokens 计费
- cache miss（每次输入都不同）

L3 + L4 分层的成本：
- L3 SM-compact：< 100ms，**零 LLM**
- L4 后台 extract：~2s，**不阻塞**，cache 命中高
- 用户感知：永远"流畅"

### 15.4 为什么不用关键词匹配做提取判断？

关键词匹配的失败率（实测估算）：
- 漏报率：~40%（"我一般用 X" 不命中"习惯"——"习惯"特指持续行为，"用"只是单次选择）
- 误报率：~15%（"你总是这样" 命中"总是"，但用户在抱怨而非陈述偏好）

LLM 评分（一次小调用）：
- 漏报率：< 5%
- 误报率：< 5%
- 成本：~0.001 美元/次（Haiku）

**结论**：用 LLM 评分替代关键词，**精度提升 5~10 倍，成本增加 < 1%**。

### 15.5 检索模式：为什么不做单选题（v2.1）

v1 把 Chroma 强制为唯一检索路径；v2.1 改为**三模式可切换**——为什么不锁定一个？

| 场景 | 最优模式 | 原因 |
| --- | --- | --- |
| 离线批处理 / 无 LLM / 10K+ 记忆 | `vector` | Chroma cosine 无 LLM 延迟，规模化成熟 |
| 生产环境 / < 1K 记忆 / 用户可读 | `file` | Claude Code 实证路径：MEMORY.md + LLM side-query |
| 通用 / A/B 测试 / 迁移期 / 不确定规模 | `hybrid` | 向量粗筛 + LLM 精排，召回率与精度兼顾 |
| 团队开发 / 学习目的 | `hybrid` | 同时跑两条路径，产出对比数据 |

**v2.1 决策**：**三模式共存**而非锁定一个,理由：
1. **不同规模/场景下最优不同**——硬切会丢灵活性
2. **学习价值**——向量召回和 LLM 二次筛选都值得研究,hybrid 模式天然支持 A/B
3. **渐进迁移**——v1 → v2.1 不必一步到位,配置切换逐步验证
4. **风险对冲**——Chroma 维护成本高,允许关闭可降低 ops 负担

> **对比表与延迟/精度数据**：详见 §6.6.3.1 量化指标表

---

## 十六、与其他文档的关系

| 文档 | 关系 |
| --- | --- |
| [`claude-code-memory-system-deep-dive.md`](claude-code-memory-system-deep-dive.md) | **设计来源**——本文档基于 deep-dive 的源码分析升级 v1 → v2 |
| [`IMPLEMENTATION_PLAN.md`](IMPLEMENTATION_PLAN.md) | **实施计划**——8 天 AI+人协作、6 个验收点、风险清单(原 §10 内容已迁出) |
| `agent_core/context/compact.py` | **实现参考**——L3 SessionMemory 实现可参考 |
| `agent_core/session/manager.py` | **集成点**——主 agent 通过 SessionManager 触发记忆系统 |
| `tests/test_usage_baseline_restore.py` | **测试参考**——记忆系统的 baseline 持久化测试可借鉴 |

**双向引用规则**(2026-06-20 约定):
- design.md 描述**是什么 / 为什么 / 不变量**
- IMPLEMENTATION_PLAN.md 描述**何时做 / 谁做 / 怎么验收**
- 变更 design.md 不变量(如 §4.5 / §4.5.1)→ 同步更新 IMPLEMENTATION_PLAN.md M6 任务清单
- 调整 IMPLEMENTATION_PLAN.md 时间表 → 不影响 design.md(plan 与 design 解耦)

### 16.1 引用 deep-dive 的关键章节

| 本文档章节 | deep-dive 对应章节 |
| --- | --- |
| §四.1 双通道 | [`§4.1` 写入双通道 + `hasMemoryWritesSince` 互斥](../../ailearning/claude-code-analysis/src/services/compact/sessionMemoryCompact.ts) |
| §四.2 Edit-only | [`§4.5` `createMemoryFileCanUseTool` 工具沙箱](../../ailearning/claude-code-analysis/src/services/SessionMemory/sessionMemory.ts#L460-L482) |
| §四.3 Extract/Compact 分层 | [`§4.6` SM 文件增量更新 + 零 LLM compact](../../ailearning/claude-code-analysis/src/services/compact/sessionMemoryCompact.ts#L461-L475) |
| §四.4 L3 触发 | [`§4.7.6` 5 条回退条件](../../ailearning/claude-code-analysis/src/services/compact/sessionMemoryCompact.ts#L519-L630) |
| §五.3.1 封闭分类 | [`§1.1` MEMORY_TYPES 四类封闭](../../ailearning/claude-code-analysis/src/memdir/memoryTypes.ts) |
| §七 autoDream | [`§5` AutoDream 跨会话整合](../../ailearning/claude-code-analysis/src/services/autoDream/) |
| §十四 安全 | [`§10` 安全模型 6 层路径校验](../../ailearning/claude-code-analysis/src/utils/permissions/) |

### 16.2 时间衰减公式修正

**v1 公式**（在 §六）：
```
time_score = max(0.3, 1.0 - age_days / 30)
```

**v2 修正**（基于 deep-dive 中 Claude Code 的实际做法）：
```python
def time_score(age_days: float) -> float:
    """
    v2: 时间衰减分两段

    30 天内: 完全可信（1.0）
    30-90 天: 线性下降到 0.5
    90 天+: 最低保留（0.3）
    """
    if age_days <= 30:
        return 1.0
    elif age_days <= 90:
        # age_days=30 → 1.0, age_days=90 → 0.5
        return 1.0 - 0.5 * (age_days - 30) / 60
    else:
        return 0.3
```

**为何修正**：v1 公式 `1.0 - age_days/30` 在 `age_days = 30` 时已经衰减到 0，与"30 天内完全可信"的语义矛盾。v2 用分段函数明确表达"30 天内稳定"+"30-90 天衰减"+"90 天保底"的语义。

---
