"""
T025 — tests/test_skill_secrets_audit.py

002-skill-secret-injection: verify SC-005 audit script 端到端可用。

覆盖(2 用例):
1. happy path: subprocess.run audit → exit 0 + stdout 含 "Audit PASSED"
2. negative path: 手工污染 prompt 产物含 sentinel → audit exit 1 + "Audit FAILED"

后者通过显式给 --config / --skills-workspace 路径实现(产物落到 caller 控制目录),
然后单独对其中一个 prompt_run_*.txt 注入 sentinel 字面值, 再调 audit 验证它能抓。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
AUDIT_SCRIPT = REPO_ROOT / "scripts" / "verify_skill_secrets_audit.py"


def _run_audit(*args: str, timeout: int = 30) -> subprocess.CompletedProcess:
    """调 audit script, 返 CompletedProcess (stdout/stderr 都拿)。"""
    return subprocess.run(
        [sys.executable, str(AUDIT_SCRIPT), *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd=str(REPO_ROOT),
    )


def test_audit_script_exits_zero_and_prints_passed(tmp_path):
    """happy path: audit 默认参数 → exit 0 + stdout 含 'Audit PASSED'。

    SC-005 (audit script 实际可用) 的 happy path 验收。
    """
    result = _run_audit(
        "--run-count", "2",
        "--config", str(tmp_path / "config.yaml"),
        "--skills-workspace", str(tmp_path / "workspace"),
    )

    assert result.returncode == 0, (
        f"audit 应 exit 0, got {result.returncode}\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    assert "Audit PASSED" in result.stdout, (
        f"stdout 应含 'Audit PASSED', got:\n{result.stdout}"
    )


def test_audit_script_detects_sentinel_in_prompt_artifact(tmp_path):
    """negative path: 手工污染 prompt 产物含 sentinel → audit exit 1 + 'Audit FAILED'。

    验证 audit script 的检测能力(任一产物含 sentinel 即 fail, 不止 happy path)。
    """
    # 1. 先跑一次 audit, 让产物落到 tmp_path/workspace
    workspace = tmp_path / "workspace"
    config_path = tmp_path / "config.yaml"
    result1 = _run_audit(
        "--run-count", "1",
        "--config", str(config_path),
        "--skills-workspace", str(workspace),
    )
    assert result1.returncode == 0, (
        f"首次 audit 应 PASS, got {result1.returncode}\n"
        f"stdout:\n{result1.stdout}"
    )

    # 2. 找到 prompt_run_1.txt, 手工注入 sentinel (模拟产物污染)
    prompt_path = workspace / "prompt_run_1.txt"
    assert prompt_path.is_file(), f"prompt_run_1.txt 应存在, 目录内容: {list(workspace.iterdir())}"
    original = prompt_path.read_text(encoding="utf-8")
    polluted = original + "\nPOLLUTED: audit_sentinel_value_8f3a2b91c4d5e6f7a8b9c0d1e2f3a4b5\n"
    prompt_path.write_text(polluted, encoding="utf-8")

    # 3. 再跑 audit (用同样的 run_count=1, 它会**覆盖**新 prompt_run_1.txt 而不是 append)
    # 注意: simulate run 内部每次都新写 prompt_run_{i+1}.txt, 但 ——
    # 第 i 次 run 调 SkillsPromptHandler.handle, 渲染新 prompt (不含 sentinel)。
    # 我们污染的是旧的 prompt_run_1.txt, 但下次 audit run_count=1 时仍会**新写**一次
    # prompt_run_1.txt, 覆盖我们的污染。
    #
    # 因此:本测试需要换策略 — 污染 agent.log (它只在 run 末尾 close 时被 flush,
    # 后续 run 不会清掉它, 只会 append)。
    # 重置: 先用 polluted prompt_run_1.txt 跑 run_count=0 (无效参数),
    # 改用污染 agent.log 路径。

    # 改用 agent.log 路径污染 (它跨 run 累积)
    log_file = workspace / "agent.log"
    assert log_file.is_file(), f"agent.log 应存在"
    log_file.write_text(
        log_file.read_text(encoding="utf-8") +
        "\nPOLLUTED: audit_sentinel_value_8f3a2b91c4d5e6f7a8b9c0d1e2f3a4b5\n",
        encoding="utf-8",
    )

    # 4. 再跑 audit (run_count=1)
    result2 = _run_audit(
        "--run-count", "1",
        "--config", str(config_path),
        "--skills-workspace", str(workspace),
    )

    # 5. 验证 audit 检测到泄漏 → exit 1 + "Audit FAILED"
    assert result2.returncode == 1, (
        f"污染 agent.log 后 audit 应 exit 1, got {result2.returncode}\n"
        f"stdout:\n{result2.stdout}\nstderr:\n{result2.stderr}"
    )
    assert "Audit FAILED" in result2.stdout, (
        f"stdout 应含 'Audit FAILED', got:\n{result2.stdout}"
    )
    assert "agent.log" in result2.stdout, (
        f"stdout 应指明泄漏在 agent.log, got:\n{result2.stdout}"
    )