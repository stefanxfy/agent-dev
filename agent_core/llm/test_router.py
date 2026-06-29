"""
LLMRouter 测试
覆盖：UsageStats 字段提取（input/output/thinking/cached_tokens） +
Fork 模式下 system_prompt_override 在 zhipu/openai 路径的行为

Stage 4 后的差异:
- `_ThinkTagSplitter` 改为 `MiniMaxProvider._ThinkTagSplitter()`(已 nest 进 provider)
- `_get_openai_provider` 改为 `ProviderRegistry.create`(稳定 classmethod 锚点)
"""
import pytest
from dataclasses import dataclass
from unittest.mock import patch, MagicMock
from agent_core.llm.router import (
    UsageStats, LLMConfig, LLMRouter, LLMProvider, LLMModel, StreamChunk,
    EmptyResponseError, InvokeTimeoutError, TextDelta,
)
from agent_core.llm.providers.openai import MiniMaxProvider

# _ThinkTagSplitter 改用 provider 嵌套版本
_ThinkTagSplitter = MiniMaxProvider._ThinkTagSplitter


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

    def _capture_provider_messages(self, router, messages, system_prompt_override):
        """Monkey-patch ProviderRegistry.create 捕获实际创建的 provider。

        Stage 4 后 LLMRouter 走 ProviderRegistry.create 拿到 BaseProvider 实例。
        3 个 OpenAI 兼容 provider 共用同一分发点,所以只需 patch 这一个 classmethod。
        """
        from agent_core.llm.registry import ProviderRegistry
        from agent_core.llm.providers.base import BaseProvider
        captured = {}
        original = ProviderRegistry.create

        def mock_create(config):
            class _FakeProvider(BaseProvider):
                provider_name = "fake"
                def _do_chat(self_, messages, tools=None, tool_choice=None,
                             system_prompt=None, cache_namespace=None):
                    # 模拟 OpenAI 兼容 provider 的 system 注入行为(保持与生产一致)
                    if system_prompt:
                        messages = [{"role": "system", "content": system_prompt}, *messages]
                    captured["messages"] = messages
                    captured["tools"] = tools
                    captured["tool_choice"] = tool_choice
                    return iter([])  # 空生成器
            return _FakeProvider(config)

        ProviderRegistry.create = classmethod(lambda cls, config: mock_create(config))
        try:
            list(router.chat(
                messages=messages,
                tools=None,
                system_prompt_override=system_prompt_override,
            ))
        finally:
            ProviderRegistry.create = original
        return captured

    def _capture_zhipu_messages(self, router, messages, system_prompt_override):
        return self._capture_provider_messages(router, messages, system_prompt_override)

    def _capture_openai_messages(self, router, messages, system_prompt_override):
        return self._capture_provider_messages(router, messages, system_prompt_override)

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


# ═══════════════════════════════════════════════════════════════
# MiniMax (MiniMax) provider 测试
# 覆盖:enum 注册 / LLMConfig 接受 / chat() 路由 / base_url / system_prompt_override
# 文档:https://platform.minimaxi.com/docs/api-reference/text-openai-api
# ═══════════════════════════════════════════════════════════════

