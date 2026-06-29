"""sm_callback + sm_prompts 的独立单测。

测试目标：
1. callback 是同步 callable（sm_layer 同步调用约定）
2. 流式 chunks 正确聚合为单一字符串
3. messages 拼装正确（system + user,user 拿到 prompt 原样）
4. cache_namespace 注入 router
5. 重试逻辑：失败 → 退避 → 重试
6. 重试耗尽：on_failure='raise' 抛异常 / 'return_empty' 返空字符串
7. 空响应视为失败（防御性）
8. sm_prompts.build_extract_prompt 正确生成 XML 包裹 prompt
9. sm_prompts.parse_sm_response 容错（markdown 围栏 / 解释文字 / 单条非法跳过）

不需要构造 SessionMemoryLayer 即可独立测试（这就是解耦的价值）。
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from agent_core.memory.sm_callback import call_sm_extract, make_sm_extract_callback
from agent_core.memory.sm_prompts import (
    SM_EDIT_SYSTEM_PROMPT,
    build_extract_prompt,
    parse_sm_response,
)


# ──────────────────────────────────────────────────────────────────
# Fake chunks
# ──────────────────────────────────────────────────────────────────


def _chunk(text: str) -> MagicMock:
    """构造一个 fake StreamChunk,text_delta.text = text"""
    c = MagicMock()
    c.text_delta = MagicMock(text=text)
    c.thinking_delta = None
    c.tool_call = None
    c.usage = None
    c.stop_reason = None
    return c


def _non_text_chunk() -> MagicMock:
    """构造一个没有 text_delta 的 chunk(思考块 / tool_call 块),应被忽略"""
    c = MagicMock()
    c.text_delta = None
    c.thinking_delta = MagicMock(thinking="some thinking")
    return c


def _make_router(chunks_or_side_effect):
    """构造 fake router: chat(messages=..., cache_namespace=...) -> generator;
    invoke() 走与 chat() 相同路径(sm_callback 改用 invoke())。"""
    router = MagicMock()

    def fake_chat(messages, **kw):
        if isinstance(chunks_or_side_effect, list):
            return iter(chunks_or_side_effect)
        return chunks_or_side_effect(messages=messages, **kw)

    def fake_invoke(messages, *, cache_namespace=None, **kwargs):
        """sm_callback._callback 改走 invoke() — 聚合 fake_chat 的 chunks。"""
        chunks = list(fake_chat(messages, cache_namespace=cache_namespace, **kwargs))
        return "".join(
            getattr(c.text_delta, "text", None) or ""
            for c in chunks
            if c.text_delta is not None
        )

    router.chat.side_effect = fake_chat
    router.invoke.side_effect = fake_invoke
    return router


# ──────────────────────────────────────────────────────────────────
# make_sm_extract_callback 测试
# ──────────────────────────────────────────────────────────────────


def test_callback_is_sync_callable():
    """callback 是同步函数(不是 coroutine / generator)"""
    cb = make_sm_extract_callback(router=_make_router([_chunk("ok")]))
    assert callable(cb)
    # 调用立即返 str(不是 awaitable)
    import inspect
    assert not inspect.iscoroutinefunction(cb)
    assert not inspect.isgeneratorfunction(cb)


def test_callback_aggregates_stream_chunks():
    """流式 chunks 拼成完整 str"""
    cb = make_sm_extract_callback(router=_make_router([
        _chunk("Hello "), _chunk("World"), _chunk("!")
    ]))
    result = cb("test prompt")
    assert result == "Hello World!"


def test_callback_skips_non_text_chunks():
    """thinking / tool_call 等非 text chunk 应被忽略,只聚合 text_delta"""
    cb = make_sm_extract_callback(router=_make_router([
        _chunk("alpha "),
        _non_text_chunk(),  # thinking 块,应跳过
        _chunk("beta"),
        _non_text_chunk(),  # 再来一个 thinking 块
        _chunk(" gamma"),
    ]))
    assert cb("p") == "alpha beta gamma"


def test_callback_messages_assembly():
    """messages 拼装:system + user(user.content == prompt 原样)"""
    router = _make_router([_chunk("ok")])
    cb = make_sm_extract_callback(
        router=router, cache_namespace="custom_ns"
    )
    cb("USER PROMPT HERE")

    router.invoke.assert_called_once()
    call_kwargs = router.invoke.call_args.kwargs
    msgs = call_kwargs["messages"]
    assert msgs[0]["role"] == "system"
    assert msgs[0]["content"] == SM_EDIT_SYSTEM_PROMPT
    assert msgs[1]["role"] == "user"
    assert msgs[1]["content"] == "USER PROMPT HERE"
    assert call_kwargs["cache_namespace"] == "custom_ns"


def test_callback_uses_default_cache_namespace():
    """默认 cache_namespace = 'sm_extract'"""
    router = _make_router([_chunk("ok")])
    cb = make_sm_extract_callback(router=router)
    cb("p")
    assert router.invoke.call_args.kwargs["cache_namespace"] == "sm_extract"


def test_callback_passes_max_retries_to_invoke():
    """callback 把 max_retries 透传给 router.invoke()(重试由 invoke() 收归)"""
    router = _make_router([_chunk("ok")])
    cb = make_sm_extract_callback(router=router, max_retries=5)
    cb("p")
    assert router.invoke.call_args.kwargs["max_retries"] == 5


def test_callback_returns_invoke_result():
    """callback 把 invoke() 的返回值原样返给 sm_layer"""
    router = MagicMock()
    router.invoke.return_value = "synthesized sm.md content"
    cb = make_sm_extract_callback(router=router, max_retries=0)
    assert cb("p") == "synthesized sm.md content"


def test_callback_raises_after_all_retries():
    """on_failure='raise': invoke() 抛错时 callback 透传(包装由 router.invoke() on_failure 负责)"""
    router = MagicMock()
    router.invoke.side_effect = RuntimeError("always fails")
    cb = make_sm_extract_callback(
        router=router, max_retries=2, backoff_base=0.001
    )
    with pytest.raises(RuntimeError, match="always fails"):
        cb("p")


def test_callback_return_empty_on_failure():
    """on_failure='return_empty': invoke() on_failure 返空后 callback 原样返空(走 fallback)

    注:真实 router.invoke() 在 on_failure='return_empty' 时自己返空字符串。
    测试模拟"invoke 内部 on_failure 已处理" — mock.invoke.return_value = ''。
    """
    router = MagicMock()
    router.invoke.return_value = ""  # 模拟 invoke() on_failure 返空
    cb = make_sm_extract_callback(
        router=router,
        max_retries=2,
        backoff_base=0.001,
        on_failure="return_empty",
    )
    result = cb("p")
    assert result == ""
    assert router.invoke.call_args.kwargs["on_failure"] is not None


# ──────────────────────────────────────────────────────────────────
# call_sm_extract 便捷函数测试
# ──────────────────────────────────────────────────────────────────


def test_call_sm_extract_passes_sm_text_and_messages():
    """call_sm_extract 应正确拼 prompt 并把响应原样返回"""
    cb = MagicMock(return_value='[{"op":"append","section":"Context","content":"x"}]')

    sm_text = """---
