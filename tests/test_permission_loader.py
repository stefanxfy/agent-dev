"""
permission_loader.py 测试

覆盖:
1. get_settings_path 优先级(env > config > ~/.agent_data)
2. load_settings_json 容忍 corrupted JSON
3. load_rules_by_source 8 source 合并
4. managed-only mode 只读 policy
5. get_permission_rules_for_source + load_all_permission_rules_from_disk
6. load_tool_permission_context 完整 context
7. add/delete roundtrip
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from agent_core.tools.permission_loader import (
    _settings_for_destination,
    add_permission_rules_to_settings,
    delete_permission_rule_from_settings,
    get_local_settings_path,
    get_permission_rules_for_source,
    get_settings_path,
    is_managed_only,
    load_all_permission_rules_from_disk,
    load_rules_by_source,
    load_settings_json,
    load_tool_permission_context,
)
from agent_core.tools.permission_types import (
    AdditionalWorkingDirectory,
    PermissionBehavior,
    PermissionRule,
    PermissionRuleSource,
    PermissionRuleValue,
    ToolPermissionContext,
)


# ────────────────────────────────────────────────────────────────────
# get_settings_path
# ────────────────────────────────────────────────────────────────────

class TestGetSettingsPath:
    def test_env_override_wins(self, monkeypatch, tmp_path):
        """env AGENT_SETTINGS_PATH 直接覆盖"""
        custom = tmp_path / "custom-settings.json"
        monkeypatch.setenv("AGENT_SETTINGS_PATH", str(custom))
        assert get_settings_path() == custom

    def test_falls_back_to_home(self, monkeypatch):
        """无 env / 无 config → ~/.agent_data/settings.json"""
        monkeypatch.delenv("AGENT_SETTINGS_PATH", raising=False)
        result = get_settings_path()
        # 默认应该以 .agent_data 结尾
        assert result.name == "settings.json"
        assert ".agent_data" in str(result)


# ────────────────────────────────────────────────────────────────────
# load_settings_json
# ────────────────────────────────────────────────────────────────────

class TestLoadSettingsJson:
    def test_load_valid_json(self, tmp_path):
        """合法 JSON 正常解析"""
        p = tmp_path / "settings.json"
        p.write_text(json.dumps({"permissions": {"allow": ["Edit"]}}))
        assert load_settings_json(p) == {"permissions": {"allow": ["Edit"]}}

    def test_nonexistent_returns_empty(self, tmp_path):
        """文件不存在返空 dict"""
        p = tmp_path / "does-not-exist.json"
        assert load_settings_json(p) == {}

    def test_corrupted_json_returns_empty(self, tmp_path):
        """corrupted JSON 返空 dict,不抛异常"""
        p = tmp_path / "bad.json"
        p.write_text("{this is not valid json")
        assert load_settings_json(p) == {}

    def test_non_dict_top_level_returns_empty(self, tmp_path):
        """顶层不是 dict 返空"""
        p = tmp_path / "list.json"
        p.write_text("[1, 2, 3]")
        assert load_settings_json(p) == {}

    def test_empty_file_returns_empty(self, tmp_path):
        """空文件返空 dict"""
        p = tmp_path / "empty.json"
        p.write_text("")
        assert load_settings_json(p) == {}


# ────────────────────────────────────────────────────────────────────
# is_managed_only
# ────────────────────────────────────────────────────────────────────

class TestIsManagedOnly:
    @pytest.mark.parametrize("val", ["1", "true", "yes", "on", "TRUE", "Yes"])
    def test_truthy_env(self, monkeypatch, val):
        """env 真值 → True"""
        monkeypatch.setenv("AGENT_MANAGED_PERMISSIONS_ONLY", val)
        assert is_managed_only() is True

    @pytest.mark.parametrize("val", ["", "0", "false", "no", "off", "garbage"])
    def test_falsy_env(self, monkeypatch, val):
        """env 假值 / 未知值 → False"""
        monkeypatch.setenv("AGENT_MANAGED_PERMISSIONS_ONLY", val)
        assert is_managed_only() is False

    def test_unset_env(self, monkeypatch):
        """env 未设 → False"""
        monkeypatch.delenv("AGENT_MANAGED_PERMISSIONS_ONLY", raising=False)
        assert is_managed_only() is False


# ────────────────────────────────────────────────────────────────────
# load_rules_by_source — 8 source 合并
# ────────────────────────────────────────────────────────────────────

class TestLoadRulesBySource:
    def test_empty_settings(self, monkeypatch, tmp_path):
        """无 settings → 全空 dict"""
        monkeypatch.setenv("AGENT_SETTINGS_PATH", str(tmp_path / "nonexistent.json"))
        monkeypatch.delenv("AGENT_MANAGED_PERMISSIONS_ONLY", raising=False)
        result = load_rules_by_source()
        assert result["always_allow_rules"] == {s.value: [] for s in PermissionRuleSource}
        assert result["always_deny_rules"] == {s.value: [] for s in PermissionRuleSource}

    def test_load_project_settings(self, monkeypatch, tmp_path):
        """project settings 正常加载"""
        settings_path = tmp_path / "settings.json"
        settings_path.write_text(json.dumps({
            "permissions": {
                "allow": ["Edit", "Read"],
                "deny": ["Bash(rm:*)"],
                "ask": ["Bash(npm publish:*)"],
            }
        }))
        monkeypatch.setenv("AGENT_SETTINGS_PATH", str(settings_path))
        monkeypatch.delenv("AGENT_MANAGED_PERMISSIONS_ONLY", raising=False)

        result = load_rules_by_source()
        assert "Edit" in result["always_allow_rules"]["projectSettings"]
        assert "Read" in result["always_allow_rules"]["projectSettings"]
        assert "Bash(rm:*)" in result["always_deny_rules"]["projectSettings"]
        assert "Bash(npm publish:*)" in result["always_ask_rules"]["projectSettings"]
        # 其他 source 仍是空
        assert result["always_allow_rules"]["userSettings"] == []

    def test_managed_only_uses_policy(self, monkeypatch, tmp_path):
        """managed-only mode 只读 policy"""
        policy_path = tmp_path / "policy.json"
        policy_path.write_text(json.dumps({
            "permissions": {
                "allow": ["Read"],
                "deny": ["Bash(rm:*)"],
            }
        }))
        monkeypatch.setenv("AGENT_MANAGED_PERMISSIONS_ONLY", "true")
        monkeypatch.setenv("AGENT_POLICY_PATH", str(policy_path))

        result = load_rules_by_source()
        # policy source 有数据
        assert "Read" in result["always_allow_rules"]["policySettings"]
        assert "Bash(rm:*)" in result["always_deny_rules"]["policySettings"]
        # project 是空
        assert result["always_allow_rules"]["projectSettings"] == []


# ────────────────────────────────────────────────────────────────────
# get_permission_rules_for_source
# ────────────────────────────────────────────────────────────────────

class TestGetPermissionRulesForSource:
    def test_returns_parsed_rules(self, monkeypatch, tmp_path):
        """返 PermissionRule 实例(含 source + behavior)"""
        settings_path = tmp_path / "settings.json"
        settings_path.write_text(json.dumps({
            "permissions": {
                "deny": ["Bash(rm:*)", "Edit(/etc)"],
            }
        }))
        monkeypatch.setenv("AGENT_SETTINGS_PATH", str(settings_path))
        monkeypatch.delenv("AGENT_MANAGED_PERMISSIONS_ONLY", raising=False)

        rules = get_permission_rules_for_source(
            PermissionRuleSource.PROJECT,
            PermissionBehavior.DENY,
        )
        assert len(rules) == 2
        assert all(r.source == PermissionRuleSource.PROJECT for r in rules)
        assert all(r.behavior == PermissionBehavior.DENY for r in rules)
        # rule_content 解析正确
        tool_names = {r.tool_name for r in rules}
        assert tool_names == {"Bash", "Edit"}


# ────────────────────────────────────────────────────────────────────
# load_all_permission_rules_from_disk
# ────────────────────────────────────────────────────────────────────

class TestLoadAllPermissionRulesFromDisk:
    def test_includes_project_rules(self, monkeypatch, tmp_path):
        """磁盘加载所有 rule(含 project)"""
        settings_path = tmp_path / "settings.json"
        settings_path.write_text(json.dumps({
            "permissions": {
                "allow": ["Edit"],
                "deny": ["Bash(rm:*)"],
            }
        }))
        monkeypatch.setenv("AGENT_SETTINGS_PATH", str(settings_path))
        monkeypatch.delenv("AGENT_MANAGED_PERMISSIONS_ONLY", raising=False)

        rules = load_all_permission_rules_from_disk()
        # project 至少 2 条
        project_rules = [r for r in rules if r.source == PermissionRuleSource.PROJECT]
        assert len(project_rules) >= 2

    def test_managed_only_only_policy(self, monkeypatch, tmp_path):
        """managed-only 只返 policy"""
        policy_path = tmp_path / "policy.json"
        policy_path.write_text(json.dumps({
            "permissions": {"allow": ["Read"]},
        }))
        monkeypatch.setenv("AGENT_MANAGED_PERMISSIONS_ONLY", "true")
        monkeypatch.setenv("AGENT_POLICY_PATH", str(policy_path))

        rules = load_all_permission_rules_from_disk()
        assert all(r.source == PermissionRuleSource.POLICY for r in rules)


# ────────────────────────────────────────────────────────────────────
# load_tool_permission_context
# ────────────────────────────────────────────────────────────────────

class TestLoadToolPermissionContext:
    def test_default_mode(self, monkeypatch, tmp_path):
        """默认 mode = default"""
        settings_path = tmp_path / "settings.json"
        settings_path.write_text(json.dumps({"permissions": {"allow": ["Edit"]}}))
        monkeypatch.setenv("AGENT_SETTINGS_PATH", str(settings_path))
        monkeypatch.delenv("AGENT_MANAGED_PERMISSIONS_ONLY", raising=False)
        monkeypatch.delenv("AGENT_PERMISSION_MODE", raising=False)

        ctx = load_tool_permission_context()
        assert isinstance(ctx, ToolPermissionContext)
        assert ctx.mode == "default"
        assert "Edit" in ctx.always_allow_rules["projectSettings"]

    def test_env_mode_override(self, monkeypatch, tmp_path):
        """env AGENT_PERMISSION_MODE 覆盖"""
        settings_path = tmp_path / "nonexistent.json"
        monkeypatch.setenv("AGENT_SETTINGS_PATH", str(settings_path))
        monkeypatch.setenv("AGENT_PERMISSION_MODE", "auto")
        monkeypatch.delenv("AGENT_MANAGED_PERMISSIONS_ONLY", raising=False)

        ctx = load_tool_permission_context()
        assert ctx.mode == "auto"

    def test_explicit_mode_arg(self, monkeypatch, tmp_path):
        """显式 mode 参数优先"""
        settings_path = tmp_path / "nonexistent.json"
        monkeypatch.setenv("AGENT_SETTINGS_PATH", str(settings_path))
        monkeypatch.setenv("AGENT_PERMISSION_MODE", "default")
        monkeypatch.delenv("AGENT_MANAGED_PERMISSIONS_ONLY", raising=False)

        ctx = load_tool_permission_context(mode="plan")
        assert ctx.mode == "plan"

    def test_no_settings_match_true_when_empty(self, monkeypatch, tmp_path):
        """无 settings → no_settings_match = True"""
        settings_path = tmp_path / "nonexistent.json"
        monkeypatch.setenv("AGENT_SETTINGS_PATH", str(settings_path))
        monkeypatch.delenv("AGENT_MANAGED_PERMISSIONS_ONLY", raising=False)
        monkeypatch.delenv("AGENT_PERMISSION_MODE", raising=False)

        ctx = load_tool_permission_context()
        assert ctx.no_settings_match is True

    def test_no_settings_match_false_when_has_settings(self, monkeypatch, tmp_path):
        """有 settings → no_settings_match = False"""
        settings_path = tmp_path / "settings.json"
        settings_path.write_text(json.dumps({"permissions": {"allow": ["Edit"]}}))
        monkeypatch.setenv("AGENT_SETTINGS_PATH", str(settings_path))
        monkeypatch.delenv("AGENT_MANAGED_PERMISSIONS_ONLY", raising=False)
        monkeypatch.delenv("AGENT_PERMISSION_MODE", raising=False)

        ctx = load_tool_permission_context()
        assert ctx.no_settings_match is False

    def test_additional_working_directories_loaded(self, monkeypatch, tmp_path):
        """additionalDirectories → AdditionalWorkingDirectory"""
        settings_path = tmp_path / "settings.json"
        settings_path.write_text(json.dumps({
            "permissions": {
                "allow": ["Edit"],
                "additionalDirectories": ["/tmp/shared", "/var/data"],
            }
        }))
        monkeypatch.setenv("AGENT_SETTINGS_PATH", str(settings_path))
        monkeypatch.delenv("AGENT_MANAGED_PERMISSIONS_ONLY", raising=False)
        monkeypatch.delenv("AGENT_PERMISSION_MODE", raising=False)

        ctx = load_tool_permission_context()
        assert "/tmp/shared" in ctx.additional_working_directories
        assert "/var/data" in ctx.additional_working_directories
        assert isinstance(
            ctx.additional_working_directories["/tmp/shared"],
            AdditionalWorkingDirectory,
        )

    def test_sandbox_enabled_passthrough(self, monkeypatch, tmp_path):
        """sandbox_enabled 参数透传"""
        settings_path = tmp_path / "nonexistent.json"
        monkeypatch.setenv("AGENT_SETTINGS_PATH", str(settings_path))
        monkeypatch.delenv("AGENT_MANAGED_PERMISSIONS_ONLY", raising=False)
        monkeypatch.delenv("AGENT_PERMISSION_MODE", raising=False)

        ctx = load_tool_permission_context(sandbox_enabled=True)
        assert ctx.sandbox_enabled is True


# ────────────────────────────────────────────────────────────────────
# add_permission_rules_to_settings + delete roundtrip
# ────────────────────────────────────────────────────────────────────

class TestAddDeleteRoundtrip:
    def test_add_new_rule(self, monkeypatch, tmp_path):
        """添加 rule 到 project settings"""
        settings_path = tmp_path / "settings.json"
        monkeypatch.setenv("AGENT_SETTINGS_PATH", str(settings_path))
        monkeypatch.delenv("AGENT_MANAGED_PERMISSIONS_ONLY", raising=False)

        rule = PermissionRule(
            source=PermissionRuleSource.PROJECT,
            behavior=PermissionBehavior.DENY,
            value=PermissionRuleValue(tool_name="Bash", rule_content="rm:*"),
        )
        add_permission_rules_to_settings([rule], PermissionRuleSource.PROJECT)

        assert settings_path.exists()
        with settings_path.open() as f:
            data = json.load(f)
        assert "Bash(rm:*)" in data["permissions"]["deny"]

    def test_add_dedupes(self, monkeypatch, tmp_path):
        """重复添加 rule 不重复"""
        settings_path = tmp_path / "settings.json"
        monkeypatch.setenv("AGENT_SETTINGS_PATH", str(settings_path))
        monkeypatch.delenv("AGENT_MANAGED_PERMISSIONS_ONLY", raising=False)

        rule = PermissionRule(
            source=PermissionRuleSource.PROJECT,
            behavior=PermissionBehavior.DENY,
            value=PermissionRuleValue(tool_name="Bash", rule_content="rm:*"),
        )
        add_permission_rules_to_settings([rule], PermissionRuleSource.PROJECT)
        add_permission_rules_to_settings([rule], PermissionRuleSource.PROJECT)

        with settings_path.open() as f:
            data = json.load(f)
        # 只出现一次
        assert data["permissions"]["deny"].count("Bash(rm:*)") == 1

    def test_add_to_local_settings(self, monkeypatch, tmp_path):
        """添加 rule 到 local settings(settings.local.json)"""
        project_path = tmp_path / "settings.json"
        local_path = tmp_path / "settings.local.json"
        monkeypatch.setenv("AGENT_SETTINGS_PATH", str(project_path))
        monkeypatch.delenv("AGENT_MANAGED_PERMISSIONS_ONLY", raising=False)

        rule = PermissionRule(
            source=PermissionRuleSource.LOCAL,
            behavior=PermissionBehavior.ALLOW,
            value=PermissionRuleValue(tool_name="Edit"),
        )
        add_permission_rules_to_settings([rule], PermissionRuleSource.LOCAL)

        assert local_path.exists()
        with local_path.open() as f:
            data = json.load(f)
        assert "Edit" in data["permissions"]["allow"]

    def test_delete_existing_rule(self, monkeypatch, tmp_path):
        """删除存在的 rule 返 True"""
        settings_path = tmp_path / "settings.json"
        settings_path.write_text(json.dumps({
            "permissions": {"deny": ["Bash(rm:*)"]},
        }))
        monkeypatch.setenv("AGENT_SETTINGS_PATH", str(settings_path))
        monkeypatch.delenv("AGENT_MANAGED_PERMISSIONS_ONLY", raising=False)

        rule = PermissionRule(
            source=PermissionRuleSource.PROJECT,
            behavior=PermissionBehavior.DENY,
            value=PermissionRuleValue(tool_name="Bash", rule_content="rm:*"),
        )
        assert delete_permission_rule_from_settings(rule, PermissionRuleSource.PROJECT) is True
        with settings_path.open() as f:
            data = json.load(f)
        assert "Bash(rm:*)" not in data["permissions"]["deny"]

    def test_delete_nonexistent_returns_false(self, monkeypatch, tmp_path):
        """删除不存在的 rule 返 False"""
        settings_path = tmp_path / "settings.json"
        settings_path.write_text(json.dumps({"permissions": {"deny": []}}))
        monkeypatch.setenv("AGENT_SETTINGS_PATH", str(settings_path))
        monkeypatch.delenv("AGENT_MANAGED_PERMISSIONS_ONLY", raising=False)

        rule = PermissionRule(
            source=PermissionRuleSource.PROJECT,
            behavior=PermissionBehavior.DENY,
            value=PermissionRuleValue(tool_name="Bash", rule_content="rm:*"),
        )
        assert delete_permission_rule_from_settings(rule, PermissionRuleSource.PROJECT) is False

    def test_managed_only_blocks_writes(self, monkeypatch, tmp_path, caplog):
        """managed-only mode 阻止写非 policy destination"""
        settings_path = tmp_path / "settings.json"
        policy_path = tmp_path / "policy.json"
        monkeypatch.setenv("AGENT_MANAGED_PERMISSIONS_ONLY", "true")
        monkeypatch.setenv("AGENT_POLICY_PATH", str(policy_path))
        monkeypatch.setenv("AGENT_SETTINGS_PATH", str(settings_path))

        rule = PermissionRule(
            source=PermissionRuleSource.PROJECT,
            behavior=PermissionBehavior.DENY,
            value=PermissionRuleValue(tool_name="Bash", rule_content="rm:*"),
        )
        add_permission_rules_to_settings([rule], PermissionRuleSource.PROJECT)
        # project settings 不应被写入
        assert not settings_path.exists()


# ────────────────────────────────────────────────────────────────────
# get_local_settings_path
# ────────────────────────────────────────────────────────────────────

class TestGetLocalSettingsPath:
    def test_local_settings_filename(self, monkeypatch, tmp_path):
        """local 路径文件名是 settings.local.json"""
        monkeypatch.setenv("AGENT_SETTINGS_PATH", str(tmp_path / "settings.json"))
        local = get_local_settings_path()
        assert local.name == "settings.local.json"
        assert local.parent == (tmp_path).resolve() or local.parent == tmp_path