class TestMinimaxProvider:
    """MiniMax (MiniMax) provider 接入测试 — OpenAI 兼容端点"""

    def test_minimax_provider_enum_exists(self):
        """LLMProvider.MINIMAX 必须存在(值 'minimax')"""
        assert hasattr(LLMProvider, "MINIMAX")
        assert LLMProvider.MINIMAX.value == "minimax"

    def test_minimax_model_enum_registered(self):
        """LLMModel 必须包含 MiniMax 模型名(MiniMax-Text-01)"""
        assert hasattr(LLMModel, "MINIMAX_TEXT_01")
        assert LLMModel.MINIMAX_TEXT_01.value == "MiniMax-Text-01"

    def test_llm_config_accepts_minimax_provider(self):
        """LLMConfig(provider='minimax') 不报错"""
        cfg = LLMConfig(
            provider="minimax",
            model="MiniMax-Text-01",
            api_key="test-key",
        )
        assert cfg.provider == LLMProvider.MINIMAX
        assert cfg.model == "MiniMax-Text-01"

    def test_minimax_routes_to_chat_minimax(self):
        """chat() 收到 provider='minimax' 必须路由到 MiniMaxProvider(而不是 zhipu/openai)
        Stage 4 后:走 ProviderRegistry.create() → MiniMaxProvider
        """
        cfg = LLMConfig(
            provider="minimax",
            model="MiniMax-Text-01",
            api_key="test-key",
        )
        router = LLMRouter(cfg)

        # Monkey-patch ProviderRegistry.create,验证 chat() 实际调用了工厂
        from agent_core.llm.registry import ProviderRegistry
        from agent_core.llm.providers.base import BaseProvider
        called = {"flag": False, "provider": None}
        original = ProviderRegistry.create

        def mock_create(config):
            called["flag"] = True
            class _FakeProvider(BaseProvider):
                provider_name = "fake"
                def _do_chat(self_, messages, tools=None, tool_choice=None,
                             system_prompt=None, cache_namespace=None):
                    return iter([])
            return _FakeProvider(config)

        ProviderRegistry.create = classmethod(lambda cls, config: mock_create(config))
        try:
            list(router.chat(messages=[{"role": "user", "content": "hi"}]))
        finally:
            ProviderRegistry.create = original
        assert called["flag"] is True, "chat() 没把 provider='minimax' 路由到 ProviderRegistry.create()"

    def test_minimax_empty_override_does_not_inject_system(self):
        """minimax: 空 system_prompt_override 不应注入空 system(与 zhipu 行为一致,保持 cache prefix 对齐)"""
        cfg = LLMConfig(
            provider="minimax",
            model="MiniMax-Text-01",
            api_key="test-key",
            system_prompt="",
        )
        router = LLMRouter(cfg)
        from agent_core.llm.registry import ProviderRegistry
        from agent_core.llm.providers.base import BaseProvider
        captured = {}
        original = ProviderRegistry.create

        def mock_create(config):
            class _FakeProvider(BaseProvider):
                provider_name = "fake"
                def _do_chat(self_, messages, tools=None, tool_choice=None,
                             system_prompt=None, cache_namespace=None):
                    # 模拟 OpenAI 兼容 provider 的 system 注入行为
                    if system_prompt:
                        messages = [{"role": "system", "content": system_prompt}, *messages]
                    captured["messages"] = messages
                    return iter([])
            return _FakeProvider(config)

        ProviderRegistry.create = classmethod(lambda cls, config: mock_create(config))
        try:
            list(router.chat(
                messages=[{"role": "user", "content": "hi"}],
                system_prompt_override="",
            ))
        finally:
            ProviderRegistry.create = original
        # 第一个 message 仍是 user(空 override 不注入)
        assert captured["messages"][0] == {"role": "user", "content": "hi"}, \
            f"minimax 空 override 不应注入 system,实际收到: {captured['messages']}"

    def test_minimax_non_empty_override_injects_system(self):
        """minimax: 非空 override 必须注入到 messages 头(Fork 模式正常用法)"""
        cfg = LLMConfig(
            provider="minimax",
            model="MiniMax-Text-01",
            api_key="test-key",
        )
        router = LLMRouter(cfg)
        from agent_core.llm.registry import ProviderRegistry
        from agent_core.llm.providers.base import BaseProvider
        captured = {}
        original = ProviderRegistry.create

        def mock_create(config):
            class _FakeProvider(BaseProvider):
                provider_name = "fake"
                def _do_chat(self_, messages, tools=None, tool_choice=None,
                             system_prompt=None, cache_namespace=None):
                    # 模拟 OpenAI 兼容 provider 的 system 注入行为
                    if system_prompt:
                        messages = [{"role": "system", "content": system_prompt}, *messages]
                    captured["messages"] = messages
                    return iter([])
            return _FakeProvider(config)

        ProviderRegistry.create = classmethod(lambda cls, config: mock_create(config))
        try:
            list(router.chat(
                messages=[{"role": "user", "content": "hi"}],
                system_prompt_override="You are helpful",
            ))
        finally:
            ProviderRegistry.create = original
        assert captured["messages"][0] == {"role": "system", "content": "You are helpful"}
        assert captured["messages"][1] == {"role": "user", "content": "hi"}

    def test_minimax_client_default_base_url(self):
        """MiniMaxProvider.default_base_url 必须是 https://api.minimaxi.com/v1
        源码级校验(.venv 没装 openai,不能 import openai)
        """
        import inspect
        from agent_core.llm.providers.openai import MiniMaxProvider, OpenAICompatibleProvider
        src = inspect.getsource(MiniMaxProvider)
        assert "https://api.minimaxi.com/v1" in src, (
            "MiniMaxProvider.default_base_url 应该是 https://api.minimaxi.com/v1,"
            f"实际源码:\n{src}"
        )
        # base_url 覆盖逻辑在基类 OpenAICompatibleProvider._get_base_url() 里
        # (config.base_url or default_base_url)
        base_src = inspect.getsource(OpenAICompatibleProvider._get_base_url)
        assert "self.config.base_url" in base_src, (
            "OpenAICompatibleProvider._get_base_url 必须支持 base_url 覆盖,"
            f"实际源码:\n{base_src}"
        )

    def test_minimax_config_registers_api_key(self):
        """config.minimax_api_key 必须能从 MINIMAX_API_KEY env 读出"""
        import os
        from agent_core.config import config as _config
        os.environ["MINIMAX_API_KEY"] = "test-minimax-key-123"
        # 清理缓存(typed() 会缓存结果)
        if hasattr(_config, "_cache"):
            _config._cache.pop("MINIMAX_API_KEY", None)
        try:
            assert _config.minimax_api_key == "test-minimax-key-123"
        finally:
            del os.environ["MINIMAX_API_KEY"]
            if hasattr(_config, "_cache"):
                _config._cache.pop("MINIMAX_API_KEY", None)


