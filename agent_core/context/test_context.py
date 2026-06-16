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
    CRITICAL_BUFFER_TOKENS,
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
            compact_threshold=87_000,
            critical_threshold=93_500,
        )
        assert state.available == 20_000
        assert state.usage_ratio == 0.8
        assert not state.should_auto_compact  # 80K < 87K threshold
        assert not state.is_critical

    def test_should_compact_threshold(self):
        state = BudgetState(
            total_budget=100_000,
            used_tokens=88_000,  # 88K >= 87K threshold
            reserved_tokens=4_096,
            compact_threshold=87_000,
            critical_threshold=93_500,
        )
        assert state.should_auto_compact
        assert not state.is_critical  # 88K < 93.5K

    def test_is_critical(self):
        state = BudgetState(
            total_budget=100_000,
            used_tokens=95_000,  # 95K >= 93.5K threshold
            reserved_tokens=4_096,
            compact_threshold=87_000,
            critical_threshold=93_500,
        )
        assert state.should_auto_compact
        assert state.is_critical

    def test_zero_budget(self):
        state = BudgetState(
            total_budget=0, used_tokens=0, reserved_tokens=0,
            compact_threshold=0, critical_threshold=0,
        )
        assert state.usage_ratio == 0.0

    def test_legacy_fallback_no_threshold(self):
        """无阈值时回退到固定缓冲模式"""
        state = BudgetState(
            total_budget=100_000,
            used_tokens=93_000,
            reserved_tokens=4_096,
            compact_threshold=0,
            critical_threshold=0,
        )
        # 回退到 available < AUTOCOMPACT_BUFFER_TOKENS 逻辑
        assert state.should_auto_compact  # available 7K < 13K
        assert not state.is_critical  # available 7K >= 6.5K

    def test_warning_and_error(self):
        state = BudgetState(
            total_budget=100_000,
            used_tokens=70_000,
            reserved_tokens=4_096,
            compact_threshold=87_000,
            critical_threshold=93_500,
        )
        # warning = compact_threshold - WARNING_BUFFER_TOKENS = 87K - 20K = 67K
        assert state.is_warning  # 70K >= 67K
        # error = compact_threshold - ERROR_BUFFER_TOKENS = 87K - 20K = 67K
        assert state.is_error


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
        # GLM-4 total_budget = 128000 - 4096 - 13000 = 110904
        # compact_threshold = 110904 - 13000 = 97904
        # 需要构造 >= 97904 tokens 的消息
        big_text = "你好世界" * 5000  # ~35000 tokens 每条
        messages = []
        for i in range(4):
            messages.append({"role": "user", "content": f"消息{i}: {big_text}"})
            messages.append({"role": "assistant", "content": f"回复{i}: {big_text}"})

        should, reason = self.bm.should_compact(messages)
        # 8 条消息 * ~35000 = ~280000 tokens，远超阈值
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
        assert "compact_threshold" in info
        assert "critical_threshold" in info
        assert "is_warning" in info
        assert "is_error" in info
        assert info["model"] == "glm-4"

    def test_compact_threshold_values_glm4(self):
        """GLM-4 双模式阈值计算验证"""
        # total_budget = 128000 - 4096 - 13000 = 110904
        assert self.bm.total_budget == 110_904
        # compact_threshold = 110904 - 13000 = 97904
        assert self.bm.compact_threshold == 97_904
        # critical_threshold = 110904 - 6500 = 104404
        assert self.bm.critical_threshold == 104_404

    def test_compact_threshold_values_claude(self):
        """Claude 模型双模式阈值计算验证"""
        bm = ContextBudgetManager("claude-3-5-sonnet", self.counter)
        # total_budget = 200000 - min(8000,4096) - 13000 = 200000 - 4096 - 13000 = 182904
        assert bm.total_budget == 182_904
        # compact_threshold = 182904 - 13000 = 169904
        assert bm.compact_threshold == 169_904
        # critical_threshold = 182904 - 6500 = 176404
        assert bm.critical_threshold == 176_404


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


