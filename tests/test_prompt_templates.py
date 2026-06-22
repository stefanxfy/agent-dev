from agent_core.memory.prompt_templates import (
    EXTRACT_SYSTEM_PROMPT,
    build_extract_prompt,
)


def test_system_prompt_mentions_structured_extraction():
    assert "结构化" in EXTRACT_SYSTEM_PROMPT
    assert "JSON" in EXTRACT_SYSTEM_PROMPT


def test_build_extract_prompt_includes_existing_memories_block():
    prompt = build_extract_prompt(
        turns_text="[turn 5] 我喜欢用 uv",
        existing_memories=[
            {"type": "user", "title": "用户姓名", "body": "张三", "turn_index": 1},
        ],
    )
    assert "<existing_memories_in_this_period>" in prompt
    assert "张三" in prompt
    assert "[turn 5] 我喜欢用 uv" in prompt


def test_build_extract_prompt_empty_existing_memories():
    prompt = build_extract_prompt(
        turns_text="[turn 5] 用户问 Python 协程",
        existing_memories=[],
    )
    assert "(无)" in prompt  # 空提示
    assert "user" in prompt   # schema 提示


def test_build_extract_prompt_includes_4_types():
    prompt = build_extract_prompt(turns_text="x", existing_memories=[])
    for t in ("user", "feedback", "project", "reference"):
        assert t in prompt
