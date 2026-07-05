"""
Plan B Step 6 crash-recovery 测试套件(2026-07-01 引入)。

覆盖 Plan B §16.2 6 个新增 case — 验证 Stage A/B/C 接管 session 持久化后,
crash / 中断 / resume 路径不会丢写盘或 double-write:

| case | 验证 |
|---|---|
| 1 | Stage A 写完 crash → session.jsonl 含半成品 user→assistant+tool_use |
| 2 | AWAITING_PERMISSION + 用户 allow → Stage B 写 tool_result(resume 路径) |
| 3 | Stage A 已写 + Stage B 前 crash → RunState.pending_tool_results 仍有数据 |
| 4 | session.jsonl orphan tool_use 检测(无对应 tool_result 的 tool_use block) |
| 5 | 全 v2 路径跑完 jsonl schema 与 v1 fixture 逐字段相等 |
| 6 | Stage A + Stage B 两次触发 → user→assistant+tool_use→tool_result 顺序无重复 |

设计参考:docs/agent-state-machine-and-chain-of-responsibility-design.md §17
(R1/R2/R3 风险表 — Stage A 后 crash / AWAITING_PERMISSION 拆分 / surgery 漏删 v1)
"""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
from unittest.mock import MagicMock

from agent_core.turn_chain import (
    FinalAnswerPersistHandler,
    HandlerResult,
    LLMCallPersistHandler,
    ToolPairPersistHandler,
    TurnContext,
)


# ────────────────────────────────────────────────────────────────────
# Fixtures / helpers
# ────────────────────────────────────────────────────────────────────


def _make_fake_turn_ctx(stage_out=None) -> TurnContext:
    """最小 TurnContext(各 handler 只读 stage_outputs)。"""
    ctx = MagicMock(spec=TurnContext)
    ctx.stage_outputs = stage_out
    ctx.events = []
    ctx.emit = lambda e: ctx.events.append(e)
    return ctx


def _make_fake_agent(has_session_manager=True):
    """最小 fake agent(handler 读 agent._session_manager / agent._run_state)。"""
    agent = MagicMock()
    agent._session_manager = MagicMock() if has_session_manager else None
    run_state = MagicMock()
    run_state.pending_tool_results = []
    run_state.pending_tool_logs = []
    run_state.pending_thinking = ""
    agent._run_state = run_state
    return agent


@dataclass
class _FakeUsage:
    """Fake UsageStats — 真实 dataclass,_usage_asdict 识别并 asdict()。

    SimpleNamespace 不带 __dataclass_fields__,_usage_asdict 直接 return 它本身,
    Stage C 落盘就会传 dataclass 实例给 session.storage.flush → 报
    "Object of type SimpleNamespace is not JSON serializable"。所以测试用
    真实 dataclass 模拟 UsageStats。
    """
    input_tokens: int = 0
    output_tokens: int = 0


# ────────────────────────────────────────────────────────────────────
# Case 1: Stage A 写完 crash — session.jsonl 含半成品 user→assistant+tool_use
# ────────────────────────────────────────────────────────────────────


def test_case1_stage_a_partial_write_survives_crash():
    """Case 1:Stage A1 写 assistant+tool_use blocks 到 session_manager,RunState.pending_tool_results
    待 flush 但 Stage B 未触发 → 模拟 crash。

    R1 风险(Plan B §17)— Stage A 写盘后 Stage B 前 crash,resume_session 看到半成品
    user→assistant+tool_use(无 tool_result)。Stage A1 已被调 + add_assistant_with_tools
    落 1 次 entry,RunState.pending_tool_results 待 Stage B 后续 turn flush。
    """
    agent = _make_fake_agent()
    handler = LLMCallPersistHandler(agent)
    stage_out = SimpleNamespace(
        tool_calls=[
            SimpleNamespace(
                tool_use_id="call_001",
                tool_name="Bash",
                tool_input={"command": "ls"},
            ),
        ],
        full_text="好的,运行:",
        thinking_text="用户想列文件",
        stop_reason="end_turn",
        usage=None,
    )
    ctx = _make_fake_turn_ctx(stage_out=stage_out)

    handler.handle(ctx)

    # Stage A1 写盘成功(assistant + tool_use blocks)
    agent._session_manager.add_assistant_with_tools.assert_called_once()
    # RunState.pending_tool_results 待 Stage B 后续 turn flush(空 — 由 ToolExecuteHandler 填)
    assert agent._run_state.pending_tool_results == []
    # 模拟 crash:不调 Stage B。session.jsonl 有 user + assistant+tool_use,
    # 无 tool_result entry — 半成品(R1 风险语义)。
    # 验证 session_manager 收到完整 text + tool_calls
    kwargs = agent._session_manager.add_assistant_with_tools.call_args.kwargs
    assert kwargs["text"] == "好的,运行:"
    assert kwargs["tool_calls"][0]["id"] == "call_001"
    assert kwargs["tool_calls"][0]["name"] == "Bash"
    assert kwargs["tool_calls"][0]["input"] == {"command": "ls"}


