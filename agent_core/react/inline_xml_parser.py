"""
Inline-XML tool-call parser (003-react-inline-xml-fallback-parser).

ReAct SM 在 LLM_THINKING 阶段接收 LLM stream。某些 provider (GLM-5.1 等)
不通过结构化 `tool_use` 块而是通过纯文本中的 inline-XML 表达工具调用:

    <tool_call>{"name":"Bash","input":{"command":"echo hi"}}</tool_call>

ChunkParseHandler 只会 emit 这种文本但不会解析它,导致 stage_outputs.tool_calls
为空 → LLMThinkingPhase.next 走 FINALIZING 路径,SM 提前收尾。

本模块的 `parse_inline_xml_tool_calls(text)` 在 ChunkParseHandler 之后被
InlineXmlFallbackHandler 调用,从 full_text 里抽出 `<tool_call>` 块并产出
`ToolCallDelta`,让 SM 正确走 EXECUTING_TOOLS 路径。

设计:
- 手写字节扫描器(per research.md §2):不用 regex,需要平衡大括号匹配 + 字符串字面量感知
- 块失败不抛异常:记入 malformed_blocks 让 handler 决定如何 degrade
- 严格 schema:`name` 必须是非空 str,`input` 必须是 dict
- 不读、不复制、不打印 input 内容(避免 secret 泄漏到 log — 见 002 三层 secret 防御)

公开 API(由 agent_core.react.__init__ 重导出):
    parse_inline_xml_tool_calls(text: str) -> ParseOutcome
    InlineToolCall, ParseOutcome
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import List, Optional


_logger = logging.getLogger("agent_core.react.inline_xml_fallback")


# 标记字面量
_OPEN_MARKER = "<tool_call>"
_CLOSE_MARKER = "</tool_call>"


@dataclass
class InlineToolCall:
    """parser 内部的单个 tool_call 表示。转 ToolCallDelta 后被消费;不持久化。"""
    name: str
    input: dict
    source_text_offset: int  # opening `<tool_call>` 在原 text 的字节 offset
    block_index: int         # 在本次响应中的块序号(0-based)
    is_final: bool = True    # 镜像 ToolCallDelta.is_final 约定(per data-model.md §ToolCallDelta)

    @property
    def tool_use_id(self) -> str:
        """synthetic id(per data-model.md §ToolCallDelta)。`inline_xml_{block_index}` 区分
        fallback 产出与结构化 `tool_use` 块,便于 audit log 追溯。"""
        return f"inline_xml_{self.block_index}"

    def to_tool_call_delta(self) -> "ToolCallDelta":
        """转 agent_core.llm.types.ToolCallDelta(handler 直接 append)。"""
        from agent_core.llm.types import ToolCallDelta
        return ToolCallDelta(
            tool_name=self.name,
            tool_input=self.input,
            tool_use_id=self.tool_use_id,
            is_final=self.is_final,
        )


@dataclass
class ParseOutcome:
    """parser 的总输出。"""
    tool_calls: List[InlineToolCall] = field(default_factory=list)
    malformed_blocks: List[int] = field(default_factory=list)
    # 解析器语义:发现 ≥1 个 valid 块即视为 fallback 候选。
    # handler 会再与 stage_outputs.tool_calls 状态合并决策是否激活。
    fallback_activated: bool = False
    original_text: str = ""


# ── Scanner 工具函数 ──────────────────────────────────────────────


def _is_ws(ch: str) -> bool:
    return ch in (" ", "\t", "\n", "\r")


def _find_marker(text: str, start: int) -> Optional[int]:
    """从 start 起找下一个 `<tool_call>` 出现的字节 offset,没找到返回 None。"""
    return text.find(_OPEN_MARKER, start)


def _extract_balanced_json(text: str, brace_start: int) -> Optional[tuple[str, int]]:
    """
    从 brace_start(指向 '{')起读一个平衡大括号对象,字符串字面量内的括号不计数。

    返回 (json_substring, end_offset_exclusive);失败返回 None。
    end_offset_exclusive 是 '}' 后第一个字符的 offset(用于接着跳 ws + close marker)。
    """
    n = len(text)
    if brace_start >= n or text[brace_start] != "{":
        return None

    depth = 0
    i = brace_start
    in_string = False
    escape = False

    while i < n:
        ch = text[i]
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            # else: 字符在 string 内,统一忽略(包含 { } 等)
        else:
            if ch == '"':
                in_string = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return text[brace_start : i + 1], i + 1
            # 其他字符(逗号/冒号/数字/字母等)忽略
        i += 1

    return None  # 未闭合


# ── Public API ─────────────────────────────────────────────────────


def parse_inline_xml_tool_calls(text: str) -> ParseOutcome:
    """
    从 assistant text 中提取所有 `<tool_call>{...}</tool_call>` 块。

    行为契约(per contracts/inline-xml-format.md):
    - 多个块按文档顺序抽取,block_index 从 0 起
    - 单块失败(JSON 不合法 / 缺字段 / 字段类型错)→ 记入 malformed_blocks
      并发 WARN log(只含 offset + 原因类名,**绝不包含 input 内容**),
      其余块继续
    - 全失败 → tool_calls 为空,fallback_activated=False
    - 至少一个 valid 块 → fallback_activated=True
    """
    if not text:
        return ParseOutcome(original_text=text or "")

    outcome = ParseOutcome(original_text=text)
    cursor = 0
    n = len(text)

    while cursor < n:
        marker_start = _find_marker(text, cursor)
        if marker_start < 0:
            break

        # marker 之后第一个非空白字符应是 '{'
        j = marker_start + len(_OPEN_MARKER)
        while j < n and _is_ws(text[j]):
            j += 1
        if j >= n or text[j] != "{":
            # 标记后无 JSON,记 malformed
            _log_malformed(len(outcome.tool_calls) + len(outcome.malformed_blocks), marker_start,
                           ValueError("no_json_object_after_marker"))
            outcome.malformed_blocks.append(len(outcome.tool_calls) + len(outcome.malformed_blocks))
            cursor = marker_start + len(_OPEN_MARKER)
            continue

        brace_start = j
        extracted = _extract_balanced_json(text, brace_start)
        if extracted is None:
            _log_malformed(len(outcome.tool_calls) + len(outcome.malformed_blocks), marker_start,
                           ValueError("unbalanced_braces"))
            outcome.malformed_blocks.append(len(outcome.tool_calls) + len(outcome.malformed_blocks))
            cursor = marker_start + len(_OPEN_MARKER)
            continue

        json_str, json_end = extracted

        # 跳过空白,期待关闭标记
        k = json_end
        while k < n and _is_ws(text[k]):
            k += 1
        if not text.startswith(_CLOSE_MARKER, k):
            _log_malformed(len(outcome.tool_calls) + len(outcome.malformed_blocks), marker_start,
                           ValueError("missing_close_marker"))
            outcome.malformed_blocks.append(len(outcome.tool_calls) + len(outcome.malformed_blocks))
            cursor = marker_start + len(_OPEN_MARKER)
            continue

        # 成功捕获一个块。开始 schema 校验。
        block_index = len(outcome.tool_calls) + len(outcome.malformed_blocks)
        try:
            obj = json.loads(json_str)
        except (ValueError, TypeError) as e:
            _log_malformed(block_index, marker_start, e)
            outcome.malformed_blocks.append(block_index)
            cursor = k + len(_CLOSE_MARKER)
            continue

        if not isinstance(obj, dict):
            _log_malformed(block_index, marker_start, ValueError("not_a_json_object"))
            outcome.malformed_blocks.append(block_index)
            cursor = k + len(_CLOSE_MARKER)
            continue

        name = obj.get("name")
        inp = obj.get("input")
        if not isinstance(name, str) or not name:
            _log_malformed(block_index, marker_start, ValueError("missing_or_invalid_name"))
            outcome.malformed_blocks.append(block_index)
            cursor = k + len(_CLOSE_MARKER)
            continue
        if not isinstance(inp, dict):
            _log_malformed(block_index, marker_start, ValueError("missing_or_invalid_input"))
            outcome.malformed_blocks.append(block_index)
            cursor = k + len(_CLOSE_MARKER)
            continue

        outcome.tool_calls.append(
            InlineToolCall(
                name=name,
                input=inp,
                source_text_offset=marker_start,
                block_index=block_index,
            )
        )
        cursor = k + len(_CLOSE_MARKER)

    outcome.fallback_activated = len(outcome.tool_calls) > 0
    return outcome


def _log_malformed(block_index: int, offset: int, exc: BaseException) -> None:
    """
    单行 WARN(per FR-003 + T022)。**绝不**包含 input / marker 之间的内容,
    防止 secret 泄漏(对齐 002-skill-secret-injection 三层 secret 防御)。
    """
    _logger.warning(
        "🧩 react.inline_xml_fallback: malformed block index=%d offset=%d (reason=%s)",
        block_index, offset, type(exc).__name__,
    )


__all__ = [
    "InlineToolCall",
    "ParseOutcome",
    "parse_inline_xml_tool_calls",
]
