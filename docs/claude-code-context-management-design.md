# Claude Code 上下文管理方案 · 精简技术实现文档

> 基于 Claude Code 源码解析，为 agent-dev 项目设计的上下文管理系统
>
> 参考：Claude Code `src/services/compact/` + `src/services/api/claude.ts`
>
> 版本：v2.0 | 日期：2026-06-14
> 变更：v1.0 → v2.0 删除不适用模块，适配 GLM，修正 Claude Code 分析错误

---

## 一、设计决策：什么不做，什么做

### 删除的模块及原因

| v1.0 模块 | 删除原因 |
|-----------|----------|
| `PromptCacheManager` | `cache_control` 是 Anthropic API 专有参数，GLM/OpenAI 不支持。应用层无法管理服务端缓存，只需保持消息顺序稳定，服务端自动做前缀匹配。Claude Code 的 Forked Agent 共享缓存前缀、TTL 策略选择、Cache Break Detection 都依赖 `cache_control` + `cache_read_input_tokens` 响应字段，GLM 兼容层不提供这些。 |
| `StateKeeper` | 追踪的 `FileReadState`、`PlanState`、`MCPState`、`DeferredTool` 在 agent-dev 中全都不存在。agent-dev 没有文件读取工具、没有 Plan 系统、没有 MCP 工具协议。设计一堆追踪空状态的功能是浪费。 |
| `MessageStore` | 与已有 `SessionStorage` 功能完全重叠。`SessionStorage` 已实现 JSONL 持久化、`get_messages()`、`get_messages_for_llm()`、tail/head 双窗口读取。两套存储并行必然导致数据不一致。 |

### 保留的模块

| 模块 | 核心价值 |
|------|----------|
| `ContextBudgetManager` | Token 预算监控 + 自动压缩触发 + 熔断保护。替换 `agent_core.py` 中简陋的 `_trim_history`。 |
| `CompactOrchestrator` | 压缩流程编排：预处理 → 构建 prompt → 调 LLM 生成摘要 → PTL 防御 → 返回压缩后的消息。 |

### Claude Code 设计理念的适用性分析

| Claude Code 设计 | agent-dev 适用性 | 说明 |
|------------------|------------------|------|
| `cache_control` 标记 | ❌ 不适用 | Anthropic API 专有，GLM 不支持 |
| TTL 策略（5min/1h） | ❌ 不适用 | Anthropic API 专有 |
| Forked Agent 共享缓存 | ❌ 不适用 | 依赖 `cache_control` 定位缓存边界 |
| Cache Break Detection | ❌ 不适用 | 依赖 `cache_read_input_tokens` 响应字段 |
| 消息前缀复用 | ✅ 天然适用 | 压缩请求复用主对话 messages 前缀，服务端自动缓存，应用层无需处理 |
| 压缩 + 状态重建 | ✅ 简化适用 | 保留摘要 + 最近消息的重建策略，去掉文件/Plan/MCP 追踪 |
| 预算管理 + 熔断 | ✅ 直接适用 | Token 预算监控、自动触发、连续失败熔断 |
| PTL 剥洋葱防御 | ✅ 直接适用 | 压缩请求本身超限时，逐层截断旧消息重试 |

---

## 二、整体架构（精简版）

```
┌───────────────────────────────────────────────────┐
│               Context Manager                     │
│  ┌──────────────────────────────────────────┐     │
│  │  1. ContextBudgetManager  预算与触发      │     │
│  │  2. CompactOrchestrator   压缩编排        │     │
│  └──────────────────────────────────────────┘     │
│                      │                             │
│                      ▼                             │
│            SessionStorage (已有)                    │
│            JSONL 持久化 + 读取                       │
└───────────────────────────────────────────────────┘
              │                        │
              ▼                        ▼
    ┌────────────────┐       ┌────────────────┐
    │  agent_core    │       │ langgraph_     │
    │  ReAct 循环     │       │ agent          │
    └────────────────┘       └────────────────┘
```

