"""
LLM 评分 + 提取提示词模板
参考 docs/memory-system-design.md §3.3 L1 合并 + L9 <conversation> 防注入
"""


EXTRACT_SYSTEM_PROMPT = """你是结构化记忆提取助手. 严格按 schema 输出 JSON.

判断两件事:
1. 是否包含"值得长期记住"的新信息
2. 如果是,提取为结构化记忆(4 类:user/feedback/project/reference)

特别说明:
- 已有记忆中已记下的内容,本轮不要再重复提取
- 只提取"新增"的信息
- source_quote 必填(用户原话片段)
- project/reference 类型必须含 "**Why:**" 段
"""


def build_extract_prompt(turns_text: str, existing_memories: list[dict]) -> str:
    """拼 LLM 评分 + 提取 prompt(参考 §6.9.1)"""
    if existing_memories:
        mem_lines = []
        for m in existing_memories:
            ti = m.get("turn_index", "?")
            mem_lines.append(
                f"- [{m.get('type', '?')}] {m.get('title', '?')}: {m.get('body', '?')[:80]} (turn {ti})"
            )
        existing_block = "\n".join(mem_lines)
    else:
        existing_block = "(无)"

    return f"""<existing_memories_in_this_period>
{existing_block}
</existing_memories_in_this_period>

<conversation>
{turns_text}
</conversation>

请评估"本周期"内是否有新记忆值得提取(避免和已记下的重复)。

输出 JSON(严格遵守 schema,不要其他内容):
{{
  "should_extract": true/false,
  "confidence": 0.0-1.0,
  "reason": "若不提取,简短说明原因",
  "candidates": [
    {{
      "type": "user" | "feedback" | "project" | "reference",
      "title": "短标题",
      "body": "一句话描述",
      "why": "若 type=feedback/project,Why 字段",
      "source_quote": "原对话中触发该记忆的逐字引用"
    }}
  ]
}}"""
