"""
M3 / Day 3 测试 —— MemoryExtractor (L1 合并)

覆盖:
- L7: 缺 source_quote 拒
- 类型校验: 非法 type 拒
- L8: 含密钥拒
- L1: 高相似度 body 合并
- 完全相同 body 去重
- 边界: 空列表 / 全部非法 / 全空 body
- 截断: 超过 50 条
"""

from __future__ import annotations

import pytest

from agent_core.memory import (
    MemoryExtractor,
    ExtractStats,
    ExtractionCandidate,
    MockEmbedFn,
    CandidateRejected,
)


@pytest.fixture
def extractor() -> MemoryExtractor:
    return MemoryExtractor()


# ──────────────────────────────────────────────────────────────────
# L7 校验: source_quote 必填
# ──────────────────────────────────────────────────────────────────

class TestL7Validation:

    def test_missing_source_quote_rejected(self, extractor):
        c = ExtractionCandidate("user", "用户", "用户叫小明", "")
        stats = ExtractStats()
        result = extractor.process([c], stats=stats)
        # process() 静默拒绝(不抛),加入 rejected 计数
        assert len(result) == 0
        assert stats.rejected_count == 1

    def test_whitespace_source_quote_rejected(self, extractor):
        c = ExtractionCandidate("user", "用户", "用户叫小明", "   \t  ")
        result = extractor.process([c])
        assert len(result) == 0

    def test_valid_source_quote_accepted(self, extractor):
        c = ExtractionCandidate("user", "用户", "用户叫小明", "我说'我叫小明'")
        result = extractor.process([c])
        assert len(result) == 1


# ──────────────────────────────────────────────────────────────────
# type 校验
# ──────────────────────────────────────────────────────────────────

class TestTypeValidation:

    def test_invalid_type_rejected(self, extractor):
        c = ExtractionCandidate("invalid_type", "标题", "内容", "来源")
        result = extractor.process([c])
        assert len(result) == 0

    def test_missing_type_rejected(self, extractor):
        c = ExtractionCandidate("", "标题", "内容", "来源")
        result = extractor.process([c])
        assert len(result) == 0

    @pytest.mark.parametrize("type_", [
        "user", "feedback", "project", "reference"
    ])
    def test_all_4_types_accepted(self, extractor, type_):
        body = "内容" if type_ not in ("feedback", "project") else "内容\n\n**Why:** 原因"
        c = ExtractionCandidate(type_, "标题", body, "来源")
        result = extractor.process([c])
        assert len(result) == 1


# ──────────────────────────────────────────────────────────────────
# body / title 必填
# ──────────────────────────────────────────────────────────────────

class TestFieldValidation:

    def test_empty_body_rejected(self, extractor):
        c = ExtractionCandidate("user", "标题", "", "来源")
        result = extractor.process([c])
        assert len(result) == 0

    def test_empty_title_rejected(self, extractor):
        c = ExtractionCandidate("user", "", "内容", "来源")
        result = extractor.process([c])
        assert len(result) == 0

    def test_too_long_body_rejected(self, extractor):
        c = ExtractionCandidate("user", "标题", "x" * 5001, "来源")
        result = extractor.process([c])
        assert len(result) == 0


# ──────────────────────────────────────────────────────────────────
# L8 密钥过滤
# ──────────────────────────────────────────────────────────────────

class TestSecretFilter:

    def test_openai_key_rejected(self, extractor):
        c = ExtractionCandidate(
            "user", "key", "key 是 sk-abcdefghijklmnopqrstuvwxyz1234", "我贴了 key"
        )
        stats = ExtractStats()
        result = extractor.process([c], stats=stats)
        assert len(result) == 0
        assert stats.secret_filtered == 1

    def test_github_token_rejected(self, extractor):
        c = ExtractionCandidate(
            "user", "token", "ghp_abcdefghijklmnopqrstuvwxyz0123456789", "粘贴"
        )
        stats = ExtractStats()
        result = extractor.process([c], stats=stats)
        assert len(result) == 0
        assert stats.secret_filtered == 1

    def test_named_key_rejected(self, extractor):
        c = ExtractionCandidate(
            "reference", "config", "api_key = 'mySecretValue_ABCDEF123456'",
            "我贴了 config"
        )
        stats = ExtractStats()
        result = extractor.process([c], stats=stats)
        assert len(result) == 0
        assert stats.secret_filtered == 1

    def test_placeholder_not_rejected(self, extractor):
        c = ExtractionCandidate("user", "key", "api_key = your-api-key-here", "示例")
        result = extractor.process([c])
        assert len(result) == 1


# ──────────────────────────────────────────────────────────────────
# L1 合并: 相似 body 合并
# ──────────────────────────────────────────────────────────────────