**核心原则**：上下文管理器不自己存消息，复用已有的 `SessionStorage`。它只负责"监控 token 用量"和"触发压缩"两件事。

---

## 三、预算管理（ContextBudgetManager）

### 3.1 GLM 适配的参数

Claude Code 的常量针对 200K 窗口的 Claude 模型。agent-dev 用 GLM-4/GLM-4-Flash，参数需要调整：

```python
# agent_core/context/budget.py

# GLM-4 上下文窗口 128K（验证值，不同版本可能不同）
GLM_CONTEXT_WINDOW = 128_000

# Auto-Compact 缓冲：剩余 ~8% 时触发
# Claude Code 用 13K/200K ≈ 6.5%，这里用 8K/128K ≈ 6.25%
AUTOCOMPACT_BUFFER_TOKENS = 8_000

# Summary API 最大输出预留
MAX_OUTPUT_TOKENS_FOR_SUMMARY = 4_096

# 熔断阈值
MAX_CONSECUTIVE_AUTOCOMPACT_FAILURES = 3

# GLM 模型配置
MODEL_CONFIGS = {
    "glm-4": {
        "context_window": 128_000,
        "max_output": 4_096,
    },
    "glm-4-flash": {
        "context_window": 128_000,
        "max_output": 4_096,
    },
    "glm-5.1": {
        "context_window": 128_000,
        "max_output": 8_192,
    },
}
```

### 3.2 预算计算

```
有效可用窗口 = 总窗口 - Summary 预留 - Auto-Compact 缓冲
             = 128,000 - 4,096 - 8,000
             ≈ 115,904 tokens
```

### 3.3 预算管理器实现

```python
# agent_core/context/budget.py
"""
ContextBudgetManager — 上下文预算管理器
参考：Claude Code src/services/compact/autoCompact.ts
适配：GLM 模型参数，删除 Claude 专有逻辑
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger("context.budget")


@dataclass
class BudgetState:
    """预算状态快照"""
    total_budget: int
    used_tokens: int
    reserved_tokens: int

    @property
    def available(self) -> int:
        return self.total_budget - self.used_tokens

    @property
    def usage_ratio(self) -> float:
        return self.used_tokens / self.total_budget if self.total_budget > 0 else 0.0

    @property
    def should_auto_compact(self) -> bool:
        return self.available < AUTOCOMPACT_BUFFER_TOKENS

    @property
    def is_critical(self) -> bool:
        return self.available < AUTOCOMPACT_BUFFER_TOKENS // 2


class ContextBudgetManager:
    """
    上下文预算管理器

    职责：
    1. 维护 token 预算状态
    2. 判断是否需要触发压缩
    3. 熔断保护（连续失败则停止压缩）

    参考：Claude Code autoCompactIfNeeded() 的触发逻辑
    """

    def __init__(self, model: str, token_counter):
        self.model = model
        self.token_counter = token_counter
        self.total_budget = self._compute_effective_window(model)
        self.reserved = MAX_OUTPUT_TOKENS_FOR_SUMMARY

        # 熔断状态
        self.consecutive_failures = 0
        self.last_compact_time: Optional[float] = None

    def _compute_effective_window(self, model: str) -> int:
        """计算有效窗口：总窗口 - Summary 预留 - 缓冲"""
        config = self._get_model_config(model)
        context_window = config["context_window"]
        max_output = config["max_output"]

        reserved = min(max_output, MAX_OUTPUT_TOKENS_FOR_SUMMARY)
        effective = context_window - reserved - AUTOCOMPACT_BUFFER_TOKENS
        return max(effective, 50_000)  # 最低保证 50K

    def _get_model_config(self, model: str) -> dict:
        """获取模型配置，支持模糊匹配"""
        model_lower = model.lower()
        for key, config in MODEL_CONFIGS.items():
            if key in model_lower:
                return config
        # 默认配置（保守）
        return {
            "context_window": 128_000,
            "max_output": 4_096,
        }

    def compute_budget_state(self, messages: list) -> BudgetState:
        """计算当前预算状态"""
        used = self.token_counter.count_messages(messages)
        return BudgetState(
            total_budget=self.total_budget,
            used_tokens=used,
            reserved_tokens=self.reserved,
        )

    def should_compact(self, messages: list) -> tuple[bool, str]:
        """
        判断是否应该触发压缩

        返回：(should_compact, reason)
        """
        # 熔断检查
        if self.consecutive_failures >= MAX_CONSECUTIVE_AUTOCOMPACT_FAILURES:
            return False, f"熔断保护：连续 {self.consecutive_failures} 次压缩失败"

        state = self.compute_budget_state(messages)

        if state.is_critical:
            return True, f"临界状态：剩余 {state.available} tokens ({state.usage_ratio:.0%})"

        if state.should_auto_compact:
            return True, f"缓冲触发：剩余 {state.available} < {AUTOCOMPACT_BUFFER_TOKENS} tokens"

        return False, f"预算充足：{state.usage_ratio:.0%} used, {state.available} available"

    def record_compact_success(self):
        self.consecutive_failures = 0
        self.last_compact_time = time.time()

    def record_compact_failure(self):
        self.consecutive_failures += 1
        logger.warning(f"Compact failure #{self.consecutive_failures}")

    def reset_circuit_breaker(self):
        self.consecutive_failures = 0
```

