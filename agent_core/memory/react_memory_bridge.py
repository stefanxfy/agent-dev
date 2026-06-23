"""
ReactAgent ↔ DualChannelWriter 适配层
参考 docs/superpowers/specs/2026-06-22-react-memory-strict-design.md
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from enum import Enum
from typing import Iterator, Optional

from agent_core.memory.cost_tracker import BudgetExceeded
from agent_core.memory.dual_channel_writer import (
    DualChannelWriter,
    TurnMessage,
    ExtractionCandidate,
)
from agent_core.memory.extraction_gate import ExtractionGate, TurnContext
from agent_core.memory.latency import LatencyTimeout

logger = logging.getLogger("memory.react_bridge")


class MemoryEventKind(str, Enum):
    CHANNEL_A_OK = "channel_a_ok"
    GATE_SKIP = "gate_skip"
    GATE_PASS = "gate_pass"
    EXTRACT_DISPATCHED = "extract_dispatched"
    EXTRACT_DONE = "extract_done"
    EXTRACT_ERROR = "extract_error"
    SECRET_DETECTED = "secret_detected"  # M10 C1.2
    # M10 C6.5: 5 回退条件 banner 用的 enum 值(LOCK_BUSY / RATE_LIMITED
    # 当前无自然发射点,YAGNI 只占位;C6.2 / C6.3 有发射点)
    LOCK_BUSY = "lock_busy"
    RATE_LIMITED = "rate_limited"
    BUDGET_EXCEEDED = "budget_exceeded"  # M10 C6.2
    TIMEOUT = "timeout"  # M10 C6.3


@dataclass
class MemoryEvent:
    kind: MemoryEventKind
    turn_index: int
    reason: Optional[str] = None
    candidates_count: int = 0


class ReactMemoryBridge:
    """
    把 ReactAgent.run() 的同步 generator 风格
    翻译成 DualChannelWriter 的 async future 风格
    """

    def __init__(
        self,
        dual_channel: DualChannelWriter,
        gate: ExtractionGate,
        memory_store,                     # 给 recover_state 用
        session_id: str,
        max_workers: int = 2,
    ):
        self.dual_channel = dual_channel
        self.gate = gate
        self.memory_store = memory_store
        self.session_id = session_id

        # 会话级累计(每次 new bridge 都从 0 开始)
        self.cumulative_tokens = 0
        self.cumulative_tool_calls = 0

        # M10 C1.2: secret 事件 queue(executor 线程 → generator 线程)
        self._pending_secret_events: list[MemoryEvent] = []
        self._secret_queue_lock = threading.Lock()

        # A3 重启恢复
        self.gate1_period_start_turn = 0
        self.recover_state()

    def recover_state(self) -> None:
        """A3:从 extract_cursor 恢复 gate1_period_start_turn"""
        try:
            cursor = self.dual_channel.extract_cursor
            self.gate1_period_start_turn = max(0, cursor)
            logger.info(
                f"bridge 恢复: gate1_period_start_turn={self.gate1_period_start_turn}"
            )
        except Exception as e:
            logger.warning(f"recover_state 失败,默认 0: {e}")

    def _enqueue_secret_event(self, evt: MemoryEvent) -> None:
        """thread-safe 入队,供 DualChannelWriter 的 event_callback 调用"""
        with self._secret_queue_lock:
            self._pending_secret_events.append(evt)

    def _drain_secret_events(self) -> Iterator[MemoryEvent]:
        """drain queue 并 yield,generator 线程安全"""
        with self._secret_queue_lock:
            pending = self._pending_secret_events
            self._pending_secret_events = []
        for evt in pending:
            yield evt

    def on_turn_end(
        self,
        user_msg: str,
        assistant_resp: str,
        turn_index: int,
        input_tokens: int,
        output_tokens: int,
        tool_calls_in_turn: int,
        last_messages: list[dict],
        recent_turns: list[TurnMessage],
    ) -> Iterator[MemoryEvent]:
        # M10 C1.2: 先 drain 之前 turn 的 secret 事件
        yield from self._drain_secret_events()

        # 1. 累计 token / tool
        self.cumulative_tokens += input_tokens + output_tokens
        self.cumulative_tool_calls += tool_calls_in_turn

        # 2. 通道 A(同步,无 LLM)
        try:
            self.dual_channel.channel_a_inline_write(
                user_msg=user_msg,
                assistant_resp=assistant_resp,
                turn_index=turn_index,
            )
            yield MemoryEvent(
                kind=MemoryEventKind.CHANNEL_A_OK, turn_index=turn_index,
            )
        except Exception as e:
            logger.error(f"通道 A 写盘失败: {e}")
            yield MemoryEvent(
                kind=MemoryEventKind.EXTRACT_ERROR, turn_index=turn_index,
                reason=f"channel_a_error({e})",
            )
            return

        # 3. 门决策
        ctx = TurnContext(
            session_id=self.session_id,
            cumulative_tokens=self.cumulative_tokens,
            cumulative_tool_calls=self.cumulative_tool_calls,
            last_messages=last_messages,
            gate1_period_start_turn=self.gate1_period_start_turn,
        )
        # M10 C6.2 + C6.3: 预算/超时异常 → yield 对应 MemoryEvent,跳过本轮
        try:
            decision = self.gate.should_extract(ctx)
        except BudgetExceeded as e:
            yield MemoryEvent(
                kind=MemoryEventKind.BUDGET_EXCEEDED, turn_index=turn_index,
                reason=f"daily_budget_exceeded(${e.today_total:.4f}>${e.budget:.4f})",
            )
            return
        except LatencyTimeout as e:
            yield MemoryEvent(
                kind=MemoryEventKind.TIMEOUT, turn_index=turn_index,
                reason=f"latency_exceeded({e.timeout}s)",
            )
            return

        if not decision.should_extract:
            yield MemoryEvent(
                kind=MemoryEventKind.GATE_SKIP, turn_index=turn_index,
                reason=decision.reason,
            )
            return

        yield MemoryEvent(
            kind=MemoryEventKind.GATE_PASS, turn_index=turn_index,
            reason=decision.reason,
            candidates_count=len(decision.candidates),
        )

        # 4. ★ 门1 跑完清零(只在 LLM 评分过 0.6 时)
        if decision.via_gate1:
            self.cumulative_tokens = 0
            self.cumulative_tool_calls = 0
            self.gate1_period_start_turn = turn_index + 1
            logger.info(
                f"门1 跑完清零: gate1_period_start_turn={self.gate1_period_start_turn}"
            )

        # 5. 通道 B(异步)
        turn_msg = TurnMessage(
            turn_index=turn_index,
            user_msg=user_msg,
            assistant_resp=assistant_resp,
        )
        # 把已有 candidates 喂给 extractor(门3 已评过)
        candidates_snapshot = list(decision.candidates)

        def _extractor(_msgs: list[TurnMessage]) -> list[ExtractionCandidate]:
            return candidates_snapshot

        try:
            future = self.dual_channel.channel_b_background_extract(
                messages=[turn_msg],
                llm_extractor=_extractor,
            )
            yield MemoryEvent(
                kind=MemoryEventKind.EXTRACT_DISPATCHED, turn_index=turn_index,
                candidates_count=len(candidates_snapshot),
            )
        except Exception as e:
            logger.error(f"通道 B 提交失败: {e}")
            yield MemoryEvent(
                kind=MemoryEventKind.EXTRACT_ERROR, turn_index=turn_index,
                reason=f"channel_b_dispatch_error({e})",
            )

    def shutdown(self, timeout: float = 30.0) -> bool:
        return self.dual_channel.shutdown(timeout=timeout)