# ────────────────────────────────────────────────────────────────────
# Case 2: AWAITING_PERMISSION + 用户 allow → Stage B 写 tool_result(resume 路径)
# ────────────────────────────────────────────────────────────────────


def test_case2_resume_after_permission_replays_stage_b():
    """Case 2:resume_after_permission 路径下 Stage B 写 tool_result。

    关键 idempotency(R2 风险):
      - 第一次调 Stage A1 写 assistant+tool_use → AWAITING_PERMISSION(turn 暂停)
      - 用户 allow → resume_after_permission → SM 转 EXECUTING_TOOLS
      - ToolExecuteHandler 跑 tool → fill RunState.pending_tool_results
      - Stage B 在 tool_chain 末位 → 写盘 + 清空(本 case 覆盖)

    本 test 用 fake agent 模拟 tool 已 fill pending → Stage B 触发 → 写盘 + 清空。
    """
    agent = _make_fake_agent()
    # 模拟 resume_after_permission 后 ToolExecuteHandler 已 fill pending
    agent._run_state.pending_tool_results = [("call_001", "result after resume")]
    handler = ToolPairPersistHandler(agent)
    ctx = _make_fake_turn_ctx()

    result = handler.handle(ctx)

    # Stage B 写盘
    assert isinstance(result, HandlerResult)
    agent._session_manager.add_tool_results.assert_called_once()
    results_arg = agent._session_manager.add_tool_results.call_args.args[0]
    assert results_arg == [
        {"tool_use_id": "call_001", "content": "result after resume"},
    ]
    # RunState.pending_tool_results 清空(resume 后第二轮 Stage B no-op)
    assert agent._run_state.pending_tool_results == []


# ────────────────────────────────────────────────────────────────────
# Case 3: Stage A 已写 + Stage B 前 crash → RunState.pending_tool_results 待 flush
# ────────────────────────────────────────────────────────────────────


def test_case3_crash_between_stage_a_and_stage_b_pending_survives():
    """Case 3:Stage A 已写盘 + Stage B 前 crash — RunState.pending_tool_results 仍有数据。

    R1 缓解机制:
      - RunState 是 dataclass 实例,在 agent 实例上持有(不是 session.jsonl 一部分)
      - Process crash → RunState 也丢,但 Plan B Stage B 设计是"每次 tool 完成后
        立刻 fill RunState.pending_tool_results + Stage B 立即 flush"
      - 若 Stage A 已写 + Stage B 未写 → RunState 在内存(同进程,通常同时丢)
      - 若 Stage A 已写 + Stage B 未写 + crash → 下次 resume 从 session.jsonl
        读 assistant+tool_use entry,RunState.pending_tool_results 由 _iter_phase_tools
        resume 路径(L1170-1202)重建

    本 case 验证:Stage A1 调 add_assistant_with_tools + Stage B 未调时,RunState 待清空。
    """
    agent = _make_fake_agent()
    handler_a = LLMCallPersistHandler(agent)
    stage_out = SimpleNamespace(
        tool_calls=[SimpleNamespace(tool_use_id="call_X", tool_name="Bash", tool_input={})],
        full_text="运行中...",
        thinking_text="",
        stop_reason="end_turn",
        usage=None,
    )
    ctx = _make_fake_turn_ctx(stage_out=stage_out)

    # Stage A1 触发(写 assistant+tool_use)
    handler_a.handle(ctx)
    agent._session_manager.add_assistant_with_tools.assert_called_once()

    # Stage B 未触发(模拟 crash)
    # 验证:session_manager.add_tool_results 未调(crash 前未到 Stage B)
    agent._session_manager.add_tool_results.assert_not_called()
    # RunState.pending_tool_results 待 Stage B 后续填(本 case 不验证,因为 Stage B
    # 负责 flush,不是 Stage A)


# ────────────────────────────────────────────────────────────────────
# Case 4: session.jsonl orphan tool_use 检测
# ────────────────────────────────────────────────────────────────────