# ═══════════════════════════════════════════════════════════════
# _ThinkTagSplitter 测试 — MiniMax M3 等把 thinking 包在 <think>...</think>
# 标签里的 model 需要的状态机
#
# 关键设计:streaming emit — 每 chunk 立即 emit 已确定的内容,
# 只缓冲最后 N 字符(可能是不完整的标签)。保证:
# 1. UI 能实时看到 thinking 流(不是等 </think> 出现才一次性 emit)
# 2. 不丢内容 — 拼接所有 chunk 得到的字符串 == 原始输入
# 3. 标签跨 chunk 切片时仍能正确切分
# ═══════════════════════════════════════════════════════════════

def _collect_splitter(splitter, *texts):
    """Helper: 喂多个 chunk,收集 StreamChunk 列表(包含 flush)"""
    out = []
    for t in texts:
        out.extend(splitter.feed(t))
    out.extend(splitter.flush())
    return out


def _join_chunks(chunks) -> str:
    """拼接所有 delta(thinking + text)成单一字符串,用于内容守恒断言"""
    parts = []
    for c in chunks:
        if c.thinking_delta:
            parts.append(c.thinking_delta.thinking)
        elif c.text_delta:
            parts.append(c.text_delta.text)
    return "".join(parts)


def _assert_no_lost_content(input_texts, chunks):
    """断言:拼接所有 chunk 得到的字符串 == 拼接所有 input(除了 <think>/</think>/紧跟 \n 这些被吃掉的)

    模拟 splitter 行为:
    1. 把 </think>\n 整段去掉(标签 + 紧跟的换行)
    2. 把 <think> 去掉
    3. 任何剩余的 </think> 单独去掉(没有紧跟 \n 的情况)
    """
    raw_input = "".join(input_texts)
    # 1. 吃掉 </think>\n 整段
    expected = raw_input.replace("</think>\n", "")
    # 2. 吃掉 <think>
    expected = expected.replace("<think>", "")
    # 3. 任何剩余的 </think>(没紧跟 \n 的)
    expected = expected.replace("</think>", "")
    actual = _join_chunks(chunks)
    assert actual == expected, (
        f"内容丢失!\n  原始(去标签后): {expected!r}\n  实际 chunk 拼接: {actual!r}"
    )


