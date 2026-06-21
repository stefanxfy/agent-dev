#!/usr/bin/env bash
# M4 / Day 4 验收 demo —— L3 会话内压缩 (SessionMemoryLayer)
# 跑法：bash scripts/demo_m4.sh   （无需参数）
# 前置：.venv/bin/python 已装好 pydantic / pyyaml
#       无需 bge-m3 / chromadb（M4 是纯文件层逻辑,不依赖向量库）
set -euo pipefail

cd "$(dirname "$0")/.."

echo "=== M4 / Day 4 验收开始 ==="
echo

# Demo 1-3: 端到端 SM 初始化 / 触发 / 压缩
.venv/bin/python <<'PYEOF'
"""
M4 / Day 4 验收 — 3 个核心 demo + 1 个回归守护

Demo 1: SM 生命周期 (init → fill → is_template=False)
Demo 2: should_trigger_compact 触发决策
Demo 3: compact() 产出 summary + kept_messages + used_tokens
回归:   _estimate_messages_tokens 累积 bug 已修(used_tokens > 0)
"""
import tempfile
from pathlib import Path

from agent_core.memory import MemoryConfig, SessionMemoryLayer
from agent_core.memory.sm_layer import TurnContext

tmp = Path(tempfile.mkdtemp())
sm_path = tmp / "sm.md"
config = MemoryConfig().compact

# ─── Demo 1: SM 生命周期 ───
print("=== Demo 1: SM 生命周期 ===")
sm = SessionMemoryLayer("demo_s2", sm_path, config)
assert not sm.sm_exists()
assert sm.sm_is_template()  # 初始化前判定为 template

sm.write_sm_template()
assert sm.sm_exists()
print(f"  write_sm_template → exists={sm.sm_exists()}, is_template={sm.sm_is_template()}")

# 模拟 LLM extract 跑过一次,填了实质内容
content = sm.read_sm()
content = content.replace(
    "<!-- 当前会话目标、约束、已知事实 -->",
    "用户学习 React,目标 6 周完成项目",
)
content = content.replace(
    "<!-- 已做的决策(用户偏好 + 系统决策) -->",
    "1. 用 Vite 不用 CRA\n2. TypeScript strict",
)
sm_path.write_text(content, encoding="utf-8")
assert not sm.sm_is_template()
print(f"  填内容后 → is_template={sm.sm_is_template()}")

# ─── Demo 2: 触发决策 ───
print("=== Demo 2: should_trigger_compact 触发决策 ===")
ctx = TurnContext(
    messages=[{"id": "m1", "role": "user", "content": "x" * 4000}],
    total_tokens=12000,  # > 10000 阈值
    tool_count=2,
)
decision = sm.should_trigger_compact(ctx)
print(f"  decision: strategy={decision.strategy}, reason={decision.reason}")
assert decision.strategy == "sm_compact", f"应触发 SM-compact,实际 = {decision.strategy}"

# ─── Demo 3: compact() 产出 ───
print("=== Demo 3: compact() 产出 summary + kept_messages ===")
messages = [
    {"id": f"m{i}", "role": "user" if i % 2 == 0 else "assistant", "content": f"msg{i}" * 50}
    for i in range(5)
]
result = sm.compact(messages, context_window=128000)
assert result is not None
assert result.strategy == "sm_compact"
assert result.used_tokens_estimate > 0
assert "用户学习 React" in result.summary_message["content"]
assert "Vite" in result.summary_message["content"]
print(f"  ✅ compacted: kept={len(result.kept_messages)}, used_tokens={result.used_tokens_estimate}")
print(f"  summary 前 80 字: {result.summary_message['content'][:80]}...")

# ─── 回归守护: _estimate_messages_tokens 累积 bug ───
print("=== 回归守护: used_tokens_estimate 累积 bug ===")
# 5 条 400 字符英文消息 — 旧实现下 ≈ -60,新实现下 > 0
long_msgs = [
    {"id": f"m{i}", "role": "user", "content": "x" * 400}
    for i in range(5)
]
result2 = sm.compact(long_msgs, context_window=128000)
assert result2.used_tokens_estimate > 0, (
    f"❌ 回归!used_tokens_estimate 应该为正,实际 = {result2.used_tokens_estimate}"
)
print(f"  ✅ used_tokens_estimate = {result2.used_tokens_estimate} (修复前: -60)")

import shutil
shutil.rmtree(tmp, ignore_errors=True)
print()
print("=== M4 / Day 4 demo 端到端 4/4 通过 ===")
PYEOF

echo
echo "=== M4 测试套件 ==="
.venv/bin/python -m pytest tests/test_sm_layer.py -v 2>&1 | tail -5

echo
echo "=== M4 验收完成 ==="
echo "提示: M4 范围 = 模块 + 测试,集成到对话流不在 M4,归 M7 (Day 7)"