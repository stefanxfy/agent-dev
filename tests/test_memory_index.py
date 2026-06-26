"""
MemoryIndex + scan_memory_files 测试
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest


@pytest.fixture
def memory_root_with_entries(tmp_path):
    """准备 5 条记忆(2 user + 1 feedback + 1 project + 1 reference)"""
    root = tmp_path / "memory"
    for t in ("user", "feedback", "project", "reference"):
        (root / t).mkdir(parents=True)
    fm_template = """---
type: {type}
created_at: 2026-06-26T00:00:00+00:00
item_hash: {hash}
schema_version: 3
name: {name}
description: {desc}
title: {name}
tags: []
---
body for {name}
"""
    entries = [
        ("user", "a" * 64, "记忆1", "第一条描述"),
        ("user", "b" * 64, "记忆2", "第二条描述"),
        ("feedback", "c" * 64, "反馈1", "反馈描述"),
        ("project", "d" * 64, "项目1", "项目描述"),
        ("reference", "e" * 64, "参考1", "参考描述"),
    ]
    for t, h, name, desc in entries:
        (root / t / f"{h}.md").write_text(
            fm_template.format(type=t, hash=h, name=name, desc=desc),
            encoding="utf-8",
        )
    return root, entries


def test_memory_index_rebuild_writes_file(memory_root_with_entries):
    """rebuild() 生成 MEMORY.md"""
    from agent_core.memory.memory_index import MemoryIndex
    root, _ = memory_root_with_entries
    idx = MemoryIndex(root)
    idx.rebuild()
    assert (root / "MEMORY.md").exists()
    content = (root / "MEMORY.md").read_text(encoding="utf-8")
    assert "# Agent Memory (auto-generated)" in content
    for _, _, name, _desc in memory_root_with_entries[1]:
        assert f"[{name}]" in content


def test_memory_index_max_200_lines(tmp_path):
    """>200 条记忆时 MEMORY.md ≤ 200 行"""
    from agent_core.memory.memory_index import MemoryIndex, MAX_ENTRYPOINT_LINES
    root = tmp_path / "memory"
    (root / "user").mkdir(parents=True)
    for i in range(250):
        h = f"{i:064x}"
        (root / "user" / f"{h}.md").write_text(
            f"---\ntype: user\ncreated_at: 2026-06-26T00:00:00+00:00\n"
            f"item_hash: {h}\nschema_version: 3\n"
            f"name: 记忆{i}\ndescription: 描述{i}\ntitle: 记忆{i}\ntags: []\n---\nbody\n",
            encoding="utf-8",
        )
    idx = MemoryIndex(root)
    idx.rebuild()
    content = (root / "MEMORY.md").read_text(encoding="utf-8")
    assert len(content.splitlines()) <= MAX_ENTRYPOINT_LINES


def test_memory_index_mark_dirty_1s_coalesce(memory_root_with_entries):
    """mark_dirty 1s 内多次调用 → 只 rebuild 1 次"""
    from agent_core.memory.memory_index import MemoryIndex
    root, _ = memory_root_with_entries
    idx = MemoryIndex(root)
    rebuild_count = [0]
    original_rebuild = idx.rebuild
    def counting_rebuild():
        rebuild_count[0] += 1
        original_rebuild()
    idx.rebuild = counting_rebuild  # type: ignore[method-assign]
    for _ in range(10):
        idx.mark_dirty()
    time.sleep(1.2)
    # 至少 1 次, 但不应 > 2(只有 1 个 Timer)
    assert 1 <= rebuild_count[0] <= 2


def test_memory_index_flush_cancels_timer(memory_root_with_entries):
    """flush() 立即 rebuild 并取消 pending timer"""
    from agent_core.memory.memory_index import MemoryIndex
    root, _ = memory_root_with_entries
    idx = MemoryIndex(root)
    idx.mark_dirty()
    idx.flush()  # 立即 rebuild
    assert idx._pending is False  # type: ignore[attr-defined]
    # 此时 MEMORY.md 已存在
    assert (root / "MEMORY.md").exists()


def test_load_index_returns_content(memory_root_with_entries):
    """load_index() 同步返回 MEMORY.md 字符串"""
    from agent_core.memory.memory_index import MemoryIndex
    root, _ = memory_root_with_entries
    idx = MemoryIndex(root)
    idx.rebuild()
    content = idx.load_index()
    assert "记忆1" in content


def test_scan_memory_files_respects_types_filter(tmp_path):
    """types_filter 只扫指定 type"""
    from agent_core.memory.memory_index import scan_memory_files
    root = tmp_path / "memory"
    for t in ("user", "feedback", "project"):
        (root / t).mkdir(parents=True)
        (root / t / f"{t}1.md").write_text(
            f"---\ntype: {t}\nname: n\ndescription: d\nschema_version: 3\n"
            f"item_hash: {'x'*64}\ncreated_at: 2026-06-26T00:00:00+00:00\n---\nbody\n",
            encoding="utf-8",
        )
    entries = scan_memory_files(root, types_filter=["user"])
    assert len(entries) == 1
    assert entries[0].type == "user"


def test_scan_memory_files_sorted_by_mtime_desc(tmp_path):
    """按 mtime 倒序"""
    from agent_core.memory.memory_index import scan_memory_files
    root = tmp_path / "memory"
    user_dir = root / "user"
    user_dir.mkdir(parents=True)
    for i, name in enumerate(["old", "mid", "new"]):
        p = user_dir / f"{i}.md"
        p.write_text(
            f"---\ntype: user\nname: {name}\ndescription: d\nschema_version: 3\n"
            f"item_hash: {'y'*64}\ncreated_at: 2026-06-26T00:00:00+00:00\n---\nb\n",
            encoding="utf-8",
        )
        time.sleep(0.01)  # 错开 mtime
    entries = scan_memory_files(root)
    assert [e.name for e in entries] == ["new", "mid", "old"]


def test_scan_memory_files_quoted_description(tmp_path):
    """T9:quoted description 用 yaml 解析正确(T4 极简版会失败)"""
    from agent_core.memory.memory_index import scan_memory_files
    root = tmp_path / "memory"
    user_dir = root / "user"
    user_dir.mkdir(parents=True)
    (user_dir / "a.md").write_text(
        f"""---
