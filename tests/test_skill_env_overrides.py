"""
T011 + T016 + T022 + T026 — tests/test_skill_env_overrides.py

002-skill-secret-injection: apply_skill_env_overrides + reverter + hot-reload 测试。

覆盖(US1 T011, 5 用例):
1. 单 key 注入 → os.environ[X] 等于 config 值
2. reverter 后 env 还原(原本没设 → pop; 原本有值 → 还原)
3. reverter pop 未设过的 key(原值 None → os.environ.pop)
4. 下游抛异常时 reverter 仍可调(snapshot 不丢)
5. 多次 apply/revert 幂等无残留

US2 T016 (2 用例):
6. 单 skill 3 secret 一次 apply 全部注入 + reverter 全部还原
7. skill-A 与 skill-B 共享 env 名 X → apply 后 os.environ[X] 等于唯一值 + reverter 后 X 等于 RUN 前原值

后续 US3 T022 + Polish T026 在同文件扩展。
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
    keys_to_clean = {
        "TEST_KEY_X", "TEST_KEY_Y", "TEST_KEY_Z", "SHARED_KEY",
        "US2_KEY_A", "US2_KEY_B", "US2_KEY_C",
        "MISSING_ENV_NAME", "MISSING_FILE_PATH",
    }
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


# ──────────────────────────────────────────────────────────────────
# T016 — US2 多 secret + 多 skill 共享 env (2 用例)
# ──────────────────────────────────────────────────────────────────

def test_single_skill_three_secrets_all_injected_and_reverted(monkeypatch):
    """单 skill 3 secret 一次 apply 全部注入; reverter 全部还原到 RUN 前状态。

    覆盖 spec US2 AS1+AS2:
    - Given skill 需 [A, B, C] + config 三个值
    - When run start
    - Then 全部注入; reverter 后全部还原
    """
    # 预置: A 已存在 'pre_A', B / C 未设
    monkeypatch.setenv("TEST_KEY_X", "pre_A")  # 复用 fixture 不污染 → 改用 3 个新 key
    keys_to_set = {"US2_KEY_A": "pre_A_value", "US2_KEY_B": "pre_B_value"}
    keys_unset = {"US2_KEY_C"}
    for k in keys_to_set:
        monkeypatch.setenv(k, keys_to_set[k])
    for k in keys_unset:
        monkeypatch.delenv(k, raising=False)

    entry = _make_skill_entry("multi-svc-bot", env_requires=["US2_KEY_A", "US2_KEY_B", "US2_KEY_C"])
    cfg = _make_config_with_entries({
        "multi-svc-bot": SkillEntryConfig(
            secrets={
                "US2_KEY_A": SecretRef(SecretRefKind.INLINE, "injected_A"),
                "US2_KEY_B": SecretRef(SecretRefKind.INLINE, "injected_B"),
                "US2_KEY_C": SecretRef(SecretRefKind.INLINE, "injected_C"),
            },
        ),
    })

    reverter = apply_skill_env_overrides([entry], cfg)

    # 全部注入
    assert os.environ["US2_KEY_A"] == "injected_A"
    assert os.environ["US2_KEY_B"] == "injected_B"
    assert os.environ["US2_KEY_C"] == "injected_C"

    reverter()

    # 还原: A/B 还原到 pre_value, C 被 pop
    assert os.environ.get("US2_KEY_A") == "pre_A_value"
    assert os.environ.get("US2_KEY_B") == "pre_B_value"
    assert "US2_KEY_C" not in os.environ


def test_multi_skill_shared_env_name_snapshot_on_first(monkeypatch):
    """skill-A 与 skill-B 共享同一 env 名 X:
    - snapshot-on-first: 只在第一次见到 X 时 snapshot, 第二次注入不重置 snapshot
    - reverter 后 X 等于 RUN 前原值(而非 skill-A 注入后的值)
    - apply 期间 os.environ[X] 等于唯一最终值(后注入者覆盖, 或先注入者保留)

    覆盖 spec US2 AS3 + Edge Cases "Multiple skills share an env name" + FR-011:
    snapshot 必须在首次注入时拍, reverter 还原到**原始** RUN 前值,
    不是上一个 skill 注入后的值。
    """
    # RUN 前: SHARED_KEY 已有 'pre_run_value' (RUN 前原始值)
    monkeypatch.setenv("SHARED_KEY", "pre_run_value")

    # skill-A 与 skill-B 共享 SHARED_KEY
    entry_a = _make_skill_entry("skill-a", env_requires=["SHARED_KEY"])
    entry_b = _make_skill_entry("skill-b", env_requires=["SHARED_KEY"])

    cfg = _make_config_with_entries({
        "skill-a": SkillEntryConfig(
            secrets={"SHARED_KEY": SecretRef(SecretRefKind.INLINE, "value_from_a")},
        ),
        "skill-b": SkillEntryConfig(
            secrets={"SHARED_KEY": SecretRef(SecretRefKind.INLINE, "value_from_b")},
        ),
    })

    # 关键: 先注入 skill-a, 再注入 skill-b (entries 顺序就是 snapshot 顺序)
    reverter = apply_skill_env_overrides([entry_a, entry_b], cfg)

    # apply 期间: SHARED_KEY 应等于后注入的 value_from_b(后写入覆盖前者)
    assert os.environ["SHARED_KEY"] == "value_from_b", (
        "apply 后 os.environ[SHARED_KEY] 应等于后注入者的值"
    )

    reverter()

    # 关键断言: reverter 还原到 RUN 前原值 'pre_run_value', 而不是 'value_from_a'
    # (snapshot-on-first 语义: 只在第一次见到时 snapshot, 第二次不重置)
    assert os.environ.get("SHARED_KEY") == "pre_run_value", (
        "reverter 后 SHARED_KEY 应还原到 RUN 前原值 (snapshot-on-first), "
        f"got {os.environ.get('SHARED_KEY')!r}"
    )


def test_multi_skill_shared_env_name_reverse_order(monkeypatch):
    """顺序相反的版本: skill-b 先, skill-a 后 → snapshot-on-first 仍生效。

    验证 snapshot 与 entries 顺序无关, 只取决于"首次见到"。
    """
    monkeypatch.setenv("SHARED_KEY", "pre_run_value_2")

    entry_a = _make_skill_entry("skill-a", env_requires=["SHARED_KEY"])
    entry_b = _make_skill_entry("skill-b", env_requires=["SHARED_KEY"])

    cfg = _make_config_with_entries({
        "skill-a": SkillEntryConfig(
            secrets={"SHARED_KEY": SecretRef(SecretRefKind.INLINE, "value_from_a")},
        ),
        "skill-b": SkillEntryConfig(
            secrets={"SHARED_KEY": SecretRef(SecretRefKind.INLINE, "value_from_b")},
        ),
    })

    # 顺序: skill-b 先
    reverter = apply_skill_env_overrides([entry_b, entry_a], cfg)
    assert os.environ["SHARED_KEY"] == "value_from_a", (
        "apply 后 SHARED_KEY 应等于后注入者(skill-a)的值"
    )

    reverter()
    assert os.environ.get("SHARED_KEY") == "pre_run_value_2", (
        "reverter 仍应还原到 RUN 前原值, 与 entries 顺序无关"
    )


# ──────────────────────────────────────────────────────────────────
# T022 — US3 源缺失 warn + skip 集成 (2 用例)
# ──────────────────────────────────────────────────────────────────

def test_missing_env_source_warns_and_skips_without_raising(caplog):
    """ENV 源 os.environ 未设 → log WARN + 该 key 未注入 + run 不 raise。

    spec FR-012: 缺失 secret 不 fail-fast, 降级为 warn + skip, run 继续。
    spec US3 AS4 (negative case)。
    """
    # 确保目标 env 未设 (fixture _clean_env 已 delenv,但显式再 ensure 一次)
    os.environ.pop("MISSING_ENV_NAME", None)
    assert "MISSING_ENV_NAME" not in os.environ

    entry = _make_skill_entry("env-skill", env_requires=["MISSING_ENV_NAME"])
    cfg = _make_config_with_entries({
        "env-skill": SkillEntryConfig(
            secrets={
                "MISSING_ENV_NAME": SecretRef(
                    kind=SecretRefKind.ENV,
                    value="MISSING_ENV_NAME",
                ),
            },
        ),
    })

    with caplog.at_level("WARNING", logger="agent_core.skills.env_overrides"):
        # 不应 raise
        reverter = apply_skill_env_overrides([entry], cfg)

    # key 未注入
    assert "MISSING_ENV_NAME" not in os.environ

    # log 含 WARN + skill 名 + secret 名 + env kind
    warn_records = [r for r in caplog.records if r.levelname == "WARNING"]
    assert any("env-skill" in r.getMessage() for r in warn_records), (
        f"WARN log 应含 skill 名 'env-skill'; got: {[r.getMessage() for r in warn_records]}"
    )
    assert any("MISSING_ENV_NAME" in r.getMessage() for r in warn_records), (
        f"WARN log 应含 secret 名 'MISSING_ENV_NAME'; got: {[r.getMessage() for r in warn_records]}"
    )
    assert any("env" in r.getMessage().lower() for r in warn_records), (
        f"WARN log 应含源类型 'env'; got: {[r.getMessage() for r in warn_records]}"
    )

    # reverter 安全 (no-op, 因为没注入)
    reverter()
    assert "MISSING_ENV_NAME" not in os.environ


def test_missing_file_source_warns_and_skips_without_raising(caplog, tmp_path):
    """FILE 源路径不存在 → log WARN + 该 key 未注入 + run 不 raise。

    spec FR-012: 缺失 secret 不 fail-fast。
    spec US3 negative case (类似 AS4)。
    """
    missing_file = tmp_path / "nonexistent_token_xyz_abc"
    assert not missing_file.exists()

    entry = _make_skill_entry("file-skill", env_requires=["MISSING_FILE_PATH"])
    cfg = _make_config_with_entries({
        "file-skill": SkillEntryConfig(
            secrets={
                "MISSING_FILE_PATH": SecretRef(
                    kind=SecretRefKind.FILE,
                    value=str(missing_file),
                ),
            },
        ),
    })

    with caplog.at_level("WARNING", logger="agent_core.skills.env_overrides"):
        # 不应 raise
        reverter = apply_skill_env_overrides([entry], cfg)

    # key 未注入
    assert "MISSING_FILE_PATH" not in os.environ

    # log 含 WARN + skill 名 + secret 名 + file kind
    warn_records = [r for r in caplog.records if r.levelname == "WARNING"]
    assert any("file-skill" in r.getMessage() for r in warn_records), (
        f"WARN log 应含 skill 名 'file-skill'; got: {[r.getMessage() for r in warn_records]}"
    )
    assert any("MISSING_FILE_PATH" in r.getMessage() for r in warn_records), (
        f"WARN log 应含 secret 名 'MISSING_FILE_PATH'; got: {[r.getMessage() for r in warn_records]}"
    )
    assert any("file" in r.getMessage().lower() for r in warn_records), (
        f"WARN log 应含源类型 'file'; got: {[r.getMessage() for r in warn_records]}"
    )

    reverter()
    assert "MISSING_FILE_PATH" not in os.environ


def test_secret_ref_kind_warns_with_distinct_message(caplog):
    """SECRET_REF kind 在 apply 路径同样 try-except, 但 log 内容区分 ("not implemented v1.1")。

    spec Edge Cases "Vault/external secret store" + T020 任务规范:
    SECRET_REF 与其他缺失源 warn 内容应不同, 便于诊断 vault 误用。
    """
    entry = _make_skill_entry("vault-skill", env_requires=["VAULT_KEY"])
    cfg = _make_config_with_entries({
        "vault-skill": SkillEntryConfig(
            secrets={
                "VAULT_KEY": SecretRef(
                    kind=SecretRefKind.SECRET_REF,
                    value="vault://prod/token",
                ),
            },
        ),
    })

    with caplog.at_level("WARNING", logger="agent_core.skills.env_overrides"):
        reverter = apply_skill_env_overrides([entry], cfg)

    assert "VAULT_KEY" not in os.environ

    # 找 WARN log, 应含 "v1.1" 或 "not implemented" 标识 (SECRET_REF 专用提示)
    warn_records = [r for r in caplog.records if r.levelname == "WARNING"]
    assert any(
        "v1.1" in r.getMessage() or "not implemented" in r.getMessage().lower()
        for r in warn_records
    ), (
        f"SECRET_REF warn 应含 'v1.1' 或 'not implemented' 标识区分其他缺失源; "
        f"got: {[r.getMessage() for r in warn_records]}"
    )

    reverter()