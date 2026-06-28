"""
classifier.py 测试

覆盖:
1. is_classifier_enabled 三段短路(provider / mode / no_settings_match / env)
2. HaikuClassifier.classify 默认 stub 返 unavailable
3. transcript_too_long 处理
4. llm_callable mock parse 成功 / 失败
5. start_speculative_classifier_check + handle.result/cancel
"""

from __future__ import annotations

import json
import os

import pytest

from agent_core.tools.classifier import (
    DEFAULT_CLASSIFIER_MODEL,
    ENV_CLASSIFIER_ENABLED,
    ClassifierResult,
    HaikuClassifier,
    SpeculativeClassifierHandle,
    is_classifier_enabled,
    start_speculative_classifier_check,
)
from agent_core.tools.permission_types import (
    PermissionMode,
    ToolPermissionContext,
)


# ────────────────────────────────────────────────────────────────────
# is_classifier_enabled — 三段短路
# ────────────────────────────────────────────────────────────────────

class TestIsClassifierEnabled:
    def test_all_conditions_met_returns_true(self, monkeypatch):
        """三段全 True + env = True → True"""
        monkeypatch.setenv(ENV_CLASSIFIER_ENABLED, "true")
        assert is_classifier_enabled("anthropic", PermissionMode.DEFAULT, True) is True

    def test_auto_mode_works(self, monkeypatch):
        """auto mode 也算 default-family"""
        monkeypatch.setenv(ENV_CLASSIFIER_ENABLED, "true")
        assert is_classifier_enabled("anthropic", PermissionMode.AUTO, True) is True

    def test_non_anthropic_provider_disables(self, monkeypatch):
        """非 anthropic provider → False"""
        monkeypatch.setenv(ENV_CLASSIFIER_ENABLED, "true")
        assert is_classifier_enabled("openai", PermissionMode.DEFAULT, True) is False
        assert is_classifier_enabled("zhipu", PermissionMode.DEFAULT, True) is False
        assert is_classifier_enabled("minimax", PermissionMode.DEFAULT, True) is False

    def test_non_default_mode_disables(self, monkeypatch):
        """非 default / auto mode → False"""
        monkeypatch.setenv(ENV_CLASSIFIER_ENABLED, "true")
        assert is_classifier_enabled("anthropic", PermissionMode.BYPASS, True) is False
        assert is_classifier_enabled("anthropic", PermissionMode.PLAN, True) is False
        assert is_classifier_enabled("anthropic", PermissionMode.ACCEPT_EDITS, True) is False
        assert is_classifier_enabled("anthropic", PermissionMode.DONT_ASK, True) is False

    def test_settings_match_disables(self, monkeypatch):
        """no_settings_match = False → False(用户已有显式声明)"""
        monkeypatch.setenv(ENV_CLASSIFIER_ENABLED, "true")
        assert is_classifier_enabled("anthropic", PermissionMode.DEFAULT, False) is False

    def test_env_not_set_disables(self, monkeypatch):
        """env 未启用 → False(M1 安全默认)"""
        monkeypatch.delenv(ENV_CLASSIFIER_ENABLED, raising=False)
        assert is_classifier_enabled("anthropic", PermissionMode.DEFAULT, True) is False

    def test_env_falsy_disables(self, monkeypatch):
        """env falsy 值 → False"""
        for val in ["0", "false", "no", "off", ""]:
            monkeypatch.setenv(ENV_CLASSIFIER_ENABLED, val)
            assert is_classifier_enabled("anthropic", PermissionMode.DEFAULT, True) is False


# ────────────────────────────────────────────────────────────────────
# ClassifierResult — dataclass 行为
# ────────────────────────────────────────────────────────────────────

class TestClassifierResult:
    def test_is_allow_when_not_blocked_and_available(self):
        """不 block 且 available → is_allow"""
        r = ClassifierResult(should_block=False)
        assert r.is_allow is True
        assert r.is_deny is False

    def test_is_deny_when_blocked(self):
        """block → is_deny"""
        r = ClassifierResult(should_block=True, reason="destructive")
        assert r.is_deny is True
        assert r.is_allow is False

    def test_unavailable_not_deny(self):
        """unavailable 不算 deny(应 fallback)"""
        r = ClassifierResult(should_block=False, unavailable=True)
        assert r.is_deny is False
        assert r.is_allow is False

    def test_default_model(self):
        """默认 model = claude-haiku-4-5"""
        r = ClassifierResult(should_block=False)
        assert r.model == DEFAULT_CLASSIFIER_MODEL


