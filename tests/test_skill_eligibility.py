"""tests/test_skill_eligibility.py — 5 维 requires + always + 即时性（T039, INV-5, FR-013/14）"""
from __future__ import annotations

import pytest

from agent_core.skills.eligibility import evaluate_eligibility
from agent_core.skills.types import (
    Skill,
    SkillEligibilityState,
    SkillEntry,
    SkillMetadata,
    SkillRequires,
    SkillSource,
)


def _entry(
    *,
    metadata: SkillMetadata | None = None,
    load_error: str | None = None,
    name: str = "x",
) -> SkillEntry:
    return SkillEntry(
        skill=Skill(name=name, description="d", file_path=f"/x/{name}/SKILL.md",
                    base_dir=f"/x/{name}", source=SkillSource.WORKSPACE),
        load_error=load_error,
        metadata=metadata,
    )


class TestAlwaysBypass:
    def test_always_eligible_ignores_unmet_requires(self):
        md = SkillMetadata(always=True, requires=SkillRequires(env=["MISSING_X"]))
        st, missing = evaluate_eligibility(_entry(metadata=md), env={})
        assert st == SkillEligibilityState.ELIGIBLE
        assert missing == ()

    def test_always_with_load_error_still_errored(self):
        # load_error 优先于 always（ERRORED 分支在前）
        md = SkillMetadata(always=True)
        st, _ = evaluate_eligibility(_entry(metadata=md, load_error="boom"), env={})
        assert st == SkillEligibilityState.ERRORED


class TestRequiresBins:
    def test_bins_all_present(self, monkeypatch):
        monkeypatch.setattr(
            "agent_core.skills.eligibility.shutil.which",
            lambda b: "/usr/bin/" + b if b in ("ls", "cat") else None,
        )
        md = SkillMetadata(requires=SkillRequires(bins=("ls", "cat")))
        st, missing = evaluate_eligibility(_entry(metadata=md), env={})
        assert st == SkillEligibilityState.ELIGIBLE

    def test_bins_one_missing(self, monkeypatch):
        monkeypatch.setattr(
            "agent_core.skills.eligibility.shutil.which",
            lambda b: "/usr/bin/ls" if b == "ls" else None,
        )
        md = SkillMetadata(requires=SkillRequires(bins=("ls", "nope-bin")))
        st, missing = evaluate_eligibility(_entry(metadata=md), env={})
        assert st == SkillEligibilityState.MISSING_REQUIREMENTS
        assert any("bin:nope-bin" in m for m in missing)


class TestRequiresAnyBins:
    def test_anybins_one_present_ok(self, monkeypatch):
        monkeypatch.setattr(
            "agent_core.skills.eligibility.shutil.which",
            lambda b: "/usr/bin/rg" if b == "rg" else None,
        )
        md = SkillMetadata(requires=SkillRequires(any_bins=("rg", "grep")))
        st, _ = evaluate_eligibility(_entry(metadata=md), env={})
        assert st == SkillEligibilityState.ELIGIBLE

    def test_anybins_none_present_missing(self, monkeypatch):
        monkeypatch.setattr(
            "agent_core.skills.eligibility.shutil.which", lambda b: None,
        )
        md = SkillMetadata(requires=SkillRequires(any_bins=("rg", "grep")))
        st, missing = evaluate_eligibility(_entry(metadata=md), env={})
        assert st == SkillEligibilityState.MISSING_REQUIREMENTS
        assert any("anyBins" in m for m in missing)


class TestRequiresEnv:
    def test_env_present(self):
        md = SkillMetadata(requires=SkillRequires(env=("FOO_KEY",)))
        st, _ = evaluate_eligibility(_entry(metadata=md), env={"FOO_KEY": "x"})
        assert st == SkillEligibilityState.ELIGIBLE

    def test_env_absent(self):
        md = SkillMetadata(requires=SkillRequires(env=("FOO_KEY",)))
        st, missing = evaluate_eligibility(_entry(metadata=md), env={})
        assert st == SkillEligibilityState.MISSING_REQUIREMENTS
        assert any("env:FOO_KEY" in m for m in missing)


class TestRequiresConfig:
    def test_config_truthy(self):
        md = SkillMetadata(requires=SkillRequires(config=("channels.x",)))
        st, _ = evaluate_eligibility(
            _entry(metadata=md), config={"channels": {"x": 1}},
        )
        assert st == SkillEligibilityState.ELIGIBLE

    def test_config_falsy_missing(self):
        md = SkillMetadata(requires=SkillRequires(config=("channels.x",)))
        st, missing = evaluate_eligibility(
            _entry(metadata=md), config={"channels": {"x": 0}},
        )
        assert st == SkillEligibilityState.MISSING_REQUIREMENTS
        assert any("config:channels.x" in m for m in missing)

    def test_config_path_absent(self):
        md = SkillMetadata(requires=SkillRequires(config=("a.b.c",)))
        st, _ = evaluate_eligibility(_entry(metadata=md), config={"a": {}})
        assert st == SkillEligibilityState.MISSING_REQUIREMENTS


class TestRequiresOs:
    def test_os_in_whitelist(self):
        md = SkillMetadata(os=("linux",))
        st, _ = evaluate_eligibility(_entry(metadata=md), platform_os="linux")
        assert st == SkillEligibilityState.ELIGIBLE

    def test_os_not_in_whitelist(self):
        md = SkillMetadata(os=("darwin",))
        st, missing = evaluate_eligibility(_entry(metadata=md), platform_os="linux")
        assert st == SkillEligibilityState.MISSING_REQUIREMENTS
        assert any(m.startswith("os:") for m in missing)


class TestErrored:
    def test_load_error_errored(self):
        st, missing = evaluate_eligibility(_entry(load_error="bad yaml"), env={})
        assert st == SkillEligibilityState.ERRORED
        assert "bad yaml" in missing[0]


class TestImmediacy:  # INV-5 — 即时性：env 变化前后状态翻转
    def test_env_set_flips_missing_to_eligible(self):
        md = SkillMetadata(requires=SkillRequires(env=("VERIFY_KEY",)))
        e = _entry(metadata=md)
        # 无 key → MISSING
        st1, _ = evaluate_eligibility(e, env={})
        assert st1 == SkillEligibilityState.MISSING_REQUIREMENTS
        # 设 key → ELIGIBLE（同一 entry，不同 env，即时求值）
        st2, _ = evaluate_eligibility(e, env={"VERIFY_KEY": "x"})
        assert st2 == SkillEligibilityState.ELIGIBLE

    def test_no_requirements_default_eligible(self):
        # 无 metadata / 无 requires → ELIGIBLE
        st, missing = evaluate_eligibility(_entry(), env={})
        assert st == SkillEligibilityState.ELIGIBLE
        assert missing == ()
