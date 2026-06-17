"""
LLMRouter 测试
覆盖：UsageStats 字段提取（input/output/thinking/cached_tokens） +
Fork 模式下 system_prompt_override 在 zhipu/openai 路径的行为
"""
import pytest
from dataclasses import dataclass
from agent_core.llm.router import UsageStats, LLMConfig, LLMRouter, LLMProvider


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

# ── Fork 模式 system_prompt_override 测试 ─────────────────────
# 背景：Fork 压缩场景下，主 agent 的 system_prompt 可能为空串。
#       主 agent 路径不会注入 system message 到 messages 里。
#       但 router.py 的 zhipu/openai 分支原本用 `is not None` 判断
#       空字符串，导致空 system 被注入，破坏 cache prefix 对齐。
# 修复：空 system_prompt_override 应与 None 等价，不注入。

class TestForkSystemPromptOverride:
    """Fork 模式下 system_prompt_override 的处理（cache prefix 对齐关键）

    Bug 背景：主 agent 路径 system_prompt="" 时不发送 system message，
    但 router.py 的 zhipu/openai 分支原本用 `is not None` 判断空字符串，
    导致空 system 被注入，破坏 cache prefix 对齐。
    修复后：空字符串与 None 等价（truthy check），保持与主 agent 路径一致。
    """

    def _make_router(self, provider: str) -> LLMRouter:
        cfg = LLMConfig(
            provider=provider,
            model="GLM-5.1" if provider == "zhipu" else "gpt-4o",
            api_key="test",
            system_prompt="",  # 默认空
        )
        return LLMRouter(cfg)

    def _capture_zhipu_messages(self, router, messages, system_prompt_override):
        """Monkey-patch _chat_zhipu 捕获实际发给 zhipu 的 messages"""
        captured = {}
        from agent_core.llm import router as router_module
        RouterClass = router_module.LLMRouter
        original = RouterClass._chat_zhipu

        def mock_chat_zhipu(self, msgs, tools, tool_choice=None):
            captured["messages"] = msgs
            captured["tools"] = tools
            captured["tool_choice"] = tool_choice
            return iter([])  # 空生成器

        RouterClass._chat_zhipu = mock_chat_zhipu
        try:
            list(router.chat(
                messages=messages,
                tools=None,
                system_prompt_override=system_prompt_override,
            ))
        finally:
            RouterClass._chat_zhipu = original
        return captured

    def _capture_openai_messages(self, router, messages, system_prompt_override):
        """Monkey-patch _chat_openai 捕获实际发给 openai 的 messages"""
        captured = {}
        from agent_core.llm import router as router_module
        RouterClass = router_module.LLMRouter
        original = RouterClass._chat_openai

        def mock_chat_openai(self, msgs, tools, tool_choice=None):
            captured["messages"] = msgs
            captured["tools"] = tools
            captured["tool_choice"] = tool_choice
            return iter([])

        RouterClass._chat_openai = mock_chat_openai
        try:
            list(router.chat(
                messages=messages,
                tools=None,
                system_prompt_override=system_prompt_override,
            ))
        finally:
            RouterClass._chat_openai = original
        return captured

    def test_zhipu_empty_override_does_not_inject_system(self):
        """zhipu: 空 system_prompt_override 不应注入空 system message（保持与主 agent 路径一致）"""
        router = self._make_router("zhipu")
        messages = [{"role": "user", "content": "hi"}]
        captured = self._capture_zhipu_messages(router, messages, system_prompt_override="")
        # 关键断言：不注入空 system，第一个 message 仍是原 user
        assert captured["messages"][0] == {"role": "user", "content": "hi"}, \
            f"zhipu 空 override 不应注入 system，实际收到: {captured['messages']}"

    def test_openai_empty_override_does_not_inject_system(self):
        """openai: 空 system_prompt_override 不应注入空 system message"""
        router = self._make_router("openai")
        messages = [{"role": "user", "content": "hi"}]
        captured = self._capture_openai_messages(router, messages, system_prompt_override="")
        assert captured["messages"][0] == {"role": "user", "content": "hi"}, \
            f"openai 空 override 不应注入 system，实际收到: {captured['messages']}"

    def test_zhipu_none_override_no_system(self):
        """zhipu: system_prompt_override=None 时也不应注入 system"""
        router = self._make_router("zhipu")
        messages = [{"role": "user", "content": "hi"}]
        captured = self._capture_zhipu_messages(router, messages, system_prompt_override=None)
        assert captured["messages"][0] == {"role": "user", "content": "hi"}

    def test_zhipu_non_empty_override_injects_system(self):
        """zhipu: 非空 system_prompt_override 应当注入（Fork 模式正常用法）"""
        router = self._make_router("zhipu")
        messages = [{"role": "user", "content": "hi"}]
        captured = self._capture_zhipu_messages(router, messages, system_prompt_override="You are helpful")
        assert captured["messages"][0] == {"role": "system", "content": "You are helpful"}
        assert captured["messages"][1] == {"role": "user", "content": "hi"}

    def test_zhipu_skip_system_in_messages_when_override(self):
        """zhipu: override 非空时跳过 messages 里已有的 system（避免重复）"""
        router = self._make_router("zhipu")
        # messages 里已经有 system（主 agent 加的）
        messages = [
            {"role": "system", "content": "OLD SYSTEM"},
            {"role": "user", "content": "hi"},
        ]
        captured = self._capture_zhipu_messages(router, messages, system_prompt_override="NEW SYSTEM")
        # 第一个必须是 NEW SYSTEM（override），OLD 被替换
        assert captured["messages"][0] == {"role": "system", "content": "NEW SYSTEM"}
        # 不应有两个 system
        system_count = sum(1 for m in captured["messages"] if m.get("role") == "system")
        assert system_count == 1, f"应只有一个 system，实际 {system_count} 个"
