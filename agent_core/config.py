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
"""

from __future__ import annotations

import logging
import os
from dotenv import load_dotenv, find_dotenv

logger = logging.getLogger("agent_core.config")


class Config:
    """统一配置管理器（单例设计，模块级实例为 `config`）"""

    def __init__(self, env_file: str | None = None):
        load_dotenv(env_file or find_dotenv())
        self._env = os.environ
        self._model_configs: dict[str, dict] | None = None

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


# 模块级单例 —— 所有代码通过此实例访问配置
config = Config()
