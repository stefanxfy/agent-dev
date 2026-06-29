"""Anthropic Claude Provider — 从 router.py:556-658 抽出的 _chat_anthropic(2026-06-29 Stage 2)。

行为差异(相对原 router.py):
- 实现 `_do_chat()` 而非 `chat()`(Template Method 由 BaseProvider 自动包重试)
- 多接收 `system_prompt` / `cache_namespace` 参数(从 Router.chat() 透传下来)
- Lazy import anthropic SDK(避免 .venv 没装时拖死整个 LLM 模块)
"""
from __future__ import annotations

import logging
from typing import Generator, Optional, TYPE_CHECKING

from ..base import BaseProvider
from ...registry import ProviderRegistry
from ...types import StreamChunk, TextDelta, ThinkingDelta, ToolCallDelta, UsageStats
from ...config import LLMConfig, LLMProvider

if TYPE_CHECKING:
    pass

logger = logging.getLogger("llm.providers.anthropic")


@ProviderRegistry.register(LLMProvider.ANTHROPIC)
class AnthropicProvider(BaseProvider):
    """Anthropic Claude(同步流式,支持 thinking blocks + prompt cache)"""

    provider_name = "anthropic"
    default_base_url = None  # 走 Anthropic 官方端点
    env_key = "ANTHROPIC_API_KEY"

    def __init__(self, config: LLMConfig) -> None:
        super().__init__(config)
        self._client = None  # lazy

    @property
    def client(self):
        """懒加载 anthropic.Anthropic client"""
        if self._client is None:
            import anthropic
            self._client = anthropic.Anthropic(api_key=self._resolve_api_key())
        return self._client

    def _resolve_api_key(self) -> str:
        from ....config import config as _env_config  # agent_core/config.py
        return self.config.api_key or _env_config.anthropic_api_key

    def _do_chat(
        self,
        messages: list[dict],
        tools: Optional[list[dict]] = None,
        tool_choice: Optional[str] = None,
        system_prompt: Optional[str] = None,
        cache_namespace: Optional[str] = None,
    ) -> Generator[StreamChunk, None, None]:
        """Anthropic Claude 流式调用(同步,支持 thinking blocks)。"""
        client = self.client

        kwargs: dict = {
            "model": self.config.model,
            "max_tokens": self.config.max_tokens,
            "messages": messages,
        }
        if tools:
            kwargs["tools"] = tools
            if tool_choice == "none":
                kwargs["tool_choice"] = {"type": "none"}
            elif tool_choice == "auto":
                kwargs["tool_choice"] = {"type": "auto"}
        if self.config.temperature > 0:
            kwargs["temperature"] = self.config.temperature

        # P2:system prompt(Anthropic 顶层参数)
        # M7:cache_namespace 非空时打包成 content block 并打 cache_control
        if system_prompt:
            if cache_namespace:
                kwargs["system"] = [{
                    "type": "text",
                    "text": system_prompt,
                    "cache_control": {"type": "ephemeral"},
                }]
            else:
                kwargs["system"] = system_prompt

        # M7:cache_namespace 非空时,最后一个 tool 打 cache_control 锚点
        if cache_namespace and tools:
            tools = [dict(t) for t in tools]
            tools[-1] = {**tools[-1], "cache_control": {"type": "ephemeral"}}
            kwargs["tools"] = tools

        # 开启思考过程(Claude 3.7+)
        if self.config.thinking and self.config.thinking.enabled:
            kwargs["thinking"] = {
                "type": "enabled",
                "budget_tokens": self.config.thinking.budget_tokens,
            }

        with client.messages.stream(**kwargs) as stream:
            for text_delta in stream.text_stream:
                if text_delta:
                    yield StreamChunk(text_delta=TextDelta(text=text_delta))

            final_message = stream.get_final_message()

            # 提取 thinking blocks
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

            # 提取 tool_use blocks
            for block in final_message.content:
                if block.type == "tool_use":
                    yield StreamChunk(
                        tool_call=ToolCallDelta(
                            tool_name=block.name,
                            tool_input=dict(block.input),
                            tool_use_id=block.id,
                            is_final=True,
                        )
                    )

            # Token 消耗
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

            # 终止原因
            stop_reason = getattr(final_message, "stop_reason", None)
            if stop_reason:
                yield StreamChunk(stop_reason=stop_reason)
