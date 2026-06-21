"""
M4 / Day 4 测试 —— L3 SessionMemoryLayer (会话内压缩)

覆盖（v2.1 IMPLEMENTATION_PLAN §Day 4 = 8 个核心 case）:
1. SM 文件不存在 / template 状态检测
2. should_trigger_compact 基础触发(token > 10K)
3. should_trigger_compact 触发条件:tool > 10
4. 未达阈值 → 不触发(走 traditional)
5. 5 条回退条件:gate 关 / 无 SM 文件 / SM 过大 / extraction 在跑 / 压完仍超阈值
6. compact(): 无 SM 文件 → 返 None(让 caller 走传统)
7. compact(): 按 ## section 截断
8. compact(): 保留 last_compacted_msg_id 之后的消息
9. extract_incremental(): 无 llm_callback → 仅推进 last_id
10. extract_incremental(): 有 llm_callback → 正常推进

总: 14 个 case(超出 plan 8 个最低要求,含边界)
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from agent_core.memory import MemoryConfig
from agent_core.memory.sm_layer import (
    CompactDecision,
    CompactResult,
    SessionMemoryError,
    SessionMemoryLayer,
    TurnContext,
)


# ──────────────────────────────────────────────────────────────────
# Fixtures
# ──────────────────────────────────────────────────────────────────

@pytest.fixture
def sm_path(tmp_path):
    """SM 文件路径(每个 test 独立 tmp_path)"""
    return tmp_path / "sm.md"


@pytest.fixture
def config():
    """默认 CompactConfig(阈值 10K tokens, 10 tools)"""
    return MemoryConfig().compact


@pytest.fixture
def sm(sm_path, config):
    """空 SessionMemoryLayer(未初始化 SM 文件)"""
    return SessionMemoryLayer("s1", sm_path, config)


@pytest.fixture
def populated_sm(sm_path, config):
    """已初始化 + 有真实内容的 SM 文件(模拟 extract 跑过一次)"""
    sm = SessionMemoryLayer("s1", sm_path, config)
    sm.write_sm_template()
    # 模拟 LLM Edit 工具填了内容
    content = sm.read_sm()
    content = content.replace(
        "<!-- 当前会话目标、约束、已知事实 -->",
        "用户学习 React,目标 6 周完成项目",
    )
    content = content.replace(
        "<!-- 已做的决策(用户偏好 + 系统决策) -->",
        "1. 用 Vite 不用 CRA\n2. TypeScript strict",
    )
    sm_path.write_text(content, encoding="utf-8")
    return sm


# ──────────────────────────────────────────────────────────────────
# 1. SM 文件状态检测
# ──────────────────────────────────────────────────────────────────

class TestSMFileState:

    def test_sm_exists_false_when_no_file(self, sm):
        """未初始化时 SM 文件不存在"""
        assert sm.sm_exists() is False

    def test_sm_is_template_true_before_init(self, sm):
        """未初始化时 template 判定为 True(SM 不可用)"""
        assert sm.sm_is_template() is True

    def test_sm_is_template_true_after_init(self, sm, sm_path):
        """初始化 template 后,仍判定为 template(占位符未填)"""
        sm.write_sm_template()
        assert sm.sm_exists()
        assert sm.sm_is_template() is True

    def test_sm_is_template_false_after_fill(self, populated_sm):
        """填了实质内容后,template 判定为 False"""
        assert populated_sm.sm_is_template() is False

    def test_write_sm_template_creates_file(self, sm, sm_path):
        """write_sm_template 实际创建文件 + 含正确 frontmatter"""
        sm.write_sm_template()
        assert sm_path.exists()
        content = sm_path.read_text(encoding="utf-8")
        assert "session_id: s1" in content
        assert "schema_version: 1" in content
        assert "last_compacted_msg_id: null" in content
        assert "## Context" in content  # 默认 sections

    def test_write_sm_template_idempotent(self, sm, sm_path):
        """重复调用 write_sm_template 不覆盖已有文件"""
        sm.write_sm_template()
        original = sm_path.read_text()
        # 写第二次
        sm.write_sm_template()
        assert sm_path.read_text() == original


# ──────────────────────────────────────────────────────────────────
# 2. 触发决策 - 基础触发条件
# ──────────────────────────────────────────────────────────────────

class TestTrigger:

    def test_should_trigger_by_token_threshold(self, populated_sm, config):
        """触发条件 1: token >= 阈值 → 走 sm_compact"""
        ctx = TurnContext(messages=[], total_tokens=config.sm_token_threshold, tool_count=0)
        decision = populated_sm.should_trigger_compact(ctx)
        assert decision.strategy == "sm_compact"
        assert decision.reason == "ok"

    def test_should_trigger_by_tool_count(self, populated_sm, config):
        """触发条件 2: tool_count >= 阈值 → 走 sm_compact"""
        ctx = TurnContext(messages=[], total_tokens=100, tool_count=config.tool_count_threshold)
        decision = populated_sm.should_trigger_compact(ctx)
        assert decision.strategy == "sm_compact"

    def test_should_not_trigger_below_threshold(self, populated_sm, config):
        """未达阈值 → traditional(避免误触发)"""
        ctx = TurnContext(
            messages=[],
            total_tokens=config.sm_token_threshold - 1,
            tool_count=config.tool_count_threshold - 1,
        )
        decision = populated_sm.should_trigger_compact(ctx)
        assert decision.strategy == "traditional"
        assert "未达触发阈值" in decision.reason


# ──────────────────────────────────────────────────────────────────
# 3. 5 条回退条件
# ──────────────────────────────────────────────────────────────────

class TestFallbackConditions:

    def test_fallback_1_gate_disabled(self, populated_sm):
        """回退 1: gate 关 → traditional"""
        populated_sm.config.enabled = False
        ctx = TurnContext(messages=[], total_tokens=15000, tool_count=0)
        decision = populated_sm.should_trigger_compact(ctx)
        assert decision.strategy == "traditional"
        assert decision.reason == "gate_disabled"

    def test_fallback_2_no_sm_file(self, sm):
        """回退 2: SM 文件不存在 → traditional"""
        ctx = TurnContext(messages=[], total_tokens=15000, tool_count=0)
        decision = sm.should_trigger_compact(ctx)
        assert decision.strategy == "traditional"
        assert decision.reason == "no_sm_file"

    def test_fallback_2_sm_is_template(self, sm, sm_path):
        """回退 2: SM 是 template → traditional"""
        sm.write_sm_template()
        ctx = TurnContext(messages=[], total_tokens=15000, tool_count=0)
        decision = sm.should_trigger_compact(ctx)
        assert decision.strategy == "traditional"
        assert decision.reason == "no_sm_file"

    def test_fallback_3_sm_too_large(self, populated_sm, config):
        """回退 3: SM 文件过大(超过 max_sm_tokens_for_compact)"""
        # 强制写超大内容
        huge_content = "用户说" * 5000  # 5000 中文字 → ~2250 tokens
        content = populated_sm.read_sm()
        content = content.replace(
            "<!-- 当前会话目标、约束、已知事实 -->",
            huge_content,
        )
        # 多填几个 section 让 token 总数超限
        content = content.replace(
            "<!-- 已做的决策(用户偏好 + 系统决策) -->",
            huge_content,
        )
        content = content.replace(
            "<!-- 技术细节、依赖、API 行为 -->",
            huge_content,
        )
        populated_sm.sm_path.write_text(content, encoding="utf-8")

        # 用更小的阈值便于触发
        populated_sm.config.max_sm_tokens_for_compact = 1000
        ctx = TurnContext(messages=[], total_tokens=15000, tool_count=0)
        decision = populated_sm.should_trigger_compact(ctx)
        assert decision.strategy == "traditional"
        assert "sm_too_large" in decision.reason

    def test_fallback_4_extraction_in_progress(self, populated_sm):
        """回退 4: extraction 在跑 → wait(带 timeout)"""
        # 模拟 extraction 在跑
        populated_sm._extraction_in_progress = True
        ctx = TurnContext(messages=[], total_tokens=15000, tool_count=0)
        decision = populated_sm.should_trigger_compact(ctx)
        assert decision.strategy == "wait"
        assert decision.reason == "extract_running"
        assert decision.timeout_ms > 0

    def test_fallback_5_sm_insufficient(self, populated_sm):
        """回退 5: SM-compact 后预估仍超阈值 → traditional"""
        # 把阈值调到极小,让 kept_messages 任何合理长度都超
        populated_sm.config.sm_token_threshold = 100
        populated_sm.config.sm_insufficient_buffer_ratio = 0.5
        # kept_messages 是 huge 的,远超 100 * 0.5 = 50
        ctx = TurnContext(
            messages=[
                {"id": "m1", "role": "user", "content": "x" * 50000},  # 50000 字符 ≈ 11000 tokens
                {"id": "m2", "role": "assistant", "content": "y" * 50000},
            ],
            total_tokens=20000,
            tool_count=0,
        )
        decision = populated_sm.should_trigger_compact(ctx)
        assert decision.strategy == "traditional"
        assert "sm_insufficient" in decision.reason


# ──────────────────────────────────────────────────────────────────
# 4. compact() - 快路径
# ──────────────────────────────────────────────────────────────────

class TestCompact:

    def test_compact_returns_none_when_no_sm(self, sm):
        """compact(): 无 SM 文件 → 返 None(让 caller 走传统)"""
        result = sm.compact(messages=[{"id": "m1", "role": "user", "content": "x"}], context_window=128000)
        assert result is None

    def test_compact_returns_none_when_template(self, sm, sm_path):
        """compact(): SM 是 template → 返 None"""
        sm.write_sm_template()
        result = sm.compact(messages=[{"id": "m1"}], context_window=128000)
        assert result is None

    def test_compact_produces_summary_message(self, populated_sm):
        """compact(): 产生 summary 消息(含 SM 内容)"""
        messages = [
            {"id": "m1", "role": "user", "content": "你好"},
            {"id": "m2", "role": "assistant", "content": "hi"},
        ]
        result = populated_sm.compact(messages, context_window=128000)
        assert result is not None
        assert isinstance(result, CompactResult)
        assert result.strategy == "sm_compact"
        assert result.summary_message["role"] == "user"
        assert "Session memory summary" in result.summary_message["content"]
        assert "用户学习 React" in result.summary_message["content"]
        assert "Vite" in result.summary_message["content"]

    def test_compact_keeps_all_when_no_last_id(self, populated_sm):
        """compact(): 未设置 last_compacted_msg_id → 保留所有消息"""
        messages = [{"id": f"m{i}", "role": "user", "content": f"msg{i}"} for i in range(5)]
        result = populated_sm.compact(messages, context_window=128000)
        assert result is not None
        assert len(result.kept_messages) == 5
        assert result.kept_messages[0]["id"] == "m0"

    def test_compact_keeps_only_after_last_id(self, populated_sm, sm_path):
        """compact(): 设置 last_id 后,只保留 last_id 之后的消息"""
        # 模拟 SM 文件记录了 last_compacted_msg_id
        content = sm_path.read_text(encoding="utf-8")
        content = content.replace(
            "last_compacted_msg_id: null",
            "last_compacted_msg_id: m2",
        )
        sm_path.write_text(content, encoding="utf-8")

        # 重新加载 layer(从 frontmatter 恢复 last_id)
        sm = SessionMemoryLayer("s1", sm_path, populated_sm.config)
        assert sm.last_compacted_msg_id == "m2"

        messages = [
            {"id": "m1", "role": "user", "content": "first"},
            {"id": "m2", "role": "assistant", "content": "second"},
            {"id": "m3", "role": "user", "content": "third"},
            {"id": "m4", "role": "assistant", "content": "fourth"},
        ]
        result = sm.compact(messages, context_window=128000)
        assert result is not None
        # 只保留 m3, m4(last_id = m2 之后)
        assert len(result.kept_messages) == 2
        assert result.kept_messages[0]["id"] == "m3"
        assert result.kept_messages[1]["id"] == "m4"

    def test_compact_truncates_long_sections(self, sm, sm_path):
        """compact(): 按 ## section 截断超长内容"""
        # 自己建一个 SM 文件(避免 fixture 占位符已替换)
        sm.write_sm_template()
        content = sm_path.read_text(encoding="utf-8")
        long_content = "x" * (sm.config.max_per_section_chars + 1000)
        # 在 Technical section 替换占位符(那个 fixture 没动过)
        content = content.replace(
            "<!-- 技术细节、依赖、API 行为 -->",
            long_content,
        )
        sm_path.write_text(content, encoding="utf-8")

        result = sm.compact([{"id": "m1"}], context_window=128000)
        assert result is not None
        summary = result.summary_message["content"]
        # 截断标记应出现
        assert "[... truncated for brevity ...]" in summary
        # 截断后长度不超过 max_per_section + overhead
        assert len(summary) < sm.config.max_per_section_chars * 2

    def test_compact_estimates_used_tokens(self, populated_sm):
        """compact(): 返回 used_tokens_estimate 字段"""
        messages = [{"id": f"m{i}", "role": "user", "content": f"msg{i}"} for i in range(3)]
        result = populated_sm.compact(messages, context_window=128000)
        assert result is not None
        assert result.used_tokens_estimate > 0
        # 至少包含 SM 的 token 数
        assert result.used_tokens_estimate >= populated_sm.sm_token_count()

    def test_compact_estimates_tokens_for_long_messages(self, populated_sm):
        """回归测试:_estimate_messages_tokens 不能返回负数(累积 bug)

        历史 bug:chinese/english 跨消息累积,other 用 content 减累积 chinese/english,
        从第二条消息起 other 变负数,total token 错算为负数,used_tokens_estimate < 0。
        修复后每条消息独立统计 chinese/english/other,再累加。
        """
        # 5 条 400 字符英文消息 — 旧实现下 total ≈ -60,新实现下 ≈ 5 * 88 = 440
        messages = [{"id": f"m{i}", "role": "user", "content": "x" * 400} for i in range(5)]
        result = populated_sm.compact(messages, context_window=128000)
        assert result is not None
        assert result.used_tokens_estimate > 0, (
            f"used_tokens_estimate 应该为正,实际 = {result.used_tokens_estimate}"
        )
        # 5 条 400 字符英文 + SM + summary overhead,至少 400 tokens
        assert result.used_tokens_estimate >= 400


