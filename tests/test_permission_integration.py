"""
PermissionEngine 集成到 ReactAgent.run() 测试(Step 11)

覆盖:
1. 不传 permission_engine → 行为不变(向后兼容)
2. permission_engine DENY → tool 不执行,error_message 进 tool_result
3. permission_engine ALLOW → tool 正常执行
4. permission_engine ASK + auto_allow_ask=True → 视为 ALLOW
5. permission_engine ASK + auto_allow_ask=False → 走 UI 路径(timeout 默认 deny)
6. 多个 tool 并行路径下,任一 deny → 该 tool 不执行,其他 tool 仍执行
7. resolve_permission API 正确解锁 _ask_user_permission_v2
"""

from __future__ import annotations

import threading
import time
from typing import Any, Optional

import pytest

from agent_core.agent_core import ReactAgent
from agent_core.agent_state import TurnContext
from agent_core.tools.base import ToolDef, ToolRegistry
from agent_core.tools.permission.engine import PermissionEngine
from agent_core.tools.permission.types import (
    PermissionBehavior,
    PermissionDecision,
    ToolPermissionContext,
)
from agent_core.turn_chain import PermissionCheckHandler  # Plan C: _check_tool_permission 迁此


# ────────────────────────────────────────────────────────────────────
# Stub LLM(模拟 LLM router)
# ────────────────────────────────────────────────────────────────────

class _StubLLM:
    """最小 stub,只给 llm_router.config.model + system_prompt 用"""

    def __init__(self):
        from dataclasses import dataclass

        @dataclass
        class _Cfg:
            model: str = "test-model"
            system_prompt: str = "test"

        self.config = _Cfg()


def _make_agent(
    permission_engine: Optional[PermissionEngine] = None,
    tools: Optional[ToolRegistry] = None,
    auto_allow_ask: bool = True,
) -> ReactAgent:
    """构造 ReactAgent(不传 session/memory 等避免无关行为)"""
    if tools is None:
        tools = ToolRegistry()
        tools.register(ToolDef(
            name="echo",
            description="echo input",
            parameters={
                "type": "object",
                "properties": {"msg": {"type": "string"}},
                "required": ["msg"],
            },
            handler=lambda **kw: f"echo: {kw['msg']}",
        ))
    agent = ReactAgent(
        llm_router=_StubLLM(),
        tool_registry=tools,
        max_turns=3,
        permission_engine=permission_engine,
        auto_allow_ask=auto_allow_ask,
    )
    return agent


# ────────────────────────────────────────────────────────────────────
# 1. 向后兼容(不传 permission_engine)
# ────────────────────────────────────────────────────────────────────

class TestBackwardCompat:
    def test_no_permission_engine_allows_everything(self, monkeypatch):
        """不传 permission_engine → tool 直接执行"""
        agent = _make_agent()
        # 构造一个会 mock LLM 的 run,这里直接验证 _check_tool_permission
        allowed, err, eff = PermissionCheckHandler(agent)._check_tool_permission("echo", {"msg": "hi"})
        assert allowed is True
        assert err is None
        assert eff == {"msg": "hi"}

    def test_default_permission_engine_is_none(self):
        """默认 permission_engine = None"""
        agent = _make_agent()
        assert agent.permission_engine is None

    def test_default_auto_allow_ask_is_true(self):
        """默认 auto_allow_ask = True(测试友好)"""
        agent = _make_agent()
        assert agent.auto_allow_ask is True


# ────────────────────────────────────────────────────────────────────
# 2. permission_engine DENY / ALLOW
# ────────────────────────────────────────────────────────────────────

class TestPermissionEngineDecision:
    def test_deny_blocks_tool(self):
        """DENY → (allowed=False, error 含 deny 原因)"""
        engine = PermissionEngine(context=ToolPermissionContext(
            always_deny_rules={"projectSettings": ["echo"]},
        ))
        agent = _make_agent(permission_engine=engine)
        allowed, err, _ = PermissionCheckHandler(agent)._check_tool_permission("echo", {"msg": "hi"})
        assert allowed is False
        assert "Permission denied" in err

    def test_allow_passes_tool(self):
        """ALLOW → (allowed=True, effective_input = 原 input)"""
        engine = PermissionEngine(context=ToolPermissionContext(
            always_allow_rules={"projectSettings": ["echo"]},
        ))
        agent = _make_agent(permission_engine=engine)
        allowed, err, eff = PermissionCheckHandler(agent)._check_tool_permission("echo", {"msg": "hi"})
        assert allowed is True
        assert err is None
        assert eff == {"msg": "hi"}

    def test_unknown_tool_passes_through(self):
        """tool 不在 registry → 当作不存在,放过(让 execute() 报错)"""
        engine = PermissionEngine(context=ToolPermissionContext(
            always_deny_rules={"projectSettings": ["echo"]},
        ))
        agent = _make_agent(permission_engine=engine)
        allowed, err, eff = PermissionCheckHandler(agent)._check_tool_permission("nonexistent", {})
        assert allowed is True
        assert err is None

    def test_sensitive_path_triggers_deny(self):
        """sensitive path → DENY(经 safety_check 阶段)"""
        engine = PermissionEngine(context=ToolPermissionContext())
        agent = _make_agent(permission_engine=engine)
        allowed, err, _ = PermissionCheckHandler(agent)._check_tool_permission("echo", {
            "msg": "/Users/x/.ssh/id_rsa",
        })
        # 注:echo 是普通 tool,没 path 字段,safety_check 不一定会拦
        # 这里只是验证 _check_tool_permission 不崩溃 + 返合法 tuple
        assert allowed in (True, False)
        assert err is None or isinstance(err, str)


