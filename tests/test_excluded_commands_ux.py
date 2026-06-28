"""
excludedCommands 消息化 UX 测试(M3 Task 4)

覆盖:
1. get_excluded_command_match 返 (pattern, message) 或 None
2. get_excluded_command_message 便捷封装
3. _is_excluded_command bool 返回值不变(向后兼容 M2)
4. save_excluded_commands / load_excluded_commands roundtrip
5. 边界 case(空命令 / None pattern / 大小写敏感 / 多 pattern 首个胜)
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from agent_core.tools.permission_loader import (
    load_excluded_commands,
    save_excluded_commands,
)
from agent_core.tools.permission_types import PermissionRuleSource
from agent_core.tools.sandbox_decision import (
    _is_excluded_command,
    get_excluded_command_match,
    get_excluded_command_message,
)
from agent_core.tools.sandbox_manager import sandbox_manager


# ────────────────────────────────────────────────────────────────────
# Fixtures
# ────────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def reset_sandbox_config():
    """每个 test 前在全局 singleton 上设置 excluded_commands(对齐 M2 测试模式)"""
    sandbox_manager._config.excluded_commands = ["git commit", "npm publish"]
    yield
    sandbox_manager._config.excluded_commands = []


# ────────────────────────────────────────────────────────────────────
# get_excluded_command_match
# ────────────────────────────────────────────────────────────────────

class TestGetExcludedMatch:
    def test_returns_pattern_and_message_when_hit(self):
        result = get_excluded_command_match("git commit -m x")
        assert result is not None
        pattern, message = result
        assert pattern == "git commit"
        assert "git commit" in message
        assert "跳过 OS 沙箱" in message

    def test_returns_none_when_no_hit(self):
        result = get_excluded_command_match("ls -la")
        assert result is None

    def test_returns_none_when_empty_command(self):
        assert get_excluded_command_match("") is None

    def test_first_match_wins_when_multiple(self):
        # config: ["git commit", "npm publish"]
        # "git commit" 在前 → 应该返回它
        result = get_excluded_command_match("git commit; npm publish")
        assert result is not None
        assert result[0] == "git commit"

    def test_substring_match_case_sensitive(self):
        # 大小写敏感 — 大写不命中
        assert get_excluded_command_match("GIT COMMIT -m x") is None
        # 小写命中
        assert get_excluded_command_match("git commit -m x") is not None

    def test_skip_none_or_empty_pattern(self):
        sandbox_manager._config.excluded_commands = [None, "", "  ", "git"]
        # 不崩,只命中 "git"
        result = get_excluded_command_match("git status")
        assert result is not None
        assert result[0] == "git"

    def test_message_mentions_ux_not_security(self):
        # message 含 "权限" 或 "UX" 语义
        result = get_excluded_command_match("git commit -m x")
        assert result is not None
        msg = result[1]
        # 应提到应用层权限检查(对齐 §5.3 "UX 而非安全")
        assert "权限" in msg or "权限检查" in msg or "应用层" in msg


# ────────────────────────────────────────────────────────────────────
# get_excluded_command_message — 便捷封装
# ────────────────────────────────────────────────────────────────────

class TestGetExcludedMessage:
    def test_returns_message_only(self):
        msg = get_excluded_command_message("git commit -m x")
        assert msg is not None
        assert "git commit" in msg
        assert "跳过 OS 沙箱" in msg

    def test_returns_none_when_no_match(self):
        assert get_excluded_command_message("ls") is None

    def test_returns_none_when_empty(self):
        assert get_excluded_command_message("") is None


# ────────────────────────────────────────────────────────────────────
# _is_excluded_command — 向后兼容(M2 bool 返回值不变)
# ────────────────────────────────────────────────────────────────────

class TestIsExcludedCommandBackwardCompat:
    def test_returns_true_on_hit(self):
        assert _is_excluded_command("Bash", {"command": "git commit -m x"}) is True

    def test_returns_false_on_no_hit(self):
        assert _is_excluded_command("Bash", {"command": "ls -la"}) is False

    def test_returns_false_for_non_bash(self):
        # Read / Write / Edit tool 不检查
        assert _is_excluded_command("Read", {"command": "git commit"}) is False
        assert _is_excluded_command("Write", {"command": "git commit"}) is False

    def test_uses_new_helper(self):
        # M3 后 _is_excluded_command 内部用 get_excluded_command_match
        # 两者必须返回一致
        cmd = "git commit -m hello"
        match = get_excluded_command_match(cmd)
        assert (_is_excluded_command("Bash", {"command": cmd})) == (match is not None)

    def test_empty_command_returns_false(self):
        assert _is_excluded_command("Bash", {"command": ""}) is False
        assert _is_excluded_command("Bash", {}) is False


# ────────────────────────────────────────────────────────────────────
# save_excluded_commands / load_excluded_commands roundtrip
# ────────────────────────────────────────────────────────────────────

class TestSaveLoadExcludedCommands:
    def test_save_writes_settings_json(self, tmp_path, monkeypatch):
        # mock get_settings_path → tmp_path
        settings_path = tmp_path / "settings.json"
        monkeypatch.setattr(
            "agent_core.tools.permission_loader.get_settings_path",
            lambda: settings_path,
        )
        # 同样要 mock _settings_for_destination(它内部用 get_settings_path)
        # 因为 _settings_for_destination 在同一 module 里,我们直接 patch 它
        monkeypatch.setattr(
            "agent_core.tools.permission_loader._settings_for_destination",
            lambda dest: settings_path,
        )

        save_excluded_commands(
            ["git commit", "npm publish"],
            PermissionRuleSource.PROJECT,
        )

        assert settings_path.exists()
        data = json.loads(settings_path.read_text("utf-8"))
        assert "sandbox" in data
        assert data["sandbox"]["excludedCommands"] == ["git commit", "npm publish"]

    def test_save_strips_whitespace_and_drops_empty(self, tmp_path, monkeypatch):
        settings_path = tmp_path / "settings.json"
        monkeypatch.setattr(
            "agent_core.tools.permission_loader._settings_for_destination",
            lambda dest: settings_path,
        )

        save_excluded_commands(
            ["  git commit  ", "", "  ", "npm publish", None, 123],
            PermissionRuleSource.PROJECT,
        )

        data = json.loads(settings_path.read_text("utf-8"))
        # 空串 / 空白 / None / 非字符串都被过滤
        assert data["sandbox"]["excludedCommands"] == ["git commit", "npm publish"]

    def test_load_returns_empty_when_no_file(self, tmp_path, monkeypatch):
        nonexistent = tmp_path / "nope.json"
        monkeypatch.setattr(
            "agent_core.tools.permission_loader._settings_for_destination",
            lambda dest: nonexistent,
        )
        assert load_excluded_commands(PermissionRuleSource.PROJECT) == []

    def test_load_roundtrip(self, tmp_path, monkeypatch):
        settings_path = tmp_path / "settings.json"
        monkeypatch.setattr(
            "agent_core.tools.permission_loader._settings_for_destination",
            lambda dest: settings_path,
        )

        original = ["git commit", "docker push", "kubectl apply"]
        save_excluded_commands(original, PermissionRuleSource.PROJECT)

        loaded = load_excluded_commands(PermissionRuleSource.PROJECT)
        assert loaded == original

    def test_load_handles_missing_sandbox_key(self, tmp_path, monkeypatch):
        settings_path = tmp_path / "settings.json"
        settings_path.write_text(json.dumps({"permissions": {"allow": ["Read"]}}),
                                 encoding="utf-8")
        monkeypatch.setattr(
            "agent_core.tools.permission_loader._settings_for_destination",
            lambda dest: settings_path,
        )
        # 没有 sandbox 段 → 返空
        assert load_excluded_commands(PermissionRuleSource.PROJECT) == []

    def test_load_filters_non_string_entries(self, tmp_path, monkeypatch):
        settings_path = tmp_path / "settings.json"
        settings_path.write_text(json.dumps({
            "sandbox": {"excludedCommands": ["git commit", None, 42, "  ", "npm"]},
        }), encoding="utf-8")
        monkeypatch.setattr(
            "agent_core.tools.permission_loader._settings_for_destination",
            lambda dest: settings_path,
        )
        loaded = load_excluded_commands(PermissionRuleSource.PROJECT)
        # 只保留非空字符串
        assert loaded == ["git commit", "npm"]

    def test_save_preserves_other_settings_keys(self, tmp_path, monkeypatch):
        settings_path = tmp_path / "settings.json"
        # 已有 permissions 段
        settings_path.write_text(json.dumps({
            "permissions": {"allow": ["Read"], "deny": ["Bash(rm:*)"]},
        }), encoding="utf-8")
        monkeypatch.setattr(
            "agent_core.tools.permission_loader._settings_for_destination",
            lambda dest: settings_path,
        )

        save_excluded_commands(["git"], PermissionRuleSource.PROJECT)

        data = json.loads(settings_path.read_text("utf-8"))
        # 原 permissions 段保留
        assert data["permissions"]["allow"] == ["Read"]
        assert data["permissions"]["deny"] == ["Bash(rm:*)"]
        # 新增 sandbox 段
        assert data["sandbox"]["excludedCommands"] == ["git"]


# ────────────────────────────────────────────────────────────────────
# 集成:消息化 helper + _is_excluded_command 行为一致
# ────────────────────────────────────────────────────────────────────

class TestIntegration:
    def test_should_use_sandbox_with_excluded(self):
        """被排除的命令:should_use_sandbox 返 False,但应用层仍过 permission check"""
        from agent_core.tools.sandbox_decision import should_use_sandbox

        # 直接设全局 singleton 上的 _initialized 跳过依赖检查
        sandbox_manager._initialized = True
        sandbox_manager._config.enabled = True
        sandbox_manager._config.excluded_commands = ["git commit"]

        # 应该被排除 → 不走沙箱
        assert should_use_sandbox("Bash", {"command": "git commit -m x"}) is False
        # 不在排除列表 → 走沙箱
        assert should_use_sandbox("Bash", {"command": "ls -la"}) is True

        # reset
        sandbox_manager._initialized = False
        sandbox_manager._config.enabled = False

    def test_message_visible_to_model_via_get(self):
        """模型/UI 可调 get_excluded_command_message 拿提示语"""
        msg = get_excluded_command_message("git commit --amend")
        assert msg is not None
        # 含 pattern 引用 + 跳过沙箱说明 + 权限提示(对齐 UX 而非安全)
        assert "git commit" in msg
        assert "跳过 OS 沙箱" in msg
        assert ("权限" in msg or "应用层" in msg)