class TestThinkTagSplitter:
    """_ThinkTagSplitter 状态机:把 <think>...</think> 标签转成 ThinkingDelta"""

    def test_no_think_tag_passes_through_as_text(self):
        """纯文本(无 <think>)→ streaming emit 后拼接 == 原文本"""
        chunks = _collect_splitter(_ThinkTagSplitter(), "hello world")
        # 拼接所有 chunk = 原文本
        assert _join_chunks(chunks) == "hello world"
        # 没有 thinking_delta
        assert all(c.thinking_delta is None for c in chunks)

    def test_basic_think_block(self):
        """简单 <think>foo</think> → thinking=foo"""
        chunks = _collect_splitter(
            _ThinkTagSplitter(),
            "<think>reasoning here</think>",
        )
        _assert_no_lost_content(["<think>reasoning here</think>"], chunks)
        # 拼接后:全部 thinking
        joined = _join_chunks(chunks)
        assert joined == "reasoning here"
        # 至少有一个 thinking_delta
        assert any(c.thinking_delta is not None for c in chunks)

    def test_think_then_text(self):
        """<think>...</think> + 后续文本 → thinking 段 + text 段"""
        chunks = _collect_splitter(
            _ThinkTagSplitter(),
            "<think>\n用户问什么\n</think>\n答案是 42",
        )
        _assert_no_lost_content(["<think>\n用户问什么\n</think>\n答案是 42"], chunks)
        # 拼接后应该是"用户问什么" + "答案是 42"(去掉标签和 \n)
        joined = _join_chunks(chunks)
        assert "用户问什么" in joined
        assert "答案是 42" in joined
        # text 部分不应该有 leading \n
        text_chunks = [c for c in chunks if c.text_delta]
        for c in text_chunks:
            assert not c.text_delta.text.startswith("\n"), \
                f"</think> 后的 \\n 应被吃掉,实际: {c.text_delta.text!r}"

    def test_text_before_think(self):
        """先文本后 think: hello<think>foo</think> → text=hello, thinking=foo"""
        chunks = _collect_splitter(
            _ThinkTagSplitter(),
            "hello<think>foo</think>",
        )
        _assert_no_lost_content(["hello<think>foo</think>"], chunks)
        # 拼接后 == "hellofoo"
        assert _join_chunks(chunks) == "hellofoo"

    def test_open_tag_split_across_chunks(self):
        """<think> 标签跨 chunk:`<thi` + `nk>foo</think>`"""
        chunks = _collect_splitter(
            _ThinkTagSplitter(),
            "<thi", "nk>foo</think>",
        )
        _assert_no_lost_content(["<thi", "nk>foo</think>"], chunks)
        # 拼接后:thinking="foo"
        assert _join_chunks(chunks) == "foo"

    def test_close_tag_split_across_chunks(self):
        """</think> 标签跨 chunk:`<think>foo</thin` + `k>`

        拼接成完整:<think>foo</think>(close tag 完整,后面无内容)
        所以 thinking='foo',无 text
        """
        chunks = _collect_splitter(
            _ThinkTagSplitter(),
            "<think>foo</thin", "k>",
        )
        _assert_no_lost_content(["<think>foo</thin", "k>"], chunks)
        # 拼接后 == "foo"
        assert _join_chunks(chunks) == "foo"
        # 至少一个 thinking 段
        assert any(c.thinking_delta for c in chunks)

    def test_both_tags_split(self):
        """open 和 close 标签都被切碎:`<th` + `ink>foo</` + `think>`"""
        chunks = _collect_splitter(
            _ThinkTagSplitter(),
            "<th", "ink>foo</", "think>",
        )
        _assert_no_lost_content(["<th", "ink>foo</", "think>"], chunks)
        # 拼接后 == "foo"("</think>" 末尾被切开成 <think>foo</think>)
        assert _join_chunks(chunks) == "foo"

    def test_multiple_think_blocks(self):
        """多对标签:<think>a</think>X<think>b</think>Y → thinking=[a,b], text=[X,Y]"""
        chunks = _collect_splitter(
            _ThinkTagSplitter(),
            "<think>think1</think>A<think>think2</think>B",
        )
        _assert_no_lost_content(["<think>think1</think>A<think>think2</think>B"], chunks)
        # 拼接后 == "think1Athink2B"
        assert _join_chunks(chunks) == "think1Athink2B"
        # 至少 2 个 thinking 段 + 2 个 text 段
        thinking_chunks = [c for c in chunks if c.thinking_delta]
        text_chunks = [c for c in chunks if c.text_delta]
        assert len(thinking_chunks) >= 1
        assert len(text_chunks) >= 1
        # thinking 总和 = "think1" + "think2" = "think1think2"
        assert "".join(c.thinking_delta.thinking for c in thinking_chunks) == "think1think2"
        # text 总和 = "A" + "B" = "AB"
        assert "".join(c.text_delta.text for c in text_chunks) == "AB"

    def test_empty_think_block(self):
        """空 think 块 <think></think> → 0 个 chunk(标签被切掉,无残留)"""
        chunks = _collect_splitter(
            _ThinkTagSplitter(),
            "<think></think>",
        )
        _assert_no_lost_content(["<think></think>"], chunks)
        # 拼接后是空字符串
        assert _join_chunks(chunks) == ""

    def test_unclosed_think_partial_buffer_at_end(self):
        """未关闭 <think>(流到末尾)→ buffer 残留由 flush 兜底"""
        s = _ThinkTagSplitter()
        # 喂一段无 </think> 的 thinking
        chunks_from_feed = s.feed("<think>partial reasoning")
        # flush 兜底
        chunks_from_flush = s.flush()
        all_chunks = chunks_from_feed + chunks_from_flush
        _assert_no_lost_content(["<think>partial reasoning"], all_chunks)
        # 拼接后 == "partial reasoning"
        assert _join_chunks(all_chunks) == "partial reasoning"

    def test_partial_buffer_at_end_flushed_as_text(self):
        """NORMAL 状态下末尾是 <think> 部分前缀 → flush 兜底为 text(不丢内容)"""
        s = _ThinkTagSplitter()
        chunks_from_feed = s.feed("hello <thi")  # <thi 是 <think> 前缀
        chunks_from_flush = s.flush()
        all_chunks = chunks_from_feed + chunks_from_flush
        _assert_no_lost_content(["hello <thi"], all_chunks)
        # 拼接后 == "hello <thi"
        assert _join_chunks(all_chunks) == "hello <thi"

    def test_real_minimax_response(self):
        """实测 MiniMax M3 真实响应格式(2026-06-24 smoke test 验证过)

        原 response: '<think>\\n用户要求用一句话介绍自己...\\n</think>\\n我是AI助手,擅长解答问题'
        拆成 3 个 chunk 喂入
        """
        s = _ThinkTagSplitter()
        all_inputs = [
            "<think>\n用户要求一句话介绍自己\n",
            "</think>\n我是AI助手",
            ",擅长解答问题",
        ]
        chunks = []
        for inp in all_inputs:
            chunks += s.feed(inp)
        chunks += s.flush()
        _assert_no_lost_content(all_inputs, chunks)
        # 拼接后:thinking = "用户要求一句话介绍自己"(去掉 \n + 标签)
        # text = "我是AI助手,擅长解答问题"
        full = _join_chunks(chunks)
        assert "用户要求" in full
        assert "我是AI助手" in full
        assert "擅长解答问题" in full
        # 答案(text 段)不应该有 leading \n
        text_chunks = [c for c in chunks if c.text_delta]
        if text_chunks:
            assert not text_chunks[0].text_delta.text.startswith("\n"), \
                f"</think> 后的 \\n 应被吃掉,实际: {text_chunks[0].text_delta.text!r}"


