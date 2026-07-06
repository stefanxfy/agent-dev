"""
T006 / T040 — tests/test_skill_config_entries.py

002-skill-secret-injection (US1): SkillsConfig.entries 校验 + load_user_config 加载路径。

用例覆盖(Constitution IV hard gate):
- T006 (Phase 2 Foundational):
  1. unknown skill name → ConfigValidationError(FR-005)
  2. secret 不在 requires.env → ConfigValidationError(FR-006)
  3. config 文件不存在 → SkillsConfig.entries=None(legacy mode)
- T040 (US1 wiring):
  4. AGENT_CONFIG_PATH 指向合法 yaml → entries 加载
  5. env var 未设 + 默认 ~/.agent_data/config.yaml 存在 → 加载
  6. 默认文件不存在 → 返 default 不报错
  7. AGENT_CONFIG_PATH 指向不存在文件 → raise ConfigValidationError
  8. chmod != 600 → log warning 不 raise
"""

from __future__ import annotations

import logging
import os
import stat
from pathlib import Path
from unittest.mock import patch

import pytest

from agent_core.skills.config import SkillsConfig
from agent_core.skills.env_overrides import (
    ConfigValidationError,
    SecretRef,
    SecretRefKind,
    SkillEntryConfig,
    load_user_config,
)
from agent_core.skills.registry import SkillsRegistry


# ──────────────────────────────────────────────────────────────────
# Helpers: 创建临时 SKILL.md 用于 SkillsRegistry 加载
# ──────────────────────────────────────────────────────────────────

def _write_skill(workspace: Path, name: str, env_requires: list[str] | None = None) -> Path:
    """在 workspace 下创建 name/SKILL.md, 返回 skill_dir。"""
    skill_dir = workspace / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    env_block = ""
    if env_requires is not None:
        env_block = f"  requires:\n    env: {env_requires!r}\n"
    skill_dir.joinpath("SKILL.md").write_text(
        f"---\n"
        f"name: {name}\n"
        f"description: test skill {name}\n"
        f"metadata:\n"
        f"{env_block}"
        f"---\n"
        f"body\n",
        encoding="utf-8",
    )
    return skill_dir


def _build_cfg_with_workspace(workspace: Path) -> SkillsConfig:
    """用临时 workspace_dir 构造 SkillsConfig(覆盖默认 ~/.agent_data/skills)。"""
    return SkillsConfig.from_dict({"paths": {"workspace_dir": str(workspace)}})


# ──────────────────────────────────────────────────────────────────
# T006 用例 — entries 校验
# ──────────────────────────────────────────────────────────────────

def test_unknown_skill_name_raises(tmp_path: Path):
    """Case A: entries 含未加载 skill 名 → ConfigValidationError(FR-005)。"""
    workspace = tmp_path / "skills"
    workspace.mkdir()
    _write_skill(workspace, "echo-skill")

    cfg = _build_cfg_with_workspace(workspace)
    cfg.entries = {
        "does-not-exist": SkillEntryConfig(
            secrets={"X": SecretRef(SecretRefKind.INLINE, "v")},
        ),
    }
    reg = SkillsRegistry(cfg)
    with pytest.raises(ConfigValidationError) as exc_info:
        reg.snapshot()
    assert "does-not-exist" in str(exc_info.value)


def test_extra_secret_not_in_requires_env_raises(tmp_path: Path):
    """Case B: entries.secrets 含不在 requires.env 的 key → ConfigValidationError(FR-006)。"""
    workspace = tmp_path / "skills"
    workspace.mkdir()
    _write_skill(workspace, "strict-skill", env_requires=["X"])

    cfg = _build_cfg_with_workspace(workspace)
    cfg.entries = {
        "strict-skill": SkillEntryConfig(
            secrets={
                "X": SecretRef(SecretRefKind.INLINE, "x_val"),
                "EXTRA": SecretRef(SecretRefKind.INLINE, "extra_val"),
            },
        ),
    }
    reg = SkillsRegistry(cfg)
    with pytest.raises(ConfigValidationError) as exc_info:
        reg.snapshot()
    msg = str(exc_info.value)
    assert "EXTRA" in msg
    assert "strict-skill" in msg


