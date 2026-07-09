"""tests/test_skill_prompt_render.py — prompt 渲染 + 字节稳定性（T022, INV-2）"""
from __future__ import annotations

import pytest

from agent_core.skills.prompt import escape_xml, render_skills_section
from agent_core.skills.snapshot import build_snapshot
from agent_core.skills.types import (
    Skill,
    SkillEntry,
    SkillRenderMode,
    SkillSource,
    SkillVisibility,
)


def _entry(name: str, desc: str = "d", path: str = "/x/SKILL.md") -> SkillEntry:
    return SkillEntry(
        skill=Skill(
            name=name, description=desc,
            file_path=path, base_dir="/x", source=SkillSource.WORKSPACE,
        )
    )


class TestEscapeXml:
    def test_basic(self):
        assert escape_xml("a&b") == "a&amp;b"
        assert escape_xml("<x>") == "&lt;x&gt;"
        assert escape_xml('"q"') == "&quot;q&quot;"
        assert escape_xml("it's") == "it&apos;s"

    def test_plain_unchanged(self):
        assert escape_xml("hello world") == "hello world"


class TestRenderFull:
    def test_empty_entries_returns_empty(self):
        assert render_skills_section([]) == ""

    def test_full_format_contains_fields(self):
        out = render_skills_section([_entry("hello", "Greet", "/a/SKILL.md")])
        assert "## Skills (mandatory)" in out
        assert "<available_skills>" in out
        assert "<name>hello</name>" in out
        assert "<description>Greet</description>" in out
        assert "<location>/a/SKILL.md</location>" in out

    def test_full_header_instructions(self):
        out = render_skills_section([_entry("x")])
        # FR-009：段头必含的关键指令
        assert "at most one" in out.lower() or "至多" in out
        assert "Read" in out  # 提到 Read 工具

    def test_xml_escaped_in_content(self):
        out = render_skills_section([_entry("x", desc="<evil> & stuff")])
        assert "<evil>" not in out.split("<description>")[1].split("</description>")[0]
        assert "&lt;evil&gt;" in out


class TestRenderCompact:
    def test_compact_omits_description(self):
        out = render_skills_section([_entry("hello", "Greet")], mode=SkillRenderMode.COMPACT)
        assert "<name>hello</name>" in out
        assert "<location>" in out
        assert "<description>" not in out

    def test_compact_header_says_name_not_description(self):
        out = render_skills_section([_entry("x")], mode=SkillRenderMode.COMPACT)
        # compact 段头提示按 name 匹配
        assert "name" in out.lower()


class TestByteStability:
    """INV-2：相同 entry 集合 → 相同输出字节"""

    def test_same_entries_same_hash(self):
        entries = [_entry(f"s{i}", f"desc {i}") for i in range(10)]
        out1 = render_skills_section(entries)
        out2 = render_skills_section(entries)
        assert hash(out1) == hash(out2)

    def test_order_independent_of_input(self):
        # 调用方应已排序；render 本身按输入顺序。相同集合（已排序）→ 稳定。
        entries_a = sorted(
            [_entry("c"), _entry("a"), _entry("b")],
            key=lambda e: e.skill.name,
        )
        entries_b = sorted(
            [_entry("b"), _entry("c"), _entry("a")],
            key=lambda e: e.skill.name,
        )
        assert render_skills_section(entries_a) == render_skills_section(entries_b)

    def test_adding_unrelated_preserves_relative_order(self):
        # 增删一个不扰动其余顺序（SC-002）
        import re
        base = [_entry(f"s{i}") for i in range(5)]
        added_entries = base + [_entry("s_new")]
        out_before = render_skills_section(base)
        added = render_skills_section(added_entries)
        # 提取 name 序列，验证前 5 个顺序不变（substring 断言会因闭合标签位移失败）
        names_before = re.findall(r"<name>(\w+)</name>", out_before)
        names_added = re.findall(r"<name>(\w+)</name>", added)
        assert names_before == names_added[:5]
        assert names_added[-1] == "s_new"


class TestVisibilityFilter:  # T040 — disable-model-invocation 不进 prompt 但 status 列出
    """US3 visibility：HIDDEN_FROM_MODEL 不进 <available_skills>，但仍登记可 /invoke"""

    def _hidden(self, name: str = "secret") -> SkillEntry:
        return SkillEntry(
            skill=Skill(name=name, description="d", file_path=f"/x/{name}/SKILL.md",
                        base_dir="/x", source=SkillSource.WORKSPACE),
            disable_model_invocation=True,
            visibility=SkillVisibility.HIDDEN_FROM_MODEL,
            user_invocable=True,
        )

    def test_hidden_excluded_from_prompt(self):
        visible = _entry("public")
        hidden = self._hidden("secret")
        snap = build_snapshot([visible, hidden], env={})
        assert "public" in snap.prompt
        assert "secret" not in snap.prompt

    def test_hidden_still_in_summary(self):
        # status 仍列出 hidden skill（可 /invoke）
        visible = _entry("public")
        hidden = self._hidden("secret")
        snap = build_snapshot([visible, hidden], env={})
        names = {s.name for s in snap.skills}
        assert "secret" in names
        assert "public" in names

    def test_hidden_summary_eligible_true(self):
        # hidden 但满足 requires → summary.eligible True（仅 visibility 隐藏，非资格问题）
        hidden = self._hidden("secret")
        snap = build_snapshot([hidden], env={})
        summary = next(s for s in snap.skills if s.name == "secret")
        assert summary.eligible is True

    def test_all_hidden_empty_prompt(self):
        snap = build_snapshot([self._hidden("a"), self._hidden("b")], env={})
        assert snap.prompt == ""  # Edge case：零 model-visible → 不注入
        # summary 仍含两者
        assert {s.name for s in snap.skills} == {"a", "b"}

    def test_user_invocable_false_does_not_affect_prompt(self):
        # user-invocable=false 仅影响 slash 命令暴露，不影响 prompt 可见性
        e = SkillEntry(
            skill=Skill(name="noslash", description="d", file_path="/x/SKILL.md",
                        base_dir="/x", source=SkillSource.WORKSPACE),
            user_invocable=False,
        )
        snap = build_snapshot([e], env={})
        assert "noslash" in snap.prompt  # 仍进 prompt（仅不可 /invoke）
