#!/usr/bin/env python3
"""skills_check — agent_core skill 系统诊断 CLI（FR-022 用户可调用面；Polish T060）。

用法：
    python3 scripts/skills_check.py

从 SKILLS_* 环境变量加载配置（双下划线表嵌套，如 SKILLS_PATHS__WORKSPACE_DIR），
print format_skill_status(registry)：每 skill 的 source/eligibility/visibility/missing。

非 UI、可单测（tests/test_skill_status.py::TestSkillsCheckCLI）；退出码恒 0
（诊断工具，即使有 errored skill 也不 panic）。
"""
from __future__ import annotations

import sys
from pathlib import Path

# 让 `python3 scripts/skills_check.py` 能 import agent_core（脚本不在包内）
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent_core.skills.config import SkillsConfig          # noqa: E402
from agent_core.skills.registry import SkillsRegistry      # noqa: E402
from agent_core.skills.status import format_skill_status   # noqa: E402


def main() -> int:
    cfg = SkillsConfig.from_env()
    registry = SkillsRegistry(cfg)
    print(format_skill_status(registry))
    return 0


if __name__ == "__main__":
    sys.exit(main())
