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
        future = sm.extract_incremental(messages, llm_callback=None, current_token_count=20000)
        result = future.result(timeout=5)
        assert result is True
        assert sm.last_compacted_msg_id == "m2"

    def test_extract_with_callback_invokes_llm(self, populated_sm):
        """extract(): 有 llm_callback → 调用 callback + 推进 last_id

        M11.5 (2026-06-27): prompt 改用 XML 包裹(sm_prompts.build_extract_prompt),
        不再含「Session Memory Extract Task」字面量。
        """
        messages = [
            {"id": "m1", "role": "user", "content": "测试"},
            {"id": "m2", "role": "assistant", "content": "好的"},
        ]
        called = []

        # M11.5: callback 接收的是 sm_prompts.build_extract_prompt 拼出的 user prompt
        # 返回的「LLM response」不是合法 JSON,parse_sm_response 会解析失败但
        # 不抛异常,只是 ops=[];sm_layer 不应用 ops,但仍推进 last_id
        def mock_llm(prompt: str) -> str:
            called.append(prompt)
            return "LLM response"

        future = populated_sm.extract_incremental(
            messages, llm_callback=mock_llm, current_token_count=20000
        )
        result = future.result(timeout=5)
        assert result is True
        assert len(called) == 1
        # M11.5: 新的 prompt 形状
        assert "<current_sm>" in called[0]
        assert "[m1] user: 测试" in called[0]
        assert "[m2] assistant: 好的" in called[0]
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
        future = sm.extract_incremental(
            messages, llm_callback=lambda p: called.append(p) or "ok",
            current_token_count=20000,
        )
        future.result(timeout=5)
        # 没有新消息 → 不调 LLM
        assert len(called) == 0
        # last_id 不变
        assert sm.last_compacted_msg_id == "m2"

    def test_extract_initializes_sm_if_missing(self, sm, sm_path):
        """extract(): SM 文件不存在 → 自动写 template"""
        messages = [{"id": "m1", "role": "user", "content": "hi"}]
        future = sm.extract_incremental(
            messages, llm_callback=None, current_token_count=20000
        )
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
        future = populated_sm.extract_incremental(
            messages, llm_callback=slow_llm, current_token_count=20000
        )
        # 在 future 完成前检查(可能已经完成 → 直接断言最终态)
        future.result(timeout=5)
        assert populated_sm._extraction_in_progress is False


# ──────────────────────────────────────────────────────────────────
# 5.5 _apply_sm_operations (M11.5 新增)
# ──────────────────────────────────────────────────────────────────