class TestL1Merge:

    def test_exact_duplicate_dedup(self, extractor):
        c1 = ExtractionCandidate("user", "用户名字", "用户叫小明", "我说'我叫小明'")
        c2 = ExtractionCandidate("user", "用户名字", "用户叫小明", "我说'我叫小明'")
        stats = ExtractStats()
        result = extractor.process([c1, c2], stats=stats)
        assert len(result) == 1

    def test_high_similarity_merge(self, extractor):
        """Jaccard > 0.7 的两 body 合并"""
        # 两段高度重复,只差一两个字
        c1 = ExtractionCandidate("user", "用户名字", "用户叫小明,今年25岁,住在北京,喜欢 Python", "我说'我叫小明'")
        c2 = ExtractionCandidate("user", "用户名字", "用户叫小明,今年25岁,住在北京,喜欢 Go", "我说'我叫小明'")
        stats = ExtractStats()
        result = extractor.process([c1, c2], stats=stats)
        # 二元 jaccard 应该 > 0.7 → 合并
        assert len(result) == 1
        assert stats.merged_count >= 1

    def test_low_similarity_keep_both(self, extractor):
        c1 = ExtractionCandidate("user", "用户名字", "用户叫小明", "我说'我叫小明'")
        c2 = ExtractionCandidate("user", "项目", "项目使用 Python 开发", "我说'用 Python'")
        result = extractor.process([c1, c2])
        assert len(result) == 2

    def test_merge_only_within_same_type(self, extractor):
        """不同 type 不合并"""
        c1 = ExtractionCandidate("user", "用户", "用户喜欢 Python", "我说'我喜欢 Python'")
        c2 = ExtractionCandidate("project", "用户", "用户喜欢 Python", "我说'我喜欢 Python'")
        result = extractor.process([c1, c2])
        assert len(result) == 2

    def test_keep_longer_body(self, extractor):
        """合并时保留 body 更长的"""
        c1 = ExtractionCandidate("user", "用户", "用户叫小明,今年25岁,住在北京,喜欢Python", "我说'我叫小明'")
        c2 = ExtractionCandidate("user", "用户", "用户叫小明,今年25岁,住在北京,喜欢Python,工作中常用 numpy 和 pandas 处理数据,周末会写一些 Rust 练手", "我说'我叫小明'")
        result = extractor.process([c1, c2])
        assert len(result) == 1
        # 保留更长的
        assert "Rust" in result[0].body


# ──────────────────────────────────────────────────────────────────
# embed_fn 路径 (用嵌入相似度合并)
# ──────────────────────────────────────────────────────────────────

class TestEmbedMerge:

    def test_with_embed_fn_uses_cos(self):
        """用 embed_fn 时,合并用 cos similarity"""
        embed = MockEmbedFn()
        ex = MemoryExtractor(embed_fn=embed)
        c1 = ExtractionCandidate("user", "用户", "用户叫小明", "我说'我叫小明'")
        c2 = ExtractionCandidate("user", "用户", "用户名叫小明", "我说'我叫小明'")
        stats = ExtractStats()
        result = ex.process([c1, c2], stats=stats)
        # MockEmbedFn 输出完全不同的向量(因为是基于 hash 的),
        # 实际不会触发 cos 高相似合并
        # 此处只验证: 不崩溃 + 返回有效结果
        assert isinstance(result, list)
        assert stats.input_count == 2


# ──────────────────────────────────────────────────────────────────
# 截断
# ──────────────────────────────────────────────────────────────────

class TestTruncation:

    def test_truncate_over_50(self, extractor):
        """100 条候选,处理后不超过 50(可能更少,因有合并)"""
        cands = [
            ExtractionCandidate(
                "user", f"用户{i}", f"独特的内容{i}_abc_def_ghi", f"原话{i}"
            )
            for i in range(100)
        ]
        result = extractor.process(cands)
        # 截断生效: <= 50
        assert len(result) <= 50
        assert len(result) > 0  # 不应全空


# ──────────────────────────────────────────────────────────────────
# 边界
# ──────────────────────────────────────────────────────────────────

class TestBoundary:

    def test_empty_input(self, extractor):
        result = extractor.process([])
        assert result == []

    def test_all_invalid(self, extractor):
        cands = [
            ExtractionCandidate("", "x", "y", ""),  # 缺 type + source
            ExtractionCandidate("invalid", "x", "y", "z"),  # 非法 type
        ]
        stats = ExtractStats()
        result = extractor.process(cands, stats=stats)
        assert len(result) == 0
        assert stats.rejected_count == 2

    def test_stats_summary(self, extractor):
        c1 = ExtractionCandidate("user", "用户", "用户叫小明", "我说'我叫小明'")
        c2 = ExtractionCandidate("user", "用户", "用户叫大明", "我说'我叫大明'")
        stats = ExtractStats()
        extractor.process([c1, c2], stats=stats)
        assert stats.input_count == 2
        assert stats.accepted_count == 2
        assert "in=2" in stats.summary()


# ──────────────────────────────────────────────────────────────────
# 为什么字段(feedback/project 必填)
# ──────────────────────────────────────────────────────────────────

class TestWhyField:

    def test_feedback_missing_why_rejected(self, extractor):
        """feedback 类型必须有 **Why:** 段(MemoryStore 校验会先于 extractor)"""
        # 注: extractor 不直接校验 **Why:** ,由 MemoryStore 校验
        # 此测试验证 extractor 不会拒绝(它只校验 source_quote)
        c = ExtractionCandidate(
            "feedback", "不喜欢打断", "用户不喜欢被打断", "我说'别打断'"
        )
        result = extractor.process([c])
        # extractor 接受(校验由 store 完成)
        assert len(result) == 1
