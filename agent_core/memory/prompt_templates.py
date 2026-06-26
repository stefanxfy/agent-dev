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
    """拼去重判定 prompt:候选 vs 已在 MemoryStore 的相似记忆。

    similar_memories: 已由 caller(dual_channel_writer._is_semantic_duplicate)
    通过 MemoryStore.read() 预解析,每条含 title/body/distance。

    新契约(方案 A):similar_memories 不再有 metadata / document 字段。
    Chroma 只存 {id, embedding};title/body 只能从 MemoryStore 读。
    """
    lines = []
    for m in similar_memories:
        title = m.get("title", "?")
        body = m.get("body", "") or ""
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


# ──────────────────────────────────────────────────────────────────
# M11: sideQuery 模式 LLM 选 path(对齐 Claude Code MEMORY.md manifest)
# ──────────────────────────────────────────────────────────────────

SIDE_QUERY_SYSTEM_PROMPT = """你是 memory recall selector。
用户给了一个 query 和一份 manifest(记忆索引), 请从 manifest 中选出 ≤{max_select} 个最相关的 path。

规则:
- 只输出 JSON, 严格按 schema
- 不要选完全无关的(描述不匹配的)
- 少于 {max_select} 个也行(强制过滤)
- 如果都不相关, selected_paths = []
- 不要解释, 不要 markdown fence

JSON schema:
{{"selected_paths": ["user/abc.md", "feedback/xyz.md", ...]}}"""


def build_side_query_prompt(query: str, manifest: str, max_select: int) -> str:
    """拼 sideQuery prompt(注入到 user message)"""
    return f"""<query>
{query}
</query>

<memory_manifest>
{manifest}
</memory_manifest>

请从 manifest 中选 ≤{max_select} 个最相关的 path, 输出 JSON。"""
