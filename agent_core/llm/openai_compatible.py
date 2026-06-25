"""OpenAI 兼容协议 Provider 基类 + 3 个具体实现(OpenAI / Zhipu / MiniMax)。

设计动机:
- 原 router.py 里的 _chat_openai / _chat_zhipu / _chat_minimax 三个方法
  有 ~80% 重复(message 转换、tool 转换、stream 循环、tool_calls 缓冲)。
- 加第 4 个 OpenAI 兼容 provider(DeepSeek / 豆包等)需复制 80+ 行。
- 抽出本基类后,新增 provider 只需:
    1. 继承 OpenAICompatibleProvider
    2. 设置 `provider_name` + `default_base_url` 类属性
    3. 必要时 override `_process_delta()` 提取 thinking
    4. 必要时 override `_resolve_api_key()` 决定 env var 名
- 3 个 provider 间真正的差异(从 router 拆出后只剩):
    * base_url 不同
    * thinking 提取策略不同(无 / reasoning_content / <think> 标签)
    * API key env var 名不同

历史:2026-06-24 从 router.py:761-1112 拆出(原 350 行 → 基类 130 行 + 3 个子类各 20 行)。
"""
from __future__ import annotations

import json
import logging
from typing import Generator, Optional

from pydantic import BaseModel

from .router import (
    LLMConfig,
    StreamChunk,
    TextDelta,
    ThinkingDelta,
    ToolCallDelta,
    UsageStats,
)
from .thinking_splitter import _ThinkTagSplitter

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════
# 基类
# ═══════════════════════════════════════════════════════════════════


