"""BaseProvider ABC — Template Method 模式封装重试(2026-06-29 LLM Router 重构 Stage 2)。

设计动机:
- 原 router.py:251-331 的 `_stream_with_retry` 方法是 Router 自己管重试,
  导致加新 provider 时必须复制粘贴 retry 逻辑。
- 抽出 BaseProvider 后,每个子类只实现 `_do_chat()`(纯流式逻辑),`chat()`
  由基类自动包一层 `_with_retry()`,子类完全看不到 RetryPolicy。

Template Method 三件套:
    @abstractmethod _do_chat():   子类实现:构造请求 + 跑 stream
    chat():                     基类实现:_do_chat + _with_retry
    _with_retry(stream_fn):     基类实现:读 self.retry_policy 包装重试
"""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Generator, Optional, TYPE_CHECKING

from ..types import StreamChunk

if TYPE_CHECKING:
    from ..config import LLMConfig
    from ._retry import RetryPolicy

logger = logging.getLogger("llm.providers.base")


class BaseProvider(ABC):
    """所有 LLM provider 的基类 — Template Method 模式。

    子类必须实现:
        _do_chat(): 实际的流式调用(不含重试)

    子类可 override:
        _resolve_api_key():  从 LLMConfig + env 拿 key
        _on_stream_end():    流结束钩子(flush 残留 buffer 等)
    """

    provider_name: str = ""
    default_base_url: Optional[str] = None
    env_key: str = ""  # 默认 API key 环境变量名(子类用)

    def __init__(self, config: "LLMConfig") -> None:
        self.config = config
        # 重试策略:LLMConfig 显式给 → 用它;否则用 env-driven 默认值
        # (RetryPolicy 本身是纯 dataclass,env 读取由 BaseProvider 负责)
        explicit = getattr(config, "retry_policy", None)
        if explicit is not None:
            self.retry_policy: "RetryPolicy" = explicit
        else:
            from ._retry import RetryPolicy  # 延迟 import 避免循环
            # Stage 2d:env-driven 默认值。env 路径走 _env_config
            # (agent_core/config.py 的 typed singleton)
            from ...config import config as _env_config
            self.retry_policy = RetryPolicy(
                max_request_retry=_env_config.llm_max_request_retry,
                max_stream_retry=_env_config.llm_max_stream_retry,
                backoff_base=_env_config.llm_retry_backoff_base,
            )

    # ── 子类入口(纯流式,无重试) ──────────────────────────────────

    @abstractmethod
    def _do_chat(
        self,
        messages: list[dict],
        tools: Optional[list[dict]] = None,
        tool_choice: Optional[str] = None,
        system_prompt: Optional[str] = None,
        cache_namespace: Optional[str] = None,
    ) -> Generator[StreamChunk, None, None]:
        """子类实现:实际跑流式请求。**不要** 在这里加重试 — 基类 chat() 会包。

        Args:
            messages:  对话消息列表(已去除 system)
            tools:     工具 schema 列表
            tool_choice: "auto" / "none" / "required" / None
            system_prompt: Anthropic 等需要顶层 system 参数的 provider 用
            cache_namespace: M7 prompt cache 命名空间(只 Anthropic 实现)
        """
        ...  # pragma: no cover
        yield  # 让 mypy 知道这是 generator

    # ── Template Method:基类包重试 ─────────────────────────────────

    def chat(
        self,
        messages: list[dict],
        tools: Optional[list[dict]] = None,
        tool_choice: Optional[str] = None,
        system_prompt: Optional[str] = None,
        cache_namespace: Optional[str] = None,
    ) -> Generator[StreamChunk, None, None]:
        """公共入口:子类不需要 override。重试由 _with_retry() 处理。"""
        def stream_fn():
            return self._do_chat(messages, tools, tool_choice, system_prompt, cache_namespace)
        yield from self._with_retry(stream_fn)

    def _with_retry(self, stream_fn) -> Generator[StreamChunk, None, None]:
        """读 self.retry_policy 包装重试(Stage 2d 接 RetryPolicy)。"""
        from ._retry import _stream_with_retry
        p = self.retry_policy
        yield from _stream_with_retry(
            stream_fn,
            self.provider_name,
            max_stream_retry=p.max_stream_retry,
            max_request_retry=p.max_request_retry,
            backoff_base=p.backoff_base,
            retryable_status_codes=p.retryable_status_codes,
            soft_retryable_status_codes=p.soft_retryable_status_codes,
            non_retryable_status_codes=p.non_retryable_status_codes,
        )

    # ── 共享工具(子类可 override) ─────────────────────────────────

    def _resolve_api_key(self) -> str:
        """默认:从 LLMConfig.api_key 读(env 兜底由子类决定)"""
        return self.config.api_key


__all__ = ["BaseProvider"]
