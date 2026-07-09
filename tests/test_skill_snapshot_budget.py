"""tests/test_skill_snapshot_budget.py — 三级预算降级（T023, INV-4, SC-004）"""
from __future__ import annotations

import pytest

from agent_core.skills.config import LimitsConfig
from agent_core.skills.snapshot import build_snapshot
from agent_core.skills.types import (
    Skill,
    SkillEntry,
    SkillRenderMode,
    SkillSource,
)


def _entry(name: str, desc_len: int = 50) -> SkillEntry:
    return SkillEntry(
        skill=Skill(
            name=name,
            description="x" * desc_len,
            file_path=f"/skills/{name}/SKILL.md",
            base_dir=f"/skills/{name}",
            source=SkillSource.WORKSPACE,
        )
    )


class TestFullMode:
    def test_small_set_full_mode(self):
        entries = [_entry(f"s{i}") for i in range(3)]
        snap = build_snapshot(entries, limits=LimitsConfig())
        assert snap.render_mode == SkillRenderMode.FULL
        assert snap.truncated_count == 0
        assert "<description>" in snap.prompt  # full 含 description
        assert snap.truncated_count == 0

    def test_no_warning_when_full(self):
        entries = [_entry("x")]
        snap = build_snapshot(entries, limits=LimitsConfig())
        assert not snap.prompt.startswith("⚠️")


class TestCompactDegradation:
    def test_full_too_big_compact_fits(self):
        # 5 skill × 长 description → full 超预算但 compact 装得下
        entries = [_entry(f"s{i}", desc_len=400) for i in range(5)]
        # 紧凑预算：full > max 但 compact < max-200
        limits = LimitsConfig(max_skills_prompt_chars=1500)
        snap = build_snapshot(entries, limits=limits)
        assert snap.render_mode == SkillRenderMode.COMPACT
        assert "compact format" in snap.prompt.lower() or "⚠️" in snap.prompt
        # 长度受控
        assert len(snap.prompt) <= 1500

    def test_compact_omits_description(self):
        entries = [_entry(f"s{i}", desc_len=400) for i in range(5)]
        limits = LimitsConfig(max_skills_prompt_chars=1500)
        snap = build_snapshot(entries, limits=limits)
        # compact 模式 prompt 不含 <description>（在 available_skills 块内）
        body = snap.prompt.split("<available_skills>")[1] if "<available_skills>" in snap.prompt else ""
        assert "<description>" not in body


class TestTruncateDegradation:
    def test_compact_too_big_truncates(self):
        # 100 个 skill，compact 也装不下 → truncate
        entries = [_entry(f"s{i:03d}", desc_len=10) for i in range(100)]
        limits = LimitsConfig(max_skills_prompt_chars=800, max_skills_in_prompt=150)
        snap = build_snapshot(entries, limits=limits)
        assert snap.render_mode == SkillRenderMode.TRUNCATE
        assert snap.truncated_count > 0
        assert "truncated" in snap.prompt.lower()
        assert len(snap.prompt) <= 800

    def test_max_skills_in_prompt_truncates(self):
        # 超过 max_skills_in_prompt 截断（即使总字符不超）
        entries = [_entry(f"s{i:03d}") for i in range(10)]
        limits = LimitsConfig(max_skills_in_prompt=3, max_skills_prompt_chars=100_000)
        snap = build_snapshot(entries, limits=limits)
        # 截掉了 7 个（但 mode 可能是 FULL 因为字符够）
        assert snap.truncated_count == 7


class TestNeverSilentDrop:
    """INV-4 / FR-018：降级必有可见警告"""

    def test_compact_has_warning(self):
        entries = [_entry(f"s{i}", desc_len=400) for i in range(5)]
        snap = build_snapshot(entries, limits=LimitsConfig(max_skills_prompt_chars=1500))
        assert snap.prompt.startswith("⚠️")

    def test_truncate_has_warning(self):
        entries = [_entry(f"s{i:03d}") for i in range(100)]
        snap = build_snapshot(entries, limits=LimitsConfig(max_skills_prompt_chars=800))
        assert snap.prompt.startswith("⚠️")
        assert "truncated" in snap.prompt.lower()

    def test_truncate_includes_count(self):
        entries = [_entry(f"s{i:03d}") for i in range(50)]
        snap = build_snapshot(entries, limits=LimitsConfig(max_skills_prompt_chars=500))
        # 警告含 "included N of M"
        assert "of 50" in snap.prompt


class TestFailedEntryExcluded:
    """US1 filter：load_error 的 entry 不进 prompt（但进 summary）"""

    def test_failed_excluded_from_prompt(self):
        good = _entry("good")
        bad = SkillEntry(
            skill=Skill("bad", "", "/x/SKILL.md", "/x", SkillSource.WORKSPACE),
            load_error="broken",
        )
        snap = build_snapshot([good, bad], limits=LimitsConfig())
        assert "good" in snap.prompt
        assert "bad" not in snap.prompt.split("<name>")[1] if "<name>" in snap.prompt else True
        # summary 仍含 bad（status 用）
        assert any(s.name == "bad" and not s.eligible for s in snap.skills)
