"""
M7 / Day 7 集成测试 —— 5 cases

覆盖:
1. test_migration_file_v0_to_v2_with_bak — 单文件迁移 + .bak sidecar
2. test_migration_file_already_v2_is_noop — 已是 CURRENT → no-op
3. test_migration_all_batch_with_mixed_versions — 批量迁移
4. test_router_signature_has_cache_namespace — chat() 接受 cache_namespace
5. test_memory_status_chunk_aggregation — UI chunk 累积逻辑(模拟 streamlit session_state)

注意:端到端 Agent run 测试需要 langchain_core(本环境未装),
故 5 个 case 聚焦在 M7 三个独立可测的接入点:
- Schema 迁移(migration.py)
- Router 合约(router.py chat 签名)
- UI chunk 累积(extract logic 出来单测)
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from agent_core.memory import (
    CURRENT_SCHEMA_VERSION,
    MemoryConfig,
    MemoryStore,
    MigrationError,
    MigrationRegistry,
    MigrationReport,
    migrate_all,
    migrate_file,
)
from agent_core.memory.types import validate_frontmatter


# ──────────────────────────────────────────────────────────────────
# Fixtures
# ──────────────────────────────────────────────────────────────────

@pytest.fixture
def memory_root(tmp_path):
    root = tmp_path / "memory"
    root.mkdir()
    return root


def _write_v0_memory(path: Path, title: str = "M7 测试", body: str = "v0 旧格式") -> None:
    """写一个 v0(无 schema_version 字段)的旧记忆文件"""
    text = f"""---
type: user
title: {title}
created_at: 2024-01-01
---
# {title}

{body}
"""
    path.write_text(text, encoding="utf-8")


def _write_v1_memory(path: Path, title: str = "v1 测试", body: str = "v1 格式") -> None:
    """写一个 v1 的旧记忆文件(schema_version=1)"""
    text = f"""---
type: user
title: {title}
schema_version: 1
created_at: 2024-06-01
confidence: 0.7
---
# {title}

{body}
"""
    path.write_text(text, encoding="utf-8")


def _write_v2_memory(path: Path, title: str = "v2 测试", body: str = "v2 当前格式") -> None:
    """写一个 v2(已 CURRENT)的记忆文件"""
    text = f"""---
type: user
title: {title}
schema_version: 2
created_at: 2025-01-01
confidence: 0.8
importance: 7
---
# {title}

