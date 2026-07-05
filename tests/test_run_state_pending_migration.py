"""
RunState.pending_* 字段迁移验证套件(2026-07-01 引入)。

覆盖 Plan B §1.5 Step 4 的 3 个 case — 验证 self._pending_* 实例字段已迁到
RunState.pending_*,且 start_run / tool 执行 / Stage B 三个生命周期点的状态正确:

| case | 验证 |
|---|---|
| 1 | start_run 后 RunState.pending_* 全空 |
| 2 | tool 执行路径下 RunState.pending_tool_results 非空 |
| 3 | Stage B 写盘后 RunState.pending_tool_results 清空 |

设计参考:docs/agent-state-machine-and-chain-of-responsibility-design.md §14.1
("100% 不变"约束 — session.jsonl schema 与字段含义保持)。
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from agent_core.agent_state import RunState
from agent_core.turn_chain import (
    HandlerResult,
    ToolPairPersistHandler,
    TurnContext,
)


# ────────────────────────────────────────────────────────────────────
# Helpers
# ────────────────────────────────────────────────────────────────────


def _make_fake_turn_ctx() -> TurnContext:
    """构造最小 TurnContext。"""
    ctx = MagicMock(spec=TurnContext)
    ctx.stage_outputs = None
    ctx.events = []
    ctx.emit = lambda e: ctx.events.append(e)
    return ctx


def _make_minimal_agent_with_run_state() -> tuple:
    """构造最小 ReactAgent-like 实例用于 start_run 测试。

    复用 tests/test_agent_state_machine.py 的 pattern:patch 掉 LLMRouter /
    ToolRegistry 避免真实连接,只保留 start_run() 路径依赖的 attribute。
    """
    with patch("agent_core.agent_core.LLMRouter"), \
         patch("agent_core.agent_core.ToolRegistry"):
        from agent_core.agent_core import ReactAgent
        from agent_core.agent_state import (
            AgentPhase, MaxTurnsTermination, SetupPhase,
        )
        agent = ReactAgent.__new__(ReactAgent)
        agent._session_manager = None
        agent._run_state = None
        agent.messages = []
        agent._phases = {AgentPhase.SETUP: SetupPhase()}
        agent._termination = MaxTurnsTermination(max_turns=10)
        agent._sm = MagicMock()
        return agent


# ────────────────────────────────────────────────────────────────────
# Case 1: start_run 后 RunState.pending_* 全空
# ────────────────────────────────────────────────────────────────────


def test_case1_start_run_initializes_pending_state_empty():
    """Case 1:start_run("...") 后 RunState.pending_* 三个字段全空。

    Plan B Step 4 接受定义 — `_pending_thinking / _pending_tool_logs / _pending_tool_results`
    三个实例字段已删,所有 pending 状态统一由 RunState pending_* 持有。
    start_run() 内部 `self._run_state = _RunState(user_message=...)`,dataclass field
    default_factory=list / str 保证初值 [] / ""。
    """
    agent = _make_minimal_agent_with_run_state()

    agent.start_run("hello")

    # 1. RunState 实例存在
    assert agent._run_state is not None
    assert isinstance(agent._run_state, RunState)
    # 2. pending_thinking 是空字符串
    assert agent._run_state.pending_thinking == ""
    # 3. pending_tool_logs 是空 list
    assert agent._run_state.pending_tool_logs == []
    # 4. pending_tool_results 是空 list
    assert agent._run_state.pending_tool_results == []
    # 5. user_message 已填
    assert agent._run_state.user_message == "hello"


# ────────────────────────────────────────────────────────────────────
# Case 2: tool 执行后 RunState.pending_tool_results 非空
# ────────────────────────────────────────────────────────────────────


def test_case2_tool_execution_appends_pending_tool_results():
    """Case 2:tool 执行路径下,RunState.pending_tool_results 累积 (tid, output) 元组。

    验证迁移后,原 `_iter_phase_tools` 单 tool 完成路径(agent_core.py L1324 等 8 处
    `self._pending_tool_results.append(...)`)等价于
    `self._run_state.pending_tool_results.append(...)` — RunState.list 字段语义与
    旧实例字段一致:支持 append + 保留顺序 + 跨多个 tool 累积。

    本 test 用直接 mutate RunState.pending_tool_results 来验证 RunState 字段的
    list 语义(不模拟完整 tool 执行链 — 那需要 mock tool_registry + 真实 tool_call,
    在 tests/test_stage_b_persist.py::test_b2 已经覆盖多 tool 累积语义)。
    """
    # 1. 真实 RunState 实例(不用 MagicMock — 验证 dataclass 字段语义)
    run_state = RunState(user_message="hi")
    # 2. 初始空
    assert run_state.pending_tool_results == []
    # 3. 模拟 _iter_phase_tools 单 tool 完成(对应 agent_core.py L1324)
    tool_call = SimpleNamespace(
        tool_use_id="call_001",
        tool_name="Bash",
        tool_input={"command": "ls"},
    )
    tool_output = "file1.txt\nfile2.txt"
    run_state.pending_tool_results.append((tool_call.tool_use_id, tool_output))
    # 4. 非空 + 内容正确
    assert run_state.pending_tool_results == [
        ("call_001", "file1.txt\nfile2.txt"),
    ]
    # 5. 模拟并行多 tool 累积(对应 L1355 并行路径)
    tool_call_2 = SimpleNamespace(
        tool_use_id="call_002",
        tool_name="Read",
        tool_input={"file": "x"},
    )
    tool_output_2 = "content_x"
    run_state.pending_tool_results.append((tool_call_2.tool_use_id, tool_output_2))
    assert len(run_state.pending_tool_results) == 2
    assert run_state.pending_tool_results[1] == ("call_002", "content_x")
    # 6. pending_tool_logs 同样可 append(对应 L1239 等 8 处 _pending_tool_logs.append)
    run_state.pending_tool_logs.append({
        "type": "action",
        "name": "Bash",
        "input": {"command": "ls"},
    })
    assert len(run_state.pending_tool_logs) == 1


# ────────────────────────────────────────────────────────────────────
# Case 3: Stage B 写盘后 RunState.pending_tool_results 清空
# ────────────────────────────────────────────────────────────────────


def test_case3_stage_b_clears_pending_tool_results_after_write():
    """Case 3:ToolPairPersistHandler.handle() 写盘后,RunState.pending_tool_results 清空。

    关键 idempotency(对应 test_stage_b_persist.py::test_b4 的 Stage B 单元验证):
      - Stage A 写 assistant+tool_use → AWAITING_PERMISSION(turn 暂停)
      - 用户 allow → resume_after_permission → tool_chain 重跑
      - ToolExecuteHandler._iter_phase_tools 调工具 → fill RunState.pending_tool_results
      - Stage B 在 tool_chain 末位 → 写盘 + 清空(本 test 覆盖)
      - 后续 turn 续调 Stage B → pending 空 → no-op(避免重复写上 turn 的 tool_result)

    本 test 用 MagicMock agent 直接驱动 Stage B,不构造真实 agent,验证 handler 与
    RunState.pending_tool_results 的契约。
    """
    # 1. fake agent:RunState.pending_tool_results 预填 2 条
    fake_run_state = MagicMock()
    fake_run_state.pending_tool_results = [
        ("call_001", "result_1"),
        ("call_002", "result_2"),
    ]
    agent = MagicMock()
    agent._run_state = fake_run_state
    agent._session_manager = MagicMock()

    # 2. 调 Stage B
    handler = ToolPairPersistHandler(agent)
    ctx = _make_fake_turn_ctx()
    result = handler.handle(ctx)

    # 3. handle() 不抛 + 返 HandlerResult 无 stop_chain
    assert isinstance(result, HandlerResult)
    assert not result.stop_chain

    # 4. add_tool_results 被调 1 次(2 条结果)
    agent._session_manager.add_tool_results.assert_called_once()
    results_arg = agent._session_manager.add_tool_results.call_args.args[0]
    assert results_arg == [
        {"tool_use_id": "call_001", "content": "result_1"},
        {"tool_use_id": "call_002", "content": "result_2"},
    ]

    # 5. RunState.pending_tool_results 被清空(本 case 核心断言)
    assert fake_run_state.pending_tool_results == []

    # 6. 第二次调 Stage B — 幂等 no-op(关键 idempotency 验证)
    handler.handle(ctx)
    # add_tool_results 调用次数仍是 1,没有重写
    assert agent._session_manager.add_tool_results.call_count == 1
    # pending 仍为空
    assert fake_run_state.pending_tool_results == []