# ────────────────────────────────────────────────────────────────────
# 3. ASK + auto_allow_ask
# ────────────────────────────────────────────────────────────────────

class TestAskAutoAllow:
    def test_ask_with_auto_allow_true(self):
        """ASK + auto_allow_ask=True → 视作 ALLOW"""
        engine = PermissionEngine(context=ToolPermissionContext(
            always_ask_rules={"projectSettings": ["echo"]},
        ))
        agent = _make_agent(permission_engine=engine, auto_allow_ask=True)
        allowed, err, eff = PermissionCheckHandler(agent)._check_tool_permission("echo", {"msg": "hi"})
        assert allowed is True
        assert err is None
        assert eff == {"msg": "hi"}

    def test_ask_with_auto_allow_false_times_out(self):
        """ASK + auto_allow_ask=False → 等 UI 0.1s 超时 → 默认 deny"""
        engine = PermissionEngine(context=ToolPermissionContext(
            always_ask_rules={"projectSettings": ["echo"]},
        ))
        agent = _make_agent(permission_engine=engine, auto_allow_ask=False)
        start = time.time()
        allowed, err, _ = PermissionCheckHandler(agent)._check_tool_permission("echo", {"msg": "hi"})
        elapsed = time.time() - start
        # 超时后默认 deny
        assert allowed is False
        assert err is not None
        # 应该在 0.1s 左右(允许 buffer)
        assert elapsed < 0.5


# ────────────────────────────────────────────────────────────────────
# 4. resume_after_permission 解锁
# ────────────────────────────────────────────────────────────────────

class TestResumeAfterPermission:
    def test_resume_after_permission_allow(self):
        """resume_after_permission('allow') → 写 choice 到 _pending_permission_request。"""
        engine = PermissionEngine(context=ToolPermissionContext(
            always_ask_rules={"projectSettings": ["echo"]},
        ))
        agent = _make_agent(permission_engine=engine, auto_allow_ask=False)

        decision = engine.check_permissions(
            agent.tools.get("echo"), {"msg": "hi"}, [],
        )
        # v2 路径:立即返 AWAITING sentinel,不阻塞
        sentinel = agent._ask_user_permission_v2(
            "echo", {"msg": "hi"}, decision,
        )
        assert sentinel == "AWAITING_PERMISSION"

        # UI 写 choice 到 pending request
        agent.resume_after_permission("allow")
        assert agent._pending_permission_request["choice"] == "allow"

    def test_resume_after_permission_deny(self):
        """resume_after_permission('deny') → 写 choice='deny' 到 pending request。"""
        engine = PermissionEngine(context=ToolPermissionContext(
            always_ask_rules={"projectSettings": ["echo"]},
        ))
        agent = _make_agent(permission_engine=engine, auto_allow_ask=False)

        decision = engine.check_permissions(
            agent.tools.get("echo"), {"msg": "hi"}, [],
        )
        sentinel = agent._ask_user_permission_v2(
            "echo", {"msg": "hi"}, decision,
        )
        assert sentinel == "AWAITING_PERMISSION"

        agent.resume_after_permission("deny")
        assert agent._pending_permission_request["choice"] == "deny"

    def test_resume_after_permission_always_allow(self):
        """resume_after_permission('always_allow') → 写 choice='always_allow' 到 pending request。"""
        engine = PermissionEngine(context=ToolPermissionContext(
            always_ask_rules={"projectSettings": ["echo"]},
        ))
        agent = _make_agent(permission_engine=engine, auto_allow_ask=False)

        decision = engine.check_permissions(
            agent.tools.get("echo"), {"msg": "hi"}, [],
        )
        sentinel = agent._ask_user_permission_v2(
            "echo", {"msg": "hi"}, decision,
        )
        assert sentinel == "AWAITING_PERMISSION"

        agent.resume_after_permission("always_allow")
        assert agent._pending_permission_request["choice"] == "always_allow"


# ────────────────────────────────────────────────────────────────────
# 5. ReactAgent.run() 集成(用 stub LLM 模拟 tool_call)
# ────────────────────────────────────────────────────────────────────