### 3.4 Token Counter

保留 v1.0 的实现，已经足够好：

```python
# agent_core/context/tokenizer.py

class SimpleTokenCounter:
    """
    Token 估算器
    - 中文：~1.4 tokens/字
    - 英文：~0.25 tokens/字
    - Role overhead：~10 tokens/消息
    - 工具调用固定开销：50 tokens
    - 工具结果固定开销：20 tokens
    """
    CHINESE_RATIO = 1.4
    ENGLISH_RATIO = 0.25
    ROLE_OVERHEAD = 10
    TOOL_CALL_FIXED = 50
    TOOL_RESULT_FIXED = 20

    def count(self, text: str) -> int:
        if not text:
            return 0
        import re
        chinese = len(re.findall(r'[\u4e00-\u9fff]', text))
        english = len(re.findall(r'[a-zA-Z]', text))
        other = len(text) - chinese - english
        return int(chinese * self.CHINESE_RATIO
                   + english * self.ENGLISH_RATIO
                   + other * 0.25)

    def count_messages(self, messages: list) -> int:
        total = 0
        for msg in messages:
            total += self.ROLE_OVERHEAD
            content = msg.get("content", "")
            if isinstance(content, str):
                total += self.count(content)
            elif isinstance(content, list):
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    btype = block.get("type", "text")
                    if btype == "text":
                        total += self.count(block.get("text", ""))
                    elif btype == "tool_use":
                        total += self.TOOL_CALL_FIXED
                        total += self.count(str(block.get("input", {})))
                    elif btype == "tool_result":
                        total += self.TOOL_RESULT_FIXED
                        rc = block.get("content", "")
                        if isinstance(rc, str):
                            total += self.count(rc)
        return total
```

---

## 四、压缩编排器（CompactOrchestrator）

### 4.1 设计思路

Claude Code 的压缩流程核心：预处理 → 构建 prompt → 调 LLM → PTL 防御 → 返回。

agent-dev 删除了 Forked Agent 共享缓存逻辑（依赖 `cache_control`），改为直接调 LLM 生成摘要。PTL 防御保留——压缩请求本身超限时，逐层截断旧消息重试。

### 4.2 压缩 Prompt

参考 Claude Code 的两阶段格式（analysis + summary）：

