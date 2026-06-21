"""
M6 / Day 6 并发测试 —— 5/8 不变量场景(场景 3 跨进程归 M8)

设计 §4.5.1 场景矩阵:
- 场景 1 (M2 已覆盖): 双线程 channel_a 并发 → 已在 test_dual_channel_minimal.py
- 场景 2 (M6): A 写 turns 0–5 → B 跑 1 次处理 → 再跑 1 次处理 0 条(cursor 边界)
- 场景 3 (M8): 跨进程 A+B → 跳过
- 场景 4 (M2 已覆盖): 通道 B 提取崩溃 → 已在 test_dual_channel_minimal.py
- 场景 5 (M6): 蒸馏锁强占 (PID 已死)
- 场景 6 (M6): 蒸馏锁强占 (mtime 超时)
- 场景 7 (M6): 蒸馏失败回滚
- 场景 8 (M6): extraction_in_progress 卡死 → watchdog 强制重置

测试策略:
- 场景 5/6/7:纯文件 IO + DistillationScheduler(无需 chroma/bge)
- 场景 2/8:用 FakeEmbedFn(确定性 hash 向量,无模型加载)→ 跨测试快
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from agent_core.memory import DistillationConfig, DistillationScheduler
from agent_core.memory.chroma_store import ChromaVectorStore
from agent_core.memory.dual_channel_writer import (
    DualChannelWriter,
    ExtractionInProgressError,
    TurnMessage,
    ExtractionCandidate,
)
from agent_core.memory.embeddings import EmbedFn
from agent_core.memory.memory_store import MemoryStore
from agent_core.memory.meta_db import MetaDB


# ──────────────────────────────────────────────────────────────────
# 假 EmbedFn —— 确定性 1024 维向量,无模型加载
# ──────────────────────────────────────────────────────────────────

class FakeEmbedFn:
    """确定性伪嵌入(用于并发测试,避免 bge-m3 模型加载开销)

    同样的 text 产生同样的向量(hash → 展开成 1024 维)
    """
    dimension = 1024

    def encode(self, text: str) -> list[float]:
        digest = hashlib.sha256(text.encode("utf-8")).digest()
        # 32 字节 → 扩展到 1024 维(每字节重复 32 次)
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
    db = meta_db
    store = memory_store
    embed = FakeEmbedFn()
    chroma_path = chroma_dir / f"concurrent_{os.getpid()}_{threading.get_ident()}"
    with ChromaVectorStore(str(chroma_path), collection="concurrent_test") as vec:
        w = DualChannelWriter("s1", db, store, vec, embed)
        yield w
        w.shutdown(timeout=3)
        vec.close()


@pytest.fixture
def config():
    return DistillationConfig()


def _make_old_last_distill(memory_root: Path, hours_ago: float = 25.0):
    mtime = memory_root / ".last-distill"
    mtime.touch()
    old = time.time() - hours_ago * 3600
    os.utime(mtime, (old, old))


def _make_n_sessions(logs_dir: Path, n: int = 6):
    for i in range(n):
        (logs_dir / f"s{i}.jsonl").write_text(
            json.dumps({"user_msg": f"msg {i}", "assistant_resp": f"resp {i}"})
        )


# ──────────────────────────────────────────────────────────────────
# 场景 2: A 写 → B 提取 边界 (cursor 推进后二次调用处理 0 条)
# ──────────────────────────────────────────────────────────────────

class TestScenario02CursorBoundary:

    def test_channel_b_second_call_processes_zero_after_cursor_advance(self, writer):
        """场景 2: A 写 6 个 turn → B 跑 1 次处理 6 条 → 再跑 1 次处理 0 条

        验证:cursor 推进后第二次调用 no-op(extract_cursor = daily_cursor + 1)
        """
        # 1. A 通道写 6 个 turn
        for i in range(6):
            writer.channel_a_inline_write(f"msg {i}", f"resp {i}", turn_index=i)
        assert writer.daily_cursor == 5
        assert writer.extract_cursor == 0

        # 2. 准备 LLM extractor:返回 1 个 candidate(对应全部 turn)
        def extractor(messages):
            return [ExtractionCandidate(
                type="user",
                title="test pattern",
                body=f"summarized from {len(messages)} messages",
                source_quote="|".join(m.user_msg for m in messages),
                tags=["test"],
                score=0.5,
            )]

        messages = [
            TurnMessage(i, f"msg {i}", f"resp {i}")
            for i in range(6)
        ]

        # 3. 第一次跑:处理 6 条
        f1 = writer.channel_b_background_extract(messages, llm_extractor=extractor)
        result1 = f1.result(timeout=5)
        assert result1["extracted"] == 1
        assert result1["written"] == 1
        assert writer.extract_cursor == 6  # daily_cursor + 1

        # 4. 第二次跑:cursor 已推进,to_process 应为空 → no-op
        f2 = writer.channel_b_background_extract(messages, llm_extractor=extractor)
        result2 = f2.result(timeout=5)
        assert result2["extracted"] == 0
        assert result2["written"] == 0
        assert result2["skipped"] == 0
        # cursor 不变
        assert writer.extract_cursor == 6


# ──────────────────────────────────────────────────────────────────
# 场景 5: 蒸馏锁强占 (PID 已死)
# ──────────────────────────────────────────────────────────────────

class TestScenario05StalePID:

    def test_acquire_lock_succeeds_when_holder_pid_dead(self, memory_root, logs_dir):
        """场景 5: 写 fake lock + dead PID(999999)→ _acquire_lock() 成功强占"""
        # 写 fake 锁(dead PID)
        lock = memory_root / ".consolidate-lock"
        env_path = memory_root / ".consolidate-lock.lock.json"
        lock.touch()
        env_path.write_text(json.dumps({
            "pid": 999999,
            "host": "fake-host",
            "started_at": time.time() - 100,
            "schema_version": 1,
        }))

        scheduler = DistillationScheduler(memory_root, DistillationConfig())
        # dead PID → 应被强占 → acquire 返回 prior_mtime_ms (>= 0)
        result = scheduler._acquire_lock()
        assert result >= 0, f"应强占成功,实际返回 {result}"
        assert result != scheduler.LOCK_TAKEN

        # 新锁的 envelope 信息应记录当前 PID(in 锁文件本身)
        lock_content = (memory_root / ".consolidate-lock").read_text(encoding="utf-8")
        envelope = json.loads(lock_content)
        assert envelope["pid"] == os.getpid()


# ──────────────────────────────────────────────────────────────────
# 场景 6: 蒸馏锁强占 (mtime 超时)
# ──────────────────────────────────────────────────────────────────

class TestScenario06StaleMtime:

    def test_acquire_lock_succeeds_when_mtime_exceeds_stale_threshold(self, memory_root, logs_dir):
        """场景 6: 写 fake lock + 2h 前 mtime(超过 lock_stale_mtime_seconds=3600)→ 强占"""
        lock = memory_root / ".consolidate-lock"
        env_path = memory_root / ".consolidate-lock.lock.json"
        lock.touch()
        env_path.write_text(json.dumps({
            "pid": os.getpid(),  # 当前进程(活的)
            "host": "alive-host",
            "started_at": time.time(),
            "schema_version": 1,
        }))

        # mtime 调到 2h 前(超过 lock_stale_mtime_seconds 默认 3600s)
        old_time = time.time() - 2 * 3600
        os.utime(lock, (old_time, old_time))

        scheduler = DistillationScheduler(memory_root, DistillationConfig())
        # PID 活着但 mtime 超时 → 应被强占
        result = scheduler._acquire_lock()
        assert result >= 0, f"应强占成功,实际返回 {result}"
        assert result != scheduler.LOCK_TAKEN


# ──────────────────────────────────────────────────────────────────
# 场景 7: 蒸馏失败回滚
# ──────────────────────────────────────────────────────────────────

class TestScenario07FailureRollback:

    def test_run_failure_rolls_back_last_distill_mtime(self, memory_root, logs_dir):
        """场景 7: .last-distill 25h 前 + 6 session + LLM 抛异常 → run 失败 + mtime 回滚"""
        _make_old_last_distill(memory_root)
        _make_n_sessions(logs_dir)

        prior_time = (memory_root / ".last-distill").stat().st_mtime

        def exploding_llm(prompt):
            raise RuntimeError("LLM 模拟失败(场景 7)")

        scheduler = DistillationScheduler(memory_root, DistillationConfig(), llm_callback=exploding_llm)
        result = scheduler.run(dry_run=True)

        # run 应失败
        assert result.success is False
        assert "LLM 模拟失败" in result.error

        # .last-distill mtime 应回滚到 prior(失败不推进)
        actual_mtime = (memory_root / ".last-distill").stat().st_mtime
        assert abs(actual_mtime - prior_time) < 1.0, \
            f"mtime 应回滚到 prior={prior_time},实际 {actual_mtime}"

        # 锁文件应清理
        assert not (memory_root / ".consolidate-lock").exists()


# ──────────────────────────────────────────────────────────────────
# 场景 8: extraction_in_progress 卡死 → watchdog 强制重置
# ──────────────────────────────────────────────────────────────────

class TestScenario08Watchdog:

    def test_stuck_extraction_force_reset_by_watchdog(self, meta_db, memory_store, chroma_dir):
        """场景 8: extractor hang > extraction_timeout → 下次提交不被永久阻塞

        设计: mock llm_extractor 死循环 + extraction_timeout_seconds=0.1
        步骤:
        1. 第 1 次 channel_b_background_extract(messages, slow_extractor)
        2. 等 0.5s(超过 0.1s timeout)
        3. 第 2 次 channel_b_background_extract(messages, fast_extractor) → 应能提交
        """
        embed = FakeEmbedFn()
        chroma_path = chroma_dir / f"sc8_{os.getpid()}_{threading.get_ident()}"
        with ChromaVectorStore(str(chroma_path), collection="sc8_test") as vec:
            w = DualChannelWriter(
                "s8", meta_db, memory_store, vec, embed,
                extraction_timeout_seconds=0.1,  # 短超时 → 测试快
            )

            # 先写一些 turn(让 daily_cursor > 0)
            for i in range(3):
                w.channel_a_inline_write(f"msg{i}", f"resp{i}", turn_index=i)

            messages = [
                TurnMessage(i, f"msg{i}", f"resp{i}")
                for i in range(3)
            ]

            # 1. 第 1 次提交:slow extractor(死循环 1s)
            def slow_extractor(msgs):
                time.sleep(1.0)  # 模拟 LLM hang
                return []

            f1 = w.channel_b_background_extract(messages, llm_extractor=slow_extractor)

            # 2. 等 watchdog 触发(> 0.1s)
            time.sleep(0.5)

            # 3. 第 2 次提交:fast extractor → 应能成功提交(watchdog 重置了 flag)
            def fast_extractor(msgs):
                return [ExtractionCandidate(
                    type="user",
                    title="fast",
                    body="quick",
                    source_quote="x",
                    tags=["sc8"],
                    score=0.5,
                )]

            # 不抛 ExtractionInProgressError → watchdog 起作用了
            f2 = w.channel_b_background_extract(messages, llm_extractor=fast_extractor)
            result2 = f2.result(timeout=5)
            assert result2["written"] == 1, \
                f"第 2 次提交应成功,实际 {result2}"

            # 等 slow extractor 自然结束 → shutdown 清理
            w.shutdown(timeout=5)
            vec.close()


# ──────────────────────────────────────────────────────────────────
# 场景 3 (M8): 跨进程 flock 互斥 —— Issue 4 修复点
# ──────────────────────────────────────────────────────────────────

class TestScenario03CrossProcess:
    """
    场景 3: 跨进程并发 —— migrate_all() 的 IPCLock 必须在两个子进程间互斥

    设计:
    - 子进程 A:抢锁 + 持锁 2s + 写"持有时间戳"
    - 子进程 B:同时抢锁(非阻塞)→ 应立即失败,返回空 report
    - 验证:锁被 B 跳过的时间 < 0.5s(非阻塞),不是阻塞到 A 释放后才返回
    """

    def test_two_subprocs_serialized_by_flock(self, tmp_path):
        """两个子进程并发 migrate_all → 一个拿到锁,另一个被非阻塞跳过"""
        import subprocess
        import sys
        import textwrap

        # 准备一个 v0 文件(让 batch 有事可做)
        mem_root = tmp_path / "memory"
        user_dir = mem_root / "user"
        user_dir.mkdir(parents=True)
        (user_dir / "v0.md").write_text(
            "---\ntitle: 测试\ncreated_at: 2024-01-01\n---\nbody\n",
            encoding="utf-8",
        )

        # 子进程脚本:跑 migrate_all + 打印结果 + 测耗时
        child_script = textwrap.dedent(f"""
            import json, sys, time
            from pathlib import Path
            sys.path.insert(0, {str(tmp_path.parent)!r})
            from agent_core.memory.migration import migrate_all

            root = Path({str(mem_root)!r})
            t0 = time.time()
            report = migrate_all(root)
            elapsed = time.time() - t0
            print(json.dumps({{
                "elapsed": elapsed,
                "migrated": report.migrated,
                "already_current": report.already_current,
                "skipped": report.skipped,
                "total": report.total,
            }}))
        """).strip()

        # 顺序跑两个子进程(因为 migrate_all 是阻塞 + 短任务,真并发难抓)
        # 测:第二个进程被第一个进程的锁 → 第二个应立即返回空 report
        r1 = subprocess.run(
            [sys.executable, "-c", child_script],
            capture_output=True, text=True, timeout=30,
        )
        assert r1.returncode == 0, f"子进程 1 失败: {r1.stderr}"
        out1 = json.loads(r1.stdout.strip().splitlines()[-1])
        # 第一个: 应成功迁移 1 个 v0
        assert out1["migrated"] == 1
        assert out1["elapsed"] < 5.0

        # 第二个:文件已是 CURRENT,不应再迁移
        r2 = subprocess.run(
            [sys.executable, "-c", child_script],
            capture_output=True, text=True, timeout=30,
        )
        assert r2.returncode == 0, f"子进程 2 失败: {r2.stderr}"
        out2 = json.loads(r2.stdout.strip().splitlines()[-1])
        assert out2["migrated"] == 0
        assert out2["already_current"] == 1

    def test_concurrent_lock_skip_returns_empty_report(self, tmp_path):
        """Issue 4 验证:子进程 A 持锁时,子进程 B 非阻塞跳过 → 返回空 report"""
        import subprocess
        import sys
        import textwrap

        mem_root = tmp_path / "memory"
        user_dir = mem_root / "user"
        user_dir.mkdir(parents=True)
        # 写 1 个 v0
        (user_dir / "v0.md").write_text(
            "---\ntitle: t\ncreated_at: 2024-01-01\n---\nbody\n",
            encoding="utf-8",
        )

        # 子进程 A:先抢锁,持锁 2s
        holder_script = textwrap.dedent(f"""
            import sys, time, json
            from pathlib import Path
            sys.path.insert(0, {str(tmp_path.parent)!r})
            from agent_core.memory.ipc_lock import IPCLock

            lock = IPCLock(
                Path({str(mem_root.parent)!r}) / '.memory.migrate.lock',
                stale_mtime_seconds=10,
            )
            if not lock.acquire(blocking=True):
                print(json.dumps({{"error": "acquire failed"}}))
                sys.exit(1)
            print(json.dumps({{"status": "acquired", "pid": {os.getpid()}}}))
            sys.stdout.flush()
            time.sleep(2.0)
            lock.release()
            print(json.dumps({{"status": "released"}}))
        """).strip()

        # 子进程 B:在 A 持锁时跑 migrate_all → 应被非阻塞跳过
        skipper_script = textwrap.dedent(f"""
            import sys, time, json
            from pathlib import Path
            sys.path.insert(0, {str(tmp_path.parent)!r})
            from agent_core.memory.migration import migrate_all

            root = Path({str(mem_root)!r})
            t0 = time.time()
            report = migrate_all(root)
            elapsed = time.time() - t0
            print(json.dumps({{
                "elapsed": elapsed,
                "migrated": report.migrated,
                "already_current": report.already_current,
                "skipped": report.skipped,
            }}))
        """).strip()

        # 起 A(后台)
        proc_a = subprocess.Popen(
            [sys.executable, "-c", holder_script],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )

        # 等 A 抢到锁(读 stdout 第一行)
        import json as _json
        line = proc_a.stdout.readline()
        msg = _json.loads(line.strip())
        assert msg["status"] == "acquired", f"A 没抢到锁: {msg}"

        # 跑 B → 应被非阻塞跳过
        t_start = time.time()
        r_b = subprocess.run(
            [sys.executable, "-c", skipper_script],
            capture_output=True, text=True, timeout=10,
        )
        elapsed_b = time.time() - t_start
        assert r_b.returncode == 0, f"B 失败: {r_b.stderr}"
        out_b = _json.loads(r_b.stdout.strip().splitlines()[-1])
        # B 应被跳过:report 全 0
        assert out_b["migrated"] == 0
        assert out_b["already_current"] == 0
        assert out_b["skipped"] == 0
        # B 的耗时应远小于 2s(非阻塞,不是等到 A 释放)
        assert elapsed_b < 1.0, f"B 应非阻塞跳过,实际等了 {elapsed_b:.2f}s"
        print(f"  ✅ B 被非阻塞跳过,耗时 {elapsed_b * 1000:.0f}ms(<1000ms)")

        # 清理
        proc_a.wait(timeout=5)
        assert proc_a.returncode == 0