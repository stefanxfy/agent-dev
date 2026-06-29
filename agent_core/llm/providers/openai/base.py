"""OpenAI 兼容协议 Provider 基类(2026-06-29 LLM Router 重构 Stage 3)。

设计:
- 共享 message 转换、tool 转换、stream 循环、tool_calls 缓冲
- 3 个子类(OpenAI / Zhipu / MiniMax)只 override 真正不同的部分
  - provider_name / default_base_url / env_key
  - `_extract_thinking(delta)` — thinking 提取(无 / reasoning_content / <think> 标签)
  - `_resolve_api_key()` — env 兜底
  - `_on_stream_end()` — flush 兜底(MiniMax 切 splitter buffer)

关键安全机制:_consumed_text 哨兵
- 解决 splitter 吃掉的文本不泄漏到 text 流的核心问题
- _extract_thinking 调用 splitter.feed(delta.content) → splitter consume 了内容
  → 设置 self._consumed_text = True
- 基类 _process_delta 检查哨兵:True 则跳过 text_delta yield 并重置
"""
from __future__ import annotations

import json
import logging
from typing import Generator, Optional, TYPE_CHECKING

from ..base import BaseProvider
from ...types import StreamChunk, TextDelta, ToolCallDelta, UsageStats

if TYPE_CHECKING:
    from ...config import LLMConfig

logger = logging.getLogger("llm.providers.openai.base")


class OpenAICompatibleProvider(BaseProvider):
    """OpenAI chat.completions 协议的统一基类。

    子类必须定义:
        provider_name:    用于日志
        default_base_url: provider 默认端点(可被 LLMConfig.base_url 覆盖)

    子类可 override:
        _resolve_api_key():    从 LLMConfig + env 拿 key(默认只看 LLMConfig.api_key)
        _extract_thinking():   提取 thinking_delta(默认无 thinking)
        _get_extra_kwargs():   注入 provider 特有参数(如 GLM 的 do_sample)
        _on_stream_end():      流结束钩子(flush splitter 残留 buffer)
    """

    provider_name: str = ""
    default_base_url: Optional[str] = None
    env_key: str = ""

    def __init__(self, config: "LLMConfig") -> None:
        super().__init__(config)
        self._client = None  # lazy
        # ⚠️ 关键哨兵:splitter consume 后设为 True,基类 _process_delta 检查后重置
        self._consumed_text: bool = False

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
        """默认:从 LLMConfig.api_key 读(env 兜底由子类决定)"""
        return self.config.api_key

    # ── 请求体构造(100% 共享) ────────────────────────────────────

    def _convert_messages(self, messages: list[dict]) -> list[dict]:
        """Anthropic-style content list → OpenAI-style string。"""
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
        """Anthropic tool 格式 → OpenAI tool 格式(若已是 OpenAI 格式则原样保留)"""
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
        kwargs.update(self._get_extra_kwargs())
        return kwargs

    def _get_extra_kwargs(self) -> dict:
        """子类 override:注入 provider 特有参数(默认空)"""
        return {}

    # ── Template Method 子类入口(替代原 _stream_with_buffer) ─────

    def _do_chat(
        self,
        messages: list[dict],
        tools: Optional[list[dict]] = None,
        tool_choice: Optional[str] = None,
        system_prompt: Optional[str] = None,  # OpenAI 兼容没有顶层 system,需要注入 messages
        cache_namespace: Optional[str] = None,  # OpenAI 兼容不实现
    ) -> Generator[StreamChunk, None, None]:
        """OpenAI 兼容 stream 主循环。

        重置哨兵 + system_prompt 注入 messages + 调 _build_kwargs + 跑 stream。
        """
        # 重置哨兵:每次新 chat() 调用前清零(防御多次复用同一 provider 实例)
        self._consumed_text = False

        # OpenAI 兼容协议没有顶层 system 参数,需要把 system_prompt 注入到 messages 头
        # ⚠️ Bug 防御:用真值判断(`if system_prompt:`),空字符串也算 falsy → 不注入
        # 这与主 agent 路径(空 system_prompt 时不发送 system)对齐,保持 cache prefix 一致
        if system_prompt:
            messages = [{"role": "system", "content": system_prompt}, *messages]

        kwargs = self._build_kwargs(messages, tools, tool_choice)

        tool_calls_buffer: dict = {}
        finish_reason: Optional[str] = None
        for chunk in self.client.chat.completions.create(**kwargs):
            if chunk.choices:
                delta = chunk.choices[0].delta
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

        if finish_reason:
            yield StreamChunk(stop_reason=finish_reason)

    def _on_stream_end(self) -> Generator[StreamChunk, None, None]:
        """流结束钩子。子类 override 做兜底(如 MiniMax flush splitter 残留 buffer)。"""
        if False:
            yield  # type: ignore[unreachable]
        return

    def _extract_thinking(self, delta) -> Generator[StreamChunk, None, None]:
        """子类 override:提取 thinking_delta(默认无 thinking)。

        重要约定:若子类 consume 了 `delta.content`(如 splitter 切走 <think> 块),
        **必须**设置 `self._consumed_text = True`,否则切走的文本会泄漏到 text 流。
        """
        if False:
            yield  # type: ignore[unreachable]
        return

    def _process_delta(self, delta, tool_calls_buffer: dict) -> Generator[StreamChunk, None, None]:
        """处理单个 delta。流程:
        1. 调 _extract_thinking → 子类可能 yield thinking chunks + 设置 _consumed_text
        2. 若 _consumed_text=False,正常 yield text_delta
        3. 始终 yield tool_call 累积
        """
        yield from self._extract_thinking(delta)

        # ⚠️ 关键:若 _extract_thinking consume 了 delta.content,跳过 text_delta
        if getattr(self, "_consumed_text", False):
            self._consumed_text = False
        elif delta.content:
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


__all__ = ["OpenAICompatibleProvider"]