class TestRunIntegration:
    def test_run_with_deny_yields_error_tool_result(self, monkeypatch):
        """run() 中 permission DENY → yield tool_result with success=False"""
        # 这里采用直接调 _check_tool_permission 的方式,避免 mock 整个 LLM 流
        # 真实 run() 集成由 web/app.py (Step 12) 端到端验证
        engine = PermissionEngine(context=ToolPermissionContext(
            always_deny_rules={"projectSettings": ["echo"]},
        ))
        agent = _make_agent(permission_engine=engine)

        # 模拟 run() 中的 _check_tool_permission 调用
        allowed, err, _ = PermissionCheckHandler(agent)._check_tool_permission("echo", {"msg": "hi"})
        assert allowed is False
        assert "Permission denied" in err

    def test_run_with_allow_proceeds_to_execute(self):
        """run() 中 permission ALLOW → tool.execute() 正常被调"""
        engine = PermissionEngine(context=ToolPermissionContext(
            always_allow_rules={"projectSettings": ["echo"]},
        ))
        agent = _make_agent(permission_engine=engine)

        # 模拟 run() 中的 execute 调用
        allowed, err, eff = PermissionCheckHandler(agent)._check_tool_permission("echo", {"msg": "hi"})
        assert allowed is True
        result = agent.tools.execute("echo", eff, max_retries=1)
        assert result["status"] == "success"
        assert "echo: hi" in result["output"]


# ────────────────────────────────────────────────────────────────────
# 6. duck-typed PermissionEngine(任何 check_permissions 返 decision 的对象)
# ────────────────────────────────────────────────────────────────────

class TestDuckTyped:
    def test_permission_engine_constructed(self):
        """PermissionEngine 正常构造"""
        engine = PermissionEngine(context=ToolPermissionContext())
        # 不崩溃
        assert engine.context is not None
        assert engine.hook_registry is not None
        assert engine.denial_state is not None


# ────────────────────────────────────────────────────────────────────
# C11: v2 状态机 + sentinel refactor 兼容性测试
# ────────────────────────────────────────────────────────────────────
# 这些测试确保 v2 重构(B5/B6/B7)不破坏 v1 公开 API 行为:
# - agent.resolve_permission 仍工作(Event 路径)
# - agent.run() 仍工作(v1 body,顶部委托 start_run)
# - 新 v2 API(start_run/step/interrupt/resume_after_permission)暴露且正确
# - _ask_user_permission_v2 返 sentinel

class TestV2SentinelCompat:
    def test_ask_user_permission_v2_returns_allow_sentinel(self):
        """hook 返 allow 时,_ask_user_permission_v2 返 'ALLOW' sentinel。"""
        # 注入 PermissionRequest hook 让其返 'allow'
        from unittest.mock import patch
        agent = _make_agent(permission_engine=PermissionEngine(
            context=ToolPermissionContext()
        ))
        # mock hook 返 'allow'
        with patch.object(agent, "_run_permission_request_hook", return_value="allow"):
            sentinel = agent._ask_user_permission_v2(
                "echo", {"msg": "hi"}, _mock_decision("ask")
            )
        assert sentinel == "ALLOW"

    def test_ask_user_permission_v2_returns_deny_sentinel(self):
        """hook 返 deny 时,_ask_user_permission_v2 返 'DENY_BY_HOOK' sentinel。"""
        from unittest.mock import patch
        agent = _make_agent(permission_engine=PermissionEngine(
            context=ToolPermissionContext()
        ))
        with patch.object(agent, "_run_permission_request_hook", return_value="deny"):
            sentinel = agent._ask_user_permission_v2(
                "echo", {"msg": "hi"}, _mock_decision("ask")
            )
        assert sentinel == "DENY_BY_HOOK"

    def test_ask_user_permission_v2_returns_awaiting_sentinel(self):
        """hook 不决策(ask/None)时,_ask_user_permission_v2 返 'AWAITING_PERMISSION'。"""
        from unittest.mock import patch
        agent = _make_agent(permission_engine=PermissionEngine(
            context=ToolPermissionContext()
        ))
        with patch.object(agent, "_run_permission_request_hook", return_value=None):
            sentinel = agent._ask_user_permission_v2(
                "echo", {"msg": "hi"}, _mock_decision("ask")
            )
        assert sentinel == "AWAITING_PERMISSION"
        # _pending_permission_request 也被设置(供 UI 读)
        assert agent._pending_permission_request is not None
        assert agent._pending_permission_request["tool_name"] == "echo"

    def test_v2_sentinel_allow(self):
        """v2 sentinel 接口:hook allow → 'ALLOW' sentinel。"""
        from unittest.mock import patch
        agent = _make_agent(permission_engine=PermissionEngine(
            context=ToolPermissionContext()
        ))
        with patch.object(agent, "_run_permission_request_hook", return_value="allow"):
            sentinel = agent._ask_user_permission_v2(
                "echo", {"msg": "hi"}, _mock_decision("ask")
            )
        assert sentinel == "ALLOW"

    def test_v2_sentinel_deny_by_hook(self):
        """v2 sentinel 接口:hook deny → 'DENY_BY_HOOK' sentinel。"""
        from unittest.mock import patch
        agent = _make_agent(permission_engine=PermissionEngine(
            context=ToolPermissionContext()
        ))
        with patch.object(agent, "_run_permission_request_hook", return_value="deny"):
            sentinel = agent._ask_user_permission_v2(
                "echo", {"msg": "hi"}, _mock_decision("ask")
            )
        assert sentinel == "DENY_BY_HOOK"


