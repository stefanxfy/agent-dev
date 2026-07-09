"""tests/test_skill_types.py — Skill 系统数据类型 + 纯校验函数（T008）"""
from __future__ import annotations

import pytest

from agent_core.skills.types import (
    CURRENT_SCHEMA_VERSION,
    Skill,
    SkillEntry,
    SkillFrontmatterError,
    SkillRenderMode,
    SkillSnapshot,
    SkillSource,
    SkillSummary,
    validate_frontmatter,
)


class TestSkillSource:
    def test_priority_workspace_overrides_bundled(self):
        assert SkillSource.WORKSPACE.priority > SkillSource.BUNDLED.priority

    def test_str_enum_values(self):
        assert SkillSource.BUNDLED.value == "bundled"
        assert SkillSource.WORKSPACE.value == "workspace"


class TestValidateFrontmatter:
    def test_valid_minimal(self):
        fm = validate_frontmatter({"name": "x", "description": "do X"})
        assert fm["name"] == "x"
        assert fm["description"] == "do X"

    def test_missing_description_raises(self):
        # FR-003：description 必填
        with pytest.raises(SkillFrontmatterError, match="description"):
            validate_frontmatter({"name": "x"})

    def test_empty_description_raises(self):
        with pytest.raises(SkillFrontmatterError):
            validate_frontmatter({"name": "x", "description": "   "})

    def test_description_non_string_raises(self):
        with pytest.raises(SkillFrontmatterError):
            validate_frontmatter({"name": "x", "description": 123})

    def test_missing_name_uses_fallback(self):
        fm = validate_frontmatter({"description": "d"}, fallback_name="mydir")
        assert fm["name"] == "mydir"

    def test_missing_name_no_fallback_raises(self):
        with pytest.raises(SkillFrontmatterError):
            validate_frontmatter({"description": "d"})

    def test_description_is_stripped(self):
        fm = validate_frontmatter({"description": "  hi  "}, fallback_name="x")
        assert fm["description"] == "hi"

    def test_non_dict_raises(self):
        with pytest.raises(SkillFrontmatterError):
            validate_frontmatter("not a dict")  # type: ignore[arg-type]

    def test_homepage_non_string_raises(self):
        with pytest.raises(SkillFrontmatterError):
            validate_frontmatter(
                {"description": "d", "homepage": 123}
            )

    def test_homepage_optional(self):
        fm = validate_frontmatter({"description": "d"}, fallback_name="x")
        assert "homepage" not in fm or fm.get("homepage") is None or isinstance(fm["homepage"], str)

    def test_unknown_fields_ignored(self):
        # 前向兼容：未知字段不抛（US2 T031 强化测试）
        fm = validate_frontmatter(
            {"description": "d", "unknown_field": "value", "another": 42},
            fallback_name="x",
        )
        assert fm["description"] == "d"


class TestValueObjects:
    def test_skill_is_frozen(self):
        s = Skill(
            name="x", description="d",
            file_path="/a/SKILL.md", base_dir="/a",
            source=SkillSource.BUNDLED,
        )
        with pytest.raises(Exception):
            s.name = "y"  # frozen dataclass

    def test_skill_hashable(self):
        # INV-2 字节稳定性测试用 hash 断言
        s1 = Skill("x", "d", "/a/SKILL.md", "/a", SkillSource.BUNDLED)
        s2 = Skill("x", "d", "/a/SKILL.md", "/a", SkillSource.BUNDLED)
        assert hash(s1) == hash(s2)

    def test_skill_entry_default_load_error_none(self):
        s = Skill("x", "d", "/a/SKILL.md", "/a", SkillSource.BUNDLED)
        e = SkillEntry(skill=s)
        assert e.load_error is None

    def test_snapshot_defaults(self):
        snap = SkillSnapshot(prompt="x")
        assert snap.skills == []
        assert snap.version == 0
        assert snap.render_mode == SkillRenderMode.FULL
        assert snap.truncated_count == 0


def test_constants():
    assert CURRENT_SCHEMA_VERSION == 1
