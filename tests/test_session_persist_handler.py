"""
SessionPersistHandler 真实现回归测试(2026-06-30 — orphan tool_result fix)。

覆盖(每个独立 case,D4 清单):
    D4.1: 空 _pending_tool_results → early return,no session call
    D4.2: stage_out final-answer(no tool_calls)+ pending populated → add_tool_results 调一次
          (Fix C 修复路径 — LLM 给最终回答、stale _pending 不丢)
    D4.3: stage_out intermediate(tool_calls truthy)+ pending populated → add_tool_results 调一次
          (常规中间轮 — 保持 v1 _iter_phase_finalize 行为)
    D4.4: agent._session_manager is None → no-op,不抛(扩展点模式兼容)

设计参考:docs/agent-state-machine-and-chain-of-responsibility-design.md §4 / §10
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from agent_core.turn_chain import (
    HandlerResult,
    SessionPersistHandler,
    TurnContext,
)


# ────────────────────────────────────────────────────────────────────
# 公共 fixture / helpers
# ────────────────────────────────────────────────────────────────────


def _make_fake_turn_ctx(stage_out=None) -> TurnContext:
    """构造最小 TurnContext(stage_outputs 字段 + events 列表)。

    SessionPersistHandler 实际只读 stage_out(stage_out.tool_calls 用于判断
    intermediate vs final — 但 D1 实现不依赖此字段,无条件刷 pending),所以
    这里只是占位。
    """
    ctx = MagicMock(spec=TurnContext)
    ctx.stage_outputs = stage_out
    ctx.events = []
    ctx.emit = lambda e: ctx.events.append(e)
    return ctx


def _make_fake_agent(pending=None, has_session_manager=True):
    """构造最小 fake agent:SessionPersistHandler 只需要 ._session_manager 和
    ._pending_tool_results 两个属性(handle() 用 getattr 防 attribute error)。
    """
    agent = MagicMock()
    if has_session_manager:
        agent._session_manager = MagicMock()
    else:
        agent._session_manager = None
    agent._pending_tool_results = pending if pending is not None else []
    # _logger 在 handler 内部用 agent._logger.warning 找不到时 fallback 到模块 logger
    agent._logger = MagicMock()
    return agent


# ────────────────────────────────────────────────────────────────────
# D4 测试
# ────────────────────────────────────────────────────────────────────


class TestSessionPersistHandler:
    """SessionPersistHandler 真实现(2026-06-30)回归套件。"""

    def test_d4_1_empty_pending_no_session_call(self):
        """D4.1:_pending_tool_results 为空 → early return,不调 add_tool_results。

        触发场景:LLM 一次性回答问题,没调任何 tool。这是大多数小对话场景。
        """
        agent = _make_fake_agent(pending=[])
        handler = SessionPersistHandler(agent)
        ctx = _make_fake_turn_ctx(stage_out=None)

        result = handler.handle(ctx)

        # 不调 session_manager
        agent._session_manager.add_tool_results.assert_not_called()
        # pending 不变(空还是空)
        assert agent._pending_tool_results == []
        # 返回标准 HandlerResult(stop_chain=False — 后续 AuditLog / MemoryBridge 还能跑)
        assert isinstance(result, HandlerResult)
        assert result.stop_chain is False

    def test_d4_2_final_answer_with_pending_writes_tool_results(self):
        """D4.2 (★ 关键 fix):stage_out final-answer(stage_out.tool_calls=[]+ None)
        + _pending_tool_results 非空 → 仍调 add_tool_results。

        触发场景:Fix C 修复路径 — 用户点 Allow 后,Fix C 重建 stage_out → 工具执行 →
        _pending_tool_results 累积 → LLM 给最终回答 → 进入 FINALIZING → output_chain 跑到
        SessionPersistHandler。这一段之前因为 _iter_phase_finalize:1582 要求
        stage_out.tool_calls truthy 才写,本 case 正是 orphan tool_result bug 的复现场。

        D1 修复后:无条件刷 _pending_tool_results,这条 case 不再丢 tool_result。
        """
        # stage_out 模拟 Fix C 重建后的状态(只有 full_text,无 tool_calls)
        stage_out = MagicMock()
        stage_out.tool_calls = []  # final-answer,no tool_calls
        stage_out.full_text = "文件已创建。"
        stage_out.stop_reason = "stop"

        # _pending_tool_results 残留 resume 路径的执行结果
        pending = [("toolu_test_abc", "-rw-r--r-- 1 user wheel 0 Jun 30 18:08 /tmp/foo.txt\n")]
        agent = _make_fake_agent(pending=list(pending))
        handler = SessionPersistHandler(agent)
        ctx = _make_fake_turn_ctx(stage_out=stage_out)

        result = handler.handle(ctx)

        # ★ 关键断言:即使 stage_out.tool_calls=[] 也写了
        agent._session_manager.add_tool_results.assert_called_once()
        call_args = agent._session_manager.add_tool_results.call_args
        results_arg = call_args[0][0]  # first positional arg
        assert len(results_arg) == 1
        assert results_arg[0]["tool_use_id"] == "toolu_test_abc"
        assert "-rw-r--r--" in results_arg[0]["content"]
        # pending 清空(避免跨 turn 重复持久化)
        assert agent._pending_tool_results == [], (
            "D1 真实现要求:刷完后必须清空 _pending_tool_results,"
            "否则下次 FINALIZING 又写一次 double-write"
        )
        # 不 stop_chain
        assert result.stop_chain is False

    def test_d4_3_intermediate_turn_with_pending_writes_tool_results(self):
        """D4.3:stage_out intermediate(stage_out.tool_calls truthy)+ pending → 写。

        触发场景:常规中间轮(LLM 给 tool_call → 执行 → 还在 turn 内,准备下一轮 LLM)。
        行为与 v1 _iter_phase_finalize:1582-1601 一致,只迁移位置,行为不破坏。

        注意:assistant_with_tools 的持久化不在 SessionPersistHandler 职责内,
        由 _iter_phase_tools:1340 在 awaiting_permission 前写。本 handler 只关心
        tool_results 这一段,所以 D4.3 不验证 add_assistant_with_tools。
        """
        # stage_out 模拟中间轮(LLM 给了 1 个 tool_call)
        tc = MagicMock()
        tc.tool_use_id = "toolu_xyz_001"
        tc.tool_name = "Bash"
        tc.tool_input = {"command": "ls"}
        stage_out = MagicMock()
        stage_out.tool_calls = [tc]  # intermediate,有 tool_calls
        stage_out.full_text = "我来执行命令"
        stage_out.stop_reason = "tool_use"

        # _pending_tool_results 累积
        pending = [
            ("toolu_xyz_001", "file1\nfile2\n"),
            ("toolu_xyz_002", "other output"),  # 残余(模拟 stale state)
        ]
        agent = _make_fake_agent(pending=list(pending))
        handler = SessionPersistHandler(agent)
        ctx = _make_fake_turn_ctx(stage_out=stage_out)

        result = handler.handle(ctx)

        # 调一次 add_tool_results,带所有 pending(无 filter 行为,本设计只 flush)
        agent._session_manager.add_tool_results.assert_called_once()
        results_arg = agent._session_manager.add_tool_results.call_args[0][0]
        assert len(results_arg) == 2
        tool_use_ids = {r["tool_use_id"] for r in results_arg}
        assert tool_use_ids == {"toolu_xyz_001", "toolu_xyz_002"}
        # pending 清空
        assert agent._pending_tool_results == []
        # 不 stop_chain,让 AuditLog / MemoryBridgeExtract 后续跑
        assert result.stop_chain is False

    def test_d4_4_no_session_manager_no_op(self):
        """D4.4:agent._session_manager is None → 静默 no-op,不抛异常。

        触发场景:agent 没启用 session 管理(纯 test fixture / 短期诊断场景)。
        handler 必须 defensive,不能 crash。
        """
        agent = _make_fake_agent(pending=[("toolu_x", "out")], has_session_manager=False)
        handler = SessionPersistHandler(agent)
        ctx = _make_fake_turn_ctx(stage_out=None)

        # 不应抛
        result = handler.handle(ctx)

        assert isinstance(result, HandlerResult)
        assert result.stop_chain is False
        # pending 没被清(因为分支在 has_session_manager check 后)
        # 这是 by-design:没有 session_manager 就啥都不做
        assert agent._pending_tool_results == [("toolu_x", "out")]

    def test_d4_5_add_tool_results_exception_does_not_crash_handler(self):
        """D4.5 防御性:session_manager.add_tool_results 抛异常时,handler 仍返回、
        pending 仍清空(否则下次又会 reuse stale 状态)。
        """
        from agent_core import turn_chain as _tc

        agent = _make_fake_agent(pending=[("toolu_y", "data")])
        agent._session_manager.add_tool_results.side_effect = RuntimeError("disk full")

        handler = SessionPersistHandler(agent)
        ctx = _make_fake_turn_ctx(stage_out=None)

        # 不应 crash
        result = handler.handle(ctx)

        # finally 块保证 pending 清空
        assert agent._pending_tool_results == []
        assert isinstance(result, HandlerResult)
        # 异常被 warning logger 记录(可通过 caplog 进一步验证)


# ────────────────────────────────────────────────────────────────────
# D6-6:SessionPersistHandler 完整接管 / DELEGATE / v1 streaming 三层测试
# ────────────────────────────────────────────────────────────────────
# 这些测试覆盖 D6-3 docstring 文档化的边界:
#   - 完整接管(D6-3 取舍 A):SessionPersistHandler 只管 tool_results,其他 entity 走 v1 streaming
#   - DELEGATE 模式(D6-4):SessionPersistHandler 变 no-op,所有 entity 走 v1 streaming
#   - v1 streaming 写盘:已实现的 7 个 add_assistant_* 调用点不被 SessionPersistHandler 触碰
#
# 设计参考:docs/agent-state-machine-and-chain-of-responsibility-design.md §10 / §11.1


class TestSessionPersistBoundary:
    """D6-6:SessionPersistHandler 边界 — 哪些 entity 它管、哪些它不管。"""

    def test_d6_6_1_only_tool_results_in_scope(self):
        """D6-6.1:SessionPersistHandler 只 flush _pending_tool_results,
        不应触碰 assistant_with_tools / assistant_message 等其他 entity。

        验证:设 agent 有 _pending_tool_results(模拟 tool 执行完)+ 同时设一个
        'assistant 已被 v1 streaming 路径写盘'的标记(session_manager 调用计数),
        handler 只触发 add_tool_results,不会调 add_assistant_message 或
        add_assistant_with_tools(否则会重复写)。
        """
        agent = _make_fake_agent(pending=[("tu_a", "out_a")])
        handler = SessionPersistHandler(agent)
        ctx = _make_fake_turn_ctx(stage_out=None)

        handler.handle(ctx)

        # 调过 add_tool_results
        agent._session_manager.add_tool_results.assert_called_once()
        # 没调 add_assistant_message / add_assistant_with_tools
        # (这些方法根本不在 MagicMock 上注册,所以如果被调会 AttributeError)
        assert not hasattr(agent._session_manager.add_assistant_message, "call_args") or \
               agent._session_manager.add_assistant_message.call_args is None, (
            "D6-3 边界:SessionPersistHandler 不该管 assistant 写入"
        )

    def test_d6_6_2_delegate_mode_via_factory_noops(self):
        """D6-6.2:DELEGATE 模式下,build_default_output_chain 产出的 chain 里的
        SessionPersistHandler 调 handle() 是 no-op,不调 session_manager 任何方法。

        这验证 D6-4 的 SessionPersistMode toggle 真的把 handle() 切成了 no-op,
        没有'表面禁用、实际还跑'的伪 toggle bug。
        """
        from agent_core.builder import build_default_output_chain, SessionPersistMode

        agent = _make_fake_agent(pending=[("tu_b", "out_b")])
        chain = build_default_output_chain(agent, session_persist_mode=SessionPersistMode.DELEGATE)

        # 找 session_persist handler 实例
        session_persist = next(h for h in chain if h.name == "session_persist")
        ctx = _make_fake_turn_ctx(stage_out=None)

        result = session_persist.handle(ctx)

        # DELEGATE 模式:add_tool_results 没被调
        agent._session_manager.add_tool_results.assert_not_called()
        # pending 不清(因为不进 try/finally)
        assert agent._pending_tool_results == [("tu_b", "out_b")]
        # 返回标准 HandlerResult
        assert isinstance(result, HandlerResult)
        assert result.stop_chain is False

    def test_d6_6_3_normal_mode_writes_pending_pending_v1_assistant_calls_intact(self):
        """D6-6.3:NORMAL 模式下,SessionPersistHandler 写 tool_results,但
        assistant_with_tools / assistant_message 这两类 entity 仍是 v1 streaming
        路径负责(不归 SessionPersistHandler 管)。

        验证:模拟 v1 streaming 已经写过一次 assistant_with_tools(add_assistant_with_tools
        已被调 1 次);handler 跑完后,add_assistant_with_tools 调用计数仍为 1(没变成 2)。
        """
        agent = _make_fake_agent(pending=[("tu_c", "out_c")])
        # 模拟 v1 streaming 路径已写 assistant_with_tools
        agent._session_manager.add_assistant_with_tools(
            text="pre-yield", tool_calls=[{"id": "tu_c", "name": "Bash", "input": {}}]
        )
        baseline_call_count = agent._session_manager.add_assistant_with_tools.call_count
        assert baseline_call_count == 1

        handler = SessionPersistHandler(agent)
        ctx = _make_fake_turn_ctx(stage_out=None)

        handler.handle(ctx)

        # tool_results 写了一次
        agent._session_manager.add_tool_results.assert_called_once()
        # add_assistant_with_tools 仍是 1 次(没被 SessionPersistHandler 重复调)
        assert agent._session_manager.add_assistant_with_tools.call_count == 1, (
            "D6-3 边界:v1 streaming 已写的 assistant entity 不该被 SessionPersistHandler 重写"
        )
