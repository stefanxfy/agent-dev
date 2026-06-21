"""
M8 / Day 8 —— lifecycle 测试 (backup / integrity / capacity)

11 cases:
- BackupReport: 4 cases(创建/重复跳过/带 meta.db/滚动删除)
- restore_backup: 2 cases(成功恢复/备份不存在报错)
- IntegrityReport: 2 cases(健康/检测损坏 frontmatter)
- CapacityReport: 3 cases(未超阈值/超出淘汰/只淘汰 importance<min)
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest

from agent_core.memory import (
    CURRENT_SCHEMA_VERSION,
    BackupReport,
    CapacityReport,
    IntegrityReport,
    LifecycleError,
    capacity_govern,
    daily_backup,
    integrity_check,
    list_backups,
    restore_backup,
)


# ──────────────────────────────────────────────────────────────────
# Fixtures
# ──────────────────────────────────────────────────────────────────

@pytest.fixture
def fake_agent_data(tmp_path) -> dict[str, Path]:
    """模拟 ~/.agent_data/ 目录结构"""
    memory_root = tmp_path / "memory"
    user_dir = memory_root / "user"
    user_dir.mkdir(parents=True)
    meta_db = tmp_path / "meta.db"
    vector_index = tmp_path / "vector_index"
    vector_index.mkdir()
    # 写 3 个 v2 记忆
    for i in range(3):
        (user_dir / f"mem{i}.md").write_text(
            f"---\n"
            f"type: user\n"
            f"title: 测试 {i}\n"
            f"created_at: 2025-01-0{i + 1}\n"
            f"schema_version: {CURRENT_SCHEMA_VERSION}\n"
            f"item_hash: {'0' * 64}\n"
            f"importance: {5 + i}\n"
            f"---\n\nbody {i}\n",
            encoding="utf-8",
        )
    # SQLite 元数据库
    with sqlite3.connect(str(meta_db)) as conn:
        conn.execute("CREATE TABLE cursor (key TEXT PRIMARY KEY, value INTEGER)")
        conn.execute("INSERT INTO cursor VALUES ('daily', 5)")
        conn.commit()
    return {
        "memory_root": memory_root,
        "meta_db": meta_db,
        "vector_index": vector_index,
        "tmp": tmp_path,
    }


# ──────────────────────────────────────────────────────────────────
# 1. daily_backup
# ──────────────────────────────────────────────────────────────────

class TestDailyBackup:

    def test_backup_creates_directory_with_files(self, fake_agent_data):
        """首次 backup → 创建目录 + 拷贝文件"""
        r = daily_backup(
            fake_agent_data["memory_root"],
            meta_db=fake_agent_data["meta_db"],
            vector_index=fake_agent_data["vector_index"],
            today="2026-06-21",
        )
        assert r.succeeded
        assert r.backup_path.name == "2026-06-21"
        assert r.files_copied >= 4  # 3 .md + 1 meta.db
        assert r.bytes_copied > 0
        # 目录确实存在
        assert r.backup_path.exists()
        assert (r.backup_path / "memory").exists()
        assert (r.backup_path / "meta.db").exists()
        assert (r.backup_path / "vector_index").exists()

    def test_backup_is_idempotent_same_day(self, fake_agent_data):
        """同一天再跑 → skipped_reason='already exists',不覆盖"""
        r1 = daily_backup(
            fake_agent_data["memory_root"],
            today="2026-06-21",
        )
        assert r1.succeeded
        # 删 1 个原文件
        (fake_agent_data["memory_root"] / "user" / "mem0.md").unlink()
        # 再跑 backup
        r2 = daily_backup(
            fake_agent_data["memory_root"],
            today="2026-06-21",
        )
        assert not r2.succeeded
        assert r2.skipped_reason == "already exists"
        # 备份里的文件没有被新跑覆盖 → mem0.md 应仍在备份里
        assert (r1.backup_path / "memory" / "user" / "mem0.md").exists()

    def test_backup_skips_bak_files(self, fake_agent_data):
        """.bak sidecar 不被 backup"""
        bak = fake_agent_data["memory_root"] / "user" / "mem0.md.bak"
        bak.write_text("stale backup\n", encoding="utf-8")
        r = daily_backup(
            fake_agent_data["memory_root"],
            today="2026-06-21",
        )
        assert r.succeeded
        # .bak 应被忽略:备份里没有 mem0.md.bak
        assert not (r.backup_path / "memory" / "user" / "mem0.md.bak").exists()

    def test_backup_prunes_old(self, fake_agent_data):
        """滚动保留:超出 keep_days 的旧备份被删"""
        # 造 8 个旧备份 + 1 个今天
        backup_root = fake_agent_data["tmp"] / "memory.backup"
        for d in range(1, 9):  # 2026-06-13 ~ 2026-06-20
            target = backup_root / f"2026-06-13+{d - 1}"  # not standard format
            # 改用标准日期
        # 改写:造 8 天前 + 7 天前 ... + 今天
        from datetime import datetime, timedelta
        base = datetime.strptime("2026-06-21", "%Y-%m-%d")
        for offset in range(-8, 0):  # 2026-06-13 ~ 2026-06-20
            d = (base + timedelta(days=offset)).strftime("%Y-%m-%d")
            (backup_root / d).mkdir(parents=True)
        # 跑今天 backup,keep_days=3 → 应删 2026-06-13 ~ 2026-06-17 (5 天)
        r = daily_backup(
            fake_agent_data["memory_root"],
            backup_root=backup_root,
            keep_days=3,
            today="2026-06-21",
        )
        assert r.succeeded
        remaining = sorted([p.name for p in backup_root.iterdir() if p.is_dir()])
        # 留 3 天:2026-06-18, 2026-06-19, 2026-06-20, 2026-06-21 = 4 天
        assert remaining == ["2026-06-18", "2026-06-19", "2026-06-20", "2026-06-21"]


# ──────────────────────────────────────────────────────────────────
# 2. restore_backup
# ──────────────────────────────────────────────────────────────────

class TestRestoreBackup:

    def test_restore_overwrites_existing(self, fake_agent_data):
        """restore 会覆盖现有 memory_root(危险操作)"""
        # 备份
        daily_backup(fake_agent_data["memory_root"], today="2026-06-20")
        # 改原数据
        (fake_agent_data["memory_root"] / "user" / "mem0.md").unlink()
        # 恢复
        restore_backup(
            "2026-06-20",
            fake_agent_data["memory_root"],
            meta_db=fake_agent_data["meta_db"],
        )
        # mem0.md 应回来
        assert (fake_agent_data["memory_root"] / "user" / "mem0.md").exists()

    def test_restore_missing_backup_raises(self, fake_agent_data):
        """备份不存在 → LifecycleError"""
        with pytest.raises(LifecycleError, match="备份不存在"):
            restore_backup(
                "2099-01-01",  # 未来
                fake_agent_data["memory_root"],
            )

    def test_restore_invalid_date_format_raises(self, fake_agent_data):
        """backup_date 格式错 → LifecycleError"""
        with pytest.raises(LifecycleError, match="格式错"):
            restore_backup(
                "2026/06/21",  # 错格式
                fake_agent_data["memory_root"],
            )


# ──────────────────────────────────────────────────────────────────
# 3. integrity_check
# ──────────────────────────────────────────────────────────────────

class TestIntegrityCheck:

    def test_healthy_data_passes(self, fake_agent_data):
        """健康数据 → is_healthy=True"""
        r = integrity_check(
            fake_agent_data["memory_root"],
            meta_db=fake_agent_data["meta_db"],
        )
        assert r.sqlite_ok is True
        assert r.sqlite_detail == "ok"
        assert r.frontmatter_total == 3
        assert r.frontmatter_invalid == 0
        assert r.is_healthy

    def test_detects_broken_frontmatter(self, fake_agent_data):
        """损坏 frontmatter → frontmatter_invalid > 0"""
        bad = fake_agent_data["memory_root"] / "user" / "broken.md"
        bad.write_text("just plain text, no ---\n", encoding="utf-8")
        r = integrity_check(
            fake_agent_data["memory_root"],
        )
        assert r.frontmatter_invalid == 1
        assert bad in r.frontmatter_invalid_paths
        assert not r.is_healthy

    def test_detects_old_schema_version(self, fake_agent_data):
        """schema_version 过旧 → 视为 frontmatter_invalid"""
        old = fake_agent_data["memory_root"] / "user" / "old.md"
        old.write_text(
            "---\ntype: user\ntitle: old\nschema_version: 1\n---\nbody\n",
            encoding="utf-8",
        )
        r = integrity_check(fake_agent_data["memory_root"])
        assert r.frontmatter_invalid == 1
        assert not r.is_healthy

    def test_no_meta_db_is_ok(self, fake_agent_data):
        """meta_db 路径为空 → sqlite_ok=True(测试场景常见)"""
        r = integrity_check(fake_agent_data["memory_root"])
        assert r.sqlite_ok is True
        assert r.sqlite_detail == "(meta_db not present)"


# ──────────────────────────────────────────────────────────────────
# 4. capacity_govern
# ──────────────────────────────────────────────────────────────────

class TestCapacityGovern:

    def test_under_threshold_is_noop(self, fake_agent_data):
        """未超阈值 → pruned_count=0"""
        r = capacity_govern(fake_agent_data["memory_root"], max_files=100)
        assert r.total_files == 3
        assert r.pruned_count == 0
        assert not r.threshold_exceeded

    def test_over_threshold_prunes_by_importance_then_age(self, fake_agent_data):
        """超阈值 → 按 importance 升序淘汰,留下的全是 importance >= min 的"""
        # 写 10 个低 importance 的文件
        user_dir = fake_agent_data["memory_root"] / "user"
        for i in range(10):
            (user_dir / f"low_{i}.md").write_text(
                f"---\ntype: user\ntitle: low {i}\n"
                f"created_at: 2024-01-01\nschema_version: {CURRENT_SCHEMA_VERSION}\n"
                f"item_hash: {'0' * 64}\nimportance: 2\n---\nbody\n",
                encoding="utf-8",
            )
        # max_files=4 → target=3,正好留 3 个 importance>=3 的文件
        r = capacity_govern(
            fake_agent_data["memory_root"],
            max_files=4,
            importance_min=3,  # importance >= 3 不淘汰
        )
        assert r.threshold_exceeded
        assert r.pruned_count == 10  # 全 10 个低 importance 被淘汰
        # 留下的应是 importance >= 3 的(原 3 个 importance 5,6,7)
        remaining = [p for p in fake_agent_data["memory_root"].rglob("*.md") if p.suffix != ".bak"]
        assert len(remaining) == 3
        for p in remaining:
            text = p.read_text(encoding="utf-8")
            assert "importance: 2" not in text
        # 验证:被淘汰的全是 importance=2(从原始 frontmatter 字段直接读)
        for p in r.pruned_paths:
            # 文件已被删 → 用旧 fixture 路径推断
            assert p.name.startswith("low_"), f"unexpected pruned: {p.name}"

    def test_over_threshold_no_importance_min_prunes_all(self, fake_agent_data):
        """importance_min=10(高过 5)→ importance=5 的文件不再被保护,可被淘汰"""
        # 写 6 个 importance=5 的(默认)
        user_dir = fake_agent_data["memory_root"] / "user"
        for i in range(6):
            (user_dir / f"mid_{i}.md").write_text(
                f"---\ntype: user\ntitle: mid {i}\n"
                f"created_at: 2024-01-01\nschema_version: {CURRENT_SCHEMA_VERSION}\n"
                f"item_hash: {'0' * 64}\nimportance: 5\n---\nbody\n",
                encoding="utf-8",
            )
        # importance_min=10 → 5 < 10,不被保护,可淘汰
        r = capacity_govern(
            fake_agent_data["memory_root"],
            max_files=3,
            importance_min=10,
        )
        assert r.pruned_count > 0

    def test_capacity_govern_handles_missing_root(self, tmp_path):
        """不存在的 root → 空 report"""
        r = capacity_govern(tmp_path / "nope")
        assert r.total_files == 0
        assert r.pruned_count == 0


# ──────────────────────────────────────────────────────────────────
# 5. list_backups
# ──────────────────────────────────────────────────────────────────

class TestListBackups:

    def test_lists_sorted_desc(self, fake_agent_data):
        """list_backups 按日期倒序"""
        daily_backup(fake_agent_data["memory_root"], today="2026-06-19")
        daily_backup(fake_agent_data["memory_root"], today="2026-06-21")
        daily_backup(fake_agent_data["memory_root"], today="2026-06-20")
        dates = [p.name for p in list_backups(fake_agent_data["memory_root"])]
        assert dates == ["2026-06-21", "2026-06-20", "2026-06-19"]

    def test_no_backup_root_returns_empty(self, tmp_path):
        """备份根目录不存在 → 空列表"""
        # 造一个不存在的 memory_root 父级
        assert list_backups(tmp_path / "nope") == []