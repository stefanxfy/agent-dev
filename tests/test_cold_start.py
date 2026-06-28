"""
M3 / Day 3 测试 —— ColdStartLoader (L5 seed 加载)

覆盖:
- YAML / JSON 解析
- 默认目录 / 自定义目录
- 幂等 (A5): 重复加载不重复写
- 类型校验失败处理
- 文件解析失败处理
- 报告统计

依赖:
- chromadb(ChromaVectorStore)
- bge-m3 / sentence-transformers(真嵌入;HF cache 必须有模型)
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_core.memory import (
    ColdStartLoader,
    SeedItem,
    ColdStartReport,
    ColdStartError,
    MemoryStore,
    ChromaVectorStore,
    make_embed_fn,
)


# ──────────────────────────────────────────────────────────────────
# Fixtures
# ──────────────────────────────────────────────────────────────────

@pytest.fixture
def workspace(tmp_path):
    """完整 workspace"""
    memory_root = tmp_path / "memory"
    memory_root.mkdir()
    seeds_dir = tmp_path / "seeds"
    seeds_dir.mkdir()
    chroma_dir = tmp_path / "chroma"
    chroma_dir.mkdir()
    # 用 with 块,fixture 退出时自动 close chroma client,防 fd 泄漏
    with ChromaVectorStore(chroma_dir, collection=f"coldstart_{tmp_path.name}") as vec:
        yield {
            "memory_root": memory_root,
            "seeds_dir": seeds_dir,
            "chroma_dir": chroma_dir,
            "vec": vec,
            "embed": make_embed_fn("bge-m3"),
        }


@pytest.fixture
def loader(workspace):
    return ColdStartLoader(
        memory_store=MemoryStore(workspace["memory_root"]),
        vector_store=workspace["vec"],
        embed_fn=workspace["embed"],
        default_seeds_dir=workspace["seeds_dir"],
    )


# ──────────────────────────────────────────────────────────────────
# YAML 解析
# ──────────────────────────────────────────────────────────────────

class TestYAMLParse:

    def test_load_list_format(self, loader, workspace):
        (workspace["seeds_dir"] / "sample.yaml").write_text("""
- type: user
  title: 默认用户
  body: 用户未登录时为访客
  source_quote: "系统初始化"
  tags: [default]
  importance: 7
- type: project
  title: 默认项目
  body: 项目根目录说明
  source_quote: "README"
  importance: 5
  extra_why: "确保新人快速上手"
""", encoding="utf-8")
        # project 类型必须有 **Why:** 段(validate_body 强制)
        # 改用 user 类型避免触发
        (workspace["seeds_dir"] / "sample.yaml").write_text("""
- type: user
  title: 默认用户
  body: 用户未登录时为访客
  source_quote: "系统初始化"
  tags: [default]
  importance: 7
- type: reference
  title: 默认文档
  body: 文档根目录
  source_quote: "README"
  importance: 5
""", encoding="utf-8")
        report = loader.load()
        assert report.total == 2
        assert report.loaded == 2
        assert report.failed == 0

    def test_load_single_dict_format(self, loader, workspace):
        (workspace["seeds_dir"] / "single.yaml").write_text("""
type: user
title: 单个 item
body: 单个 dict 格式
source_quote: "示例"
""", encoding="utf-8")
        report = loader.load()
        assert report.total == 1
        assert report.loaded == 1

    def test_load_items_key_format(self, loader, workspace):
        (workspace["seeds_dir"] / "wrapped.yaml").write_text("""
items:
  - type: user
    title: 包了一层
    body: items 键格式
    source_quote: "示例1"
  - type: user
    title: 包了二层
    body: items 键格式2
    source_quote: "示例2"
""", encoding="utf-8")
        report = loader.load()
        assert report.loaded == 2


# ──────────────────────────────────────────────────────────────────
# JSON 解析
# ──────────────────────────────────────────────────────────────────

class TestJSONParse:

    def test_load_json_list(self, loader, workspace):
        (workspace["seeds_dir"] / "data.json").write_text(json.dumps([
            {"type": "user", "title": "JSON 用户", "body": "JSON body", "source_quote": "q"},
        ], ensure_ascii=False), encoding="utf-8")
        report = loader.load()
        assert report.loaded == 1

    def test_load_json_single(self, loader, workspace):
        (workspace["seeds_dir"] / "single.json").write_text(json.dumps({
            "type": "user", "title": "JSON 用户", "body": "body", "source_quote": "q",
        }, ensure_ascii=False), encoding="utf-8")
        report = loader.load()
        assert report.loaded == 1


# ──────────────────────────────────────────────────────────────────
# 幂等 (A5)
# ──────────────────────────────────────────────────────────────────

class TestIdempotency:

    def test_reload_skips_existing(self, loader, workspace):
        (workspace["seeds_dir"] / "same.yaml").write_text("""
