"""tests/test_skill_frontmatter.py — SKILL.md 解析器（T010）"""
from __future__ import annotations

import pytest

from agent_core.skills.frontmatter import parse_skill_md


VALID = """---
name: hello
description: "Greet the user warmly."
---

# Hello

Always say hi.
"""

NO_DESC = """---
name: hello
---

body
"""

BAD_YAML = """---
name: hello
description: "ok"
  bad: : : indent
---

body
"""

NO_FRONTMATTER = """This is just markdown, no frontmatter at all."""


class TestParseSkillMd:
    def test_valid(self):
        r = parse_skill_md(VALID, fallback_name="hello")
        assert r.ok
        assert r.frontmatter["name"] == "hello"
        assert r.frontmatter["description"] == "Greet the user warmly."
        assert "# Hello" in r.body
        assert r.error is None

    def test_missing_description(self):
        # FR-003：缺 description → 解析失败
        r = parse_skill_md(NO_DESC, fallback_name="hello")
        assert not r.ok
        assert r.frontmatter is None
        assert "description" in (r.error or "")

    def test_corrupted_yaml(self):
        # FR-005：YAML 损坏 → 优雅失败
        r = parse_skill_md(BAD_YAML, fallback_name="hello")
        assert not r.ok
        assert r.error is not None

    def test_no_frontmatter_prefix(self):
        r = parse_skill_md(NO_FRONTMATTER, fallback_name="x")
        assert not r.ok
        assert "frontmatter" in (r.error or "").lower()

    def test_name_fallback_to_dir(self):
        content = """---
description: "no name field"
---

body
"""
        r = parse_skill_md(content, fallback_name="mydir")
        assert r.ok
        assert r.frontmatter["name"] == "mydir"

    def test_crlf_line_endings_tolerated(self):
        crlf = VALID.replace("\n", "\r\n")
        r = parse_skill_md(crlf, fallback_name="hello")
        assert r.ok
        assert r.frontmatter["name"] == "hello"

    def test_only_frontmatter_marker(self):
        # 极端：只有 ---
        r = parse_skill_md("---", fallback_name="x")
        assert not r.ok


class TestContractMust:  # T031 — contracts/skill-md-format.md 全部 MUST 覆盖
    """契约 MUST 条款的执行（缺 desc 拒绝 / YAML 损坏 skip / 超限 / 未知字段忽略）"""

    def test_unknown_fields_ignored(self):
        # 前向兼容：未知字段（如 primaryEnv/install/apiKey）静默忽略，不拒绝
        content = """---
name: foo
description: "ok"
primaryEnv: FOO_API_KEY
install: "npm install"
apiKey: "secret"
unknownTopLevelField: 42
---

body
"""
        r = parse_skill_md(content, fallback_name="foo")
        assert r.ok
        assert r.frontmatter["name"] == "foo"
        # 未知字段保留在 dict 中但不影响解析（loader 只取已知字段）
        assert r.frontmatter["description"] == "ok"

    def test_description_non_string_rejected(self):
        # description 必须是 str；数字 / 列表 → 拒绝（FR-003）
        content = """---
name: foo
description: 123
---

body
"""
        r = parse_skill_md(content, fallback_name="foo")
        assert not r.ok
        assert "description" in (r.error or "")

    def test_description_blank_rejected(self):
        # 空白 description 视同缺失
        content = '---\nname: foo\ndescription: "   "\n---\nbody\n'
        r = parse_skill_md(content, fallback_name="foo")
        assert not r.ok
        assert "description" in (r.error or "")

    def test_homepage_invalid_type_rejected(self):
        # homepage 非法类型（非 str）→ 拒绝（契约 §3 homepage）
        content = '---\nname: foo\ndescription: "ok"\nhomepage: 123\n---\nbody\n'
        r = parse_skill_md(content, fallback_name="foo")
        assert not r.ok
        assert "homepage" in (r.error or "")

    def test_empty_frontmatter_dict_rejected(self):
        # frontmatter 解析为空 dict / 非 dict → 拒绝
        # 用一个仅含注释、无字段的 frontmatter（description 缺失）
        content = "---\n# only a comment\n---\nbody\n"
        r = parse_skill_md(content, fallback_name="foo")
        assert not r.ok
        assert "description" in (r.error or "")