class OpenAICompatibleProvider:
    """OpenAI chat.completions 协议的统一基类。

    子类必须定义:
        provider_name:    用于日志
        default_base_url: provider 默认端点(可被 LLMConfig.base_url 覆盖)

    子类可 override:
        _resolve_api_key():   从 LLMConfig + env 拿 key(默认只看 LLMConfig.api_key)
        _process_delta():     提取 thinking_delta(默认无 thinking)
        _get_extra_kwargs():  注入 provider 特有参数(如 GLM 的 do_sample)
    """

    provider_name: str = "openai-compatible"
    default_base_url: Optional[str] = None

    def __init__(self, config: LLMConfig) -> None:
        self.config = config
        self._client = None  # lazy

    # ── Client 管理 ───────────────────────────────────────────────

    @property
    def client(self):
        """懒加载 openai.OpenAI client(根据 base_url 走不同端点)"""
        if self._client is None:
            import openai
            self._client = openai.OpenAI(
                api_key=self._resolve_api_key(),
                base_url=self._get_base_url(),
            )
        return self._client

    def _get_base_url(self) -> Optional[str]:
        return self.config.base_url or self.default_base_url

    def _resolve_api_key(self) -> str:
        """默认:从 LLMConfig.api_key 读(已经包含所有 fallback 逻辑)"""
        return self.config.api_key

    # ── 请求体构造(100% 共享) ────────────────────────────────────

    def _convert_messages(self, messages: list[dict]) -> list[dict]:
        """Anthropic-style content list → OpenAI-style string。

        同时把 role 强制小写(OpenAI 协议要求)。"""
        converted: list[dict] = []
        for m in messages:
            role = m["role"].lower()
            content = m["content"]
            if isinstance(content, list):
                text_parts: list[str] = []
                for item in content:
                    if isinstance(item, dict):
                        if item.get("type") == "text":
                            text_parts.append(item.get("text", ""))
                        elif item.get("type") == "tool_result":
                            text_parts.append(f"[Tool Result: {item.get('content', '')}]")
                content = "\n".join(text_parts)
            converted.append({"role": role, "content": content})
        return converted

    def _convert_tools(self, tools: list[dict]) -> list[dict]:
        """Anthropic tool 格式({name, description, input_schema})
        → OpenAI tool 格式({type: function, function: {name, description, parameters}})

        若 tool 已是 OpenAI 格式(已经含 type='function'),原样保留。
        """
        converted: list[dict] = []
        for t in tools:
            if t.get("type") == "function" and "function" in t:
                converted.append(t)
            else:
                converted.append({
                    "type": "function",
                    "function": {
                        "name": t.get("name", ""),
                        "description": t.get("description", ""),
                        "parameters": t.get("input_schema", {}),
                    },
                })
        return converted

    def _build_kwargs(
        self,
        messages: list[dict],
        tools: Optional[list[dict]],
        tool_choice: Optional[str],
    ) -> dict:
        kwargs: dict = {
            "model": self.config.model,
            "messages": self._convert_messages(messages),
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if tools:
            kwargs["tools"] = self._convert_tools(tools)
            if tool_choice is not None:
                kwargs["tool_choice"] = tool_choice
        if self.config.temperature > 0:
            kwargs["temperature"] = self.config.temperature
        # 留给子类注入特有参数
        kwargs.update(self._get_extra_kwargs())
        return kwargs

    def _get_extra_kwargs(self) -> dict:
        """子类 override:注入 provider 特有参数(默认空)"""
        return {}

    # ── 流式循环(共享主体) ──────────────────────────────────────

    def chat(
        self,
        messages: list[dict],
        tools: Optional[list[dict]] = None,
        tool_choice: Optional[str] = None,
    ) -> Generator[StreamChunk, None, None]:
        """主入口:构造请求 + 启动 stream + 处理每个 chunk。"""
        kwargs = self._build_kwargs(messages, tools, tool_choice)
        yield from self._stream_with_buffer(kwargs)

    def _stream_with_buffer(self, kwargs: dict) -> Generator[StreamChunk, None, None]:
        """主 stream 循环:每 chunk 调 _process_delta,流结束后 yield 完整 tool_calls。"""
        tool_calls_buffer: dict = {}
        finish_reason: Optional[str] = None
        for chunk in self.client.chat.completions.create(**kwargs):
            if chunk.choices:
                delta = chunk.choices[0].delta
                # finish_reason 只在最后一个 chunk 非空(stop / tool_calls / length / ...)
                if chunk.choices[0].finish_reason:
                    finish_reason = chunk.choices[0].finish_reason
                yield from self._process_delta(delta, tool_calls_buffer)
            if hasattr(chunk, "usage") and chunk.usage:
                usage = UsageStats.from_chunk_usage(chunk.usage)
                logger.debug(f"[{self.provider_name}] usage: {usage.summary(self.provider_name)}")
                yield StreamChunk(usage=usage)

        # 流结束:yield 完整 tool_calls + 调子类钩子(flush splitter 等)
        yield from self._finalize_tool_calls(tool_calls_buffer)
        yield from self._on_stream_end()

        # 终止原因(OpenAI 兼容): stop / tool_calls / length / content_filter
        if finish_reason:
            yield StreamChunk(stop_reason=finish_reason)

    def _on_stream_end(self) -> Generator[StreamChunk, None, None]:
        """流结束钩子。子类 override 做兜底(如 MiniMax flush splitter 残留 buffer)。

        默认无操作。
        """
        return
        yield  # noqa: 让函数成为 generator

    def _process_delta(
        self,
        delta,
        tool_calls_buffer: dict,
    ) -> Generator[StreamChunk, None, None]:
        """处理单个 delta。子类可 override 加 thinking 提取。

        默认实现:
        - text_delta.content → StreamChunk(text_delta)
        - tool_calls 累加到 buffer(不 yield,等流结束统一 yield)
        """
        if delta.content:
            yield StreamChunk(text_delta=TextDelta(text=delta.content))
        if delta.tool_calls:
            for tc in delta.tool_calls:
                self._accumulate_tool_call(tc, tool_calls_buffer)

    def _accumulate_tool_call(self, tc, buffer: dict) -> None:
        """把单个 OpenAI tool_call delta 累加到 buffer(共享逻辑)。"""
        idx = tc.index
        if idx not in buffer:
            buffer[idx] = {"id": "", "name": "", "arguments": ""}
        if tc.id:
            buffer[idx]["id"] = tc.id
        if tc.function and tc.function.name:
            buffer[idx]["name"] = tc.function.name
        if tc.function and tc.function.arguments:
            buffer[idx]["arguments"] += tc.function.arguments

    def _finalize_tool_calls(self, buffer: dict) -> Generator[StreamChunk, None, None]:
        """流结束后 yield 完整 tool_calls(JSON 解析后的 input)"""
        for idx in sorted(buffer.keys()):
            tc = buffer[idx]
            if not (tc["id"] and tc["name"]):
                continue
            try:
                tool_input = json.loads(tc["arguments"]) if tc["arguments"] else {}
            except json.JSONDecodeError:
                tool_input = {"raw_arguments": tc["arguments"]}
            yield StreamChunk(
                tool_call=ToolCallDelta(
                    tool_name=tc["name"],
                    tool_input=tool_input,
                    tool_use_id=tc["id"],
                    is_final=True,
                )
            )


# ═══════════════════════════════════════════════════════════════════
# 3 个具体子类
# ═══════════════════════════════════════════════════════════════════


class OpenAIProvider(OpenAICompatibleProvider):
    """OpenAI 官方 GPT(无 thinking 提取)"""

    provider_name = "openai"
    default_base_url = None  # 走 OpenAI 官方端点


class ZhipuProvider(OpenAICompatibleProvider):
    """智谱 GLM(Coding Plan 专用端点)—— thinking 从 reasoning_content 流式取。"""

    provider_name = "zhipu"
    default_base_url = "https://open.bigmodel.cn/api/coding/paas/v4"

    def _resolve_api_key(self) -> str:
        from ..config import config as _config
        return self.config.api_key or _config.zhipu_api_key

    def _process_delta(self, delta, tool_calls_buffer):
        # GLM thinking 走独立 reasoning_content 字段,流式逐块到达
        # 与 Anthropic 不同:Anthropic SDK 不提供 thinking streaming delta
        if hasattr(delta, "reasoning_content") and delta.reasoning_content:
            yield StreamChunk(thinking_delta=ThinkingDelta(thinking=delta.reasoning_content))
        if delta.content:
            yield StreamChunk(text_delta=TextDelta(text=delta.content))
        if delta.tool_calls:
            for tc in delta.tool_calls:
                self._accumulate_tool_call(tc, tool_calls_buffer)


class MiniMaxProvider(OpenAICompatibleProvider):
    """MiniMax (MiniMax M3)—— thinking 可能在 reasoning_content 或 `` 标签里。"""

    provider_name = "minimax"
    default_base_url = "https://api.minimaxi.com/v1"

    def __init__(self, config: LLMConfig) -> None:
        super().__init__(config)
        # M3 等模型把 thinking 包在 <think>...</think> 标签里(text_delta 内)
        # 每次 chat() 重新创建,避免跨调用状态污染
        self._think_splitter = _ThinkTagSplitter()

    def _resolve_api_key(self) -> str:
        from ..config import config as _config
        return self.config.api_key or _config.minimax_api_key

    def _stream_with_buffer(self, kwargs):
        """Override:每 chat() 重置 splitter,避免上次流残留。"""
        self._think_splitter = _ThinkTagSplitter()
        yield from super()._stream_with_buffer(kwargs)

    def _process_delta(self, delta, tool_calls_buffer):
        # 优先 reasoning_content(若 model 支持,GLM 风格)
        if hasattr(delta, "reasoning_content") and delta.reasoning_content:
            yield StreamChunk(thinking_delta=ThinkingDelta(thinking=delta.reasoning_content))
        # 否则 text_delta 走 splitter 切 <think> 标签
        elif delta.content:
            yield from self._think_splitter.feed(delta.content)
        if delta.tool_calls:
            for tc in delta.tool_calls:
                self._accumulate_tool_call(tc, tool_calls_buffer)

    def _on_stream_end(self):
        """兜底:流结束 flush splitter 残留 buffer(不丢任何字符)"""
        yield from self._think_splitter.flush()


# ═══════════════════════════════════════════════════════════════════
# 工厂:LLMProvider → 具体 provider 类
# ═══════════════════════════════════════════════════════════════════


def create_openai_compatible_provider(config: LLMConfig) -> OpenAICompatibleProvider:
    """根据 LLMConfig.provider 返回对应 provider 实例。

    只处理走 OpenAI 协议的 3 个 provider(OPENAI / ZHIPU / MINIMAX)。
    Anthropic 不走这里。
    """
    from .router import LLMProvider
    mapping = {
        LLMProvider.OPENAI: OpenAIProvider,
        LLMProvider.ZHIPU: ZhipuProvider,
        LLMProvider.MINIMAX: MiniMaxProvider,
    }
    cls = mapping.get(config.provider)
    if cls is None:
        raise ValueError(
            f"Provider {config.provider} 不是 OpenAI 兼容协议,"
            f"不能走 create_openai_compatible_provider()"
        )
    return cls(config)


__all__ = [
    "OpenAICompatibleProvider",
    "OpenAIProvider",
    "ZhipuProvider",
    "MiniMaxProvider",
    "create_openai_compatible_provider",
]
