"""
LLM Router — 统一多厂商 LLM 调用（支持流式输出）
支持：Anthropic Claude / OpenAI GPT
使用同步生成器，避免 Streamlit 的 async 复杂性。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from enum import Enum
from typing import Generator, Optional

from pydantic import BaseModel, Field


# ── 枚举与配置 ────────────────────────────────────────────────────────────

class LLMProvider(str, Enum):
    ANTHROPIC = "anthropic"
    OPENAI = "openai"
    ZHIPU = "zhipu"  # 智谱 GLM


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
    system_prompt: Optional[str] = None  # P2 新增：系统提示词


# ── 流式响应块定义 ─────────────────────────────────────────────────────────

@dataclass
class TextDelta:
    text: str
    is_final: bool = False


@dataclass
class ThinkingDelta:
    thinking: str
    is_final: bool = False


@dataclass
class ToolCallDelta:
    """工具调用增量（完整工具调用信息，非流式）"""
    tool_name: str
    tool_input: dict
    tool_use_id: str
    is_final: bool = True  # 当前实现为完整返回，非增量


def _get_int(obj, *attrs, default=0):
    """从 Pydantic model 或 dict 中取第一个存在的整数字段"""
    for attr in attrs:
        val = getattr(obj, attr, None) if hasattr(obj, attr) else obj.get(attr) if isinstance(obj, dict) else None
        if val is not None:
            return int(val)
    return default


def _get_nested(obj, *attrs, default=0):
    """取嵌套属性：先取中间对象，再取目标字段（Pydantic+dict 混合）"""
    cur = obj
    for attr in attrs:
        if cur is None:
            return default
        cur = getattr(cur, attr, None) if hasattr(cur, attr) else cur.get(attr) if isinstance(cur, dict) else None
    if cur is None:
        return default
    return int(cur) if isinstance(cur, (int, float)) else default


@dataclass
class UsageStats:
    """Token 消耗统计（统一格式，适配所有 LLM provider）"""
    input_tokens: int = 0
    output_tokens: int = 0
    thinking_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens + self.thinking_tokens

    @classmethod
    def from_chunk_usage(cls, usage_obj) -> "UsageStats":
        """
        从不同 LLM provider 的 chunk.usage 对象提取统计，自动适配字段名。

        支持的格式：
        - Anthropic:     input_tokens, output_tokens, thinking_tokens
        - OpenAI/GLM:    prompt_tokens, completion_tokens,
                         completion_tokens_details.reasoning_tokens
        """
        if usage_obj is None:
            return cls()

        # input_tokens / prompt_tokens
        input_tokens = _get_int(usage_obj, "input_tokens", "prompt_tokens")
        # output_tokens / completion_tokens
        output_tokens = _get_int(usage_obj, "output_tokens", "completion_tokens")
        # thinking_tokens（优先 Anthropic 格式，其次 GLM/OpenAI 嵌套格式）
        thinking_tokens = _get_int(usage_obj, "thinking_tokens")
        if not thinking_tokens:
            # GLM/OpenAI: completion_tokens_details.reasoning_tokens
            thinking_tokens = _get_nested(
                usage_obj, "completion_tokens_details", "reasoning_tokens"
            )

        return cls(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            thinking_tokens=thinking_tokens,
        )

    def summary(self, provider: str = "") -> str:
        """格式化统计摘要（用于日志/UI）"""
        parts = [f"in={self.input_tokens:,}", f"out={self.output_tokens:,}"]
        if self.thinking_tokens:
            parts.append(f"think={self.thinking_tokens:,}")
        parts.append(f"total={self.total_tokens:,}")
        if provider:
            return f"[{provider}] " + " · ".join(parts)
        return " · ".join(parts)


@dataclass
class StreamChunk:
    """
    统一的流式响应块。
    每次 yield 其中一种 delta，最终 yield 一个带 usage 的块。
    """
    text_delta: Optional[TextDelta] = None
    thinking_delta: Optional[ThinkingDelta] = None
    tool_call: Optional[ToolCallDelta] = None  # 工具调用（完整返回，非增量）
    usage: Optional[UsageStats] = None


# ── LLM Router ───────────────────────────────────────────────────────────────

class LLMRouter:
    """
    多厂商 LLM 统一调用路由。
    使用同步生成器（Streamlit 兼容更好）。
    """

    def __init__(self, config: LLMConfig):
        self.config = config
        self._anthropic_client = None
        self._openai_client = None
        self._zhipu_client = None

    # ── 懒加载客户端 ────────────────────────────────────────────────────────

    def _get_anthropic_client(self):
        if self._anthropic_client is None:
            import anthropic
            api_key = self.config.api_key or os.getenv("ANTHROPIC_API_KEY", "")
            self._anthropic_client = anthropic.Anthropic(api_key=api_key)
        return self._anthropic_client

    def _get_openai_client(self):
        if self._openai_client is None:
            import openai
            api_key = self.config.api_key or os.getenv("OPENAI_API_KEY", "")
            kwargs = {"api_key": api_key}
            if self.config.base_url:
                kwargs["base_url"] = self.config.base_url
            self._openai_client = openai.OpenAI(**kwargs)
        return self._openai_client

    # ── 流式调用入口 ─────────────────────────────────────────────────────────

    def chat(
        self,
        messages: list[dict],
        tools: Optional[list[dict]] = None,
    ) -> Generator[StreamChunk, None, None]:
        """
        统一 chat 接口，返回 StreamChunk 生成器（同步）。
        根据 provider 路由到不同厂商。
        
        特殊处理 system message：
        - Anthropic: 提取 system 作为顶层参数
        - OpenAI/Zhipu: 保留在 messages 中
        """
        provider = self.config.provider.value if isinstance(self.config.provider, Enum) else self.config.provider
        
        # 提取 system message（如果有）
        system_message = None
        filtered_messages = []
        for m in messages:
            if m.get("role") == "system":
                system_message = m.get("content", "")
            else:
                filtered_messages.append(m)
        
        if provider == "anthropic":
            yield from self._chat_anthropic(filtered_messages, tools, system_message)
        elif provider == "openai":
            yield from self._chat_openai(messages, tools)  # OpenAI 保留 system
        elif provider == "zhipu":
            yield from self._chat_zhipu(messages, tools)  # Zhipu 保留 system
        else:
            raise ValueError(f"不支持的厂商: {self.config.provider}")

    # ── Anthropic 流式实现 ─────────────────────────────────────────────────

    def _chat_anthropic(
        self,
        messages: list[dict],
        tools: Optional[list[dict]],
        system_message: Optional[str] = None,  # P2 新增：system prompt
    ) -> Generator[StreamChunk, None, None]:
        """Anthropic Claude 流式调用（同步，支持 thinking blocks）"""
        client = self._get_anthropic_client()

        kwargs: dict = {
            "model": self.config.model,
            "max_tokens": self.config.max_tokens,
            "messages": messages,
        }
        if tools:
            kwargs["tools"] = tools
        if self.config.temperature > 0:
            kwargs["temperature"] = self.config.temperature
        
        # P2 新增：添加 system prompt（Anthropic 特有格式）
        if system_message:
            kwargs["system"] = system_message

        # 开启思考过程（Claude 3.7+）
        if self.config.thinking and self.config.thinking.enabled:
            kwargs["thinking"] = {
                "type": "enabled",
                "budget_tokens": self.config.thinking.budget_tokens,
            }

        # 使用 stream() 上下文管理器（同步）
        with client.messages.stream(**kwargs) as stream:
            # text_stream 是同步生成器，逐 token 输出
            for text_delta in stream.text_stream:
                if text_delta:
                    yield StreamChunk(text_delta=TextDelta(text=text_delta))

            # 获取完整消息（包含 thinking blocks 和 tool_use blocks）
            final_message = stream.get_final_message()

            # 提取 thinking blocks（如果有）
            for block in final_message.content:
                if block.type == "thinking":
                    thinking_text = getattr(block, "thinking", "")
                    if thinking_text:
                        yield StreamChunk(
                            thinking_delta=ThinkingDelta(
                                thinking=thinking_text,
                                is_final=True,
                            )
                        )

            # 提取 tool_use blocks（如果有）←── 新增：用于 ReAct 工具调用
            for block in final_message.content:
                if block.type == "tool_use":
                    yield StreamChunk(
                        tool_call=ToolCallDelta(
                            tool_name=block.name,
                            tool_input=dict(block.input),  # Anthropic SDK 的 input 是特殊类型，转 dict
                            tool_use_id=block.id,
                            is_final=True,
                        )
                    )

            # 返回 Token 消耗
            usage = UsageStats(
                input_tokens=final_message.usage.input_tokens,
                output_tokens=final_message.usage.output_tokens,
                thinking_tokens=getattr(
                    final_message.usage, "thinking_tokens", 0
                ),
            )
            yield StreamChunk(usage=usage)

    # ── OpenAI 流式实现 ────────────────────────────────────────────────────

    def _chat_openai(
        self,
        messages: list[dict],
        tools: Optional[list[dict]],
    ) -> Generator[StreamChunk, None, None]:
        """OpenAI GPT 流式调用（同步，支持 tool_calls 解析）"""
        client = self._get_openai_client()

        kwargs: dict = {
            "model": self.config.model,
            "max_tokens": self.config.max_tokens,
            "messages": messages,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if tools:
            kwargs["tools"] = [{"type": "function", "function": t} for t in tools]
        if self.config.temperature > 0:
            kwargs["temperature"] = self.config.temperature

        stream = client.chat.completions.create(**kwargs)

        # 收集 tool_calls（流式响应中可能分散在多个 chunk）
        tool_calls_buffer = {}  # index -> {"id": ..., "name": ..., "arguments": ...}

        for chunk in stream:
            delta = chunk.choices[0].delta

            # 文本增量
            if delta.content:
                yield StreamChunk(text_delta=TextDelta(text=delta.content))

            # 工具调用增量（OpenAI 格式）
            if delta.tool_calls:
                for tc in delta.tool_calls:
                    idx = tc.index
                    if idx not in tool_calls_buffer:
                        tool_calls_buffer[idx] = {"id": "", "name": "", "arguments": ""}
                    if tc.id:
                        tool_calls_buffer[idx]["id"] = tc.id
                    if tc.function and tc.function.name:
                        tool_calls_buffer[idx]["name"] = tc.function.name
                    if tc.function and tc.function.arguments:
                        tool_calls_buffer[idx]["arguments"] += tc.function.arguments

            # Token 消耗（最后一个 chunk 携带，自动适配字段名）
            if hasattr(chunk, "usage") and chunk.usage:
                usage = UsageStats.from_chunk_usage(chunk.usage)
                logger.debug(f"[OpenAI] usage: {usage.summary('OpenAI')}")
                yield StreamChunk(usage=usage)

        # 流式结束后，yield 完整的 thinking（GLM reasoning_content）
        if reasoning_buffer:
            full_thinking = "".join(reasoning_buffer)
            yield StreamChunk(thinking_delta=ThinkingDelta(thinking=full_thinking))

        # 流式结束后，如果有完整的 tool_calls，yield 它们
        for idx in sorted(tool_calls_buffer.keys()):
            tc = tool_calls_buffer[idx]
            if tc["id"] and tc["name"]:
                import json
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

    # ── 智谱 GLM 流式实现 ───────────────────────────────────────────────

    def _get_zhipu_client(self):
        """获取智谱客户端（Coding Plan 专用，复用 Anthropic 兼容协议）"""
        if self._zhipu_client is None:
            import openai
            api_key = self.config.api_key or os.getenv("ZHIPU_API_KEY", "")
            # Coding Plan 专用端点
            base_url = self.config.base_url or "https://open.bigmodel.cn/api/coding/paas/v4"
            self._zhipu_client = openai.OpenAI(
                api_key=api_key,
                base_url=base_url,
            )
        return self._zhipu_client

    def _chat_zhipu(
        self,
        messages: list[dict],
        tools: Optional[list[dict]],
    ) -> Generator[StreamChunk, None, None]:
        """智谱 GLM 流式调用（Coding Plan 专用端点）"""
        client = self._get_zhipu_client()

        # 转换 messages 格式：智谱要求 role 为小写，content 为 string
        zhipu_messages = []
        for m in messages:
            role = m["role"].lower()
            content = m["content"]
            # 如果 content 是 list（Anthropic 格式），转换为 string
            if isinstance(content, list):
                # 提取 text 内容，忽略 tool_result 等
                text_parts = []
                for item in content:
                    if isinstance(item, dict):
                        if item.get("type") == "text":
                            text_parts.append(item.get("text", ""))
                        elif item.get("type") == "tool_result":
                            # tool_result 转为文本描述
                            text_parts.append(f"[Tool Result: {item.get('content', '')}]")
                content = "\n".join(text_parts)
            zhipu_messages.append({"role": role, "content": content})

        kwargs: dict = {
            "model": self.config.model,
            "messages": zhipu_messages,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if tools:
            # Zhipu 需要 OpenAI 格式：{"type": "function", "function": {...}}
            # 输入的 tools 可能是 Anthropic 格式，需要转换
            openai_tools = []
            for t in tools:
                if "type" in t and t.get("type") == "function":
                    # 已经是 OpenAI 格式
                    openai_tools.append(t)
                else:
                    # Anthropic 格式：{"name": ..., "description": ..., "input_schema": ...}
                    openai_tools.append({
                        "type": "function",
                        "function": {
                            "name": t.get("name", ""),
                            "description": t.get("description", ""),
                            "parameters": t.get("input_schema", {}),
                        }
                    })
            kwargs["tools"] = openai_tools
        if self.config.temperature > 0:
            kwargs["temperature"] = self.config.temperature

        stream = client.chat.completions.create(**kwargs)

        # 收集 tool_calls（流式响应中可能分散在多个 chunk）
        tool_calls_buffer = {}  # index -> {"id": ..., "name": ..., "arguments": ...}

        # GLM thinking 内容缓冲区（reasoning_content 字段，逐块到达）
        reasoning_buffer = []

        for chunk in stream:
            if chunk.choices:
                delta = chunk.choices[0].delta

                # 文本增量
                if delta.content:
                    yield StreamChunk(text_delta=TextDelta(text=delta.content))

                # GLM thinking 内容增量（reasoning_content 字段）
                if hasattr(delta, 'reasoning_content') and delta.reasoning_content:
                    reasoning_buffer.append(delta.reasoning_content)
                
                # 工具调用增量（OpenAI 格式）
                if delta.tool_calls:
                    for tc in delta.tool_calls:
                        idx = tc.index
                        if idx not in tool_calls_buffer:
                            tool_calls_buffer[idx] = {"id": "", "name": "", "arguments": ""}
                        if tc.id:
                            tool_calls_buffer[idx]["id"] = tc.id
                        if tc.function and tc.function.name:
                            tool_calls_buffer[idx]["name"] = tc.function.name
                        if tc.function and tc.function.arguments:
                            tool_calls_buffer[idx]["arguments"] += tc.function.arguments

            if hasattr(chunk, "usage") and chunk.usage:
                usage = UsageStats.from_chunk_usage(chunk.usage)
                yield StreamChunk(usage=usage)

        # 流式结束后，yield 完整的 thinking（GLM reasoning_content）
        if reasoning_buffer:
            full_thinking = "".join(reasoning_buffer)
            yield StreamChunk(thinking_delta=ThinkingDelta(thinking=full_thinking))

        # 流式结束后，如果有完整的 tool_calls，yield 它们
        for idx in sorted(tool_calls_buffer.keys()):
            tc = tool_calls_buffer[idx]
            if tc["id"] and tc["name"]:
                import json
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


# ── 便捷工厂函数 ────────────────────────────────────────────────────────────

def create_router(
    provider: str = "anthropic",
    model: str = "",
    api_key: str = "",
    **kwargs,
) -> LLMRouter:
    """快速创建 LLM Router"""
    from enum import Enum as _Enum
    config = LLMConfig(
        provider=LLMProvider(provider),
        model=model or _default_model(provider),
        api_key=api_key,
        **kwargs,
    )
    return LLMRouter(config)


def _default_model(provider: str) -> str:
    """从环境变量读取默认模型，没有则返回空字符串"""
    # 如果 DEFAULT_PROVIDER 匹配当前 provider，返回 DEFAULT_MODEL
    if os.getenv("DEFAULT_PROVIDER", "").lower() == provider:
        return os.getenv("DEFAULT_MODEL", "")
    return ""
