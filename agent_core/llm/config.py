"""LLM 配置相关(从 router.py 抽出,2026-06-29 LLM Router 重构 Stage 2)。

- `LLMProvider` / `LLMModel` enum
- `ThinkingConfig` / `LLMConfig` (Pydantic)
- `MODELS_BY_PROVIDER` / `PROVIDER_ENV_KEY` 元数据(供 UI / 调度使用)
"""
from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import BaseModel


# ── 枚举 ────────────────────────────────────────────────────────────


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


# ── Provider 元数据(给 UI / 配置使用)──────────────────────────────
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


# ── Thinking / LLMConfig ──────────────────────────────────────────


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
    system_prompt: Optional[str] = None  # P2 新增:系统提示词
    # Stage 2 新增:Provider 重试策略覆盖。None = 用 BaseProvider 默认(env-driven)
    # 用前向引用字符串避开循环 import(BaseProvider 需要这个类型,RetryPolicy 在 Stage 2d 添加)
    retry_policy: Optional["RetryPolicy"] = None


# Stage 2d 添加 RetryPolicy 后,解析前向引用。
# 延迟到模块末尾避免循环 import(config.py → providers._retry.py)
from .providers._retry import RetryPolicy  # noqa: E402

LLMConfig.model_rebuild()


__all__ = [
    "LLMProvider", "LLMModel",
    "ThinkingConfig", "LLMConfig",
    "MODELS_BY_PROVIDER", "PROVIDER_ENV_KEY",
]
