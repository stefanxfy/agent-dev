"""
ContextBudgetManager — 上下文预算管理器
参考：Claude Code src/services/compact/autoCompact.ts
适配：GLM 模型参数，删除 Claude 专有逻辑
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from typing import Optional, Protocol, runtime_checkable

logger = logging.getLogger("context.budget")


def _get_env_window_override() -> int | None:
    """
    调试用：读取环境变量 CONTEXT_WINDOW_OVERRIDE 覆盖模型窗口
    用于快速验证压缩流程（无需发几万字对话）

    用法：
        export CONTEXT_WINDOW_OVERRIDE=8000
        # 然后对话中达到 ~6K tokens 就触发压缩
    """
    val = os.environ.get("CONTEXT_WINDOW_OVERRIDE", "").strip()
    if not val:
        return None
    try:
        n = int(val)
        if n <= 0:
            return None
        return n
    except ValueError:
        return None


# ── 常量配置 ────────────────────────────────────────────────────

# GLM-4 / GLM-5.1 上下文窗口（验证值）
GLM_CONTEXT_WINDOW = 128_000

# Auto-Compact 缓冲：剩余 ~6.25% 时触发
# Claude Code 用 13K/200K ≈ 6.5%，这里用 8K/128K ≈ 6.25%
AUTOCOMPACT_BUFFER_TOKENS = 8_000

# Summary API 最大输出预留
MAX_OUTPUT_TOKENS_FOR_SUMMARY = 4_096

# 熔断阈值：连续压缩失败 N 次则停止压缩
MAX_CONSECUTIVE_AUTOCOMPACT_FAILURES = 3

# 最低有效窗口保证
MIN_EFFECTIVE_WINDOW = 50_000


# ── 模型配置 ────────────────────────────────────────────────────

MODEL_CONFIGS: dict[str, dict] = {
    "glm-4": {
        "context_window": 128_000,
        "max_output": 4_096,
    },
    "glm-4-flash": {
        "context_window": 128_000,
        "max_output": 4_096,
    },
    "glm-5": {
        "context_window": 128_000,
        "max_output": 8_192,
    },
    "glm-5.1": {
        "context_window": 128_000,
        "max_output": 8_192,
    },
    # Claude 模型（通过 ANTHROPIC_BASE_URL 指向智谱兼容层时可能用）
    "claude-3-5-sonnet": {
        "context_window": 200_000,
        "max_output": 8_000,
    },
    "claude-3-7-sonnet": {
        "context_window": 200_000,
        "max_output": 8_000,
    },
}


def get_model_config(model: str) -> dict:
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


def get_effective_context_window(model: str) -> int:
    """
    计算有效可用窗口：总窗口 - Summary 预留 - Auto-Compact 缓冲

    参考：Claude Code getEffectiveContextWindowSize()

    这保证了当触发压缩时，API 还有足够空间容纳：
    - 压缩 prompt
    - Summary 输出（最多 4,096 tokens）
    """
    config = get_model_config(model)
    context_window = config["context_window"]
    max_output = config["max_output"]

    # 调试用：环境变量覆盖窗口（优先级最高）
    override = _get_env_window_override()
    if override is not None:
        # 调试模式：不设下限，完整尊重用户值
        effective = override - min(max_output, MAX_OUTPUT_TOKENS_FOR_SUMMARY) - AUTOCOMPACT_BUFFER_TOKENS
        logger.info(f"[DEBUG] CONTEXT_WINDOW_OVERRIDE={override}, effective={effective}")
        return max(effective, 1_000)  # 最低 1K，保证有足够空间

    reserved = min(max_output, MAX_OUTPUT_TOKENS_FOR_SUMMARY)
    effective = context_window - reserved - AUTOCOMPACT_BUFFER_TOKENS

    return max(effective, MIN_EFFECTIVE_WINDOW)


# ── 预算状态 ────────────────────────────────────────────────────

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
        """剩余空间低于缓冲阈值时触发"""
        return self.available < AUTOCOMPACT_BUFFER_TOKENS

    @property
    def is_critical(self) -> bool:
        """剩余空间低于缓冲阈值的一半"""
        return self.available < AUTOCOMPACT_BUFFER_TOKENS // 2

    def summary(self) -> str:
        """生成可读的状态摘要"""
        return (
            f"{self.used_tokens:,} / {self.total_budget:,} tokens "
            f"({self.usage_ratio:.0%} used, {self.available:,} available)"
        )


# ── TokenCounter 协议 ──────────────────────────────────────────

@runtime_checkable
class TokenCounterProto(Protocol):
    """Token 计数器协议"""
    def count(self, text: str) -> int: ...
    def count_messages(self, messages: list[dict]) -> int: ...


# ── 预算管理器 ─────────────────────────────────────────────────

class ContextBudgetManager:
    """
    上下文预算管理器

    职责：
    1. 维护 token 预算状态
    2. 判断是否需要触发压缩
    3. 熔断保护（连续失败则停止压缩）

    参考：Claude Code autoCompactIfNeeded() 的触发逻辑

    用法：
        bm = ContextBudgetManager("glm-4", token_counter)
        should, reason = bm.should_compact(messages)
        if should:
            ...  # 触发压缩
            bm.record_compact_success()  # 或 record_compact_failure()
    """

    def __init__(
        self,
        model: str,
        token_counter: TokenCounterProto,
    ):
        self.model = model
        self.token_counter = token_counter
        self.total_budget = get_effective_context_window(model)
        self.reserved = MAX_OUTPUT_TOKENS_FOR_SUMMARY

        # 熔断状态
        self.consecutive_failures = 0
        self.last_compact_time: Optional[float] = None

    def compute_budget_state(self, messages: list[dict]) -> BudgetState:
        """计算当前预算状态"""
        used = self.token_counter.count_messages(messages)
        return BudgetState(
            total_budget=self.total_budget,
            used_tokens=used,
            reserved_tokens=self.reserved,
        )

    def should_compact(self, messages: list[dict]) -> tuple[bool, str]:
        """
        判断是否应该触发压缩

        返回：(should_compact, reason)

        判断逻辑：
        1. 熔断检查 — 连续失败达上限则拒绝压缩
        2. 临界检查 — 剩余 < 缓冲/2，紧急触发
        3. 缓冲检查 — 剩余 < 缓冲阈值，正常触发
        4. 否则不触发
        """
        # 熔断检查
        if self.consecutive_failures >= MAX_CONSECUTIVE_AUTOCOMPACT_FAILURES:
            return False, f"熔断保护：连续 {self.consecutive_failures} 次压缩失败"

        state = self.compute_budget_state(messages)

        if state.is_critical:
            return True, f"临界状态：剩余 {state.available:,} tokens ({state.usage_ratio:.0%})"

        if state.should_auto_compact:
            return True, f"缓冲触发：剩余 {state.available:,} < {AUTOCOMPACT_BUFFER_TOKENS:,} tokens"

        return False, f"预算充足：{state.summary()}"

    def get_usage_info(self, messages: list[dict]) -> dict:
        """获取详细用量信息（用于 UI 显示）"""
        state = self.compute_budget_state(messages)
        return {
            "total_budget": state.total_budget,
            "used_tokens": state.used_tokens,
            "available_tokens": state.available,
            "usage_ratio": state.usage_ratio,
            "should_compact": state.should_auto_compact,
            "is_critical": state.is_critical,
            "consecutive_failures": self.consecutive_failures,
            "model": self.model,
        }

    def record_compact_success(self):
        """记录压缩成功"""
        self.consecutive_failures = 0
        self.last_compact_time = time.time()
        logger.info("Compact succeeded, circuit breaker reset")

    def record_compact_failure(self):
        """记录压缩失败"""
        self.consecutive_failures += 1
        logger.warning(
            f"Compact failure #{self.consecutive_failures}/"
            f"{MAX_CONSECUTIVE_AUTOCOMPACT_FAILURES}"
        )

    def reset_circuit_breaker(self):
        """手动重置熔断器"""
        self.consecutive_failures = 0
        logger.info("Circuit breaker manually reset")