class TestStopReasonPlumbing:
    """Bug 1e:provider 必须把 finish_reason / stop_reason 透传成 StreamChunk.stop_reason"""

    def _make_provider(self):
        from unittest.mock import MagicMock
        from agent_core.llm.providers.openai import OpenAIProvider
        cfg = LLMConfig(provider="openai", model="gpt-4o", api_key="x")
        prov = OpenAIProvider(cfg)
        prov._client = MagicMock()  # 注入 mock,绕过 lazy import openai
        return prov

    @staticmethod
    def _chunk(content=None, finish=None, usage=None):
        from unittest.mock import MagicMock
        ch = MagicMock()
        delta = MagicMock(); delta.content = content; delta.tool_calls = None
        choice = MagicMock(); choice.delta = delta; choice.finish_reason = finish
        ch.choices = [choice]
        ch.usage = usage
        return ch

    def test_openai_compat_emits_stop_reason_from_finish_reason(self):
        prov = self._make_provider()
        prov._client.chat.completions.create.return_value = iter([
            self._chunk(content="半句被切"),
            self._chunk(finish="length"),   # max_tokens 截断
        ])
        chunks = list(prov.chat([{"role": "user", "content": "hi"}], tools=None))
        stops = [c.stop_reason for c in chunks if c.stop_reason]
        assert stops == ["length"], f"应透传 finish_reason=length,实际 {stops}"

    def test_openai_compat_emits_stop_for_normal_finish(self):
        prov = self._make_provider()
        prov._client.chat.completions.create.return_value = iter([
            self._chunk(content="完整回答"),
            self._chunk(finish="stop"),
        ])
        chunks = list(prov.chat([{"role": "user", "content": "hi"}], tools=None))
        stops = [c.stop_reason for c in chunks if c.stop_reason]
        assert stops == ["stop"]


