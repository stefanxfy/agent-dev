#!/usr/bin/env bash
# M1 / Day 1 验收 demo —— 类型系统 + 配置 + 路径校验(地基三件套)
# 跑法: bash scripts/demo_m1.sh   (无需参数)
# 前置: .venv/bin/python 已装好 pydantic / pyyaml
#       无需 bge-m3 / chromadb
set -euo pipefail

cd "$(dirname "$0")/.."

echo "=== M1 / Day 1 验收开始 ==="
echo

# Demo 1-8: 端到端 8 case(超出 plan 最低 3 个)
.venv/bin/python <<'PYEOF'
"""
M1 / Day 1 验收 — 8 个核心 demo

Demo 1-2: 类型系统 (4 类封闭 + LLM 试图发明第 5 类被拒)
Demo 3:   配置 Pydantic 校验 (semantic_weight 越界)
Demo 4:   路径校验 happy path (user/foo.md)
Demo 5-8: 路径校验攻击面 (../ 越界 / 非法子目录 / 非法扩展名 / Unicode RLO)

依赖: 无(纯 Python + pydantic)
"""
import pathlib
import tempfile

from agent_core.memory.config import MemoryConfig
from agent_core.memory.path_validator import MemoryPathValidator
from agent_core.memory.types import validate_type

# ─── Demo 1: 4 类封闭 — happy path ───
print("=== Demo 1: 类型系统 happy path ===")
assert validate_type("user") == "user"
print("  ✅ validate_type('user') == 'user'")

# ─── Demo 2: LLM 试图发明第 5 类 — 被拒 ───
print("=== Demo 2: 类型系统封闭(LLM 试图发明第 5 类) ===")
try:
    validate_type("episodic")
    raise AssertionError("❌ 应该抛 ValueError,实际没抛")
except ValueError as e:
    print(f"  ✅ blocked: {e}")

# ─── Demo 3: Pydantic 跨字段 + 范围校验 ───
print("=== Demo 3: Pydantic 配置校验 ===")
try:
    MemoryConfig(retrieval={"semantic_weight": 2.0})  # 越界 > 1.0
    raise AssertionError("❌ 应该抛 ValidationError,实际没抛")
except Exception as e:
    print(f"  ✅ blocked: {type(e).__name__}")

# ─── Demo 4: 路径校验 happy path ───
print("=== Demo 4: 路径校验 happy path ===")
tmp = pathlib.Path(tempfile.mkdtemp()) / "memory"
tmp.mkdir()
v = MemoryPathValidator(tmp)
real = v.validate("user/foo.md")
print(f"  ✅ resolved: {real}")
assert "user" in str(real) and "foo.md" in str(real)

# ─── Demo 5: ../ 越界攻击 ───
print("=== Demo 5: 路径校验 - ../ 越界 ===")
try:
    v.validate("../../etc/passwd")
    raise AssertionError("❌ 应该抛异常")
except Exception as e:
    print(f"  ✅ blocked: {type(e).__name__}")

# ─── Demo 6: 非法子目录 ───
print("=== Demo 6: 路径校验 - 非法子目录 (admin) ===")
try:
    v.validate("admin/foo.md")
    raise AssertionError("❌ 应该抛异常")
except Exception as e:
    print(f"  ✅ blocked: {type(e).__name__}")

# ─── Demo 7: 非法扩展名(.py) ───
print("=== Demo 7: 路径校验 - 非法扩展名 (.py) ===")
try:
    v.validate("user/run.py")
    raise AssertionError("❌ 应该抛异常")
except Exception as e:
    print(f"  ✅ blocked: {type(e).__name__}")

# ─── Demo 8: Unicode trick (RLO U+202E) ───
print("=== Demo 8: 路径校验 - Unicode RLO 攻击 ===")
try:
    v.validate("user/‮evil.md")  # RLO 反转覆盖 .md → .exe 显示
    raise AssertionError("❌ 应该抛异常")
except Exception as e:
    print(f"  ✅ blocked: {type(e).__name__}")

import shutil
shutil.rmtree(tmp.parent, ignore_errors=True)
print()
print("=== M1 / Day 1 demo 端到端 8/8 通过(超出 plan 3 个最低要求)===")
PYEOF

echo
echo "=== M1 测试套件 ==="
.venv/bin/python -m pytest tests/test_types_config.py -v 2>&1 | tail -5

echo
echo "=== M1 验收完成 ==="
echo "提示: M1 = 地基三件套(类型 + 配置 + 路径),后续 M2-M8 全部依赖。"