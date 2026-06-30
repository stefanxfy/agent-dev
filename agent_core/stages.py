"""
Stage Data Classes — handler 间强类型 contract

v2 重构引入(详见 docs/agent-state-machine-and-chain-of-responsibility-design.md §5):
- StageInputs:inputs_chain 输出(被 llm_chain 消费)
- LLMResult:llm_chain 输出(被 tool_chain 消费)
- ToolExecutionResult:tool_chain 输出(被 output_chain 消费)

为什么用 dataclass:
- v1 靠 ctx.xxx 字段名约定传递(handler 重排会静默失败)
- v2 强类型,handler 签名显式声明,type system 强制 contract
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class StageInputs:
    """inputs_chain 的输出(被 llm_chain 消费)。"""
    messages: list[dict]
    system_prompt: Optional[str] = None
    tool_schemas: list[dict] = field(default_factory=list)


@dataclass
class LLMResult:
    """llm_chain 的输出(被 tool_chain 消费)。

    关键字段:
    - chunks:原始 LLM chunks(供 ChunkParseHandler 解析)
    - full_text / thinking_text:累积文本
    - tool_calls:LLM 决定的 tool calls
    - tool_results:tool_chain 写入的执行结果(供 output_chain 落 session)
    - usage:token 用量
    - stop_reason:终止原因("end_turn" / "tool_use" / "max_tokens" / "error" / "interrupted")
    """
    chunks: list = field(default_factory=list)
    full_text: str = ""
    thinking_text: str = ""
    tool_calls: list = field(default_factory=list)
    tool_results: list[tuple[str, str]] = field(default_factory=list)  # (tool_use_id, output)
    usage: Optional[Any] = None
    stop_reason: Optional[str] = None


@dataclass
class ToolExecutionResult:
    """tool_chain 的输出(被 output_chain 消费)。"""
    tool_calls: list = field(default_factory=list)
    tool_results: list[tuple[str, str]] = field(default_factory=list)
    success_count: int = 0
    error_count: int = 0
    total_elapsed: float = 0.0
