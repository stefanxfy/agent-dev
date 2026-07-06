"""tests/test_skill_bundled.py — bundled skill-creator 被发现且 model-visible（T033）

独立文件（避免与 US5 test_skill_index_merge.py 并行冲突，analyze I1）。
通过 registry.snapshot() 端到端验证 bundled skill 进 prompt。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from agent_core.skills.config import SkillsConfig
from agent_core.skills.registry import SkillsRegistry


# bundled skill 真实目录（包内 agent_core/skills/builtin/）
BUILTIN_DIR = Path(__file__).resolve().parent.parent / "agent_core" / "skills" / "builtin"


class TestBundledSkillCreator:
    """bundled skill-creator：自带、被发现、进 prompt（SC：ship 即可用）"""

    def test_builtin_dir_exists_with_skill_creator(self):
        # 前置：bundled 目录确实含 skill-creator/SKILL.md（ship 即可用）
        assert BUILTIN_DIR.is_dir(), f"builtin 目录不存在: {BUILTIN_DIR}"
        assert (BUILTIN_DIR / "skill-creator" / "SKILL.md").is_file()

    def test_bundled_skill_discovered_in_snapshot(self, tmp_path):
        # workspace 指向空目录 → snapshot 应含 bundled skill-creator
        empty_ws = tmp_path / "empty-workspace"
        empty_ws.mkdir()
        cfg = SkillsConfig.from_dict({
            "paths": {
                "bundled_dir": str(BUILTIN_DIR),
                "workspace_dir": str(empty_ws),
            }
        })
        reg = SkillsRegistry(cfg)
        snap = reg.snapshot()
        assert "skill-creator" in snap.prompt
        # summary 中也有它，且 eligible
        names = {s.name for s in snap.skills}
        assert "skill-creator" in names

    def test_bundled_skill_creator_has_metadata_os(self):
        # T030：bundled skill-creator 含 metadata.os（完整 frontmatter）
        from agent_core.skills.skill_store import load_single_skill
        from agent_core.skills.types import SkillSource

        entry = load_single_skill(BUILTIN_DIR / "skill-creator", SkillSource.BUNDLED)
        assert entry.load_error is None
        assert entry.skill.name == "skill-creator"
        assert entry.skill.description  # 非空

    def test_bundled_location_is_absolute(self, tmp_path):
        empty_ws = tmp_path / "empty-workspace"
        empty_ws.mkdir()
        cfg = SkillsConfig.from_dict({
            "paths": {
                "bundled_dir": str(BUILTIN_DIR),
                "workspace_dir": str(empty_ws),
            }
        })
        reg = SkillsRegistry(cfg)
        snap = reg.snapshot()
        # <location> 是绝对路径
        assert str(BUILTIN_DIR / "skill-creator" / "SKILL.md") in snap.prompt
