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
    ExtractionInProgressError,
)
from agent_core.memory.extraction_gate import ExtractionGate, TurnContext
from agent_core.memory.latency import LatencyTimeout

logger = logging.getLogger("memory.react_bridge")


class MemoryEventKind(str, Enum):
    TURN_PERSISTED = "turn_persisted"
    GATE_SKIP = "gate_skip"
    GATE_PASS = "gate_pass"
    EXTRACT_DISPATCHED = "extract_dispatched"
    EXTRACT_DONE = "extract_done"
    EXTRACT_ERROR = "extract_error"
    EXTRACT_DEFERRED = "extract_deferred"  # extract 忙,turn 入队等下次 flush
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


@dataclass
class _PendingExtract:
    """入队的待提取 turn + 闭包 extractor

    为什么 extractor 也入队:
    bridge 每次 on_turn_end 调 _extractor(_msgs)=candidates_snapshot,
    闭包绑定本轮的 decision.candidates。
    如果 turn 3 被 defer,等下次 _flush_pending 调度时,_extractor 仍要
    返回 turn 3 的 candidates(不是别的 turn)。
    """
    turn_msg: TurnMessage
    extractor: object  # Callable[[list[TurnMessage]], list[ExtractionCandidate]]
    candidates_count: int


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

        # 2026-06-24: extract 排队 queue(修复 turn 被 silently drop 的 race)
        # bridge.on_turn_end 调 extract_candidates 时,如果上一个 extract
        # 还没完(_extraction_in_progress=True)会抛 ExtractionInProgressError,
        # 旧代码 catch 后 yield EXTRACT_ERROR 静默结束,turn 永远进不了 .md / vec。
        # 新行为:catch 后入队,_on_extract_done 回调自动 flush。
        self._pending_extracts: list[_PendingExtract] = []
        self._pending_extracts_lock = threading.Lock()

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
    ) -> Iterator[MemoryEvent]:
        # M10 C1.2: 先 drain 之前 turn 的 secret 事件
        yield from self._drain_secret_events()

        # 1. 累计 token / tool
        self.cumulative_tokens += input_tokens + output_tokens
        self.cumulative_tool_calls += tool_calls_in_turn

        # 2. persist_turn(同步,无 LLM)
        # Bug 1 修复(2026-06-24):不传 turn_index,让 persist_turn 内部用 daily_cursor + 1
        # 避免 per-run turn 计数 vs session-global daily_cursor 冲突
        #
        # Bug 1b 修复(2026-06-24):接住 persist_turn 写回的 session-global cursor,
        # 作为 extract_candidates 的 turn_index(见下方 turn_msg)。
        # 原 Bug 1 修复只对齐了 persist_turn,extract_candidates 仍用 run-local turn_index,
        # 导致第一轮后 extract_candidates 窗口过滤 extract_cursor<=turn_index<=daily_cursor
        # 永远 miss(run-local turn_index 每轮重置回 1,extract_cursor 已推进过 1),
        # 记忆被静默丢弃。这里统一两通道编号。
        try:
            written_cursor = self.dual_channel.persist_turn(
                user_msg=user_msg,
                assistant_resp=assistant_resp,
                # turn_index 缺省 → persist_turn 内部用 self.daily_cursor + 1
            )
            logger.debug(
                f"turn {turn_index}: persist_turn 写盘成功 "
                f"(session cursor={written_cursor})"
            )
            yield MemoryEvent(
                kind=MemoryEventKind.TURN_PERSISTED, turn_index=turn_index,
            )
        except Exception as e:
            logger.error(f"persist_turn 写盘失败: {e}")
            yield MemoryEvent(
                kind=MemoryEventKind.EXTRACT_ERROR, turn_index=turn_index,
                reason=f"turn_persist_error({e})",
            )
            return

        # 3. 门决策
        # Bug 1d 修复(2026-06-24):gate 只看「本轮」的 user+assistant。
        # 历史窗口概念(原 caller 传 self.messages[-6:] 当 last_messages)已彻底移除:
        # on_turn_end 不再接收任何历史参数,提取上下文只由本轮一对就地构造。
        # 原行为让 LLM 一次看到最近3轮 → 把早先轮次里还没记的事一并重提,
        # 配合 since_turn 去重窗口 → 反复落盘重复记忆(如周杰伦)。现严格"只提取本轮"。
        current_turn_messages = [
            {"role": "user", "content": user_msg},
            {"role": "assistant", "content": assistant_resp},
        ]
        ctx = TurnContext(
            session_id=self.session_id,
            cumulative_tokens=self.cumulative_tokens,
            cumulative_tool_calls=self.cumulative_tool_calls,
            last_messages=current_turn_messages,
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
            logger.debug(f"turn {turn_index}: gate 跳过 — {decision.reason}")
            yield MemoryEvent(
                kind=MemoryEventKind.GATE_SKIP, turn_index=turn_index,
                reason=decision.reason,
            )
            return

        logger.info(
            f"turn {turn_index}: gate 通过, {len(decision.candidates)} candidates "
            f"— {decision.reason}"
        )
        yield MemoryEvent(
            kind=MemoryEventKind.GATE_PASS, turn_index=turn_index,
            reason=decision.reason,
            candidates_count=len(decision.candidates),
        )

        # 4. ★ 门1 跑完清零 token/工具预算窗口(只在 LLM 评分过 0.6 时)
        if decision.via_gate1:
            self.cumulative_tokens = 0
            self.cumulative_tool_calls = 0
            logger.info(f"门1 跑完清零 token/工具预算窗口 (turn={turn_index})")

        # 5. extract_candidates(异步)
        # Bug 1b:用 persist_turn 写回的 session-global cursor,而非 run-local turn_index,
        # 保证 extract_candidates 窗口过滤 extract_cursor <= turn_index <= daily_cursor 命中
        # (written_cursor == daily_cursor 刚写的,extract_cursor <= daily_cursor 恒成立)
        turn_msg = TurnMessage(
            turn_index=written_cursor,
            user_msg=user_msg,
            assistant_resp=assistant_resp,
        )
        # 把已有 candidates 喂给 extractor(门3 已评过)
        candidates_snapshot = list(decision.candidates)

        def _extractor(_msgs: list[TurnMessage]) -> list[ExtractionCandidate]:
            return candidates_snapshot

        try:
            future = self.dual_channel.extract_candidates(
                messages=[turn_msg],
                llm_extractor=_extractor,
            )
            # 绑定 done callback:in-flight extract 完成后自动 flush 队列
            # 用 add_done_callback 而不是 _on_extract_done(后者属于 dual_channel_writer 内部)
            future.add_done_callback(self._flush_pending_extracts)
            logger.info(
                f"turn {turn_index}: extract_candidates 已提交提取 "
                f"({len(candidates_snapshot)} candidates)"
            )
            yield MemoryEvent(
                kind=MemoryEventKind.EXTRACT_DISPATCHED, turn_index=turn_index,
                candidates_count=len(candidates_snapshot),
            )
        except ExtractionInProgressError as e:
            # 2026-06-24 修复:不静默 drop — 入队等下次 flush
            # 防止 fast successive turn 触发 race 时 memory 永久丢失
            pending = _PendingExtract(
                turn_msg=turn_msg,
                extractor=_extractor,
                candidates_count=len(candidates_snapshot),
            )
            with self._pending_extracts_lock:
                self._pending_extracts.append(pending)
            logger.info(
                f"extract 忙,turn={turn_index} 入队 "
                f"(queue size={len(self._pending_extracts)})"
            )
            yield MemoryEvent(
                kind=MemoryEventKind.EXTRACT_DEFERRED, turn_index=turn_index,
                reason=f"extract_busy({e})",
                candidates_count=len(candidates_snapshot),
            )
        except Exception as e:
            logger.error(f"extract_candidates 提交失败: {e}")
            yield MemoryEvent(
                kind=MemoryEventKind.EXTRACT_ERROR, turn_index=turn_index,
                reason=f"extract_dispatch_error({e})",
            )

    def shutdown(self, timeout: float = 30.0) -> bool:
        # 2026-06-24:shutdown 前如果队列里还有 defer 的 turn,
        # 等待 dual_channel_writer shutdown(会等所有 in-flight 完成)
        # in-flight 完成后,_on_extract_done 会触发 _flush_pending_extracts。
        # 不需要在这里手动 flush,让 callback 链自然结束。
        ok = self.dual_channel.shutdown(timeout=timeout)
        # 双保险:shutdown 超时后还有遗留,直接 log warning
        with self._pending_extracts_lock:
            leftover = len(self._pending_extracts)
        if leftover > 0:
            logger.warning(
                f"bridge shutdown 后仍有 {leftover} 个 pending extract 未处理"
            )
        return ok

    def _flush_pending_extracts(self, _completed_future) -> None:
        """executor 线程触发的 done callback

        触发时机:
        - 上一个 extract 正常完成(success 或 exception 都触发)
        - 由 future.add_done_callback 自动调用

        行为:从 _pending_extracts 队首取一个,调 extract_candidates 提交。
        如果还是被 in-flight 占了(竞态),放回队首等下次 callback。
        """
        while True:
            with self._pending_extracts_lock:
                if not self._pending_extracts:
                    return
                next_pe = self._pending_extracts[0]

            try:
                future = self.dual_channel.extract_candidates(
                    messages=[next_pe.turn_msg],
                    llm_extractor=next_pe.extractor,
                )
            except ExtractionInProgressError:
                # 还是被另一个 in-flight 占(罕见 — 同 callback 里多次 flush 才出现)
                # 等 100ms 重试,不递归避免忙等
                import time as _time
                _time.sleep(0.1)
                continue

            # 成功提交:从队列移除,绑定下一轮 callback(链式)
            with self._pending_extracts_lock:
                if self._pending_extracts and self._pending_extracts[0] is next_pe:
                    self._pending_extracts.pop(0)
                else:
                    # 已被别的 callback 取走,只 log(不重复提交)
                    logger.warning(
                        "flush_pending: 队首已被其他 callback 消费,跳过"
                    )
                    return
            future.add_done_callback(self._flush_pending_extracts)
            logger.info(
                f"flush_pending: 提交 deferred turn {next_pe.turn_msg.turn_index} "
                f"(剩余 queue={len(self._pending_extracts)})"
            )
            return  # 这次 callback 结束,等下一个 future 完成

    def pending_extracts_count(self) -> int:
        """诊断:队列里还有几个 deferred turn(给 UI banner / 测试用)"""
        with self._pending_extracts_lock:
            return len(self._pending_extracts)