type: user
name: 'has: colon'
description: "含 冒号 : 和 #hash 都不应截断"
schema_version: 3
item_hash: {'q'*64}
created_at: 2026-06-26T00:00:00+00:00
---
body""",
        encoding="utf-8",
    )
    entries = scan_memory_files(root)
    assert len(entries) == 1
    assert entries[0].name == "has: colon"
    assert "含 冒号" in entries[0].description


def test_format_memory_manifest_in_memory_index_module():
    """T9:format_memory_manifest 可从 memory_index 模块直接 import"""
    from agent_core.memory.memory_index import (
        MemoryFileEntry, format_memory_manifest,
    )
    entries = [
        MemoryFileEntry(rel_path="user/abc.md", name="用户",
                        description="小明", type="user", mtime_ms=0),
    ]
    out = format_memory_manifest(entries)
    assert out == "- [用户](user/abc.md) — 小明"


def test_format_memory_manifest_renders_correctly():
    """format_memory_manifest 渲染格式对齐 CC"""
    from agent_core.memory.memory_index import MemoryFileEntry, format_memory_manifest
    entries = [
        MemoryFileEntry(rel_path="user/abc.md", name="用户",
                        description="小明", type="user", mtime_ms=0),
        MemoryFileEntry(rel_path="feedback/xyz.md", name="反馈",
                        description="不要 mock", type="feedback", mtime_ms=0),
    ]
    out = format_memory_manifest(entries)
    assert out == (
        "- [用户](user/abc.md) — 小明\n"
        "- [反馈](feedback/xyz.md) — 不要 mock"
    )