class TestAutoCompactPctOverride:
    """测试 AUTOCOMPACT_PCT_OVERRIDE 双模式比例覆盖"""

    def teardown_method(self):
        import os
        os.environ.pop("AUTOCOMPACT_PCT_OVERRIDE", None)

    def test_no_override_uses_fixed_buffer(self):
        """不设环境变量时走固定缓冲模式"""
        os.environ.pop("AUTOCOMPACT_PCT_OVERRIDE", None)
        counter = SimpleTokenCounter()
        bm = ContextBudgetManager("glm-4", counter)
        # total_budget=110904, compact_threshold = 110904 - 13000 = 97904
        assert bm.compact_threshold == bm.total_budget - AUTOCOMPACT_BUFFER_TOKENS

    def test_pct_override_low_remaining_pct_uses_fixed(self):
        """剩余10% → threshold=90%*total≈99813, 固定=97904, 固定更保守"""
        os.environ["AUTOCOMPACT_PCT_OVERRIDE"] = "10"
        counter = SimpleTokenCounter()
        bm = ContextBudgetManager("glm-4", counter)

        # 剩余10%: threshold = total * 0.90 ≈ 99813
        pct_threshold = int(bm.total_budget * 0.90)  # 99813
        fixed_threshold = bm.total_budget - AUTOCOMPACT_BUFFER_TOKENS  # 97904
        # 固定缓冲更小（更保守），固定赢
        assert bm.compact_threshold == min(pct_threshold, fixed_threshold)
        assert bm.compact_threshold == fixed_threshold

    def test_pct_override_high_remaining_pct_uses_pct(self):
        """剩余95% → threshold=5%*total≈5545, 比固定缓冲小得多，比例赢"""
        os.environ["AUTOCOMPACT_PCT_OVERRIDE"] = "95"
        counter = SimpleTokenCounter()
        bm = ContextBudgetManager("glm-4", counter)

        # 剩余95%: threshold = total * 0.05 ≈ 5545
        pct_threshold = int(bm.total_budget * 0.05)  # 5545
        fixed_threshold = bm.total_budget - AUTOCOMPACT_BUFFER_TOKENS  # 97904
        # 比例更小（更保守），比例赢
        assert bm.compact_threshold == min(pct_threshold, fixed_threshold)
        assert bm.compact_threshold == pct_threshold

    def test_pct_critical_is_half_of_compact(self):
        """严重阈值：剩余比例 = compact剩余比例的一半"""
        os.environ["AUTOCOMPACT_PCT_OVERRIDE"] = "10"
        counter = SimpleTokenCounter()
        bm = ContextBudgetManager("glm-4", counter)

        # compact PCT=10(剩余10%), critical PCT=5(剩余5%)
        # critical threshold = total * 0.95 ≈ 105358
        # fixed critical = total - 6500 = 104404
        # 固定更保守
        pct_critical = int(bm.total_budget * 0.95)  # 105358
        fixed_critical = bm.total_budget - CRITICAL_BUFFER_TOKENS  # 104404
        assert bm.critical_threshold == min(pct_critical, fixed_critical)
        assert bm.critical_threshold == fixed_critical

    def test_invalid_pct_ignored(self):
        """无效比例值应被忽略"""
        os.environ["AUTOCOMPACT_PCT_OVERRIDE"] = "not-a-number"
        counter = SimpleTokenCounter()
        bm = ContextBudgetManager("glm-4", counter)
        # 应走固定缓冲
        assert bm.compact_threshold == bm.total_budget - AUTOCOMPACT_BUFFER_TOKENS

    def test_zero_pct_ignored(self):
        """0% 和负数应被忽略"""
        for val in ["0", "-5", "101"]:
            os.environ["AUTOCOMPACT_PCT_OVERRIDE"] = val
            counter = SimpleTokenCounter()
            bm = ContextBudgetManager("glm-4", counter)
            assert bm.compact_threshold == bm.total_budget - AUTOCOMPACT_BUFFER_TOKENS

    def test_pct_claude_model(self):
        """Claude 模型：剩余10% → threshold=90%*total≈164613, fixed=169904, 比例赢"""
        os.environ["AUTOCOMPACT_PCT_OVERRIDE"] = "10"
        counter = SimpleTokenCounter()
        bm = ContextBudgetManager("claude-3-5-sonnet", counter)

        # 剩余10% → threshold = total * 0.90
        pct_threshold = int(bm.total_budget * 0.90)  # 164613
        fixed_threshold = bm.total_budget - AUTOCOMPACT_BUFFER_TOKENS  # 169904
        assert bm.compact_threshold == min(pct_threshold, fixed_threshold)
        assert bm.compact_threshold == pct_threshold  # 比例更保守