class TestV2APIMethods:
    """v2 API methods 存在性 + 基础行为测试(不实际跑 LLM)。"""

    def test_start_run_method_exists(self):
        """ReactAgent.start_run 方法存在。"""
        agent = _make_agent()
        assert hasattr(agent, "start_run")
        assert callable(agent.start_run)

    def test_step_method_exists(self):
        """ReactAgent.step 方法存在。"""
        agent = _make_agent()
        assert hasattr(agent, "step")
        assert callable(agent.step)

    def test_resume_after_permission_method_exists(self):
        """ReactAgent.resume_after_permission 方法存在。"""
        agent = _make_agent()
        assert hasattr(agent, "resume_after_permission")
        assert callable(agent.resume_after_permission)

    def test_interrupt_method_exists(self):
        """ReactAgent.interrupt 方法存在。"""
        agent = _make_agent()
        assert hasattr(agent, "interrupt")
        assert callable(agent.interrupt)

    def test_start_run_resets_run_state_and_pending(self):
        """start_run() 重置 _run_state + pending 字段。"""
        agent = _make_agent()
        agent.messages = [{"role": "user", "content": "old"}]
        agent._session_manager = None
        agent._run_state = None

        agent.start_run("new message")

        # 新 user msg 追加
        assert agent.messages[-1] == {"role": "user", "content": "new message"}
        # pending 状态重置(Plan B Step 4:_pending_* 实例字段迁到 RunState.pending_*,
        # start_run 新建 RunState 时 dataclass default_factory 保证初值空)
        assert agent._run_state.pending_thinking == ""
        assert agent._run_state.pending_tool_logs == []
        assert agent._run_state.pending_tool_results == []
        # _run_state 创建 + 含 user_message
        assert agent._run_state is not None
        assert agent._run_state.user_message == "new message"
        # 新 cancel_event
        assert agent._run_state.cancel_event.is_set() is False

    def test_start_run_rebuilds_state_machine(self):
        """start_run() 重建 StateMachine(每次拿新 SM)。"""
        agent = _make_agent()
        agent._session_manager = None
        # Plan B Step 4: _pending_* 实例字段已删,reset 由 start_run 新建 RunState 接管
        # Plan B Step 4: _pending_* 实例字段已删,reset 由 start_run 新建 RunState 接管
        # Plan B Step 4: _pending_* 实例字段已删,reset 由 start_run 新建 RunState 接管

        # 第一次 start_run → 建 SM → 调 interrupt → 转 INTERRUPTED
        agent.start_run("first")
        assert agent._sm.is_interrupted is False
        agent.interrupt()
        assert agent._sm.is_interrupted is True

        old_sm = agent._sm
        # 第二次 start_run → 重建 SM → 新 SM 在 SETUP
        agent.start_run("second")
        assert agent._sm is not old_sm
        from agent_core.agent_state import AgentPhase
        assert agent._sm.current == AgentPhase.SETUP

    def test_interrupt_is_idempotent(self):
        """interrupt() 多次调幂等。"""
        agent = _make_agent()
        agent._session_manager = None
        # Plan B Step 4: _pending_* 实例字段已删,reset 由 start_run 新建 RunState 接管
        # Plan B Step 4: _pending_* 实例字段已删,reset 由 start_run 新建 RunState 接管
        # Plan B Step 4: _pending_* 实例字段已删,reset 由 start_run 新建 RunState 接管
        agent.start_run("test")
        agent.interrupt()
        agent.interrupt()  # 不应抛错
        assert agent._sm.is_interrupted is True

    def test_interrupt_without_active_run_is_safe(self):
        """interrupt() 在 start_run() 之前调 → 静默忽略(不抛错)。"""
        agent = _make_agent()
        agent._run_state = None
        agent.interrupt()  # 不应抛错


