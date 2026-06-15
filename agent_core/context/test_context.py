"""
上下文管理系统测试
覆盖：tokenizer, budget, compact, manager
"""

import pytest
import sys
import os

# 确保能 import agent_core
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

from agent_core.context.tokenizer import SimpleTokenCounter
from agent_core.context.budget import (
    ContextBudgetManager,
    BudgetState,
    AUTOCOMPACT_BUFFER_TOKENS,
    MAX_CONSECUTIVE_AUTOCOMPACT_FAILURES,
    get_effective_context_window,
    get_model_config,
)
from agent_core.context.compact import (
    CompactOrchestrator,
    CompactionResult,
    COMPACT_SYSTEM_PROMPT,
    COMPACT_USER_PROMPT_TEMPLATE,
    PRESERVED_HEAD_MESSAGES,
    MAX_PTL_RETRIES,
)
from agent_core.context.manager import ContextManager


# ═══════════════════════════════════════════════════════════════
# Token Counter 测试
# ═══════════════════════════════════════════════════════════════

class TestSimpleTokenCounter:
    
    def setup_method(self):
        self.counter = SimpleTokenCounter()

    def test_empty_text(self):
        assert self.counter.count("") == 0
        assert self.counter.count(None) == 0  # type: ignore

    def test_chinese_text(self):
        # "你好世界" = 4 个中文字
        # count() 不含 overhead，只有纯文本 token
        # 预期 ~4 * 1.4 = 5.6 → int = 5
        result = self.counter.count("你好世界")
        assert 3 < result < 10

    def test_english_text(self):
        # "hello world" = 11 chars (all English)
        # count() 不含 overhead
        # 预期 ~11 * 0.25 = 2.75 → int = 2
        result = self.counter.count("hello world")
        assert 1 < result < 10

    def test_mixed_text(self):
        # 中英混合
        result = self.counter.count("Hello 你好 world 世界")
        assert result > 5

    def test_string_messages(self):
        messages = [
            {"role": "system", "content": "You are helpful"},
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi there"},
        ]
        result = self.counter.count_messages(messages)
        assert result > 30  # 3 条消息 * overhead + 内容

    def test_list_content_messages(self):
        messages = [
            {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "Let me calculate"},
                    {"type": "tool_use", "name": "calculator", "input": {"expr": "1+1"}},
                ]
            },
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "content": "2"},
                ]
            }
        ]
        result = self.counter.count_messages(messages)
        assert result > 50  # 包含 tool_use 和 tool_result 开销

    def test_empty_messages(self):
        assert self.counter.count_messages([]) == 0

    def test_none_content(self):
        messages = [{"role": "assistant", "content": None}]
        result = self.counter.count_messages(messages)
        # 只有 role overhead
        assert result == 10


# ═══════════════════════════════════════════════════════════════
# Budget Manager 测试
# ═══════════════════════════════════════════════════════════════

class TestBudgetState:
    
    def test_properties(self):
        state = BudgetState(
            total_budget=100_000,
            used_tokens=80_000,
            reserved_tokens=4_096,
        )
        assert state.available == 20_000
        assert state.usage_ratio == 0.8
        assert not state.should_auto_compact  # 20K > 8K buffer
        assert not state.is_critical

    def test_should_compact_threshold(self):
        state = BudgetState(
            total_budget=100_000,
            used_tokens=93_000,  # available = 7K < 8K
            reserved_tokens=4_096,
        )
        assert state.should_auto_compact
        assert not state.is_critical  # 7K > 4K

    def test_is_critical(self):
        state = BudgetState(
            total_budget=100_000,
            used_tokens=97_000,  # available = 3K < 4K
            reserved_tokens=4_096,
        )
        assert state.should_auto_compact
        assert state.is_critical

    def test_zero_budget(self):
        state = BudgetState(total_budget=0, used_tokens=0, reserved_tokens=0)
        assert state.usage_ratio == 0.0


