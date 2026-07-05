"""
SessionPersistHandler 真实现回归测试(2026-06-30 — orphan tool_result fix + 2026-07-01
扩到 final assistant text)。

覆盖(每个独立 case,D4 清单):
    D4.1: 空 _pending_tool_results → early return,no session call
    D4.2: stage_out final-answer(no tool_calls)+ pending populated → add_tool_results 调一次
          (Fix C 修复路径 — LLM 给最终回答、stale _pending 不丢)
    D4.3: stage_out intermediate(tool_calls truthy)+ pending populated → add_tool_results 调一次
          (常规中间轮 — 保持 v1 _iter_phase_finalize 行为)
    D4.4: agent._session_manager is None → no-op,不抛(扩展点模式兼容)

扩展覆盖(2026-07-01 final text 接管):
    FT.1: stage_out final-answer(no tool_calls)+ full_text → add_assistant_message 调一次,
          4 字段对齐 v1 _iter_phase_llm:2263(full_text + thinking + tool_logs + usage)
    FT.2: stage_out intermediate(tool_calls truthy)+ full_text → 不调 add_assistant_message
          (中间轮由 v1 _iter_phase_tools:1340 / 2475 写,本 handler 不接管)
    FT.3: stage_out final-answer + thinking / tool_logs / usage → 4 字段全部正确传入
    FT.4: final text 写入后清空 _run_state.pending_tool_logs,避免跨 turn double-write

设计参考:docs/agent-state-machine-and-chain-of-responsibility-design.md §4 / §10
        + docs/session-management-implementation-design.md §2.2 / §2.4
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


def _make_fake_agent(pending=None, has_session_manager=True, pending_tool_logs=None):
    """构造最小 fake agent:SessionPersistHandler 只需要 ._session_manager 和
    ._pending_tool_results 两个属性(handle() 用 getattr 防 attribute error)。

    2026-07-01 扩展:final text 写入需要从 agent._run_state.pending_tool_logs
    取 tool_logs,并在写入后清空。所以 fake agent 也提供 _run_state 字段。
    """
    agent = MagicMock()
    if has_session_manager:
        agent._session_manager = MagicMock()
    else:
        agent._session_manager = None
    agent._pending_tool_results = pending if pending is not None else []
    # _run_state 模拟 RunState,handler 会读 .pending_tool_logs 字段
    run_state = MagicMock()
    run_state.pending_tool_logs = pending_tool_logs if pending_tool_logs is not None else []
    agent._run_state = run_state
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


# ────────────────────────────────────────────────────────────────────
# FT (2026-07-01):SessionPersistHandler 扩到接 final assistant text
# ────────────────────────────────────────────────────────────────────
# 触发场景:v2 路径(via start_run + step)走 FINALIZING phase → output_chain
# SessionPersistHandler 检测 stage_out 有 full_text + 无 tool_calls →
# 调 session_manager.add_assistant_message 写 final text(对齐 v1 L2263 4 字段)。
#
# 修复目标:c212e367.jsonl 缺第 5 条 final answer entry 的根因
# (v2 路径不经过 v1 _iter_phase_llm:2263,SessionPersistHandler 必须接管)。
#
# 设计参考:docs/agent-state-machine-and-chain-of-responsibility-design.md §10
#        + docs/session-management-implementation-design.md §2.4


class TestSessionPersistFinalText:
    """FT:SessionPersistHandler 真实现扩到接 final assistant text(2026-07-01)。"""

    def test_ft_1_final_answer_writes_assistant_message_with_full_text(self):
        """FT.1 (★ 关键 fix):stage_out final-answer + full_text →
        add_assistant_message 调一次,full_text 走 content 位置参数。

        触发场景:LLM 给最终回答 → FINALIZING → output_chain SessionPersistHandler。
        修复前:c212e367.jsonl 复现 — 缺第 5 条 final assistant entry。
        修复后:handler 把 full_text 写入 session,UI 重渲染可看到 final text。
        """
        stage_out = MagicMock()
        stage_out.tool_calls = []  # final-answer,no tool_calls
        stage_out.full_text = "23*34 = 782.0"
        stage_out.stop_reason = "end_turn"
        # thinking_text / usage 也存在(handler 会读)
        stage_out.thinking_text = "用户计算 23*34,直接给答案"
        stage_out.usage = MagicMock()

        agent = _make_fake_agent(pending=[])  # 纯 final answer,无 pending
        handler = SessionPersistHandler(agent)
        ctx = _make_fake_turn_ctx(stage_out=stage_out)

        handler.handle(ctx)

        # ★ 关键断言:add_assistant_message 被调一次,full_text 走 content 参数
        agent._session_manager.add_assistant_message.assert_called_once()
        call_args = agent._session_manager.add_assistant_message.call_args
        # content 走第一个位置参数(对齐 v1 _iter_phase_llm:2263)
        assert call_args[0][0] == "23*34 = 782.0", (
            "final assistant text 应走 add_assistant_message 的 content 位置参数"
        )

    def test_ft_2_intermediate_turn_with_tool_calls_skips_assistant_message(self):
        """FT.2:stage_out intermediate(tool_calls truthy)+ full_text →
        不调 add_assistant_message。

        触发场景:中间轮(LLM 调 tool)。中间轮的 assistant_with_tools 由
        _iter_phase_tools:1340 / 2475 在 awaiting_permission 前实时写,本 handler
        不接管,避免 double-write。
        """
        tc = MagicMock()
        tc.tool_use_id = "toolu_int_001"
        stage_out = MagicMock()
        stage_out.tool_calls = [tc]  # intermediate,有 tool_calls
        stage_out.full_text = "我来执行"
        stage_out.stop_reason = "tool_use"

        agent = _make_fake_agent(pending=[("toolu_int_001", "out")])
        handler = SessionPersistHandler(agent)
        ctx = _make_fake_turn_ctx(stage_out=stage_out)

        handler.handle(ctx)

        # add_assistant_message 不该被调(中间轮由 v1 streaming 负责)
        agent._session_manager.add_assistant_message.assert_not_called()
        # tool_results 仍写(中间轮也要刷)
        agent._session_manager.add_tool_results.assert_called_once()

    def test_ft_3_final_answer_writes_thinking_tool_logs_usage(self):
        """FT.3:final answer 写入时,4 字段(full_text + thinking + tool_logs + usage)
        全部正确传入 add_assistant_message。

        对齐 v1 _iter_phase_llm:2263 的 4 字段签名:
            add_assistant_message(full_text, thinking=..., tool_logs=..., usage=...)
        """
        stage_out = MagicMock()
        stage_out.tool_calls = []
        stage_out.full_text = "answer text"
        stage_out.thinking_text = "thinking reasoning"
        stage_out.usage = {"input_tokens": 100, "output_tokens": 50}

        tool_logs_payload = [
            {"type": "action", "name": "Bash", "input": {"command": "ls"}},
            {"type": "result", "name": "Bash", "success": True},
        ]
        agent = _make_fake_agent(pending=[], pending_tool_logs=list(tool_logs_payload))
        handler = SessionPersistHandler(agent)
        ctx = _make_fake_turn_ctx(stage_out=stage_out)

        handler.handle(ctx)

        agent._session_manager.add_assistant_message.assert_called_once()
        call_args = agent._session_manager.add_assistant_message.call_args
        # content(full_text)走第一个位置参数
        assert call_args[0][0] == "answer text"
        # thinking / tool_logs / usage 走 kwargs(对齐 v1 _iter_phase_llm:2263)
        kwargs = call_args.kwargs
        assert kwargs.get("thinking") == "thinking reasoning", (
            "thinking 字段应对齐 L2263,走 thinking kwarg"
        )
        assert kwargs.get("tool_logs") == tool_logs_payload, (
            "tool_logs 字段应对齐 L2263,从 _run_state.pending_tool_logs 取"
        )
        assert kwargs.get("usage") == {"input_tokens": 100, "output_tokens": 50}

    def test_ft_4_pending_tool_logs_cleared_after_final_write(self):
        """FT.4:final text 写入后清空 _run_state.pending_tool_logs,
        避免下次 turn double-write。

        跟 D4 tool_results 的 finally 清空是同一个 invariant:落库即清。
        """
        stage_out = MagicMock()
        stage_out.tool_calls = []
        stage_out.full_text = "final answer"
        stage_out.thinking_text = ""
        stage_out.usage = None

        pending_logs = [
            {"type": "result", "name": "calc", "success": True, "output": "42"}
        ]
        agent = _make_fake_agent(pending=[], pending_tool_logs=list(pending_logs))
        handler = SessionPersistHandler(agent)
        ctx = _make_fake_turn_ctx(stage_out=stage_out)

        handler.handle(ctx)

        # 写入后 pending_tool_logs 应清空
        assert agent._run_state.pending_tool_logs == [], (
            "FT invariant:final text 落库后必须清空 pending_tool_logs,"
            "否则下次 turn 又写一次 double-write"
        )

    def test_ft_5_usage_dataclass_serialized_to_dict(self):
        """FT.5 (回归 2026-07-01):stage_out.usage 是 UsageStats dataclass 时,
        handler 必须 asdict() 转 dict 再传给 add_assistant_message。

        Bug 复现:session.storage.flush 报 "Object of type UsageStats is not
        JSON serializable" — 原 handler 直接传 UsageStats 对象,add_assistant_message
        的 **extra 把它当 dict 字段存 → flush 时 JSON dumps 失败。

        v1 _iter_phase_llm:2263 / 1039 / 1098 / 2131 / 2215 都用 asdict() 转,
        本 handler 必须对齐。
        """
        from dataclasses import dataclass

        @dataclass
        class FakeUsageStats:
            input_tokens: int = 100
            output_tokens: int = 50
            thinking_tokens: int = 0
            cached_tokens: int = 0

        usage_obj = FakeUsageStats(input_tokens=200, output_tokens=80)

        stage_out = MagicMock()
        stage_out.tool_calls = []
        stage_out.full_text = "done"
        stage_out.thinking_text = ""
        stage_out.usage = usage_obj  # dataclass 对象,不是 dict

        agent = _make_fake_agent(pending=[])
        handler = SessionPersistHandler(agent)
        ctx = _make_fake_turn_ctx(stage_out=stage_out)

        handler.handle(ctx)

        agent._session_manager.add_assistant_message.assert_called_once()
        kwargs = agent._session_manager.add_assistant_message.call_args.kwargs
        passed_usage = kwargs.get("usage")
        # 必须不是原 dataclass 对象(否则 flush 时 json.dumps 失败)
        assert not hasattr(passed_usage, "__dataclass_fields__"), (
            "FT.5 回归:UsageStats dataclass 必须 asdict() 转 dict 再传,"
            "否则 session.storage.flush 会报 'not JSON serializable'"
        )
        # 应是 dict,字段值保留
        assert isinstance(passed_usage, dict)
        assert passed_usage["input_tokens"] == 200
        assert passed_usage["output_tokens"] == 80

    def test_ft_6_tool_result_written_before_final_text(self):
        """FT.6 (回归 2026-07-01):tool_result 必须先于 final assistant text 落库。

        用户偏好 2026-07-01:JSONL 顺序应为 tool_use → tool_result → final,
        即 tool_result 是 "倒数第二行",final 是最后一行 — 对齐 Anthropic API
        协议 tool_use → tool_result → final_answer 的因果链。

        验证方式:用 manager 顶层 mock_calls 列表,断言 add_tool_results
        在 add_assistant_message 之前。
        """
        stage_out = MagicMock()
        stage_out.tool_calls = []  # final-answer
        stage_out.full_text = "答案是 782"
        stage_out.thinking_text = "thinking"
        stage_out.usage = None

        # 同时有 tool_result pending(模拟 calc tool 执行完 + LLM 给 final answer 的 FINALIZING 场景)
        pending = [("toolu_calc_001", "782.0")]
        agent = _make_fake_agent(pending=list(pending))
        handler = SessionPersistHandler(agent)
        ctx = _make_fake_turn_ctx(stage_out=stage_out)

        handler.handle(ctx)

        # 两个调用都应发生
        agent._session_manager.add_tool_results.assert_called_once()
        agent._session_manager.add_assistant_message.assert_called_once()

        # ★ 关键断言:add_tool_results 必须在 add_assistant_message 之前调
        # (用户偏好 2026-07-01:JSONL 顺序 tool_use → tool_result → final,
        # 对齐 Anthropic API 协议 tool_use → tool_result → final_answer 因果链)
        all_calls = agent._session_manager.mock_calls
        method_names = [c[0] for c in all_calls]
        first_tool_result_idx = next(
            i for i, n in enumerate(method_names) if "add_tool_results" in n
        )
        first_assistant_idx = next(
            i for i, n in enumerate(method_names) if "add_assistant_message" in n
        )
        assert first_tool_result_idx < first_assistant_idx, (
            "FT.6 用户偏好:tool_result 必须先于 final text 落库,"
            "JSONL 顺序 = tool_use → tool_result → final,"
            "tool_result 是倒数第二行,final 是最后一行"
        )
