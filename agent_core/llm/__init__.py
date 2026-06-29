"""LLM 模块公共 API(2026-06-29 LLM Router 重构 Stage 4)。

import agent_core.llm 会触发所有 provider 的 @register_provider 装饰器
(通过 import .providers 子包)。
"""
# 触发 provider 自动注册(import 副作用:每个 provider 文件顶层有 @ProviderRegistry.register 装饰器)
from . import providers  # noqa: F401

# 公共类型
from .types import (
    StreamChunk,
    TextDelta,
    ThinkingDelta,
    ToolCallDelta,
    UsageStats,
)

# 配置 / 元数据
from .config import (
    LLMConfig,
    LLMProvider,
    LLMModel,
    ThinkingConfig,
    MODELS_BY_PROVIDER,
    PROVIDER_ENV_KEY,
)

# Provider 抽象 + 注册表
from .providers.base import BaseProvider
from .registry import ProviderRegistry, register_provider

# Router
from .router import LLMRouter, create_router

__all__ = [
    # Types
    "StreamChunk", "TextDelta", "ThinkingDelta", "ToolCallDelta", "UsageStats",
    # Config
    "LLMConfig", "LLMProvider", "LLMModel", "ThinkingConfig",
    "MODELS_BY_PROVIDER", "PROVIDER_ENV_KEY",
    # Provider
    "BaseProvider", "ProviderRegistry", "register_provider",
    # Router
    "LLMRouter", "create_router",
]
