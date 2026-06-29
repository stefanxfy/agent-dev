"""Anthropic Claude provider(2026-06-29 LLM Router 重构 Stage 2)。

import agent_core.llm.providers.anthropic 会触发 AnthropicProvider @register_provider 装饰器。
"""
from .base import AnthropicProvider

__all__ = ["AnthropicProvider"]