# ────────────────────────────────────────────────────────────────────
# HaikuClassifier.classify — stub 行为
# ────────────────────────────────────────────────────────────────────

class TestHaikuClassifierStub:
    def test_no_llm_callable_returns_unavailable(self):
        """无 llm_callable → unavailable=True"""
        c = HaikuClassifier()
        ctx = ToolPermissionContext()
        result = c.classify(
            messages=[{"role": "user", "content": "hello"}],
            tool_name="Read",
            tool_input={"path": "x.py"},
            context=ctx,
        )
        assert result.unavailable is True
        assert result.should_block is False
        assert result.transcript_too_long is False

    def test_transcript_too_long(self):
        """transcript 太长 → unavailable + transcript_too_long"""
        c = HaikuClassifier(max_transcript_tokens=5)  # 极小阈值
        ctx = ToolPermissionContext()
        # 100 messages 远超阈值
        messages = [{"role": "user", "content": f"msg {i}"} for i in range(100)]
        result = c.classify(
            messages=messages,
            tool_name="Read",
            tool_input={"path": "x.py"},
            context=ctx,
        )
        assert result.unavailable is True
        assert result.transcript_too_long is True

    def test_llm_callable_returns_allow(self):
        """llm_callable 返 allow JSON → should_block=False"""
        def mock_llm(messages, model, **kwargs):
            return json.dumps({"should_block": False, "reason": "looks safe"})

        c = HaikuClassifier(llm_callable=mock_llm)
        ctx = ToolPermissionContext()
        result = c.classify(
            messages=[{"role": "user", "content": "hello"}],
            tool_name="Read",
            tool_input={"path": "x.py"},
            context=ctx,
        )
        assert result.unavailable is False
        assert result.should_block is False
        assert result.reason == "looks safe"
        assert result.model == DEFAULT_CLASSIFIER_MODEL

    def test_llm_callable_returns_deny(self):
        """llm_callable 返 deny JSON → should_block=True"""
        def mock_llm(messages, model, **kwargs):
            return json.dumps({"should_block": True, "reason": "rm -rf detected"})

        c = HaikuClassifier(llm_callable=mock_llm)
        ctx = ToolPermissionContext()
        result = c.classify(
            messages=[{"role": "user", "content": "delete everything"}],
            tool_name="Bash",
            tool_input={"command": "rm -rf /"},
            context=ctx,
        )
        assert result.unavailable is False
        assert result.should_block is True
        assert "rm -rf" in result.reason

    def test_llm_callable_parse_error_returns_unavailable(self):
        """llm_callable 返坏 JSON → unavailable"""
        def mock_llm(messages, model, **kwargs):
            return "this is not json"

        c = HaikuClassifier(llm_callable=mock_llm)
        ctx = ToolPermissionContext()
        result = c.classify(
            messages=[{"role": "user", "content": "hi"}],
            tool_name="Read",
            tool_input={},
            context=ctx,
        )
        assert result.unavailable is True
        assert "parse error" in result.reason

    def test_llm_callable_markdown_json(self):
        """llm_callable 返 markdown 包裹 JSON 也能 parse"""
        def mock_llm(messages, model, **kwargs):
            return "```json\n{\"should_block\": true, \"reason\": \"dangerous\"}\n```"

        c = HaikuClassifier(llm_callable=mock_llm)
        ctx = ToolPermissionContext()
        result = c.classify(
            messages=[],
            tool_name="Bash",
            tool_input={"command": "rm -rf /"},
            context=ctx,
        )
        assert result.unavailable is False
        assert result.should_block is True

    def test_llm_callable_exception_returns_unavailable(self):
        """llm_callable 抛异常 → unavailable(M1 鲁棒性)"""
        def mock_llm(messages, model, **kwargs):
            raise RuntimeError("API timeout")

        c = HaikuClassifier(llm_callable=mock_llm)
        ctx = ToolPermissionContext()
        result = c.classify(
            messages=[],
            tool_name="Read",
            tool_input={},
            context=ctx,
        )
        assert result.unavailable is True
        assert "error" in result.reason.lower()

    def test_duration_ms_recorded(self):
        """duration_ms 有记录"""
        c = HaikuClassifier()
        ctx = ToolPermissionContext()
        result = c.classify(
            messages=[{"role": "user", "content": "hi"}],
            tool_name="Read",
            tool_input={},
            context=ctx,
        )
        assert result.duration_ms >= 0