# ═══════════════════════════════════════════════════════════════
# Stage 0 基线测试:MiniMax splitter 不泄漏 <think> 文本
# (LLM Router 重构 Stage 0 — 必须在 Stage 3 之前通过)
# ═══════════════════════════════════════════════════════════════

class TestMinimaxSplitterNoLeakBaseline:
    """基线:<think>hello</think> world → text 流只含 ' world',不能含 '<think>'。

    重构后 `MiniMaxProvider._extract_thinking` 走钩子 + `_consumed_text` 哨兵,
    本测试确保重构没破坏这个 invariant。
    """

    def _make_minimax(self):
        from agent_core.llm.providers.openai import MiniMaxProvider
        from unittest.mock import MagicMock
        cfg = LLMConfig(
            provider="minimax", model="MiniMax-Text-01", api_key="test",
        )
        p = MiniMaxProvider(cfg)
        p._client = MagicMock()
        return p

    def _chunk(self, content):
        from unittest.mock import MagicMock
        ch = MagicMock()
        # 用 spec 限定 delta 的属性,避免 MagicMock 默认 truthy 导致 reasoning_content
        # 误命中(MagicMock 的 hasattr 永远 True,任何属性都是 MagicMock 实例 → truthy)
        delta = MagicMock(spec=["content", "tool_calls", "reasoning_content"])
        delta.content = content
        delta.tool_calls = None
        delta.reasoning_content = None
        ch.choices = [MagicMock(delta=delta, finish_reason="stop")]
        ch.usage = None
        return ch

    def test_splitter_does_not_leak_think_text(self):
        """<think>hello</think> world → text 只剩 ' world',thinking 拿到 'hello'"""
        p = self._make_minimax()
        p._client.chat.completions.create.return_value = iter([
            self._chunk("<think>hello</think> world"),
        ])
        chunks = list(p.chat([{"role": "user", "content": "hi"}]))
        text = "".join(c.text_delta.text for c in chunks if c.text_delta)
        thinking = "".join(c.thinking_delta.thinking for c in chunks if c.thinking_delta)
        assert "<think>" not in text, f"text 流泄漏 <think> 标签: {text!r}"
        assert "world" in text, f"text 流应含 'world',实际 {text!r}"
        assert "hello" in thinking, f"thinking 流应含 'hello',实际 {thinking!r}"

    def test_splitter_preserves_text_outside_think_tags(self):
        """<think>foo</think>bar → text 仅为 'bar'(无空字符/无 tag)"""
        p = self._make_minimax()
        p._client.chat.completions.create.return_value = iter([
            self._chunk("<think>foo</think>bar"),
        ])
        chunks = list(p.chat([{"role": "user", "content": "hi"}]))
        text = "".join(c.text_delta.text for c in chunks if c.text_delta)
        assert text == "bar", f"text 应为 'bar',实际 {text!r}"


