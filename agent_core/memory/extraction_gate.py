"""
三级门决策树（v2.1.1 用户调整版）
参考 docs/memory-system-design.md §3.3.1
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from agent_core.memory.dual_channel_writer import ExtractionCandidate


@dataclass
class TurnContext:
    session_id: str
    cumulative_tokens: int
    cumulative_tool_calls: int
    last_messages: list[dict]
    gate1_period_start_turn: int = 0


@dataclass
class Decision:
    should_extract: bool
    reason: str
    confidence: float = 0.0
    candidates: list[ExtractionCandidate] = field(default_factory=list)
    via_gate1: bool = False


class ExtractionGate:
    """
    三级门 OR 关系决策树:
      门1（累计）OR 门2（关键词）→ 门3（LLM 评分）
    """

    MIN_TOKENS_TO_INIT = 10_000
    MIN_TOOL_CALLS = 10
    MIN_CONFIDENCE = 0.6

    KEYWORDS = [
        "记住", "记一下", "帮我记住", "别忘了",
        "偏好", "决策", "选择", "拒绝", "采用",
        "教训", "经验", "原则",
        "总是", "从不", "永远", "习惯",
    ]

    def should_extract(self, ctx: TurnContext) -> Decision:
        gate1_pass = (
            ctx.cumulative_tokens >= self.MIN_TOKENS_TO_INIT
            or ctx.cumulative_tool_calls >= self.MIN_TOOL_CALLS
        )
        gate2_pass = self._keyword_filter(ctx.last_messages)

        if not (gate1_pass or gate2_pass):
            return Decision(
                should_extract=False,
                reason="no_trigger(gate1_no_threshold, gate2_no_keyword)",
                via_gate1=False,
            )

        # 占位:门3 LLM 评分后续 task 接入
        return Decision(
            should_extract=False,
            reason="gate3_not_implemented_yet",
            via_gate1=gate1_pass and not gate2_pass,
        )

    def _keyword_filter(self, last_messages: list[dict]) -> bool:
        text = " ".join(
            m.get("content", "")
            for m in last_messages
            if isinstance(m.get("content"), str)
        )
        return any(kw in text for kw in self.KEYWORDS)