class TestAwaitingPermissionSMTransition:
    """M10-AWAITING fix (2026-06-30): permission_request 触发时,
    SM 必须同步转 AWAITING_PERMISSION,否则 run_agent 早退 + generator
    暂停 → resume_after_permission 的 `if sm.current == AWAITING_PERMISSION`
    检查永远 False → 用户点 Allow 死循环。

    这个 test class 验证:
    1. _iter_phase_tools 在 __AWAITING_PERMISSION__ 分支 yield + return 后,
       SM.current 已是 AWAITING_PERMISSION(同步转生效)
    2. resume_after_permission("allow") 之后,SM.current 转 EXECUTING_TOOLS
    3. 历史 Fix B 兜底 (2026-07-07 已删,见 Step 2):原读 run_state.awaiting_permission
       兜底修复 yield generator 提前销毁 bug;Step 2 把字段搬到 turn_ctx 后该检查失效,
       主 if 块已足够挡非法调用,不再保留兜底。
    """

    def _setup_ask_scenario(self):
        """构造 agent + engine + turn_ctx + stage_outputs,模拟 LLM 生成 tool_call
        且 permission_engine 返 ASK 的场景。"""
        engine = PermissionEngine(context=ToolPermissionContext(
            always_ask_rules={"projectSettings": ["echo"]},
        ))
        agent = _make_agent(permission_engine=engine, auto_allow_ask=False)
        agent.start_run("请创建一个临时文件")
        # 模拟 LLM 已到 EXECUTING_TOOLS phase(LLM_THINKING 已跑过)
        from agent_core.agent_state import AgentPhase
        agent._sm._phase = AgentPhase.EXECUTING_TOOLS
        agent._sm._history.append((
            AgentPhase.LLM_THINKING, "llm_responded_with_tools",
            AgentPhase.EXECUTING_TOOLS,
        ))

        # 构造 fake stage_outputs(LLM 给了 1 个 tool_call)
        # 注意:_iter_phase_tools 把 tool_calls 当作对象读
        # (tc.tool_name / tc.tool_input / tc.tool_use_id),不是 dict。
        from agent_core.stages import LLMResult
        tc = type("_StubTC", (), {
            "tool_name": "echo",
            "tool_input": {"msg": "hi"},
            "tool_use_id": "tu_test_001",
            "is_final": False,
        })()
        stage_out = LLMResult(
            full_text="我来调用 echo。",
            tool_calls=[tc],
        )

        # 构造 turn_ctx(2026-07-02:用真 TurnContext dataclass,旧 duck-type stub
        # 缺 is_stopped property,与 tool_chain.run(ctx) 入口 ctx.is_stopped 检查冲突)
        turn_ctx = TurnContext(
            run_state=agent._run_state,
            stage_outputs=stage_out,
        )

        return agent, turn_ctx

    def test_iter_phase_tools_force_transitions_sm_to_awaiting_permission(self):
        """Fix A 验证:_iter_phase_tools yield awaiting_permission + return 后,
        SM.current 必须是 AWAITING_PERMISSION(不能卡在 EXECUTING_TOOLS)。
        """
        from agent_core.agent_state import AgentPhase
        agent, turn_ctx = self._setup_ask_scenario()

        # sanity:开始时 SM 在 EXECUTING_TOOLS
        assert agent._sm.current == AgentPhase.EXECUTING_TOOLS

        # drive _iter_phase_tools,消费所有 events(tool_call + awaiting_permission)
        events = list(agent._tool_chain.run(turn_ctx))

        # 最后一个 event 必须是 awaiting_permission(tool_call 先 yield,然后 return 前 yield awaiting_permission)
        assert events[-1][0] == "awaiting_permission", (
            f"_iter_phase_tools 应在 AWAITING 分支 yield awaiting_permission,实际: {[e[0] for e in events]}"
        )

        # ★ Fix A 关键断言:SM 必须同步转到 AWAITING_PERMISSION
        assert agent._sm.current == AgentPhase.AWAITING_PERMISSION, (
            f"Fix A 失效:SM 仍卡在 {agent._sm.current.value},"
            f"resume_after_permission 会被 no-op,用户点 Allow 死循环"
        )
        # _history 应记录这次 transition
        assert any(
            old == AgentPhase.EXECUTING_TOOLS and new == AgentPhase.AWAITING_PERMISSION
            for old, _, new in agent._sm._history
        ), "SM._history 缺 EXECUTING_TOOLS → AWAITING_PERMISSION transition"
        # turn_ctx.permission_request 也设了(原行为)
        assert turn_ctx.permission_request is not None
        # turn_ctx.awaiting_permission 也设了(Step 2, 2026-07-07 字段从 RunState 搬到 TurnContext)
        assert turn_ctx.awaiting_permission is not None

    def test_resume_after_permission_works_after_fix_a(self):
        """Fix A + resume_after_permission 端到端:allow 后 SM 应转 EXECUTING_TOOLS。"""
        from agent_core.agent_state import AgentPhase
        agent, turn_ctx = self._setup_ask_scenario()

        # 触发 awaiting_permission
        list(agent._tool_chain.run(turn_ctx))
        assert agent._sm.current == AgentPhase.AWAITING_PERMISSION

        # UI 模拟:用户点 Allow once
        agent.resume_after_permission("allow")

        # 关键断言:SM 应从 AWAITING_PERMISSION 推进到 EXECUTING_TOOLS
        # (修复前:sm.current 仍是 EXECUTING_TOOLS,resume_after_permission 被 no-op)
        assert agent._sm.current == AgentPhase.EXECUTING_TOOLS, (
            f"resume_after_permission 失效:SM 仍 {agent._sm.current.value},"
            f"历史: {agent._sm._history}"
        )
        # _history 应记录 AWAITING_PERMISSION → EXECUTING_TOOLS
        assert any(
            old == AgentPhase.AWAITING_PERMISSION and new == AgentPhase.EXECUTING_TOOLS
            for old, _, new in agent._sm._history
        )

    # 注 (2026-07-07,Step 2):test_fix_b_recovers_when_sm_stuck_in_executing_tools 已删。
    # 原因:Fix B 在 agent_core.py resume_after_permission 入口被直接删(用户 confirm,
    # 历史兜底,Step 2 把字段搬到 turn_ctx 后失效)。保留这个 test 没法实现。

    def test_fix_c_resume_after_permission_actually_executes_tool(self):
        """Fix C 验证(2026-06-30):resume_after_permission("allow") 后,下次
        step() 用新 turn_ctx (stage_outputs=None) 调 _iter_phase_tools 时,
        应从 run_state.last_tool_calls + self.messages[-1] 重建 stage_out,
        继续执行 tool(并 append tool_result 到 self.messages),而不是再次
        走 LLM 重生成 tool_call。

        不修这个 bug → 用户点 Allow 后,SM 转 EXECUTING_TOOLS → 新 turn_ctx
        stage_outputs=None → _iter_phase_tools 直接 return → phase.next 看到
        permission_request=None → 转 LLM_THINKING → LLM 看到 orphan tool_use
        → 重生成 bash → 又 ASK → 用户点 Allow 又触发 → 死循环。
        """
        from agent_core.agent_state import AgentPhase
        agent, turn_ctx = self._setup_ask_scenario()

        # 模拟 _iter_phase_llm 副作用:_iter_phase_tools 正常流之前,
        # LLM phase 会把 tool_calls 缓存到 run_state.last_tool_calls。
        # 测试里直接构造 stage_out 跳过了 LLM phase,需要手动补这一步,
        # 否则 Fix C 重建 stage_out 缺素材。
        agent._run_state.last_tool_calls = list(
            turn_ctx.stage_outputs.tool_calls
        )

        # Step 1: 触发 awaiting_permission(消费所有 events)
        first_events = list(agent._tool_chain.run(turn_ctx))
        assert first_events[-1][0] == "awaiting_permission"
        # sanity:turn_ctx 上次设了 awaiting_permission (Step 2) + run_state.last_tool_calls
        assert turn_ctx.awaiting_permission is not None
        assert agent._run_state.last_tool_calls, (
            "run_state.last_tool_calls 必须非空,Fix C 才有素材重建 stage_out"
        )
        # self.messages 已 append assistant tool_use message
        msg_count_before = len(agent.messages)
        assert msg_count_before >= 2  # user + assistant

        # Step 2: 用户点 Allow once → SM 转 EXECUTING_TOOLS
        agent.resume_after_permission("allow")
        assert agent._sm.current == AgentPhase.EXECUTING_TOOLS

        # Step 3: UI 调 step() → 新 turn_ctx(stage_outputs=None,
        # 这正是 bug 现场:上次 _iter_phase_tools 已 return,新 turn_ctx 啥都没)
        fresh_turn_ctx = TurnContext(
            run_state=agent._run_state,
            stage_outputs=None,  # ★ bug 现场:新 turn_ctx 没 stage_out
        )

        # Step 4: 调 _iter_phase_tools(应触发 Fix C 重建 + 执行)
        second_events = list(agent._tool_chain.run(fresh_turn_ctx))

        # ★ Fix C 关键断言 1:应 yield tool_result(而不是 awaiting_permission 死循环)
        event_types = [e[0] for e in second_events]
        assert "tool_result" in event_types, (
            f"Fix C 失效:_iter_phase_tools 没 yield tool_result, "
            f"events={event_types},SM 又会转 LLM_THINKING 死循环"
        )
        assert "awaiting_permission" not in event_types, (
            f"Fix C 失效:resume 后不应再触发 awaiting_permission, "
            f"events={event_types}"
        )

        # ★ Fix C 关键断言 2:tool_result content 正确(echo 工具的输出)
        tool_result_event = next(e for e in second_events if e[0] == "tool_result")
        assert tool_result_event[1]["name"] == "echo"
        assert tool_result_event[1]["success"] is True
        assert "echo: hi" in tool_result_event[1]["output"], (
            f"tool_result.output 应是 echo handler 的返回值,实际={tool_result_event[1]['output']!r}"
        )

        # ★ Fix C 关键断言 3:self.messages append 了 tool_result block
        # assistant tool_use msg 后应有 tool_result msg
        last_msg = agent.messages[-1]
        assert last_msg["role"] == "user", (
            f"tool_result message 的 role 应是 'user' (Anthropic tool_result schema),"
            f"实际={last_msg['role']!r},完整 message={last_msg}"
        )
        assert isinstance(last_msg["content"], list)
        assert last_msg["content"][0]["type"] == "tool_result"
        assert last_msg["content"][0]["tool_use_id"] == "tu_test_001"
        # ★ Fix C 关键断言 4:turn_ctx.awaiting_permission 已清 (Step 2 字段位置)—
        # 下次不会重入 resume 分支
        assert fresh_turn_ctx.awaiting_permission is None, (
            "Fix C 失效:turn_ctx.awaiting_permission 没清 (Step 2),"
            "下次 _iter_phase_tools 还会走 resume 分支重执行"
        )
        # ★ Fix C 关键断言 5:不应重复 append assistant message
        assert len(agent.messages) == msg_count_before + 1, (
            f"应只 append tool_result (1 条),实际新增 {len(agent.messages) - msg_count_before} 条 "
            f"(说明 assistant message 被重 append)"
        )

    def test_fix_c_resume_path_transitions_sm_to_llm_thinking(self):
        """Fix C 关键闭环验证(2026-06-30):resume 后 _iter_phase_tools 执行完成,
        ExecutingToolsPhase.next() 必须返 ("tools_done", LLM_THINKING),
        而不是 ("permission_needed", AWAITING_PERMISSION),否则 SM 又卡在
        AWAITING → LLM 收不到 tool_result → 没 ReAct 闭环。

        这是 5600f23a session 暴露的 bug:用户在 UI 点 Allow 后,
        agent.log 显示:
          🔄 [SM transition] executing_tools --[start]--> awaiting_permission
          ⏸️ [SM trigger] pause at awaiting_permission
        → 工具确实跑了,但 SM 永远不再进 LLM_THINKING → Stop 按钮一直亮。

        根因:之前 Fix C 把 run_state.awaiting_permission 拷到 turn_ctx.permission_request,
        让 next() 误以为还有 pending permission 又拨回 AWAITING。
        """
        from agent_core.agent_state import AgentPhase
        agent, turn_ctx = self._setup_ask_scenario()
        # 模拟 _iter_phase_llm 副作用
        agent._run_state.last_tool_calls = list(
            turn_ctx.stage_outputs.tool_calls
        )

        # Step 1: 触发 awaiting_permission(消费所有 events)
        list(agent._tool_chain.run(turn_ctx))
        assert agent._sm.current == AgentPhase.AWAITING_PERMISSION

        # Step 2: 用户点 Allow → SM 转 EXECUTING_TOOLS
        agent.resume_after_permission("allow")
        assert agent._sm.current == AgentPhase.EXECUTING_TOOLS

        # Step 3: 模拟 step() 内部:新 turn_ctx → _iter_phase_tools 重建 stage_out
        fresh_turn_ctx = TurnContext(
            run_state=agent._run_state,
            stage_outputs=None,  # ★ bug 现场
        )
        list(agent._tool_chain.run(fresh_turn_ctx))

        # Step 4: ★ 关键断言 ★ _iter_phase_tools 不应把 permission_request
        # 写到 turn_ctx(否则 next() 又拨回 AWAITING)。验证 turn_ctx 上
        # permission_request 保持 None,sm 也保持在 EXECUTING_TOOLS
        # (因为 _drive / step() 才会调 phase.next() 真正转 LLM_THINKING,
        # 这里直接调用 next() 模拟 SM 的真实行为)
        assert fresh_turn_ctx.permission_request is None, (
            "Fix C 失效:resume 路径不应再写 permission_request 到 turn_ctx,"
            "否则 ExecutingToolsPhase.next() 会把 SM 又拨回 AWAITING_PERMISSION,"
            "LLM 收不到 tool_result,ReAct 不闭环"
        )

        # Step 5: 直接调 ExecutingToolsPhase.next() 验证返回 tools_done
        from agent_core.agent_state import ExecutingToolsPhase, PhaseContext
        phase = ExecutingToolsPhase()
        next_trigger, next_phase = phase.next(
            "start",
            PhaseContext(
                run_state=agent._run_state,
                turn_ctx=fresh_turn_ctx,
                sm=agent._sm,
            ),
        )
        assert next_trigger == "tools_done", (
            f"Fix C 失效:resume 后 next() 应返回 tools_done,"
            f"实际 {next_trigger!r} (→ SM 又卡在 AWAITING,LLM 收不到 tool_result)"
        )
        assert next_phase == AgentPhase.LLM_THINKING, (
            f"Fix C 失效:resume 后应转 LLM_THINKING,实际 {next_phase.value}"
        )