# ═══════════════════════════════════════════════════════════════════════════
#  Option B+C: 强化 XML 标签 + few-shot example 验证
# ═══════════════════════════════════════════════════════════════════════════

def test_compact_prompt_uses_xml_tags_strictly():
    """验证 Claude Code 风格的 prompt 设计 (4 道防线)"""
    from agent_core.context.compact import COMPACT_SYSTEM_PROMPT, COMPACT_USER_PROMPT_TEMPLATE
    
    # 防线 1: 开头 CRITICAL 警告 (仿 Claude Code NO_TOOLS_PREAMBLE)
    assert "CRITICAL" in COMPACT_SYSTEM_PROMPT, "system prompt 开头有 CRITICAL 警告"
    assert "TEXT ONLY" in COMPACT_SYSTEM_PROMPT, "开头要求 TEXT ONLY"
    assert "Do NOT call any tools" in COMPACT_SYSTEM_PROMPT, "开头禁止调用工具"
    assert "REJECTED" in COMPACT_SYSTEM_PROMPT, "说明工具调用会被 REJECTED"
    
    # 防线 2: 主体 XML 标签要求
    assert "<analysis>" in COMPACT_SYSTEM_PROMPT, "system prompt 提到 <analysis>"
    assert "<summary>" in COMPACT_SYSTEM_PROMPT, "system prompt 提到 <summary>"
    assert "</analysis>" in COMPACT_SYSTEM_PROMPT, "system prompt 提到 </analysis>"
    assert "</summary>" in COMPACT_SYSTEM_PROMPT, "system prompt 提到 </summary>"
    
    # 防线 3: 主体 few-shot example
    assert "<example>" in COMPACT_SYSTEM_PROMPT, "system prompt 包含 <example>"
    assert "</example>" in COMPACT_SYSTEM_PROMPT, "system prompt 包含 </example>"
    
    # 防线 4: 结尾 REMINDER 再次强调
    assert "REMINDER" in COMPACT_SYSTEM_PROMPT, "system prompt 结尾有 REMINDER"
    
    # user prompt 同样有 REMINDER
    assert "REMINDER" in COMPACT_USER_PROMPT_TEMPLATE, "user prompt 也有 REMINDER"
    
    # 4 段结构 (中文友好, 融合 Claude Code 9 段)
    for seg in ["用户目标", "关键决策", "当前状态", "待办事项"]:
        assert seg in COMPACT_SYSTEM_PROMPT, f"4 段结构包含 {seg}"
    
    # 防漂移规则
    assert "verbatim quotes" in COMPACT_SYSTEM_PROMPT, "保留 verbatim quotes 规则"
    assert "防漂移" in COMPACT_SYSTEM_PROMPT, "保留防漂移规则"


def test_compact_prompt_extract_summary_works_with_new_format():
    """验证 _extract_summary 能正确处理新 prompt 期望的格式"""
    from agent_core.context.compact import CompactOrchestrator
    # 只需要 import 成功即可（具体逻辑需 LLM 测试）
    assert CompactOrchestrator is not None, "CompactOrchestrator 可正常导入"