```python
# agent_core/context/compact.py

COMPACT_SYSTEM_PROMPT = """你是对话摘要生成器。将长对话历史压缩为简洁摘要，保留关键信息。

要求：
- 用纯文本回复，不要调用任何工具
- 输出分两部分：<analysis> 和 <summary>
- <analysis>：自由分析关键决策、行动、重要上下文
- <summary>：结构化摘要（用户目标/关键决策/当前状态/待办事项）

防漂移规则：
- 用户消息必须逐字引用（verbatim quotes），不要改写
- Next Step 必须与用户最近显式请求直接相关
- 不要捡起旧的已完成任务
"""

COMPACT_USER_PROMPT_TEMPLATE = """请总结以下对话历史。

对话内容：
{conversation}

用 <analysis> + <summary> 格式输出摘要。"""
```

### 4.3 压缩编排器实现

```python
# agent_core/context/compact.py
"""
CompactOrchestrator — 压缩编排器
参考：Claude Code src/services/compact/compact.ts
适配：删除 Forked Agent / cache_control，保留 PTL 防御
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger("context.compact")

# PTL 防御：剥洋葱策略
MAX_PTL_RETRIES = 3
TRUNCATE_RATIO = 0.2  # 每次剥掉最旧的 20%


@dataclass
class CompactionResult:
    """压缩结果"""
    success: bool
    summary: str
    compacted_messages: list[dict]
    tokens_before: int
    tokens_after: int
    error: Optional[str] = None
    compact_time_ms: float = 0
    ptl_retries: int = 0


class CompactOrchestrator:
    """
    压缩编排器

    职责：
    1. 消息预处理（脱水：截断长工具结果、移除图片占位）
    2. 构建压缩 prompt
    3. 调用 LLM 生成摘要
    4. PTL 防御（压缩请求超限时剥洋葱重试）
    5. 返回压缩后的消息列表

    不负责：
    - 状态追踪与重建（agent-dev 当前不需要）
    - Prompt Cache 管理（GLM 不支持 cache_control）
    - Forked Agent（依赖 cache_control，不适用）
    """

    def __init__(self, llm_router, budget_manager, token_counter):
        self.llm = llm_router
        self.budget = budget_manager
        self.token_counter = token_counter

    async def compact(self, messages: list[dict]) -> CompactionResult:
        """
        执行压缩

        流程：
        1. 预处理消息（脱水）
        2. 构建压缩 prompt
        3. 调 LLM 生成摘要
        4. PTL 防御
        5. 组装压缩后消息
        """
        start = time.time()
        tokens_before = self.token_counter.count_messages(messages)

        try:
            # 1. 预处理
            preprocessed = self._preprocess(messages)

            # 2-4. 生成摘要（含 PTL 防御）
            summary, ptl_retries = await self._generate_summary_with_ptl(
                preprocessed
            )

            if not summary:
                raise ValueError("LLM 返回空摘要")

            # 5. 组装压缩后消息
            compacted = self._build_compacted_messages(
                summary, preprocessed
            )

            tokens_after = self.token_counter.count_messages(compacted)
            elapsed = (time.time() - start) * 1000

            self.budget.record_compact_success()

            return CompactionResult(
                success=True,
                summary=summary,
                compacted_messages=compacted,
                tokens_before=tokens_before,
                tokens_after=tokens_after,
                compact_time_ms=elapsed,
                ptl_retries=ptl_retries,
            )

        except Exception as e:
            logger.error(f"Compact failed: {e}")
            self.budget.record_compact_failure()
            return CompactionResult(
                success=False,
                summary="",
                compacted_messages=messages,
                tokens_before=tokens_before,
                tokens_after=tokens_before,
                error=str(e),
                compact_time_ms=(time.time() - start) * 1000,
            )

    def _preprocess(self, messages: list[dict]) -> list[dict]:
        """
        消息脱水：
        1. 截断超长工具结果（>8000 字符）
        2. 移除图片/文档内容，替换为占位符
        3. 保留消息结构
        """
        result = []
        for msg in messages:
            content = msg.get("content", "")
            if isinstance(content, str):
                result.append(msg)
            elif isinstance(content, list):
                new_blocks = []
                for block in content:
                    if not isinstance(block, dict):
                        new_blocks.append(block)
                        continue
                    btype = block.get("type", "text")
                    if btype == "text":
                        new_blocks.append(block)
                    elif btype in ("image", "document"):
                        new_blocks.append({
                            "type": "text",
                            "text": f"[{btype} content removed for compression]"
                        })
                    elif btype == "tool_result":
                        rc = block.get("content", "")
                        text = rc if isinstance(rc, str) else str(rc)
                        if len(text) > 8000:
                            text = text[:8000] + "\n... [truncated]"
                        new_blocks.append({
                            "type": "text",
                            "text": f"[RESULT] {text}"
                        })
                    else:
                        new_blocks.append(block)
                result.append({**msg, "content": new_blocks})
            else:
                result.append(msg)
        return result

    async def _generate_summary_with_ptl(
        self, messages: list[dict]
    ) -> tuple[str, int]:
        """
        生成摘要，含 PTL 防御

        如果压缩请求本身触发 Prompt-Too-Long 错误，
        逐层截断最旧的消息分组，最多重试 MAX_PTL_RETRIES 次。

        参考：Claude Code compact.ts 的 PTL retry loop
        """
        to_summarize = messages
        last_error = ""

        for attempt in range(MAX_PTL_RETRIES + 1):
            conversation_text = self._messages_to_text(to_summarize)
            prompt = COMPACT_USER_PROMPT_TEMPLATE.format(
                conversation=conversation_text
            )

            try:
                response = await self.llm.chat(
                    messages=[
                        {"role": "system", "content": COMPACT_SYSTEM_PROMPT},
                        {"role": "user", "content": prompt},
                    ],
                    max_tokens=4096,
                    temperature=0.3,
                )
                summary = self._extract_summary(response)
                if summary:
                    return summary, attempt

            except Exception as e:
                last_error = str(e)
                err_lower = last_error.lower()
                # 检测 PTL 错误
                if any(kw in err_lower for kw in [
                    "prompt too long", "context length",
                    "maximum context", "too many tokens"
                ]):
                    logger.warning(
                        f"PTL retry {attempt + 1}/{MAX_PTL_RETRIES}: "
                        f"truncating {TRUNCATE_RATIO:.0%} oldest messages"
                    )
                    truncate_count = max(
                        1, int(len(to_summarize) * TRUNCATE_RATIO)
                    )
                    # 保留 system prompt（第一条），截断其后最旧的消息
                    if len(to_summarize) > truncate_count + 2:
                        to_summarize = (
                            to_summarize[:1]  # system
                            + to_summarize[truncate_count + 1:]  # 去掉最旧的
                        )
                    continue
                raise

        raise ValueError(
            f"PTL defense exhausted after {MAX_PTL_RETRIES} retries: {last_error}"
        )

    def _messages_to_text(self, messages: list[dict]) -> str:
        """将消息列表转为可读文本"""
        lines = []
        for msg in messages:
            role = msg.get("role", "user").upper()
            content = msg.get("content", "")
            if isinstance(content, str):
                lines.append(f"[{role}]\n{content}")
            elif isinstance(content, list):
                parts = []
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    btype = block.get("type", "text")
                    if btype == "text":
                        parts.append(block.get("text", ""))
                    elif btype == "tool_use":
                        name = block.get("name", "unknown")
                        inp = json.dumps(
                            block.get("input", {}),
                            ensure_ascii=False
                        )[:200]
                        parts.append(f"[TOOL: {name}] {inp}")
                    elif btype == "tool_result":
                        rc = block.get("content", "")
                        text = rc if isinstance(rc, str) else str(rc)
                        parts.append(f"[RESULT] {text[:500]}")
                lines.append(f"[{role}]\n" + "\n".join(parts))
        return "\n\n---\n\n".join(lines)

    def _extract_summary(self, text: str) -> str:
        """从 LLM 响应中提取摘要"""
        if not text:
            return ""
        # <summary> 标签优先
        if "<summary>" in text:
            start = text.find("<summary>") + len("<summary>")
            end = text.find("</summary>")
            if end > start:
                return text[start:end].strip()
        # <analysis> 标签兜底
        if "<analysis>" in text:
            start = text.find("<analysis>")
            return text[start:].strip()
        # 纯文本兜底
        return text.strip()

    def _build_compacted_messages(
        self,
        summary: str,
        original: list[dict],
        preserved_head: int = 6,
    ) -> list[dict]:
        """
        组装压缩后的消息列表

        结构：[system] + [summary] + [最近 N 条消息]

        参考 Claude Code 的压缩后消息结构：
        [System 边界宣告] + [精简摘要] + [最近对话]
        """
        result = []

        # 1. 保留 system prompt
        for msg in original:
            if msg.get("role") == "system":
                result.append(msg)
                break

        # 2. 插入摘要消息
        result.append({
            "role": "user",
            "content": f"[Previous conversation summarized]\n\n{summary}"
        })

        # 3. 保留最近 N 条消息
        recent = [m for m in original if m.get("role") != "system"][-preserved_head:]
        result.extend(recent)

        return result
```

