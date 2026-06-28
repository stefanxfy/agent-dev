import os
import stat
import tempfile
from pathlib import Path

import pytest

from agent_core.memory.memory_store import MemoryStore
from agent_core.memory.path_validator import PathSecurityError


def _store(tmp: Path) -> MemoryStore:
    return MemoryStore(tmp)


def _cleanup(tmp: Path) -> None:
    import shutil
    shutil.rmtree(tmp, ignore_errors=True)


def test_write_valid_path_succeeds():
    """Smoke: 合法路径写盘成功"""
    tmp = Path(tempfile.mkdtemp(prefix="mem_test_"))
    try:
        store = _store(tmp)
        item_hash = store.write(
            type="user", title="t", body="b", source_quote="q"
        )
        md = list(tmp.rglob("*.md"))
        assert len(md) == 1
        assert item_hash in md[0].read_text()
    finally:
        _cleanup(tmp)


def test_write_invalid_type_blocked_by_path_validator():
    """L1 防御 1: 非法 type (不在 4 类白名单) → validator 前置后
    在 path validator 阶段即被拒(PathSecurityError)。

    注意:reorder 之后,validate_type 仍然是 type 校验的第二道防线,
    但任何非法 type 必然不在 _ALLOWED_TOP_DIRS 白名单内,会被前置的
    path validator 拦下。这正是「security check 先于 schema check」
    的本意。
    """
    tmp = Path(tempfile.mkdtemp(prefix="mem_test_"))
    try:
        store = _store(tmp)
        with pytest.raises(PathSecurityError):
            store.write(type="bogus_type", title="t", body="b", source_quote="q")
    finally:
        _cleanup(tmp)


def test_write_path_traversal_in_type_blocked_by_path_validator():
    """关键: type 含 ../ 现在先被 path validator 拦截(PathSecurityError)"""
    tmp = Path(tempfile.mkdtemp(prefix="mem_test_"))
    try:
        store = _store(tmp)
        # 因为 validator 已前置,type 含 ../ 会先被 path validator 拦
        with pytest.raises(PathSecurityError):
            store.write(
                type="../../etc",
                title="t",
                body="b",
                source_quote="q",
            )
    finally:
        _cleanup(tmp)


def test_write_unicode_null_in_type_blocked_by_path_validator():
    """L4: type 含 \\x00 → PathSecurityError(由 path validator L4 拦)"""
    tmp = Path(tempfile.mkdtemp(prefix="mem_test_"))
    try:
        store = _store(tmp)
        with pytest.raises(PathSecurityError):
            store.write(
                type="user\x00",
                title="t",
                body="b",
                source_quote="q",
            )
    finally:
        _cleanup(tmp)


def test_write_sets_file_mode_0600():
    """§14.3: chmod 0o600(本 task 一并做 C1.3)"""
    tmp = Path(tempfile.mkdtemp(prefix="mem_test_"))
    try:
        store = _store(tmp)
        store.write(type="user", title="t", body="b", source_quote="q")
        md = list(tmp.rglob("*.md"))
        assert len(md) == 1
        mode = stat.S_IMODE(md[0].stat().st_mode)
        assert mode == 0o600, f"expected 0o600, got {oct(mode)}"
    finally:
        _cleanup(tmp)