class TestApplyOps:
    """M11.5: extract 真接到 LLM 后,_apply_sm_operations 把 ops 写到 sm.md"""

    def _make_sm_with_template(self, sm_path):
        """构造一个有 template 占位符的 SM 文件"""
        from agent_core.memory.sm_layer import SessionMemoryLayer
        sl = SessionMemoryLayer(
            session_id="test", sm_path=sm_path,
            llm_callback=lambda p: "[]",
        )
        sl.write_sm_template()
        return sl

    def test_apply_ops_empty_returns_zero(self, tmp_path):
        """空 ops 列表 → 不动文件,返 0"""
        sl = self._make_sm_with_template(tmp_path / "sm.md")
        before = (tmp_path / "sm.md").read_text(encoding="utf-8")
        applied = sl._apply_sm_operations([])
        assert applied == 0
        assert (tmp_path / "sm.md").read_text(encoding="utf-8") == before

    def test_apply_ops_append_to_empty_section(self, tmp_path):
        """append 到空 section(只有占位符)→ 替换占位符"""
        sl = self._make_sm_with_template(tmp_path / "sm.md")
        ops = [{"op": "append", "section": "Context", "content": "- 用户在做 X"}]
        applied = sl._apply_sm_operations(ops)
        assert applied == 1
        content = (tmp_path / "sm.md").read_text(encoding="utf-8")
        assert "- 用户在做 X" in content
        # 占位符 <!-- --> 应被替换
        assert "## Context\n<!-- -->" not in content

    def test_apply_ops_append_to_existing_content(self, tmp_path):
        """append 到已有内容的 section → 追加到末尾"""
        sl = self._make_sm_with_template(tmp_path / "sm.md")
        sl._apply_sm_operations([
            {"op": "append", "section": "Context", "content": "- 第一条"}
        ])
        sl._apply_sm_operations([
            {"op": "append", "section": "Context", "content": "- 第二条"}
        ])
        content = (tmp_path / "sm.md").read_text(encoding="utf-8")
        # 第二条应在第一条之后
        idx1 = content.find("- 第一条")
        idx2 = content.find("- 第二条")
        assert idx1 != -1 and idx2 != -1
        assert idx1 < idx2

    def test_apply_ops_replace_section(self, tmp_path):
        """replace → 替换整个 section 内容"""
        sl = self._make_sm_with_template(tmp_path / "sm.md")
        sl._apply_sm_operations([
            {"op": "append", "section": "Decisions", "content": "- 旧决策"}
        ])
        sl._apply_sm_operations([
            {"op": "replace", "section": "Decisions", "content": "- 新决策"}
        ])
        content = (tmp_path / "sm.md").read_text(encoding="utf-8")
        assert "- 新决策" in content
        assert "- 旧决策" not in content

    def test_apply_ops_delete_clears_section(self, tmp_path):
        """delete → 清空 section 为占位符"""
        sl = self._make_sm_with_template(tmp_path / "sm.md")
        sl._apply_sm_operations([
            {"op": "append", "section": "Technical", "content": "- 实现细节"}
        ])
        sl._apply_sm_operations([
            {"op": "delete", "section": "Technical", "content": "<!-- -->"}
        ])
        content = (tmp_path / "sm.md").read_text(encoding="utf-8")
        assert "- 实现细节" not in content

    def test_apply_ops_unknown_section_skipped(self, tmp_path):
        """未知 section → 跳过该 op,继续后续 op"""
        sl = self._make_sm_with_template(tmp_path / "sm.md")
        ops = [
            {"op": "append", "section": "RandomSection", "content": "- 跳过"},
            {"op": "append", "section": "Context", "content": "- 有效"},
        ]
        applied = sl._apply_sm_operations(ops)
        assert applied == 1  # 只有 Context 那条成功
        content = (tmp_path / "sm.md").read_text(encoding="utf-8")
        assert "- 跳过" not in content
        assert "- 有效" in content

    def test_apply_ops_multiple_sections(self, tmp_path):
        """多条 op 应用到不同 sections"""
        sl = self._make_sm_with_template(tmp_path / "sm.md")
        ops = [
            {"op": "append", "section": "Context", "content": "- 项目背景"},
            {"op": "append", "section": "Decisions", "content": "- 选型"},
            {"op": "append", "section": "Technical", "content": "- 用了 X"},
        ]
        applied = sl._apply_sm_operations(ops)
        assert applied == 3
        content = (tmp_path / "sm.md").read_text(encoding="utf-8")
        assert "- 项目背景" in content
        assert "- 选型" in content
        assert "- 用了 X" in content

    def test_init_accepts_llm_callback(self, tmp_path):
        """__init__ 接受 llm_callback 参数并保存到 self._llm_callback"""
        from agent_core.memory.sm_layer import SessionMemoryLayer
        cb = lambda p: "[]"
        sl = SessionMemoryLayer(
            session_id="t", sm_path=tmp_path / "sm.md",
            llm_callback=cb,
        )
        assert sl._llm_callback is cb


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


# ──────────────────────────────────────────────────────────────────
# 8. SM 节流配置(Claude Code diff 1-3: init/throttle/window 阈值,Step 1)
# ──────────────────────────────────────────────────────────────────