# ──────────────────────────────────────────────────────────────────
# 5. extract_incremental() - 慢路径
# ──────────────────────────────────────────────────────────────────

class TestExtract:

    def test_extract_with_no_callback_advances_last_id(self, sm):
        """extract(): 无 llm_callback → 仅推进 last_id(测试路径)"""
        sm.write_sm_template()
        messages = [
            {"id": "m1", "role": "user", "content": "你好"},
            {"id": "m2", "role": "assistant", "content": "hi"},
        ]
        future = sm.extract_incremental(messages, llm_callback=None)
        result = future.result(timeout=5)
        assert result is True
        assert sm.last_compacted_msg_id == "m2"

    def test_extract_with_callback_invokes_llm(self, populated_sm):
        """extract(): 有 llm_callback → 调用 callback + 推进 last_id"""
        messages = [
            {"id": "m1", "role": "user", "content": "测试"},
            {"id": "m2", "role": "assistant", "content": "好的"},
        ]
        called = []

        def mock_llm(prompt: str) -> str:
            called.append(prompt)
            return "LLM response"

        future = populated_sm.extract_incremental(messages, llm_callback=mock_llm)
        result = future.result(timeout=5)
        assert result is True
        assert len(called) == 1
        assert "Session Memory Extract Task" in called[0]
        assert populated_sm.last_compacted_msg_id == "m2"

    def test_extract_skips_when_no_new_messages(self, populated_sm, sm_path):
        """extract(): last_id 已覆盖所有消息 → 跳过(不调 LLM)"""
        # 设置 last_id 为最后一条
        content = sm_path.read_text(encoding="utf-8")
        content = content.replace(
            "last_compacted_msg_id: null",
            "last_compacted_msg_id: m2",
        )
        sm_path.write_text(content, encoding="utf-8")

        sm = SessionMemoryLayer("s1", sm_path, populated_sm.config)
        messages = [
            {"id": "m1", "role": "user", "content": "first"},
            {"id": "m2", "role": "assistant", "content": "last"},
        ]
        called = []
        future = sm.extract_incremental(messages, llm_callback=lambda p: called.append(p) or "ok")
        future.result(timeout=5)
        # 没有新消息 → 不调 LLM
        assert len(called) == 0
        # last_id 不变
        assert sm.last_compacted_msg_id == "m2"

    def test_extract_initializes_sm_if_missing(self, sm, sm_path):
        """extract(): SM 文件不存在 → 自动写 template"""
        messages = [{"id": "m1", "role": "user", "content": "hi"}]
        future = sm.extract_incremental(messages, llm_callback=None)
        future.result(timeout=5)
        assert sm.sm_exists()
        assert sm_path.exists()

    def test_extract_sets_in_progress_flag(self, populated_sm):
        """extract(): 跑完后 _extraction_in_progress 复位为 False"""
        # 用一个慢一点的 callback 来观察 in_progress
        def slow_llm(prompt: str) -> str:
            time.sleep(0.2)
            return "ok"

        messages = [{"id": "m1", "role": "user", "content": "hi"}]
        future = populated_sm.extract_incremental(messages, llm_callback=slow_llm)
        # 在 future 完成前检查(可能已经完成 → 直接断言最终态)
        future.result(timeout=5)
        assert populated_sm._extraction_in_progress is False


