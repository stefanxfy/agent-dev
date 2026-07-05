"""
ToolPairPersistHandler (Stage B) 测试套件(2026-07-01 引入)。

覆盖 Plan B §15 step 22 的 4 个 case — Stage B 接管 v1 _iter_phase_tools
普通 tool_result 路径:

| case | 验证 |
|---|---|
| B1 | stage_out.tool_calls 单个 + RunState.pending_tool_results = [(tid, out)] → add_tool_results 调 1 次,RunState 清空 |
| B2 | RunState.pending_tool_results = 多条 (并行 path) → add_tool_results([N 条]) |
| B3 | RunState.pending_tool_results = [] → no-op(普通文本回复轮) |
| B4 | resume_after_permission 路径幂等:Stage B 写后清空 → 第二次调不重复 |

设计参考:docs/agent-state-machine-and-chain-of-responsibility-design.md §10.1
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from agent_core.turn_chain import (
    HandlerResult,
    ToolPairPersistHandler,
    TurnContext,
)


# ────────────────────────────────────────────────────────────────────
# Fixtures / helpers
# ────────────────────────────────────────────────────────────────────


def _make_fake_turn_ctx() -> TurnContext:
    """构造最小 TurnContext(Stage B 只读 ctx 字段)。"""
    ctx = MagicMock(spec=TurnContext)
    ctx.stage_outputs = None
    ctx.events = []
    ctx.emit = lambda e: ctx.events.append(e)
    return ctx


def _make_fake_agent(pending=None, has_session_manager=True, has_run_state=True):
    """构造最小 fake agent(Stage B 读 RunState.pending_tool_results)。"""
    agent = MagicMock()
    agent._session_manager = MagicMock() if has_session_manager else None
    if has_run_state:
        run_state = MagicMock()
        run_state.pending_tool_results = list(pending) if pending is not None else []
        agent._run_state = run_state
    else:
        agent._run_state = None
    return agent


# ────────────────────────────────────────────────────────────────────
# B1: 单 tool 路径
# ────────────────────────────────────────────────────────────────────


def test_b1_stage_b_writes_single_tool_result():
    """B1:RunState.pending_tool_results = 单条 (tid, output) → add_tool_results 调 1 次 + 清空。

    对应 _iter_phase_tools 单 tool 路径(/workspace/agent-dev/agent_core/agent_core.py L1322/1352 普通执行完成)。
    """
    agent = _make_fake_agent(pending=[("call_001", "result text")])
    handler = ToolPairPersistHandler(agent)
    ctx = _make_fake_turn_ctx()

    result = handler.handle(ctx)

    # 1. handle() 返 HandlerResult (无 stop_chain)
    assert isinstance(result, HandlerResult)
    assert not result.stop_chain
    # 2. add_tool_results 调 1 次,内容是 [{"tool_use_id": ..., "content": ...}]
    agent._session_manager.add_tool_results.assert_called_once()
    results_arg = agent._session_manager.add_tool_results.call_args.args[0]
    assert results_arg == [
        {"tool_use_id": "call_001", "content": "result text"},
    ]
    # 3. RunState.pending_tool_results 清空,避免跨 turn 累加
    assert agent._run_state.pending_tool_results == []


# ────────────────────────────────────────────────────────────────────
# B2: 并行多 tool 路径
# ────────────────────────────────────────────────────────────────────


def test_b2_stage_b_writes_multiple_tool_results_in_one_call():
    """B2:RunState.pending_tool_results = 多条(并行多工具)→ add_tool_results([N 条]) 调一次。

    对应 _iter_phase_tools parallel 路径(L1467)— ThreadPool 收集所有 tool result 后
    一次性 append,Stage B 应该一条 user entry 包含所有 tool_result。
    """
    agent = _make_fake_agent(pending=[
        ("call_001", "out1"),
        ("call_002", "out2"),
        ("call_003", "out3"),
    ])
    handler = ToolPairPersistHandler(agent)
    ctx = _make_fake_turn_ctx()

    handler.handle(ctx)

    # 1. add_tool_results 调 1 次,3 条结果一次传入
    agent._session_manager.add_tool_results.assert_called_once()
    results_arg = agent._session_manager.add_tool_results.call_args.args[0]
    assert len(results_arg) == 3
    assert results_arg == [
        {"tool_use_id": "call_001", "content": "out1"},
        {"tool_use_id": "call_002", "content": "out2"},
        {"tool_use_id": "call_003", "content": "out3"},
    ]
    # 2. RunState 清空
    assert agent._run_state.pending_tool_results == []


# ────────────────────────────────────────────────────────────────────
# B3: 普通文本回复轮 — no-op
# ────────────────────────────────────────────────────────────────────


def test_b3_stage_b_no_op_when_pending_empty():
    """B3:RunState.pending_tool_results = [] → 不调 add_tool_results。

    触发场景:LLM 一次性回答问题,没调任何 tool(普通对话)— Stage B 必须跳过。
    """
    agent = _make_fake_agent(pending=[])
    handler = ToolPairPersistHandler(agent)
    ctx = _make_fake_turn_ctx()

    handler.handle(ctx)

    agent._session_manager.add_tool_results.assert_not_called()
    # pending 仍是 []
    assert agent._run_state.pending_tool_results == []


# ────────────────────────────────────────────────────────────────────
# B4: resume_after_permission 路径幂等性
# ────────────────────────────────────────────────────────────────────


def test_b4_stage_b_is_idempotent_via_clear_pending_after_write():
    """B4:resume_after_permission 路径下 Stage B 写盘后清空,第二次调用不重写。

    关键 idempotency:
      - Stage A 写 assistant+tool_use → AWAITING_PERMISSION(turn 暂停)
      - 用户 allow → resume_after_permission 续 turn
      - tool_chain 重跑 → ToolExecuteHandler._iter_phase_tools 调工具 → fill new pending
      - Stage B 在续 turn tool_chain 末位 → 看到新 fill 的 pending → 写盘+清空
      - 后续 turn 续调到 Stage B → pending 空 → no-op(不重写上 turn 的 tool_result)

    本 test 模拟"第二次调 Stage B":第一次 handle 后,RunState 已空,
    第二次 handle 应该 no-op,不调 add_tool_results。
    """
    pending_first = [("call_001", "first_output")]
    agent = _make_fake_agent(pending=pending_first)
    handler = ToolPairPersistHandler(agent)
    ctx = _make_fake_turn_ctx()

    # 第一次调 — 写盘 + 清空
    handler.handle(ctx)
    agent._session_manager.add_tool_results.assert_called_once()
    assert agent._run_state.pending_tool_results == []

    # 第二次调(模拟下一个 turn / 重入)— no-op
    handler.handle(ctx)
    # 调用次数仍是 1(第二次没加)
    assert agent._session_manager.add_tool_results.call_count == 1


# ────────────────────────────────────────────────────────────────────
# Extra: agent._run_state is None — no-op(v1 老路径 fallback)
# ────────────────────────────────────────────────────────────────────


def test_b5_stage_b_no_op_when_run_state_none():
    """B5:agent._run_state is None → handle() 直接 return,不抛。

    触发场景:v1 老路径(agent.run() 直接调 _iter_phase_tools 而不通过 start_run())
    — 这种调用方式将在 Step 7 删 run() 时一并不存在;Step 2 期间作为过渡期兜底。
    """
    agent = _make_fake_agent(has_run_state=False)
    handler = ToolPairPersistHandler(agent)
    ctx = _make_fake_turn_ctx()

    result = handler.handle(ctx)  # 不抛
    assert isinstance(result, HandlerResult)
    agent._session_manager.add_tool_results.assert_not_called()


# ────────────────────────────────────────────────────────────────────
# Extra: agent._session_manager is None — no-op
# ────────────────────────────────────────────────────────────────────


def test_b6_stage_b_no_session_manager():
    """B6:agent._session_manager is None → handle() 直接 return。

    触发场景:用户禁用 session(暂存模式)— Stage B 必须容错。
    """
    agent = _make_fake_agent(pending=[("call_007", "out")], has_session_manager=False)
    handler = ToolPairPersistHandler(agent)
    ctx = _make_fake_turn_ctx()

    result = handler.handle(ctx)  # 不抛
    assert isinstance(result, HandlerResult)
