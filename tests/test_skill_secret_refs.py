"""
T010 + T021 — tests/test_skill_secret_refs.py

002-skill-secret-injection: resolve_secret 单元测试。

覆盖(US1 T010, 2 用例):
1. INLINE 返字面值
2. SECRET_REF 抛 NotImplementedError(v1.1 vault)

US3 T021 (4 用例新增):
3. ENV kind 返 os.environ[name]
4. ENV kind 未设抛 SecretResolutionError
5. FILE kind 读文件 .strip() 去掉首尾空白
6. FILE kind 文件不存在抛 SecretResolutionError
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from agent_core.skills.env_overrides import (
    SecretRef,
    SecretResolutionError,
    SecretRefKind,
    resolve_secret,
)


def test_resolve_inline_returns_value_verbatim():
    """US1 T007: INLINE kind → 返 ref.value 字面值, 不做变换。"""
    ref = SecretRef(kind=SecretRefKind.INLINE, value="inline_token_abc123")
    assert resolve_secret(ref) == "inline_token_abc123"


def test_resolve_secret_ref_raises_not_implemented():
    """US1 T007: SECRET_REF → raise NotImplementedError(v1.1 vault 保留口)。"""
    ref = SecretRef(kind=SecretRefKind.SECRET_REF, value="vault://any")
    with pytest.raises(NotImplementedError) as exc_info:
        resolve_secret(ref)
    assert "v1.1" in str(exc_info.value)


# ──────────────────────────────────────────────────────────────────
# T021 — US3 ENV / FILE 形式测试 (4 用例)
# ──────────────────────────────────────────────────────────────────

def test_resolve_env_returns_os_environ_value(monkeypatch):
    """US3 T018: ENV kind → 返 os.environ[ref.value] 字面值, 不 strip。"""
    monkeypatch.setenv("RAW_SHELL_KEY", "raw_value_xyz")
    ref = SecretRef(kind=SecretRefKind.ENV, value="RAW_SHELL_KEY")
    assert resolve_secret(ref) == "raw_value_xyz"


def test_resolve_env_preserves_whitespace(monkeypatch):
    """US3 T018: ENV kind 不 strip 空白(与 shell 行为一致)。"""
    monkeypatch.setenv("WHITESPACE_KEY", "  padded_value  ")
    ref = SecretRef(kind=SecretRefKind.ENV, value="WHITESPACE_KEY")
    # 保留首尾空白
    assert resolve_secret(ref) == "  padded_value  "


def test_resolve_env_missing_raises_secret_resolution_error(monkeypatch):
    """US3 T018: ENV kind shell 未设 → raise SecretResolutionError。

    FR-004 case 2 + FR-012: 解析失败由 apply 路径 warn+skip,不 fail-fast。
    这里测 resolve_secret 直调行为(直调 fail-fast,raise 给 caller 决定)。
    """
    monkeypatch.delenv("DEFINITELY_UNSET_VAR_XYZ", raising=False)
    ref = SecretRef(kind=SecretRefKind.ENV, value="DEFINITELY_UNSET_VAR_XYZ")
    with pytest.raises(SecretResolutionError) as exc_info:
        resolve_secret(ref)
    assert "DEFINITELY_UNSET_VAR_XYZ" in str(exc_info.value)
    assert "not set" in str(exc_info.value)


def test_resolve_file_reads_and_strips_content(tmp_path):
    """US3 T019: FILE kind → 读文件内容, .strip() 移除首尾空白(含 trailing newline)。"""
    secret_file = tmp_path / "github_token"
    secret_file.write_text("  ghp_file_value\n")
    ref = SecretRef(kind=SecretRefKind.FILE, value=str(secret_file))
    assert resolve_secret(ref) == "ghp_file_value"


def test_resolve_file_strip_handles_multiline(tmp_path):
    """US3 T019: FILE kind 单行 strip; 多行只 strip 首尾, 内部 newline 保留。"""
    secret_file = tmp_path / "multiline_token"
    secret_file.write_text("\n  first_line\nsecond_line\n  \n")
    ref = SecretRef(kind=SecretRefKind.FILE, value=str(secret_file))
    # .strip() 只去首尾空白, 内部 \n 保留
    assert resolve_secret(ref) == "first_line\nsecond_line"


def test_resolve_file_missing_raises_secret_resolution_error(tmp_path):
    """US3 T019: FILE kind 文件不存在 → raise SecretResolutionError。"""
    missing_path = tmp_path / "nonexistent_secret_file_xyz"
    ref = SecretRef(kind=SecretRefKind.FILE, value=str(missing_path))
    with pytest.raises(SecretResolutionError) as exc_info:
        resolve_secret(ref)
    assert "unreadable" in str(exc_info.value)
    assert str(missing_path) in str(exc_info.value)