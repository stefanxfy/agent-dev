"""
LLM Router — 统一多厂商 LLM 调用（支持流式输出）
支持：Anthropic Claude / OpenAI GPT
使用同步生成器，避免 Streamlit 的 async 复杂性。
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from enum import Enum
from typing import Generator, Optional

from pydantic import BaseModel, Field

# 内部模块
from .thinking_splitter import _ThinkTagSplitter  # noqa: F401  保留 re-export 给测试/外部使用


logger = logging.getLogger("llm.router")


# ── 枚举与配置 ────────────────────────────────────────────────────────────

class LLMProvider(str, Enum):
    ANTHROPIC = "anthropic"
    OPENAI = "openai"
    ZHIPU = "zhipu"  # 智谱 GLM
    MINIMAX = "minimax"  # MiniMax (MiniMax API,OpenAI 兼容)


class LLMModel(str, Enum):
    # Anthropic
    CLAUDE_SONNET_4 = "claude-sonnet-4-20250514"
    CLAUDE_OPUS_4 = "claude-opus-4-20250514"
    CLAUDE_HAIKU_4 = "claude-haiku-4-20250514"

    # OpenAI
    GPT_4O = "gpt-4o"
    GPT_4O_MINI = "gpt-4o-mini"
    GPT_41 = "gpt-4.1"
    O3_MINI = "o3-mini"

    # 智谱 GLM
    GLM_5 = "GLM-5.1"
    GLM_5_TURBO = "glm-5-turbo"
    GLM_4_7 = "GLM-4.7"

    # MiniMax (MiniMax 文本模型)
    # MiniMax-M3 是当前生产模型(2026 年 6 月)
    # 通过 OpenAI 兼容端点 https://api.minimaxi.com/v1 调用
    MINIMAX_M3 = "MiniMax-M3"  # 主线生产模型
    MINIMAX_TEXT_01 = "MiniMax-Text-01"  # 旧版通用文本(留作兼容)
    MINIMAX_TEXT_01_PREVIEW = "MiniMax-Text-01-preview"  # 预览版(留作兼容)


# ── Provider 元数据(给 UI / 配置使用)───────────────────────────────────
# 把每个 provider 暴露的 model 集中起来,新增 model 时只改这里,UI 自动同步。
# 不放 LLMModel enum 内是因为 enum 应该只表达"模型名",UI 列表是另一回事。

MODELS_BY_PROVIDER: dict[LLMProvider, list[str]] = {
    LLMProvider.ANTHROPIC: [
        LLMModel.CLAUDE_SONNET_4,
        LLMModel.CLAUDE_OPUS_4,
        LLMModel.CLAUDE_HAIKU_4,
    ],
    LLMProvider.OPENAI: [
        LLMModel.GPT_4O,
        LLMModel.GPT_4O_MINI,
        LLMModel.GPT_41,
        LLMModel.O3_MINI,
    ],
    LLMProvider.ZHIPU: [
        LLMModel.GLM_5,
        LLMModel.GLM_5_TURBO,
        LLMModel.GLM_4_7,
    ],
    LLMProvider.MINIMAX: [
        LLMModel.MINIMAX_M3,
        LLMModel.MINIMAX_TEXT_01,
        LLMModel.MINIMAX_TEXT_01_PREVIEW,
    ],
}

# 每个 provider 对应的 API Key 环境变量名(从 agent_core.config.ENV_VAR_REGISTRY 推得)
PROVIDER_ENV_KEY: dict[LLMProvider, str] = {
    LLMProvider.ANTHROPIC: "ANTHROPIC_API_KEY",
    LLMProvider.OPENAI: "OPENAI_API_KEY",
    LLMProvider.ZHIPU: "ZHIPU_API_KEY",
    LLMProvider.MINIMAX: "MINIMAX_API_KEY",
}


class ThinkingConfig(BaseModel):
    """Claude 扩展思考配置"""
    enabled: bool = True
    budget_tokens: int = 1024


class LLMConfig(BaseModel):
    """LLM 路由配置"""
    provider: LLMProvider = LLMProvider.ANTHROPIC
    model: str = LLMModel.CLAUDE_SONNET_4
    api_key: str = ""
    base_url: Optional[str] = None
    max_tokens: int = 4096
    temperature: float = 0.7
    thinking: Optional[ThinkingConfig] = None
    stream: bool = True
    system_prompt: Optional[str] = None  # P2 新增：系统提示词


# ── 流式响应块定义 ─────────────────────────────────────────────────────────

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
    """工具调用增量（完整工具调用信息，非流式）"""
    tool_name: str
    tool_input: dict
    tool_use_id: str
    is_final: bool = True  # 当前实现为完整返回，非增量


def _get_int(obj, *attrs, default=0):
    """从 Pydantic model 或 dict 中取第一个存在的整数字段"""
    for attr in attrs:
        val = getattr(obj, attr, None) if hasattr(obj, attr) else obj.get(attr) if isinstance(obj, dict) else None
        if val is not None:
            return int(val)
    return default


def _get_nested(obj, *attrs, default=0):
    """取嵌套属性：先取中间对象，再取目标字段（Pydantic+dict 混合）"""
    cur = obj
    for attr in attrs:
        if cur is None:
            return default
        cur = getattr(cur, attr, None) if hasattr(cur, attr) else cur.get(attr) if isinstance(cur, dict) else None
    if cur is None:
        return default
    return int(cur) if isinstance(cur, (int, float)) else default


# ── P1-8 / P1-9 错误分类与重试策略 ─────────────────────────────
#
# 不同 provider 的错误语义不一样，必须区分：
# - 400 (Bad Request): 客户端错误，不重试（重试也是同样错）
#   特殊情况：MiniMax 等小厂商可能因格式问题返回 400 但实际可重试
#   （例如：参数顺序、空字符串等）→ 视为"可重试 1 次"兜底
# - 401/403: 鉴权错误，不重试
# - 404: 模型不存在，不重试
# - 429 (Rate Limit): 限流，重试（尊重 Retry-After）
# - 500/502/503/504: 服务端错误，重试
# - 408 (Request Timeout): 重试
# - Stream 中断（IncompleteRead / ConnectionError / chunked_encoding_error）：
#   P1-9 修复：GLM-5.1 实测偶发 stream 截断，重试 + 上限 2 次
#
# 重试退避：指数退避 0.5s → 1s → 2s，最多 3 次

import time as _time

RETRYABLE_STATUS_CODES = frozenset({408, 429, 500, 502, 503, 504})
# 某些小厂商（如 MiniMax）400 也可能因临时格式问题触发，单独标记
SOFT_RETRYABLE_STATUS_CODES = frozenset({400})  # 仅 1 次重试
NON_RETRYABLE_STATUS_CODES = frozenset({401, 403, 404, 422})

MAX_STREAM_RETRY = 2
MAX_REQUEST_RETRY = 3
RETRY_BACKOFF_BASE = 0.5  # 秒

# E-1 修复：env 变量集中管理后的默认值引用
# 实际值可通过 LLM_MAX_STREAM_RETRY / LLM_MAX_REQUEST_RETRY / LLM_RETRY_BACKOFF_BASE 覆盖
# (运行时由 config.llm_max_stream_retry 等访问)


def _classify_http_error(status: int) -> str:
    """分类 HTTP 错误 → 'retry' / 'soft_retry' / 'fail'

    Returns:
        'retry': 正常重试
        'soft_retry': 仅 1 次重试（小厂商的 400 兜底）
        'fail': 不重试，直接抛给上层
    """
    if status in RETRYABLE_STATUS_CODES:
        return "retry"
    if status in SOFT_RETRYABLE_STATUS_CODES:
        return "soft_retry"
    return "fail"


def _is_stream_interruption_error(exc: Exception) -> bool:
    """判断是否是 stream 中断类错误（P1-9 触发条件）

    触发场景：
    - http.client.IncompleteRead: GLM-5.1 实测偶发
    - requests.exceptions.ChunkedEncodingError
    - urllib3.ProtocolError
    - ConnectionError / ConnectionResetError
    """
    type_name = type(exc).__name__
    if type_name in (
        "IncompleteRead",
        "ChunkedEncodingError",
        "ProtocolError",
        "RemoteDisconnected",
        "ConnectionResetError",
        "ConnectionAbortedError",
        "BrokenPipeError",
    ):
        return True
    # 字符串兜底（防止 provider 包装了异常）
    msg = str(exc).lower()
    if any(
        s in msg
        for s in (
            "incomplete read",
            "connection broken",
            "connection reset",
            "server disconnected",
            "stream is closed",
        )
    ):
        return True
    return False


@dataclass
class UsageStats:
    """Token 消耗统计（统一格式，适配所有 LLM provider）"""
    input_tokens: int = 0
    output_tokens: int = 0
    thinking_tokens: int = 0
    cached_tokens: int = 0  # 从 prompt cache 命中的 token 数（Fork 压缩验证关键指标）

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens + self.thinking_tokens

    @property
    def cache_hit_rate(self) -> float:
        """Cache 命中率（cached_tokens / input_tokens），input=0 返回 0"""
        if self.input_tokens <= 0:
            return 0.0
        return self.cached_tokens / self.input_tokens

    @classmethod
    def from_chunk_usage(cls, usage_obj) -> "UsageStats":
        """
        从不同 LLM provider 的 chunk.usage 对象提取统计，自动适配字段名。

        支持的格式：
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
        # thinking_tokens（优先 Anthropic 格式，其次 GLM/OpenAI 嵌套格式）
        thinking_tokens = _get_int(usage_obj, "thinking_tokens")
        if not thinking_tokens:
            # GLM/OpenAI: completion_tokens_details.reasoning_tokens
            thinking_tokens = _get_nested(
                usage_obj, "completion_tokens_details", "reasoning_tokens"
            )

        # cached_tokens（prompt cache 命中数）
        # Anthropic: cache_read_input_tokens（直接字段）
        # OpenAI/GLM: prompt_tokens_details.cached_tokens（嵌套字段）
        cached_tokens = 0
        # Anthropic 优先
        anthropic_cache_read = _get_int(usage_obj, "cache_read_input_tokens")
        if anthropic_cache_read:
            cached_tokens = anthropic_cache_read
        else:
            # GLM/OpenAI: prompt_tokens_details.cached_tokens
            # 可能是 Pydantic 对象也可能是 dict，统一处理
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
        """格式化统计摘要（用于日志/UI）"""
        parts = [f"in={self.input_tokens:,}", f"out={self.output_tokens:,}"]
        if self.thinking_tokens:
            parts.append(f"think={self.thinking_tokens:,}")
        if self.cached_tokens:
            parts.append(f"cached={self.cached_tokens:,} ({self.cache_hit_rate*100:.1f}%)")
        parts.append(f"total={self.total_tokens:,}")
        if provider:
            return f"[{provider}] " + " · ".join(parts)
        return " · ".join(parts)


