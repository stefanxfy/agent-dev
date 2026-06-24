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


class _ThinkTagSplitter:
    """把含 `` 标签的 text 切成 (text_delta, thinking_delta) 序列

    背景：MiniMax-M3 等 provider 把 thinking 内容直接放在
    text_delta 里(包在 <think>...</think> 标签中),不像 GLM 那样有
    独立的 reasoning_content 字段。本 splitter 状态机把这种
    格式转成统一的 StreamChunk.thinking_delta,让 UI 能区分显示。

    状态机:
        NORMAL  ──(见 <think>)──▶ THINKING
        THINKING ──(见 </think>)──▶ NORMAL

    关键能力:
    1. 标签跨 chunk 切片:`<thi` + `nk>...` + `</thin` + `king>`
       都能正确解析(用 _buf 缓冲未完成的部分)
    2. 多对标签:`<think>a</think> hello <think>b</think> world`
    3. 流末尾收尾:flush() 兜底未完成的缓冲区
    4. 失败降级:任何异常路径都保留原文(不丢内容)

    简单嵌套(<think><think>x</think></think>)不处理 — MiniMax
    实际输出没有嵌套,过度工程化无收益。
    """
    OPEN_TAG = "<think>"
    CLOSE_TAG = "</think>"
    NORMAL = "normal"
    THINKING = "thinking"

    def __init__(self) -> None:
        self._state = self.NORMAL
        self._buf = ""  # 缓冲可能是不完整标签的后缀

    def feed(self, text: str) -> list[StreamChunk]:
        """喂入一段 text,产出 0+ 个 StreamChunk(可能是空的,如果还在缓冲)

        Streaming 策略:每 chunk emit 已确定的内容(不卡整段),
        只缓冲最后 6 字符(可能是不完整的 <think> / </think>)。
        """
        if not text:
            return []
        s = self._buf + text
        self._buf = ""
        out: list[StreamChunk] = []
        i = 0
        while i < len(s):
            tag = self.OPEN_TAG if self._state == self.NORMAL else self.CLOSE_TAG
            idx = s.find(tag, i)
            if idx == -1:
                # 没找到完整标签 — 保留最后 6 字符在 _buf(可能是不完整标签)
                # 其余 emit 出去(保留 streaming 体验)
                tail = s[i:]
                keep = len(tag) - 1  # 6
                if len(tail) > keep:
                    out.append(self._emit(tail[:-keep]))
                    self._buf = tail[-keep:]
                else:
                    self._buf = tail
                break
            else:
                # 标签前的内容
                if idx > i:
                    out.append(self._emit(s[i:idx]))
                i = idx + len(tag)
                self._state = self.THINKING if self._state == self.NORMAL else self.NORMAL
                # </think> 后跳过一个换行(MiniMax 实测紧跟 \n)
                if (self._state == self.NORMAL
                        and i < len(s)
                        and s[i] == "\n"):
                    i += 1
        return out

    def flush(self) -> list[StreamChunk]:
        """流结束时调用,把残留 buffer 兜底输出(不丢内容)"""
        if not self._buf:
            return []
        chunk = self._emit(self._buf)
        self._buf = ""
        return [chunk]

    def _emit(self, text: str) -> StreamChunk:
        if self._state == self.THINKING:
            return StreamChunk(thinking_delta=ThinkingDelta(thinking=text))
        return StreamChunk(text_delta=TextDelta(text=text))

    def _looks_like_partial_tag(self, s: str) -> bool:
        """s 末尾是否可能是 OPEN_TAG 或 CLOSE_TAG 的前缀(需要继续缓冲)"""
        for tag in (self.OPEN_TAG, self.CLOSE_TAG):
            for k in range(1, len(tag)):
                if s.endswith(tag[:k]):
                    return True
        return False


# ── LLM Router ───────────────────────────────────────────────────────────────

