"""LLM Router — slim dispatch(2026-06-29 LLM Router 重构 Stage 4)。

历史:原 694 行的 god class。Stage 1-3 拆分后,本文件只负责:
1. 解析 system_prompt_override(Fork 模式 cache prefix 对齐)
2. 通过 ProviderRegistry.dispatch 到对应 provider
3. 转发 tools / tool_choice / cache_namespace 给 provider

重试 / thinking 提取 / 协议实现 / 流式循环 全部在 providers/* 里。

补充(2026-06-29):router 上加了 invoke() — 同步聚合调用,内置重试+超时。
设计参考:docs/llm-invoke-retry-design.md
"""
from __future__ import annotations

# ⚠️ 防御性 side-effect import:确保任何从 .router 直接 import LLMRouter 的代码
# (绕过包级 __init__.py 的 `from . import providers`) 也能触发 @register_provider 装饰器
# 不加这一行,会出现:
#   ValueError: Provider <LLMProvider.MINIMAX: 'minimax'> 未注册。已注册: []。
# 见 agent_core/agent_core.py:33、web/pages/00_Chat.py:22、agent_core/memory/{distill,sm}_callback.py 等。
from . import providers  # noqa: F401

import concurrent.futures
import logging
import time
from typing import Callable, Generator, Optional

from .config import LLMConfig
from .types import StreamChunk
from .providers.base import BaseProvider
from .registry import ProviderRegistry

logger = logging.getLogger("llm.router")


# ── invoke() 专用异常 ───────────────────────────────────────

class EmptyResponseError(RuntimeError):
    """LLM 返回空响应。invoke() 视空响应为错误,自动触发重试。"""


class InvokeTimeoutError(TimeoutError):
    """invoke() 超时(底层 future.result(timeout=N) 触发)。"""


class LLMRouter:
    """多厂商 LLM 统一调用路由 — 委托给 ProviderRegistry。

    Retry / thinking / cache / 协议实现 全部在 Provider 里,Router 只管 dispatch + system_prompt 处理。

    流式调用:`chat()`(yield StreamChunk)
    同步聚合:`invoke()`(返回 str,内置重试+超时)
    """

    # ── invoke() 内部常量 ──────────────────────────────────────
    _BACKOFF_BASE: float = 0.5  # 指数退避基准秒数(0.5s × 2^attempt)

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

    # ── invoke():同步聚合调用(内置重试+超时) ────────────────────

    def invoke(
        self,
        messages: list[dict],
        *,
        max_retries: int = 2,
        timeout: Optional[float] = 30.0,
        on_failure: Optional[Callable[[Exception], str]] = None,
        **kwargs,  # 透传给 self.provider.chat() (cache_namespace 等)
    ) -> str:
        """同步调用 LLM,聚合流式 chunks 返回 str。内置重试 + 超时 + 空响应检测。

        设计:收归调用点重复的 chunk 聚合 / 重试循环 / 超时守卫(5+10+8 行 → 1 行)。
        参考:`docs/llm-invoke-retry-design.md`

        Args:
            messages:     对话消息列表
            max_retries:  重试次数(总尝试次数 = max_retries + 1,默认 2 = 3 次)
            timeout:      单次调用的超时秒数(None 禁用,默认 30s)
            on_failure:   所有重试失败后的回调 `(Exception) -> str`。
                          回调内部可以 raise(穿透 invoke() 到达调用方),或 return 降级文本。
                          传 None 时,所有重试耗尽后 re-raise 最后一次异常。
            **kwargs:     透传给 self.provider.chat()(如 cache_namespace="memory_xxx")

        Returns:
            聚合后的文本。空响应视为错误(自动重试),非空才返回。

        Raises:
            EmptyResponseError: 所有重试都返回空文本
            InvokeTimeoutError:  单次调用超时
            其他 Exception:     provider 抛出的原始异常
        """
        last_err: Optional[Exception] = None

        for attempt in range(max_retries + 1):
            try:
                # ── 超时守卫(可选)— ThreadPoolExecutor 跑同步流 ──
                if timeout is not None:
                    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
                        future = ex.submit(self._aggregate_chunks, messages, **kwargs)
                        text = future.result(timeout=timeout)
                else:
                    text = self._aggregate_chunks(messages, **kwargs)

                # ── 空响应检测(无条件重试) ──
                if not text.strip():
                    raise EmptyResponseError(
                        f"LLM invoke 返回空响应 attempt={attempt}"
                    )

                return text

            except concurrent.futures.TimeoutError as e:
                last_err = InvokeTimeoutError(
                    f"LLM invoke timeout ({timeout}s) attempt={attempt}"
                )
                last_err.__cause__ = e  # 保留原 TimeoutError 链(替代 raise X from e)
            except EmptyResponseError as e:
                last_err = e
            except Exception as e:
                last_err = e

            # ── 退避 + 日志(内置) ──
            if attempt < max_retries:
                delay = self._BACKOFF_BASE * (2 ** attempt)
                logger.debug(
                    f"LLM invoke retry {attempt + 1}/{max_retries} "
                    f"after {delay:.1f}s: {type(last_err).__name__}: {last_err}"
                )
                time.sleep(delay)

        # ── 所有重试耗尽 ──
        assert last_err is not None  # type guard
        if on_failure is not None:
            return on_failure(last_err)  # 回调可 raise 穿透 或 return 降级文本
        raise last_err

    def _aggregate_chunks(self, messages: list[dict], **kwargs) -> str:
        """聚合 provider.chat() 的流式 chunks 为一个字符串。

        收归 5 个调用点(agent_core/memory/{retriever,distill_callback,dedup,
        extraction_gate,sm_callback}.py)里重复的那 3-5 行 chunk 迭代。
        """
        parts: list[str] = []
        for chunk in self.provider.chat(messages=messages, **kwargs):
            td = getattr(chunk, "text_delta", None)
            if td is not None:
                t = getattr(td, "text", None)
                if t:
                    parts.append(t)
        return "".join(parts)

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
