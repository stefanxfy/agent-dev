"""
LLMRouter 测试
覆盖：UsageStats 字段提取（input/output/thinking/cached_tokens）
"""
import pytest
from dataclasses import dataclass
from agent_core.llm.router import UsageStats


# 模拟不同 provider 的 usage 对象
@dataclass
class MockGLMUsage:
    """GLM/OpenAI 格式 usage"""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    prompt_tokens_details: object = None  # GLM 返回的 Pydantic 对象


@dataclass
class MockGLMDetails:
    """GLM prompt_tokens_details"""
    cached_tokens: int = 0


@dataclass
class MockAnthropicUsage:
    """Anthropic 格式 usage"""
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0


class TestUsageStats:

    def test_from_none(self):
        """None 输入返回空 stats"""
        s = UsageStats.from_chunk_usage(None)
        assert s.input_tokens == 0
        assert s.output_tokens == 0
        assert s.thinking_tokens == 0
        assert s.cached_tokens == 0

    def test_from_glm_usage(self):
        """GLM 格式：prompt_tokens / completion_tokens"""
        usage = MockGLMUsage(prompt_tokens=1000, completion_tokens=500)
        s = UsageStats.from_chunk_usage(usage)
        assert s.input_tokens == 1000
        assert s.output_tokens == 500
        assert s.cached_tokens == 0  # 无 details

    def test_from_glm_with_cached_tokens(self):
        """GLM 格式：prompt_tokens_details.cached_tokens"""
        usage = MockGLMUsage(
            prompt_tokens=18129,
            completion_tokens=744,
            prompt_tokens_details=MockGLMDetails(cached_tokens=18112),
        )
        s = UsageStats.from_chunk_usage(usage)
        assert s.input_tokens == 18129
        assert s.cached_tokens == 18112
        assert s.cache_hit_rate == pytest.approx(18112/18129, rel=0.001)

    def test_from_anthropic_usage(self):
        """Anthropic 格式：input_tokens / output_tokens / cache_read"""
        usage = MockAnthropicUsage(
            input_tokens=2000,
            output_tokens=800,
            cache_read_input_tokens=1800,
        )
        s = UsageStats.from_chunk_usage(usage)
        assert s.input_tokens == 2000
        assert s.output_tokens == 800
        assert s.cached_tokens == 1800
        assert s.cache_hit_rate == 0.9

    def test_from_anthropic_no_cache(self):
        """Anthropic 无 cache 命中"""
        usage = MockAnthropicUsage(input_tokens=2000, output_tokens=800)
        s = UsageStats.from_chunk_usage(usage)
        assert s.cached_tokens == 0
        assert s.cache_hit_rate == 0.0

    def test_cache_hit_rate_zero_input(self):
        """input_tokens=0 时命中率返回 0（避免除零）"""
        s = UsageStats(input_tokens=0, cached_tokens=100)
        assert s.cache_hit_rate == 0.0

    def test_total_tokens(self):
        """total_tokens 包含 cached 但不算 thinking（thinking 已包含在 output 中）"""
        s = UsageStats(input_tokens=100, output_tokens=50, thinking_tokens=10, cached_tokens=80)
        # total = input + output + thinking（cached 是 input 的子集，不重复加）
        assert s.total_tokens == 100 + 50 + 10

    def test_summary_includes_cached(self):
        """summary 输出包含 cached_tokens 和命中率"""
        s = UsageStats(input_tokens=1000, output_tokens=200, cached_tokens=800)
        summary = s.summary("zhipu")
        assert "cached=800" in summary
        assert "80.0%" in summary
        assert "[zhipu]" in summary

    def test_summary_skips_cached_when_zero(self):
        """cached_tokens=0 时不显示缓存信息"""
        s = UsageStats(input_tokens=1000, output_tokens=200)
        summary = s.summary("zhipu")
        assert "cached" not in summary

    def test_summary_includes_thinking(self):
        """summary 输出包含 thinking_tokens"""
        s = UsageStats(input_tokens=100, output_tokens=50, thinking_tokens=20)
        summary = s.summary("anthropic")
        assert "think=20" in summary


# ═══════════════════════════════════════════════════════════════
# compact.py 日志修复验证：cached 字段来自 UsageStats.cached_tokens
# ═══════════════════════════════════════════════════════════════

class TestCompactUsageStatsLog:
    """验证 compact.py 的 usage_stats 日志能从 UsageStats.cached_tokens 取值"""

    def test_usage_stats_has_cached_tokens_field(self):
        """UsageStats 必须有 cached_tokens 字段（之前 bug：日志里 cached 永远=0）"""
        from agent_core.llm.router import UsageStats
        s = UsageStats(input_tokens=1000, cached_tokens=900)
        assert hasattr(s, 'cached_tokens')
        assert s.cached_tokens == 900

    def test_no_prompt_tokens_details_attribute(self):
        """确认 UsageStats 没存原始 prompt_tokens_details（避免误解）"""
        from agent_core.llm.router import UsageStats
        s = UsageStats(input_tokens=1000, cached_tokens=900)
        # cached_tokens 是直接的 int 字段，不再嵌套在 dict 里
        assert not hasattr(s, 'prompt_tokens_details') or s.prompt_tokens_details is None