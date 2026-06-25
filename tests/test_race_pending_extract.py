"""
2026-06-24 修复回归测试 — channel_b race condition 导致 turn 被 silently drop

复现场景:
1. turn 1 → bridge.on_turn_end → channel_b 提交(异步,in-flight)
2. turn 2 在 turn 1 extract 完成前到达 → channel_b 抛 ExtractionInProgressError
3. 旧代码:catch 后 yield EXTRACT_ERROR,turn 2 永久丢失(.md 不写)
4. 新代码:catch 后入队 → _on_extract_done callback 自动 flush

不变量:
A. turn 1 extract 进行中时,turn 2 触发 → yield EXTRACT_DEFERRED(不是 EXTRACT_ERROR)
B. 队列里 deferred turn 会被 _flush_pending_extracts 自动提交
C. 所有 turn 最终都写入 .md / vec(queue_size=0 at end)
D. candidates 闭包正确绑定到对应 turn(不串号)

测试技巧:fake embed + stub gate 跑得太快,race 测不出来。
_submit_slow_extract 提交一个真实的 channel_b extract,extractor 内 sleep N 秒,
让 _extraction_in_progress 真实保持 True 足够久,后续 turn 才会真触发
ExtractionInProgressError。slow extract 完成时其 done callback 自动 flush 队列
(生产里这个 callback 由 bridge 在 EXTRACT_DISPATCHED 路径绑定;测试里 helper
显式绑定 bridge._flush_pending_extracts 模拟同样的链)。
"""
from __future__ import annotations

import hashlib
import os
import threading
import time
from pathlib import Path

import pytest

from agent_core.memory.chroma_store import ChromaVectorStore
from agent_core.memory.dual_channel_writer import (
    DualChannelWriter, TurnMessage, ExtractionCandidate,
    ExtractionInProgressError,
)
from agent_core.memory.extraction_gate import ExtractionGate, TurnContext, Decision
from agent_core.memory.memory_store import MemoryStore
from agent_core.memory.meta_db import MetaDB
from agent_core.memory.react_memory_bridge import (
    ReactMemoryBridge, MemoryEventKind, _PendingExtract,
)


class FakeEmbedFn:
    dimension = 1024

    def encode(self, text: str) -> list[float]:
        digest = hashlib.sha256(text.encode("utf-8")).digest()
        vec = []
        for _ in range(32):
            for b in digest:
                vec.append(b / 255.0)
        return vec


class _StubGate:
    """固定 should_extract=True,返回指定 candidates(免 LLM 调用)"""
    def __init__(self, candidates_by_turn: dict[str, list[ExtractionCandidate]]):
        self.candidates_by_turn = candidates_by_turn

    def should_extract(self, ctx: TurnContext):
        turn_text = ctx.last_messages[0].get("content", "")
        cands = []
        for key, lst in self.candidates_by_turn.items():
            if key in turn_text:
                cands = lst
                break
        return Decision(
            should_extract=bool(cands),
            reason="stub",
            candidates=cands,
            via_gate1=False,
        )


class _SlowExtractor:
    """wrapper:bridge 传给 channel_b 的 extractor。
    模拟 LLM 慢调用 — 在返回 candidates 前 sleep N 秒,让 _extraction_in_progress
    真实地保持 True 足够长,后续 turn 提交时真触发 ExtractionInProgressError。
    """
    def __init__(self, candidates, sleep_seconds: float = 0.3):
        self.candidates = candidates
        self.sleep_seconds = sleep_seconds

    def __call__(self, _msgs):
        time.sleep(self.sleep_seconds)
        return self.candidates


def _force_in_progress(writer):
    """把 writer 标记成 in-flight(模拟 extract 还在跑)

    真实情况:extractor 是慢 LLM 调用,channel_b 提交后 _extraction_in_progress
    保持 True 直到 _on_extract_done。FakeEmbedFn + stub gate 让这个周期短到
    测不出来,所以手动设置 flag 来模拟真实竞态。
    """
    with writer._extraction_in_progress_lock:
        writer._extraction_in_progress = True
        writer._extraction_started_at = time.time()


@pytest.fixture
def workdir(tmp_path):
    return {
        "meta": tmp_path / "meta.db",
        "memory": tmp_path / "memory",
        "chroma": tmp_path / "chroma",
    }


def _wait_until(predicate, timeout=5.0, interval=0.05):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


def _setup_bridge(workdir, candidates_map):
    """helper:建临时 writer + bridge,返回 (writer, bridge, chroma_path)"""
    embed = FakeEmbedFn()
    meta_db = MetaDB(workdir["meta"])
    memory_store = MemoryStore(workdir["memory"])
    chroma_path = (
        workdir["chroma"]
        / f"race_{os.getpid()}_{threading.get_ident()}_{time.time_ns()}"
    )
    vec = ChromaVectorStore(str(chroma_path), collection="race_test")
    writer = DualChannelWriter(
        f"race_{os.getpid()}_{threading.get_ident()}_{time.time_ns()}",
        meta_db, memory_store, vec, embed,
    )
    gate = _StubGate(candidates_map)
    bridge = ReactMemoryBridge(writer, gate, memory_store, writer.session_id)
    return writer, bridge, vec


