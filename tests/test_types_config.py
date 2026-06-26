"""
M1 / Day 1 测试 —— 类型系统 + 配置 + 路径校验

覆盖:
- types.py: 10 个 case（4 类封闭 + frontmatter schema + body 校验）
- config.py: 6 个 case（Pydantic 校验 + 跨字段 + env 解析）
- path_validator.py: 8 个 case（4 层防御 + Unicode + 越界 + 白名单）

总计: 24 个 case（plan 要求 ≥20）
"""

from __future__ import annotations

import os
import platform
import tempfile
from pathlib import Path

import pytest
from pydantic import ValidationError

from agent_core.memory.types import (
    CURRENT_SCHEMA_VERSION,
    validate_body,
    validate_frontmatter,
    validate_type,
    all_types,
    type_description,
)
from agent_core.memory.config import (
    DistillationConfig,
    MemoryConfig,
    PathsConfig,
    RetrievalConfig,
    SafetyConfig,
)
from agent_core.memory.path_validator import (
    MemoryPathValidator,
    PathSecurityError,
)


# ──────────────────────────────────────────────────────────────────
# Part 1: types.py —— 10 个 case
# ──────────────────────────────────────────────────────────────────

class TestValidateType:
    """4 封闭类型校验（v2.1 §5.3.1）"""

    def test_all_four_types_accepted(self):
        """合法 4 类全部通过"""
        for t in ("user", "feedback", "project", "reference"):
            assert validate_type(t) == t

    def test_llm_invented_type_rejected(self):
        """LLM 试图发明的第 5 类被拒绝（v2.1 §4.5 #6）"""
        with pytest.raises(ValueError, match="非法记忆类型"):
            validate_type("episodic")

    def test_case_sensitive(self):
        """大小写敏感（'User' 不是 'user'）"""
        with pytest.raises(ValueError, match="非法记忆类型"):
            validate_type("User")

    def test_non_string_rejected(self):
        """非字符串直接拒"""
        for bad in (None, 42, [], {}, b"user"):
            with pytest.raises(ValueError, match="必须是字符串"):
                validate_type(bad)

    def test_all_types_returns_four(self):
        """all_types() 返回 4 类完整列表"""
        types = all_types()
        assert len(types) == 4
        assert set(types) == {"user", "feedback", "project", "reference"}

    def test_type_description_has_all_four(self):
        """每个类型都有语义描述（给 LLM prompt 用）"""
        for t in all_types():
            desc = type_description(t)
            assert isinstance(desc, str)
            assert len(desc) > 0


def _good_frontmatter() -> dict:
    return {
        "type": "user",
        "created_at": "2026-06-20T10:00:00Z",
        "item_hash": "a" * 64,
        "schema_version": CURRENT_SCHEMA_VERSION,
        "name": "默认名",
        "description": "默认描述",
    }


