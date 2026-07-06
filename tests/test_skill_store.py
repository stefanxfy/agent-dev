"""tests/test_skill_store.py — 单 skill 加载器（T011）"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from agent_core.skills.skill_store import load_single_skill, resolve_body_references
from agent_core.skills.types import SkillSource


VALID_SKILL_MD = """---
name: hello
description: "Greet the user."
---

# Hello
Always say hi.
"""

NO_DESC_SKILL_MD = """---
name: broken
---

body
"""


def _write_skill(root: Path, name: str, content: str = VALID_SKILL_MD) -> Path:
    d = root / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(content, encoding="utf-8")
    return d


class TestLoadSingleSkill:
    def test_loads_valid_skill(self, tmp_path):
        d = _write_skill(tmp_path, "hello")
        entry = load_single_skill(d, SkillSource.WORKSPACE)
        assert entry.load_error is None
        assert entry.skill.name == "hello"
        assert entry.skill.description == "Greet the user."
        assert entry.skill.file_path.endswith("hello/SKILL.md")
        assert entry.skill.source == SkillSource.WORKSPACE
        assert entry.skill.base_dir.endswith("hello")

    def test_missing_skill_md(self, tmp_path):
        d = tmp_path / "empty"
        d.mkdir()
        entry = load_single_skill(d, SkillSource.WORKSPACE)
        assert entry.load_error is not None
        assert "SKILL.md" in entry.load_error
        # 占位 name 回退到目录名
        assert entry.skill.name == "empty"

    def test_malformed_frontmatter_load_error(self, tmp_path):
        d = _write_skill(tmp_path, "broken", NO_DESC_SKILL_MD)
        entry = load_single_skill(d, SkillSource.WORKSPACE)
        assert entry.load_error is not None
        assert "description" in entry.load_error

    def test_oversize_rejected(self, tmp_path):
        d = tmp_path / "big"
        d.mkdir()
        # 写一个 > max_bytes 的 SKILL.md
        big_content = "---\ndescription: x\n---\n\n" + ("x" * 300_000)
        (d / "SKILL.md").write_text(big_content, encoding="utf-8")
        entry = load_single_skill(d, SkillSource.WORKSPACE, max_bytes=256_000)
        assert entry.load_error is not None
        assert "大小上限" in entry.load_error or "超过" in entry.load_error

    def test_oversize_custom_limit(self, tmp_path):
        d = tmp_path / "medium"
        d.mkdir()
        content = "---\ndescription: x\n---\n\n" + ("y" * 500)
        (d / "SKILL.md").write_text(content, encoding="utf-8")
        # 内容 ~530 字节，限制 100 → 拒绝
        entry = load_single_skill(d, SkillSource.WORKSPACE, max_bytes=100)
        assert entry.load_error is not None
        # 不设限制 → 通过
        entry2 = load_single_skill(d, SkillSource.WORKSPACE, max_bytes=10_000)
        assert entry2.load_error is None

    @pytest.mark.skipif(
        os.name == "nt", reason="symlink 权限/语义在 Windows 不稳定"
    )
    def test_symlink_skill_md_rejected(self, tmp_path):
        # 真实 SKILL.md
        real_dir = _write_skill(tmp_path, "real")
        # symlink 指向它
        link_dir = tmp_path / "linked"
        link_dir.mkdir()
        link_md = link_dir / "SKILL.md"
        try:
            os.symlink(real_dir / "SKILL.md", link_md)
        except (OSError, PermissionError):
            pytest.skip("无法创建 symlink（权限不足）")
        entry = load_single_skill(link_dir, SkillSource.WORKSPACE)
        assert entry.load_error is not None
        assert "symlink" in entry.load_error.lower() or "安全" in entry.load_error

    def test_name_fallback_to_dirname(self, tmp_path):
        # SKILL.md 缺 name → 回退到目录名
        d = tmp_path / "fallback-name"
        d.mkdir()
        (d / "SKILL.md").write_text(
            '---\ndescription: "no name here"\n---\nbody\n',
            encoding="utf-8",
        )
        entry = load_single_skill(d, SkillSource.BUNDLED)
        assert entry.load_error is None
        assert entry.skill.name == "fallback-name"

    def test_does_not_raise_on_any_failure(self, tmp_path):
        # 关键不变量：任何单 skill 失败都不抛（SC-005）
        d = tmp_path / "no-skill-md"
        d.mkdir()
        # 不应抛异常
        entry = load_single_skill(d, SkillSource.WORKSPACE)
        assert isinstance(entry.load_error, str)


class TestBodyReferenceResolution:  # T032 — body 相对路径解析（contracts §5.2）
    """body 中 references//scripts//assets/ 引用 → 解析为 base_dir 绝对路径"""

    def test_resolves_references_scripts_assets(self, tmp_path):
        d = _write_skill(tmp_path, "refs")
        body = (
            "# Refs\n"
            "See references/cheatsheet.md and scripts/run.py and assets/logo.png.\n"
        )
        (d / "SKILL.md").write_text(
            f"---\nname: refs\ndescription: d\n---\n{body}",
            encoding="utf-8",
        )
        entry = load_single_skill(d, SkillSource.WORKSPACE)
        assert entry.load_error is None
        refs = entry.body_references
        # 三类子目录引用都被解析
        assert any(r.endswith("references/cheatsheet.md") for r in refs)
        assert any(r.endswith("scripts/run.py") for r in refs)
        assert any(r.endswith("assets/logo.png") for r in refs)
        # 全部为绝对路径（含 base_dir）
        assert all(d.name in r for r in refs)

    def test_resolves_markdown_link_and_backtick(self, tmp_path):
        d = tmp_path / "md"
        d.mkdir()
        body = (
            "see [cheat](references/a.md) and `scripts/b.sh` for details.\n"
        )
        (d / "SKILL.md").write_text(
            f"---\nname: md\ndescription: d\n---\n{body}",
            encoding="utf-8",
        )
        entry = load_single_skill(d, SkillSource.WORKSPACE)
        assert any(r.endswith("references/a.md") for r in entry.body_references)
        assert any(r.endswith("scripts/b.sh") for r in entry.body_references)

    def test_dedup_and_sorted(self, tmp_path):
        # 同一引用多次出现 → 去重；结果字典序（确定性，INV-2）
        d = tmp_path / "dup"
        d.mkdir()
        body = "references/z.md references/z.md references/a.md\n"
        (d / "SKILL.md").write_text(
            f"---\nname: dup\ndescription: d\n---\n{body}",
            encoding="utf-8",
        )
        entry = load_single_skill(d, SkillSource.WORKSPACE)
        names = [r.split("/")[-1] for r in entry.body_references]
        assert names == ["a.md", "z.md"]  # 去重 + 字典序

    def test_traversal_ref_rejected(self, tmp_path):
        # 含 .. 的引用 → 丢弃（防逃逸 base_dir）
        body = "../etc/passwd references/ok.md and ../../escape\n"
        refs = resolve_body_references(body, "/skills/my")
        # 仅 references/ok.md 被解析；.. 逃逸被丢
        assert any(r.endswith("references/ok.md") for r in refs)
        assert not any("etc/passwd" in r for r in refs)
        assert not any("escape" in r for r in refs)

    def test_no_refs_empty_tuple(self, tmp_path):
        d = _write_skill(tmp_path, "norefs")
        entry = load_single_skill(d, SkillSource.WORKSPACE)
        assert entry.body_references == ()

    def test_size_boundary_exactly_at_limit(self, tmp_path):
        # 边界：恰好等于上限 → 通过；超 1 字节 → 拒绝
        d = tmp_path / "edge"
        d.mkdir()
        head = "---\ndescription: x\n---\n\n"
        # 构造 body 使总长恰好 100
        body_len = 100 - len(head)
        (d / "SKILL.md").write_text(head + ("y" * body_len), encoding="utf-8")
        assert len((d / "SKILL.md").read_text()) == 100
        assert load_single_skill(d, SkillSource.WORKSPACE, max_bytes=100).load_error is None
        # 超 1 字节 → 拒绝
        (d / "SKILL.md").write_text(head + ("y" * (body_len + 1)), encoding="utf-8")
        assert load_single_skill(d, SkillSource.WORKSPACE, max_bytes=100).load_error is not None