def _submit_slow_extract(writer, bridge=None, sleep_seconds=0.5):
    """helper:提交一个会 sleep 的 channel_b extract,模拟慢 LLM。
    让 _extraction_in_progress 真实地保持 True 足够久,
    后续 turn 提交时能触发 ExtractionInProgressError。

    实现要点:
    1. 先 channel_a_inline_write 把 daily_cursor 推到 ≥ 1,否则
       _do_channel_b_extract 会因 to_process=[] 早退,sleep 还没跑就 done。
    2. 慢 extractor sleep N 秒后返回空 candidates(无副作用,只拖时间)。
    3. 如果传了 bridge,自动绑定 _flush_pending_extracts callback —
       模拟生产里"turn N+1 来时 bridge 给自己 future 绑的 callback 链"。
       真实生产中,bridge 永远只在 EXTRACT_DISPATCHED 路径绑 callback,
       queue 依赖下一个新 turn 触发 flush。这是设计缺陷,但测试模拟此行为。
    """
    # 1. 推进 daily_cursor — 否则 channel_b 早退
    writer.channel_a_inline_write(user_msg="warmup", assistant_resp="warmup")
    # 2. 用 1 作为 turn_index(肯定 ≤ daily_cursor)
    def slow_extractor(_msgs):
        time.sleep(sleep_seconds)
        return []

    future = writer.channel_b_background_extract(
        messages=[TurnMessage(turn_index=1, user_msg="slow", assistant_resp="slow")],
        llm_extractor=slow_extractor,
        advance_cursor=False,  # ★ 不动 extract_cursor,避免污染后续
    )
    # 3. 模拟生产里 bridge 自己会绑的 callback
    if bridge is not None:
        future.add_done_callback(bridge._flush_pending_extracts)
    return future