class TestValidateFrontmatter:
    """frontmatter schema 校验"""

    def test_minimal_valid_frontmatter(self):
        """最小合法 frontmatter（仅必填字段）"""
        fm = validate_frontmatter(_good_frontmatter())
        assert fm["type"] == "user"
        assert fm["schema_version"] == CURRENT_SCHEMA_VERSION

    def test_missing_required_field_rejected(self):
        """缺必填字段（item_hash）被拒"""
        bad = _good_frontmatter()
        del bad["item_hash"]
        with pytest.raises(ValueError, match="缺少必填字段"):
            validate_frontmatter(bad)

    def test_invalid_iso8601_rejected(self):
        """created_at 非 ISO 8601 被拒"""
        bad = _good_frontmatter()
        bad["created_at"] = "not-a-date"
        with pytest.raises(ValueError, match="ISO 8601"):
            validate_frontmatter(bad)

    def test_item_hash_must_be_64_hex(self):
        """item_hash 必须是 64 字符 hex（SHA-256）"""
        bad = _good_frontmatter()
        bad["item_hash"] = "abc123"  # 太短
        with pytest.raises(ValueError, match="64 字符 hex"):
            validate_frontmatter(bad)

        bad2 = _good_frontmatter()
        bad2["item_hash"] = "Z" * 64  # 非 hex
        with pytest.raises(ValueError, match="64 字符 hex"):
            validate_frontmatter(bad2)

    # ───────────────────────────────────────────────
    # M11 v3 新增字段 name / description 校验
    # ───────────────────────────────────────────────

    def test_validate_frontmatter_v3_requires_name(self):
        """M11 schema v3 必填 name"""
        from agent_core.memory.types import FrontmatterError
        fm = {
            "type": "user",
            "created_at": "2026-06-26T00:00:00+00:00",
            "item_hash": "a" * 64,
            "schema_version": 3,
            "description": "desc",  # 缺 name
        }
        with pytest.raises(FrontmatterError, match="name"):
            validate_frontmatter(fm)

    def test_validate_frontmatter_v3_requires_description(self):
        """M11 schema v3 必填 description"""
        from agent_core.memory.types import FrontmatterError
        fm = {
            "type": "user",
            "created_at": "2026-06-26T00:00:00+00:00",
            "item_hash": "a" * 64,
            "schema_version": 3,
            "name": "x",  # 缺 description
        }
        with pytest.raises(FrontmatterError, match="description"):
            validate_frontmatter(fm)

    def test_validate_frontmatter_v3_accepts_full(self):
        """M11 v3 6 字段必填齐全 → 通过"""
        fm = {
            "type": "user",
            "created_at": "2026-06-26T00:00:00+00:00",
            "item_hash": "a" * 64,
            "schema_version": 3,
            "name": "用户叫小明",
            "description": "Python 后端工程师",
        }
        validate_frontmatter(fm)  # 不抛

    def test_description_too_long_truncated_with_warning(self, caplog):
        """description > 200 字符 → 截断 + warning(caplog)"""
        import logging
        fm = {
            "type": "user",
            "created_at": "2026-06-26T00:00:00+00:00",
            "item_hash": "a" * 64,
            "schema_version": 3,
            "name": "x",
            "description": "a" * 500,
        }
        with caplog.at_level(logging.WARNING, logger="agent_core.memory.types"):
            validate_frontmatter(fm)
        assert len(fm["description"]) == 200
        assert ("description 截断" in caplog.text
                or "truncated" in caplog.text.lower()
                or "过长" in caplog.text)

    def test_schema_version_below_3_rejected(self):
        """M11:schema_version < 3 被拒"""
        from agent_core.memory.types import FrontmatterError
        fm = {
            "type": "user",
            "created_at": "2026-06-26T00:00:00+00:00",
            "item_hash": "a" * 64,
            "schema_version": 2,  # M10
            "name": "x",
            "description": "d",
        }
        with pytest.raises(FrontmatterError, match=">=3"):
            validate_frontmatter(fm)


class TestValidateBody:
    """v2.1 §4.5 #7 不变量: feedback/project 必须含 **Why:**"""

    def test_feedback_must_have_why(self):
        """feedback 缺 **Why:** 被拒"""
        with pytest.raises(ValueError, match=r"\*\*Why:\*\*"):
            validate_body("feedback", "用户不喜欢打断对话。")

    def test_project_must_have_why(self):
        """project 缺 **Why:** 被拒"""
        with pytest.raises(ValueError, match=r"\*\*Why:\*\*"):
            validate_body("project", "用 ChromaDB 做向量存储。")

    def test_user_does_not_require_why(self):
        """user 类不强制 **Why:**（避免矫枉过正）"""
        # 不应抛异常
        validate_body("user", "用户叫小明。")

    def test_feedback_with_why_passes(self):
        """feedback 含 **Why:** 通过"""
        body = "用户讨厌并行打断。\n\n**Why:** 之前在多 agent 场景被打断过，影响效率。"
        validate_body("feedback", body)


# ──────────────────────────────────────────────────────────────────
# Part 2: config.py —— 6 个 case
# ──────────────────────────────────────────────────────────────────

class TestRetrievalConfig:
    """RetrievalConfig + 跨字段校验（weights sum = 1）"""

    def test_default_weights_sum_to_one(self):
        """默认 hybrid 模式权重之和 = 1"""
        cfg = RetrievalConfig()
        assert cfg.mode == "hybrid"
        assert abs(cfg.semantic_weight + cfg.lexical_weight - 1.0) < 1e-6

    def test_weights_must_sum_to_one(self):
        """hybrid 模式权重 != 1 被拒"""
        with pytest.raises(ValidationError, match="必须 = 1.0"):
            RetrievalConfig(mode="hybrid", semantic_weight=0.5, lexical_weight=0.3)

    def test_non_hybrid_mode_skips_weight_check(self):
        """非 hybrid 模式不要求权重之和 = 1（vector/file 单独走）"""
        # 不应抛异常
        RetrievalConfig(mode="vector", semantic_weight=0.0, lexical_weight=0.0)

    def test_top_k_bounds_enforced(self):
        """top_k 越界被拒"""
        with pytest.raises(ValidationError):
            RetrievalConfig(top_k=0)        # ge=1
        with pytest.raises(ValidationError):
            RetrievalConfig(top_k=100)      # le=50