class TestContextBudgetManager:

    def setup_method(self):
        self.counter = SimpleTokenCounter()
        self.bm = ContextBudgetManager("glm-4", self.counter)

    def test_effective_window_positive(self):
        window = get_effective_context_window("glm-4")
        assert window > 50_000

    def test_model_config_glm4(self):
        config = get_model_config("glm-4")
        assert config["context_window"] == 128_000

    def test_model_config_unknown_fallback(self):
        config = get_model_config("unknown-model")
        assert config["context_window"] == 128_000  # 默认保守值

    def test_model_config_glm5(self):
        config = get_model_config("glm-5.1")
        assert config["context_window"] == 128_000
        assert config["max_output"] == 8_192

    def test_should_not_compact_when_budget_ok(self):
        # 少量消息，远未到阈值
        messages = [
            {"role": "system", "content": "You are helpful"},
            {"role": "user", "content": "Hello"},
        ]
        should, reason = self.bm.should_compact(messages)
        assert should is False
        assert "充足" in reason or "available" in reason.lower()

    def test_should_compact_when_near_limit(self):
        # 构造接近上限的消息（模拟大量消息）
        # GLM-4 有效窗口 = 128000 - 4096 - 8000 = 115904
        # 需要构造 >107000 tokens 的消息
        big_text = "你好世界" * 5000  # ~35000 tokens 每条
        messages = []
        for i in range(4):
            messages.append({"role": "user", "content": f"消息{i}: {big_text}"})
            messages.append({"role": "assistant", "content": f"回复{i}: {big_text}"})

        should, reason = self.bm.should_compact(messages)
        # 8 条消息 * ~35000 = ~280000 tokens，远超 115904
        assert should is True

    def test_circuit_breaker(self):
        # 模拟连续失败
        for _ in range(MAX_CONSECUTIVE_AUTOCOMPACT_FAILURES):
            self.bm.record_compact_failure()

        messages = [{"role": "user", "content": "test" * 100000}]
        should, reason = self.bm.should_compact(messages)
        assert should is False
        assert "熔断" in reason

    def test_circuit_breaker_reset_on_success(self):
        self.bm.record_compact_failure()
        self.bm.record_compact_failure()
        assert self.bm.consecutive_failures == 2

        self.bm.record_compact_success()
        assert self.bm.consecutive_failures == 0

    def test_manual_reset_circuit_breaker(self):
        self.bm.record_compact_failure()
        self.bm.record_compact_failure()
        self.bm.record_compact_failure()

        self.bm.reset_circuit_breaker()
        assert self.bm.consecutive_failures == 0

    def test_get_usage_info(self):
        messages = [
            {"role": "user", "content": "Hello world"},
        ]
        info = self.bm.get_usage_info(messages)
        assert "total_budget" in info
        assert "used_tokens" in info
        assert "available_tokens" in info
        assert "usage_ratio" in info
        assert "should_compact" in info
        assert info["model"] == "glm-4"


# ═══════════════════════════════════════════════════════════════
# CompactOrchestrator 测试（不需要真实 LLM 调用）
# ═══════════════════════════════════════════════════════════════