class TestRaceConditionFix:
    """2026-06-24 修复:channel_b race condition 不再 silently drop turn"""

    def test_extraction_in_progress_defers_to_queue(self, workdir):
        """A. channel_b 忙时,turn 触发 → yield EXTRACT_DEFERRED(不是 EXTRACT_ERROR)"""
        cand_t2 = ExtractionCandidate("user", "T2 title", "T2 body", "T2", [], 0.9)
        writer, bridge, vec = _setup_bridge(workdir, {
            "T2msg": [cand_t2],
        })
        try:
            # 1. 提交一个会 sleep 1s 的慢 extract → _extraction_in_progress=True
            slow_future = _submit_slow_extract(writer, bridge, sleep_seconds=1.0)

            # 2. bridge.on_turn_end → channel_b 立即抛 ExtractionInProgressError
            events = list(bridge.on_turn_end(
                user_msg="T2msg 用户消息", assistant_resp="T2 resp",
                turn_index=2, input_tokens=100, output_tokens=50,
                tool_calls_in_turn=0,
            ))
            kinds = [e.kind for e in events]

            # 关键断言:yield EXTRACT_DEFERRED,不是 EXTRACT_ERROR
            assert MemoryEventKind.EXTRACT_DEFERRED in kinds, (
                f"应 yield EXTRACT_DEFERRED,实际 {kinds}"
            )
            assert MemoryEventKind.EXTRACT_ERROR not in kinds, (
                f"不应 yield EXTRACT_ERROR(race 修复目标): {kinds}"
            )

            # 队列里应有 1 个 deferred turn
            assert bridge.pending_extracts_count() == 1, (
                f"deferred turn 应入队,实际 queue={bridge.pending_extracts_count()}"
            )

            # 让 slow extract 完成(callback 链 flush 队列)
            slow_future.result(timeout=5)
            # 等 flush
            ok = _wait_until(
                lambda: bridge.pending_extracts_count() == 0,
                timeout=5,
            )
            assert ok, "slow extract 完后应自动 flush"
        finally:
            writer.shutdown(timeout=10)
            vec.close()

    def test_deferred_turn_auto_flushed_after_extract_done(self, workdir):
        """B+C. deferred turn 在 in-flight 完成后自动 flush,queue 清空,
        且 .md 都写出来了"""
        cand_t2 = ExtractionCandidate("user", "T2 title", "T2 body", "T2", [], 0.9)
        writer, bridge, vec = _setup_bridge(workdir, {
            "T2msg": [cand_t2],
        })
        try:
            # 慢 extract 让 in-flight 持续 ~1s(helper 内部已 warmup channel_a)
            slow_future = _submit_slow_extract(writer, bridge, sleep_seconds=1.0)

            # turn 2 触发 defer(on_turn_end 内部 channel_a 会推进 daily_cursor)
            events_2 = list(bridge.on_turn_end(
                user_msg="T2msg", assistant_resp="r",
                turn_index=2, input_tokens=100, output_tokens=50,
                tool_calls_in_turn=0,
            ))
            assert any(
                e.kind == MemoryEventKind.EXTRACT_DEFERRED for e in events_2
            ), f"turn 2 应被 defer,实际 {events_2}"
            assert bridge.pending_extracts_count() == 1

            # 等 slow extract 完成 → 触发 callback 链 → flush
            slow_future.result(timeout=5)
            ok = _wait_until(
                lambda: bridge.pending_extracts_count() == 0,
                timeout=10,
            )
            assert ok, (
                f"deferred turn 应在 10s 内 auto-flush,"
                f"实际 queue={bridge.pending_extracts_count()}"
            )

            writer.shutdown(timeout=10)

            # 关键断言:turn 2 写入了 .md
            md_files = list((workdir["memory"] / "user").glob("*.md"))
            titles = []
            for p in md_files:
                if p.suffix == ".bak":
                    continue
                content = p.read_text(encoding="utf-8")
                if "T2 title" in content:
                    titles.append("T2")
            assert "T2" in titles, (
                f"deferred turn 2 应写入 .md,实际 titles={titles}"
            )
        finally:
            writer.shutdown(timeout=10)
            vec.close()

    def test_extractor_closure_binds_correct_turn(self, workdir):
        """D. deferred turn 的 extractor 闭包仍返回自己 turn 的 candidates

        防止修复引入"队列里的 turn 用了别 turn 的 candidates"bug。
        """
        t2_cand = ExtractionCandidate("user", "TUNIQUE2", "T2 body", "T2", [], 0.9)
        t3_cand = ExtractionCandidate("user", "TUNIQUE3", "T3 body", "T3", [], 0.9)
        writer, bridge, vec = _setup_bridge(workdir, {
            "T2msg": [t2_cand], "T3msg": [t3_cand],
        })
        try:
            # 慢 extract → 真实 in-flight(helper 内部已 warmup channel_a)
            slow_future = _submit_slow_extract(writer, bridge, sleep_seconds=1.5)

            # turn 2 defer(on_turn_end 内部 channel_a 推进 daily_cursor)
            list(bridge.on_turn_end(
                user_msg="T2msg", assistant_resp="r",
                turn_index=2, input_tokens=100, output_tokens=50,
                tool_calls_in_turn=0,
            ))
            assert bridge.pending_extracts_count() == 1

            # turn 3 defer(还在 slow extract 跑)
            list(bridge.on_turn_end(
                user_msg="T3msg", assistant_resp="r",
                turn_index=3, input_tokens=100, output_tokens=50,
                tool_calls_in_turn=0,
            ))
            assert bridge.pending_extracts_count() == 2

            # 等 slow extract 完成 → flush 链:
            # slow done → flush turn 2 → turn 2 done → flush turn 3 → turn 3 done
            slow_future.result(timeout=5)
            ok = _wait_until(
                lambda: bridge.pending_extracts_count() == 0,
                timeout=10,
            )
            assert ok, (
                f"链式 flush 后 queue 应清空,实际={bridge.pending_extracts_count()}"
            )

            writer.shutdown(timeout=10)

            # 两个 turn 都写入,且 title 不串号
            md_files = list((workdir["memory"] / "user").glob("*.md"))
            titles_found = set()
            for p in md_files:
                if p.suffix == ".bak":
                    continue
                content = p.read_text(encoding="utf-8")
                for marker in ("TUNIQUE2", "TUNIQUE3"):
                    if marker in content:
                        titles_found.add(marker)
            assert titles_found == {"TUNIQUE2", "TUNIQUE3"}, (
                f"每个 deferred turn 的 candidates 应绑定到正确 turn,"
                f"实际 {titles_found}"
            )
        finally:
            writer.shutdown(timeout=10)
            vec.close()

    def test_pending_extracts_count_helper(self, workdir):
        """诊断 API:pending_extracts_count() 返回队列长度"""
        cand_t2 = ExtractionCandidate("user", "T2 title", "T2 body", "T2", [], 0.9)
        writer, bridge, vec = _setup_bridge(workdir, {
            "T2msg": [cand_t2],
        })
        try:
            assert bridge.pending_extracts_count() == 0  # 初始为空

            # 慢 extract in-flight
            slow_future = _submit_slow_extract(writer, bridge, sleep_seconds=1.0)

            list(bridge.on_turn_end(
                user_msg="T2msg", assistant_resp="r",
                turn_index=2, input_tokens=100, output_tokens=50,
                tool_calls_in_turn=0,
            ))
            # turn 2 deferred
            assert bridge.pending_extracts_count() == 1

            slow_future.result(timeout=5)
            writer.shutdown(timeout=10)
        finally:
            writer.shutdown(timeout=10)
            vec.close()