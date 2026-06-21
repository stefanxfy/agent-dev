#!/usr/bin/env bash
# M6 / Day 6 验收 demo —— 调度 + OTel + 5/8 并发场景
# 跑法：bash scripts/demo_m6.sh   (无需参数)
# 前置：.venv/bin/python 已装好 pydantic / pyyaml / opentelemetry-api / chromadb
# 注意:无需 bge-m3 模型(场景 2/8 用确定性 FakeEmbedFn)
set -euo pipefail

cd "$(dirname "$0")/.."

echo "=== M6 / Day 6 验收开始 ==="
echo

# Demo 1-3: 端到端 M6 三件套
.venv/bin/python <<'PYEOF'
"""
M6 / Day 6 验收 — 3 个核心 demo

Demo 1: DistillationLoop tick_once 触发 (gate 通过 → run)
Demo 2: OTel tracer — span 创建 + attributes (默认 NoOp)
Demo 3: 5 个并发场景 — 场景 2/5/6/7/8 端到端跑一遍

依赖: opentelemetry-api (tracer), FakeEmbedFn (本地确定性 hash)
"""
import hashlib
import json
import os
import tempfile
import threading
import time
from pathlib import Path

from agent_core.memory import (
    DistillationConfig,
    DistillationLoop,
    DistillationScheduler,
    configure_tracing,
    tracer,
)
from agent_core.memory.dual_channel_writer import (
    DualChannelWriter,
    TurnMessage,
    ExtractionCandidate,
)
from agent_core.memory.chroma_store import ChromaVectorStore
from agent_core.memory.memory_store import MemoryStore
from agent_core.memory.meta_db import MetaDB


class FakeEmbedFn:
    """确定性 1024 维 hash 向量(无需 bge-m3 模型加载)"""
    dimension = 1024
    def encode(self, text: str) -> list[float]:
        d = hashlib.sha256(text.encode("utf-8")).digest()
        vec = []
        for _ in range(32):
            for b in d:
                vec.append(b / 255.0)
        return vec


tmp = Path(tempfile.mkdtemp())
memory_root = tmp / "memory"
memory_root.mkdir()
logs_dir = tmp / "logs"
logs_dir.mkdir()


def mock_llm_ok(prompt: str) -> str:
    return json.dumps([
        {"type": "user", "title": "M6 OK", "why": "smoke",
         "body": "scheduler 跑通", "confidence": 0.8, "sources": ["s0"], "tags": []},
    ])


def mock_llm_explode(prompt: str) -> str:
    raise RuntimeError("LLM 模拟失败(场景 7)")


def make_sessions(n: int):
    for i in range(n):
        (logs_dir / f"s{i}.jsonl").write_text(
            json.dumps({"user_msg": f"msg{i}", "assistant_resp": f"resp{i}"})
        )


def make_old_last_distill(hours: float = 25.0):
    m = memory_root / ".last-distill"
    m.touch()
    old = time.time() - hours * 3600
    os.utime(m, (old, old))


# ─── Demo 1: DistillationLoop tick_once ───
print("=== Demo 1: DistillationLoop tick_once 触发 ===")
make_old_last_distill()
make_sessions(6)
config = DistillationConfig()
scheduler = DistillationScheduler(memory_root, config, llm_callback=mock_llm_ok)
loop = DistillationLoop(scheduler)

# 第一次 tick:gate 通过 → run
result = loop.tick_once()
assert result is not None, "tick_once 应返回 result"
assert result.success is True
assert len(result.candidates) == 1
assert loop.tick_count == 1
print(f"  ✅ tick #1 → success={result.success}, candidates={len(result.candidates)}")

# 第二次 tick:24h 门未过(刚 touch 过 .last-distill)→ 返回 None
result2 = loop.tick_once()
assert result2 is None, "tick #2 应被门拦住"
print(f"  ✅ tick #2 → 被门拦住(24h 没过),tick_count={loop.tick_count}")