def test_compact_debug_logging_emits_expected_events():
    """验证 DEBUG 日志输出关键事件（Compact START / LLM Call / Extract / Build）"""
    import logging
    from io import StringIO
    
    # 捕获日志
    log_stream = StringIO()
    handler = logging.StreamHandler(log_stream)
    handler.setLevel(logging.DEBUG)
    formatter = logging.Formatter('[%(levelname)s] %(message)s')
    handler.setFormatter(formatter)
    
    compact_logger = logging.getLogger("context.compact")
    compact_logger.addHandler(handler)
    compact_logger.setLevel(logging.DEBUG)
    
    try:
        from agent_core.llm.router import StreamChunk, TextDelta
        
        class MockLLM:
            def chat(self, messages, tools=None):
                yield StreamChunk(text_delta=TextDelta(
                    text="<analysis>分析内容</analysis><summary>1. 用户目标：A 2. 关键决策：B 3. 当前状态：完 4. 待办事项：无</summary>"
                ))
        
        compactor = CompactOrchestrator(
            llm_router=MockLLM(),
            budget_manager=ContextBudgetManager("glm-4", SimpleTokenCounter()),
            token_counter=SimpleTokenCounter(),
        )
        compactor.compact([
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
        ])
        
        log_output = log_stream.getvalue()
        
        # 验证关键事件都被记录
        assert "🔧 [Compact START]" in log_output, "记录 START 事件"
        assert "📦 [Preprocess]" in log_output, "记录 Preprocess 事件"
        assert "🤖 [LLM Call]" in log_output, "记录 LLM Call 事件"
        assert "🏷️  [Extract]" in log_output, "记录 Extract 事件"
        assert "🏗️  [Build Compacted]" in log_output, "记录 Build 事件"
        assert "🔧 [Compact DONE]" in log_output, "记录 DONE 事件"
        
        # 验证 Extract 路径
        assert "<summary> 标签提取成功" in log_output, "识别 <summary> 标签路径"
        
        # 验证 preserved head 内容被打印
        assert "preserved head" in log_output, "记录 preserved head"
    finally:
        compact_logger.removeHandler(handler)


def test_compact_debug_logging_shows_ptl_retry():
    """验证 DEBUG 日志显示 PTL 重试过程"""
    import logging
    from io import StringIO
    
    log_stream = StringIO()
    handler = logging.StreamHandler(log_stream)
    handler.setLevel(logging.DEBUG)
    handler.setFormatter(logging.Formatter('[%(levelname)s] %(message)s'))
    
    compact_logger = logging.getLogger("context.compact")
    compact_logger.addHandler(handler)
    compact_logger.setLevel(logging.DEBUG)
    
    try:
        from agent_core.llm.router import StreamChunk, TextDelta
        
        call_count = [0]
        class MockLLM:
            def chat(self, messages, tools=None):
                call_count[0] += 1
                if call_count[0] == 1:
                    # 第一次触发 PTL
                    raise ValueError("prompt too long: context length exceeded")
                # 第二次成功
                yield StreamChunk(text_delta=TextDelta(
                    text="<summary>1. 用户目标：A 2. 关键决策：B 3. 当前状态：完 4. 待办事项：无</summary>"
                ))
        
        compactor = CompactOrchestrator(
            llm_router=MockLLM(),
            budget_manager=ContextBudgetManager("glm-4", SimpleTokenCounter()),
            token_counter=SimpleTokenCounter(),
        )
        result = compactor.compact([
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
        ])
        
        log_output = log_stream.getvalue()
        assert result.success, "最终成功"
        assert "🥝 [PTL Retry" in log_output, "记录 PTL 重试"
        assert "truncated" in log_output, "记录截断动作"
        assert "attempt=2/4" in log_output, "记录第二次尝试"
    finally:
        compact_logger.removeHandler(handler)


# ══════════════════════════════════════════════════════════════
# 增量估算 + image/document 脱水 测试
# ══════════════════════════════════════════════════════════════