---

## 五、上下文管理器主入口（ContextManager）

```python
# agent_core/context/manager.py
"""
ContextManager — 统一上下文管理器

职责边界：
- 监控 token 用量（通过 ContextBudgetManager）
- 触发压缩（通过 CompactOrchestrator）
- 不存储消息（复用 SessionStorage）
- 不追踪状态（agent-dev 当前不需要）
"""

from __future__ import annotations

import logging
from typing import Optional

from .budget import ContextBudgetManager
from .compact import CompactOrchestrator, CompactionResult
from .tokenizer import SimpleTokenCounter

logger = logging.getLogger("context.manager")


class ContextManager:
    """
    统一上下文管理器

    用法：
        cm = ContextManager(llm_router, model="glm-4")
        cm.check_and_compact(messages)  # 每轮 ReAct 循环后调用

    注意：ContextManager 不持有消息列表，消息由 Agent / SessionStorage 管理。
    ContextManager 只接收消息列表做"检查 → 压缩 → 返回新列表"。
    """

    def __init__(
        self,
        llm_router,
        model: str = "glm-4",
        token_counter=None,
    ):
        self.llm = llm_router
        self.model = model
        self.token_counter = token_counter or SimpleTokenCounter()
        self.budget = ContextBudgetManager(model, self.token_counter)
        self.compactor = CompactOrchestrator(
            llm_router=llm_router,
            budget_manager=self.budget,
            token_counter=self.token_counter,
        )

        # 统计
        self.compact_count = 0
        self.total_tokens_freed = 0

    def should_compact(self, messages: list[dict]) -> tuple[bool, str]:
        """检查是否需要压缩"""
        return self.budget.should_compact(messages)

    async def check_and_compact(
        self, messages: list[dict]
    ) -> tuple[list[dict], Optional[CompactionResult]]:
        """
        检查并在需要时执行压缩

        返回：(压缩后的消息列表, 压缩结果)
        如果不需要压缩，返回 (原消息, None)
        """
        should, reason = self.budget.should_compact(messages)
        if not should:
            return messages, None

        logger.info(f"Auto-compact triggered: {reason}")
        result = await self.compactor.compact(messages)

        if result.success:
            self.compact_count += 1
            self.total_tokens_freed += (
                result.tokens_before - result.tokens_after
            )
            logger.info(
                f"Compact succeeded: "
                f"{result.tokens_before} → {result.tokens_after} tokens, "
                f"{len(result.compacted_messages)} messages, "
                f"{result.compact_time_ms:.0f}ms"
            )
            return result.compacted_messages, result
        else:
            logger.warning(f"Compact failed: {result.error}")
            return messages, result

    def get_stats(self) -> dict:
        """获取上下文管理统计"""
        return {
            "model": self.model,
            "total_budget": self.budget.total_budget,
            "compact_count": self.compact_count,
            "total_tokens_freed": self.total_tokens_freed,
            "consecutive_failures": self.budget.consecutive_failures,
        }
```