# ──────────────────────────────────────────────────────────────────
# 6. 数据结构
# ──────────────────────────────────────────────────────────────────

class TestDataStructures:

    def test_turn_context_defaults(self):
        """TurnContext 默认值"""
        ctx = TurnContext(messages=[])
        assert ctx.total_tokens == 0
        assert ctx.tool_count == 0

    def test_compact_decision_construction(self):
        """CompactDecision 4 种 strategy 都能构造"""
        for s in ["sm_compact", "traditional", "wait", "disabled"]:
            d = CompactDecision(strategy=s, reason="test")
            assert d.strategy == s

    def test_compact_result_construction(self):
        """CompactResult 字段齐全"""
        r = CompactResult(
            summary_message={"role": "user", "content": "summary"},
            kept_messages=[{"id": "m1"}],
            used_tokens_estimate=100,
            strategy="sm_compact",
        )
        assert r.used_tokens_estimate == 100
        assert r.strategy == "sm_compact"


# ──────────────────────────────────────────────────────────────────
# 7. 集成(decision → compact 完整链路)
# ──────────────────────────────────────────────────────────────────

class TestIntegration:

    def test_full_flow_should_compact_then_compact(self, populated_sm):
        """完整链路:should_trigger → compact → 拿到 summary + kept"""
        messages = [
            {"id": "m1", "role": "user", "content": "第一句"},
            {"id": "m2", "role": "assistant", "content": "回答"},
            {"id": "m3", "role": "user", "content": "第二句"},
        ]
        ctx = TurnContext(messages=messages, total_tokens=15000, tool_count=2)
        decision = populated_sm.should_trigger_compact(ctx)
        assert decision.strategy == "sm_compact"

        result = populated_sm.compact(messages, context_window=128000)
        assert result is not None
        # summary_message 在最前 + kept_messages 是 last_id 之后的所有消息
        # (no last_id → 全部保留)
        new_messages = [result.summary_message] + result.kept_messages
        # kept_messages = 全部 3 条 + summary = 4 条
        assert len(new_messages) == len(messages) + 1
        # summary 应在第一位
        assert "Session memory summary" in new_messages[0]["content"]
        # 原始消息按顺序保留
        assert new_messages[1]["id"] == "m1"
        assert new_messages[3]["id"] == "m3"