class LLMRouter:
    """
    多厂商 LLM 统一调用路由。
    使用同步生成器（Streamlit 兼容更好）。
    """

    def __init__(self, config: LLMConfig):
        self.config = config
        self._anthropic_client = None
        self._openai_client = None
        self._zhipu_client = None
        self._minimax_client = None  # MiniMax (OpenAI 兼容)

    # ── 懒加载客户端 ────────────────────────────────────────────────────────

    def _get_anthropic_client(self):
        if self._anthropic_client is None:
            import anthropic
            # E-1 修复：使用集中式 config 访问器（不再散落 os.getenv）
            from ..config import config as _config
            api_key = self.config.api_key or _config.anthropic_api_key
            self._anthropic_client = anthropic.Anthropic(api_key=api_key)
        return self._anthropic_client

    def _get_openai_client(self):
        if self._openai_client is None:
            import openai
            from ..config import config as _config
            api_key = self.config.api_key or _config.openai_api_key
            kwargs = {"api_key": api_key}
            if self.config.base_url:
                kwargs["base_url"] = self.config.base_url
            self._openai_client = openai.OpenAI(**kwargs)
        return self._openai_client

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
        elif provider == "openai":
            # OpenAI/Zhipu 保留 system 在 messages 中
            # Fork 模式：需要用 override 替换/注入 system
            # ⚠️ Bug 修复：使用真值判断（`if system_prompt_override`）而不是 `is not None`
            #    空字符串也是 is not None True，会被注入空 system message，
            #    破坏 cache prefix 对齐（主 agent 路径空 system_prompt 时不发送 system）
            final_messages = messages
            if system_prompt_override:
                final_messages = [{"role": "system", "content": system_prompt_override}]
                final_messages.extend(filtered_messages)
            if cache_namespace:
                logger.warning(
                    "cache_namespace=%r passed but openai provider doesn't "
                    "support explicit cache_control; implicit caching may apply",
                    cache_namespace,
                )
            yield from self._stream_with_retry(
                lambda: self._chat_openai(final_messages, tools, tool_choice),
                provider=provider,
            )
        elif provider == "zhipu":
            final_messages = messages
            if system_prompt_override:
                final_messages = [{"role": "system", "content": system_prompt_override}]
                final_messages.extend(filtered_messages)
            if cache_namespace:
                logger.warning(
                    "cache_namespace=%r passed but zhipu doesn't support "
                    "cache_control; ignored",
                    cache_namespace,
                )
            yield from self._stream_with_retry(
                lambda: self._chat_zhipu(final_messages, tools, tool_choice),
                provider=provider,
            )
        elif provider == "minimax":
            # MiniMax:OpenAI 兼容,与 zhipu 行为对齐
            # 实测(2026-06-24 smoke test):MiniMax-M3 支持 implicit prompt cache
            # (cached=114 / input=186 = 61.3% 命中率),不需要显式 cache_control 块
            final_messages = messages
            if system_prompt_override:
                final_messages = [{"role": "system", "content": system_prompt_override}]
                final_messages.extend(filtered_messages)
            if cache_namespace:
                # explicit cache_control 块暂未验证;只记 info,不影响调用
                logger.info(
                    "cache_namespace=%r passed to minimax; implicit caching may apply",
                    cache_namespace,
                    cache_namespace,
                )
            yield from self._stream_with_retry(
                lambda: self._chat_minimax(final_messages, tools, tool_choice),
                provider=provider,
            )
        else:
            raise ValueError(f"不支持的厂商: {self.config.provider}")

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

    # ── OpenAI 流式实现 ────────────────────────────────────────────────────

    def _chat_openai(
        self,
        messages: list[dict],
        tools: Optional[list[dict]],
        tool_choice: Optional[str] = None,  # Fork 压缩：传 "none" 禁调工具
    ) -> Generator[StreamChunk, None, None]:
        """OpenAI GPT 流式调用（同步，支持 tool_calls 解析）"""
        client = self._get_openai_client()

        kwargs: dict = {
            "model": self.config.model,
            "max_tokens": self.config.max_tokens,
            "messages": messages,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if tools:
            kwargs["tools"] = [{"type": "function", "function": t} for t in tools]
            if tool_choice is not None:
                # OpenAI 接受: "auto" / "none" / "required" / {"type": "function", "function": {"name": "..."}}
                kwargs["tool_choice"] = tool_choice
        if self.config.temperature > 0:
            kwargs["temperature"] = self.config.temperature

        stream = client.chat.completions.create(**kwargs)

        # 收集 tool_calls（流式响应中可能分散在多个 chunk）
        tool_calls_buffer = {}  # index -> {"id": ..., "name": ..., "arguments": ...}

        for chunk in stream:
            delta = chunk.choices[0].delta

            # 文本增量
            if delta.content:
                yield StreamChunk(text_delta=TextDelta(text=delta.content))

            # 工具调用增量（OpenAI 格式）
            if delta.tool_calls:
                for tc in delta.tool_calls:
                    idx = tc.index
                    if idx not in tool_calls_buffer:
                        tool_calls_buffer[idx] = {"id": "", "name": "", "arguments": ""}
                    if tc.id:
                        tool_calls_buffer[idx]["id"] = tc.id
                    if tc.function and tc.function.name:
                        tool_calls_buffer[idx]["name"] = tc.function.name
                    if tc.function and tc.function.arguments:
                        tool_calls_buffer[idx]["arguments"] += tc.function.arguments

            # Token 消耗（最后一个 chunk 携带，自动适配字段名）
            if hasattr(chunk, "usage") and chunk.usage:
                usage = UsageStats.from_chunk_usage(chunk.usage)
                logger.debug(f"[OpenAI] usage: {usage.summary('OpenAI')}")
                yield StreamChunk(usage=usage)

        # 流式结束后，yield 完整的 thinking（GLM reasoning_content）
        if reasoning_buffer:
            full_thinking = "".join(reasoning_buffer)
            yield StreamChunk(thinking_delta=ThinkingDelta(thinking=full_thinking))

        # 流式结束后，如果有完整的 tool_calls，yield 它们
        for idx in sorted(tool_calls_buffer.keys()):
            tc = tool_calls_buffer[idx]
            if tc["id"] and tc["name"]:
                import json
                try:
                    tool_input = json.loads(tc["arguments"]) if tc["arguments"] else {}
                except json.JSONDecodeError:
                    tool_input = {"raw_arguments": tc["arguments"]}
                yield StreamChunk(
                    tool_call=ToolCallDelta(
                        tool_name=tc["name"],
                        tool_input=tool_input,
                        tool_use_id=tc["id"],
                        is_final=True,
                    )
                )

    # ── 智谱 GLM 流式实现 ───────────────────────────────────────────────

    def _get_zhipu_client(self):
        """获取智谱客户端（Coding Plan 专用，复用 Anthropic 兼容协议）"""
        if self._zhipu_client is None:
            import openai
            from ..config import config as _config
            api_key = self.config.api_key or _config.zhipu_api_key
            # Coding Plan 专用端点
            base_url = self.config.base_url or "https://open.bigmodel.cn/api/coding/paas/v4"
            self._zhipu_client = openai.OpenAI(
                api_key=api_key,
                base_url=base_url,
            )
        return self._zhipu_client

    def _get_minimax_client(self):
        """获取 MiniMax 客户端（OpenAI 兼容端点,文档:platform.minimaxi.com/docs/api-reference/text-openai-api）

        注意:URL 路径名是 text-openai-api,说明其 chat completions 端点完全
        兼容 OpenAI 协议,直接用 openai.OpenAI SDK + base_url 覆盖即可。
        401/429 等 HTTP 错误由 _stream_with_retry 统一处理。
        """
        if self._minimax_client is None:
            import openai
            from ..config import config as _config
            api_key = self.config.api_key or _config.minimax_api_key
            # 官方文档 base_url:https://api.minimaxi.com/v1
            # 也可由 LLMConfig.base_url 覆盖(便于私有部署 / 代理)
            base_url = self.config.base_url or "https://api.minimaxi.com/v1"
            self._minimax_client = openai.OpenAI(
                api_key=api_key,
                base_url=base_url,
            )
        return self._minimax_client

    def _chat_zhipu(
        self,
        messages: list[dict],
        tools: Optional[list[dict]],
        tool_choice: Optional[str] = None,  # Fork 压缩：传 "none" 禁调工具
    ) -> Generator[StreamChunk, None, None]:
        """智谱 GLM 流式调用（Coding Plan 专用端点）"""
        client = self._get_zhipu_client()

        # 转换 messages 格式：智谱要求 role 为小写，content 为 string
        zhipu_messages = []
        for m in messages:
            role = m["role"].lower()
            content = m["content"]
            # 如果 content 是 list（Anthropic 格式），转换为 string
            if isinstance(content, list):
                # 提取 text 内容，忽略 tool_result 等
                text_parts = []
                for item in content:
                    if isinstance(item, dict):
                        if item.get("type") == "text":
                            text_parts.append(item.get("text", ""))
                        elif item.get("type") == "tool_result":
                            # tool_result 转为文本描述
                            text_parts.append(f"[Tool Result: {item.get('content', '')}]")
                content = "\n".join(text_parts)
            zhipu_messages.append({"role": role, "content": content})

        kwargs: dict = {
            "model": self.config.model,
            "messages": zhipu_messages,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if tools:
            # Zhipu 需要 OpenAI 格式：{"type": "function", "function": {...}}
            # 输入的 tools 可能是 Anthropic 格式，需要转换
            openai_tools = []
            for t in tools:
                if "type" in t and t.get("type") == "function":
                    # 已经是 OpenAI 格式
                    openai_tools.append(t)
                else:
                    # Anthropic 格式：{"name": ..., "description": ..., "input_schema": ...}
                    openai_tools.append({
                        "type": "function",
                        "function": {
                            "name": t.get("name", ""),
                            "description": t.get("description", ""),
                            "parameters": t.get("input_schema", {}),
                        }
                    })
            kwargs["tools"] = openai_tools
            if tool_choice is not None:
                # GLM (OpenAI 兼容) 接受: "auto" / "none" / "required"
                kwargs["tool_choice"] = tool_choice
        if self.config.temperature > 0:
            kwargs["temperature"] = self.config.temperature

        stream = client.chat.completions.create(**kwargs)

        # 收集 tool_calls（流式响应中可能分散在多个 chunk）
        tool_calls_buffer = {}  # index -> {"id": ..., "name": ..., "arguments": ...}

        # P3 优化：GLM thinking 改为实时流式 yield（之前缓存后在流结束一次性 yield，
        # 导致 UI 看到'先出回答，后出思考'。现在逐块 yield 就能真正流式）
        for chunk in stream:
            if chunk.choices:
                delta = chunk.choices[0].delta

                # 文本增量
                if delta.content:
                    yield StreamChunk(text_delta=TextDelta(text=delta.content))

                # GLM thinking 实时流式（reasoning_content 字段逐块到达）
                # 与 Anthropic 不同：Anthropic SDK 不提供 thinking streaming delta，
                # 只能在 get_final_message() 后一次性拿到。GLM 原生支持流式。
                if hasattr(delta, 'reasoning_content') and delta.reasoning_content:
                    yield StreamChunk(
                        thinking_delta=ThinkingDelta(thinking=delta.reasoning_content)
                    )
                # 工具调用增量（OpenAI 格式）
                if delta.tool_calls:
                    for tc in delta.tool_calls:
                        idx = tc.index
                        if idx not in tool_calls_buffer:
                            tool_calls_buffer[idx] = {"id": "", "name": "", "arguments": ""}
                        if tc.id:
                            tool_calls_buffer[idx]["id"] = tc.id
                        if tc.function and tc.function.name:
                            tool_calls_buffer[idx]["name"] = tc.function.name
                        if tc.function and tc.function.arguments:
                            tool_calls_buffer[idx]["arguments"] += tc.function.arguments

            if hasattr(chunk, "usage") and chunk.usage:
                usage = UsageStats.from_chunk_usage(chunk.usage)
                yield StreamChunk(usage=usage)

        # P3：thinking 已在流式循环内逐块 yield（router.py:557-563），
        # 这里不再需要流结束后一次性 yield。

        # 流式结束后，如果有完整的 tool_calls，yield 它们
        for idx in sorted(tool_calls_buffer.keys()):
            tc = tool_calls_buffer[idx]
            if tc["id"] and tc["name"]:
                import json
                try:
                    tool_input = json.loads(tc["arguments"]) if tc["arguments"] else {}
                except json.JSONDecodeError:
                    tool_input = {"raw_arguments": tc["arguments"]}
                yield StreamChunk(
                    tool_call=ToolCallDelta(
                        tool_name=tc["name"],
                        tool_input=tool_input,
                        tool_use_id=tc["id"],
                        is_final=True,
                    )
                )

    # ── MiniMax (MiniMax) 流式实现 ────────────────────────────────────
    #
    # 协议：OpenAI 兼容(参考 docs/platform.minimaxi.com/docs/api-reference/text-openai-api)
    # 端点：https://api.minimaxi.com/v1/chat/completions
    # 鉴权：Authorization: Bearer <MINIMAX_API_KEY>
    # 行为对齐 _chat_zhipu：Anthropic-style content list → string 转换
    #   + reasoning_content 流式 thinking(若 model 支持) + tool_calls 解析
    # 注意：tool_choice "none" / "auto" / "required" 由 MiniMax 自行映射

    def _chat_minimax(
        self,
        messages: list[dict],
        tools: Optional[list[dict]],
        tool_choice: Optional[str] = None,
    ) -> Generator[StreamChunk, None, None]:
        """MiniMax (MiniMax) 流式调用 — OpenAI 兼容协议"""
        client = self._get_minimax_client()

        # 转换 messages 格式：MiniMax 要求 role 为小写，content 为 string
        minimax_messages = []
        for m in messages:
            role = m["role"].lower()
            content = m["content"]
            # 如果 content 是 list（Anthropic 格式），转换为 string
            if isinstance(content, list):
                text_parts = []
                for item in content:
                    if isinstance(item, dict):
                        if item.get("type") == "text":
                            text_parts.append(item.get("text", ""))
                        elif item.get("type") == "tool_result":
                            text_parts.append(f"[Tool Result: {item.get('content', '')}]")
                content = "\n".join(text_parts)
            minimax_messages.append({"role": role, "content": content})

        kwargs: dict = {
            "model": self.config.model,
            "messages": minimax_messages,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if tools:
            openai_tools = []
            for t in tools:
                if "type" in t and t.get("type") == "function":
                    openai_tools.append(t)
                else:
                    # Anthropic 格式 → OpenAI 格式
                    openai_tools.append({
                        "type": "function",
                        "function": {
                            "name": t.get("name", ""),
                            "description": t.get("description", ""),
                            "parameters": t.get("input_schema", {}),
                        }
                    })
            kwargs["tools"] = openai_tools
            if tool_choice is not None:
                # OpenAI 兼容协议：接受 "auto" / "none" / "required"
                kwargs["tool_choice"] = tool_choice
        if self.config.temperature > 0:
            kwargs["temperature"] = self.config.temperature

        stream = client.chat.completions.create(**kwargs)

        tool_calls_buffer: dict = {}  # index -> {"id", "name", "arguments"}
        think_splitter = _ThinkTagSplitter()  # M3 等模型把 thinking 包在 <think> 标签里

        for chunk in stream:
            if chunk.choices:
                delta = chunk.choices[0].delta

                # MiniMax reasoning_content（若支持,优先于 text 标签解析,GLM 风格）
                if hasattr(delta, 'reasoning_content') and delta.reasoning_content:
                    yield StreamChunk(
                        thinking_delta=ThinkingDelta(thinking=delta.reasoning_content)
                    )
                # 文本增量:用 splitter 分离 <think>...</think>(M3 风格)
                elif delta.content:
                    for sc in think_splitter.feed(delta.content):
                        yield sc

                # 工具调用增量（OpenAI 格式）
                if delta.tool_calls:
                    for tc in delta.tool_calls:
                        idx = tc.index
                        if idx not in tool_calls_buffer:
                            tool_calls_buffer[idx] = {"id": "", "name": "", "arguments": ""}
                        if tc.id:
                            tool_calls_buffer[idx]["id"] = tc.id
                        if tc.function and tc.function.name:
                            tool_calls_buffer[idx]["name"] = tc.function.name
                        if tc.function and tc.function.arguments:
                            tool_calls_buffer[idx]["arguments"] += tc.function.arguments

            if hasattr(chunk, "usage") and chunk.usage:
                usage = UsageStats.from_chunk_usage(chunk.usage)
                yield StreamChunk(usage=usage)

        # 流式结束后，yield 完整的 tool_calls
        for idx in sorted(tool_calls_buffer.keys()):
            tc = tool_calls_buffer[idx]
            if tc["id"] and tc["name"]:
                import json
                try:
                    tool_input = json.loads(tc["arguments"]) if tc["arguments"] else {}
                except json.JSONDecodeError:
                    tool_input = {"raw_arguments": tc["arguments"]}
                yield StreamChunk(
                    tool_call=ToolCallDelta(
                        tool_name=tc["name"],
                        tool_input=tool_input,
                        tool_use_id=tc["id"],
                        is_final=True,
                    )
                )

        # 流式结束后,把 think_splitter 残留 buffer 兜底输出(不丢内容)
        for sc in think_splitter.flush():
            yield sc


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