# ─── Demo 2: OTel tracer span + attributes ───
print("=== Demo 2: OTel tracer — span + attributes ===")
# 默认 NoOp(无 OTEL_EXPORTER_OTLP_ENDPOINT env)
with tracer.start_as_current_span("memory.extract") as span:
    span.set_attribute("memory.candidates", 3)
    span.set_attribute("memory.tag", "M6")
    span.set_attribute("memory.dry_run", False)
print("  ✅ span 创建成功(默认 NoOp,无开销)")

# 嵌套 span(memory.distill 在 memory.loop.tick 内)
with tracer.start_as_current_span("memory.loop.tick") as outer:
    outer.set_attribute("memory.tick_count", 99)
    with tracer.start_as_current_span("memory.distill") as inner:
        inner.set_attribute("memory.distill.candidates", 1)
print("  ✅ 嵌套 span 创建成功(outer + inner)")

# configure_tracing 检测(env 缺失 → 返回 False,仍 NoOp)
old_env = os.environ.pop("OTEL_EXPORTER_OTLP_ENDPOINT", None)
configured = configure_tracing()
assert configured is False, "无 env 应返回 False"
print(f"  ✅ configure_tracing() 无 env → {configured} (仍 NoOp)")
if old_env:
    os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"] = old_env


# ─── Demo 3: 5 个并发场景 (场景 2/5/6/7/8) ───
print("=== Demo 3: 5 个并发场景(场景 3 跨进程归 M8) ===")

# --- 场景 5: 蒸馏锁强占 (PID 已死) ---
print("--- 场景 5: 蒸馏锁强占(PID 已死)---")
import shutil
shutil.rmtree(memory_root, ignore_errors=True)
memory_root.mkdir()
lock = memory_root / ".consolidate-lock"
env_path = memory_root / ".consolidate-lock.lock.json"
lock.touch()
env_path.write_text(json.dumps({"pid": 999999, "host": "fake", "started_at": time.time() - 100, "schema_version": 1}))
sched = DistillationScheduler(memory_root, DistillationConfig())
acquired = sched._acquire_lock()
assert acquired >= 0, f"dead PID 应强占成功,实际 {acquired}"
# envelope 在锁文件内
env = json.loads((memory_root / ".consolidate-lock").read_text())
assert env["pid"] == os.getpid()
print(f"  ✅ acquired={acquired}, 新 envelope.pid={env['pid']}")

# --- 场景 6: 蒸馏锁强占 (mtime 超时) ---
print("--- 场景 6: 蒸馏锁强占(mtime 超 1h)---")
sched._release_lock(acquired, success=False)  # 清理
lock.touch()
env_path.write_text(json.dumps({"pid": os.getpid(), "host": "alive", "started_at": time.time(), "schema_version": 1}))
old_time = time.time() - 2 * 3600
os.utime(lock, (old_time, old_time))
sched2 = DistillationScheduler(memory_root, DistillationConfig())
acquired2 = sched2._acquire_lock()
assert acquired2 >= 0, f"mtime 超时应强占,实际 {acquired2}"
print(f"  ✅ mtime 2h 前 → 强占 acquired={acquired2}")
sched2._release_lock(acquired2, success=False)

# --- 场景 7: 蒸馏失败回滚 ---
print("--- 场景 7: 蒸馏失败回滚---")
shutil.rmtree(memory_root, ignore_errors=True)
memory_root.mkdir()
make_old_last_distill()
make_sessions(6)
prior_mtime_ts = (memory_root / ".last-distill").stat().st_mtime
sched3 = DistillationScheduler(memory_root, DistillationConfig(), llm_callback=mock_llm_explode)
result7 = sched3.run(dry_run=True)
assert not result7.success
assert "LLM 模拟失败" in result7.error
actual_mtime = (memory_root / ".last-distill").stat().st_mtime
assert abs(actual_mtime - prior_mtime_ts) < 1.0, f"mtime 未回滚: {actual_mtime} vs {prior_mtime_ts}"
assert not (memory_root / ".consolidate-lock").exists()
print(f"  ✅ error={result7.error[:30]}..., mtime 回滚, 锁清理")