session_id: t
---

# Session Memory

## Context
<!-- -->

## Decisions
<!-- -->

## Technical
<!-- -->

## Open Questions
<!-- -->

## User Preferences
<!-- -->"""

    new_msgs = [
        {"id": "m3", "role": "user", "content": "hi"},
        {"id": "m4", "role": "assistant", "content": "hello"},
    ]

    response = call_sm_extract(
        cb, sm_full_text=sm_text,
        new_messages=new_msgs, last_compacted_msg_id="m2",
    )

    # cb 收到 1 个 prompt 字符串
    cb.assert_called_once()
    prompt_arg = cb.call_args.args[0]
    assert "<current_sm>" in prompt_arg
    assert "[m3] user: hi" in prompt_arg
    assert "[m4] assistant: hello" in prompt_arg
    assert 'after_id="m2"' in prompt_arg
    # 响应原样返回
    assert response.startswith("[{")


def test_call_sm_extract_with_empty_sm():
    """SM 文件还不存在时,prompt 应含「SM 文件不存在」占位"""
    cb = MagicMock(return_value="[]")
    call_sm_extract(
        cb, sm_full_text="",
        new_messages=[{"id": "m0", "role": "user", "content": "x"}],
        last_compacted_msg_id=None,
    )
    prompt_arg = cb.call_args.args[0]
    assert "(SM 文件不存在,首次提取)" in prompt_arg
    assert 'after_id="null"' in prompt_arg


# ──────────────────────────────────────────────────────────────────
# build_extract_prompt 测试(直接测 prompt 拼装)
# ──────────────────────────────────────────────────────────────────


def test_build_extract_prompt_includes_xml_wrappers():
    prompt = build_extract_prompt(
        sm_full_text="sm content",
        new_messages=[{"id": "m0", "role": "user", "content": "hi"}],
        last_compacted_msg_id=None,
    )
    assert "<current_sm>" in prompt
    assert "</current_sm>" in prompt
    assert "<new_messages" in prompt
    assert "</new_messages>" in prompt


def test_build_extract_prompt_message_count_and_after_id():
    prompt = build_extract_prompt(
        sm_full_text="",
        new_messages=[
            {"id": "m5", "role": "user", "content": "a"},
            {"id": "m6", "role": "assistant", "content": "b"},
        ],
        last_compacted_msg_id="m4",
    )
    assert 'count="2"' in prompt
    assert 'after_id="m4"' in prompt
    assert "[m5]" in prompt
    assert "[m6]" in prompt


def test_build_extract_prompt_empty_messages():
    prompt = build_extract_prompt(
        sm_full_text="", new_messages=[], last_compacted_msg_id=None,
    )
    assert "(无新增消息)" in prompt
    assert 'count="0"' in prompt


# ──────────────────────────────────────────────────────────────────
# parse_sm_response 测试(LLM 输出容错)
# ──────────────────────────────────────────────────────────────────


def test_parse_sm_response_clean_json():
    """标准 JSON 数组,直接解析"""
    raw = '[{"op":"append","section":"Context","content":"用户在做 X"}]'
    ops = parse_sm_response(raw)
    assert ops == [
        {"op": "append", "section": "Context", "content": "用户在做 X"}
    ]


def test_parse_sm_response_strips_markdown_fence():
    """LLM 偶尔加 ```json 围栏,应剥掉"""
    raw = """```json