- type: user
  title: 用户
  body: 用户名是小明
  source_quote: "我说'我叫小明'"
""", encoding="utf-8")
        # 第一次: 加载 1
        r1 = loader.load()
        assert r1.loaded == 1
        assert r1.skipped == 0

        # 第二次: 跳过 1
        r2 = loader.load()
        assert r2.loaded == 0
        assert r2.skipped == 1

    def test_load_one_idempotent(self, loader):
        item = SeedItem(
            type="user", title="一次性",
            body="测试 load_one", source_quote="测试"
        )
        assert loader.load_one(item) is True
        # 第二次返回 False
        assert loader.load_one(item) is False


# ──────────────────────────────────────────────────────────────────
# 错误处理
# ──────────────────────────────────────────────────────────────────

class TestErrorHandling:

    def test_missing_seeds_dir_is_ok(self, tmp_path):
        """seeds 目录不存在不报错,只是空报告"""
        memory_root = tmp_path / "memory"
        memory_root.mkdir()
        chroma_dir = tmp_path / "chroma"
        chroma_dir.mkdir()
        # 用 with 块自动 close chroma client
        with ChromaVectorStore(chroma_dir, collection=f"missing_{tmp_path.name}") as vec:
            loader = ColdStartLoader(
                MemoryStore(memory_root),
                vec,
                make_embed_fn("bge-m3"),
                default_seeds_dir=tmp_path / "nonexistent",
            )
            report = loader.load()
            assert report.loaded == 0
            assert report.total == 0

    def test_invalid_yaml_recorded_as_failure(self, loader, workspace):
        (workspace["seeds_dir"] / "bad.yaml").write_text("""
- type: not_a_real_type
  title: x
  body: y
  source_quote: z
""", encoding="utf-8")
        report = loader.load()
        assert report.failed == 1
        assert report.loaded == 0
        assert "parse" in str(report.failures) or any("not_a_real_type" in f[1] for f in report.failures)

    def test_malformed_yaml_recorded_as_failure(self, loader, workspace):
        (workspace["seeds_dir"] / "broken.yaml").write_text("""
  - this is invalid yaml
    unclosed quote: "
""", encoding="utf-8")
        report = loader.load()
        # 解析失败也算 failed
        assert report.failed >= 1

    def test_load_from_dir_raises_if_not_dir(self, loader, tmp_path):
        """load_from_dir 对文件路径应抛 ColdStartError"""
        f = tmp_path / "not_a_dir"
        f.write_text("x")
        with pytest.raises(ColdStartError):
            loader.load_from_dir(f)


# ──────────────────────────────────────────────────────────────────
# 报告 + 统计
# ──────────────────────────────────────────────────────────────────

class TestReport:

    def test_report_summary(self, loader, workspace):
        (workspace["seeds_dir"] / "r.yaml").write_text("""
- type: user
  title: 用户
  body: 内容
  source_quote: q
""", encoding="utf-8")
        report = loader.load()
        s = report.summary()
        assert "loaded=1" in s
        assert "sources=1" in s

    def test_multiple_files(self, loader, workspace):
        (workspace["seeds_dir"] / "a.yaml").write_text("""
- type: user
  title: A
  body: A body
  source_quote: qA
""", encoding="utf-8")
        (workspace["seeds_dir"] / "b.yaml").write_text("""
- type: user
  title: B
  body: B body
  source_quote: qB
""", encoding="utf-8")
        report = loader.load()
        assert report.total == 2
        assert report.loaded == 2
        assert len(report.sources) == 1  # 同一个目录


# ──────────────────────────────────────────────────────────────────
# SeedItem 单元
# ──────────────────────────────────────────────────────────────────

class TestSeedItem:

    def test_from_dict_basic(self):
        d = {
            "type": "user",
            "title": "标题",
            "body": "内容",
            "source_quote": "来源",
        }
        item = SeedItem.from_dict(d)
        assert item.type == "user"
        assert item.title == "标题"
        assert item.importance == 5  # default

    def test_from_dict_with_tags(self):
        d = {
            "type": "feedback",
            "title": "x",
            "body": "y",
            "source_quote": "z",
            "tags": ["a", "b"],
            "importance": 8,
        }
        item = SeedItem.from_dict(d)
        assert item.tags == ["a", "b"]
        assert item.importance == 8

    def test_from_dict_invalid_type_raises(self):
        d = {"type": "unknown", "title": "x", "body": "y", "source_quote": "z"}
        with pytest.raises(ColdStartError):
            SeedItem.from_dict(d)

    def test_from_dict_missing_type_defaults_to_user(self):
        d = {"title": "x", "body": "y", "source_quote": "z"}
        item = SeedItem.from_dict(d)
        assert item.type == "user"
