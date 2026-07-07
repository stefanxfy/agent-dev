"""
T024 — byte-equal log scan:确认激活 fallback 时无 input 字节泄漏。
借鉴 002-skill-secret-injection SC-005 (scripts/verify_skill_secrets_audit.py) 模式。
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from agent_core.turn_chain import (
    InlineXmlFallbackHandler,
    _LLMResult,
    TurnContext,
    AgentPhase,
)


FIXTURES_DIR = Path(__file__).parent / "fixtures" / "inline_xml"


def _load_fixture_first(name: str) -> dict:
    p = FIXTURES_DIR / name
    return json.loads([l for l in p.read_text().splitlines() if l.strip()][0])


def test_byte_equal_log_scan_no_input_bytes(caplog):
    case = _load_fixture_first("02_multiple_blocks.jsonl")
    canary = "CANARY_SECRET_XYZ"
    # 在 fixture text 里塞入 canary,确保 parser 解析后它出现在 input dict 里
    text_with_canary = case["text"].replace(
        '"command":"ls /tmp"',
        f'"command":"ls /tmp; echo {canary}"',
    )
    stage = _LLMResult(
        tool_calls=[],
        full_text=text_with_canary,
        stop_reason="end_turn",
    )
    ctx = TurnContext(run_state=None)
    ctx.stage_outputs = stage

    handler = InlineXmlFallbackHandler(agent=None)
    with caplog.at_level(logging.DEBUG):
        handler.handle(ctx)

    # 扫描所有捕获的 log 字节(不只 agent_core 子 logger,避免漏)
    full_text = "\n".join(r.getMessage() for r in caplog.records)
    full_text += "\n".join(
        f"{r.name}:{r.levelname}:{r.getMessage()}" for r in caplog.records
    )
    assert canary not in full_text, (
        f"Canary substring leaked to logs!\n{canary!r} present in:\n{full_text!r}"
    )
