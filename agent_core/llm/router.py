"""LLM Router — slim dispatch(2026-06-29 LLM Router 重构 Stage 4)。

历史:原 694 行的 god class。Stage 1-3 拆分后,本文件只负责:
1. 解析 system_prompt_override(Fork 模式 cache prefix 对齐)
2. 通过 ProviderRegistry.dispatch 到对应 provider
3. 转发 tools / tool_choice / cache_namespace 给 provider

重试 / thinking 提取 / 协议实现 / 流式循环 全部在 providers/* 里。
"""
from __future__ import annotations

# ⚠️ 防御性 side-effect import:确保任何从 .router 直接 import LLMRouter 的代码
# (绕过包级 __init__.py 的 `from . import providers`) 也能触发 @register_provider 装饰器
# 不加这一行,会出现:
#   ValueError: Provider <LLMProvider.MINIMAX: 'minimax'> 未注册。已注册: []。
# 见 agent_core/agent_core.py:33、web/pages/00_Chat.py:22、agent_core/memory/{distill,sm}_callback.py 等。
from . import providers  # noqa: F401

import logging
from typing import Generator, Optional

from .config import LLMConfig
from .types import StreamChunk
from .providers.base import BaseProvider
from .registry import ProviderRegistry

logger = logging.getLogger("llm.router")


class LLMRouter:
    """多厂商 LLM 统一调用路由 — 委托给 ProviderRegistry。

    Retry / thinking / cache / 协议实现 全部在 Provider 里,Router 只管 dispatch + system_prompt 处理。

    流式调用:`chat()`(yield StreamChunk)
    """

    def __init__(self, config: LLMConfig):
        self.config = config
        self._provider: Optional[BaseProvider] = None

    @property
    def provider(self) -> BaseProvider:
        """懒加载 provider(由 ProviderRegistry 创建)。"""
        if self._provider is None:
            self._provider = ProviderRegistry.create(self.config)
        return self._provider

    def chat(
        self,
        messages: list[dict],
        tools: Optional[list[dict]] = None,
        system_prompt_override: Optional[str] = None,
        tool_choice: Optional[str] = None,
        cache_namespace: Optional[str] = None,
    ) -> Generator[StreamChunk, None, None]:
        """统一 chat 入口(流式)。

        Args:
            messages: 对话消息列表
            tools: 工具 schema 列表
            system_prompt_override: Fork 模式下覆盖 system prompt(保持 cache prefix 字节级一致)
            tool_choice: 工具选择策略(None / "auto" / "none" / "required")
            cache_namespace: M7 prompt cache 命名空间,透传给 provider(目前仅 Anthropic 使用)。
        """
        system_prompt, filtered_messages = self._resolve_system(
            messages, system_prompt_override
        )
        yield from self.provider.chat(
            messages=filtered_messages,
            tools=tools,
            tool_choice=tool_choice,
            system_prompt=system_prompt,
            cache_namespace=cache_namespace,
        )

    # ── 内部辅助 ─────────────────────────────────────────────────

    def _resolve_system(
        self,
        messages: list[dict],
        override: Optional[str],
    ) -> tuple[Optional[str], list[dict]]:
        """解析 system_prompt + 过滤 messages 中的 system(避免双注入)。

        Returns:
            (system_prompt, filtered_messages) — 传给 provider。
            - system_prompt: 提取出来的 system 文本(给 Anthropic 顶层参数)
            - filtered_messages: 去掉 system role 后的消息列表
        """
        if override is not None:
            return override, [m for m in messages if m.get("role") != "system"]
        for m in messages:
            if m.get("role") == "system":
                return m.get("content", ""), [x for x in messages if x.get("role") != "system"]
        return None, list(messages)


# ── Backward-compat re-exports(24 个外部文件从这里 import) ──────
# Stage 4 拆出去的符号全部 re-export,保持外部 import 路径不变。
from .types import TextDelta, ThinkingDelta, ToolCallDelta, UsageStats  # noqa: E402, F401
from .config import (  # noqa: E402, F401
    LLMProvider,
    LLMModel,
    ThinkingConfig,
    MODELS_BY_PROVIDER,
    PROVIDER_ENV_KEY,
)


def create_router(
    provider: str = "anthropic",
    model: str = "",
    api_key: str = "",
    **kwargs,
) -> LLMRouter:
    """快速创建 LLM Router(便捷工厂)"""
    config = LLMConfig(
        provider=LLMProvider(provider),
        model=model or _default_model(provider),
        api_key=api_key,
        **kwargs,
    )
    return LLMRouter(config)


def _default_model(provider: str) -> str:
    """从环境变量读取默认模型,没有则返回空字符串"""
    from ..config import config as _env_config
    if _env_config.default_provider.lower() == provider:
        return _env_config.default_model
    return ""