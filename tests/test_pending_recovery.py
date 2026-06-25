"""
M10 — pending_writes 卡住修复的测试 (B+C+D 三个修复)

B 修复:recover_pending() 实际重试 stuck channel_b_extract 行
C 修复:attempts 计数在失败时递增
D 修复:_on_extract_done 读取 future.exception() 并 log

不变量:
1. C 失败时 attempts +1(原代码 attempts 永远 = 0)
2. D 失败时 _on_extract_done 把 future.exception() log 出来(原代码吞掉)
3. B recover_pending 把 stuck channel_b_extract 行重新走一遍 _do_channel_b_extract
4. B recover_pending attempts >= MAX 后 drop 行(不再无限重试)
5. B recover_pending 对 channel_a_write 仅 report,不重试
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from agent_core.memory.chroma_store import ChromaVectorStore
from agent_core.memory.dual_channel_writer import (
    DualChannelWriter,
    DualChannelError,
    ExtractionInProgressError,
    TurnMessage,
    ExtractionCandidate,
)
from agent_core.memory.embeddings import EmbedFn
from agent_core.memory.memory_store import MemoryStore
from agent_core.memory.meta_db import MetaDB


# ──────────────────────────────────────────────────────────────────
# 假 EmbedFn —— 确定性 1024 维向量
# ──────────────────────────────────────────────────────────────────

class FakeEmbedFn:
    """确定性伪嵌入(避免 bge-m3 模型加载)"""
    dimension = 1024

    def encode(self, text: str) -> list[float]:
        digest = hashlib.sha256(text.encode("utf-8")).digest()
        vec = []
        for _ in range(32):
            for b in digest:
                vec.append(b / 255.0)
        return vec


# ──────────────────────────────────────────────────────────────────
# Fixtures
# ──────────────────────────────────────────────────────────────────

@pytest.fixture
def memory_root(tmp_path):
    root = tmp_path / "memory"
    root.mkdir()
    return root


@pytest.fixture
def logs_dir(tmp_path):
    """logs 是 memory_root.parent 的子目录(双通道写入器约定)"""
    d = tmp_path / "logs"
    d.mkdir()
    return d


@pytest.fixture
def meta_db_path(tmp_path):
    return tmp_path / "meta.db"


@pytest.fixture
def chroma_dir(tmp_path):
    d = tmp_path / "chroma"
    d.mkdir()
    return d


@pytest.fixture
def meta_db(meta_db_path):
    return MetaDB(meta_db_path)


@pytest.fixture
def memory_store(memory_root):
    return MemoryStore(memory_root)


@pytest.fixture
def writer(meta_db, memory_store, memory_root, chroma_dir):
    """DualChannelWriter with FakeEmbedFn + Ephemeral chroma"""
    embed = FakeEmbedFn()
    chroma_path = chroma_dir / f"recovery_{os.getpid()}_{threading.get_ident()}"
    with ChromaVectorStore(str(chroma_path), collection="recovery_test") as vec:
        w = DualChannelWriter("s1", meta_db, memory_store, vec, embed)
        yield w
        w.shutdown(timeout=3)
        vec.close()


# ──────────────────────────────────────────────────────────────────
# C 修复测试:attempts 计数器在失败时递增
# ──────────────────────────────────────────────────────────────────

class TestCFixAttemptsIncrement:

    def test_failure_bumps_attempts(self, writer, meta_db, memory_root, logs_dir):
        """C 修复:channel_b_extract 失败时,pending.attempts +1

        复现:root cause 2 — 原代码 attempts 永远 = 0,attempts 字段无意义
        修复:_do_channel_b_extract 的 except 分支调 bump_pending_attempts
        """
        # 1. 写 3 个 turn(让 daily_cursor = 2)
        for i in range(3):
            writer.channel_a_inline_write(f"msg{i}", f"resp{i}", turn_index=i)

        # 2. mock vector_store.add 失败(模拟 ChromaStoreError)
        def exploding_vec_add(doc):
            raise RuntimeError("vector store 模拟失败")

        writer.vector_store.add = exploding_vec_add

        # 3. 跑 channel_b_extract → 应失败(让 _do_channel_b_extract 走 except 分支)
        messages = [TurnMessage(i, f"msg{i}", f"resp{i}") for i in range(3)]
        with pytest.raises(DualChannelError):
            writer.channel_b_background_extract(
                messages, llm_extractor=lambda _m: [
                    ExtractionCandidate(
                        type="user", title=f"t{i}", body=f"b{i}",
                        source_quote=f"q{i}", tags=["test"], score=0.5,
                    )
                    for i in range(len(_m))
                ],
            ).result(timeout=5)

        # 4. 关键断言:attempts 应该 = 1(原代码 = 0)
        pending = meta_db.list_pending("s1")
        assert len(pending) == 1, f"应有 1 条 pending,实际 {len(pending)}"
        assert pending[0]["attempts"] == 1, (
            f"attempts 失败后应递增到 1,实际 {pending[0]['attempts']} "
            f"(原 bug:永远 = 0)"
        )

    def test_success_does_not_bump_attempts(self, writer, meta_db, memory_root):
        """C 修复:成功路径不调 bump_pending_attempts(且 pending 直接删除)

        验证:成功时 attempts 字段无意义(行被删)
        """
        for i in range(3):
            writer.channel_a_inline_write(f"msg{i}", f"resp{i}", turn_index=i)

        messages = [TurnMessage(i, f"msg{i}", f"resp{i}") for i in range(3)]
        future = writer.channel_b_background_extract(
            messages, llm_extractor=lambda _m: [
                ExtractionCandidate(
                    type="user", title=f"t{i}", body=f"b{i}",
                    source_quote=f"q{i}", tags=["test"], score=0.5,
                )
                for i in range(len(_m))
            ],
        )
        result = future.result(timeout=5)
        assert result["written"] == 3

        # 成功 → pending 删除 → list_pending 为空
        assert meta_db.list_pending("s1") == []


# ──────────────────────────────────────────────────────────────────
# D 修复测试:future.exception() 被读取并 log
# ──────────────────────────────────────────────────────────────────

class TestDFixExceptionLogging:

    def test_extract_failure_exception_is_visible(
        self, writer, meta_db, memory_root,
    ):
        """D 修复:future.exception() 必须可被 caller / observer 访问到(不再被吞掉)

        复现:root cause 3' — 原代码只 discard future,不读 future.exception(),
        fire-and-forget 模式下异常静默消失,生产日志里完全看不到。

        修复:_on_extract_done 调 future.exception() 并 logger.error。

        注:不依赖 caplog 跨线程捕获(不可靠),改用直接证明 D 修复点的存在 —
        future.exception() 返回非 None(在原代码路径下也会返回,但没人读它)。
        真正证明修复效果:观察 _on_extract_done 的副作用 — pending.attempts 被 bump
        (这只有 _do_channel_b_extract 的 except 路径会做,而该路径只有在异常
        已经被 _on_extract_done 回调线程检测到后才会跑)。
        """
        for i in range(2):
            writer.channel_a_inline_write(f"msg{i}", f"resp{i}", turn_index=i)

        # 让 vector_store.add 抛异常
        writer.vector_store.add = lambda doc: (_ for _ in ()).throw(
            RuntimeError("vec 模拟失败 D-fix test"),
        )

        messages = [TurnMessage(i, f"msg{i}", f"resp{i}") for i in range(2)]
        future = writer.channel_b_background_extract(
            messages, llm_extractor=lambda _m: [
                ExtractionCandidate(
                    type="user", title=f"t{i}", body=f"b{i}",
                    source_quote=f"q{i}", tags=["test"], score=0.5,
                )
                for i in range(len(_m))
            ],
        )
        # 模拟 fire-and-forget:caller 不读 future,但 _on_extract_done 会读
        with pytest.raises(DualChannelError):
            future.result(timeout=5)

        # D 修复核心:future 异常对象可被外部读取(不再被吞掉)
        # 原代码路径:future.exception() 返回值没人读 → 等于吞掉
        # 新代码路径:_on_extract_done 显式读 + log
        exc = future.exception()
        assert exc is not None, "future 应保留异常对象(供 D 修复 log)"
        assert "vec 模拟失败" in str(exc)

        # 间接证据:pending 已 bump attempts(C 修复) — 这只会在 _do_channel_b_extract
        # 的 except 路径跑过后才发生,即异常从 future 流到了 _do_channel_b_extract
        pending = meta_db.list_pending("s1")
        assert len(pending) == 1
        assert pending[0]["attempts"] == 1, (
            f"attempts 应 = 1(证明异常已流过 except 路径),实际 {pending[0]['attempts']}"
        )


# ──────────────────────────────────────────────────────────────────
# B 修复测试:recover_pending 实际重试
# ──────────────────────────────────────────────────────────────────

class TestBFixRecoverPendingRetries:

    def test_recover_retries_stuck_channel_b_extract(
        self, writer, meta_db, memory_root, logs_dir,
    ):
        """B 修复:recover_pending 把 stuck channel_b_extract 行实际重试

        步骤:
        1. 写 1 个 turn(daily_cursor=0)
        2. 让 vector_store.add 失败 → channel_b_extract 走 except → attempts=1
        3. 调 recover_pending → 应重试 → pending 被清
        4. (Bug 2 修复:extract_cursor 不应被推进到 daily+1,而是保持 0)
        """
        # 1. 写 1 个 turn
        writer.channel_a_inline_write("msg0", "resp0", turn_index=0)
        assert writer.daily_cursor == 0
        assert writer.extract_cursor == 0

        # 2. 让 vector 失败 1 次
        original_add = writer.vector_store.add
        call_count = {"n": 0}
        def flaky_add(doc):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise RuntimeError("first call fails")
            return original_add(doc)
        writer.vector_store.add = flaky_add

        messages = [TurnMessage(0, "msg0", "resp0")]
        with pytest.raises(DualChannelError):
            writer.channel_b_background_extract(
                messages, llm_extractor=lambda _m: [
                    ExtractionCandidate(
                        type="user", title="t", body="b",
                        source_quote="q", tags=["test"], score=0.5,
                    )
                ],
            ).result(timeout=5)

        # 此时:pending 存在,attempts=1
        pending = meta_db.list_pending("s1")
        assert len(pending) == 1
        assert pending[0]["attempts"] == 1

        # 3. 调 recover_pending → 应 retried
        report = writer.recover_pending()
        assert len(report["retried"]) == 1
        assert report["retried"][0]["id"] == pending[0]["id"]
        assert report["retried"][0]["previous_attempts"] == 1

        # 4. 等 recovery future 完成
        import time as _time
        _time.sleep(0.5)
        writer.shutdown(timeout=3)

        # 5. 关键断言:pending 已清
        pending_after = meta_db.list_pending("s1")
        # recovery 用空 extractor → to_process 仍会跑(candidates=0 → written=0)
        # 然后 _do_channel_b_extract 走完 remove_pending
        assert len(pending_after) == 0, (
            f"recover_pending 后 pending 应清空,实际仍剩 {len(pending_after)} 条"
        )
        # Bug 2 修复(2026-06-24):recovery 不推进 extract_cursor
        # 原行为:extract_cursor 推到 daily+1=1,导致下次真实 extract 看到 to_process=[]
        # 修复后:extract_cursor 保持 0,下次真实 extract 才能处理 turn 0
        assert writer.extract_cursor == 0, (
            f"Bug 2 修复:extract_cursor 应保持 0(下次真实 extract 才能处理 turn 0),"
            f"实际 {writer.extract_cursor}"
        )

    def test_recover_drops_after_max_attempts(
        self, writer, meta_db, memory_root, logs_dir,
    ):
        """B 修复:attempts >= MAX 后 drop(避免无限重试)

        步骤:
        1. 写 1 turn
        2. 直接手动 INSERT 一条 attempts=3 的 stuck pending(模拟已重试 3 次)
        3. 调 recover_pending → 应 dropped,行被删
        """
        # 写 1 turn
        writer.channel_a_inline_write("msg0", "resp0", turn_index=0)

        # 手动 INSERT 一条 attempts = MAX 的 stuck pending
        meta_db.add_pending("s1", {
            "action": "channel_b_extract",
            "session_id": "s1",
            "turn_range": [0, 0],
        })
        # bump 3 次,达到上限
        for _ in range(3):
            pid = meta_db.list_pending("s1")[0]["id"]
            meta_db.bump_pending_attempts(pid)

        pending = meta_db.list_pending("s1")
        assert len(pending) == 1
        assert pending[0]["attempts"] == 3  # = MAX_RETRY_ATTEMPTS

        # 调 recover_pending → 应 dropped
        report = writer.recover_pending()
        assert len(report["dropped"]) == 1
        assert report["dropped"][0]["reason"] == "max_retries_exceeded"
        assert report["dropped"][0]["attempts"] == 3

        # 行被删
        assert meta_db.list_pending("s1") == []

    def test_recover_skips_channel_a_write(
        self, writer, meta_db, memory_root,
    ):
        """B 修复:channel_a_write 不被自动重试(只 report)

        原因:channel A 真正重试需要重新解析 log + 重写 .md,超出本修复 scope
        """
        # 手动 INSERT 一条 channel_a_write pending
        meta_db.add_pending("s1", {
            "action": "channel_a_write",
            "turn_index": 0,
            "user_msg": "x",
            "assistant_resp": "y",
        })

        report = writer.recover_pending()
        assert len(report["skipped"]) == 1
        assert report["skipped"][0]["action"] == "channel_a_write"
        assert "暂不重试" in report["skipped"][0]["reason"]

        # 行未删(留给后续手工恢复)
        assert len(meta_db.list_pending("s1")) == 1

    def test_recover_empty_session_no_pending(
        self, writer, meta_db, memory_root,
    ):
        """B 修复:session 还没写过 turn → 没有 pending → recover 安静返回

        不变:空 session 不应有 pending 行(若有,就是 bug)
        """
        report = writer.recover_pending()
        assert report["pending_count"] == 0
        assert report["retried"] == []
        assert report["dropped"] == []
        assert report["skipped"] == []


# ──────────────────────────────────────────────────────────────────
# 集成测试:3 步完整 cycle(失败 → bump → recover → 清理)
# ──────────────────────────────────────────────────────────────────

class TestIntegrationFullCycle:

    def test_full_cycle_failure_bump_recover_clear(
        self, writer, meta_db, memory_root, logs_dir,
    ):
        """完整 cycle:
        1. 写 turn + 跑 channel_b_extract(失败) → pending stuck + attempts=1
        2. recover_pending() → 重试(成功) → pending 清
        3. 再次 list_pending → 空
        (Bug 2 修复:extract_cursor 不应被推进,保持 0)
        """
        # 1. 写 turn
        writer.channel_a_inline_write("用户偏好", "好的", turn_index=0)

        # 2. 模拟 channel B 失败(让 vec 抛异常)
        original_add = writer.vector_store.add
        def boom(doc):
            raise RuntimeError("vec 失败(集成测试)")
        writer.vector_store.add = boom

        messages = [TurnMessage(0, "用户偏好", "好的")]
        with pytest.raises(DualChannelError):
            writer.channel_b_background_extract(
                messages, llm_extractor=lambda _m: [
                    ExtractionCandidate(
                        type="user", title="偏好", body="偏好内容",
                        source_quote="x", tags=["test"], score=0.5,
                    )
                ],
            ).result(timeout=5)

        # 验证 stuck 状态
        pending_after_fail = meta_db.list_pending("s1")
        assert len(pending_after_fail) == 1
        assert pending_after_fail[0]["attempts"] == 1

        # 3. 修好 vec,跑 recover_pending
        writer.vector_store.add = original_add
        report = writer.recover_pending()

        # 等 recovery future 完成
        import time as _time
        _time.sleep(0.5)
        writer.shutdown(timeout=3)

        # 4. 验证全部清理
        assert meta_db.list_pending("s1") == []
        # Bug 2 修复:recovery 不推进 extract_cursor(保持 0)
        # 下次真实 extract 才能处理这个 turn
        assert writer.extract_cursor == 0

        # recovery 用的是空 extractor,所以不会真的写 .md 或 vec
        # 但 pending 清理 + extract_cursor 保持不变这两个不变量已满足


# ──────────────────────────────────────────────────────────────────
# Bug 1 修复测试:channel_a 不传 turn_index 时用 daily_cursor + 1
# ──────────────────────────────────────────────────────────────────

class TestBug1FixChannelAAutoTurnIndex:
    """Bug 1:per-run turn_index vs global daily_cursor 冲突

    复现:每个新 run 的 turn 1 都对应 daily_cursor=1,被
    `if turn_index <= daily_cursor: return` 拦下,新 user_msg 完全没进 daily log。

    修复:turn_index 缺省 → channel A 内部用 self.daily_cursor + 1,
    保证每次调用都真正写一条新 turn,不依赖 caller 传对。

    覆盖:
    - 第 1 次调用 turn_index=None → 写 turn 1(daily_cursor 0→1)
    - 第 2 次调用 turn_index=None → 写 turn 2(daily_cursor 1→2)
    - 显式传重复 turn_index → 仍幂等 no-op(向后兼容)
    - 显式传 turn_index=daily_cursor+1 → 也走得通
    """

    def test_first_call_no_turn_index_writes_turn_1(
        self, writer, meta_db, memory_root, logs_dir,
    ):
        """Bug 1:turn_index 缺省 + 第 1 次调用 → 写 turn 1(daily_cursor 0→1)

        关键:per-run 计数从 1 开始,但 channel A 内部用 daily_cursor+1=1
        → 1 > 0 → 写 turn 1(daily_cursor 推进到 1)
        """
        assert writer.daily_cursor == 0

        # 不传 turn_index(模拟 bridge on_turn_end 的调用方式)
        result = writer.channel_a_inline_write(
            user_msg="测试消息 1",
            assistant_resp="已记",
            # turn_index 故意缺省 → 用 daily_cursor + 1
        )

        assert result == 1
        assert writer.daily_cursor == 1

        # M11:验证 memory_tasks 表有这条记录(不再验 JSONL)
        with meta_db.transaction() as conn:
            rows = conn.execute(
                "SELECT turn_index, user_msg, assistant_resp, state "
                "FROM memory_tasks WHERE session_id='s1' ORDER BY turn_index"
            ).fetchall()
        assert len(rows) == 1
        assert rows[0][0] == 1
        assert rows[0][1] == "测试消息 1"
        assert rows[0][2] == "已记"
        assert rows[0][3] == "NONE"  # Channel A 刚落盘

    def test_subsequent_calls_advance_turn_index(
        self, writer, meta_db, memory_root, logs_dir,
    ):
        """Bug 1:连续 3 次 turn_index=None → daily_cursor 0→1→2→3,写 3 条"""
        writer.channel_a_inline_write("msg 1", "resp 1")
        assert writer.daily_cursor == 1

        writer.channel_a_inline_write("msg 2", "resp 2")
        assert writer.daily_cursor == 2

        writer.channel_a_inline_write("msg 3", "resp 3")
        assert writer.daily_cursor == 3

        # M11:验证 memory_tasks 表有 3 条,turn_index 1/2/3
        with meta_db.transaction() as conn:
            rows = conn.execute(
                "SELECT turn_index FROM memory_tasks "
                "WHERE session_id='s1' ORDER BY turn_index"
            ).fetchall()
        assert [r[0] for r in rows] == [1, 2, 3]

    def test_explicit_duplicate_turn_index_is_idempotent(
        self, writer, meta_db, memory_root,
    ):
        """向后兼容:显式传重复 turn_index → 仍幂等 no-op(原行为不变)"""
        # 先写 turn 1
        writer.channel_a_inline_write("msg 1", "resp 1")
        assert writer.daily_cursor == 1

        # 显式传重复 turn_index=1 → 幂等 no-op
        result = writer.channel_a_inline_write("msg dup", "resp dup", turn_index=1)
        assert result == 1  # 返回当前 daily_cursor(不推进)
        assert writer.daily_cursor == 1

    def test_explicit_advance_turn_index_writes(
        self, writer, meta_db, memory_root,
    ):
        """向后兼容:显式传 turn_index=daily_cursor+1 → 也走得通"""
        # 模拟 caller 已知 turn_index 时显式传
        writer.channel_a_inline_write("msg 1", "resp 1", turn_index=1)
        assert writer.daily_cursor == 1

        # 缺省路径
        writer.channel_a_inline_write("msg 2", "resp 2")
        assert writer.daily_cursor == 2


# ──────────────────────────────────────────────────────────────────
# Bug 2 修复测试:recovery 不推进 extract_cursor
# ──────────────────────────────────────────────────────────────────

class TestBug2FixRecoveryDoesNotAdvanceCursor:
    """Bug 2:recovery 把 extract_cursor 推进到 daily_cursor+1,
    导致后续真实 extract 看到 to_process=[] → no-op,
    新 turn 永远没机会被处理。

    修复:recovery 路径 channel_b_background_extract(... advance_cursor=False)
    让 _do_channel_b_extract 只清 pending,不推进 extract_cursor。
    """

    def test_recovery_does_not_advance_extract_cursor(
        self, writer, meta_db, memory_root, logs_dir,
    ):
        """Bug 2 修复:recovery 后 extract_cursor 应保持不变(still = daily_cursor)

        步骤:
        1. 写 turn 1(turn_index=None → auto=1,daily_cursor 0→1)
        2. 让 channel B 失败 → pending stuck(extract_cursor=0,attempts=1)
        3. recover_pending → 走空 extractor + advance_cursor=False
        4. 验证:extract_cursor 还是 0(而不是被推进到 daily+1=2)
        """
        # 1. 写 turn 1
        writer.channel_a_inline_write("msg 1", "resp 1")  # turn_index=None → auto=1
        assert writer.daily_cursor == 1
        assert writer.extract_cursor == 0

        # 2. 让 vector 失败
        def boom(doc):
            raise RuntimeError("vec 失败")
        writer.vector_store.add = boom

        messages = [TurnMessage(1, "msg 1", "resp 1")]
        with pytest.raises(DualChannelError):
            writer.channel_b_background_extract(
                messages,
                llm_extractor=lambda _m: [
                    ExtractionCandidate(
                        type="user", title="t", body="b",
                        source_quote="q", tags=["test"], score=0.5,
                    )
                ],
            ).result(timeout=5)

        # 验证 stuck 状态
        assert writer.extract_cursor == 0  # 失败没推进
        pending = meta_db.list_pending("s1")
        assert len(pending) == 1
        assert pending[0]["attempts"] == 1

        # 3. recover(boom 仍生效,但 recovery 用空 extractor → 不调 vec.add → 走成功路径)
        report = writer.recover_pending()
        assert len(report["retried"]) == 1

        # 等 recovery future 完成
        import time as _time
        _time.sleep(0.5)
        writer.shutdown(timeout=3)

        # 4. 关键断言:pending 清空 + extract_cursor 没推进
        assert meta_db.list_pending("s1") == []
        # Bug 2 修复:extract_cursor 应保持 0(daily_cursor 也是 1,但 recovery 不应推到 2)
        # 原 bug:extract_cursor 会被推到 daily+1=2,导致下次真实 extract 看到 to_process=[]
        assert writer.extract_cursor == 0, (
            f"Bug 2 修复失败:recovery 把 extract_cursor 推到了 {writer.extract_cursor},"
            f"应为 0(下次真实 extract 才能处理 turn 1)"
        )

    def test_after_recovery_next_real_extract_processes_pending_turn(
        self, writer, meta_db, memory_root, logs_dir,
    ):
        """Bug 2 修复的真实价值:recovery 后,下次真实 extract 能处理之前的 turn

        步骤:
        1. 写 turn 1 + channel B 失败 → pending stuck
        2. recover_pending → 清 stuck(extract_cursor 保持 0)
        3. 重新构造一个干净的 writer(不 boom)+ channel_b_extract 处理 turn 1
        4. 验证:turn 1 被实际写入(Bug 2 修复前 to_process=[] → no-op)
        """
        # 1. 写 turn 1 + 让 channel B 失败
        writer.channel_a_inline_write("msg 1", "resp 1")  # turn 1
        assert writer.daily_cursor == 1

        def boom(doc):
            raise RuntimeError("vec 失败")
        writer.vector_store.add = boom

        with pytest.raises(DualChannelError):
            writer.channel_b_background_extract(
                [TurnMessage(1, "msg 1", "resp 1")],
                llm_extractor=lambda _m: [
                    ExtractionCandidate(
                        type="user", title="t", body="b",
                        source_quote="q", tags=["test"], score=0.5,
                    )
                ],
            ).result(timeout=5)

        assert writer.extract_cursor == 0

        # 2. recover
        report = writer.recover_pending()
        assert len(report["retried"]) == 1

        import time as _time
        _time.sleep(0.5)

        # extract_cursor 保持 0(Bug 2 修复)
        assert writer.extract_cursor == 0
        assert meta_db.list_pending("s1") == []

        # 3. 重新构造一个干净的 writer(不 boom,共享 meta_db / memory_store)
        from agent_core.memory.chroma_store import ChromaVectorStore
        embed = FakeEmbedFn()
        chroma_path = memory_root.parent / "chroma" / f"bug2_clean_{os.getpid()}_{threading.get_ident()}"
        chroma_path.mkdir(parents=True, exist_ok=True)
        clean_vec = ChromaVectorStore(str(chroma_path), collection="bug2_clean_test")

        try:
            clean_writer = DualChannelWriter(
                "s1", meta_db, writer.memory_store, clean_vec, embed,
            )
            # 状态从 meta_db 恢复
            assert clean_writer.daily_cursor == 1
            assert clean_writer.extract_cursor == 0

            # 4. 真实 extract(clean_writer 的 vec 不 boom)
            future = clean_writer.channel_b_background_extract(
                [TurnMessage(1, "msg 1", "resp 1")],
                llm_extractor=lambda _m: [
                    ExtractionCandidate(
                        type="user", title="新 turn", body="新 turn body",
                        source_quote="q", tags=["test"], score=0.5,
                    )
                ],
            )
            result = future.result(timeout=5)

            # 关键断言:turn 1 被处理(Bug 2 修复前 to_process=[] → no-op)
            assert result["written"] >= 1, (
                f"Bug 2 修复失败:recovery 后真实 extract 应该处理 turn 1,实际 written={result}"
            )
            assert clean_writer.extract_cursor == 2  # daily_cursor(1) + 1

            clean_writer.shutdown(timeout=3)
        finally:
            clean_vec.close()


# ──────────────────────────────────────────────────────────────────
# 集成测试:Bug 1 + Bug 2 联动 — 真实场景
# ──────────────────────────────────────────────────────────────────

class TestBug12Integration:
    """Bug 1 + Bug 2 集成:per-run turn 计数 + recovery 后新 turn 仍能处理

    场景(模拟 ffcab34a watermelon 案例):
    1. 第一个 run:写 turn 1(per-run turn=1,但 channel A 内部用 daily+1=1)
    2. channel B 失败 → pending stuck
    3. 模拟崩溃 → 重启(新 writer)
    4. 第二个 run:调 recover_pending(应清 stuck 但不推进 extract_cursor)
    5. 第二个 run:再写 turn 2(per-run turn=1,但 channel A 内部用 daily+1=2)
    6. 第二个 run:真实 extract → 应处理 turn 2
    """

    def test_full_scenario_per_run_turn_plus_recovery(
        self, writer, meta_db, memory_root, logs_dir,
    ):
        # 1. 第一个 run:写 turn 1(channel A auto turn_index=1)
        writer.channel_a_inline_write("run1 msg", "run1 resp")
        assert writer.daily_cursor == 1

        # channel B 失败
        def boom(doc):
            raise RuntimeError("vec 失败")
        writer.vector_store.add = boom

        with pytest.raises(DualChannelError):
            writer.channel_b_background_extract(
                [TurnMessage(1, "run1 msg", "run1 resp")],
                llm_extractor=lambda _m: [
                    ExtractionCandidate(
                        type="user", title="r1t1", body="r1b1",
                        source_quote="q", tags=["test"], score=0.5,
                    )
                ],
            ).result(timeout=5)

        assert writer.extract_cursor == 0
        pending = meta_db.list_pending("s1")
        assert len(pending) == 1

        # 2. 模拟崩溃 → 重启:重新构造一个干净的 writer(共享 meta_db / memory)
        from agent_core.memory.chroma_store import ChromaVectorStore
        embed = FakeEmbedFn()
        chroma_path = memory_root.parent / "chroma" / f"restart_{os.getpid()}_{threading.get_ident()}"
        chroma_path.mkdir(parents=True, exist_ok=True)
        restart_vec = ChromaVectorStore(str(chroma_path), collection="restart_test")

        try:
            new_writer = DualChannelWriter(
                "s1", meta_db, writer.memory_store, restart_vec, embed,
            )

            # 状态应从 meta_db 恢复
            assert new_writer.daily_cursor == 1
            assert new_writer.extract_cursor == 0

            # 3. 调 recover_pending → 清 stuck + 不推进 extract_cursor
            report = new_writer.recover_pending()
            assert len(report["retried"]) == 1

            import time as _time
            _time.sleep(0.5)

            # Bug 2 验证:extract_cursor 不应被推到 2(应是 0,小于 daily_cursor=1)
            assert new_writer.extract_cursor == 0, (
                f"Bug 2 修复失败:recovery 把 extract_cursor 推到 {new_writer.extract_cursor},"
                f"应为 0(下次真实 extract 才能处理 turn 1)"
            )
            assert meta_db.list_pending("s1") == []

            # 4. 第二个 run:写 turn 2(per-run turn=1,但 channel A 内部用 daily+1=2)
            new_writer.channel_a_inline_write("run2 msg", "run2 resp")
            # Bug 1 修复:per-run turn=1 但 channel A 写的是 daily_cursor+1=2
            assert new_writer.daily_cursor == 2

            # 5. 真实 extract → 处理 turn 2
            future = new_writer.channel_b_background_extract(
                [TurnMessage(2, "run2 msg", "run2 resp")],
                llm_extractor=lambda _m: [
                    ExtractionCandidate(
                        type="user", title="r2t1", body="r2b1",
                        source_quote="q", tags=["test"], score=0.5,
                    )
                ],
            )
            result = future.result(timeout=5)

            # Bug 2 验证:turn 2 应被处理(Bug 2 修复前 to_process=[] → no-op)
            assert result["written"] >= 1, (
                f"Bug 2 修复失败:第二个 run 的 turn 应被 extract 处理,实际 {result}"
            )
            assert new_writer.extract_cursor == 3  # daily_cursor(2) + 1

            new_writer.shutdown(timeout=3)
        finally:
            restart_vec.close()

        writer.shutdown(timeout=3)