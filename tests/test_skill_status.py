"""tests/test_skill_status.py — format_skill_status + scripts/skills_check.py CLI（T054, T061, FR-022）"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from agent_core.skills.config import SkillsConfig
from agent_core.skills.registry import SkillsRegistry
from agent_core.skills.status import format_skill_status


def _write(root: Path, name: str, body: str = "---\ndescription: d\n---\nb\n") -> Path:
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(body, encoding="utf-8")
    return d


def _registry(tmp_path, **skills):
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    for name, body in skills.items():
        _write(ws, name, body)
    bundled = tmp_path / "b"
    bundled.mkdir(exist_ok=True)
    cfg = SkillsConfig.from_dict({
        "paths": {"workspace_dir": str(ws), "bundled_dir": str(bundled)}
    })
    return SkillsRegistry(cfg)


class TestFormatSkillStatus:
    def test_empty_registry(self, tmp_path):
        reg = _registry(tmp_path)
        out = format_skill_status(reg)
        assert "total=0" in out
        assert "无 skill" in out

    def test_eligible_skill_listed(self, tmp_path):
        reg = _registry(tmp_path, hello="---\nname: hello\ndescription: greet\n---\nb\n")
        out = format_skill_status(reg)
        assert "hello" in out
        assert "eligible" in out
        assert "total=1" in out
        assert "eligible=1" in out

    def test_errored_skill_with_load_error(self, tmp_path):
        reg = _registry(tmp_path, broken="---\nname: broken\n---\nb\n")  # 缺 description
        out = format_skill_status(reg)
        assert "broken" in out
        assert "errored" in out
        assert "errored=1" in out

    def test_hidden_skill_flagged(self, tmp_path):
        reg = _registry(
            tmp_path,
            secret="---\nname: secret\ndescription: d\ndisable-model-invocation: true\n---\nb\n",
        )
        out = format_skill_status(reg)
        assert "secret" in out
        assert "hidden_from_model" in out
        assert "hidden_from_model=1" in out

    def test_missing_requirements_detail(self, tmp_path):
        reg = _registry(
            tmp_path,
            needkey="---\nname: needkey\ndescription: d\nmetadata:\n  requires:\n    env: [NEED_X]\n---\nb\n",
        )
        out = format_skill_status(reg)
        assert "needkey" in out
        assert "missing" in out
        assert "env:NEED_X" in out

    def test_user_invocable_false_flagged(self, tmp_path):
        reg = _registry(
            tmp_path,
            noslash="---\nname: noslash\ndescription: d\nuser-invocable: false\n---\nb\n",
        )
        out = format_skill_status(reg)
        assert "noslash" in out
        assert "no-slash" in out

    def test_body_references_listed(self, tmp_path):
        # T064：body 引用解析后的绝对路径在 status 列出（作者诊断用）
        reg = _registry(
            tmp_path,
            myref=(
                "---\nname: myref\ndescription: d\n---\n"
                "see references/cheat.md and scripts/run.sh\n"
            ),
        )
        out = format_skill_status(reg)
        assert "myref" in out
        assert "refs=2" in out
        # 解析后的绝对路径出现（作者据此校验资产是否齐全）
        assert "references/cheat.md" in out
        assert "scripts/run.sh" in out

    def test_no_refs_no_extra_lines(self, tmp_path):
        # 无 body 引用的 skill 不出现 refs 标记 / 不多行
        reg = _registry(tmp_path, plain="---\nname: plain\ndescription: d\n---\nb\n")
        out = format_skill_status(reg)
        assert "refs=" not in out
        assert "└─" not in out


class TestSkillsCheckCLI:  # T061 — scripts/skills_check.py subprocess
    """FR-022 用户可调用 CLI：subprocess 入口 + 退出码 + 输出分类"""

    @pytest.fixture
    def skills_check_path(self) -> Path:
        return Path(__file__).resolve().parent.parent / "scripts" / "skills_check.py"

    def test_script_exists(self, skills_check_path):
        assert skills_check_path.is_file(), f"skills_check.py 不存在: {skills_check_path}"

    def test_exit_zero_and_categories(self, tmp_path, skills_check_path):
        ws = tmp_path / "ws"; ws.mkdir()
        bundled = tmp_path / "b"; bundled.mkdir()
        _write(ws, "ok", "---\nname: ok\ndescription: d\n---\nb\n")
        _write(ws, "bad", "---\nname: bad\n---\nb\n")  # 缺 desc → errored

        env = {
            "PATH": __import__("os").environ.get("PATH", ""),
            "SKILLS_PATHS__WORKSPACE_DIR": str(ws),
            "SKILLS_PATHS__BUNDLED_DIR": str(bundled),
        }
        result = subprocess.run(
            [sys.executable, str(skills_check_path)],
            capture_output=True, text=True, env=env, timeout=30,
        )
        assert result.returncode == 0, f"stderr={result.stderr}"
        out = result.stdout
        assert "Skills status" in out
        assert "ok" in out
        assert "bad" in out
        assert "errored" in out  # 分类出现

    def test_handles_empty_config(self, tmp_path, skills_check_path):
        # 空目录配置 → 不崩，退出 0
        env = {
            "PATH": __import__("os").environ.get("PATH", ""),
            "SKILLS_PATHS__WORKSPACE_DIR": str(tmp_path / "empty"),
            "SKILLS_PATHS__BUNDLED_DIR": str(tmp_path / "empty2"),
        }
        result = subprocess.run(
            [sys.executable, str(skills_check_path)],
            capture_output=True, text=True, env=env, timeout=30,
        )
        assert result.returncode == 0
        assert "total=0" in result.stdout