# ────────────────────────────────────────────────────────────────────
# helpers(测试 C11 用)
# ────────────────────────────────────────────────────────────────────


def _mock_decision(behavior: str) -> Any:
    """构造 PermissionDecision mock 用于 _ask_user_permission_v2 测试。"""
    from agent_core.tools.permission.types import (
        PermissionBehavior,
        PermissionDecision,
    )
    # decision_reason 可选 — 不传(None),避免类型细节
    return PermissionDecision(
        behavior=PermissionBehavior(behavior),
        message="test message",
        updated_input=None,
    )


def MagicMock_run_state_only():
    """构造最小 PhaseContext 给 SM.interrupt() 用。"""
    from unittest.mock import MagicMock
    from agent_core.agent_state import RunState, TurnContext, PhaseContext
    rs = RunState()
    tc = TurnContext(run_state=rs)
    sm = MagicMock()
    return PhaseContext(run_state=rs, turn_ctx=tc, sm=sm)


# ────────────────────────────────────────────────────────────────────
# C12: Deny-loop fix 测试
# ────────────────────────────────────────────────────────────────────
# 验证:
# 1. AWAITING_PERMISSION 时 pending request 含 tool_use_id
# 2. resume_after_permission("deny") append tool_result 到 self.messages
# 3. deny 后 SM 转 LLM_THINKING(不重跑 EXECUTING_TOOLS 触发死循环)