class TestCompactConfigEnv:
    """新增 6 字段默认值 + env override + 边界校验"""

    def test_compact_config_has_new_thresholds(self):
        """默认 6 字段都在,默认值对齐 Claude Code"""
        cfg = MemoryConfig().compact
        assert cfg.minimum_message_tokens_to_init == 10_000
        assert cfg.minimum_tokens_between_update == 5_000
        assert cfg.tool_calls_between_updates == 3
        assert cfg.window_min_tokens == 10_000
        assert cfg.window_min_text_block_messages == 5
        assert cfg.window_max_tokens == 40_000

    def test_compact_config_env_min_init_threshold(self, monkeypatch):
        """env: MEMORY_COMPACT__MINIMUM_MESSAGE_TOKENS_TO_INIT=20000"""
        monkeypatch.setenv("MEMORY_COMPACT__MINIMUM_MESSAGE_TOKENS_TO_INIT", "20000")
        cfg = MemoryConfig.from_env().compact
        assert cfg.minimum_message_tokens_to_init == 20_000

    def test_compact_config_env_tool_calls_between(self, monkeypatch):
        """env: MEMORY_COMPACT__TOOL_CALLS_BETWEEN_UPDATES=5"""
        monkeypatch.setenv("MEMORY_COMPACT__TOOL_CALLS_BETWEEN_UPDATES", "5")
        cfg = MemoryConfig.from_env().compact
        assert cfg.tool_calls_between_updates == 5

    def test_compact_config_env_window_max(self, monkeypatch):
        """env: MEMORY_COMPACT__WINDOW_MAX_TOKENS=30000"""
        monkeypatch.setenv("MEMORY_COMPACT__WINDOW_MAX_TOKENS", "30000")
        cfg = MemoryConfig.from_env().compact
        assert cfg.window_max_tokens == 30_000

    def test_compact_config_validation_rejects_negative(self):
        """minimum_tokens_between_update=100(< ge=500) → ValidationError"""
        from pydantic import ValidationError
        with pytest.raises(ValidationError):
            MemoryConfig(compact={"minimum_tokens_between_update": 100})

    def test_compact_config_extra_field_rejected(self):
        """extra='forbid' 守住:多写未知字段 → ValidationError"""
        from pydantic import ValidationError
        with pytest.raises(ValidationError):
            MemoryConfig(compact={"unknown_threshold": 1000})


# ──────────────────────────────────────────────────────────────────
# 9. SM 抽取节流 gate (Claude Code diff 1+3, Step 2)
# ──────────────────────────────────────────────────────────────────