@dataclass
class StreamChunk:
    """
    统一的流式响应块。
    每次 yield 其中一种 delta，最终 yield 一个带 usage 的块。
    """
    text_delta: Optional[TextDelta] = None
    thinking_delta: Optional[ThinkingDelta] = None
    tool_call: Optional[ToolCallDelta] = None  # 工具调用（完整返回，非增量）
    usage: Optional[UsageStats] = None
    # 本次响应的终止原因(流末尾 yield 一次)。统一透传 provider 原值:
    #   Anthropic: end_turn / tool_use / max_tokens / stop_sequence
    #   OpenAI 兼容: stop / tool_calls / length / content_filter
    # 上层据此判断回答是否「完整收尾」——尤其区分 max_tokens/length 截断。
    stop_reason: Optional[str] = None


# ── LLM Router ───────────────────────────────────────────────────────────────

class LLMRouter:
    """
    多厂商 LLM 统一调用路由。
    使用同步生成器（Streamlit 兼容更好）。
    """

    def __init__(self, config: LLMConfig):
        self.config = config
        self._anthropic_client = None  # 懒加载,见 _get_anthropic_client()
        # OpenAI 兼容 provider 的 client 在 OpenAICompatibleProvider 里懒加载
        # (OpenAI / Zhipu / MiniMax 各有独立实例,由 _get_openai_provider() 创建)

    # ── 懒加载客户端 ────────────────────────────────────────────────────────

    def _get_anthropic_client(self):
        if self._anthropic_client is None:
            import anthropic
            # E-1 修复：使用集中式 config 访问器（不再散落 os.getenv）
            from ..config import config as _config
            api_key = self.config.api_key or _config.anthropic_api_key
            self._anthropic_client = anthropic.Anthropic(api_key=api_key)
        return self._anthropic_client

    # ── 流式调用入口 ─────────────────────────────────────────────────────────

    def chat(
        self,
        messages: list[dict],
        tools: Optional[list[dict]] = None,
        system_prompt_override: Optional[str] = None,
        tool_choice: Optional[str] = None,
        cache_namespace: Optional[str] = None,
    ) -> Generator[StreamChunk, None, None]:
        """
        统一 chat 接口，返回 StreamChunk 生成器（同步）。
        根据 provider 路由到不同厂商。

        特殊处理 system message：
        - Anthropic: 提取 system 作为顶层参数
        - OpenAI/Zhipu: 保留在 messages 中

        Args:
            messages: 对话消息列表
            tools: 工具 schema 列表
            system_prompt_override: Fork 模式下覆盖 system prompt。
                仿照 Claude Code createCacheSafeParams，Fork Agent 必须
                使用主 agent 的 system prompt 字节才能命中 prompt cache。
                传入时，忽略 messages 中的 system message，用此值替代。
            tool_choice: 工具选择策略。None=auto（默认）、"none"=禁用工具调用、
                "auto"=同 None、"required"=必须调用工具。
                压缩场景下传 "none" 防止 LLM 调工具打破输出格式。
            cache_namespace: M7 新增——prompt cache 命名空间。
                Anthropic:在 system message + tools 上打 cache_control:{"type":"ephemeral"},
                          后续相同 namespace 跨调用可命中 prompt cache。
                OpenAI/GLM:暂不支持,记 warning log,不影响正常调用。
        """
        provider = self.config.provider.value if isinstance(self.config.provider, Enum) else self.config.provider

        # 提取 system message
        # Fork 模式：system_prompt_override 优先（保证字节级一致）
        system_message = system_prompt_override
        filtered_messages = []
        for m in messages:
            if m.get("role") == "system":
                if system_prompt_override is None:
                    # 非 Fork 模式：从 messages 中提取 system
                    system_message = m.get("content", "")
                # Fork 模式：跳过 messages 中的 system（用 override）
            else:
                filtered_messages.append(m)

        if provider == "anthropic":
            yield from self._stream_with_retry(
                lambda: self._chat_anthropic(filtered_messages, tools, system_message, tool_choice, cache_namespace),
                provider=provider,
            )
        elif provider in ("openai", "zhipu", "minimax"):
            # 3 个 OpenAI 兼容 provider 共用同一调度路径
            # OpenAI 兼容协议保留 system 在 messages 中
            # Fork 模式：需要用 override 替换/注入 system
            # ⚠️ Bug 修复：使用真值判断（`if system_prompt_override`）而不是 `is not None`
            #    空字符串也是 is not None True，会被注入空 system message，
            #    破坏 cache prefix 对齐（主 agent 路径空 system_prompt 时不发送 system）
            final_messages = messages
            if system_prompt_override:
                final_messages = [{"role": "system", "content": system_prompt_override}]
                final_messages.extend(filtered_messages)
            if cache_namespace:
                if provider == "openai":
                    logger.warning(
                        "cache_namespace=%r passed but openai provider doesn't "
                        "support explicit cache_control; implicit caching may apply",
                        cache_namespace,
                    )
                elif provider == "zhipu":
                    logger.warning(
                        "cache_namespace=%r passed but zhipu doesn't support "
                        "cache_control; ignored",
                        cache_namespace,
                    )
                else:  # minimax
                    # 实测(2026-06-24 smoke test):MiniMax-M3 支持 implicit prompt cache
                    # (cached=114 / input=186 = 61.3% 命中率),不需要显式 cache_control 块
                    logger.info(
                        "cache_namespace=%r passed to minimax; implicit caching may apply",
                        cache_namespace,
                    )
            openai_provider = self._get_openai_provider()
            yield from self._stream_with_retry(
                lambda: openai_provider.chat(final_messages, tools, tool_choice),
                provider=provider,
            )
        else:
            raise ValueError(f"不支持的厂商: {self.config.provider}")

    def _get_openai_provider(self):
        """懒加载当前 provider 对应的 OpenAICompatibleProvider 实例。

        替换原 _get_openai_client / _get_zhipu_client / _get_minimax_client 三个方法,
        统一收敛到一个分发点。
        """
        from .openai_compatible import create_openai_compatible_provider
        return create_openai_compatible_provider(self.config)

    def _stream_with_retry(
        self,
        stream_fn,
        provider: str,
    ) -> Generator:
        """
        P1-8 / P1-9 修复：流式调用统一重试包装

        处理两类错误：
        1. HTTP 错误（_chat_* 抛 openai.BadRequestError 等）：
           - 4xx (除 408/429) → 不重试，直接抛
           - 5xx / 408 / 429 → 重试，指数退避
           - 小厂商 400 兜底：仅 1 次重试（P1-8 MiniMax）
        2. Stream 中断（生成器内部抛 IncompleteRead 等）：
           - P1-9 修复：GLM-5.1 偶发 stream 截断 → 重试 + 上限 2 次

        Args:
            stream_fn: 返回 StreamChunk 生成器的函数
            provider: 用于日志
        """
        # 缓存已 yield 的 chunk，以便重试时不会重复输出
        # 注意：重试只能"从头开始"，因此丢弃所有已 yield 的内容
        # 这是 streaming + retry 的固有限制：上游需要容忍少量重复内容
        # E-1 修复：env 变量集中管理（默认仍是 MAX_STREAM_RETRY/MAX_REQUEST_RETRY）
        from ..config import config as _config
        max_stream_retry = _config.llm_max_stream_retry
        max_request_retry = _config.llm_max_request_retry
        backoff_base = _config.llm_retry_backoff_base

        # 阶段 1: 整体请求级重试（捕获生成器内部未抛出的 HTTP 错误）
        for req_attempt in range(max_request_retry + 1):
            stream_interrupted = False
            try:
                # 阶段 2: 流式中断重试（仅在生成器内部抛异常时触发）
                for stream_attempt in range(max_stream_retry + 1):
                    try:
                        for chunk in stream_fn():
                            yield chunk
                        return  # 成功
                    except Exception as e:
                        if _is_stream_interruption_error(e):
                            if stream_attempt < max_stream_retry:
                                backoff = backoff_base * (2 ** stream_attempt)
                                logger.warning(
                                    f"🔄 [{provider}] stream interrupted "
                                    f"(attempt {stream_attempt + 1}/{max_stream_retry}): {e}, "
                                    f"retrying in {backoff:.1f}s"
                                )
                                _time.sleep(backoff)
                                continue
                        # 非中断错误或重试耗尽 → 抛给外层请求级重试
                        raise
            except Exception as e:
                # 尝试分类 HTTP 错误
                status = getattr(e, "status_code", None) or getattr(e, "code", None)
                if status is None:
                    # 尝试从 openai SDK 的错误对象取
                    status = getattr(getattr(e, "response", None), "status_code", None)

                if status is not None:
                    classification = _classify_http_error(int(status))
                    if classification == "fail":
                        logger.error(
                            f"❌ [{provider}] HTTP {status} (no retry): {e}"
                        )
                        raise
                    if classification == "soft_retry" and req_attempt >= 1:
                        logger.error(
                            f"❌ [{provider}] HTTP {status} soft-retry exhausted: {e}"
                        )
                        raise
                    if req_attempt < max_request_retry:
                        backoff = backoff_base * (2 ** req_attempt)
                        logger.warning(
                            f"🔄 [{provider}] HTTP {status} (attempt {req_attempt + 1}/"
                            f"{max_request_retry}): {e}, retrying in {backoff:.1f}s"
                        )
                        _time.sleep(backoff)
                        continue
                # 非 HTTP 错误 或 重试耗尽 → 抛给上层
                raise

    # ── Anthropic 流式实现 ─────────────────────────────────────────────────

    def _chat_anthropic(
        self,
        messages: list[dict],
        tools: Optional[list[dict]],
        system_message: Optional[str] = None,  # P2 新增：system prompt
        tool_choice: Optional[str] = None,    # Fork 压缩：传 "none" 禁调工具
        cache_namespace: Optional[str] = None,  # M7 新增：prompt cache 命名空间
    ) -> Generator[StreamChunk, None, None]:
        """Anthropic Claude 流式调用（同步，支持 thinking blocks）"""
        client = self._get_anthropic_client()

        kwargs: dict = {
            "model": self.config.model,
            "max_tokens": self.config.max_tokens,
            "messages": messages,
        }
        if tools:
            kwargs["tools"] = tools
            # tool_choice 必须在传 tools 时才有效
            # Anthropic 接受: "auto" / "any" / "tool" / "none"
            if tool_choice == "none":
                kwargs["tool_choice"] = {"type": "none"}
            elif tool_choice == "auto":
                kwargs["tool_choice"] = {"type": "auto"}
        if self.config.temperature > 0:
            kwargs["temperature"] = self.config.temperature

        # P2 新增：添加 system prompt（Anthropic 特有格式）
        # M7 扩展：cache_namespace 非空时,把 system 打包成 content block 并打 cache_control
        if system_message:
            if cache_namespace:
                kwargs["system"] = [{
                    "type": "text",
                    "text": system_message,
                    "cache_control": {"type": "ephemeral"},
                }]
            else:
                kwargs["system"] = system_message

        # M7：cache_namespace 非空时,在最后一个 tool 上打 cache_control 锚点
        # （Anthropic 限制：最多 4 个 cache_control 块,默认放最后一个即可）
        if cache_namespace and tools:
            tools = [dict(t) for t in tools]  # 浅拷贝避免污染
            tools[-1] = {**tools[-1], "cache_control": {"type": "ephemeral"}}
            kwargs["tools"] = tools

        # 开启思考过程（Claude 3.7+）
        if self.config.thinking and self.config.thinking.enabled:
            kwargs["thinking"] = {
                "type": "enabled",
                "budget_tokens": self.config.thinking.budget_tokens,
            }

        # 使用 stream() 上下文管理器（同步）
        with client.messages.stream(**kwargs) as stream:
            # text_stream 是同步生成器，逐 token 输出
            for text_delta in stream.text_stream:
                if text_delta:
                    yield StreamChunk(text_delta=TextDelta(text=text_delta))

            # 获取完整消息（包含 thinking blocks 和 tool_use blocks）
            final_message = stream.get_final_message()

            # 提取 thinking blocks（如果有）
            for block in final_message.content:
                if block.type == "thinking":
                    thinking_text = getattr(block, "thinking", "")
                    if thinking_text:
                        yield StreamChunk(
                            thinking_delta=ThinkingDelta(
                                thinking=thinking_text,
                                is_final=True,
                            )
                        )

            # 提取 tool_use blocks（如果有）←── 新增：用于 ReAct 工具调用
            for block in final_message.content:
                if block.type == "tool_use":
                    yield StreamChunk(
                        tool_call=ToolCallDelta(
                            tool_name=block.name,
                            tool_input=dict(block.input),  # Anthropic SDK 的 input 是特殊类型，转 dict
                            tool_use_id=block.id,
                            is_final=True,
                        )
                    )

            # 返回 Token 消耗 —— M7 修复:补 cached_tokens(Anthropic cache_read_input_tokens)
            cached = getattr(final_message.usage, "cache_read_input_tokens", 0)
            usage = UsageStats(
                input_tokens=final_message.usage.input_tokens,
                output_tokens=final_message.usage.output_tokens,
                thinking_tokens=getattr(
                    final_message.usage, "thinking_tokens", 0
                ),
                cached_tokens=cached,
            )
            yield StreamChunk(usage=usage)

            # 终止原因(Anthropic): end_turn / tool_use / max_tokens / stop_sequence
            stop_reason = getattr(final_message, "stop_reason", None)
            if stop_reason:
                yield StreamChunk(stop_reason=stop_reason)

# ── OpenAI 兼容 provider 已抽到 openai_compatible.py ──────────────────
# (OpenAI / Zhipu / MiniMax 走 OpenAICompatibleProvider 基类 + 3 个子类)
# 客户端懒加载在 OpenAICompatibleProvider.client 属性里,
# 初始化逻辑在对应子类的 _resolve_api_key() + default_base_url 里。
# 增加第 4 个 OpenAI 兼容 provider 只需新建一个 ~20 行的子类。


# ── 便捷工厂函数 ────────────────────────────────────────────────────────────

def create_router(
    provider: str = "anthropic",
    model: str = "",
    api_key: str = "",
    **kwargs,
) -> LLMRouter:
    """快速创建 LLM Router"""
    from enum import Enum as _Enum
    config = LLMConfig(
        provider=LLMProvider(provider),
        model=model or _default_model(provider),
        api_key=api_key,
        **kwargs,
    )
    return LLMRouter(config)


def _default_model(provider: str) -> str:
    """从环境变量读取默认模型，没有则返回空字符串"""
    # E-1 修复：使用集中式 config 访问器
    from ..config import config as _config
    if _config.default_provider.lower() == provider:
        return _config.default_model
    return ""
