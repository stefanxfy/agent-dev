"""
Config — 统一配置管理器

集中从 .env 读取所有配置，提供类型安全的访问。
其他模块通过 `from agent_core.config import config` 获取单例实例。

env 变量命名约定：
  模型配置: MODEL_CONFIG__<模型键>__<字段>=<值>
    模型键用下划线替代连字符/点号: glm-5.1 → glm_5_1
    支持字段: context_window, max_output, autocompact_buffer
    示例: MODEL_CONFIG__glm_5_1__context_window=128000

  通用配置: <同名常量>=<值>
    示例: MAX_PTL_RETRIES=3

E-1 修复：所有环境变量集中管理
- 旧实现：散落在各模块的 os.getenv("XX", default)
- 新实现：所有 env 变量在此处定义 ENV_VAR_REGISTRY，类型 + 默认值
- 提供 typed accessors (anthropic_api_key, zhipu_api_key, default_provider 等)
- 自动生成文档 / 验证 / IDE 提示
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Optional, Literal, get_args
from dotenv import load_dotenv, find_dotenv

logger = logging.getLogger("agent_core.config")


# ── 集中式环境变量注册表 ────────────────────────────────────────
#
# 每条记录：(env_name, type, default, description)
# 新增 env 变量只需在这里加一行
# type 必须是：str / int / float / bool / Literal[...]

@dataclass(frozen=True)
class EnvVarSpec:
    name: str
    type: type
    default: object
    description: str = ""


ENV_VAR_REGISTRY: tuple[EnvVarSpec, ...] = (
    # ── LLM Provider API Keys ──
    EnvVarSpec("ANTHROPIC_API_KEY", str, "", "Anthropic Claude API Key"),
    EnvVarSpec("OPENAI_API_KEY", str, "", "OpenAI GPT API Key"),
    EnvVarSpec("ZHIPU_API_KEY", str, "", "智谱 GLM API Key"),

    # ── Default LLM ──
    EnvVarSpec("DEFAULT_PROVIDER", Literal["anthropic", "openai", "zhipu"], "zhipu", "默认 LLM 厂商"),
    EnvVarSpec("DEFAULT_MODEL", str, "GLM-5.1", "默认 LLM 模型"),
    EnvVarSpec("DEFAULT_TEMPERATURE", float, 0.7, "默认 LLM 温度"),
    EnvVarSpec("DEFAULT_MAX_TOKENS", int, 4096, "默认最大输出 tokens"),

    # ── Context 压缩 ──
    EnvVarSpec("AUTOCOMPACT_PCT_OVERRIDE", float, 0.0, "压缩触发百分比覆盖（0 表示用模型默认）"),
    EnvVarSpec("MAX_CONSECUTIVE_AUTOCOMPACT_FAILURES", int, 3, "熔断器：连续失败次数上限"),
    EnvVarSpec("MAX_PTL_RETRIES", int, 3, "PTL 防御：剥洋葱重试上限"),
    EnvVarSpec("TRUNCATE_RATIO", float, 0.2, "PTL 防御：每次剥掉的比例"),
    EnvVarSpec("PRESERVED_HEAD_MAX_TOKENS", int, 4000, "Preserved Head 总 token 预算"),
    EnvVarSpec("MAX_PRESERVED_TURNS", int, 3, "Preserved Head 最多 turn 对数"),

    # ── 路由重试 (P1-8 / P1-9) ──
    EnvVarSpec("LLM_MAX_STREAM_RETRY", int, 2, "流式中断重试上限"),
    EnvVarSpec("LLM_MAX_REQUEST_RETRY", int, 3, "HTTP 错误重试上限"),
    EnvVarSpec("LLM_RETRY_BACKOFF_BASE", float, 0.5, "重试退避基础秒数"),

    # ── 数据目录 ──
    EnvVarSpec("AGENT_DATA_DIR", str, "", "会话数据目录（空=默认 ~/.agent_data）"),
)


class Config:
    """统一配置管理器（单例设计，模块级实例为 `config`）"""

    def __init__(self, env_file: str | None = None):
        load_dotenv(env_file or find_dotenv())
        self._env = os.environ
        self._model_configs: dict[str, dict] | None = None
        self._cache: dict[str, object] = {}

    # ── 基础类型安全访问器 ────────────────────────────────────

    def get(self, key: str, default: str = "") -> str:
        return self._env.get(key, default)

    def int(self, key: str, default: int) -> int:
        val = self._env.get(key, "").strip()
        if not val:
            return default
        try:
            return int(val)
        except ValueError:
            logger.warning("Config %r = %r 不是合法整数，使用默认值 %d", key, val, default)
            return default

    def float(self, key: str, default: float) -> float:
        val = self._env.get(key, "").strip()
        if not val:
            return default
        try:
            return float(val)
        except ValueError:
            logger.warning("Config %r = %r 不是合法浮点数，使用默认值 %f", key, val, default)
            return default

    def bool(self, key: str, default: bool = False) -> bool:
        val = self._env.get(key, "").strip().lower()
        if not val:
            return default
        return val in ("1", "true", "yes", "on")

    def typed(self, spec: EnvVarSpec) -> object:
        """
        按 EnvVarSpec 读取配置（带类型校验 + 缓存）

        用法：
            spec = ENV_VAR_REGISTRY_BY_NAME["ZHIPU_API_KEY"]
            api_key = config.typed(spec)  # str
        """
        if spec.name in self._cache:
            return self._cache[spec.name]
        raw = self._env.get(spec.name, "").strip()
        if not raw:
            value = spec.default
        else:
            try:
                if spec.type is int:
                    value = int(raw)
                elif spec.type is float:
                    value = float(raw)
                elif spec.type is bool:
                    value = raw.lower() in ("1", "true", "yes", "on")
                elif spec.type is str:
                    value = raw
                else:
                    # Literal[...] 类型
                    valid = get_args(spec.type)
                    if raw in valid:
                        value = raw
                    else:
                        logger.warning(
                            "Config %r = %r 不在合法值 %s 中，使用默认值 %r",
                            spec.name, raw, valid, spec.default,
                        )
                        value = spec.default
            except (ValueError, TypeError):
                logger.warning(
                    "Config %r = %r 无法解析为 %s，使用默认值 %r",
                    spec.name, raw, spec.type.__name__, spec.default,
                )
                value = spec.default
        self._cache[spec.name] = value
        return value

    # ── Typed accessors（E-1 修复后的推荐用法）──────────────
    #
    # 这些属性自动从 ENV_VAR_REGISTRY 读取，避免散落的 os.getenv

    @property
    def anthropic_api_key(self) -> str:
        spec = next(s for s in ENV_VAR_REGISTRY if s.name == "ANTHROPIC_API_KEY")
        return str(self.typed(spec))

    @property
    def openai_api_key(self) -> str:
        spec = next(s for s in ENV_VAR_REGISTRY if s.name == "OPENAI_API_KEY")
        return str(self.typed(spec))

    @property
    def zhipu_api_key(self) -> str:
        spec = next(s for s in ENV_VAR_REGISTRY if s.name == "ZHIPU_API_KEY")
        return str(self.typed(spec))

    @property
    def default_provider(self) -> str:
        spec = next(s for s in ENV_VAR_REGISTRY if s.name == "DEFAULT_PROVIDER")
        return str(self.typed(spec))

    @property
    def default_model(self) -> str:
        spec = next(s for s in ENV_VAR_REGISTRY if s.name == "DEFAULT_MODEL")
        return str(self.typed(spec))

    @property
    def default_temperature(self) -> float:
        spec = next(s for s in ENV_VAR_REGISTRY if s.name == "DEFAULT_TEMPERATURE")
        return float(self.typed(spec))

    @property
    def default_max_tokens(self) -> int:
        spec = next(s for s in ENV_VAR_REGISTRY if s.name == "DEFAULT_MAX_TOKENS")
        return int(self.typed(spec))

    @property
    def llm_max_stream_retry(self) -> int:
        spec = next(s for s in ENV_VAR_REGISTRY if s.name == "LLM_MAX_STREAM_RETRY")
        return int(self.typed(spec))

    @property
    def llm_max_request_retry(self) -> int:
        spec = next(s for s in ENV_VAR_REGISTRY if s.name == "LLM_MAX_REQUEST_RETRY")
        return int(self.typed(spec))

    @property
    def llm_retry_backoff_base(self) -> float:
        spec = next(s for s in ENV_VAR_REGISTRY if s.name == "LLM_RETRY_BACKOFF_BASE")
        return float(self.typed(spec))

    @property
    def agent_data_dir(self) -> Optional[str]:
        spec = next(s for s in ENV_VAR_REGISTRY if s.name == "AGENT_DATA_DIR")
        val = str(self.typed(spec))
        return val or None

    # ── 模型配置 ──────────────────────────────────────────────

    @property
    def model_configs(self) -> dict[str, dict]:
        """
        从 MODEL_CONFIG__<模型键>__<字段> 环境变量构建模型配置表。
        结果缓存，仅首次解析。
        """
        if self._model_configs is not None:
            return self._model_configs

        configs: dict[str, dict] = {}
        prefix = "MODEL_CONFIG__"

        for k, v in self._env.items():
            if not k.startswith(prefix):
                continue
            suffix = k[len(prefix):]       # e.g. "glm_5_1__context_window"
            parts = suffix.rsplit("__", 1)
            if len(parts) != 2:
                continue
            model_key, field = parts
            if field not in ("context_window", "max_output", "autocompact_buffer"):
                continue
            if not v.strip():
                continue
            try:
                configs.setdefault(model_key, {})[field] = int(v.strip())
            except ValueError:
                logger.warning("模型配置 %s=%s 无法解析为整数，已跳过", k, v)

        self._model_configs = configs
        logger.debug("已加载 %d 个模型配置", len(configs))
        return configs

    def get_model_config(self, model: str) -> dict:
        """
        获取模型配置，支持模糊匹配。

        匹配逻辑（对齐旧版 get_model_config）：
          1. 精确匹配
          2. 子串匹配（取最长匹配 key）
          3. 回退默认值
        """
        configs = self.model_configs
        if not configs:
            return self._default_model_config()

        # 查询名标准化（将 model 中的 - 和 . 转为 _）
        norm = model.lower().replace("-", "_").replace(".", "_")

        # 1. 精确匹配
        if norm in configs:
            return configs[norm]

        # 2. 模糊匹配（key 是 norm 的子串 → 最长优先）
        best, best_len = None, 0
        for key in configs:
            if key in norm and len(key) > best_len:
                best, best_len = key, len(key)

        if best:
            return configs[best]

        # 3. 默认
        return self._default_model_config()

    def _default_model_config(self) -> dict:
        return {
            "context_window": self.int("MODEL_CONFIG__default__context_window", 32_000),
            "max_output": self.int("MODEL_CONFIG__default__max_output", 4_096),
            "autocompact_buffer": self.int("MODEL_CONFIG__default__autocompact_buffer", 13_000),
        }


# ── 索引（按名字查 spec）───────────────────────────────────────

ENV_VAR_REGISTRY_BY_NAME: dict[str, EnvVarSpec] = {
    spec.name: spec for spec in ENV_VAR_REGISTRY
}


def list_env_vars() -> list[EnvVarSpec]:
    """列出所有受支持的环境变量（用于文档生成 / --help）"""
    return list(ENV_VAR_REGISTRY)


# 模块级单例 —— 所有代码通过此实例访问配置
config = Config()