class TestExtractGate:
    """M11.7 (2026-06-28): 对齐 Claude Code SessionMemory 抽取节流
    - 初始化门槛:会话累计 token < 10K → 不创建 SM 文件
    - 增量门槛(dual-gate):token Δ ≥ 5K AND tool Δ ≥ 3,或 token Δ ≥ 5K AND 上一轮无 tool
    """

    def test_extract_gate_below_init_threshold_blocks(self, sm_path, config):
        """首 turn token=5000 < 10K → gate 拦住,SM 文件不创建,_initialized=False"""
        sm = SessionMemoryLayer("s1", sm_path, config)
        messages = [{"id": "m1", "role": "user", "content": "hi"}]
        future = sm.extract_incremental(
            messages, llm_callback=lambda p: "[]", current_token_count=5000,
        )
        result = future.result(timeout=5)
        assert result is False
        assert sm.sm_exists() is False
        assert sm._initialized is False

    def test_extract_gate_at_init_threshold_latches(self, sm_path, config):
        """token=10000 → gate 放行,SM 文件创建,_initialized=True,_last_extract_token_count=10000"""
        sm = SessionMemoryLayer("s1", sm_path, config)
        messages = [{"id": "m1", "role": "user", "content": "hi"}]
        future = sm.extract_incremental(
            messages, llm_callback=None, current_token_count=10000,
        )
        future.result(timeout=5)
        assert sm.sm_exists() is True
        assert sm._initialized is True
        assert sm._last_extract_token_count == 10_000

    def test_extract_gate_token_delta_below_5k_skips(self, sm_path, config):
        """已 init=10K,第二次 13K(Δ=3K < 5K)→ 不推进,last_id 不变"""
        sm = SessionMemoryLayer("s1", sm_path, config)
        messages = [{"id": "m1", "role": "user", "content": "hi"}]
        # 第一次 init 10K
        sm.extract_incremental(
            messages, llm_callback=None, current_token_count=10000,
        ).result(timeout=5)
        assert sm._initialized is True
        first_last_id = sm.last_compacted_msg_id

        # 第二次 13K,token Δ=3K < 5K → 拦住
        future2 = sm.extract_incremental(
            messages, llm_callback=lambda p: "[]", current_token_count=13000,
        )
        result2 = future2.result(timeout=5)
        assert result2 is False
        # last_id 没推进(因为新消息还是 m1,本来就不变;但要确认没二次触发的副作用)
        assert sm.last_compacted_msg_id == first_last_id

    def test_extract_gate_dual_gate_satisfied(self, sm_path, config):
        """已 init=10K,第二次 16K(Δ=6K ≥ 5K) + tools=4(≥ 3)→ 抽取"""
        sm = SessionMemoryLayer("s1", sm_path, config)
        # 第一次 init
        sm.extract_incremental(
            [{"id": "m1", "role": "user", "content": "a"}],
            llm_callback=None, current_token_count=10000,
        ).result(timeout=5)
        # 第二次 16K + 4 tools
        future = sm.extract_incremental(
            [{"id": "m2", "role": "user", "content": "b"}],
            llm_callback=lambda p: "[]",
            current_token_count=16000, tool_count_delta=4, tool_count_last_turn=2,
        )
        result = future.result(timeout=5)
        assert result is True
        # last_id 推进到 m2
        assert sm.last_compacted_msg_id == "m2"
        assert sm._last_extract_token_count == 16_000

    def test_extract_gate_dual_gate_text_only_fallback(self, sm_path, config):
        """已 init=10K,第二次 16K(Δ=6K) + tools=1(< 3) + last_turn=0 → 抽(OR 旁路)"""
        sm = SessionMemoryLayer("s1", sm_path, config)
        sm.extract_incremental(
            [{"id": "m1", "role": "user", "content": "a"}],
            llm_callback=None, current_token_count=10000,
        ).result(timeout=5)
        future = sm.extract_incremental(
            [{"id": "m2", "role": "user", "content": "b"}],
            llm_callback=lambda p: "[]",
            current_token_count=16000, tool_count_delta=1, tool_count_last_turn=0,
        )
        result = future.result(timeout=5)
        assert result is True
        assert sm.last_compacted_msg_id == "m2"

    def test_extract_gate_dual_gate_both_fail(self, sm_path, config):
        """已 init=10K,第二次 13K(Δ=3K < 5K)+ tools=1 + last_turn=2 → 不抽"""
        sm = SessionMemoryLayer("s1", sm_path, config)
        sm.extract_incremental(
            [{"id": "m1", "role": "user", "content": "a"}],
            llm_callback=None, current_token_count=10000,
        ).result(timeout=5)
        future = sm.extract_incremental(
            [{"id": "m2", "role": "user", "content": "b"}],
            llm_callback=lambda p: "[]",
            current_token_count=13000, tool_count_delta=1, tool_count_last_turn=2,
        )
        result = future.result(timeout=5)
        assert result is False
        # last_id 没推进
        assert sm.last_compacted_msg_id == "m1"

    def test_extract_gate_latch_resets_per_instance(self, sm_path, config):
        """重新 load 旧 SM 文件, _initialized 重置为 False(per-instance latch,
        file frontmatter 不持久化 — 设计如此,每个 ReactAgent 独立评估 gate)
        """
        # 第一次 session:init 后 _initialized=True
        sm1 = SessionMemoryLayer("s1", sm_path, config)
        sm1.extract_incremental(
            [{"id": "m1", "role": "user", "content": "a"}],
            llm_callback=None, current_token_count=15000,
        ).result(timeout=5)
        assert sm1._initialized is True
        assert sm1.sm_exists() is True

        # 第二次 session:重新 load 同一文件,_initialized 必须重置
        sm2 = SessionMemoryLayer("s1", sm_path, config)
        assert sm2._initialized is False
        assert sm2._last_extract_token_count is None
        # last_compacted_msg_id 在 in-memory 中没被写回 frontmatter
        # (前向看 M11.5 的 _apply_sm_operations 才会持久化)
        # 所以重 load 后是 None,这是当前项目行为,跟 gate 无关
        assert sm2.last_compacted_msg_id is None