def test_from_yaml_missing_file_returns_legacy(tmp_path: Path):
    """Case C: from_yaml 文件不存在 → 返 SkillsConfig(entries=None), 不抛。"""
    missing = tmp_path / "no-such-config.yaml"
    cfg = SkillsConfig.from_yaml(missing)
    assert cfg.entries is None  # legacy mode


# ──────────────────────────────────────────────────────────────────
# T040 用例 — load_user_config 加载路径
# ──────────────────────────────────────────────────────────────────

def test_load_user_config_from_agenv_path(tmp_path: Path, monkeypatch):
    """AGENT_CONFIG_PATH 指向合法 yaml → entries 加载到 default 上。"""
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        "skills:\n"
        "  entries:\n"
        "    my-skill:\n"
        "      secrets:\n"
        "        X: 'inline_val_xxx'\n",
        encoding="utf-8",
    )
    config_file.chmod(0o600)
    monkeypatch.setenv("AGENT_CONFIG_PATH", str(config_file))

    default = SkillsConfig()
    result = load_user_config(default)
    assert result is default
    assert result.entries is not None
    assert "my-skill" in result.entries
    assert result.entries["my-skill"].secrets["X"].value == "inline_val_xxx"


def test_load_user_config_default_file_exists(tmp_path: Path, monkeypatch):
    """env 未设 + 默认 ~/.agent_data/config.yaml 存在 → 加载。"""
    monkeypatch.delenv("AGENT_CONFIG_PATH", raising=False)
    # monkeypatch Path.home → tmp_path(让 default path 落到 tmp_path/.agent_data/config.yaml)
    fake_home = tmp_path
    agent_data = fake_home / ".agent_data"
    agent_data.mkdir()
    config_file = agent_data / "config.yaml"
    config_file.write_text(
        "skills:\n"
        "  entries:\n"
        "    default-skill:\n"
        "      secrets:\n"
        "        X: 'val'\n",
        encoding="utf-8",
    )
    config_file.chmod(0o600)

    with patch("agent_core.skills.env_overrides.Path.home", return_value=fake_home):
        default = SkillsConfig()
        result = load_user_config(default)

    assert result.entries is not None
    assert "default-skill" in result.entries


def test_load_user_config_no_file_returns_default(tmp_path: Path, monkeypatch):
    """默认文件不存在 → 返 default(entries=None), 不报错。"""
    monkeypatch.delenv("AGENT_CONFIG_PATH", raising=False)
    fake_home = tmp_path  # .agent_data/ 不存在
    with patch("agent_core.skills.env_overrides.Path.home", return_value=fake_home):
        default = SkillsConfig()
        result = load_user_config(default)
    assert result is default
    assert result.entries is None


def test_load_user_config_agenv_points_to_missing_file_raises(tmp_path: Path, monkeypatch):
    """AGENT_CONFIG_PATH 指向不存在文件 → raise ConfigValidationError(fail-fast)。"""
    monkeypatch.setenv("AGENT_CONFIG_PATH", str(tmp_path / "missing.yaml"))
    default = SkillsConfig()
    with pytest.raises(ConfigValidationError) as exc_info:
        load_user_config(default)
    assert "AGENT_CONFIG_PATH" in str(exc_info.value)


def test_load_user_config_warns_on_bad_permissions(tmp_path: Path, caplog, monkeypatch):
    """config 文件 chmod != 600 → log warning(caplog 验证), 不 raise。"""
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        "skills:\n  entries:\n    s:\n      secrets:\n        X: 'v'\n",
        encoding="utf-8",
    )
    # chmod 644 → group/other 可读 → 应触发 warning
    config_file.chmod(0o644)
    monkeypatch.setenv("AGENT_CONFIG_PATH", str(config_file))

    # 确保有可读权限在 POSIX 系统上有效
    mode = stat.S_IMODE(config_file.stat().st_mode)
    if not (mode & stat.S_IRWXG or mode & stat.S_IRWXO):
        pytest.skip("platform does not honor chmod group/other bits")

    default = SkillsConfig()
    with caplog.at_level(logging.WARNING, logger="agent_core.skills.env_overrides"):
        result = load_user_config(default)
    assert result is default
    # 至少一条 WARNING, 内容含 mode / chmod 字样
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert any("chmod" in r.getMessage() or "mode" in r.getMessage() for r in warnings)