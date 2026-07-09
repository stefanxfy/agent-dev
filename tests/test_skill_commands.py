"""tests/test_skill_commands.py — slash 命令解析（T045, FR-020）"""
from __future__ import annotations

import pytest

from agent_core.skills.commands import resolve_skill_command, sanitize_skill_command_name


class TestSanitize:
    def test_lowercase(self):
        assert sanitize_skill_command_name("HELLO") == "hello"

    def test_non_alnum_to_underscore(self):
        assert sanitize_skill_command_name("hello-world") == "hello_world"
        assert sanitize_skill_command_name("foo.bar baz") == "foo_bar_baz"

    def test_already_clean(self):
        assert sanitize_skill_command_name("my_skill_42") == "my_skill_42"

    def test_truncates_to_32(self):
        long = "a" * 50
        out = sanitize_skill_command_name(long)
        assert len(out) == 32

    def test_all_special_chars(self):
        assert sanitize_skill_command_name("!!!") == "___"


class TestResolveSlash:
    def test_name_with_args(self):
        assert resolve_skill_command("/hello please greet") == ("hello", "please greet")

    def test_name_only_no_args(self):
        assert resolve_skill_command("/hello") == ("hello", "")

    def test_args_trimmed(self):
        # 多空格 / tab / 前后空白 → args strip
        name, args = resolve_skill_command("/hello    multiple   spaces   ")
        assert name == "hello"
        assert args == "multiple   spaces"  # 内部多空格保留，仅去首尾

    def test_sanitize_in_name(self):
        # "nonexistent-skill" → sanitize → "nonexistent_skill"
        name, args = resolve_skill_command("/nonexistent-skill x")
        assert name == "nonexistent_skill"
        assert args == "x"

    def test_multiline_args(self):
        name, args = resolve_skill_command("/foo line1\nline2")
        assert name == "foo"
        assert "line1" in args and "line2" in args

    def test_uppercase_command(self):
        assert resolve_skill_command("/HELLO world") == ("hello", "world")


class TestResolveNonSlash:
    def test_plain_text_none(self):
        assert resolve_skill_command("hello there") is None

    def test_empty_string_none(self):
        assert resolve_skill_command("") is None

    def test_only_slash_none(self):
        assert resolve_skill_command("/") is None

    def test_slash_then_space_none(self):
        # "/ foo"（/ 后直接空白）→ 非命令
        assert resolve_skill_command("/ foo") is None

    def test_not_string_none(self):
        assert resolve_skill_command(None) is None  # type: ignore[arg-type]
        assert resolve_skill_command(123) is None  # type: ignore[arg-type]

    def test_all_special_name_none(self):
        # sanitize 后全下划线 → 视为无名 → None
        assert resolve_skill_command("/!!!") is None

    def test_leading_whitespace_stripped(self):
        # 前导空白后 / 仍识别
        assert resolve_skill_command("   /hello x") == ("hello", "x")


class TestSlashDispatchIntegration:
    """端到端：resolve 返回的 name 可被 registry.get_entry 校验（FR-020 backend）"""

    def test_resolved_name_is_sane_for_registry_lookup(self, tmp_path):
        # 真实 registry + skill：resolve 的 name 能命中 get_entry
        from agent_core.skills.config import SkillsConfig
        from agent_core.skills.registry import SkillsRegistry

        ws = tmp_path / "skills"
        ws.mkdir()
        (ws / "hello").mkdir()
        (ws / "hello" / "SKILL.md").write_text(
            "---\nname: hello\ndescription: greet\n---\nbody\n", encoding="utf-8",
        )
        cfg = SkillsConfig.from_dict({
            "paths": {"workspace_dir": str(ws), "bundled_dir": str(tmp_path / "b")},
        })
        reg = SkillsRegistry(cfg)
        name, args = resolve_skill_command("/hello please greet")
        entry = reg.get_entry(name)
        assert entry is not None
        assert entry.load_error is None
        assert entry.user_invocable is True
        assert args == "please greet"
