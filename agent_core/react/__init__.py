"""
ReAct inline-XML tool-call fallback (003-react-inline-xml-fallback-parser)

Per spec.md: when an LLM emits tool calls as inline XML text
(`<tool_call>{"name":"Bash","input":{...}}</tool_call>`) instead of structured
`tool_use` blocks, this parser recovers them as `ToolCallDelta` instances so
the ReAct SM transitions to EXECUTING_TOOLS instead of finalizing early.

公开 API:
- parse_inline_xml_tool_calls(text): parse text → ParseOutcome
- InlineToolCall / ParseOutcome: parser-internal dataclasses
- InlineXmlFallbackConfig: opt-out config (default enabled, lives in agent_core.config)

设计文档:specs/003-react-inline-xml-fallback-parser/{spec,plan,research,data-model}.md
"""

from __future__ import annotations

# 公开 API 重导出(per T007)
from .inline_xml_parser import (
    InlineToolCall,
    ParseOutcome,
    parse_inline_xml_tool_calls,
)
from agent_core.config import InlineXmlFallbackConfig


__all__ = [
    "InlineToolCall",
    "ParseOutcome",
    "parse_inline_xml_tool_calls",
    "InlineXmlFallbackConfig",
]
