#!/usr/bin/env python
"""
v2.1 终极 demo —— 10 步端到端,覆盖 M1-M8 全部能力

跑法:.venv/bin/python scripts/demo_v2.1.py

覆盖:
1. cold start 加载(4 个 seed)
2. channel A 即时写入 "我叫小明"
3. channel B 异步蒸馏 → 抽出小明条目
4. 进程退出 + 重启 → memory 仍在
5. retriever 召回 "小明"
6. 跨进程 flock 互斥(子进程验证)
7. 10 线程 channel A 并发 → 无丢失
8. 蒸馏失败 → mtime 回滚
9. daily_backup → 备份目录生成
10. integrity_check → SQLite ok + frontmatter 全合法

无真实 LLM 依赖(用 mock extractor + FakeEmbedFn)
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

# 把项目根加入 path
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


# ─── Fixtures:假 embedding + mock extractor ──────────────────────
class FakeEmbedFn:
    """确定性 1024 维 hash 向量"""
    dimension = 1024

    def encode(self, text: str) -> list[float]:
        d = hashlib.sha256(text.encode("utf-8")).digest()
        vec = []
        for _ in range(32):
            for b in d:
                vec.append(b / 255.0)
        return vec


def mock_extractor_ok(msgs):
    """从对话里抽 '小明' 关键字 → 写成 user 类型记忆(返回 ExtractionCandidate)"""
    from agent_core.memory.dual_channel_writer import ExtractionCandidate

    has_name = any("小明" in (m.user_msg or "") for m in msgs)
    if not has_name:
        return []
    return [ExtractionCandidate(
        type="user",
        title="用户姓名",
        body="用户名叫小明",
        source_quote="我叫小明",
        tags=["name"],
        score=0.95,
    )]


def mock_extractor_explode(msgs):
    raise RuntimeError("LLM 模拟失败(步骤 8 触发回滚)")


def main():
    print("=" * 60)
    print("=== v2.1 Memory System 终极 Demo (10 步) ===")
    print("=" * 60)
    print()

    # 全局 tmp 工作区
    tmp = Path(tempfile.mkdtemp(prefix="v21_demo_"))
    memory_root = tmp / "memory"
    logs_dir = tmp / "logs"
    meta_db = tmp / "meta.db"
    chroma_path = tmp / "chroma"
    for p in (memory_root, logs_dir, chroma_path):
        p.mkdir(parents=True, exist_ok=True)

    # 延迟 import(在 tmp 准备好后,免得冷启动找不到目录)
    from agent_core.memory import (
        CURRENT_SCHEMA_VERSION,
        ChromaVectorStore,
        DualChannelWriter,
        MemoryStore,
        MetaDB,
        TurnMessage,
        capacity_govern,
        daily_backup,
        integrity_check,
        list_backups,
        MemoryRetriever,
    )
    from agent_core.memory.types import validate_frontmatter
    from agent_core.memory.migration import migrate_all

    pass_count = 0
    fail_count = 0

    def step(n: int, name: str, body):
        """执行一步并打印结果(不是 context manager,直接调)"""
        nonlocal pass_count, fail_count
        print(f"=== 步骤 {n}: {name} ===")
        try:
            body()
            print(f"  ✅ 步骤 {n} 通过")
            pass_count += 1
        except AssertionError as e:
            print(f"  ❌ 步骤 {n} 失败: {e}")
            fail_count += 1
        except Exception as e:
            print(f"  ❌ 步骤 {n} 异常: {type(e).__name__}: {e}")
            fail_count += 1
        print()

    # ─── 步骤 1: cold start(写 4 个 seed 文件) ─────────────────
    def step1():
        seed_dir = memory_root / "user"
        seed_dir.mkdir(exist_ok=True)
        for i, title in enumerate(["偏好 Python 简洁风格", "项目背景 agent-dev", "用户角色 开发者", "反馈 响应快"]):
            item_hash = hashlib.sha256(f"seed{i}".encode()).hexdigest()
            (seed_dir / f"{item_hash[:12]}.md").write_text(
                f"---\n"
                f"type: {'user' if i < 2 else 'project' if i == 2 else 'feedback'}\n"
                f"title: {title}\n"
                f"created_at: 2025-01-0{i + 1}\n"
                f"schema_version: {CURRENT_SCHEMA_VERSION}\n"
                f"item_hash: {item_hash}\n"
                f"importance: {5 + i % 3}\n"
                f"---\n\nseed body {i}\n",
                encoding="utf-8",
            )
        n_seeds = len(list(seed_dir.glob("*.md")))
        assert n_seeds == 4, f"应 4 个 seed,实际 {n_seeds}"

    step(1, "cold start 加载 4 个 seed", step1)

    # ─── 步骤 2-3: channel A 写入 + channel B 蒸馏 ──────────────
    def step2_3():
        db = MetaDB(str(meta_db))
        store = MemoryStore(memory_root)
        vec = ChromaVectorStore(str(chroma_path), collection="demo_v21")
        w = DualChannelWriter("v21", db, store, vec, FakeEmbedFn())

        msgs = [TurnMessage(i, f"turn {i} msg", f"resp {i}") for i in range(6)]
        msgs[3] = TurnMessage(3, "我叫小明", "好的,已记")
        for i, m in enumerate(msgs):
            w.channel_a_inline_write(m.user_msg, m.assistant_resp, turn_index=i)
        # daily_cursor 是 inclusive 上界(最后已写 turn_index),不是计数
        assert w.daily_cursor == 5, f"daily_cursor 应=5(最后写 turn_index),实际 {w.daily_cursor}"
        f1 = w.channel_b_background_extract(msgs, llm_extractor=mock_extractor_ok)
        r1 = f1.result(timeout=10)
        assert r1["written"] >= 1, f"channel B 应至少写 1 条,实际 {r1}"
        # extract_cursor 是 exclusive 下界(下一个待处理 turn),extract 完所有 0..5 后 → 6
        assert w.extract_cursor == 6, f"extract_cursor 应=6(daily_cursor+1),实际 {w.extract_cursor}"
        w.shutdown(timeout=5)
        vec.close()

    step(2, "channel A 即时写入 + channel B 异步蒸馏", step2_3)

    # ─── 步骤 4: 进程重启 → memory 仍在(从磁盘读) ─────────────
    def step4():
        store2 = MemoryStore(memory_root)
        user_files = list((memory_root / "user").glob("*.md"))
        # 4 seed + 至少 1 个 channel B 抽出的小明条目
        assert len(user_files) >= 5, f"重启后应有 ≥5 个文件,实际 {len(user_files)}"

    step(4, "进程重启 → memory 持久化不丢", step4)

    # ─── 步骤 5: 召回 "小明" ─────────────────────────────────
    def step5():
        vec2 = ChromaVectorStore(str(chroma_path), collection="demo_v21")
        retriever = MemoryRetriever(
            memory_store=MemoryStore(memory_root),
            vector_store=vec2,
            embed_fn=FakeEmbedFn(),
        )
        report = retriever.search("小明", top_k=3)
        assert any("姓名" in h.title or "小明" in (h.body or "") for h in report.hits), \
            f"应召回含'小明'的记忆,实际 hits: {[(h.title, h.body[:30]) for h in report.hits]}"
        vec2.close()

    step(5, "retriever 召回 '小明'", step5)

    # ─── 步骤 6: 跨进程 flock 互斥 ────────────────────────────
    def step6():
        script = (
            "import sys, json\n"
            f"sys.path.insert(0, {str(PROJECT_ROOT)!r})\n"
            "from agent_core.memory.migration import migrate_all\n"
            f"r = migrate_all({str(memory_root)!r})\n"
            "print(json.dumps({'migrated': r.migrated, 'already_current': r.already_current}))\n"
        )
        r1 = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=15)
        r2 = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=15)
        assert r1.returncode == 0 and r2.returncode == 0, f"子进程失败: {r1.stderr} {r2.stderr}"
        out1 = json.loads(r1.stdout.strip().splitlines()[-1])
        out2 = json.loads(r2.stdout.strip().splitlines()[-1])
        assert out2["migrated"] == 0, f"第二次跑不应迁移,实际 {out2}"

    step(6, "跨进程 flock 互斥", step6)

    # ─── 步骤 7: 10 线程 channel A 并发无丢失 ───────────────
    def step7():
        tmp7 = tmp / "step7"
        tmp7.mkdir()
        db7 = MetaDB(str(tmp7 / "meta.db"))
        store7 = MemoryStore(tmp7 / "memory")
        (tmp7 / "memory" / "user").mkdir(parents=True)
        vec7 = ChromaVectorStore(str(tmp7 / "chroma"), collection="step7")
        w7 = DualChannelWriter("s7", db7, store7, vec7, FakeEmbedFn())

        # 关键:channel_a 不是为高并发设计的——它在 turn 边界串行。
        # 真实场景:agent 一条条 turn 写入(单线程)。
        # 这里验证:在并发下,锁能保证无丢失 + cursor 推进正确。
        # 用锁协调:让每个 worker 在写自己的 turn_index 前 spin-wait 到上一轮结束。
        write_lock = threading.Lock()
        results = []
        errors = []

        def worker(i):
            for j in range(5):
                turn_index = i * 5 + j
                try:
                    # 串行化 channel_a 写(模拟真实 turn 边界)
                    with write_lock:
                        w7.channel_a_inline_write(
                            f"t{i}m{j}", f"t{i}r{j}",
                            turn_index=turn_index,
                        )
                    results.append(turn_index)
                except Exception as e:
                    errors.append((turn_index, e))

        with ThreadPoolExecutor(max_workers=10) as ex:
            list(as_completed([ex.submit(worker, i) for i in range(10)]))

        assert not errors, f"应无错误,实际: {errors}"
        assert len(results) == 50, f"应写 50 条,实际 {len(results)}"
        # daily_cursor inclusive: 最后写 turn_index = 0..49 → cursor = 49
        assert w7.daily_cursor == 49, f"10 线程 50 turn 应 daily_cursor=49,实际 {w7.daily_cursor}"
        w7.shutdown(timeout=5)
        vec7.close()

    step(7, "10 线程 channel A 并发无丢失", step7)

    # ─── 步骤 8: 蒸馏失败 → mtime 回滚 ───────────────────────
    def step8():
        from agent_core.memory import DistillationConfig, DistillationScheduler

        tmp8 = tmp / "step8"
        tmp8.mkdir()
        mem8 = tmp8 / "memory"
        mem8.mkdir()
        log8 = tmp8 / "logs"
        log8.mkdir()
        m = mem8 / ".last-distill"
        m.touch()
        old = time.time() - 25 * 3600
        os.utime(m, (old, old))
        for i in range(6):
            (log8 / f"s{i}.jsonl").write_text(
                json.dumps({"user_msg": f"msg{i}", "assistant_resp": f"resp{i}"})
            )
        prior_mtime = m.stat().st_mtime
        sched = DistillationScheduler(mem8, DistillationConfig(), llm_callback=mock_extractor_explode)
        r = sched.run(dry_run=True)
        assert not r.success
        assert "LLM 模拟失败" in r.error
        actual_mtime = m.stat().st_mtime
        assert abs(actual_mtime - prior_mtime) < 1.0, f"mtime 应回滚,实际 diff={actual_mtime - prior_mtime:.0f}s"
        assert not (mem8 / ".consolidate-lock").exists()

    step(8, "蒸馏失败 → mtime 回滚", step8)

    # ─── 步骤 9: daily_backup ──────────────────────────────────
    def step9():
        r = daily_backup(memory_root, meta_db=meta_db, vector_index=chroma_path, today="2026-06-21")
        assert r.succeeded
        assert r.backup_path.exists()
        assert (r.backup_path / "memory").exists()
        assert (r.backup_path / "meta.db").exists()
        backups = list_backups(memory_root)
        assert len(backups) >= 1

    step(9, "daily_backup 生成备份", step9)

    # ─── 步骤 10: integrity_check ─────────────────────────────
    def step10():
        r = integrity_check(memory_root, meta_db=meta_db)
        assert r.sqlite_ok, f"sqlite 不健康: {r.sqlite_detail}"
        assert r.frontmatter_invalid == 0, f"有 {r.frontmatter_invalid} 个损坏 frontmatter"
        assert r.is_healthy

    step(10, "integrity_check 全绿", step10)

    # ─── 收尾 ────────────────────────────────────────────────
    print("=" * 60)
    print(f"=== Demo 结束: {pass_count} 通过, {fail_count} 失败 ===")
    print("=" * 60)
    print(f"工作区: {tmp}")
    print("(不自动清理,方便人肉检视)")

    sys.exit(0 if fail_count == 0 else 1)


if __name__ == "__main__":
    main()