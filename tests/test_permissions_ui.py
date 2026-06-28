"""
权限规则编辑 UI helper 测试(M3 Task 1)

覆盖 permission_ui_helpers.py 纯函数 + add/delete roundtrip。
不测 Streamlit 渲染(UI 难单测),只测可复用的纯逻辑。
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from agent_core.tools.permission_ui_helpers import (
    _split_rule_str,
    build_permission_rule,
    format_rules_by_source,
    render_rule_preview,
)
from agent_core.tools.permission_loader import (
    add_permission_rules_to_settings,
    delete_permission_rule_from_settings,
    load_rules_by_source,
)
from agent_core.tools.permission_types import (
    PermissionBehavior,
    PermissionRule,
    PermissionRuleSource,
    PermissionRuleValue,
)


# ────────────────────────────────────────────────────────────────────
# render_rule_preview
# ────────────────────────────────────────────────────────────────────

class TestRenderRulePreview:
    def test_prefix_preview(self):
        # rm:* → prefix
        result = render_rule_preview("Bash", "rm:*")
        assert "prefix" in result
        assert "rm" in result

    def test_exact_preview(self):
        result = render_rule_preview("Bash", "npm run build")
        assert "exact" in result
        assert "npm run build" in result

    def test_wildcard_preview(self):
        result = render_rule_preview("Bash", "*echo*")
        assert "wildcard" in result
        assert "echo" in result

    def test_none_content_whole_tool(self):
        # content=None → 整个 tool 命中
        result = render_rule_preview("Bash", None)
        assert "exact" in result  # exact 空字符串 = 整个 tool

    def test_empty_tool_name(self):
        result = render_rule_preview("", "rm:*")
        assert "空" in result or "tool_name" in result

    def test_compound_preview(self):
        result = render_rule_preview("Bash", "rm:* && echo:*")
        # compound 形态
        assert "compound" in result or "rm" in result


# ────────────────────────────────────────────────────────────────────
# build_permission_rule
# ────────────────────────────────────────────────────────────────────

class TestBuildPermissionRule:
    def test_valid_deny_rule(self):
        rule = build_permission_rule(
            behavior="deny", tool_name="Bash", content="rm:*",
            destination="projectSettings",
        )
        assert rule.behavior == PermissionBehavior.DENY
        assert rule.tool_name == "Bash"
        assert rule.rule_content == "rm:*"
        assert rule.source == PermissionRuleSource.PROJECT

    def test_valid_allow_rule_no_content(self):
        # content 空 → None(整个 tool 命中)
        rule = build_permission_rule(
            behavior="allow", tool_name="Read", content="",
            destination="localSettings",
        )
        assert rule.behavior == PermissionBehavior.ALLOW
        assert rule.rule_content is None
        assert rule.source == PermissionRuleSource.LOCAL

    def test_valid_ask_rule(self):
        rule = build_permission_rule(
            behavior="ask", tool_name="Bash", content="git push:*",
            destination="userSettings",
        )
        assert rule.behavior == PermissionBehavior.ASK
        assert rule.source == PermissionRuleSource.USER

    def test_invalid_behavior_raises(self):
        with pytest.raises(ValueError, match="behavior"):
            build_permission_rule(
                behavior="bogus", tool_name="Bash", content="x",
                destination="projectSettings",
            )

    def test_invalid_destination_raises(self):
        # session/command 不可写(managed-only)
        with pytest.raises(ValueError, match="destination"):
            build_permission_rule(
                behavior="deny", tool_name="Bash", content="x",
                destination="session",
            )

    def test_invalid_destination_policy_raises(self):
        with pytest.raises(ValueError, match="destination"):
            build_permission_rule(
                behavior="deny", tool_name="Bash", content="x",
                destination="policySettings",
            )

    def test_empty_tool_name_raises(self):
        with pytest.raises(ValueError, match="tool_name"):
            build_permission_rule(
                behavior="deny", tool_name="", content="x",
                destination="projectSettings",
            )

    def test_whitespace_tool_name_stripped(self):
        rule = build_permission_rule(
            behavior="deny", tool_name="  Bash  ", content="rm:*",
            destination="projectSettings",
        )
        assert rule.tool_name == "Bash"

    def test_content_stripped(self):
        rule = build_permission_rule(
            behavior="deny", tool_name="Bash", content="  rm:*  ",
            destination="projectSettings",
        )
        assert rule.rule_content == "rm:*"


# ────────────────────────────────────────────────────────────────────
# format_rules_by_source
# ────────────────────────────────────────────────────────────────────

class TestFormatRulesBySource:
    def test_empty_dict_returns_empty_list(self):
        assert format_rules_by_source({}) == []
        assert format_rules_by_source(None) == []  # type: ignore

    def test_flattens_nested_dict(self):
        rules = {
            "always_deny_rules": {"projectSettings": ["Bash(rm:*)"]},
            "always_allow_rules": {"localSettings": ["Read"]},
            "always_ask_rules": {},
        }
        flat = format_rules_by_source(rules)
        assert len(flat) == 2

    def test_deny_shown_first(self):
        # deny 优先显示
        rules = {
            "always_allow_rules": {"projectSettings": ["Read"]},
            "always_deny_rules": {"projectSettings": ["Bash(rm:*)"]},
        }
        flat = format_rules_by_source(rules)
        assert flat[0]["behavior"] == "deny"
        assert flat[1]["behavior"] == "allow"

    def test_rule_dict_has_all_fields(self):
        rules = {
            "always_deny_rules": {"projectSettings": ["Bash(rm:*)"]},
        }
        flat = format_rules_by_source(rules)
        r = flat[0]
        assert r["source"] == "projectSettings"
        assert r["behavior"] == "deny"
        assert r["rule_str"] == "Bash(rm:*)"
        assert r["tool_name"] == "Bash"
        assert r["content"] == "rm:*"

    def test_skips_non_string_entries(self):
        rules = {
            "always_deny_rules": {"projectSettings": ["Bash(rm:*)", "", None, 123]},  # type: ignore
        }
        flat = format_rules_by_source(rules)
        assert len(flat) == 1  # 只有第一条有效

    def test_multiple_sources_multiple_rules(self):
        rules = {
            "always_deny_rules": {
                "projectSettings": ["Bash(rm:*)"],
                "userSettings": ["Edit(.env:*)"],
            },
        }
        flat = format_rules_by_source(rules)
        assert len(flat) == 2
        sources = {r["source"] for r in flat}
        assert sources == {"projectSettings", "userSettings"}


# ────────────────────────────────────────────────────────────────────
# _split_rule_str
# ────────────────────────────────────────────────────────────────────

class TestSplitRuleStr:
    def test_with_content(self):
        assert _split_rule_str("Bash(rm:*)") == ("Bash", "rm:*")

    def test_without_content(self):
        assert _split_rule_str("Read") == ("Read", None)

    def test_empty_content(self):
        assert _split_rule_str("Bash()") == ("Bash", None)

    def test_strips_whitespace(self):
        assert _split_rule_str("  Bash(rm:*)  ") == ("Bash", "rm:*")


# ────────────────────────────────────────────────────────────────────
# add/delete roundtrip(用 tmp settings)
# ────────────────────────────────────────────────────────────────────

class TestAddDeleteRoundtrip:
    def test_add_then_load(self, tmp_path, monkeypatch):
        # mock settings 路径到 tmp
        fake_settings = tmp_path / "settings.json"
        monkeypatch.setattr(
            "agent_core.tools.permission_loader.get_settings_path",
            lambda: fake_settings,
        )

        rule = build_permission_rule(
            behavior="deny", tool_name="Bash", content="rm:*",
            destination="projectSettings",
        )
        add_permission_rules_to_settings([rule], PermissionRuleSource.PROJECT)

        # 写入后文件存在
        assert fake_settings.exists()
        data = json.loads(fake_settings.read_text())
        assert "Bash(rm:*)" in data["permissions"]["deny"]

    def test_delete_after_add(self, tmp_path, monkeypatch):
        fake_settings = tmp_path / "settings.json"
        monkeypatch.setattr(
            "agent_core.tools.permission_loader.get_settings_path",
            lambda: fake_settings,
        )

        rule = build_permission_rule(
            behavior="deny", tool_name="Bash", content="rm:*",
            destination="projectSettings",
        )
        add_permission_rules_to_settings([rule], PermissionRuleSource.PROJECT)

        # 删除
        deleted = delete_permission_rule_from_settings(rule, PermissionRuleSource.PROJECT)
        assert deleted is True

        # 再读 → 不含该 rule
        data = json.loads(fake_settings.read_text())
        assert "Bash(rm:*)" not in data["permissions"]["deny"]

    def test_delete_nonexistent_returns_false(self, tmp_path, monkeypatch):
        fake_settings = tmp_path / "settings.json"
        monkeypatch.setattr(
            "agent_core.tools.permission_loader.get_settings_path",
            lambda: fake_settings,
        )

        rule = PermissionRule(
            source=PermissionRuleSource.PROJECT,
            behavior=PermissionBehavior.DENY,
            value=PermissionRuleValue(tool_name="Bash", rule_content="rm:*"),
        )
        # 文件不存在 → 删除返 False
        deleted = delete_permission_rule_from_settings(rule, PermissionRuleSource.PROJECT)
        assert deleted is False

    def test_full_roundtrip_via_format(self, tmp_path, monkeypatch):
        # 端到端:add → load_rules_by_source → format_rules_by_source
        fake_settings = tmp_path / "settings.json"
        monkeypatch.setattr(
            "agent_core.tools.permission_loader.get_settings_path",
            lambda: fake_settings,
        )
        # local/user 也指向 tmp(避免污染真实文件)
        monkeypatch.setattr(
            "agent_core.tools.permission_loader.get_local_settings_path",
            lambda: tmp_path / "settings.local.json",
        )

        rule = build_permission_rule(
            behavior="allow", tool_name="Read", content="",
            destination="projectSettings",
        )
        add_permission_rules_to_settings([rule], PermissionRuleSource.PROJECT)

        rules_by_source = load_rules_by_source()
        flat = format_rules_by_source(rules_by_source)
        # 应能读到刚加的 Read allow rule
        read_rules = [r for r in flat if r["tool_name"] == "Read"]
        assert len(read_rules) >= 1
        assert read_rules[0]["behavior"] == "allow"


# ────────────────────────────────────────────────────────────────────
# 页面文件 smoke(存在 + import 不报错的 helper)
# ────────────────────────────────────────────────────────────────────

class TestPageFileSmoke:
    def test_page_file_exists(self):
        page = Path(__file__).parent.parent / "web" / "pages" / "03_Permissions.py"
        assert page.exists()

    def test_page_imports_helpers(self):
        # 页面文件应 import permission_ui_helpers(验证接线)
        page = Path(__file__).parent.parent / "web" / "pages" / "03_Permissions.py"
        content = page.read_text(encoding="utf-8")
        assert "from agent_core.tools.permission_ui_helpers import" in content
        assert "build_permission_rule" in content
        assert "render_rule_preview" in content

    def test_page_has_form(self):
        page = Path(__file__).parent.parent / "web" / "pages" / "03_Permissions.py"
        content = page.read_text(encoding="utf-8")
        assert "st.form" in content
        assert "add_permission_rules_to_settings" in content

    def test_sidebar_entry_in_app_py(self):
        app = Path(__file__).parent.parent / "web" / "app.py"
        content = app.read_text(encoding="utf-8")
        assert "03_Permissions.py" in content
        assert "权限规则" in content