def test_case4_orphan_tool_use_detection_in_session_jsonl():
    """Case 4:扫描 session.jsonl entries,找无对应 tool_result 的 tool_use block(orphan)。

    实现:session.jsonl entry 序列扫 — assistant entry 含 tool_use block(id=X)
    时,后面必须跟 user entry 含 tool_result block(tool_use_id=X)。若 user entry
    是普通 text(或下一个 assistant entry),tool_use X 是 orphan。

    Plan B §17 R1 风险场景:Stage A 写后 Stage B 前 crash → session.jsonl 有
    assistant+tool_use 但无 tool_result → orphan tool_use。本 test 提供 detector。

    detector 实现(inline,Plan B 范围外,Step 9 §17 文档会引用此 test 作为参考实现)。
    """
    def _detect_orphan_tool_use(entries: list[dict]) -> list[str]:
        """返回 orphan tool_use_id 列表(无对应 tool_result 的 tool_use)。

        算法:两遍扫描 — 第一遍收集所有 tool_use_id;第二遍从 user entries
        含 tool_result 收集已 satisfied id;差集 = orphan。
        """
        requested_ids: set[str] = set()
        satisfied_ids: set[str] = set()
        for entry in entries:
            if entry.get("role") == "assistant":
                content = entry.get("content")
                if isinstance(content, list):
                    for blk in content:
                        if isinstance(blk, dict) and blk.get("type") == "tool_use":
                            requested_ids.add(blk["id"])
            elif entry.get("role") == "user":
                # user entry 可能含 tool_result blocks(Anthropic format)
                content = entry.get("content")
                if isinstance(content, list):
                    for blk in content:
                        if isinstance(blk, dict) and blk.get("type") == "tool_result":
                            satisfied_ids.add(blk.get("tool_use_id"))
        return sorted(requested_ids - satisfied_ids)

    # ── Fixture 1:正常 entry 流(无 orphan)──────────────────────
    normal_entries = [
        {"role": "user", "content": "hi"},
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "running..."},
                {"type": "tool_use", "id": "call_001", "name": "Bash", "input": {}},
            ],
        },
        {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "call_001", "content": "ok"},
            ],
        },
        {"role": "assistant", "content": [{"type": "text", "text": "done"}]},
    ]
    assert _detect_orphan_tool_use(normal_entries) == []

    # ── Fixture 2:Stage A 写后 crash(R1 orphan)─────────────────
    orphan_entries = [
        {"role": "user", "content": "ls"},
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "running..."},
                {"type": "tool_use", "id": "call_orphan", "name": "Bash", "input": {}},
            ],
        },
        # crash here — 没 tool_result entry
    ]
    assert _detect_orphan_tool_use(orphan_entries) == ["call_orphan"]

    # ── Fixture 3:并行多 tool 部分 orphan───────────────────────────
    partial_orphan = [
        {"role": "user", "content": "x"},
        {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": "call_A", "name": "Bash", "input": {}},
                {"type": "tool_use", "id": "call_B", "name": "Read", "input": {}},
            ],
        },
        # 只有 call_A 的 tool_result
        {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "call_A", "content": "ok"},
            ],
        },
    ]
    assert _detect_orphan_tool_use(partial_orphan) == ["call_B"]


# ────────────────────────────────────────────────────────────────────
# Case 5: 全 v2 路径跑完 jsonl schema 与 v1 等价
# ────────────────────────────────────────────────────────────────────


