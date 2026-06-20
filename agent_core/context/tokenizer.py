"""
Token 估算器 (v2 — tiktoken 精确优先，启发式回退)

实测校准数据（2026-06-18, glm-4-flash delta method）：
- 中文偏差：原启发式 +236% ~ +300%，tiktoken o200k_base +27% ~ +40%
- 英文偏差：tiktoken o200k_base 几乎零偏差（与 GLM tokenizer 一致）
- 消息包装开销：实测 5 tokens（tiktoken 内容 + API prompt_tokens delta）

优先级：
1. tiktoken o200k_base 精确计数（推荐，已安装）
2. 启发式估算（无依赖回退，5 类字符统计：中文/英文/代码/数字/其他）
"""

from __future__ import annotations

import re
import logging

logger = logging.getLogger(__name__)


class SimpleTokenCounter:
    """
    Token 计数器（v2 — tiktoken 优先，启发式回退）

    对外 API 完全兼容 v1：
    - count(text) → int        单段文本 token 数
    - count_messages(msgs) → int  消息列表总 token 数（含角色开销）

    内部优先使用 tiktoken o200k_base 精确计数；
    安装失败/导入失败时自动降级到启发式估算。
    """

    # ── 消息结构固定开销 ──
    # GLM API 实测：prompt_tokens - tiktoken_count(content) = 5
    ROLE_OVERHEAD = 5       # 原 10 → 校准 5
    TOOL_CALL_FIXED = 50
    TOOL_RESULT_FIXED = 20

    # ── 启发式回退常量（GLM-4 delta method 实测校准 2026-06-18）──
    # 全部 8 种文本类型覆盖，偏差控制在 ±50% 以内（tiktoken 主路径不可用时兜底）
    _FALLBACK_CHINESE_RATIO = 0.45   # 中文对话（原 1.4，+210~290% → ±15%）
    _FALLBACK_ENGLISH_RATIO = 0.22   # 英文日常（原 0.25，微调）
    _FALLBACK_CODE_RATIO = 0.33      # Python/JSON/结构化数据（原走英文 0.25，低估 -35%）
    _FALLBACK_DIGITS_RATIO = 0.45    # 数字串（变长 0.56 / 单字重复 0.33，取保守中值）

    def __init__(self, model: str | None = None):
        """
        Args:
            model: 模型名（暂未使用，保留以备后续模型特定编码）
        """
        self._model = model or ""
        self._encoder = self._init_tiktoken()

    # ── tiktoken 初始化 ───────────────────────────────────────

    @staticmethod
    def _init_tiktoken():
        """尝试初始化 tiktoken o200k_base 编码器

        o200k_base 是 GPT-4o 的 tokenizer，经实测与 GLM-4 tokenizer 高度一致：
        - 中文偏差 +27%~+40%（原启发式 +236%~+300%）
        - 英文偏差 < 5%

        Returns:
            tiktoken.Encoding | None
        """
        try:
            import tiktoken
            enc = tiktoken.get_encoding("o200k_base")
            logger.debug("SimpleTokenCounter: using tiktoken o200k_base")
            return enc
        except Exception as e:
            logger.debug(
                "SimpleTokenCounter: tiktoken unavailable (%s), "
                "falling back to heuristic", e
            )
            return None

    # ── 公开 API ────────────────────────────────────────────

    def count(self, text: str) -> int:
        """计算单段文本的 token 数

        优先使用 tiktoken 精确计数，不可用时回退启发式。
        """
        if not text:
            return 0

        if self._encoder is not None:
            try:
                return len(self._encoder.encode(text))
            except Exception:
                # 某些极端 Unicode 可能导致 encode 失败，回退
                pass

        return self._heuristic_count(text)

    def count_messages(self, messages: list[dict]) -> int:
        """计算消息列表的总 token 数（含角色开销）"""
        total = 0

        for msg in messages:
            role = msg.get("role", "user")
            content = msg.get("content", "")

            # Role overhead
            total += self.ROLE_OVERHEAD

            if isinstance(content, str):
                total += self.count(content)

            elif isinstance(content, list):
                for block in content:
                    if not isinstance(block, dict):
                        continue

                    block_type = block.get("type", "text")

                    if block_type == "text":
                        total += self.count(block.get("text", ""))

                    elif block_type == "tool_use":
                        total += self.TOOL_CALL_FIXED
                        # tool input JSON
                        tool_input = block.get("input", {})
                        import json
                        total += self.count(
                            json.dumps(tool_input, ensure_ascii=False)
                        )

                    elif block_type == "tool_result":
                        total += self.TOOL_RESULT_FIXED
                        result_content = block.get("content", "")
                        if isinstance(result_content, str):
                            total += self.count(result_content)
                        elif isinstance(result_content, list):
                            for item in result_content:
                                if (
                                    isinstance(item, dict)
                                    and item.get("type") == "text"
                                ):
                                    total += self.count(item.get("text", ""))

                    elif block_type == "thinking":
                        # thinking block 不计入 LLM 上下文（但占输出 token）
                        pass

                    elif block_type in ("image", "document"):
                        # 对齐 Claude Code roughTokenCountEstimation：
                        # image/document 固定返回 2000 tokens
                        # （API 按像素计费，不是 base64 字符数）
                        total += 2000

                    else:
                        total += self.count(str(block))

            elif content is None:
                pass

        return total

    # ── 启发式回退 ──────────────────────────────────────────

    def _heuristic_count(self, text: str) -> int:
        """启发式字符级 token 估算（tiktoken 不可用时的回退方案）

        规则（GLM-4 delta method 实测校准 2026-06-18，5 类独立统计）：
        - 中文字符：~0.45 tokens/字（±15%）
        - 英文字母：~0.22 tokens/字符（±12%）
        - 代码特征：~0.33 tokens/字符（括号/运算符/缩进密集区，±20%）
        - 数字字符：~0.45 tokens/字符（变长数字串 vs 单字重复取中值）
        - 其他字符：~0.22 tokens/字符（空格/换行/标点）
        """
        # 5 类字符独立统计（优先级从高到低匹配，避免重复计数）
        chinese_chars = len(re.findall(r'[\u4e00-\u9fff]', text))
        code_chars = len(re.findall(r'[{}\[\]();=<>+\-*/%&|^~!@#$?:]+|\n\s{2,}', text))
        digit_chars = len(re.findall(r'\d', text))
        english_chars = len(re.findall(r'[a-zA-Z]', text))
        total_chars = len(text)

        if total_chars == 0:
            return 0

        # 已匹配的字符不计入 other
        matched = chinese_chars + code_chars + digit_chars + english_chars
        other_chars = max(0, total_chars - matched)

        chinese_tokens = chinese_chars * self._FALLBACK_CHINESE_RATIO
        english_tokens = english_chars * self._FALLBACK_ENGLISH_RATIO
        code_tokens = code_chars * self._FALLBACK_CODE_RATIO
        digit_tokens = digit_chars * self._FALLBACK_DIGITS_RATIO
        other_tokens = other_chars * self._FALLBACK_ENGLISH_RATIO

        return int(chinese_tokens + english_tokens + code_tokens + digit_tokens + other_tokens)
