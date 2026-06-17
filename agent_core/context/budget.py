"""
ContextBudgetManager — 上下文预算管理器
参考：Claude Code src/services/compact/autoCompact.ts
适配：支持多模型，配置由 MODEL_CONFIGS 提供，硬编码值仅作后备
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from typing import Optional, Protocol, runtime_checkable

logger = logging.getLogger("context.budget")


# ── 常量配置 ────────────────────────────────────────────────────

# ── 双模式阈值常量（对齐 Claude Code autoCompact.ts）────────────

# 默认缓冲量（可被 MODEL_CONFIGS[model]["autocompact_buffer"] 覆盖）
# Claude Code 用 13K/200K ≈ 6.5%，默认值 13K/128K ≈ 10%
DEFAULT_AUTOCOMPACT_BUFFER_TOKENS = 13_000

# 严重阈值固定缓冲：剩余 < CRITICAL_BUFFER_TOKENS 时为临界状态
CRITICAL_BUFFER_TOKENS = 6_500

# UI 警告阈值固定缓冲（类似 Claude Code WARNING/ERROR_THRESHOLD_BUFFER_TOKENS）
WARNING_BUFFER_TOKENS = 20_000
ERROR_BUFFER_TOKENS = 20_000

# Summary API 最大输出预留
MAX_OUTPUT_TOKENS_FOR_SUMMARY = 4_096

# 熔断阈值：连续压缩失败 N 次则停止压缩
MAX_CONSECUTIVE_AUTOCOMPACT_FAILURES = 3

# 最低有效窗口保证
MIN_EFFECTIVE_WINDOW = 50_000


# ── 比例覆盖（环境变量，测试用）──────────────────────────────────

def _get_autocompact_pct_override() -> float | None:
    """
    读取 AUTOCOMPACT_PCT_OVERRIDE 环境变量
    语义：剩余百分比（对齐 Claude Code CLAUDE_AUTOCOMPACT_PCT_OVERRIDE）
    设为 25 = 剩余 ≤ 25% 时触发压缩（即已用 ≥ 75%）
    设为 10 = 剩余 ≤ 10% 时触发压缩（即已用 ≥ 90%）
    """
    val = os.environ.get("AUTOCOMPACT_PCT_OVERRIDE", "").strip()
    if not val:
        return None
    try:
        pct = float(val)
        if 0 < pct <= 100:
            return pct
        return None
    except ValueError:
        return None


# ── 模型配置 ────────────────────────────────────────────────────

MODEL_CONFIGS: dict[str, dict] = {
    # GLM 系列
    "glm-4": {
        "context_window": 128_000,
        "max_output": 4_096,
        "autocompact_buffer": 13_000,
    },
    "glm-4-flash": {
        "context_window": 128_000,
        "max_output": 4_096,
        "autocompact_buffer": 13_000,
    },
    "glm-5": {
        "context_window": 128_000,
        "max_output": 8_192,
        "autocompact_buffer": 13_000,
    },
    "glm-5.1": {
        "context_window": 128_000,
        "max_output": 8_192,
        "autocompact_buffer": 13_000,
    },
    # Claude 系列
    "claude-3-5-sonnet": {
        "context_window": 200_000,
        "max_output": 8_000,
        "autocompact_buffer": 13_000,
    },
    "claude-3-7-sonnet": {
        "context_window": 200_000,
        "max_output": 8_000,
        "autocompact_buffer": 13_000,
    },
    # OpenAI GPT 系列（示例，可按需添加）
    "gpt-4o": {
        "context_window": 128_000,
        "max_output": 16_384,
        "autocompact_buffer": 13_000,
    },
    "gpt-4-turbo": {
        "context_window": 128_000,
        "max_output": 4_096,
        "autocompact_buffer": 13_000,
    },
    "gpt-3.5-turbo": {
        "context_window": 16_385,
        "max_output": 4_096,
        "autocompact_buffer": 2_000,
    },
}


def get_model_config(model: str) -> dict:
    """获取模型配置，支持模糊匹配；未知模型返回保守默认值"""
    model_lower = model.lower()
    for key, config in MODEL_CONFIGS.items():
        if key in model_lower:
            return config
    # 未知模型：保守默认值（中等上下文窗口）
    return {
        "context_window": 32_000,
        "max_output": 4_096,
        "autocompact_buffer": DEFAULT_AUTOCOMPACT_BUFFER_TOKENS,
    }


def _get_autocompact_buffer(model: str) -> int:
    """获取模型专属的 autocompact 缓冲量（字节），未知模型用默认值"""
    config = get_model_config(model)
    return config.get("autocompact_buffer", DEFAULT_AUTOCOMPACT_BUFFER_TOKENS)


def get_effective_context_window(model: str) -> int:
    """
    计算有效可用窗口：总窗口 - Summary 预留 - Auto-Compact 缓冲

    参考：Claude Code getEffectiveContextWindowSize()

    缓冲量由模型配置决定（MODEL_CONFIGS[model]["autocompact_buffer"]），
    未知模型使用 DEFAULT_AUTOCOMPACT_BUFFER_TOKENS。

    这保证了当触发压缩时，API 还有足够空间容纳：
    - 压缩 prompt
    - Summary 输出（最多 max_output tokens）
    """
    config = get_model_config(model)
    context_window = config["context_window"]
    max_output = config["max_output"]
    buffer = _get_autocompact_buffer(model)

    reserved = min(max_output, MAX_OUTPUT_TOKENS_FOR_SUMMARY)
    effective = context_window - reserved - buffer

    return max(effective, MIN_EFFECTIVE_WINDOW)


# ── 预算状态 ────────────────────────────────────────────────────

@dataclass
class BudgetState:
    """预算状态快照"""
    total_budget: int
    used_tokens: int
    reserved_tokens: int

    # 双模式阈值（由 ContextBudgetManager 计算后传入）
    compact_threshold: int = 0    # should_auto_compact 触发线
    critical_threshold: int = 0   # is_critical 触发线

    @property
    def available(self) -> int:
        return self.total_budget - self.used_tokens

    @property
    def usage_ratio(self) -> float:
        return self.used_tokens / self.total_budget if self.total_budget > 0 else 0.0

    @property
    def should_auto_compact(self) -> bool:
        """已用 token 达到压缩阈值时触发（对齐 Claude Code: used >= threshold）"""
        if self.compact_threshold <= 0:
            # 兼容旧用法：无阈值时用固定缓冲
            return self.available < DEFAULT_AUTOCOMPACT_BUFFER_TOKENS
        return self.used_tokens >= self.compact_threshold

    @property
    def is_critical(self) -> bool:
        """已用 token 达到严重阈值时触发"""
        if self.critical_threshold <= 0:
            return self.available < CRITICAL_BUFFER_TOKENS
        return self.used_tokens >= self.critical_threshold

    @property
    def is_warning(self) -> bool:
        """已用 token 达到警告阈值（UI 黄色提示）"""
        warning_line = self.compact_threshold - WARNING_BUFFER_TOKENS
        return self.used_tokens >= warning_line if warning_line > 0 else False

    @property
    def is_error(self) -> bool:
        """已用 token 达到错误阈值（UI 红色提示）"""
        error_line = self.compact_threshold - ERROR_BUFFER_TOKENS
        return self.used_tokens >= error_line if error_line > 0 else False

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

        # 双模式阈值计算（对齐 Claude Code getAutoCompactThreshold）
        self.compact_threshold = self._calc_compact_threshold()
        self.critical_threshold = self._calc_critical_threshold()

        # 增量估算基准（对齐 Claude Code tokenCountWithEstimation）
        # 用上次 API 响应的 usage.input_tokens 作基准，只估算新增消息
        self._baseline_tokens: int = 0         # 上次 API 响应的 input_tokens
        self._baseline_msg_count: int = 0       # 上次 API 响应时的消息数
        self._baseline_valid: bool = False      # 基准是否有效（压缩/session切换后失效）

        # 熔断状态
        self.consecutive_failures = 0
        self.last_compact_time: Optional[float] = None

    # ── 增量估算基准 ──────────────────────────────────────────

    def set_baseline(self, input_tokens: int, message_count: int) -> None:
        """
        从第 N-1 轮 LLM 响应的 usage 中捕获基准。

        参考：Claude Code tokenCountWithEstimation()
        核心思想：当前上下文大小 ≈ 上次 API input_tokens + 新增消息粗略估算

        Args:
            input_tokens: API 返回的 usage.input_tokens（权威数字）
            message_count: 捕获 usage 时的 self.messages 长度
        """
        if input_tokens <= 0:
            return
        self._baseline_tokens = input_tokens
        self._baseline_msg_count = message_count
        self._baseline_valid = True
        logger.debug(
            f"[增量基准] input_tokens={input_tokens:,}, "
            f"msg_count={message_count}"
        )

    def invalidate_baseline(self) -> None:
        """
        使增量基准失效。

        场景：压缩发生/session 切换/消息列表被改写后，
        上次 API 的 input_tokens 不再准确，需要重新全量估算。
        """
        self._baseline_valid = False
        logger.debug("[增量基准] 已失效")

    # ── 双模式阈值计算 ────────────────────────────────────────

    def _calc_compact_threshold(self) -> int:
        """
        计算压缩触发阈值（双模式）

        模式1（默认）：固定缓冲 — total_budget - 模型专属缓冲量
        模式2（环境变量）：剩余比例 — 剩余 ≤ pct% 时触发
            threshold = total_budget * (1 - pct/100)
        取两者中更小的（更早触发 = 更保守）

        参考：Claude Code autoCompact.ts getAutoCompactThreshold()
        示例：PCT=25 → threshold = 75% * total_budget（剩余25%时触发）
        """
        buffer = _get_autocompact_buffer(self.model)
        fixed = self.total_budget - buffer

        pct = _get_autocompact_pct_override()
        if pct is not None:
            # 剩余百分比语义：剩余 ≤ pct% 时触发，即 used ≥ (1-pct%) * total
            pct_threshold = int(self.total_budget * (1 - pct / 100))
            result = min(pct_threshold, fixed)
            logger.info(
                f"[双模式] 剩余比例={pct}%, 比例阈值={pct_threshold:,}, "
                f"固定阈值={fixed:,}, 取较小值={result:,}"
            )
            return result

        return fixed

    def _calc_critical_threshold(self) -> int:
        """
        计算严重阈值（双模式）

        固定缓冲：total_budget - 模型专属 critical_buffer（= autocompact_buffer × 0.5）
        比例覆盖：critical 剩余比例 = compact 剩余比例 / 2
            如果 compact PCT=25（剩余25%），critical PCT=12.5（剩余12.5%）
            threshold = total_budget * (1 - critical_pct/100)
        取更保守的
        """
        # critical_buffer 与 autocompact_buffer 保持 0.5 倍比例
        model_buffer = _get_autocompact_buffer(self.model)
        critical_fixed = int(model_buffer * 0.5)
        fixed = self.total_budget - critical_fixed

        pct = _get_autocompact_pct_override()
        if pct is not None:
            critical_pct = pct / 2
            pct_threshold = int(self.total_budget * (1 - critical_pct / 100))
            return min(pct_threshold, fixed)

        return fixed

    def compute_budget_state(self, messages: list[dict]) -> BudgetState:
        """计算当前预算状态（优先增量估算，降级全量估算）"""
        used = self._estimate_used_tokens(messages)
        return BudgetState(
            total_budget=self.total_budget,
            used_tokens=used,
            reserved_tokens=self.reserved,
            compact_threshold=self.compact_threshold,
            critical_threshold=self.critical_threshold,
        )

    def _estimate_used_tokens(self, messages: list[dict]) -> int:
        """
        估算当前上下文 token 数（增量优先，全量降级）。

        参考：Claude Code tokenCountWithEstimation()
        逻辑：
        1. 如果有有效基准 → 基准 + 新增消息粗略估算（O(ΔN)）
        2. 否则 → 全量粗略估算（O(N)）

        注意：增量估算可能高估（cache_read_input_tokens 计入了基准但不占实际窗口），
        高估 = 更保守 = 安全余量。
        """
        msg_count = len(messages)

        # 增量路径：基准 + 新增
        if (
            self._baseline_valid
            and self._baseline_msg_count > 0
            and self._baseline_msg_count <= msg_count
        ):
            new_messages = messages[self._baseline_msg_count:]
            if new_messages:
                estimated_new = self.token_counter.count_messages(new_messages)
                result = self._baseline_tokens + estimated_new
                logger.debug(
                    f"[增量估算] baseline={self._baseline_tokens:,} + "
                    f"new({len(new_messages)} msgs)={estimated_new:,} = {result:,}"
                )
                return result
            else:
                # 无新增 → 直接用基准（API 权威数字比粗略估算更准确）
                logger.debug(
                    f"[增量估算] baseline={self._baseline_tokens:,}, 无新增"
                )
                return self._baseline_tokens

        # 全量路径
        used = self.token_counter.count_messages(messages)
        logger.debug(f"[全量估算] {msg_count} msgs = {used:,} tokens")
        return used

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
            return True, f"临界状态：已用 {state.used_tokens:,} / {self.critical_threshold:,} ({state.usage_ratio:.0%})"

        if state.should_auto_compact:
            return True, f"压缩触发：已用 {state.used_tokens:,} / {self.compact_threshold:,} ({state.usage_ratio:.0%})"

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
            "is_warning": state.is_warning,
            "is_error": state.is_error,
            "compact_threshold": self.compact_threshold,
            "critical_threshold": self.critical_threshold,
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


# ── 向后兼容别名（旧常量名仍可导入）────────────────────────────────
AUTOCOMPACT_BUFFER_TOKENS = DEFAULT_AUTOCOMPACT_BUFFER_TOKENS