---

## 六、与 agent_core.py 的集成

### 6.1 集成方案

ContextManager 替换 `agent_core.py` 中的 `_trim_history`：

```python
# agent_core/agent_core.py 修改

class ReactAgent:
    def __init__(self, ...):
        # ... 现有初始化 ...
        
        # 新增：上下文管理器
        from .context.manager import ContextManager
        self.context_manager = ContextManager(
            llm_router=llm_router,
            model=llm_router.model,
        )

    async def run(self, user_input: str):
        # 添加用户消息到 history
        self.history.append({"role": "user", "content": user_input})

        while self.turn < self.max_turns:
            # 检查并压缩上下文
            self.history, compact_result = (
                await self.context_manager.check_and_compact(self.history)
            )
            
            # 调用 LLM
            response = await self.llm_router.chat(self.history)
            
            # ... 处理工具调用 ...
            
            self.history.append({"role": "assistant", "content": response})
            
            if no_more_tools:
                break
        
        return final_response
```

### 6.2 删除的旧逻辑

```python
# 删除：_trim_history（粗暴截断）
def _trim_history(self):
    """旧的暴力截断，用 ContextManager 替代"""
    ...
```

### 6.3 迁移步骤

```
Phase 1: 基础框架
  - 实现 ContextBudgetManager + SimpleTokenCounter
  - 单元测试：should_compact 逻辑、熔断器
  - 不集成到 Agent，先独立验证

Phase 2: 压缩功能
  - 实现 CompactOrchestrator
  - 端到端测试：构造超长对话 → 触发压缩 → 验证摘要质量
  - PTL 防御测试

Phase 3: 集成
  - 替换 agent_core.py 的 _trim_history
  - Streamlit UI 显示 token 用量和压缩状态
  - 集成测试
```

