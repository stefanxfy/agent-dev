#!/usr/bin/env bash
# M5 / Day 5 验收 demo —— 蒸馏 (autoDream, L5)
# 跑法：bash scripts/demo_m5.sh   (无需参数)
# 前置：.venv/bin/python 已装好 pydantic / pyyaml
#       无需 bge-m3 / chromadb
set -euo pipefail

cd "$(dirname "$0")/.."

echo "=== M5 / Day 5 验收开始 ==="
echo

# Demo 1-5: 端到端 蒸馏流程
.venv/bin/python <<'PYEOF'
"""
M5 / Day 5 验收 — 5 个核心 demo

Demo 1: 四重门 (gate_disabled)
Demo 2: 四重门 (too_soon) — .last-distill 刚 touch 过
Demo 3: 锁原子性 — 10 线程并发 acquire,只 1 个赢
Demo 4: 失败回滚 — mock LLM 抛异常,mtime 不变
Demo 5: 端到端 — dry_run 路径,2 个候选产出,不写盘

依赖: 无(纯 Python + pydantic)
"""
import json
import os
import tempfile
import threading
import time
from pathlib import Path

from agent_core.memory import DistillationConfig, DistillationScheduler

tmp = Path(tempfile.mkdtemp())
memory_root = tmp / "memory"
memory_root.mkdir()
logs_dir = tmp / "logs"
logs_dir.mkdir()


def make_sessions(n: int) -> None:
    """在 logs_dir 里造 n 个 session 文件"""
    for i in range(n):
        (logs_dir / f"s{i}.jsonl").write_text(
            json.dumps({"user_msg": f"用户消息 {i}", "assistant_resp": f"回复 {i}"})
        )


def mock_llm_ok(prompt: str) -> str:
    """合法 JSON 响应的 mock LLM"""
    return json.dumps([
        {
            "type": "user",
            "title": "偏好 Vite",
            "why": "用户多次明确",
            "body": "项目用 Vite 不用 CRA",
            "confidence": 0.8,
            "sources": ["s1"],
            "tags": ["preference"],
        },
        {
            "type": "feedback",
            "title": "重视底层原理",
            "why": "学习风格",
            "body": "用户偏好先理解原理再看 API",
            "confidence": 0.7,
            "sources": ["s1"],
            "tags": ["learning"],
        },
    ])


# ─── Demo 1: 门0 gate_disabled ───
print("=== Demo 1: 门0 — gate_disabled ===")
config = DistillationConfig(enabled=False)
s = DistillationScheduler(memory_root, config, llm_callback=mock_llm_ok)
ok, reason = s.should_distill()
assert ok is False
assert reason == "gate_disabled"
print(f"  ✅ reason={reason}")

# ─── Demo 2: 门1 too_soon(.last-distill 刚 touch) ───
print("=== Demo 2: 门1 — too_soon ===")
config = DistillationConfig()
s = DistillationScheduler(memory_root, config, llm_callback=mock_llm_ok)
# touch .last-distill(mtime = now,< 24h)
(memory_root / ".last-distill").touch()
ok, reason = s.should_distill()
assert ok is False
assert "too_soon" in reason
print(f"  ✅ reason={reason}")
# 清掉再继续
(memory_root / ".last-distill").unlink()

# ─── Demo 3: 锁原子性 (10 线程并发) ───
print("=== Demo 3: 锁原子性 — 10 线程并发 acquire ===")
s = DistillationScheduler(memory_root, config, llm_callback=mock_llm_ok)
results = []
barrier = threading.Barrier(10)
mutex = threading.Lock()

def attempt():
    barrier.wait()
    prior = s._acquire_lock()
    with mutex:
        results.append(prior)

threads = [threading.Thread(target=attempt) for _ in range(10)]
for t in threads:
    t.start()
for t in threads:
    t.join()

winners = [r for r in results if r >= 0]
losers = [r for r in results if r == s.LOCK_TAKEN]
assert len(winners) == 1, f"应 1 个赢家,实际 {len(winners)}"
assert len(losers) == 9, f"应 9 个 loser,实际 {len(losers)}"
print(f"  ✅ winners={len(winners)}, losers={len(losers)}")
# 清理锁文件
s._release_lock(winners[0], success=False)

# ─── Demo 4: 失败回滚(mock LLM 抛异常 → mtime 不变) ───
print("=== Demo 4: 失败回滚 — mock LLM 异常 ===")
def mock_llm_explode(prompt: str) -> str:
    raise RuntimeError("LLM 调用失败(模拟)")

# 设置 .last-distill 25h 前 + 6 个 session
mtime = memory_root / ".last-distill"
mtime.touch()
prior_time = time.time() - 25 * 3600
os.utime(mtime, (prior_time, prior_time))
make_sessions(6)

s = DistillationScheduler(memory_root, config, llm_callback=mock_llm_explode)
result = s.run(dry_run=True)
assert result.success is False
assert "RuntimeError" in result.error or "LLM" in result.error

# 验证 .last-distill mtime 未推进
actual_mtime = mtime.stat().st_mtime
assert abs(actual_mtime - prior_time) < 1.0, f"mtime 被推进: {actual_mtime} != {prior_time}"
# 锁文件应被清理
assert not (memory_root / ".consolidate-lock").exists()
print(f"  ✅ error={result.error[:50]}..., mtime 未变")

# ─── Demo 5: 端到端 dry_run(2 个候选产出,不写盘) ───
print("=== Demo 5: 端到端 — dry_run 路径 ===")
# 重置: 让 .last-distill 仍是 25h 前
mtime.touch()
os.utime(mtime, (prior_time, prior_time))

s = DistillationScheduler(memory_root, config, llm_callback=mock_llm_ok)
result = s.run(dry_run=True)
assert result.success is True, f"期望 success,实际 skip_reason={result.skip_reason}"
assert len(result.candidates) == 2
assert result.candidates[0]["title"] == "偏好 Vite"
assert result.candidates_written == []  # dry_run 不写
assert not (memory_root / "_candidate").exists()
print(f"  ✅ candidates={len(result.candidates)}, sessions={result.sessions_processed}")

# ─── Bonus Demo: 真写盘路径(dry_run=False) ───
print("=== Bonus: dry_run=False 真写盘 ===")
mtime.touch()
os.utime(mtime, (prior_time, prior_time))

s = DistillationScheduler(memory_root, config, llm_callback=mock_llm_ok)
result = s.run(dry_run=False)
assert result.success
assert len(result.candidates_written) == 2
user_files = list((memory_root / "_candidate" / "user").glob("*.md"))
feedback_files = list((memory_root / "_candidate" / "feedback").glob("*.md"))
assert len(user_files) == 1
assert len(feedback_files) == 1
print(f"  ✅ wrote {len(result.candidates_written)} files:")
for p in result.candidates_written:
    print(f"     - {p.relative_to(memory_root)}")

import shutil
shutil.rmtree(tmp, ignore_errors=True)
print()
print("=== M5 / Day 5 demo 端到端 5/5 通过(超出 plan 3 个最低要求)===")
PYEOF

echo
echo "=== M5 测试套件 ==="
.venv/bin/python -m pytest tests/test_distiller.py -v 2>&1 | tail -5

echo
echo "=== M5 验收完成 ==="
echo "提示: M5 = 蒸馏骨架 + 锁 v2.1,真实 LLM 调用与 UI diff/merge review 归 M7。"