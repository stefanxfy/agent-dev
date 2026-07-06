"""tests/test_read_tool.py — Read 工具（T025, FR-011/012, C3）"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from agent_core.tools.base import ToolRegistry
from agent_core.tools.builtin import READ_TOOL, register_builtin_tools


class TestReadToolDef:
    def test_name_is_Read(self):
        assert READ_TOOL.name == "Read"  # 与 permission/safety 已硬编码的 "Read" 对齐（C3）

    def test_category_is_read(self):
        assert READ_TOOL.category == "read"

    def test_parameters_have_path_required(self):
        assert READ_TOOL.parameters["properties"]["path"]["type"] == "string"
        assert "path" in READ_TOOL.parameters["required"]

    def test_registered_via_register_builtin(self):
        reg = ToolRegistry()
        register_builtin_tools(reg)
        assert "Read" in reg.list_names()
        assert reg.get("Read") is READ_TOOL


class TestReadHandler:
    def test_reads_existing_file(self, tmp_path):
        f = tmp_path / "x.txt"
        f.write_text("hello world", encoding="utf-8")
        out = READ_TOOL.handler(path=str(f))
        assert out == "hello world"

    def test_missing_path_arg(self):
        out = READ_TOOL.handler()
        assert "错误" in out or "缺少" in out

    def test_missing_file(self):
        out = READ_TOOL.handler(path=str(Path("/nonexistent") / "nope.md"))
        assert "错误" in out
        assert "不存在" in out or "not" in out.lower()

    def test_directory_not_file(self, tmp_path):
        out = READ_TOOL.handler(path=str(tmp_path))
        assert "错误" in out

    def test_oversize_rejected(self, tmp_path):
        f = tmp_path / "big.txt"
        f.write_text("x" * 300_000, encoding="utf-8")
        out = READ_TOOL.handler(path=str(f))
        assert "错误" in out
        assert "大" in out or "exceed" in out.lower() or "上限" in out

    def test_returns_string_never_raises(self, tmp_path):
        # 关键不变量：所有失败返回错误串，不抛（与 calc/bash 一致）
        # 权限拒绝场景由 permission_engine 在外层处理，handler 不重复
        for bad_path in ["", "/nonexistent/x", str(tmp_path)]:
            out = READ_TOOL.handler(path=bad_path)
            assert isinstance(out, str)

    def test_expanduser(self, monkeypatch, tmp_path):
        # ~ 展开
        monkeypatch.setenv("HOME", str(tmp_path))
        f = tmp_path / "tilde.txt"
        f.write_text("tilde ok", encoding="utf-8")
        out = READ_TOOL.handler(path="~/tilde.txt")
        assert out == "tilde ok"

    @pytest.mark.skipif(os.name == "nt", reason="symlink 不稳定 on Windows")
    def test_symlink_rejected(self, tmp_path):
        real = tmp_path / "real.txt"
        real.write_text("real", encoding="utf-8")
        link = tmp_path / "link.txt"
        try:
            os.symlink(real, link)
        except (OSError, PermissionError):
            pytest.skip("cannot create symlink")
        out = READ_TOOL.handler(path=str(link))
        assert "错误" in out
        assert "symlink" in out.lower()

    def test_reads_actual_skill_md(self):
        # 端到端：读真实的 bundled skill-creator
        out = READ_TOOL.handler(path="agent_core/skills/builtin/skill-creator/SKILL.md")
        assert "skill-creator" in out
        assert "description" in out  # frontmatter 字段
