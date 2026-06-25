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