{body}
"""
    path.write_text(text, encoding="utf-8")


# ──────────────────────────────────────────────────────────────────
# 1. 单文件迁移 v0 → v2 + .bak sidecar
# ──────────────────────────────────────────────────────────────────

class TestMigrationFile:

    def test_migrate_v0_to_current_creates_bak(self, memory_root):
        """v0 文件迁移:写回 v2 + 留 .bak sidecar"""
        mem_dir = memory_root / "user"
        mem_dir.mkdir()
        mem_file = mem_dir / "abc.md"
        _write_v0_memory(mem_file)

        result = migrate_file(mem_file)
        assert result["migrated"] is True
        assert result["from_v"] == 0
        assert result["frontmatter"]["schema_version"] == CURRENT_SCHEMA_VERSION
        # .bak sidecar 存在
        bak = mem_file.with_suffix(mem_file.suffix + ".bak")
        assert bak.exists()
        # 原内容在 .bak 里
        bak_text = bak.read_text(encoding="utf-8")
        assert "schema_version" not in bak_text  # 旧版无此字段
        # 新文件是 v2
        new_text = mem_file.read_text(encoding="utf-8")
        assert f"schema_version: {CURRENT_SCHEMA_VERSION}" in new_text
        assert "importance" in new_text  # v2 必填

    def test_migrate_v1_to_v2_adds_importance(self, memory_root):
        """v1 → v2:补 importance 字段"""
        mem_dir = memory_root / "user"
        mem_dir.mkdir()
        mem_file = mem_dir / "v1test.md"
        _write_v1_memory(mem_file)

        result = migrate_file(mem_file)
        assert result["migrated"] is True
        assert result["from_v"] == 1
        assert result["frontmatter"]["schema_version"] == 2
        assert result["frontmatter"]["importance"] == 5  # 默认值

    def test_migrate_already_current_is_noop(self, memory_root):
        """已是 v2 → migrated=False,不写 .bak,不修改文件"""
        mem_dir = memory_root / "user"
        mem_dir.mkdir()
        mem_file = mem_dir / "current.md"
        _write_v2_memory(mem_file)
        original = mem_file.read_text(encoding="utf-8")

        result = migrate_file(mem_file)
        assert result["migrated"] is False
        assert result["from_v"] == 2
        # 内容未变
        assert mem_file.read_text(encoding="utf-8") == original
        # 无 .bak
        assert not mem_file.with_suffix(mem_file.suffix + ".bak").exists()


# ──────────────────────────────────────────────────────────────────
# 2. 批量迁移
# ──────────────────────────────────────────────────────────────────

class TestMigrationBatch:

    def test_migrate_all_handles_mixed_versions(self, memory_root):
        """migrate_all():3 个文件(2 旧 1 新)→ 应迁移 2 个,1 个 already_current"""
        user_dir = memory_root / "user"
        user_dir.mkdir()
        _write_v0_memory(user_dir / "v0a.md")
        _write_v1_memory(user_dir / "v1a.md")
        _write_v2_memory(user_dir / "current.md")

        report = migrate_all(memory_root)
        assert isinstance(report, MigrationReport)
        assert report.migrated == 2          # v0a + v1a
        assert report.already_current == 1   # current.md
        assert report.skipped == 0
        assert report.total == 3
        assert not report.has_errors

        # 全部应是 v2
        for name in ("v0a", "v1a", "current"):
            text = (user_dir / f"{name}.md").read_text(encoding="utf-8")
            assert f"schema_version: {CURRENT_SCHEMA_VERSION}" in text

    def test_migrate_all_skips_bak_files(self, memory_root):
        """migrate_all() 不递归 .bak sidecar"""
        user_dir = memory_root / "user"
        user_dir.mkdir()
        _write_v0_memory(user_dir / "test.md")
        migrate_file(user_dir / "test.md")  # 产生 .bak

        # 再跑一次 migrate_all → 全部 already_current
        report = migrate_all(memory_root)
        assert report.migrated == 0
        assert report.already_current == 1
        assert report.skipped == 0
        assert not report.has_errors

    def test_migrate_all_reports_skipped_for_broken_files(self, memory_root):
        """Issue 3 修复:坏文件应进 skipped + errors,而不是被静默吞掉"""
        user_dir = memory_root / "user"
        user_dir.mkdir()
        _write_v0_memory(user_dir / "good_v0.md")  # 正常 v0
        # 故意写一个损坏的文件(无 --- frontmatter)
        (user_dir / "broken.md").write_text(
            "just plain text, no frontmatter at all\n", encoding="utf-8"
        )

        report = migrate_all(memory_root)
        # good_v0 应被迁移,broken 应被 skipped
        assert report.migrated == 1
        assert report.already_current == 0
        assert report.skipped == 1
        assert report.has_errors
        assert len(report.errors) == 1
        broken_path, err_msg = report.errors[0]
        assert broken_path.name == "broken.md"
        assert "frontmatter" in err_msg or "---" in err_msg
        print(f"  skipped: {broken_path.name} → {err_msg[:60]}")

    def test_migrate_all_on_nonexistent_root_returns_empty_report(self, tmp_path):
        """不存在的 root → 返回空 report,不抛"""
        fake_root = tmp_path / "does_not_exist"
        report = migrate_all(fake_root)
        assert isinstance(report, MigrationReport)
        assert report.migrated == 0
        assert report.already_current == 0
        assert report.skipped == 0
        assert report.total == 0


# ──────────────────────────────────────────────────────────────────
# 3. Router 合约 —— cache_namespace 签名
# ──────────────────────────────────────────────────────────────────

class TestRouterContract:

    def test_chat_accepts_cache_namespace_kwarg(self):
        """LLMRouter.chat 接受 cache_namespace 参数(签名 + docstring)"""
        from agent_core.llm.router import LLMRouter
        import inspect

        sig = inspect.signature(LLMRouter.chat)
        assert "cache_namespace" in sig.parameters, "chat() 必须有 cache_namespace 参数"
        # 默认值是 None
        assert sig.parameters["cache_namespace"].default is None

    def test_chat_docstring_documents_cache_namespace(self):
        """chat() docstring 提到 cache_namespace(给使用者看)"""
        from agent_core.llm.router import LLMRouter
        doc = LLMRouter.chat.__doc__ or ""
        assert "cache_namespace" in doc
        # 应说明 cache_namespace 的语义(不是孤立单词)
        assert "Anthropic" in doc or "anthropic" in doc


# ──────────────────────────────────────────────────────────────────
# 4. UI chunk 累积逻辑(从 app_langgraph.py 提取,单测)
# ──────────────────────────────────────────────────────────────────

class TestUIChunkAggregation:
    """
    测试 session_state.memory_stats / token_stats 的累积逻辑
    (从 app_langgraph.py 的 chunk 处理代码抽出,避免依赖 streamlit)
    """

    def _accumulate_usage(self, stats: dict, usage_obj) -> None:
        """从 app_langgraph.py 抽出的 usage chunk 累积逻辑"""
        stats["input"] += getattr(usage_obj, "input_tokens", 0)
        stats["output"] += getattr(usage_obj, "output_tokens", 0)
        stats["thinking"] += getattr(usage_obj, "thinking_tokens", 0)
        stats["cached"] += getattr(usage_obj, "cached_tokens", 0)

    def _accumulate_memory_status(self, ms: dict, content: dict) -> None:
        """从 app_langgraph.py 抽出的 memory_status chunk 累积逻辑"""
        ms["total_searches"] += 1
        ms["total_hits"] += int(content.get("hits", 0))
        ms["current_turn_hits"] = int(content.get("hits", 0))
        ms["stored_total"] = int(content.get("stored_total", 0))
        if content.get("zero_hit"):
            ms["last_zero_hit_turn"] = ms["total_searches"]

    def test_cached_tokens_now_accumulated(self):
        """M7 修复:cached_tokens 不再被静默丢弃"""
        stats = {"input": 0, "output": 0, "thinking": 0, "cached": 0}
        usage = MagicMock(input_tokens=100, output_tokens=50, thinking_tokens=20, cached_tokens=80)
        self._accumulate_usage(stats, usage)
        assert stats == {"input": 100, "output": 50, "thinking": 20, "cached": 80}

    def test_memory_status_zero_hit_marks_last_zero_turn(self):
        """memory_status chunk zero_hit=True → 记下 last_zero_hit_turn"""
        ms = {"total_searches": 0, "total_hits": 0, "last_zero_hit_turn": None,
              "current_turn_hits": 0, "stored_total": 0}
        # turn 1: 有 2 hits
        self._accumulate_memory_status(ms, {"hits": 2, "stored_total": 10, "zero_hit": False})
        assert ms["total_searches"] == 1
        assert ms["total_hits"] == 2
        assert ms["last_zero_hit_turn"] is None
        # turn 2: 0 hits
        self._accumulate_memory_status(ms, {"hits": 0, "stored_total": 10, "zero_hit": True})
        assert ms["total_searches"] == 2
        assert ms["total_hits"] == 2  # 总和不变
        assert ms["last_zero_hit_turn"] == 2
        # turn 3: 又有 1 hits
        self._accumulate_memory_status(ms, {"hits": 1, "stored_total": 10, "zero_hit": False})
        assert ms["total_searches"] == 3
        assert ms["total_hits"] == 3
        assert ms["last_zero_hit_turn"] == 2  # 不变


# ──────────────────────────────────────────────────────────────────
# 5. Schema 严校验 —— reject v>current
# ──────────────────────────────────────────────────────────────────

# 64-char hex(SHA-256)用于 test fixtures
_FAKE_SHA256 = "0" * 64


class TestSchemaStrict:

    def test_validate_frontmatter_rejects_future_version(self):
        """未来版本(> CURRENT)→ ValueError,错误信息提到 schema_version"""
        with pytest.raises(ValueError, match="schema_version"):
            validate_frontmatter({
                "type": "user",
                "title": "test",
                "schema_version": CURRENT_SCHEMA_VERSION + 99,
                "created_at": "2025-01-01",
                "item_hash": _FAKE_SHA256,
            })

    def test_validate_frontmatter_accepts_current_version(self):
        """CURRENT 版本 + 完整必填字段 → OK"""
        fm = {
            "type": "user",
            "title": "ok",
            "schema_version": CURRENT_SCHEMA_VERSION,
            "created_at": "2025-01-01",
            "item_hash": _FAKE_SHA256,
            "importance": 5,
        }
        result = validate_frontmatter(fm)
        assert result["schema_version"] == CURRENT_SCHEMA_VERSION
        assert result["item_hash"] == _FAKE_SHA256

    def test_validate_frontmatter_rejects_missing_item_hash(self):
        """缺 item_hash → ValueError,提示必填字段"""
        with pytest.raises(ValueError, match="item_hash"):
            validate_frontmatter({
                "type": "user",
                "title": "missing hash",
                "schema_version": CURRENT_SCHEMA_VERSION,
                "created_at": "2025-01-01",
            })

    def test_validate_frontmatter_rejects_short_item_hash(self):
        """item_hash 不是 64 字符 hex → ValueError"""
        with pytest.raises(ValueError, match="item_hash"):
            validate_frontmatter({
                "type": "user",
                "title": "bad hash",
                "schema_version": CURRENT_SCHEMA_VERSION,
                "created_at": "2025-01-01",
                "item_hash": "deadbeef",  # 太短
            })


# ──────────────────────────────────────────────────────────────────
# 6. 回归 —— v0 迁移后必须能过 validate_frontmatter (Issue 2 修复点)
# ──────────────────────────────────────────────────────────────────

class TestMigrationRoundtrip:

    def test_v0_migrated_passes_validate_frontmatter(self, memory_root):
        """
        修复 v0 → v2 后,frontmatter 必须满足 validate_frontmatter 必填字段
        (type / created_at / item_hash / schema_version)
        """
        from agent_core.memory.types import validate_frontmatter
        mem_dir = memory_root / "user"
        mem_dir.mkdir()
        mem_file = mem_dir / "oldnote.md"
        # 极简 v0:只有 title + body,无 type / item_hash / schema_version
        _write_v0_memory(mem_file, title="M7 v0 测试", body="老格式笔记内容")

        result = migrate_file(mem_file)
        # 必填字段都在
        fm = result["frontmatter"]
        assert fm["type"] == "user"
        assert fm["item_hash"] == "0" * 64
        assert fm["schema_version"] == CURRENT_SCHEMA_VERSION
        # validate 不抛
        validated = validate_frontmatter(fm)
        assert validated["schema_version"] == CURRENT_SCHEMA_VERSION

    def test_v0_minimal_body_only(self, memory_root):
        """v0 极端退化:只有 body,无 frontmatter 字段 → 仍应迁移成功且 validate 通过"""
        mem_dir = memory_root / "user"
        mem_dir.mkdir()
        mem_file = mem_dir / "min.md"
        # 退化到只有标题(body 即 title)
        mem_file.write_text(
            "---\n"
            "title: bare\n"
            "---\n\n"
            "body\n",
            encoding="utf-8",
        )
        result = migrate_file(mem_file)
        from agent_core.memory.types import validate_frontmatter
        validated = validate_frontmatter(result["frontmatter"])
        assert validated["schema_version"] == CURRENT_SCHEMA_VERSION


# ──────────────────────────────────────────────────────────────────
# 7. MigrationError 必须支持 cause= (Issue 1 修复点)
# ──────────────────────────────────────────────────────────────────

class TestMigrationErrorBase:

    def test_migration_error_accepts_cause_keyword(self):
        """
        回归:之前 MigrationError(Exception) 不接 cause=,OSError 路径会 TypeError
        修复后继承 AgentError,支持 cause= + from e 双链
        """
        try:
            try:
                raise OSError("disk full (simulated)")
            except OSError as e:
                raise MigrationError("写 .bak 失败", cause=e) from e
        except MigrationError as e:
            assert e.cause is not None
            assert isinstance(e.cause, OSError)
            assert e.__cause__ is e.cause  # from e 也设置
            assert e.code == "MIGRATION_ERROR"
            # __str__ 含 code + cause
            assert "MIGRATION_ERROR" in str(e)
            assert "disk full" in str(e)