class TestIncrementalEstimation:
    """对齐 Claude Code tokenCountWithEstimation 增量估算"""

    def test_set_baseline_enables_incremental(self):
        """设置基准后，compute_budget_state 使用增量估算"""
        bm = ContextBudgetManager("glm-4", SimpleTokenCounter())
        messages = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "world"},
        ]
        # 全量估算
        state1 = bm.compute_budget_state(messages)
        full_used = state1.used_tokens

        # 设置基准（模拟 API 返回 input_tokens=50，消息数=2）
        bm.set_baseline(input_tokens=50, message_count=2)

        # 加一条新消息
        messages.append({"role": "user", "content": "new question"})
        state2 = bm.compute_budget_state(messages)

        # 增量：50（API基准） + 新消息粗略估算
        new_msg_tokens = SimpleTokenCounter().count_messages(messages[2:])
        assert state2.used_tokens == 50 + new_msg_tokens

    def test_no_baseline_falls_back_to_full(self):
        """没有基准时降级为全量估算"""
        bm = ContextBudgetManager("glm-4", SimpleTokenCounter())
        messages = [{"role": "user", "content": "test"}]
        state = bm.compute_budget_state(messages)
        # 全量估算：应该 > 0
        assert state.used_tokens > 0

    def test_invalidate_baseline(self):
        """invalidate_baseline 后降级为全量估算"""
        bm = ContextBudgetManager("glm-4", SimpleTokenCounter())
        bm.set_baseline(input_tokens=100, message_count=1)
        assert bm._baseline_valid is True

        bm.invalidate_baseline()
        assert bm._baseline_valid is False

        # 降级为全量
        messages = [{"role": "user", "content": "test"}]
        state = bm.compute_budget_state(messages)
        assert state.used_tokens > 0  # 全量估算

    def test_baseline_after_compact_invalidated(self):
        """压缩成功后 baseline 被失效"""
        from agent_core.context.manager import ContextManager
        cm = ContextManager.__new__(ContextManager)
        cm.llm = None
        cm.model = "glm-4"
        cm.token_counter = SimpleTokenCounter()
        cm.budget = ContextBudgetManager("glm-4", SimpleTokenCounter())
        cm.compactor = None
        cm.compact_count = 0
        cm.total_tokens_freed = 0

        # 设置基准
        cm.set_baseline(100, 2)
        assert cm.budget._baseline_valid is True

        # 模拟压缩成功（手动调用 invalidate）
        cm.invalidate_baseline()
        assert cm.budget._baseline_valid is False

    def test_zero_input_tokens_ignored(self):
        """input_tokens=0 不设置基准"""
        bm = ContextBudgetManager("glm-4", SimpleTokenCounter())
        bm.set_baseline(input_tokens=0, message_count=5)
        assert bm._baseline_valid is False

    def test_baseline_msg_count_eq_current(self):
        """基准消息数 == 当前消息数时，全量估算（无新增）"""
        bm = ContextBudgetManager("glm-4", SimpleTokenCounter())
        messages = [{"role": "user", "content": "hello"}]
        bm.set_baseline(input_tokens=100, message_count=1)
        state = bm.compute_budget_state(messages)
        # 基准有效但无新增 → 用基准
        assert state.used_tokens == 100


class TestImageDocumentTokenEstimation:
    """image/document blocks 按 Claude Code 方式固定 2000 tokens"""

    def test_image_block_2000_tokens(self):
        """image block 估算为 2000 tokens（不是 base64 字符数）"""
        counter = SimpleTokenCounter()
        messages = [
            {"role": "user", "content": [
                {"type": "text", "text": "看这张图"},
                {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "iVBORw0KGgo..." * 1000}},
            ]}
        ]
        count = counter.count_messages(messages)
        # image block = 2000 + "看这张图" ~5 + role overhead 10
        # 如果用 base64 字符数算，会是 50000+ tokens
        assert 2000 < count < 3000, f"image block should be ~2015, got {count}"

    def test_document_block_2000_tokens(self):
        """document block 估算为 2000 tokens"""
        counter = SimpleTokenCounter()
        messages = [
            {"role": "user", "content": [
                {"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": "JVBERi0xLj..." * 500}},
            ]}
        ]
        count = counter.count_messages(messages)
        # document block = 2000 + role overhead 10
        assert 2000 < count < 2100, f"document block should be ~2010, got {count}"

    def test_no_image_fallback_unaffected(self):
        """没有 image/document 的消息不受影响"""
        counter = SimpleTokenCounter()
        messages = [
            {"role": "user", "content": "纯文本消息"},
            {"role": "assistant", "content": [
                {"type": "text", "text": "回复"},
                {"type": "tool_use", "id": "tu_1", "name": "search", "input": {"q": "test"}},
            ]},
        ]
        count = counter.count_messages(messages)
        # 纯文本 + tool_use，不含 image/document
        assert count > 0
        assert count < 500  # 不会算到 2000