# ────────────────────────────────────────────────────────────────────
# start_speculative_classifier_check — handle 行为
# ────────────────────────────────────────────────────────────────────

class TestSpeculativeClassifierHandle:
    def test_handle_result_returns_classifier_result(self):
        """handle.result() 返 ClassifierResult"""
        handle = start_speculative_classifier_check(
            messages=[{"role": "user", "content": "hi"}],
            tool_name="Read",
            tool_input={},
            context=ToolPermissionContext(),
        )
        assert isinstance(handle, SpeculativeClassifierHandle)
        result = handle.result()
        assert isinstance(result, ClassifierResult)
        assert result.unavailable is True  # stub

    def test_handle_cancel_marks_unavailable(self):
        """cancel 后 result() 返 unavailable"""
        handle = start_speculative_classifier_check(
            messages=[],
            tool_name="Read",
            tool_input={},
            context=ToolPermissionContext(),
        )
        handle.cancel()
        assert handle.is_cancelled() is True
        result = handle.result()
        assert result.unavailable is True
        assert result.reason == "cancelled"

    def test_custom_classifier_passed_through(self):
        """custom classifier 透传"""
        def mock_llm(messages, model, **kwargs):
            return json.dumps({"should_block": True, "reason": "test"})

        classifier = HaikuClassifier(llm_callable=mock_llm)
        handle = start_speculative_classifier_check(
            messages=[],
            tool_name="Read",
            tool_input={},
            context=ToolPermissionContext(),
            classifier=classifier,
        )
        result = handle.result()
        assert result.unavailable is False
        assert result.should_block is True


# ────────────────────────────────────────────────────────────────────
# 集成:context 字段影响 classifier
# ────────────────────────────────────────────────────────────────────

class TestClassifierContextAware:
    def test_full_context_passed_to_llm(self):
        """完整 context 透传给 llm_callable"""
        captured = {}

        def mock_llm(messages, model, **kwargs):
            captured["messages"] = messages
            captured["model"] = model
            return json.dumps({"should_block": False})

        ctx = ToolPermissionContext(mode="default")
        c = HaikuClassifier(llm_callable=mock_llm)
        c.classify(
            messages=[{"role": "user", "content": "do X"}],
            tool_name="Bash",
            tool_input={"command": "ls"},
            context=ctx,
        )
        assert captured["model"] == DEFAULT_CLASSIFIER_MODEL
        # prompt 包含 system + user
        assert len(captured["messages"]) == 2
        assert captured["messages"][0]["role"] == "system"
        assert captured["messages"][1]["role"] == "user"
        # user content 提及 tool_name
        assert "Bash" in captured["messages"][1]["content"]

    def test_summarize_messages_handles_list_content(self):
        """transcript 摘要处理 Anthropic content list 格式"""
        captured = {}

        def mock_llm(messages, model, **kwargs):
            captured["messages"] = messages
            return json.dumps({"should_block": False})

        c = HaikuClassifier(llm_callable=mock_llm)
        c.classify(
            messages=[
                {"role": "user", "content": [
                    {"type": "text", "text": "first text"},
                    {"type": "text", "text": "second text"},
                ]},
            ],
            tool_name="Read",
            tool_input={},
            context=ToolPermissionContext(),
        )
        # 摘要应包含 text 内容
        assert "first text" in captured["messages"][1]["content"]
        assert "second text" in captured["messages"][1]["content"]
