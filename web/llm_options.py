"""LLM Provider/Model 选项的 UI 辅助层。

设计目标:
- provider 列表 + 模型列表 + API Key 环境变量名 都从 `agent_core.llm.router` 派生
- 加新 provider 只需改 `LLMProvider` enum + `MODELS_BY_PROVIDER` / `PROVIDER_ENV_KEY`,
  所有 web 页面(下拉框、API Key 输入框)自动同步
- 不在这里硬编码任何 provider 字符串
"""
from __future__ import annotations

import os
from typing import Optional

from agent_core.llm.router import (
    LLMProvider,
    MODELS_BY_PROVIDER,
    PROVIDER_ENV_KEY,
)


def get_provider_options() -> list[str]:
    """返回所有可用 provider 的字符串值(给 st.selectbox 用)。

    按 `LLMProvider` enum 定义顺序返回,保证 UI 展示稳定。
    """
    return [p.value for p in LLMProvider]


def get_default_provider() -> str:
    """从 `DEFAULT_PROVIDER` env 读取默认 provider,fallback 到 zhipu。

    不在合法列表中时返回 enum 第一个值(anthropic),避免 Streamlit 崩溃。
    """
    raw = os.getenv("DEFAULT_PROVIDER", "").lower().strip()
    if raw in get_provider_options():
        return raw
    return LLMProvider.ANTHROPIC.value


def get_default_provider_index() -> int:
    """默认 provider 在 `get_provider_options()` 里的索引。"""
    default = get_default_provider()
    options = get_provider_options()
    return options.index(default) if default in options else 0


def get_models_for_provider(provider: str) -> list[str]:
    """返回指定 provider 的可选模型列表(字符串值)。

    未知 provider 返回空 list(让 selectbox 自然 fallback 到第一个)。
    """
    try:
        p = LLMProvider(provider)
    except ValueError:
        return []
    return [m.value for m in MODELS_BY_PROVIDER.get(p, [])]


def get_env_key_for_provider(provider: str) -> str:
    """返回指定 provider 对应的 API Key 环境变量名。

    未知 provider fallback 到 OPENAI_API_KEY(与历史行为一致)。
    """
    try:
        p = LLMProvider(provider)
    except ValueError:
        return "OPENAI_API_KEY"
    return PROVIDER_ENV_KEY.get(p, "OPENAI_API_KEY")