class TestMemoryConfig:
    """顶层 MemoryConfig 集成"""

    def test_default_construction(self):
        """默认构造（全部子 config 用默认值）"""
        cfg = MemoryConfig()
        assert cfg.enabled is True
        assert cfg.retrieval.mode == "hybrid"
        assert cfg.distillation.enabled is True
        assert cfg.embed_model == "BAAI/bge-m3"  # v2.1 §九.1 默认

    def test_from_env_overrides_nested(self):
        """from_env 支持 MEMORY_RETRIEVAL__TOP_K 嵌套覆盖"""
        os.environ["MEMORY_RETRIEVAL__TOP_K"] = "20"
        os.environ["MEMORY_DISTILLATION__ENABLED"] = "false"
        os.environ["MEMORY_EMBED_MODEL"] = "all-MiniLM-L6-v2"
        try:
            cfg = MemoryConfig.from_env()
            assert cfg.retrieval.top_k == 20
            assert cfg.distillation.enabled is False
            assert cfg.embed_model == "all-MiniLM-L6-v2"
        finally:
            del os.environ["MEMORY_RETRIEVAL__TOP_K"]
            del os.environ["MEMORY_DISTILLATION__ENABLED"]
            del os.environ["MEMORY_EMBED_MODEL"]


# ──────────────────────────────────────────────────────────────────
# Part 3: path_validator.py —— 8 个 case
# ──────────────────────────────────────────────────────────────────

class TestPathValidator:
    """4 层防御路径校验"""

    @pytest.fixture
    def v(self, tmp_path):
        """创建一个临时 sandbox"""
        memory_root = tmp_path / "memory"
        memory_root.mkdir()
        return MemoryPathValidator(memory_root)

    # ── L1 绝对路径 ──

    def test_absolute_path_rejected(self, v):
        """L1: 绝对路径被拒"""
        with pytest.raises(PathSecurityError, match="绝对路径"):
            v.validate("/etc/passwd")

    @pytest.mark.skipif(platform.system() != "Windows", reason="仅 Windows 测试盘符")
    def test_windows_drive_rejected(self, v):
        """L1: Windows 盘符 C:\\ 被拒"""
        with pytest.raises(PathSecurityError, match="Windows 盘符"):
            v.validate("C:\\Windows\\System32")

    # ── L2 normpath 越界 ──

    def test_relative_traversal_rejected(self, v):
        """L2: ../../etc/passwd 被拒"""
        with pytest.raises(PathSecurityError, match=r"\.\. 穿越"):
            v.validate("../../etc/passwd")

    def test_mid_path_traversal_rejected(self, v):
        """L2: 路径中部含 ..（user/../../../etc）被拒"""
        with pytest.raises(PathSecurityError):
            v.validate("user/../../../etc/passwd")

    # ── L3 沙箱内 ──

    def test_valid_path_resolves(self, v):
        """合法路径正确解析（validator 只校验，不创建目录 —— 创建由调用方负责）"""
        real = v.validate("user/foo.md")
        assert real == (v.root / "user" / "foo.md")
        # 注意: validator 不创建父目录, 创建由 caller 负责（dual_channel_writer / editor）

    # ── L4 Unicode ──

    def test_rtl_override_rejected(self, v):
        """L4: Unicode RLO (‮) 被拒"""
        with pytest.raises(PathSecurityError, match="Unicode"):
            v.validate("user/‮foo.md")

    def test_zero_width_rejected(self, v):
        """L4: 零宽字符 (U+200B) 被拒"""
        with pytest.raises(PathSecurityError, match="Unicode|Format"):
            v.validate("user/foo​.md")

    def test_fullwidth_slash_rejected(self, v):
        """L4: 全角斜杠 (／) 被拒"""
        with pytest.raises(PathSecurityError, match="Unicode"):
            v.validate("user／admin／passwd")

    # ── 白名单 ──

    def test_disallowed_extension_rejected(self, v):
        """禁止的扩展名（.py / .sh / .env）被拒"""
        with pytest.raises(PathSecurityError, match="扩展名"):
            v.validate("user/run.py")

    def test_disallowed_top_dir_rejected(self, v):
        """非 4 类子目录（admin / secret）被拒"""
        with pytest.raises(PathSecurityError, match="非法子目录"):
            v.validate("admin/foo.md")

    # ── 工具方法 ──

    def test_is_within_sandbox(self, v):
        """is_within_sandbox 不抛异常"""
        real = v.root / "user" / "foo.md"
        assert v.is_within_sandbox(real) is True
        assert v.is_within_sandbox("/etc/passwd") is False


# ──────────────────────────────────────────────────────────────────
# Sanity: 默认子配置可构造（防止 from_env 默认值坏掉）
# ──────────────────────────────────────────────────────────────────

def test_all_subconfigs_default_constructible():
    """4 个子 config 默认值都合法"""
    RetrievalConfig()
    DistillationConfig()
    PathsConfig()
    SafetyConfig()
    MemoryConfig()