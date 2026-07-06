"""
T011 + T016 + T022 + T026 — tests/test_skill_env_overrides.py

002-skill-secret-injection: apply_skill_env_overrides + reverter + hot-reload 测试。

覆盖(US1 T011, 5 用例):
1. 单 key 注入 → os.environ[X] 等于 config 值
2. reverter 后 env 还原(原本没设 → pop; 原本有值 → 还原)
3. reverter pop 未设过的 key(原值 None → os.environ.pop)
4. 下游抛异常时 reverter 仍可调(snapshot 不丢)
5. 多次 apply/revert 幂等无残留

后续 US2 T016 + US3 T022 + Polish T026 在同文件扩展。
"""

from __future__ import annotations

import os
from typing import Any
from unittest.mock import MagicMock

import pytest

from agent_core.skills.config import SkillsConfig
from agent_core.skills.env_overrides import (
    SecretRef,
    SecretRefKind,
    SkillEntryConfig,
    apply_skill_env_overrides,
)


# ──────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────

def _make_skill_entry(name: str, env_requires: list[str]) -> Any:
    """构造一个 fake SkillEntry-like 对象(只需 apply 函数读到的字段)。"""
    from agent_core.skills.types import Skill, SkillMetadata, SkillRequires

    skill = Skill(
        name=name,
        description=f"fake skill {name}",
        file_path=f"/fake/{name}/SKILL.md",
        base_dir=f"/fake/{name}",
        source="workspace",  # SkillSource.WORKSPACE.value
    )
    metadata = SkillMetadata(
        requires=SkillRequires(env=tuple(env_requires)),
    )
    # 构造 SkillEntry 但只填必要字段
    from agent_core.skills.types import (
        SkillEligibilityState,
        SkillEntry,
        SkillVisibility,
    )
    return SkillEntry(
        skill=skill,
        metadata=metadata,
        eligibility=SkillEligibilityState.ELIGIBLE,
        visibility=SkillVisibility.MODEL_VISIBLE,
    )


def _make_config_with_entries(entries_dict: dict[str, SkillEntryConfig]) -> SkillsConfig:
    """构造带 entries 的 SkillsConfig(paths 用 default 即可)。"""
    cfg = SkillsConfig()
    cfg.entries = entries_dict
    return cfg


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """每个 case 前后清掉可能由本测试注入的 env key, 避免串扰。"""
    keys_to_clean = {"TEST_KEY_X", "TEST_KEY_Y", "TEST_KEY_Z", "SHARED_KEY"}
    for k in keys_to_clean:
        monkeypatch.delenv(k, raising=False)
    yield


# ──────────────────────────────────────────────────────────────────
# T011 — apply + reverter 基础 (5 用例)
# ──────────────────────────────────────────────────────────────────

def test_apply_injects_single_inline_key():
    """单 key 注入: apply 后 os.environ[X] = config 字面值。"""
    entry = _make_skill_entry("echo-skill", env_requires=["TEST_KEY_X"])
    cfg = _make_config_with_entries({
        "echo-skill": SkillEntryConfig(
            secrets={"TEST_KEY_X": SecretRef(SecretRefKind.INLINE, "injected_value_abc")},
        ),
    })

    reverter = apply_skill_env_overrides([entry], cfg)

    try:
        assert os.environ["TEST_KEY_X"] == "injected_value_abc"
    finally:
        reverter()


def test_reverter_pops_unset_key_and_restores_set_key(monkeypatch):
    """reverter 还原: 原 None → pop; 原 'old' → 还原 'old'。"""
    # 预置: SHARED_KEY 已有 'pre_existing' 值
    monkeypatch.setenv("SHARED_KEY", "pre_existing")
    # NEW_KEY 原本未设

    entry = _make_skill_entry("s", env_requires=["SHARED_KEY", "NEW_KEY"])
    cfg = _make_config_with_entries({
        "s": SkillEntryConfig(
            secrets={
                "SHARED_KEY": SecretRef(SecretRefKind.INLINE, "injected_shared"),
                "NEW_KEY": SecretRef(SecretRefKind.INLINE, "injected_new"),
            },
        ),
    })

    reverter = apply_skill_env_overrides([entry], cfg)
    assert os.environ["SHARED_KEY"] == "injected_shared"
    assert os.environ["NEW_KEY"] == "injected_new"

    reverter()
    # 还原后:
    assert os.environ.get("SHARED_KEY") == "pre_existing", "SHARED_KEY 应还原到 pre_existing"
    assert "NEW_KEY" not in os.environ, "NEW_KEY 应被 pop"


def test_reverter_safe_when_apply_injected_nothing():
    """空注入(无 entries 或无 matches)→ reverter 是 no-op, 可安全调。"""
    # case 1: entries=None
    cfg_no_entries = SkillsConfig()
    reverter1 = apply_skill_env_overrides([], cfg_no_entries)
    reverter1()  # 不抛

    # case 2: entry 无 matches(requires.env 不在 cfg.entries)
    entry = _make_skill_entry("s", env_requires=["NEVER_INJECTED"])
    cfg2 = _make_config_with_entries({"other-skill": SkillEntryConfig()})
    reverter2 = apply_skill_env_overrides([entry], cfg2)
    reverter2()  # 不抛


def test_apply_survives_downstream_exception(monkeypatch):
    """下游抛异常时, 之前的 reverter 仍可调(snapshot 不丢)。

    模拟场景: apply 完注入后, '业务代码' 抛异常 → try/finally 调 reverter。
    """
    entry = _make_skill_entry("s", env_requires=["TEST_KEY_X"])
    cfg = _make_config_with_entries({
        "s": SkillEntryConfig(
            secrets={"TEST_KEY_X": SecretRef(SecretRefKind.INLINE, "v")},
        ),
    })

    reverter = apply_skill_env_overrides([entry], cfg)
    assert os.environ.get("TEST_KEY_X") == "v"

    try:
        raise RuntimeError("downstream error")
    except RuntimeError:
        reverter()

    assert "TEST_KEY_X" not in os.environ


def test_repeat_apply_revert_no_residual(monkeypatch):
    """多次 apply / revert 幂等无残留。"""
    entry = _make_skill_entry("s", env_requires=["TEST_KEY_X"])

    def _one_round(value: str):
        cfg = _make_config_with_entries({
            "s": SkillEntryConfig(
                secrets={"TEST_KEY_X": SecretRef(SecretRefKind.INLINE, value)},
            ),
        })
        reverter = apply_skill_env_overrides([entry], cfg)
        try:
            return os.environ.get("TEST_KEY_X")
        finally:
            reverter()

    # 3 轮: 不同 value, 每轮 apply + revert 后 env 应回到 baseline
    for v in ["v1", "v2", "v3"]:
        assert _one_round(v) == v
        assert "TEST_KEY_X" not in os.environ