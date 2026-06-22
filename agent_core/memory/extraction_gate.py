"""
三级门决策树（v2.1.1 用户调整版）
参考 docs/memory-system-design.md §3.3.1
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Optional, Protocol, Any

from agent_core.memory.dual_channel_writer import ExtractionCandidate
from agent_core.memory.prompt_templates import (
    EXTRACT_SYSTEM_PROMPT,
    build_extract_prompt,
)

logger = logging.getLogger("memory.extraction_gate")


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


class _LLMRouterProtocol(Protocol):
    """LLM router 最小接口(本类只用 chat)"""
    config: Any

    def chat(self, messages: list[dict], **kw): ...


class _MemoryStoreProtocol(Protocol):
    """本类只用 list_by_session"""
    def list_by_session(self, session_id: str, since_turn: int) -> list[dict]: ...


class ExtractionGate:
    """三级门 OR 关系决策树(§3.3.1)"""

    MIN_TOKENS_TO_INIT = 10_000
    MIN_TOOL_CALLS = 10
    MIN_CONFIDENCE = 0.6
    CACHE_NAMESPACE = "memory_extract_score"

    KEYWORDS = [
        "记住", "记一下", "帮我记住", "别忘了",
        "偏好", "决策", "选择", "拒绝", "采用",
        "教训", "经验", "原则",
        "总是", "从不", "永远", "习惯",
    ]

    def __init__(
        self,
        llm_router: _LLMRouterProtocol,
        memory_store: _MemoryStoreProtocol,
        session_id: str,
        cache_namespace: Optional[str] = None,
    ):
        self.llm_router = llm_router
        self.memory_store = memory_store
        self.session_id = session_id
        self.cache_namespace = cache_namespace or self.CACHE_NAMESPACE

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

        # 门3:LLM 评分
        return self._llm_score(ctx, via_gate1=gate1_pass and not gate2_pass)

    def _keyword_filter(self, last_messages: list[dict]) -> bool:
        text = " ".join(
            m.get("content", "")
            for m in last_messages
            if isinstance(m.get("content"), str)
        )
        return any(kw in text for kw in self.KEYWORDS)

    def _llm_score(self, ctx: TurnContext, *, via_gate1: bool) -> Decision:
        """门3:LLM 一次调用,既评分又提取(§3.3 L1 合并)"""
        # 拼 turns_text(取 gate1 周期内的 turn)
        turns_text = "\n".join(
            f"[turn {i}] {m.get('content', '')[:200]}"
            for i, m in enumerate(ctx.last_messages)
        )

        # 拼已有记忆(门1 触发时让 LLM 看到已提过的,避免重复)
        try:
            existing = self.memory_store.list_by_session(
                session_id=ctx.session_id,
                since_turn=ctx.gate1_period_start_turn,
            )
        except Exception as e:
            logger.warning(f"list_by_session 失败,降级为空: {e}")
            existing = []

        prompt = build_extract_prompt(turns_text, existing)

        # 调 LLM(用 cache_namespace 隔离)
        try:
            text = self._call_llm(prompt)
        except Exception as e:
            logger.warning(f"LLM 评分调用失败: {e}")
            return Decision(
                should_extract=False,
                reason=f"llm_call_error({type(e).__name__})",
                via_gate1=via_gate1,
            )

        # 解析 JSON
        try:
            data = json.loads(text.strip())
        except json.JSONDecodeError as e:
            logger.warning(f"LLM 评分解析失败: {e}, raw={text[:200]!r}")
            return Decision(
                should_extract=False,
                reason=f"parse_error({e})",
                via_gate1=via_gate1,
            )

        confidence = float(data.get("confidence", 0.0))
        should = bool(data.get("should_extract", False))
        raw_candidates = data.get("candidates", [])

        candidates = [
            ExtractionCandidate(
                type=c.get("type", "user"),
                title=c.get("title", ""),
                body=c.get("body", ""),
                source_quote=c.get("source_quote", ""),
                tags=[],
                score=confidence,
            )
            for c in raw_candidates
        ]

        if confidence < self.MIN_CONFIDENCE:
            return Decision(
                should_extract=False,
                reason=f"low_confidence({confidence:.2f})",
                confidence=confidence,
                via_gate1=via_gate1,
            )

        if not should or not candidates:
            return Decision(
                should_extract=False,
                reason=f"llm_says_no({data.get('reason', 'no_reason')})",
                confidence=confidence,
                via_gate1=via_gate1,
            )

        return Decision(
            should_extract=True,
            reason="extract",
            confidence=confidence,
            candidates=candidates,
            via_gate1=via_gate1,
        )

    def _call_llm(self, prompt: str) -> str:
        """调 LLM,收集 text_delta"""
        text = ""
        for chunk in self.llm_router.chat(
            messages=[
                {"role": "system", "content": EXTRACT_SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            cache_namespace=self.cache_namespace,
        ):
            if chunk.text_delta:
                text += chunk.text_delta.text
        return text