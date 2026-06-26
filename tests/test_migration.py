"""
M11 migration v2 → v3 测试
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml


def _parse_fm(text: str) -> dict:
    """简易 frontmatter 解析(测试用)"""
    m = re.match(r"\A---\n(.*?)\n---\s*\n?(.*)\Z", text, re.DOTALL)
    if not m:
        return {}
    return yaml.safe_load(m.group(1)) or {}


from agent_core.memory.migration import (
    MigrationRegistry,
    _v2_to_v3,
    migrate_file,
)


def test_v2_to_v3_fills_name_and_description(tmp_path):
    """v2 → v3 迁移: 自动补 name (= title) + description(从 body 摘要)"""
    fm_v2 = {
        "type": "user",
        "created_at": "2026-06-01T00:00:00+00:00",
        "item_hash": "a" * 64,
        "schema_version": 2,
        "title": "客户档案",
        "tags": ["person"],
    }
    body = "用户叫张三,深圳,Python 工程师"
    new_fm, new_body = _v2_to_v3(fm_v2, body)
    assert new_fm["schema_version"] == 3
    assert new_fm["name"] == "客户档案"  # mirror title
    assert "张三" in new_fm["description"]  # 从 body 摘要
    assert len(new_fm["description"]) <= 200
    assert new_body == body  # body 不变


def test_v2_to_v3_fills_description_from_title_if_body_empty():
    """body 为空时, description fallback 到 title"""
    fm_v2 = {
        "type": "user",
        "created_at": "2026-06-01T00:00:00+00:00",
        "item_hash": "a" * 64,
        "schema_version": 2,
        "title": "客户档案",
        "tags": [],
    }
    new_fm, _ = _v2_to_v3(fm_v2, "")
    assert new_fm["description"] == "客户档案"  # fallback


def test_v2_to_v3_preserves_existing_name():
    """已有 name 字段时保留"""
    fm_v2 = {
        "type": "user",
        "created_at": "2026-06-01T00:00:00+00:00",
        "item_hash": "a" * 64,
        "schema_version": 2,
        "title": "客户档案",
        "name": "已有名字",
        "description": "已有描述",
        "tags": [],
    }
    new_fm, _ = _v2_to_v3(fm_v2, "body")
    assert new_fm["name"] == "已有名字"
    assert new_fm["description"] == "已有描述"


def test_migrate_chain_v0_v1_v2_v3(tmp_path):
    """链式迁移: v0 → v3"""
    fm_v0 = {
        "type": "user",
        "title": "客户档案",
        # 无 schema_version / created_at / item_hash / importance / name / description
    }
    body = "用户叫张三,深圳"
    new_fm, new_body = MigrationRegistry.migrate(0, fm_v0, body)
    assert new_fm["schema_version"] == 3
    assert new_fm["name"] == "客户档案"  # v2→v3: title → name
    assert "张三" in new_fm["description"]
    assert new_fm["importance"] == 5  # v1→v2


def test_migrate_file_creates_bak_sidecar(tmp_path):
    """迁移写盘前生成 .bak sidecar"""
    md = tmp_path / "test.md"
    md.write_text(
        """---
type: user
created_at: 2026-06-01T00:00:00+00:00
item_hash: aaaa
schema_version: 2
title: 旧记忆
tags: []
---
旧 body""",
        encoding="utf-8",
    )
    result = migrate_file(md)
    assert result["migrated"] is True
    assert (tmp_path / "test.md.bak").exists()
    fm = _parse_fm(md.read_text(encoding="utf-8"))
    assert fm["schema_version"] == 3
    assert "name" in fm and "description" in fm


def test_migrate_file_noop_when_already_current(tmp_path):
    """schema_version 已 = CURRENT → no-op, 无 .bak"""
    md = tmp_path / "current.md"
    md.write_text(
        """---
type: user
created_at: 2026-06-01T00:00:00+00:00
item_hash: aaaa
schema_version: 3
name: n
description: d
title: n
tags: []
---
body""",
        encoding="utf-8",
    )
    result = migrate_file(md)
    assert result["migrated"] is False
    assert not (tmp_path / "current.md.bak").exists()


def test_v2_to_v3_via_registry():
    """v2 → v3 已注册到 MigrationRegistry"""
    fm_v2 = {
        "type": "user",
        "created_at": "2026-06-01T00:00:00+00:00",
        "item_hash": "a" * 64,
        "schema_version": 2,
        "title": "T",
    }
    new_fm, _ = MigrationRegistry.migrate(2, fm_v2, "张三")
    assert new_fm["schema_version"] == 3
    assert new_fm["name"] == "T"
    assert new_fm["description"] == "张三"