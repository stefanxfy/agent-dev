"""
Classifier — Haiku YOLO classifier + ANT-only stub

对齐 Claude Code:
- src/utils/transcriptClassifier/transcriptClassifier.ts(classify / isEnabled)
- doc §4.5 Haiku YOLO classifier + §4.5.2 启用条件 + §4.5.3 transcript 截断

核心设计:
1. **ANT-only 默认**:非 anthropic provider 一律 unavailable
2. **三段短路启用条件**:
   - provider == "anthropic"
   - mode in {"default", "auto"}
   - no_settings_match == True
3. **Haiku YOLO 模型**:"claude-haiku-4-5" — 小模型做 tool call 风险分类
4. **transcript 截断**:太长 → unavailable(true)
5. **同步 API**:M1 用 stub 返 unavailable,M3+ 接真 provider
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from typing import Any, Optional

from .permission_types import PermissionMode, ToolPermissionContext


logger = logging.getLogger(__name__)


# ────────────────────────────────────────────────────────────────────
# 常量
# ────────────────────────────────────────────────────────────────────

# 默认 classifier 模型(对齐 CC claude-haiku-4-5)
DEFAULT_CLASSIFIER_MODEL = "claude-haiku-4-5"

# transcript 最大 token 限制(超过则 unavailable)
_MAX_TRANSCRIPT_TOKENS = 100_000

# env 控制
ENV_CLASSIFIER_ENABLED = "TRANSCRIPT_CLASSIFIER_ENABLED"


# ────────────────────────────────────────────────────────────────────
# ClassifierResult — 分类结果 dataclass
# ────────────────────────────────────────────────────────────────────

@dataclass
class ClassifierResult:
    """
    Classifier 决策结果(对齐 CC ClassifierResult)

    字段:
    - should_block: True = 应阻止(deny), False = 应放行(allow)
    - reason: 决策原因(简短)
    - unavailable: True = classifier 不可用(应 fallback 到 default 行为)
    - transcript_too_long: True = transcript 超长跳过分类
    - model: 使用的模型名
    - duration_ms: 分类耗时(毫秒)
    """
    should_block: bool
    reason: str = ""
    unavailable: bool = False
    transcript_too_long: bool = False
    model: str = DEFAULT_CLASSIFIER_MODEL
    duration_ms: float = 0.0

    @property
    def is_allow(self) -> bool:
        """True 表示允许(M1 简化:不用 classifier 时通常 allow)"""
        return not self.should_block and not self.unavailable

    @property
    def is_deny(self) -> bool:
        """True 表示应阻止"""
        return self.should_block


# ────────────────────────────────────────────────────────────────────
# is_classifier_enabled — 三段短路
# ────────────────────────────────────────────────────────────────────

def is_classifier_enabled(
    provider: str,
    mode: PermissionMode,
    no_settings_match: bool,
) -> bool:
    """
    检查 classifier 是否启用(对齐 CC isTranscriptClassifierEnabled + doc §4.5.2)

    三段短路(必须全部为 True):
      1. provider == "anthropic"
      2. mode in {DEFAULT, AUTO}
      3. no_settings_match == True
      4. env TRANSCRIPT_CLASSIFIER_ENABLED == "true"(显式 opt-in)

    Args:
        provider: LLM provider 名("anthropic" / "openai" / "zhipu" / "minimax")
        mode: 当前权限模式
        no_settings_match: 是否有 settings 命中

    Returns:
        True 如果 classifier 应启用

    Examples:
        >>> is_classifier_enabled("anthropic", PermissionMode.DEFAULT, True)
        True
        >>> is_classifier_enabled("openai", PermissionMode.DEFAULT, True)
        False
        >>> is_classifier_enabled("anthropic", PermissionMode.BYPASS, True)
        False
    """
    # 1. Provider 必须是 anthropic
    if provider != "anthropic":
        return False

    # 2. Mode 必须是 default 或 auto
    if mode not in (PermissionMode.DEFAULT, PermissionMode.AUTO):
        return False

    # 3. 必须 no_settings_match(有 settings 命中 → 用户已显式声明偏好)
    if not no_settings_match:
        return False

    # 4. 显式 opt-in env(M1 安全:必须显式开启)
    if not os.environ.get(ENV_CLASSIFIER_ENABLED, "").strip().lower() in ("1", "true", "yes", "on"):
        return False

    return True


# ────────────────────────────────────────────────────────────────────
# HaikuClassifier — 主类
# ────────────────────────────────────────────────────────────────────

class HaikuClassifier:
    """
    Haiku YOLO classifier(对齐 CC TranscriptClassifier)

    用途:
      - 在 auto mode 下,替代用户弹窗,用小模型判断 tool call 是否安全
      - 输入:transcript messages + 当前 tool + tool_input + context
      - 输出:ClassifierResult(should_block / reason / unavailable)

    M1 实现:
      - 默认行为:unavailable=True(M1 不接真 LLM)
      - 可注入 llm_callable:测试时 mock 真分类器
    """

    def __init__(
        self,
        model: str = DEFAULT_CLASSIFIER_MODEL,
        max_transcript_tokens: int = _MAX_TRANSCRIPT_TOKENS,
        llm_callable: Optional[Any] = None,
    ):
        """
        Args:
            model: classifier 使用的模型名
            max_transcript_tokens: transcript 最大 token 数
            llm_callable: 真 LLM 调用 callable(messages, model, **kwargs) -> str
                          测试时可注入 mock
        """
        self.model = model
        self.max_transcript_tokens = max_transcript_tokens
        self.llm_callable = llm_callable

    def classify(
        self,
        messages: list[dict],
        tool_name: str,
        tool_input: dict,
        context: ToolPermissionContext,
    ) -> ClassifierResult:
        """
        分类当前 tool call(同步)

        Args:
            messages: 对话历史(Anthropic content list 格式)
            tool_name: 待调用的工具名
            tool_input: 工具输入参数 dict
            context: 权限上下文

        Returns:
            ClassifierResult

        行为:
          1. transcript 太长 → unavailable=True, transcript_too_long=True
          2. 无 llm_callable → unavailable=True(M1 stub)
          3. 否则调用 llm_callable,parse 出 should_block + reason
        """
        start = time.time()

        # 1. 检查 transcript 长度(简化:每条 message 估 1 token)
        estimated_tokens = self._estimate_tokens(messages)
        if estimated_tokens > self.max_transcript_tokens:
            return ClassifierResult(
                should_block=False,
                reason="transcript too long",
                unavailable=True,
                transcript_too_long=True,
                model=self.model,
                duration_ms=(time.time() - start) * 1000,
            )

        # 2. 检查是否启用
        if not self.llm_callable:
            return ClassifierResult(
                should_block=False,
                reason="classifier not configured (no llm_callable)",
                unavailable=True,
                model=self.model,
                duration_ms=(time.time() - start) * 1000,
            )

        # 3. 调真 classifier
        try:
            prompt = self._build_classifier_prompt(messages, tool_name, tool_input, context)
            response_text = self.llm_callable(
                messages=prompt,
                model=self.model,
                max_tokens=256,
                temperature=0.0,
            )
            return self._parse_classifier_response(response_text, start)
        except Exception as e:
            logger.warning("classifier 调用失败,降级为 unavailable: %s", e)
            return ClassifierResult(
                should_block=False,
                reason=f"classifier error: {e}",
                unavailable=True,
                model=self.model,
                duration_ms=(time.time() - start) * 1000,
            )

    def _estimate_tokens(self, messages: list[dict]) -> int:
        """
        粗估 transcript token 数(M1 简化:每 message ~1000 tokens)

        真接 LLM 时可换 tiktoken 等
        """
        # 简单启发:每个 message 平均 1000 tokens
        return len(messages) * 1000

    def _build_classifier_prompt(
        self,
        messages: list[dict],
        tool_name: str,
        tool_input: dict,
        context: ToolPermissionContext,
    ) -> list[dict]:
        """
        构造 classifier prompt(对齐 CC buildClassifierPrompt)

        结构:
          System: 你是 Haiku classifier,判断 tool call 是否安全
          User: 当前 tool + tool_input + 简短 transcript 摘要
        """
        # 简化:只取最后 5 条 message 作 transcript 摘要
        recent = messages[-5:] if len(messages) > 5 else messages
        transcript_summary = self._summarize_messages(recent)

        prompt = [
            {
                "role": "system",
                "content": (
                    "You are a safety classifier for tool calls. "
                    "Given a tool name, its input, and recent transcript context, "
                    "decide if the tool call should be allowed or blocked.\n"
                    "Reply with a JSON object: {\"should_block\": bool, \"reason\": str}"
                ),
            },
            {
                "role": "user",
                "content": (
                    f"Tool: {tool_name}\n"
                    f"Input: {tool_input}\n"
                    f"Recent transcript:\n{transcript_summary}\n\n"
                    "Decision?"
                ),
            },
        ]
        return prompt

    def _summarize_messages(self, messages: list[dict]) -> str:
        """简化 transcript 摘要"""
        lines = []
        for msg in messages:
            role = msg.get("role", "unknown")
            content = msg.get("content", "")
            if isinstance(content, list):
                content = " ".join(
                    str(c.get("text", "")) if isinstance(c, dict) else str(c)
                    for c in content
                )
            lines.append(f"[{role}] {str(content)[:200]}")
        return "\n".join(lines)

    def _parse_classifier_response(
        self,
        response_text: str,
        start_time: float,
    ) -> ClassifierResult:
        """Parse classifier 输出"""
        import json
        import re

        # 尝试从 response_text 抽 JSON
        # 兼容:response 可能是纯 JSON,或 JSON 嵌在 markdown 里
        text = response_text.strip()

        # 去掉 markdown code fence
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\s*", "", text)
            text = re.sub(r"\s*```$", "", text)

        try:
            data = json.loads(text)
            should_block = bool(data.get("should_block", False))
            reason = str(data.get("reason", ""))
            return ClassifierResult(
                should_block=should_block,
                reason=reason,
                unavailable=False,
                model=self.model,
                duration_ms=(time.time() - start_time) * 1000,
            )
        except (json.JSONDecodeError, ValueError) as e:
            # parse 失败 → unavailable
            logger.warning("classifier 输出 parse 失败: %s — raw: %s", e, text[:200])
            return ClassifierResult(
                should_block=False,
                reason=f"classifier parse error: {e}",
                unavailable=True,
                model=self.model,
                duration_ms=(time.time() - start_time) * 1000,
            )


# ────────────────────────────────────────────────────────────────────
# start_speculative_classifier_check — placeholder
# ────────────────────────────────────────────────────────────────────

def start_speculative_classifier_check(
    messages: list[dict],
    tool_name: str,
    tool_input: dict,
    context: ToolPermissionContext,
    classifier: Optional[HaikuClassifier] = None,
) -> "SpeculativeClassifierHandle":
    """
    启动投机性 classifier check(对齐 CC startSpeculativeClassifierCheck)

    当前 M1 实现:同步调用 + 立即返 handle。M2+ 可改为真 background task。

    Returns:
        SpeculativeClassifierHandle(可 .result() 取结果 / .cancel() 取消)
    """
    classifier = classifier or HaikuClassifier()
    # M1 简化:同步调用
    result = classifier.classify(messages, tool_name, tool_input, context)
    return SpeculativeClassifierHandle(result=result, cancelled=False)


class SpeculativeClassifierHandle:
    """投机性 classifier 句柄(对齐 CC SpeculativeClassifierHandle)"""

    def __init__(self, result: ClassifierResult, cancelled: bool = False):
        self._result = result
        self._cancelled = cancelled

    def result(self) -> ClassifierResult:
        """取结果(同步)"""
        if self._cancelled:
            return ClassifierResult(
                should_block=False,
                reason="cancelled",
                unavailable=True,
            )
        return self._result

    def cancel(self) -> None:
        """取消(M1 stub:仅标记)"""
        self._cancelled = True

    def is_cancelled(self) -> bool:
        return self._cancelled