---

## 七、Claude Code 设计精髓（修正版）

从源码中提炼的真正有价值的设计原则：

1. **不要暴力截断** — 提前 ~6.5% 缓冲触发，优雅压缩而非丢失最近上下文
2. **熔断保护** — 连续 N 次压缩失败就停止，避免死循环浪费 API 调用
3. **PTL 剥洋葱** — 压缩请求本身超限时，逐层截断旧消息重试
4. **两阶段摘要** — analysis（自由分析）+ summary（结构化输出），比单一摘要质量更高
5. **verbatim quotes 防漂移** — 用户消息逐字引用，防止摘要改写导致语义偏移
6. **构造函数零副作用** — `__init__` 不写盘不触发压缩，避免 Streamlit 重建时的元数据爆炸问题
7. **消息顺序稳定** — system prompt → tools → history → new message 的固定顺序让服务端前缀缓存自然命中
8. **压缩后保留最近 N 条原始消息** — 不全部用摘要替代，最近的上下文保留原文确保连贯
9. **lazy 操作** — 延迟创建、延迟写入、按需读取（tail/head 窗口）

---

## 八、不实现的模块与原因

| 模块 | Claude Code 的用途 | 不实现的原因 |
|------|-------------------|-------------|
| PromptCacheManager | `cache_control` 标记 + TTL 策略 + Cache Break Detection | 全部依赖 Anthropic API 专有特性，GLM 不支持 |
| StateKeeper | 追踪 FileRead/Plan/MCP/DeferredTool | agent-dev 没有这些工具和系统 |
| MessageStore | 消息存储 + 分层保留 | SessionStorage 已实现，不重复造轮子 |
| Forked Agent | 共享父对话缓存前缀做压缩 | 依赖 `cache_control`，GLM 场景下无意义 |

如果 agent-dev 未来加入文件读取工具、Plan 系统、或 MCP 协议支持，可以再增量添加 StateKeeper 的对应子模块。

---

> 文档版本：v2.0
> 更新日期：2026-06-14
> 适用项目：agent-dev
> 基于 Claude Code 源码 + GLM 适配分析