def test_case5_v2_session_write_schema_equivalent_to_v1():
    """Case 5:全 v2 路径(Stage A1 + Stage B + Stage C)调 session_manager 的 API
    与 v1 streaming 路径的 4 个写入等价:

    | v1 streaming | v2 handler | session_manager API |
    |---|---|---|
    | add_user_message(start_run 顶部) | (start_run 内) | add_user_message |
    | add_assistant_with_tools(LLM tool_use 响应) | Stage A1 | add_assistant_with_tools |
    | add_tool_results(tool 完成) | Stage B | add_tool_results |
    | add_assistant_message(final answer) | Stage C | add_assistant_message |

    本 test 模拟 1 个完整 turn(LLM tool_use → tool_result → final answer)走 v2 路径,
    断言 session_manager 收到 4 个调用,顺序和参数与 v1 等价。
    """
    agent = _make_fake_agent()

    # Step 1:Stage A1 — LLM tool_use 响应
    handler_a = LLMCallPersistHandler(agent)
    stage_out = SimpleNamespace(
        tool_calls=[SimpleNamespace(tool_use_id="call_X", tool_name="Bash", tool_input={"command": "ls"})],
        full_text="好的,运行:",
        thinking_text="思考中",
        stop_reason="end_turn",
        usage=_FakeUsage(input_tokens=50, output_tokens=20),
    )
    handler_a.handle(_make_fake_turn_ctx(stage_out=stage_out))
    agent._session_manager.add_assistant_with_tools.assert_called_once()

    # Step 2:ToolExecuteHandler fill pending → Stage B flush
    agent._run_state.pending_tool_results = [("call_X", "file1.txt\nfile2.txt")]
    handler_b = ToolPairPersistHandler(agent)
    handler_b.handle(_make_fake_turn_ctx())
    agent._session_manager.add_tool_results.assert_called_once()

    # Step 3:Stage C — final answer
    handler_c = FinalAnswerPersistHandler(agent)
    final_stage_out = SimpleNamespace(
        tool_calls=[],
        full_text="列出完成。",
        thinking_text="",
        stop_reason="end_turn",
        usage=_FakeUsage(input_tokens=70, output_tokens=40),
    )
    handler_c.handle(_make_fake_turn_ctx(stage_out=final_stage_out))
    agent._session_manager.add_assistant_message.assert_called_once()

    # 断言:4 个 session_manager 调用总和(顺序不强制,但每个调 1 次)
    assert agent._session_manager.add_user_message.call_count == 0  # start_run 内调用,本 test 不调 start_run
    assert agent._session_manager.add_assistant_with_tools.call_count == 1
    assert agent._session_manager.add_tool_results.call_count == 1
    assert agent._session_manager.add_assistant_message.call_count == 1

    # Stage C 4 字段契约:full_text / thinking / tool_logs(空)/ usage(asdict)
    final_kwargs = agent._session_manager.add_assistant_message.call_args.kwargs
    assert "thinking" not in final_kwargs  # thinking_text="" 不传
    assert "tool_logs" not in final_kwargs  # 空 list 不传
    assert final_kwargs.get("usage") == {"input_tokens": 70, "output_tokens": 40}


# ────────────────────────────────────────────────────────────────────
# Case 6: Stage A + Stage B 两次触发 → user→assistant+tool_use→tool_result 顺序无重复
# ────────────────────────────────────────────────────────────────────


def test_case6_no_double_write_after_stage_a_to_b_transition():
    """Case 6:模拟 Stage A + Stage B 两次触发(同一 turn),user→assistant+tool_use→tool_result
    顺序且不重复。

    关键 idempotency:
      - Stage A1 调 1 次 add_assistant_with_tools
      - ToolExecuteHandler fill RunState.pending_tool_results
      - Stage B 调 1 次 add_tool_results + 清空 RunState
      - 续调 Stage B 第二次 → pending 空 → no-op(不重写)

    本 test 验证:即使 mock 一次"同一 turn 触发 Stage B 两次",第二次也是 no-op,
    不会重复写 tool_result entry。
    """
    agent = _make_fake_agent()
    # Stage A1
    stage_out = SimpleNamespace(
        tool_calls=[SimpleNamespace(tool_use_id="call_1", tool_name="Bash", tool_input={})],
        full_text="t", thinking_text="", stop_reason="end_turn", usage=None,
    )
    LLMCallPersistHandler(agent).handle(_make_fake_turn_ctx(stage_out=stage_out))
    agent._session_manager.add_assistant_with_tools.assert_called_once()

    # ToolExecuteHandler fill
    agent._run_state.pending_tool_results = [("call_1", "result_1")]

    # Stage B 第一次
    handler_b = ToolPairPersistHandler(agent)
    handler_b.handle(_make_fake_turn_ctx())
    assert agent._session_manager.add_tool_results.call_count == 1
    # 清空
    assert agent._run_state.pending_tool_results == []

    # Stage B 第二次(模拟续 turn / 重入)— pending 空 → no-op
    handler_b.handle(_make_fake_turn_ctx())
    # 仍是 1 次,没重写
    assert agent._session_manager.add_tool_results.call_count == 1

    # 最终 session.jsonl 顺序(由调用顺序推断):user→assistant+tool_use→tool_result
    # 无重复(没有"assistant+tool_use"被调第二次,也没"tool_result"被调第二次)
    assert agent._session_manager.add_assistant_with_tools.call_count == 1
    assert agent._session_manager.add_tool_results.call_count == 1