"""tests/test_skill_config.py — SkillsConfig loaders（T009）"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from agent_core.skills.config import SkillsConfig
from agent_core.skills.types import (
    DEFAULT_MAX_SKILL_FILE_BYTES,
    DEFAULT_MAX_SKILLS_IN_PROMPT,
    DEFAULT_MAX_SKILLS_PROMPT_CHARS,
)


class TestDefaults:
    def test_default_config_constructs(self):
        cfg = SkillsConfig()
        assert cfg.limits.max_skill_file_bytes == DEFAULT_MAX_SKILL_FILE_BYTES
        assert cfg.limits.max_skills_in_prompt == DEFAULT_MAX_SKILLS_IN_PROMPT
        assert cfg.limits.max_skills_prompt_chars == DEFAULT_MAX_SKILLS_PROMPT_CHARS
        assert cfg.load.enabled is True

    def test_bundled_dir_points_to_package_builtin(self):
        cfg = SkillsConfig()
        # __file__-relative：必须指向 agent_core/skills/builtin
        assert cfg.paths.bundled_dir.name == "builtin"
        assert cfg.paths.bundled_dir.is_dir()  # 包内确实有此目录

    def test_workspace_dir_default_in_project(self, tmp_path, monkeypatch):
        # 项目根（有 .git 标记）→ cwd/skills（plan.md:23，镜像 session/storage.py:88-93）
        monkeypatch.chdir(tmp_path)
        (tmp_path / ".git").mkdir()
        cfg = SkillsConfig()
        assert cfg.paths.workspace_dir == tmp_path / "skills"

    def test_workspace_dir_default_fallback_home(self, tmp_path, monkeypatch):
        # 非项目 cwd（无 .git / agent_core）→ 回退 ~/.agent_data/skills
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("HOME", str(tmp_path / "fakehome"))
        monkeypatch.delenv("AGENT_DATA_DIR", raising=False)
        cfg = SkillsConfig()
        assert cfg.paths.workspace_dir == tmp_path / "fakehome" / ".agent_data" / "skills"

    def test_workspace_dir_default_agent_data_dir_override(self, tmp_path, monkeypatch):
        # AGENT_DATA_DIR 覆盖家目录基
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("AGENT_DATA_DIR", str(tmp_path / "customdata"))
        cfg = SkillsConfig()
        assert cfg.paths.workspace_dir == tmp_path / "customdata" / "skills"

    def test_workspace_dir_default_project_marker_agent_core(self, tmp_path, monkeypatch):
        # agent_core/ 标记也算项目根
        monkeypatch.chdir(tmp_path)
        (tmp_path / "agent_core").mkdir()
        cfg = SkillsConfig()
        assert cfg.paths.workspace_dir == tmp_path / "skills"


class TestFromDict:
    def test_override_limits(self):
        cfg = SkillsConfig.from_dict(
            {"limits": {"max_skills_prompt_chars": 5000}}
        )
        assert cfg.limits.max_skills_prompt_chars == 5000
        # 未覆盖的走默认
        assert cfg.limits.max_skill_file_bytes == DEFAULT_MAX_SKILL_FILE_BYTES

    def test_override_paths(self):
        cfg = SkillsConfig.from_dict(
            {"paths": {"workspace_dir": "/tmp/my-skills"}}
        )
        assert cfg.paths.workspace_dir == Path("/tmp/my-skills")

    def test_extra_forbidden(self):
        with pytest.raises(Exception):
            SkillsConfig.from_dict({"unknown_section": "x"})


class TestFromEnv:
    def test_env_overrides_limits(self, monkeypatch):
        monkeypatch.setenv("SKILLS_LIMITS__MAX_SKILLS_PROMPT_CHARS", "9000")
        cfg = SkillsConfig.from_env()
        assert cfg.limits.max_skills_prompt_chars == 9000

    def test_env_overrides_workspace_dir(self, monkeypatch, tmp_path):
        monkeypatch.setenv("SKILLS_PATHS__WORKSPACE_DIR", str(tmp_path))
        cfg = SkillsConfig.from_env()
        assert cfg.paths.workspace_dir == tmp_path

    def test_env_disabled_flag(self, monkeypatch):
        monkeypatch.setenv("SKILLS_LOAD__ENABLED", "false")
        cfg = SkillsConfig.from_env()
        assert cfg.load.enabled is False

    def test_env_bool_true(self, monkeypatch):
        monkeypatch.setenv("SKILLS_LOAD__ENABLED", "true")
        cfg = SkillsConfig.from_env()
        assert cfg.load.enabled is True

    def test_no_env_returns_defaults(self, monkeypatch):
        # 清掉所有 SKILLS_ 变量
        for k in list(os.environ):
            if k.startswith("SKILLS_"):
                monkeypatch.delenv(k, raising=False)
        cfg = SkillsConfig.from_env()
        assert cfg.limits.max_skills_in_prompt == DEFAULT_MAX_SKILLS_IN_PROMPT


class TestExpanduser:
    def test_workspace_dir_expanduser(self, monkeypatch):
        monkeypatch.setenv("HOME", "/tmp/fakehome")
        cfg = SkillsConfig.from_dict({"paths": {"workspace_dir": "~/myskills"}})
        assert cfg.paths.workspace_dir == Path("/tmp/fakehome/myskills")
