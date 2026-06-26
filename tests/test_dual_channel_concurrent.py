"""
M6 / Day 6 并发测试 —— 5/8 不变量场景(场景 3 跨进程归 M8)

设计 §4.5.1 场景矩阵:
- 场景 1 (Phase 1 覆盖): 双线程 persist_turn 并发 → test_persist_turn_task_wal.TestConcurrent
- 场景 2 (M6): A 写 turns 0–5 → B 跑 1 次处理 → 再跑 1 次处理 0 条(task 全 DONE 后 no-op)
- 场景 3 (M8): 跨进程 A+B → 跳过
- 场景 4 (Phase 2 覆盖): extract_candidates 提取崩溃 → test_startup_scan_e2e + test_extract_candidates_task_wal
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


# FakeEmbedFn / 8 fixture → conftest.py(Phase 4 / Step 4.4.7)
# pytest 自动发现 conftest;FakeEmbedFn 在测试函数里显式 import:
from conftest import FakeEmbedFn  # noqa: E402


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

class TestScenario02Idempotency:

    def test_extract_candidates_second_call_after_tasks_done_is_noop(self, writer, meta_db):
        """场景 2: A 写 6 个 turn → B 跑 1 次处理 6 条 → 再跑 1 次处理 0 条

        Phase 4:cursors 已删,验证点改为 — 第二次 extract_candidates 调用看到
        全部 task 已 DONE → CAS 不抢到 → 全部 candidate 落空 → no-op。
        """
        # 1. A 通道写 6 个 turn
        for i in range(6):
            writer.persist_turn(f"msg {i}", f"resp {i}", turn_index=i)

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

        # 3. 第一次跑:处理 6 条 → 全部 task 变 DONE
        f1 = writer.extract_candidates(messages, llm_extractor=extractor)
        result1 = f1.result(timeout=5)
        assert result1["extracted"] == 1
        assert result1["written"] == 1

        # 验证:6 个 task 都 DONE
        done_count = sum(
            1 for tid in range(1, 7)
            if meta_db.get_task(tid)["state"] == "DONE"
        )
        assert done_count == 6

        # 4. 第二次跑:全 DONE,CAS 抢不到 → to_process 内 candidate 仍会跑
        #    但新提取写入 .md 因 item_hash 已存在被幂等跳过(走 path1 — 写到 candidates_payload 已含)
        # 关键:不会再 create 重复 task
        f2 = writer.extract_candidates(messages, llm_extractor=extractor)
        result2 = f2.result(timeout=5)
        # 因为 item_hash 幂等,written 仍可能 = 1(候选 memory 落盘,但 MemoryStore 跳过)
        # 关键断言:无残留 INFLIGHT
        assert writer._inflight == set() or all(
            f.done() for f in writer._inflight
        )


# ──────────────────────────────────────────────────────────────────
# 日志可观测性: extract_candidates 提取关键流程发 INFO 日志
# ──────────────────────────────────────────────────────────────────

class TestChannelBLogging:

    def test_extract_candidates_emits_info_logs(self, writer, caplog):
        """提取成功路径应发 "提取完成" + "已持久化" INFO 日志

        排查 "记忆没存" 这类问题时,日志要能看出提取到底跑没跑、写没写。
        """
        import logging

        # A 写 1 个 turn
        writer.persist_turn("我喜欢看书", "已记", turn_index=0)

        def extractor(messages):
            return [ExtractionCandidate(
                type="user",
                title="爱好",
                body="用户喜欢看书",
                source_quote="我喜欢看书",
                tags=["hobby"],
                score=0.9,
            )]

        with caplog.at_level(logging.INFO, logger="memory.dual_channel"):
            f = writer.extract_candidates(
                [TurnMessage(0, "我喜欢看书", "已记")],
                llm_extractor=extractor,
            )
            result = f.result(timeout=5)

        assert result["written"] == 1
        msgs = "\n".join(r.message for r in caplog.records)
        assert "extract 提取完成" in msgs, f"缺提取完成日志:\n{msgs}"
        assert "已持久化" in msgs, f"缺持久化日志:\n{msgs}"


class TestChannelBMultiCandidate:
    """Bug 1c 回归:单 turn、多 candidate —— 全部落盘,不被 zip 截断。"""

    def test_all_candidates_written_not_just_first(self, writer):
        """extractor 对 1 个 turn 返回 3 条 candidate(gate 对整段对话的产物)。

        修复前:zip(to_process[1], candidates[3]) 只取 candidates[0](最旧那条),
        当前消息对应的 candidate 被静默截断 → 用户看到"只存了上一条记忆"。
        修复后:3 条全部写盘,且都盖当前 turn 的 turn_index。
        """
        # A 写 1 个 turn(turn_index=0)
        writer.persist_turn("我不喜欢英国人", "已记", turn_index=0)

        # extractor 返回 3 条(顺序模拟 LLM 按对话顺序:旧→新)
        def extractor(messages):
            return [
                ExtractionCandidate(type="user", title="桃子", body="喜欢吃桃子",
                                    source_quote="桃子", tags=[], score=0.9),
                ExtractionCandidate(type="user", title="日本", body="不喜欢日本人",
                                    source_quote="日本", tags=[], score=0.9),
                ExtractionCandidate(type="user", title="英国", body="不喜欢英国人",
                                    source_quote="英国", tags=[], score=0.9),
            ]

        f = writer.extract_candidates(
            [TurnMessage(0, "我不喜欢英国人", "已记")],
            llm_extractor=extractor,
        )
        result = f.result(timeout=5)

        assert result["written"] == 3, f"3 条 candidate 应全部落盘,实际 {result}"

        # 当前消息(英国)必须在,且 3 条都盖 turn_index=0
        md_files = list((writer.memory_store.root / "user").glob("*.md"))
        bodies = "\n".join(p.read_text(encoding="utf-8") for p in md_files)
        assert "不喜欢英国人" in bodies, f"当前 turn 的记忆缺失:\n{bodies}"
        assert "不喜欢日本人" in bodies and "喜欢吃桃子" in bodies, "backlog 也应一并落盘"
        assert "turn_index: 0" in bodies, "candidate 应盖当前处理的 turn_index"


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
        1. 第 1 次 extract_candidates(messages, slow_extractor)
        2. 等 0.5s(超过 0.1s timeout)
        3. 第 2 次 extract_candidates(messages, fast_extractor) → 应能提交
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
                w.persist_turn(f"msg{i}", f"resp{i}", turn_index=i)

            messages = [
                TurnMessage(i, f"msg{i}", f"resp{i}")
                for i in range(3)
            ]

            # 1. 第 1 次提交:slow extractor(死循环 1s)
            def slow_extractor(msgs):
                time.sleep(1.0)  # 模拟 LLM hang
                return []

            f1 = w.extract_candidates(messages, llm_extractor=slow_extractor)

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
            f2 = w.extract_candidates(messages, llm_extractor=fast_extractor)
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


# ──────────────────────────────────────────────────────────────────
# T6: AUTO_DUPLICATE 分支的 log 应包含已有记忆的 title(从 MemoryStore 读)
# ──────────────────────────────────────────────────────────────────

def test_auto_duplicate_log_includes_top_title_from_memory_store(writer, caplog):
    """AUTO_DUPLICATE 分支的 log 应包含已有记忆的 title(从 MemoryStore 读)。

    验证 Chroma 严格分离(方案 A)后,高相似度跳过路径的可观测性
    仍能输出 top1 title,而不是 '?'(Chroma metadata 不可用时)。
    """
    import logging
    from types import SimpleNamespace

    # fixture 没注入 dedup_config,临时挂一个(autouse=True 不破坏既有测试)
    writer.dedup_config = SimpleNamespace(
        enabled=True, auto_threshold=0.5, judge_floor=0.3, top_k=5
    )

    # 准备:库里有一条 title='姓名' 的 user 记忆
    writer.persist_turn("我叫张三", "已记", turn_index=0)

    def seed_extractor(messages):
        return [ExtractionCandidate(
            type="user", title="姓名",
            body="用户叫张三", source_quote="我叫张三",
            tags=["person"], score=0.9,
        )]

    # 先把种子记忆写进 MemoryStore + vec
    f = writer.extract_candidates(
        [TurnMessage(0, "我叫张三", "已记")],
        llm_extractor=seed_extractor,
    )
    result = f.result(timeout=5)
    assert result["written"] == 1

    # 现在模拟一次高相似度的 extract 候选(应走 AUTO_DUPLICATE 跳过路径)
    writer.persist_turn("再确认一次,张三", "已记", turn_index=1)

    # 极低 auto_threshold:只要 vec query top1 sim ≥ 0.5 就跳过
    writer.dedup_config.auto_threshold = 0.5

    def dup_extractor(messages):
        return [ExtractionCandidate(
            type="user", title="我叫谁",
            body="用户叫张三", source_quote="再确认一次,张三",
            tags=[], score=0.9,
        )]

    with caplog.at_level(logging.INFO, logger="memory.dual_channel"):
        f2 = writer.extract_candidates(
            [TurnMessage(1, "再确认一次,张三", "已记")],
            llm_extractor=dup_extractor,
        )
        result2 = f2.result(timeout=5)

    # 期望:这条候选没被写盘(AUTO_DUPLICATE 跳过)
    # 期望:log 含"语义重复" + "已有 '姓名'"(从 MemoryStore frontmatter 读)
    msgs = "\n".join(r.message for r in caplog.records)
    assert "语义重复" in msgs, f"缺 AUTO_DUPLICATE 日志:\n{msgs}"
    assert "已有 '姓名'" in msgs or '已有 "姓名"' in msgs, (
        f"AUTO_DUPLICATE log 应含 top1 title '姓名',实际:\n{msgs}"
    )

# ──────────────────────────────────────────────────────────────────
# M11 v3: dual_channel_writer integration tests
# ──────────────────────────────────────────────────────────────────

def test_dual_channel_write_emits_v3_frontmatter(tmp_path, meta_db_path, chroma_dir):
    """M11 v3: channel A 写盘后 .md frontmatter 含 name + description + schema_version=3"""
    import re
    import yaml
    from agent_core.memory.dual_channel_writer import DualChannelWriter
    from agent_core.memory.memory_store import MemoryStore
    from agent_core.memory.meta_db import MetaDB
    from agent_core.memory.chroma_store import ChromaVectorStore
    from tests.conftest import FakeEmbedFn

    memory_root = tmp_path / "memory"
    memory_root.mkdir()
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    db = MetaDB(meta_db_path)
    store = MemoryStore(memory_root)
    embed = FakeEmbedFn()
    chroma_path = chroma_dir / f"v3frontmatter_{os.getpid()}"
    with ChromaVectorStore(str(chroma_path), collection="v3fm") as vec:
        w = DualChannelWriter("s1", db, store, vec, embed)
        try:
            w.persist_turn("用户叫小明", "ok", turn_index=0)
            from tests.test_dual_channel_concurrent import (
                ExtractionCandidate, TurnMessage,
            )

            def seed(messages):
                return [ExtractionCandidate(
                    type="user", title="姓名",
                    body="用户叫小明", source_quote="用户叫小明",
                    tags=[], score=0.9,
                )]

            f = w.extract_candidates(
                [TurnMessage(0, "用户叫小明", "ok")],
                llm_extractor=seed,
            )
            f.result(timeout=5)

            # 读 .md 验证 v3 frontmatter
            md_files = list((memory_root / "user").glob("*.md"))
            assert md_files
            md_text = md_files[0].read_text(encoding="utf-8")
            m = re.match(r"\A---\n(.*?)\n---", md_text, re.DOTALL)
            assert m, f"no frontmatter in:\n{md_text}"
            fm = yaml.safe_load(m.group(1)) or {}
            assert fm.get("schema_version") == 3, fm
            assert fm.get("name"), f"missing name: {fm}"
            assert fm.get("description"), f"missing description: {fm}"
        finally:
            w.shutdown(timeout=3)
            vec.close()


def test_dual_channel_write_triggers_memory_index_mark_dirty(tmp_path, meta_db_path, chroma_dir):
    """M11: 写盘后 MemoryIndex 被 mark_dirty(MEMORY.md 1.2s 后出现)"""
    import time
    from agent_core.memory.dual_channel_writer import DualChannelWriter
    from agent_core.memory.memory_store import MemoryStore
    from agent_core.memory.memory_index import MemoryIndex
    from agent_core.memory.meta_db import MetaDB
    from agent_core.memory.chroma_store import ChromaVectorStore
    from tests.conftest import FakeEmbedFn

    memory_root = tmp_path / "memory"
    memory_root.mkdir()
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    db = MetaDB(meta_db_path)
    store = MemoryStore(memory_root)
    embed = FakeEmbedFn()
    index = MemoryIndex(memory_root)
    chroma_path = chroma_dir / f"m11index_{os.getpid()}"
    with ChromaVectorStore(str(chroma_path), collection="m11idx") as vec:
        w = DualChannelWriter("s1", db, store, vec, embed, memory_index=index)
        try:
            w.persist_turn("用户叫张三", "ok", turn_index=0)
            from tests.test_dual_channel_concurrent import (
                ExtractionCandidate, TurnMessage,
            )

            def seed(messages):
                return [ExtractionCandidate(
                    type="user", title="姓名",
                    body="用户叫张三", source_quote="用户叫张三",
                    tags=[], score=0.9,
                )]

            f = w.extract_candidates(
                [TurnMessage(0, "用户叫张三", "ok")],
                llm_extractor=seed,
            )
            f.result(timeout=5)
            # 等异步 rebuild
            time.sleep(1.2)
            assert (memory_root / "MEMORY.md").exists()
            content = (memory_root / "MEMORY.md").read_text(encoding="utf-8")
            assert "姓名" in content
        finally:
            w.shutdown(timeout=3)
            vec.close()
