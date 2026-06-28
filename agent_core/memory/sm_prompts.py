"""L3 SessionMemory 专用 prompt 模板。

为什么独立成模块：
- SM 的 prompt 跟 SM 的 IO / 决策逻辑无关，独立成模块好维护
- 改 prompt 不需要碰 sm_layer.py（676 行的核心文件少碰为妙）
- 单元测试 prompt 模板可以单独跑（不需要构造 SessionMemoryLayer）
- 真实 LLM callback 直接 import 这里拼 messages

跟普通 extract_candidate 的 system prompt 区别：
- 提取候选：输入候选文本 → 输出 JSON 结构化字段（type/title/body/tags）
- SM 编辑：输入「当前 SM 全文 + 新增 messages」 → 输出 JSON 操作列表（append/replace/delete）
"""

from __future__ import annotations

import json
from typing import Iterable

# ──────────────────────────────────────────────────────────────────
# System prompt：LLM 的角色定义 + 输出格式约束
# ──────────────────────────────────────────────────────────────────

SM_EDIT_SYSTEM_PROMPT = """你是 L3 SessionMemory 编辑器。

输入：
1. <current_sm>...</current_sm> 当前 SessionMemory 全文
2. <new_messages>...</new_messages> 自上次提取之后的对话消息

任务：根据 new_messages 中的实质信息，更新 current_sm 中的对应 section。

输出格式（严格 JSON 数组，不要任何额外文字 / markdown 标记 / 注释）：
[
  {
    "op": "append" | "replace" | "delete",
    "section": "Context" | "Decisions" | "Technical" | "Open Questions" | "User Preferences",
    "content": "要追加或替换的内容（delete 操作不需要此字段）"
  }
]

约束：
1. 只输出 JSON 数组，不要任何解释 / 前言 / 后缀
2. section 必须是以下 5 个固定值之一（精确匹配大小写）：
   - Context
   - Decisions
   - Technical
   - Open Questions
   - User Preferences
3. append：把新内容加到 section 末尾（保留历史）
4. replace：替换整个 section（先清空旧内容，再写新内容）
5. delete：把 section 置空（content="<!-- -->"），表示该 section 不再适用
6. 不要操作 sm.md 之外的任何文件
7. 没有新信息时返回空数组 []
8. 优先用 append 而不是 replace（保留历史决策痕迹）
"""


# ──────────────────────────────────────────────────────────────────
# User prompt 拼装：把 SM 全文 + 新消息拼成 XML 包裹
# ──────────────────────────────────────────────────────────────────


def build_extract_prompt(
    sm_full_text: str,
    new_messages: Iterable[dict],
    last_compacted_msg_id: str | None,
) -> str:
    """拼 SM extract 用的 user prompt。

    Args:
        sm_full_text: 当前 sm.md 的完整内容（含 frontmatter + sections）。
            若 SM 文件还不存在，传空字符串。
        new_messages: 自 last_compacted_msg_id 之后的对话消息。
            每条形如 {"id": "m3", "role": "user", "content": "..."}。
        last_compacted_msg_id: 上次 extract 推进到的 message id（边界标记）。
            None 表示首次提取。

    Returns:
        str: 完整的 user prompt（XML 标签包裹，便于 LLM 解析边界）。
    """
    msgs_block_lines = []
    for m in new_messages:
        role = m.get("role", "?")
        content = m.get("content", "")
        mid = m.get("id", "?")
        msgs_block_lines.append(f"[{mid}] {role}: {content}")
    msgs_block = "\n".join(msgs_block_lines) or "(无新增消息)"

    sm_block = sm_full_text.strip() if sm_full_text else "(SM 文件不存在,首次提取)"

    return (
        f"<current_sm>\n{sm_block}\n</current_sm>\n\n"
        f"<new_messages count=\"{len(msgs_block_lines)}\" "
        f"after_id=\"{last_compacted_msg_id or 'null'}\">\n"
        f"{msgs_block}\n</new_messages>\n\n"
        f"请根据上述 current_sm 和 new_messages，输出 SM 编辑操作 JSON。"
    )


# ──────────────────────────────────────────────────────────────────
# Response 解析：把 LLM 输出的字符串解析成操作列表
# ──────────────────────────────────────────────────────────────────

VALID_OPS = {"append", "replace", "delete"}
VALID_SECTIONS = {
    "Context",
    "Decisions",
    "Technical",
    "Open Questions",
    "User Preferences",
}


def parse_sm_response(response_text: str) -> list[dict]:
    """把 LLM 的纯文本响应解析为操作列表。

    容错策略：
    - LLM 偶尔会在 JSON 外加 ```json ... ``` markdown 标记 → 剥掉
    - LLM 偶尔会在 JSON 前后加解释文字 → 尝试提取第一个 [...] 块
    - 单条操作不符合契约（op/section 不在合法值）→ 跳过，不抛
    - 整体解析失败 → 返回空列表（让 sm_layer 走 fallback）

    Args:
        response_text: LLM 的原始响应字符串。

    Returns:
        list[dict]: 操作列表，每条形如
            {"op": "append", "section": "Context", "content": "..."}
    """
    if not response_text or not response_text.strip():
        return []

    # Step 1: 剥 markdown 围栏
    text = response_text.strip()
    if text.startswith("```"):
        # 去掉首尾 ``` 或 ```json 行
        lines = text.split("\n")
        lines = [ln for ln in lines if not ln.strip().startswith("```")]
        text = "\n".join(lines).strip()

    # Step 2: 找第一个 [ 到最后一个 ] 的 JSON 数组
    start = text.find("[")
    end = text.rfind("]")
    if start == -1 or end == -1 or end <= start:
        return []
    json_str = text[start : end + 1]

    # Step 3: 解析 JSON
    try:
        parsed = json.loads(json_str)
    except json.JSONDecodeError:
        return []

    if not isinstance(parsed, list):
        return []

    # Step 4: 逐条校验 + 过滤
    ops: list[dict] = []
    for item in parsed:
        if not isinstance(item, dict):
            continue
        op = item.get("op")
        section = item.get("section")
        if op not in VALID_OPS:
            continue
        if section not in VALID_SECTIONS:
            continue
        content = item.get("content", "")
        if op == "delete":
            # delete 操作的 content 固定为占位符（sm_layer 会按 section 清空）
            ops.append({"op": op, "section": section, "content": "<!-- -->"})
        else:
            ops.append({"op": op, "section": section, "content": str(content)})
    return ops