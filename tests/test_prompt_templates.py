from agent_core.memory.prompt_templates import (
    EXTRACT_SYSTEM_PROMPT,
    build_extract_prompt,
)


def test_system_prompt_mentions_structured_extraction():
    assert "结构化" in EXTRACT_SYSTEM_PROMPT
    assert "JSON" in EXTRACT_SYSTEM_PROMPT


def test_build_extract_prompt_includes_conversation():
    prompt = build_extract_prompt(turns_text="[turn 5] 我喜欢用 uv")
    assert "[turn 5] 我喜欢用 uv" in prompt
    assert "<conversation>" in prompt


def test_build_extract_prompt_no_existing_memories_block():
    """去重已下沉到写盘前语义去重 —— 提取 prompt 不再含「已有记忆」块。"""
    prompt = build_extract_prompt(turns_text="[turn 5] 用户问 Python 协程")
    assert "existing_memories" not in prompt
    assert "已有记忆" not in prompt


def test_build_extract_prompt_includes_4_types():
    prompt = build_extract_prompt(turns_text="x")
    for t in ("user", "feedback", "project", "reference"):
        assert t in prompt


# ──────────────────────────────────────────────────────────────────
# M11 v3: side_query prompt templates
# ──────────────────────────────────────────────────────────────────

def test_side_query_system_prompt_has_json_schema():
    """SIDE_QUERY_SYSTEM_PROMPT 含 selected_paths JSON 字段说明"""
    from agent_core.memory.prompt_templates import SIDE_QUERY_SYSTEM_PROMPT
    assert "selected_paths" in SIDE_QUERY_SYSTEM_PROMPT
    assert "JSON" in SIDE_QUERY_SYSTEM_PROMPT


def test_build_side_query_prompt_includes_query_and_manifest():
    """build_side_query_prompt 把 query + manifest 拼到 prompt"""
    from agent_core.memory.prompt_templates import build_side_query_prompt
    prompt = build_side_query_prompt(
        "用户叫啥",
        "- [用户](user/x.md) — 张三",
        5,
    )
    assert "用户叫啥" in prompt
    assert "- [用户](user/x.md)" in prompt
    assert "5" in prompt


def test_build_side_query_prompt_max_select_in_instruction():
    """prompt 里有 ≤max_select 提示"""
    from agent_core.memory.prompt_templates import build_side_query_prompt
    prompt = build_side_query_prompt("q", "m", 3)
    assert "≤3" in prompt or "≤ 3" in prompt