class TestCompactOrchestrator:

    def setup_method(self):
        self.counter = SimpleTokenCounter()
        self.bm = ContextBudgetManager("glm-4", self.counter)

        # Mock LLM Router — 不实际调用 LLM
        class MockLLMRouter:
            def chat(self, messages, tools=None):
                """返回模拟的摘要响应"""
                from agent_core.llm.router import StreamChunk, TextDelta
                yield StreamChunk(text_delta=TextDelta(
                    text="<analysis>用户做了计算</analysis>\n<summary>用户请求计算并被成功完成</summary>",
                ))

        self.mock_llm = MockLLMRouter()
        self.compactor = CompactOrchestrator(
            llm_router=self.mock_llm,
            budget_manager=self.bm,
            token_counter=self.counter,
        )

    def test_preprocess_string_content(self):
        """字符串内容应原样保留"""
        messages = [
            {"role": "system", "content": "You are helpful"},
            {"role": "user", "content": "Hello"},
        ]
        result = self.compactor._preprocess(messages)
        assert len(result) == 2
        assert result[0]["content"] == "You are helpful"

    def test_preprocess_truncates_tool_result(self):
        """超长工具结果应被截断"""
        long_text = "x" * 10000
        messages = [{
            "role": "user",
            "content": [{
                "type": "tool_result",
                "content": long_text,
            }]
        }]
        result = self.compactor._preprocess(messages)
        # 找到截断后的文本
        block = result[0]["content"][0]
        assert "[truncated]" in block["text"]
        assert len(block["text"]) < len(long_text)

    def test_preprocess_removes_images(self):
        """图片应替换为占位符"""
        messages = [{
            "role": "user",
            "content": [
                {"type": "text", "text": "看这张图"},
                {"type": "image", "source": {"data": "..."}},
            ]
        }]
        result = self.compactor._preprocess(messages)
        blocks = result[0]["content"]
        assert len(blocks) == 2
        assert blocks[1]["type"] == "text"
        assert "image" in blocks[1]["text"]

    def test_preprocess_skips_thinking(self):
        """thinking blocks 应被移除"""
        messages = [{
            "role": "assistant",
            "content": [
                {"type": "thinking", "thinking": "let me think..."},
                {"type": "text", "text": "The answer is 42"},
            ]
        }]
        result = self.compactor._preprocess(messages)
        blocks = result[0]["content"]
        assert len(blocks) == 1
        assert blocks[0]["type"] == "text"

    def test_messages_to_text(self):
        """测试消息转文本"""
        messages = [
            {"role": "system", "content": "You are helpful"},
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi there"},
        ]
        text = self.compactor._messages_to_text(messages)
        assert "[SYSTEM]" in text
        assert "[USER]" in text
        assert "[ASSISTANT]" in text
        assert "Hello" in text

    def test_extract_summary_tag(self):
        text = "prefix <summary>actual summary</summary> suffix"
        result = self.compactor._extract_summary(text)
        assert "actual summary" == result

    def test_extract_analysis_tag(self):
        text = "<analysis>free form analysis</analysis>"
        result = self.compactor._extract_summary(text)
        assert "analysis" in result

    def test_extract_plain_text(self):
        text = "just plain text summary"
        result = self.compactor._extract_summary(text)
        assert result == text

    def test_extract_empty(self):
        assert self.compactor._extract_summary("") == ""

    def test_build_compacted_messages_structure(self):
        """压缩后消息应包含：system + summary + 最近N条"""
        messages = [
            {"role": "system", "content": "You are helpful"},
            {"role": "user", "content": "msg1"},
            {"role": "assistant", "content": "reply1"},
            {"role": "user", "content": "msg2"},
            {"role": "assistant", "content": "reply2"},
            {"role": "user", "content": "msg3"},
            {"role": "assistant", "content": "reply3"},
            {"role": "user", "content": "recent1"},
            {"role": "assistant", "content": "recent_reply"},
        ]
        result = self.compactor._build_compacted_messages(
            summary="这是摘要",
            original=messages,
            preserved_head=4,
        )
        # 1 system + 1 summary + 4 recent = 6
        assert len(result) == 6
        # 第一条是 system
        assert result[0]["role"] == "system"
        # 第二条是摘要（role=user）
        assert result[1]["role"] == "user"
        assert "这是摘要" in result[1]["content"]
        # 最近 4 条保留
        assert result[2]["content"] == "msg3"

    def test_build_compacted_preserves_system(self):
        """没有 system 消息时不应崩溃"""
        messages = [
            {"role": "user", "content": "msg1"},
            {"role": "assistant", "content": "reply1"},
        ]
        result = self.compactor._build_compacted_messages(
            summary="摘要",
            original=messages,
        )
        # 无 system + summary + 2 recent = 3
        assert len(result) == 3

    def test_compact_success_with_mock_llm(self):
        """用 Mock LLM 测试完整压缩流程"""
        messages = [
            {"role": "system", "content": "You are helpful"},
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi there"},
        ]
        result = self.compactor.compact(messages)

        assert result.success is True
        assert result.tokens_before > 0
        assert len(result.compacted_messages) > 0
        assert result.ptl_retries == 0

    def test_compact_failure_with_broken_llm(self):
        """LLM 调用失败时应返回失败结果"""
        class BrokenLLMRouter:
            def chat(self, messages, tools=None):
                raise RuntimeError("API down")

        compactor = CompactOrchestrator(
            llm_router=BrokenLLMRouter(),
            budget_manager=self.bm,
            token_counter=self.counter,
        )
        messages = [{"role": "user", "content": "test"}]
        result = compactor.compact(messages)

        assert result.success is False
        assert "API down" in result.error

    def test_ptl_defense_truncates_and_retries(self):
        """PTL 防御：第一次 PTL 错误，截断后第二次成功"""
        call_count = [0]

        class PTLLLMRouter:
            def chat(self, messages, tools=None):
                call_count[0] += 1
                if call_count[0] == 1:
                    raise RuntimeError("Prompt too long")
                # 第二次成功
                from agent_core.llm.router import StreamChunk, TextDelta
                yield StreamChunk(text_delta=TextDelta(text="<summary>摘要</summary>"))

        compactor = CompactOrchestrator(
            llm_router=PTLLLMRouter(),
            budget_manager=self.bm,
            token_counter=self.counter,
        )

        # 构造足够多的消息让截断有意义
        messages = [{"role": "system", "content": "system"}]
        for i in range(20):
            messages.append({"role": "user", "content": f"消息{i} " * 100})
            messages.append({"role": "assistant", "content": f"回复{i} " * 100})

        result = compactor.compact(messages)
        assert result.success is True
        assert result.ptl_retries == 1
        assert call_count[0] == 2

    def test_ptl_defense_exhausted(self):
        """PTL 防御：重试用完后报错"""

        class AlwaysPTLLLMRouter:
            def chat(self, messages, tools=None):
                raise RuntimeError("context length exceeded")

        compactor = CompactOrchestrator(
            llm_router=AlwaysPTLLLMRouter(),
            budget_manager=self.bm,
            token_counter=self.counter,
        )

        messages = [{"role": "system", "content": "system"}]
        for i in range(20):
            messages.append({"role": "user", "content": f"msg{i}"})
            messages.append({"role": "assistant", "content": f"reply{i}"})

        result = compactor.compact(messages)
        assert result.success is False
        # PTL 重试耗尽后抛 ValueError 被 compact() 捕获
        assert result.error is not None


