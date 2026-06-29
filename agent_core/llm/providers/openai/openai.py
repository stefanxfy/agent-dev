"""OpenAI 官方 provider(2026-06-29 LLM Router 重构 Stage 3)。

无 thinking 提取 — 用基类默认(无 _extract_thinking override)。
"""
from __future__ import annotations

from ..base import BaseProvider  # noqa: F401  (type-check reference)
from ...config import LLMProvider
from ...registry import ProviderRegistry
from .base import OpenAICompatibleProvider


@ProviderRegistry.register(LLMProvider.OPENAI)
class OpenAIProvider(OpenAICompatibleProvider):
    """OpenAI 官方 GPT(无 thinking 提取)"""

    provider_name = "openai"
    default_base_url = None  # 走 OpenAI 官方端点
    env_key = "OPENAI_API_KEY"

    def _resolve_api_key(self) -> str:
        from ....config import config as _env_config  # agent_core/config.py
        return self.config.api_key or _env_config.openai_api_key


__all__ = ["OpenAIProvider"]
