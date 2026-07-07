"""
Parser tests for 003-react-inline-xml-fallback-parser.

TDD: these tests are written FIRST per Constitution Principle IV.
T008 + T009 are US1 tests (single block + ToolCallDelta shape).
T014 + T015 are US2 tests (multiple blocks, document order).
T017-T021 are US3 tests (malformed/empty/wrong-type).
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from agent_core.react import (
    InlineToolCall,
    ParseOutcome,
    parse_inline_xml_tool_calls,
)
from agent_core.llm.types import ToolCallDelta


FIXTURES_DIR = Path(__file__).parent / "fixtures" / "inline_xml"


def _load_fixture(name: str) -> list[dict]:
    """加载一行 JSONL fixture → dict(只取第一个 case)。"""
    p = FIXTURES_DIR / name
    lines = [l for l in p.read_text().splitlines() if l.strip()]
    assert lines, f"fixture {name} is empty"
    return [json.loads(lines[0])]


# ── US1: T008 — single block parses to one tool call ─────────────


class TestUS1SingleBlock:
    def test_single_block_parses_to_one_tool_call(self):
        case = _load_fixture("01_single_block.jsonl")[0]
        out = parse_inline_xml_tool_calls(case["text"])
        assert len(out.tool_calls) == 1, f"expected 1, got {len(out.tool_calls)}"
        tc = out.tool_calls[0]
        assert tc.name == "Bash"
        assert tc.input == {"command": "echo hi"}
        assert tc.tool_use_id == "inline_xml_0"
        assert out.fallback_activated is True
        assert out.malformed_blocks == []

    def test_parser_produces_tool_call_delta_shape(self):
        """T009 — InlineToolCall has the 4 ToolCallDelta fields, is_final semantics
        由 handler 在 append ToolCallDelta 时设 True(per data-model §ToolCallDelta)。"""
        out = parse_inline_xml_tool_calls(
            '<tool_call>{"name":"Bash","input":{"command":"ls"}}</tool_call>'
        )
        assert len(out.tool_calls) == 1
        itc = out.tool_calls[0]
        # 全部 4 字段都存在(tool_name / tool_input / tool_use_id / is_final)
        assert hasattr(itc, "name")
        assert hasattr(itc, "input")
        assert hasattr(itc, "tool_use_id")
        assert hasattr(itc, "is_final")
        # parser 不直接产 ToolCallDelta — 由 handler 转;但 shape 字段齐
        assert itc.tool_use_id.startswith("inline_xml_")
        assert itc.is_final is True  # dataclass default;handler 也会显式设 True


# ── US2: T014 + T015 — multiple blocks ──────────────────────────


class TestUS2MultipleBlocks:
    def test_multiple_blocks_parse_in_document_order(self):
        case = _load_fixture("02_multiple_blocks.jsonl")[0]
        out = parse_inline_xml_tool_calls(case["text"])
        assert len(out.tool_calls) == 3
        assert out.tool_calls[0].tool_use_id == "inline_xml_0"
        assert out.tool_calls[1].tool_use_id == "inline_xml_1"
        assert out.tool_calls[2].tool_use_id == "inline_xml_2"
        names = [tc.name for tc in out.tool_calls]
        assert names == ["Read", "Bash", "Write"], f"document order broken: {names}"

    def test_five_blocks_linear_scaling(self):
        text = "".join(
            f'toolcall{i}<tool_call>{{"name":"T{i}","input":{{"i":{i}}}}}</tool_call>'
            for i in range(5)
        )
        out = parse_inline_xml_tool_calls(text)
        assert len(out.tool_calls) == 5
        assert [tc.name for tc in out.tool_calls] == [f"T{i}" for i in range(5)]
        assert out.tool_calls[0].source_text_offset == len("toolcall0")


# ── US3: T017 + T018 + T019 + T020 + T021 — malformed ────────────


class TestUS3Malformed:
    def test_malformed_inside_valid_skips_bad_keeps_good(self):
        case = _load_fixture("03_malformed_inside_valid.jsonl")[0]
        out = parse_inline_xml_tool_calls(case["text"])
        # fixture:3 个块,中间一个 broken brace → 应得 2 个 valid + [1] malformed
        assert len(out.tool_calls) == 2
        assert out.malformed_blocks == [1]
        assert out.fallback_activated is True

    def test_empty_markers_skipped(self):
        case = _load_fixture("04_empty_markers.jsonl")[0]
        out = parse_inline_xml_tool_calls(case["text"])
        assert out.tool_calls == []
        # fixture:2 个空标记,都是 malformed
        assert out.malformed_blocks == [0, 1]
        assert out.fallback_activated is False

    def test_only_malformed_blocks_returns_empty(self):
        out = parse_inline_xml_tool_calls(
            "<tool_call>not json</tool_call>"
        )
        assert out.tool_calls == []
        assert out.malformed_blocks == [0]
        assert out.fallback_activated is False

    def test_missing_required_field_rejected(self):
        out = parse_inline_xml_tool_calls(
            '<tool_call>{"name":"Bash"}</tool_call>'
        )
        assert out.tool_calls == []
        assert out.malformed_blocks == [0]

    def test_wrong_type_for_input_rejected(self):
        out = parse_inline_xml_tool_calls(
            '<tool_call>{"name":"Bash","input":"not an object"}</tool_call>'
        )
        assert out.tool_calls == []
        assert out.malformed_blocks == [0]


# ── Edge cases ────────────────────────────────────────────────────


class TestEdgeCases:
    def test_nested_braces_in_input(self):
        case = _load_fixture("05_nested_braces_in_input.jsonl")[0]
        out = parse_inline_xml_tool_calls(case["text"])
        assert len(out.tool_calls) == 1
        assert out.tool_calls[0].name == "Bash"
        # input 含嵌套 JSON,parser 用 balanced-brace 抽出
        assert "{" in out.tool_calls[0].input["command"]

    def test_adjacent_other_xml_ignored(self):
        case = _load_fixture("07_adjacent_other_xml.jsonl")[0]
        out = parse_inline_xml_tool_calls(case["text"])
        assert len(out.tool_calls) == 1
        assert out.tool_calls[0].name == "Bash"

    def test_all_providers(self):
        # FR-009:不依赖 provider。8_all_providers.jsonl 有 4 case,逐个过
        p = FIXTURES_DIR / "08_all_providers.jsonl"
        for line in p.read_text().splitlines():
            if not line.strip():
                continue
            case = json.loads(line)
            out = parse_inline_xml_tool_calls(case["text"])
            assert len(out.tool_calls) == 1, f"{case['name']}: expected 1, got {len(out.tool_calls)}"
            assert out.fallback_activated is True