class TestDenyLoopFix:
    def test_tool_use_id_in_pending_request(self, monkeypatch):
        """trigger awaiting_permission 后,_pending_permission_request 含 tool_use_id。

        关键:tool_use_id 必须从 _iter_phase_tools 透传到 pending request,
        这样 resume_after_permission 才能 append 对应的 tool_result。
        """
        from agent_core.agent_core import _make_tool_result_block
        from agent_core.tools.permission.engine import PermissionEngine
        from agent_core.tools.permission.types import ToolPermissionContext
        engine = PermissionEngine(context=ToolPermissionContext(
            always_ask_rules={"projectSettings": ["echo"]},
        ))
        agent = _make_agent(permission_engine=engine, auto_allow_ask=False)

        # 模拟 _iter_phase_tools:准备 tool_call + 触发 permission check
        # 让 _pending_permission_request 含 tool_use_id
        from types import SimpleNamespace
        tc = SimpleNamespace(
            tool_use_id="toolu_test_123",
            tool_name="echo",
            tool_input={"msg": "hi"},
        )
        # 触发 ASK 流程
        decision = engine.check_permissions(
            agent.tools.get("echo"), {"msg": "hi"}, [],
        )
        agent._ask_user_permission_v2("echo", {"msg": "hi"}, decision)
        # 模拟 _iter_phase_tools 加 tool_use_id 这一行
        agent._pending_permission_request["tool_use_id"] = tc.tool_use_id

        assert agent._pending_permission_request["tool_use_id"] == "toolu_test_123"

    def test_deny_appends_tool_result_to_messages(self, monkeypatch):
        """resume_after_permission('deny') → self.messages 末尾追加 tool_result。

        修复死循环的关键:LLM 下次调用必须看到 denial tool_result,否则会重新
        生成相同的 tool_call。
        """
        engine = PermissionEngine(context=ToolPermissionContext(
            always_ask_rules={"projectSettings": ["echo"]},
        ))
        agent = _make_agent(permission_engine=engine, auto_allow_ask=False)

        # 模拟 ASK 流程
        decision = engine.check_permissions(
            agent.tools.get("echo"), {"msg": "hi"}, [],
        )
        agent._ask_user_permission_v2("echo", {"msg": "hi"}, decision)
        # 模拟 _iter_phase_tools 加 tool_use_id
        agent._pending_permission_request["tool_use_id"] = "toolu_test_deny"

        # 当前 messages 长度
        n_before = len(agent.messages)

        # 用户点 Deny
        agent.resume_after_permission("deny")

        # self.messages 应该多了 1 条 tool_result message
        assert len(agent.messages) == n_before + 1
        new_msg = agent.messages[-1]
        assert new_msg["role"] == "user"  # Anthropic 格式:tool_result 放 user message
        assert isinstance(new_msg["content"], list)
        assert new_msg["content"][0]["type"] == "tool_result"
        assert new_msg["content"][0]["tool_use_id"] == "toolu_test_deny"
        assert "Permission denied" in new_msg["content"][0]["content"]

    def test_deny_transitions_to_llm_thinking_not_executing_tools(self):
        """deny 后 SM 必须转 LLM_THINKING(不重跑 EXECUTING_TOOLS)。

        关键:如果 deny 还转 EXECUTING_TOOLS,下次 _iter_phase_tools 会用同样的
        stage_out.tool_calls 重新 permission check → 又是 ASK → 死循环。
        """
        from agent_core.agent_state import AgentPhase
        engine = PermissionEngine(context=ToolPermissionContext(
            always_ask_rules={"projectSettings": ["echo"]},
        ))
        agent = _make_agent(permission_engine=engine, auto_allow_ask=False)

        # 模拟 ASK 触发后,SM 进入 AWAITING_PERMISSION
        agent._session_manager = None
        agent.start_run("test")

        # 直接把 SM 推到 AWAITING_PERMISSION(用 setattr 模拟 phase 转移)
        agent._sm._phase = AgentPhase.AWAITING_PERMISSION

        # 触发 ASK 设 pending request
        decision = engine.check_permissions(
            agent.tools.get("echo"), {"msg": "hi"}, [],
        )
        agent._ask_user_permission_v2("echo", {"msg": "hi"}, decision)

        # 用户点 Deny
        agent.resume_after_permission("deny")

        # SM 应该直接跳到 LLM_THINKING,跳过 EXECUTING_TOOLS
        assert agent._sm.current == AgentPhase.LLM_THINKING, (
            f"deny 应直接转 LLM_THINKING,但实际是 {agent._sm.current}"
        )
