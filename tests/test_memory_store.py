"""
MemoryStore.write() M11 v3 测试

覆盖:
- name / description 必填
- name/description 缺省回退(title → name, body 第一段 → description)
- title 显式传 → 保留不覆盖
- item_hash 不变 (name/description 不参与 hash 计算)
- description 截断 (>200)
- frontmatter 写入包含 name/description + schema_version=3
"""

from __future__ import annotations

import pytest

from agent_core.memory.memory_store import MemoryStore, compute_item_hash
from agent_core.memory.types import FrontmatterError


def test_memory_store_write_v3_requires_name(tmp_path):
    """M11 v3: 缺 name → 抛 FrontmatterError"""
    store = MemoryStore(tmp_path / "memory")
    with pytest.raises(FrontmatterError, match="name"):
        store.write(
            type="user",
            description="desc",
            body="x",
            source_quote="x",
        )


def test_memory_store_write_v3_description_fallback_when_missing(tmp_path):
    """M11 v3: 缺 description 且 body 空白 → fallback '未描述' 写盘成功"""
    store = MemoryStore(tmp_path / "memory")
    item_hash = store.write(
        type="user",
        name="x",
        body="   \n  ",  # 全空白 → 描述 fallback = "未描述"
        source_quote="x",
    )
    data = store.read(f"user/{item_hash}.md")
    assert data["frontmatter"]["description"] == "未描述"


def test_memory_store_write_v3_accepts_explicit(tmp_path):
    """M11 v3: 显式 name + description → 通过"""
    store = MemoryStore(tmp_path / "memory")
    item_hash = store.write(
        type="user",
        name="用户叫小明",
        description="Python 后端工程师",
        body="小明是 Python 工程师",
        source_quote="小明是 Python 工程师",
    )
    assert len(item_hash) == 64
    data = store.read(f"user/{item_hash}.md")
    fm = data["frontmatter"]
    assert fm["name"] == "用户叫小明"
    assert fm["description"] == "Python 后端工程师"
    assert fm["schema_version"] == 3
    assert fm["title"] == "用户叫小明"  # mirror


def test_memory_store_write_v3_mirrors_name_to_title(tmp_path):
    """M11 v3: caller 没传 title 时,title 自动 = name"""
    store = MemoryStore(tmp_path / "memory")
    item_hash = store.write(
        type="user",
        name="用户叫小明",
        description="Python 后端",
        body="小明是 Python 工程师",
        source_quote="小明是 Python 工程师",
    )
    data = store.read(f"user/{item_hash}.md")
    fm = data["frontmatter"]
    assert fm["name"] == "用户叫小明"
    assert fm["description"] == "Python 后端"
    assert fm["title"] == "用户叫小明"


def test_memory_store_write_v3_explicit_title_kept(tmp_path):
    """M11 v3: caller 传 title 时,保留 title(不覆盖)"""
    store = MemoryStore(tmp_path / "memory")
    item_hash = store.write(
        type="user",
        name="用户叫小明",
        description="Python 后端",
        body="x",
        source_quote="x",
        title="客户档案",
    )
    data = store.read(f"user/{item_hash}.md")
    assert data["frontmatter"]["title"] == "客户档案"
    assert data["frontmatter"]["name"] == "用户叫小明"


def test_memory_store_write_v3_item_hash_unchanged_by_name(tmp_path):
    """M11: item_hash 不变 (name/description 不参与 hash)"""
    store1 = MemoryStore(tmp_path / "m1")
    store2 = MemoryStore(tmp_path / "m2")
    body = "x"
    sq = "x"
    h1 = store1.write(type="user", name="nameA", description="descA",
                      body=body, source_quote=sq)
    h2 = store2.write(type="user", name="nameB", description="descB",
                      body=body, source_quote=sq)
    assert h1 == h2  # 同样 type/body/source_quote → 同 hash


def test_memory_store_write_v3_description_truncated(tmp_path):
    """M11 v3: description > 200 自动截断"""
    store = MemoryStore(tmp_path / "memory")
    item_hash = store.write(
        type="user",
        name="x",
        description="a" * 500,
        body="b",
        source_quote="b",
    )
    data = store.read(f"user/{item_hash}.md")
    assert len(data["frontmatter"]["description"]) == 200


def test_memory_store_write_v3_legacy_caller_compatible(tmp_path):
    """M11 v3 向后兼容:旧 caller 只传 title(不传 name/description) 仍可工作

    name fallback = title, description fallback = body 第一段
    """
    store = MemoryStore(tmp_path / "memory")
    item_hash = store.write(
        type="user",
        title="客户档案",
        body="用户叫张三",
        source_quote="张三说: 我叫张三",
    )
    data = store.read(f"user/{item_hash}.md")
    fm = data["frontmatter"]
    assert fm["name"] == "客户档案"  # title → name
    assert fm["description"] == "用户叫张三"  # body 第一段 → description
    assert fm["title"] == "客户档案"
    assert fm["schema_version"] == 3


def test_compute_item_hash_unchanged_in_m11(tmp_path):
    """M11: compute_item_hash 仍只依赖 type/body/source_quote(不依赖 name/description)"""
    h1 = compute_item_hash("user", "body", "quote")
    h2 = compute_item_hash("user", "body", "quote")
    assert h1 == h2
    # 不同 body → 不同 hash
    assert h1 != compute_item_hash("user", "body2", "quote")
    assert h1 != compute_item_hash("feedback", "body", "quote")