# ═══════════════════════════════════════════════════════════════
# ContextManager 测试
# ═══════════════════════════════════════════════════════════════

class TestContextManager:

    def setup_method(self):
        class MockLLMRouter:
            def chat(self, messages, tools=None):
                from agent_core.llm.router import StreamChunk, TextDelta
                yield StreamChunk(text_delta=TextDelta(
                    text="<summary>测试摘要</summary>"
                ))

        self.mock_llm = MockLLMRouter()
        self.cm = ContextManager(
            llm_router=self.mock_llm,
            model="glm-4",
        )

    def test_init(self):
        assert self.cm.model == "glm-4"
        assert self.cm.compact_count == 0
        assert self.cm.total_tokens_freed == 0

    def test_should_compact(self):
        messages = [{"role": "user", "content": "Hello"}]
        should, reason = self.cm.should_compact(messages)
        assert should is False

    def test_check_and_compact_not_needed(self):
        messages = [{"role": "user", "content": "Hello"}]
        result_messages, result = self.cm.check_and_compact(messages)
        assert result is None
        assert result_messages == messages

    def test_check_and_compact_triggered(self):
        """构造接近上限的消息触发压缩"""
        big_text = "你好世界测试" * 5000  # ~35000 tokens 每条
        messages = [
            {"role": "system", "content": "You are helpful"},
        ]
        # 构造足够多的消息（超过 PRESERVED_HEAD_MESSAGES=6）
        for i in range(10):
            messages.append({"role": "user", "content": f"用户消息{i}: {big_text}"})
            messages.append({"role": "assistant", "content": f"回复{i}: {big_text}"})

        result_messages, result = self.cm.check_and_compact(messages)

        assert result is not None
        assert result.success is True
        # 压缩后消息数应显著减少（system + summary + 6 recent = 8）
        assert len(result_messages) < len(messages)
        assert len(result_messages) <= 8
        assert self.cm.compact_count == 1
        assert self.cm.total_tokens_freed > 0

    def test_get_stats(self):
        stats = self.cm.get_stats()
        assert stats["model"] == "glm-4"
        assert stats["total_budget"] > 50_000
        assert stats["compact_count"] == 0

    def test_get_usage_info(self):
        messages = [{"role": "user", "content": "Hello world"}]
        info = self.cm.get_usage_info(messages)
        assert "total_budget" in info
        assert "used_tokens" in info
        assert "compact_count" in info

    def test_force_compact(self):
        messages = [
            {"role": "system", "content": "You are helpful"},
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi"},
        ]
        result = self.cm.force_compact(messages)
        assert result.success is True
        assert self.cm.compact_count == 1


class TestContextWindowOverride:
    """测试 CONTEXT_WINDOW_OVERRIDE 环境变量覆盖（调试用）"""

    def teardown_method(self):
        """清理环境变量"""
        import os
        os.environ.pop("CONTEXT_WINDOW_OVERRIDE", None)

    def test_no_override_uses_default(self):
        from agent_core.context.budget import get_effective_context_window
        os.environ.pop("CONTEXT_WINDOW_OVERRIDE", None)
        eff = get_effective_context_window("glm-4")
        # 不覆盖：应遵循 50K 下限
        assert eff >= 50_000

    def test_override_small_window(self):
        from agent_core.context.budget import get_effective_context_window
        os.environ["CONTEXT_WINDOW_OVERRIDE"] = "8000"
        eff = get_effective_context_window("glm-4")
        # 8000 - 4096 - 8000 = -4096 → 提升到 1K 最低
        assert eff <= 5_000  # 接近用户值（不被 50K 下限拦住）
        assert eff >= 1_000

    def test_override_medium_window(self):
        from agent_core.context.budget import get_effective_context_window
        os.environ["CONTEXT_WINDOW_OVERRIDE"] = "20000"
        eff = get_effective_context_window("glm-4")
        # 20000 - 4096 - 8000 = 7904
        assert 7_000 <= eff <= 9_000

    def test_invalid_override_ignored(self):
        from agent_core.context.budget import get_effective_context_window
        os.environ["CONTEXT_WINDOW_OVERRIDE"] = "not-a-number"
        eff = get_effective_context_window("glm-4")
        # 无效值应被忽略，使用默认
        assert eff >= 50_000
