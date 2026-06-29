"""OpenAI 兼容 provider 包(2026-06-29 LLM Router 重构 Stage 3)。

import agent_core.llm.providers.openai 会触发 4 个 provider 的 @register_provider 装饰器:
- OpenAIProvider (官方 GPT)
- ZhipuProvider (智谱 GLM)
- MiniMaxProvider (MiniMax M3)
- OpenAICompatibleProvider (基类,通常不直接注册)
"""
from .base import OpenAICompatibleProvider
from .openai import OpenAIProvider
from .zhipu import ZhipuProvider
from .minimax import MiniMaxProvider

__all__ = [
    "OpenAICompatibleProvider",
    "OpenAIProvider",
    "ZhipuProvider",
    "MiniMaxProvider",
]