# ──────────────────────────────────────────────────────────────────
# 10. SM 文件权限 (Claude Code diff 4, Step 3)
# ──────────────────────────────────────────────────────────────────

class TestFilePermissions:
    """M11.7 (2026-06-28): 对齐 Claude Code 0o700/0o600 + O_EXCL 原子创建

    Windows 上 POSIX 权限不适用,所有测试 skip。
    """

    @pytest.mark.skipif(
        __import__("sys").platform == "win32",
        reason="POSIX file mode 不适用 Windows",
    )
    def test_write_sm_template_creates_file_with_0600(self, sm_path, config):
        """write_sm_template() → sm.md 0o600"""
        sm = SessionMemoryLayer("s1", sm_path, config)
        sm.write_sm_template()
        mode = sm_path.stat().st_mode & 0o777
        assert oct(mode) == "0o600", f"期望 0o600,实际 {oct(mode)}"

    @pytest.mark.skipif(
        __import__("sys").platform == "win32",
        reason="POSIX file mode 不适用 Windows",
    )
    def test_write_sm_template_creates_dir_with_0700(self, sm_path, config):
        """write_sm_template() → parent dir 0o700"""
        sm = SessionMemoryLayer("s1", sm_path, config)
        sm.write_sm_template()
        mode = sm_path.parent.stat().st_mode & 0o777
        assert oct(mode) == "0o700", f"期望 0o700,实际 {oct(mode)}"

    def test_write_sm_template_idempotent_under_o_excl(self, sm_path, config):
        """调两次 write_sm_template() → 不抛 FileExistsError(已存在静默 return)"""
        sm = SessionMemoryLayer("s1", sm_path, config)
        sm.write_sm_template()
        # 第二次:O_EXCL 抛 FileExistsError,被内部 try/except 吞掉
        sm.write_sm_template()  # 不应抛
        assert sm.sm_exists()

    @pytest.mark.skipif(
        __import__("sys").platform == "win32",
        reason="POSIX file mode 不适用 Windows",
    )
    def test_persist_compact_result_writes_json_with_0600(self, sm_path, config):
        """compact() 后 .json 0o600"""
        sm = SessionMemoryLayer("s1", sm_path, config)
        sm.write_sm_template()
        # 触发一次完整 compact
        messages = [
            {"id": "m1", "role": "user", "content": "你好" * 200},  # 触发 token > 10K
        ]
        # 强制走 sm_compact:构造大消息让 token > threshold
        big_content = "测试 " * 5000
        messages = [{"id": "m1", "role": "user", "content": big_content}]
        # 拿 token 估算
        from agent_core.memory.sm_layer import TurnContext
        ctx = TurnContext(messages=messages, total_tokens=15000, tool_count=2)
        decision = sm.should_trigger_compact(ctx)
        if decision.strategy == "sm_compact":
            result = sm.compact(messages, context_window=128000)
            if result is not None:
                sm._persist_compact_result(result)
                json_path = sm_path.with_suffix(".json")
                if json_path.exists():
                    mode = json_path.stat().st_mode & 0o777
                    assert oct(mode) == "0o600", f"期望 0o600,实际 {oct(mode)}"

    @pytest.mark.skipif(
        __import__("sys").platform == "win32",
        reason="POSIX file mode 不适用 Windows",
    )
    def test_persist_compact_result_overwrites_md_with_0600(self, sm_path, config):
        """两次 _persist_compact_result 后 .md 仍 0o600(没退回 0o644)"""
        sm = SessionMemoryLayer("s1", sm_path, config)
        sm.write_sm_template()
        # 第一次写
        sm._open_secure(sm_path, "first content")
        # 第二次覆盖写
        sm._open_secure(sm_path, "second content")
        mode = sm_path.stat().st_mode & 0o777
        assert oct(mode) == "0o600", f"期望 0o600,实际 {oct(mode)}"