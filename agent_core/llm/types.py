"""Stream response 数据类(纯数据,无业务逻辑)。

历史:从 router.py:116-343 抽出(2026-06-29,LLM Router 重构 Stage 1)。
让 router.py 专注 dispatch,数据类集中到 types.py 便于复用 / 类型提示。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


# ── Helpers(Pydantic+dict 混合取字段) ─────────────────────────────


def _get_int(obj, *attrs, default=0):
    """从 Pydantic model 或 dict 中取第一个存在的整数字段"""
    for attr in attrs:
        val = getattr(obj, attr, None) if hasattr(obj, attr) else obj.get(attr) if isinstance(obj, dict) else None
        if val is not None:
            return int(val)
    return default


def _get_nested(obj, *attrs, default=0):
    """取嵌套属性:先取中间对象,再取目标字段(Pydantic+dict 混合)"""
    cur = obj
    for attr in attrs:
        if cur is None:
            return default
        cur = getattr(cur, attr, None) if hasattr(cur, attr) else cur.get(attr) if isinstance(cur, dict) else None
    if cur is None:
        return default
    return int(cur) if isinstance(cur, (int, float)) else default


# ── Delta 数据类 ──────────────────────────────────────────────────


@dataclass
class TextDelta:
    text: str
    is_final: bool = False


@dataclass
class ThinkingDelta:
    thinking: str
    is_final: bool = False


@dataclass
class ToolCallDelta:
    """工具调用增量(完整工具调用信息,非流式)"""
    tool_name: str
    tool_input: dict
    tool_use_id: str
    is_final: bool = True  # 当前实现为完整返回,非增量


# ── Usage 统计 ─────────────────────────────────────────────────────


@dataclass
class UsageStats:
    """Token 消耗统计(统一格式,适配所有 LLM provider)"""
    input_tokens: int = 0
    output_tokens: int = 0
    thinking_tokens: int = 0
    cached_tokens: int = 0  # 从 prompt cache 命中的 token 数(Fork 压缩验证关键指标)

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens + self.thinking_tokens

    @property
    def cache_hit_rate(self) -> float:
        """Cache 命中率(cached_tokens / input_tokens),input=0 返回 0"""
        if self.input_tokens <= 0:
            return 0.0
        return self.cached_tokens / self.input_tokens

    @classmethod
    def from_chunk_usage(cls, usage_obj) -> "UsageStats":
        """
        从不同 LLM provider 的 chunk.usage 对象提取统计,自动适配字段名。

        支持的格式:
        - Anthropic:     input_tokens, output_tokens, thinking_tokens,
                         cache_creation_input_tokens, cache_read_input_tokens
        - OpenAI/GLM:    prompt_tokens, completion_tokens,
                         completion_tokens_details.reasoning_tokens,
                         prompt_tokens_details.cached_tokens
        """
        if usage_obj is None:
            return cls()

        # input_tokens / prompt_tokens
        input_tokens = _get_int(usage_obj, "input_tokens", "prompt_tokens")
        # output_tokens / completion_tokens
        output_tokens = _get_int(usage_obj, "output_tokens", "completion_tokens")
        # thinking_tokens(优先 Anthropic 格式,其次 GLM/OpenAI 嵌套格式)
        thinking_tokens = _get_int(usage_obj, "thinking_tokens")
        if not thinking_tokens:
            # GLM/OpenAI: completion_tokens_details.reasoning_tokens
            thinking_tokens = _get_nested(
                usage_obj, "completion_tokens_details", "reasoning_tokens"
            )

        # cached_tokens(prompt cache 命中数)
        # Anthropic: cache_read_input_tokens(直接字段)
        # OpenAI/GLM: prompt_tokens_details.cached_tokens(嵌套字段)
        cached_tokens = 0
        # Anthropic 优先
        anthropic_cache_read = _get_int(usage_obj, "cache_read_input_tokens")
        if anthropic_cache_read:
            cached_tokens = anthropic_cache_read
        else:
            # GLM/OpenAI: prompt_tokens_details.cached_tokens
            # 可能是 Pydantic 对象也可能是 dict,统一处理
            ptd = getattr(usage_obj, "prompt_tokens_details", None)
            if ptd is None:
                # dict fallback
                if isinstance(usage_obj, dict):
                    ptd = usage_obj.get("prompt_tokens_details")
            if ptd is not None:
                if hasattr(ptd, "cached_tokens"):
                    cached_tokens = ptd.cached_tokens or 0
                elif isinstance(ptd, dict):
                    cached_tokens = ptd.get("cached_tokens", 0) or 0

        return cls(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            thinking_tokens=thinking_tokens,
            cached_tokens=cached_tokens,
        )

    def summary(self, provider: str = "") -> str:
        """格式化统计摘要(用于日志/UI)"""
        parts = [f"in={self.input_tokens:,}", f"out={self.output_tokens:,}"]
        if self.thinking_tokens:
            parts.append(f"think={self.thinking_tokens:,}")
        if self.cached_tokens:
            parts.append(f"cached={self.cached_tokens:,} ({self.cache_hit_rate*100:.1f}%)")
        parts.append(f"total={self.total_tokens:,}")
        if provider:
            return f"[{provider}] " + " · ".join(parts)
        return " · ".join(parts)


# ── 流式响应块 ─────────────────────────────────────────────────────


@dataclass
class StreamChunk:
    """
    统一的流式响应块。
    每次 yield 其中一种 delta,最终 yield 一个带 usage 的块。
    """
    text_delta: Optional[TextDelta] = None
    thinking_delta: Optional[ThinkingDelta] = None
    tool_call: Optional[ToolCallDelta] = None  # 工具调用(完整返回,非增量)
    usage: Optional[UsageStats] = None
    # 本次响应的终止原因(流末尾 yield 一次)。统一透传 provider 原值:
    #   Anthropic: end_turn / tool_use / max_tokens / stop_sequence
    #   OpenAI 兼容: stop / tool_calls / length / content_filter
    # 上层据此判断回答是否「完整收尾」——尤其区分 max_tokens/length 截断。
    stop_reason: Optional[str] = None


__all__ = [
    "TextDelta", "ThinkingDelta", "ToolCallDelta", "UsageStats", "StreamChunk",
]