class TestLLMRouterInvoke:
    """router.invoke() — 同步聚合调用,内置重试+超时+空响应检测。
    参考:docs/llm-invoke-retry-design.md
    """

    @staticmethod
    def _make_chunks(*texts: str):
        """构造一个 generator 返回带 text_delta 的 StreamChunk"""
        for t in texts:
            yield StreamChunk(text_delta=TextDelta(text=t))

    @staticmethod
    def _make_router_with_chat(chunks_or_side_effect):
        """构造 LLMRouter,mock provider.chat() 返回指定 chunks。

        chunks_or_side_effect:
          - list/tuple: 每次 chat() 都返回这些 chunks 的新 generator
          - callable:   chat() 调用它(可能 raise 或返回 generator)
        """
        router = LLMRouter(LLMConfig(
            provider=LLMProvider.MINIMAX,
            model="test-model",
            api_key="sk-test",
            base_url="http://x",
        ))
        if callable(chunks_or_side_effect):
            chat_side_effect = chunks_or_side_effect
        else:
            def chat_side_effect(messages, **kwargs):
                return iter([StreamChunk(text_delta=TextDelta(text=t)) for t in chunks_or_side_effect])
        # 替换 provider.chat (不走网络)
        router._provider = MagicMock()
        router._provider.chat = MagicMock(side_effect=chat_side_effect)
        return router

    def test_invoke_basic_returns_aggregated_text(self):
        """正常场景:chunks 聚合为单个字符串返回"""
        router = self._make_router_with_chat(["hello", " ", "world"])
        text = router.invoke(
            messages=[{"role": "user", "content": "hi"}],
            cache_namespace="test",
        )
        assert text == "hello world"
        # kwargs 透传
        router._provider.chat.assert_called_once()
        call_kwargs = router._provider.chat.call_args.kwargs
        assert call_kwargs["cache_namespace"] == "test"

    def test_invoke_empty_response_triggers_retry_then_raises(self):
        """空响应触发重试,所有重试都空时抛 EmptyResponseError"""
        call_count = 0
        def always_empty(messages, **kwargs):
            nonlocal call_count
            call_count += 1
            return iter([])  # 永远空

        router = self._make_router_with_chat(always_empty)
        with pytest.raises(EmptyResponseError, match="空响应"):
            router.invoke(
                messages=[{"role": "user", "content": "hi"}],
                max_retries=1,  # 总尝试 2 次
                timeout=None,   # 关闭超时避免测试慢
            )
        assert call_count == 2, f"应调 2 次(1 retry),实际 {call_count}"

    def test_invoke_on_failure_callback_used_when_all_retries_fail(self):
        """所有重试失败后调 on_failure,返回降级文本"""
        def always_fails(messages, **kwargs):
            raise RuntimeError("network down")

        router = self._make_router_with_chat(always_fails)
        result = router.invoke(
            messages=[{"role": "user", "content": "hi"}],
            max_retries=1,
            timeout=None,
            on_failure=lambda e: f"degraded:{type(e).__name__}",
        )
        assert result == "degraded:RuntimeError"

    def test_invoke_on_failure_can_raise_through(self):
        """on_failure 回调内部 raise,异常穿透 invoke()"""
        def always_fails(messages, **kwargs):
            raise RuntimeError("api down")

        def raise_specific(e):
            raise ValueError(f"wrapped:{e}") from e

        router = self._make_router_with_chat(always_fails)
        with pytest.raises(ValueError, match="wrapped"):
            router.invoke(
                messages=[{"role": "user", "content": "hi"}],
                max_retries=0,
                timeout=None,
                on_failure=raise_specific,
            )