[{"op":"replace","section":"Decisions","content":"新决策"}]
```"""
    ops = parse_sm_response(raw)
    assert len(ops) == 1
    assert ops[0]["op"] == "replace"
    assert ops[0]["section"] == "Decisions"


def test_parse_sm_response_extracts_json_from_explanation():
    """LLM 在 JSON 前后加解释文字,应提取 [...] 块"""
    raw = """好的,根据当前 SM 和新消息,我建议如下操作:

[{"op":"append","section":"Technical","content":"用了 X 库"}]

这样可以保留历史。"""
    ops = parse_sm_response(raw)
    assert len(ops) == 1
    assert ops[0]["section"] == "Technical"


def test_parse_sm_response_empty_array():
    """LLM 返 [] 表示「没新信息」"""
    ops = parse_sm_response("[]")
    assert ops == []


def test_parse_sm_response_delete_normalizes_content():
    """delete 操作的 content 应标准化为占位符(无论 LLM 写什么)"""
    raw = '[{"op":"delete","section":"Open Questions","content":"随便写啥"}]'
    ops = parse_sm_response(raw)
    assert ops == [{"op": "delete", "section": "Open Questions", "content": "<!-- -->"}]


def test_parse_sm_response_skips_invalid_op():
    """op 不在合法值 → 跳过该条"""
    raw = '[{"op":"invalid_op","section":"Context","content":"x"},' \
          '{"op":"append","section":"Context","content":"valid"}]'
    ops = parse_sm_response(raw)
    assert len(ops) == 1
    assert ops[0]["op"] == "append"


def test_parse_sm_response_skips_invalid_section():
    """section 不在 5 个固定值 → 跳过该条"""
    raw = '[{"op":"append","section":"RandomSection","content":"x"},' \
          '{"op":"append","section":"Technical","content":"valid"}]'
    ops = parse_sm_response(raw)
    assert len(ops) == 1
    assert ops[0]["section"] == "Technical"


def test_parse_sm_response_skips_non_dict_items():
    """数组里有非 dict 项(字符串/数字)→ 跳过"""
    raw = '["a string", 123, ' \
          '{"op":"append","section":"Context","content":"valid"}]'
    ops = parse_sm_response(raw)
    assert len(ops) == 1


def test_parse_sm_response_garbage_returns_empty():
    """完全乱码 → 返空列表,sm_layer 走 fallback"""
    assert parse_sm_response("") == []
    assert parse_sm_response("not json at all") == []
    assert parse_sm_response("[invalid json") == []
    assert parse_sm_response('{"not":"an array"}') == []


def test_parse_sm_response_filters_all_invalid():
    """所有条都不合法 → 返空列表"""
    raw = '[{"op":"x","section":"y","content":"z"}]'
    assert parse_sm_response(raw) == []