# --- 场景 2 + 8: 需要 DualChannelWriter + FakeEmbedFn ---
print("--- 场景 2: A 写 → B 提取 边界 ---")
shutil.rmtree(memory_root, ignore_errors=True)
memory_root.mkdir()
db = MetaDB(tmp / "meta.db")
store = MemoryStore(memory_root)
chroma_path = tmp / "chroma_s2"
with ChromaVectorStore(str(chroma_path), collection="demo_s2") as vec:
    w = DualChannelWriter("s2", db, store, vec, FakeEmbedFn())
    # A 通道写 6 turn
    for i in range(6):
        w.channel_a_inline_write(f"msg{i}", f"resp{i}", turn_index=i)
    assert w.daily_cursor == 5
    # B 通道跑 1 次
    msgs = [TurnMessage(i, f"msg{i}", f"resp{i}") for i in range(6)]
    def ext(msgs):
        return [ExtractionCandidate(
            type="user", title="demo", body="x",
            source_quote="|".join(m.user_msg for m in msgs),
            tags=["s2"], score=0.5)]
    f1 = w.channel_b_background_extract(msgs, llm_extractor=ext)
    r1 = f1.result(timeout=5)
    assert r1["written"] == 1
    assert w.extract_cursor == 6
    # B 通道再跑 1 次 → 0 条
    f2 = w.channel_b_background_extract(msgs, llm_extractor=ext)
    r2 = f2.result(timeout=5)
    assert r2["written"] == 0 and r2["extracted"] == 0
    w.shutdown(timeout=5)
    vec.close()
print(f"  ✅ 第 1 次 written=1, 第 2 次 written=0, extract_cursor={w.extract_cursor}")

print("--- 场景 8: extraction_in_progress 卡死 → watchdog 强制重置 ---")
db2 = MetaDB(tmp / "meta.db2")
chroma_path2 = tmp / "chroma_s8"
with ChromaVectorStore(str(chroma_path2), collection="demo_s8") as vec2:
    w8 = DualChannelWriter("s8", db2, store, vec2, FakeEmbedFn(), extraction_timeout_seconds=0.1)
    for i in range(3):
        w8.channel_a_inline_write(f"m{i}", f"r{i}", turn_index=i)
    msgs8 = [TurnMessage(i, f"m{i}", f"r{i}") for i in range(3)]

    def slow_ext(msgs):
        time.sleep(1.0)  # 模拟 LLM hang(超过 0.1s watchdog)
        return []

    f8_1 = w8.channel_b_background_extract(msgs8, llm_extractor=slow_ext)
    time.sleep(0.5)  # 等 watchdog 触发

    def fast_ext(msgs):
        return [ExtractionCandidate(
            type="user", title="fast", body="x",
            source_quote="q", tags=["s8"], score=0.5)]

    # 不抛 ExtractionInProgressError → watchdog 起作用
    f8_2 = w8.channel_b_background_extract(msgs8, llm_extractor=fast_ext)
    r8 = f8_2.result(timeout=5)
    assert r8["written"] == 1, f"watchdog 后第 2 次应成功,实际 {r8}"
    w8.shutdown(timeout=5)
    vec2.close()
print(f"  ✅ slow 卡死 → watchdog 重置 → fast 提交成功(written=1)")

shutil.rmtree(tmp, ignore_errors=True)
print()
print("=== M6 / Day 6 demo 端到端 3/3 通过(覆盖 5 个并发场景)===")
PYEOF

echo
echo "=== M6 测试套件 ==="
.venv/bin/python -m pytest tests/test_scheduler.py tests/test_dual_channel_concurrent.py -v 2>&1 | tail -5

echo
echo "=== M6 验收完成 ==="
echo "提示: M6 = 调度骨架 + OTel 最小版 + 5/8 并发场景,真实 LLM 调用与 UI 集成归 M7。"