"""智谱 GLM provider(2026-06-29 LLM Router 重构 Stage 3)。

thinking 走 reasoning_content 字段(流式逐块到达,GLM 风格)。
"""
from __future__ import annotations

from typing import Generator

from ...config import LLMProvider
from ...registry import ProviderRegistry
from ...types import StreamChunk, TextDelta, ThinkingDelta
from .base import OpenAICompatibleProvider


@ProviderRegistry.register(LLMProvider.ZHIPU)
class ZhipuProvider(OpenAICompatibleProvider):
    """智谱 GLM(Coding Plan 专用端点)—— thinking 从 reasoning_content 流式取。"""

    provider_name = "zhipu"
    default_base_url = "https://open.bigmodel.cn/api/coding/paas/v4"
    env_key = "ZHIPU_API_KEY"

    def _resolve_api_key(self) -> str:
        from ....config import config as _env_config  # agent_core/config.py
        return self.config.api_key or _env_config.zhipu_api_key

    def _extract_thinking(self, delta) -> Generator[StreamChunk, None, None]:
        """GLM thinking 走独立 reasoning_content 字段。

        注:不 consume delta.content(因为 delta.content 和 reasoning_content
        是两个独立字段),所以 _consumed_text 不需要设为 True。
        """
        if hasattr(delta, "reasoning_content") and delta.reasoning_content:
            yield StreamChunk(thinking_delta=ThinkingDelta(thinking=delta.reasoning_content))


__all__ = ["ZhipuProvider"]
