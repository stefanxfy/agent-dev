"""
LLM 评分 + 提取提示词模板
参考 docs/memory-system-design.md §3.3 L1 合并 + L9 <conversation> 防注入
"""


EXTRACT_SYSTEM_PROMPT = """你是结构化记忆提取助手. 严格按 schema 输出 JSON.

判断两件事:
1. 是否包含"值得长期记住"的信息
2. 如果是,提取为结构化记忆(4 类:user/feedback/project/reference)

特别说明:
- source_quote 必填(用户原话片段)
- project/reference 类型必须含 "**Why:**" 段
- 不需要判断是否与已有记忆重复:去重由写盘前的语义去重(向量召回+LLM 判定)统一负责
"""


def build_extract_prompt(turns_text: str) -> str:
    """拼 LLM 提取 prompt。

    只看本轮对话提取记忆;是否与已有记忆重复**不在此判断**——
    去重统一交给写盘前的语义去重(向量召回 + 阈值/LLM 判定)。
    """
    return f"""<conversation>
{turns_text}
</conversation>

请评估本轮对话中是否有值得长期记住的信息,提取为结构化记忆。

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


# ──────────────────────────────────────────────────────────────────
# 语义去重判定(向量召回后,可疑带交 LLM 判)
# ──────────────────────────────────────────────────────────────────

DEDUP_SYSTEM_PROMPT = """你是记忆去重判定助手。判断「候选记忆」是否和「已有记忆」表达同一件事实。

判定为重复(is_duplicate=true)当且仅当:
- 它们陈述的是同一主体的同一事实/偏好,只是措辞不同(如"喜欢华语歌手周杰伦" vs "喜欢华语流行音乐歌手周杰伦")。

判定为不重复(is_duplicate=false)当出现下列任一情况:
- 极性相反(如"喜欢X" vs "不喜欢X")—— 这是两条不同记忆,绝不能合并;
- 主体不同(如"喜欢周杰伦" vs "喜欢周深");
- 候选包含已有记忆没有的新信息,属于补充/更新而非纯重复。

只输出 JSON,不要其他内容:
{"is_duplicate": true/false, "reason": "简短理由"}
"""


def build_dedup_prompt(candidate_text: str, similar_memories: list[dict]) -> str:
    """拼去重判定 prompt:候选 vs 向量召回到的相似记忆。

    similar_memories: vector_store.query 的返回,每条含 metadata.title / document。
    """
    lines = []
    for m in similar_memories:
        meta = m.get("metadata") or {}
        title = meta.get("title", "?")
        body = m.get("document", "") or ""
        lines.append(f"- [{title}] {body[:120]}")
    existing_block = "\n".join(lines) if lines else "(无)"

    return f"""<candidate_memory>
{candidate_text}
</candidate_memory>

<existing_similar_memories>
{existing_block}
</existing_similar_memories>

候选记忆是否与上述任一已有记忆表达同一件事实(仅措辞不同)?
注意:极性相反(喜欢/不喜欢)或主体不同 → 不是重复。

输出 JSON:
{{"is_duplicate": true/false, "reason": "简短理由"}}"""
