"""
Token 估算器
参考 Claude Code 的 token 估算逻辑，适配 GLM 模型

估算规则（基于经验数据）：
- 中文：约 1.4 tokens / 字
- 英文：约 0.25 tokens / 字
- Role overhead（每条消息的 role 标签）：约 10 tokens
- 工具调用固定开销：50 tokens
- 工具结果固定开销：20 tokens
"""

from __future__ import annotations

import re


class SimpleTokenCounter:
    """
    简单 Token 计数器

    不依赖外部库，纯启发式估算。
    误差约 ±20%，对触发压缩判断足够用。
    """

    CHINESE_RATIO = 1.4   # 中文 tokens/字
    ENGLISH_RATIO = 0.25  # 英文 tokens/字符
    ROLE_OVERHEAD = 10    # 每条消息固定开销
    TOOL_CALL_FIXED = 50  # tool_use block 固定开销
    TOOL_RESULT_FIXED = 20  # tool_result block 固定开销

    def count(self, text: str) -> int:
        """计算单段文本的 token 数"""
        if not text:
            return 0

        chinese_chars = len(re.findall(r'[\u4e00-\u9fff]', text))
        english_chars = len(re.findall(r'[a-zA-Z]', text))
        total_chars = len(text)

        if total_chars == 0:
            return 0

        other_chars = total_chars - chinese_chars - english_chars

        chinese_tokens = chinese_chars * self.CHINESE_RATIO
        english_tokens = english_chars * self.ENGLISH_RATIO
        other_tokens = other_chars * 0.25

        return int(chinese_tokens + english_tokens + other_tokens)

    def count_messages(self, messages: list[dict]) -> int:
        """计算消息列表的总 token 数"""
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
                        total += self.count(json.dumps(tool_input, ensure_ascii=False))

                    elif block_type == "tool_result":
                        total += self.TOOL_RESULT_FIXED
                        result_content = block.get("content", "")
                        if isinstance(result_content, str):
                            total += self.count(result_content)
                        elif isinstance(result_content, list):
                            for item in result_content:
                                if isinstance(item, dict) and item.get("type") == "text":
                                    total += self.count(item.get("text", ""))

                    elif block_type == "thinking":
                        # thinking block 不计入 LLM 上下文（但占输出 token）
                        pass

                    else:
                        total += self.count(str(block))

            elif content is None:
                pass

        return total
