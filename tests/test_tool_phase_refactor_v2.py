"""
Plan B Step 7 acceptance — 3 个 tool_chain handler 真实现的 runtime 验证(2026-07-02 引入)。

覆盖 plan §15 Step 7 acceptance 列出的 10 case:
- PermissionCheckHandler:三态分类(allow / ask / deny)+ batch ASK 收集 + Fix C resume
- ToolDispatchHandler:emit tool_call events(单/并行)+ resume skip
- ToolExecuteHandler:DENY pre-fill + ALLOW execute(单/并行)+ Fix C 清理
- _iter_phase_tools orchestrator:thin 委托,Generator 语义不变

测试策略:
- 构造 ReactAgent(走 ReactAgent.__init__,自动注入 self._tool_chain / self._sm / self._run_state)
- monkeypatch agent._check_tool_permission 控 permission 决策(allow/ask/deny)
- monkeypatch agent.tools.execute 控 execute 结果
- 直接构造 _LLMResult(tool_calls=...) 灌 ctx.stage_outputs
- 调 agent._tool_chain.run(ctx) → 收集 yielded events,断言 shape + 顺序

链顺序(`tests/test_builder_e2e.py::test_d8_1` 锁定):
    tool_chain == [permission_check, tool_dispatch, tool_execute, tool_pair_persist]

完整委托链:
    _iter_phase_tools(orchestrator) → self._tool_chain.run(ctx)
        → PermissionCheckHandler.handle(ctx)
        → ToolDispatchHandler.handle(ctx)
        → ToolExecuteHandler.handle(ctx)
        → ToolPairPersistHandler.handle(ctx)(本次不覆盖,Stage B 自有 test)
"""

from __future__ import annotations

import time
from types import SimpleNamespace
from typing import Any, List, Optional

import pytest

from agent_core.agent_core import ReactAgent
from agent_core.agent_state import RunState, TurnContext
from agent_core.tools.base import ToolDef, ToolRegistry
from agent_core.turn_chain import _LLMResult



def _get_perm_handler(agent):
    """从 agent._tool_chain 拿 PermissionCheckHandler 实例。

    Plan C (2026-07-02):_check_tool_permission 从 agent 迁到 handler,
    monkeypatch 目标改为 handler 实例(实例属性不绑 self,fn 签名不变)。
    """
    return next(h for h in agent._tool_chain if h.name == "permission_check")


# ────────────────────────────────────────────────────────────
# Fixtures
# ────────────────────────────────────────────────────────────


class _StubLLM:
    """最小 stub — 给 llm_router.config 用"""

    def __init__(self):
        from dataclasses import dataclass

        @dataclass
        class _Cfg:
            model: str = "test-model"
            system_prompt: str = "test"

        self.config = _Cfg()


def _make_tool_call(name: str, input_: dict, id_: Optional[str] = None) -> SimpleNamespace:
    """构造 LLM 产出的 ToolCall(SimpleNamespace duck-type 即可)。"""
    return SimpleNamespace(
        tool_name=name,
        tool_input=input_,
        tool_use_id=id_ or f"toolu_{name}_{input_}",
    )


def _make_agent(tools: Optional[ToolRegistry] = None) -> ReactAgent:
    """构造 ReactAgent(走 __init__,自动注入 _tool_chain / _sm / _run_state)。

    默认 register echo + deny_tool 两种 tool,测试用。
    """
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
        tools.register(ToolDef(
            name="deny_tool",
            description="deny test tool",
            parameters={"type": "object", "properties": {}, "required": []},
            handler=lambda **kw: "should not reach",
        ))
    agent = ReactAgent(
        llm_router=_StubLLM(),
        tool_registry=tools,
        max_turns=3,
    )
    # ReactAgent.__init__ 不主动建 _run_state(由 start_run() 触发),
    # 这里手工建一个空 RunState 让测试不依赖完整 start_run 流程。
    agent._run_state = RunState(user_message="<test>")
    return agent


def _make_ctx(agent: ReactAgent, tool_calls: Optional[list] = None,
              full_text: str = "", stage_out: Optional[_LLMResult] = None) -> TurnContext:
    """构造 TurnContext + 把 stage_outputs 灌 _LLMResult(模仿 LLM phase 写完的状态)。"""
    ctx = TurnContext(run_state=agent._run_state)
    if stage_out is not None:
        ctx.stage_outputs = stage_out
    elif tool_calls is not None:
        ctx.stage_outputs = _LLMResult(
            tool_calls=tool_calls,
            full_text=full_text,
        )
    return ctx


def _set_resume_state(agent: ReactAgent, ctx: TurnContext, last_tool_calls: list) -> None:
    """模拟 Fix C resume 状态:run_state 已经有 last_tool_calls,turn_ctx 已有 awaiting_permission(Step 2, 2026-07-07),
    _pending_permission_request["choice"] 已设(Step 2 修复 _is_resume 检测改用此信号)。

    Step 2 把 awaiting_permission 系列从 RunState 搬到 TurnContext(per-turn 生命周期),
    所以测试模拟 resume 状态时也得写到 turn_ctx 上,这样 PermissionCheckHandler 读得到。
    此外 Step 2 (turn_chain.py:1423-1431) _is_resume 检测改用 _pending_permission_request["choice"]
    (agent 级跨 turn 持久信号,user 已点 Allow/Deny 后 set),所以模拟也要补这个。
    """
    agent._run_state.last_tool_calls = list(last_tool_calls)
    ctx.awaiting_permission = {"tool_name": "stub", "tool_use_id": "stub"}
    # Step 2 修复 (2026-07-07):_is_resume 检测改用 _pending_permission_request.choice
    if agent._pending_permission_request is None:
        agent._pending_permission_request = {
            "tool_name": "stub", "tool_use_id": "stub", "choice": "allow",
        }
    else:
        agent._pending_permission_request["choice"] = "allow"


# ────────────────────────────────────────────────────────────
# PermissionCheckHandler — 三态分类 + batch ASK + resume
# ────────────────────────────────────────────────────────────


class TestPermissionCheck3Way:
    """1:3 tools 全 ALLOW → ctx.permission_decisions 3 个 allow,chain 继续"""

    def test_permission_check_3tools_all_allow(self, monkeypatch):
        agent = _make_agent()
        tcs = [
            _make_tool_call("echo", {"msg": "a"}, id_="id1"),
            _make_tool_call("echo", {"msg": "b"}, id_="id2"),
            _make_tool_call("echo", {"msg": "c"}, id_="id3"),
        ]
        ctx = _make_ctx(agent, tool_calls=tcs)

        # monkeypatch _check_tool_permission 全返 ALLOW
        monkeypatch.setattr(
            _get_perm_handler(agent), "_check_tool_permission",
            lambda name, input_: (True, None, input_),
        )
        # mock tools.execute — 只关心 call 次数
        execute_calls: list = []
        monkeypatch.setattr(
            agent.tools, "execute",
            lambda name, input_, **kw: (execute_calls.append((name, input_))
                                          or {"status": "success", "output": f"ok:{input_['msg']}"}),
        )

        events = list(agent._tool_chain.run(ctx))

        # 3 个 tool_result event(每个 ALLOW 工具)
        tool_result_events = [e for e in events if e[0] == "tool_result"]
        assert len(tool_result_events) == 3
        # 1 个 tool_call event(并行)
        tool_call_events = [e for e in events if e[0] == "tool_call"]
        assert len(tool_call_events) == 1
        assert tool_call_events[0][1].get("parallel") is True
        assert set(tool_call_events[0][1].get("names", [])) == {"echo"}
        # execute 调 3 次
        assert len(execute_calls) == 3


class TestPermissionCheckBatchAsk:
    """2:batch 中 ≥1 ASK → 写 awaiting_permission_batch,SM 转 AWAITING,_StopChain 返回"""

    def test_permission_check_batch_with_ask_pauses_chain(self, monkeypatch):
        agent = _make_agent()
        tc_ask = _make_tool_call("echo", {"msg": "ask"}, id_="id_ask")
        ctx = _make_ctx(agent, tool_calls=[tc_ask])

        # monkeypatch 让 _check_tool_permission 返 ASK(perm_err marker)
        monkeypatch.setattr(
            _get_perm_handler(agent), "_check_tool_permission",
            lambda name, input_: (False, "__AWAITING_PERMISSION__", None),
        )
        # 避免 _ask_user_permission_v2 真的等用户
        monkeypatch.setattr(agent, "_pending_permission_request", {
            "tool_name": "echo", "tool_input": {"msg": "ask"},
            "reason": "needs approval", "message": "Allow?",
        })

        events = list(agent._tool_chain.run(ctx))

        # emit awaiting_permission event(单 dict back-compat)
        await_events = [e for e in events if e[0] == "awaiting_permission"]
        assert len(await_events) == 1
        assert await_events[0][1]["tool_use_id"] == "id_ask"
        # 写入 turn_ctx.awaiting_permission_batch(整 batch)— Step 2, 2026-07-07
        assert len(ctx.awaiting_permission_batch) == 1
        assert ctx.awaiting_permission_batch[0]["tool_use_id"] == "id_ask"
        # back-compat:awaiting_permission 单字段填 batch[0]
        assert ctx.awaiting_permission is ctx.awaiting_permission_batch[0]
        # SM 转 AWAITING_PERMISSION
        from agent_core.agent_state import AgentPhase
        assert agent._sm.current == AgentPhase.AWAITING_PERMISSION
        # chain stop:ToolDispatchHandler / ToolExecuteHandler 不应跑(无 tool_call event)
        tool_call_events = [e for e in events if e[0] == "tool_call"]
        assert tool_call_events == []


class TestPermissionCheckBatchDeny:
    """3:DENY path → outcome="deny", error=perm_err"""

    def test_permission_check_batch_with_deny(self, monkeypatch):
        agent = _make_agent()
        tc = _make_tool_call("echo", {"msg": "x"}, id_="id_deny")
        ctx = _make_ctx(agent, tool_calls=[tc])

        monkeypatch.setattr(
            _get_perm_handler(agent), "_check_tool_permission",
            lambda name, input_: (False, "Permission denied: scary tool", None),
        )

        events = list(agent._tool_chain.run(ctx))

        # emit 1 个 tool_result event(success=False)
        tool_result_events = [e for e in events if e[0] == "tool_result"]
        assert len(tool_result_events) == 1
        assert tool_result_events[0][1]["success"] is False
        assert "Permission denied" in tool_result_events[0][1]["output"]
        # 不应 emit awaiting_permission
        assert not any(e[0] == "awaiting_permission" for e in events)
        # turn_ctx.awaiting_permission_batch 应保持空 — Step 2, 2026-07-07
        assert ctx.awaiting_permission_batch == []


class TestPermissionCheckResume:
    """4:resume 路径 → 整 batch pre-ALLOW,跳 permission check,chain 不停"""

    def test_permission_check_resume_skips_check(self, monkeypatch):
        agent = _make_agent()
        tc = _make_tool_call("echo", {"msg": "resume"}, id_="id_resume")
        # 原始 messages 末条 = assistant tool_use(Fix C resume 依赖此恢复 full_text)
        agent.messages.append({
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": "id_resume", "name": "echo", "input": {"msg": "resume"}},
            ],
        })
        ctx = TurnContext(run_state=agent._run_state)  # stage_outputs=None 触发 resume 分支
        # 模拟 resume 状态:last_tool_calls 在 run_state,awaiting_permission 在 turn_ctx (Step 2)
        _set_resume_state(agent, ctx, [tc])

        # 关键断言:_check_tool_permission 根本不应该被调
        call_count = {"n": 0}

        def counting_check(name, input_):
            call_count["n"] += 1
            return (False, "should not be called", None)

        monkeypatch.setattr(_get_perm_handler(agent), "_check_tool_permission", counting_check)

        # mock execute
        execute_calls: list = []
        monkeypatch.setattr(
            agent.tools, "execute",
            lambda name, input_, **kw: (execute_calls.append((name, input_))
                                          or {"status": "success", "output": "resumed"}),
        )

        events = list(agent._tool_chain.run(ctx))

        # _check_tool_permission 没被调
        assert call_count["n"] == 0, "resume 路径不应重跑 permission check"
        # 1 个 tool_result(ALLOW pre-fill + tools.execute 同步)
        tool_result_events = [e for e in events if e[0] == "tool_result"]
        assert len(tool_result_events) == 1
        assert tool_result_events[0][1]["output"] == "resumed"
        # execute 调 1 次
        assert len(execute_calls) == 1


class TestPermissionCheckResumeNoRequest:
    """5:resume 路径不写 ctx.permission_request(否则 LLMThinkingPhase.next() 误判)"""

    def test_permission_check_resume_does_not_set_permission_request(self, monkeypatch):
        agent = _make_agent()
        tc = _make_tool_call("echo", {"msg": "x"}, id_="id1")
        agent.messages.append({
            "role": "assistant",
            "content": [{"type": "tool_use", "id": "id1", "name": "echo", "input": {"msg": "x"}}],
        })
        ctx = TurnContext(run_state=agent._run_state)
        _set_resume_state(agent, ctx, [tc])

        monkeypatch.setattr(_get_perm_handler(agent), "_check_tool_permission", lambda *_: (True, None, {}))
        monkeypatch.setattr(agent.tools, "execute",
                            lambda *a, **kw: {"status": "success", "output": "ok"})

        list(agent._tool_chain.run(ctx))

        # 关键 — resume 路径下 ctx.permission_request 必须保持 None
        # 否则 ExecutingToolsPhase.next() 看 permission_request 非空 → 误转 AWAITING_PERMISSION
        assert ctx.permission_request is None, (
            "resume 路径写 permission_request 会触发 await loop(LLMThinkingPhase 误判)"
        )


# ────────────────────────────────────────────────────────────
# ToolDispatchHandler — emit tool_call events + resume skip
# ────────────────────────────────────────────────────────────


class TestToolDispatchSingle:
    """6:单 ALLOW → emit 1 个 tool_call event(parallel=False)+ 1 个 tool_result"""

    def test_tool_dispatch_single_sync_call(self, monkeypatch):
        agent = _make_agent()
        tc = _make_tool_call("echo", {"msg": "hi"}, id_="id_single")
        ctx = _make_ctx(agent, tool_calls=[tc])

        monkeypatch.setattr(_get_perm_handler(agent), "_check_tool_permission",
                            lambda n, i: (True, None, i))
        execute_calls: list = []
        monkeypatch.setattr(
            agent.tools, "execute",
            lambda name, input_, **kw: (execute_calls.append(name)
                                          or {"status": "success", "output": f"echo: {input_['msg']}"}),
        )

        events = list(agent._tool_chain.run(ctx))

        # 1 个 tool_call event(单 tool 形态:key="name" + "input" + parallel=False)
        tool_call_events = [e for e in events if e[0] == "tool_call"]
        assert len(tool_call_events) == 1
        assert tool_call_events[0][1]["name"] == "echo"
        assert tool_call_events[0][1]["input"] == {"msg": "hi"}
        assert tool_call_events[0][1]["parallel"] is False
        # 1 个 tool_result event(success=True)
        tool_result_events = [e for e in events if e[0] == "tool_result"]
        assert len(tool_result_events) == 1
        assert tool_result_events[0][1]["success"] is True
        assert tool_result_events[0][1]["output"] == "echo: hi"
        # execute 调 1 次
        assert execute_calls == ["echo"]


class TestToolDispatchParallel:
    """7:≥2 ALLOW → ThreadPoolExecutor 触发,events 按 LLM 顺序"""

    def test_tool_dispatch_parallel_via_executor(self, monkeypatch):
        agent = _make_agent()
        tcs = [
            _make_tool_call("echo", {"msg": "p1"}, id_="idp1"),
            _make_tool_call("echo", {"msg": "p2"}, id_="idp2"),
            _make_tool_call("echo", {"msg": "p3"}, id_="idp3"),
        ]
        ctx = _make_ctx(agent, tool_calls=tcs)

        monkeypatch.setattr(_get_perm_handler(agent), "_check_tool_permission",
                            lambda n, i: (True, None, i))

        def slow_execute(name, input_, **kw):
            # 故意打乱完成顺序:第 2 个先完成,验证 sort 回 LLM 顺序
            time.sleep(0.01 * (3 - int(input_["msg"][-1])))
            return {"status": "success", "output": f"echo: {input_['msg']}"}

        monkeypatch.setattr(agent.tools, "execute", slow_execute)

        events = list(agent._tool_chain.run(ctx))

        # 1 个 tool_call event(parallel=True, names 含 3 个)
        tool_call_events = [e for e in events if e[0] == "tool_call"]
        assert len(tool_call_events) == 1
        assert tool_call_events[0][1]["parallel"] is True
        assert tool_call_events[0][1]["names"] == ["echo", "echo", "echo"]
        # 3 个 tool_result,顺序按 LLM 原始(tc_order)而非完成顺序
        tool_result_events = [e for e in events if e[0] == "tool_result"]
        assert len(tool_result_events) == 3
        outputs = [e[1]["output"] for e in tool_result_events]
        # LLM 给的顺序是 p1 / p2 / p3 → sort 后应保持这个顺序
        assert outputs == ["echo: p1", "echo: p2", "echo: p3"], (
            f"events 应按 LLM 顺序 emit(实际 {outputs})"
        )


# ────────────────────────────────────────────────────────────
# ToolExecuteHandler — DENY pre-fill + Fix C 清理
# ────────────────────────────────────────────────────────────


class TestToolExecuteDenyPrefill:
    """8:outcome="deny" → emit 假 tool_result(error)同步,不抛错"""

    def test_tool_execute_deny_pre_fills_error_result(self, monkeypatch):
        agent = _make_agent()
        tc = _make_tool_call("echo", {"msg": "x"}, id_="id_denied")
        ctx = _make_ctx(agent, tool_calls=[tc])

        monkeypatch.setattr(
            _get_perm_handler(agent), "_check_tool_permission",
            lambda n, i: (False, "blocked by deny rule", None),
        )
        # 关键 — execute 不应被调(DENY pre-fill 路径跳过 execute)
        execute_called = {"n": 0}
        monkeypatch.setattr(
            agent.tools, "execute",
            lambda *a, **kw: (execute_called.__setitem__("n", execute_called["n"] + 1)
                               or {"status": "error", "error": "should not reach"}),
        )

        events = list(agent._tool_chain.run(ctx))

        # emit 1 个 tool_result(success=False, output=deny 错误文案)
        tool_result_events = [e for e in events if e[0] == "tool_result"]
        assert len(tool_result_events) == 1
        assert tool_result_events[0][1]["success"] is False
        assert tool_result_events[0][1]["output"] == "blocked by deny rule"
        assert tool_result_events[0][1]["elapsed"] == 0.0
        # execute 没被调
        assert execute_called["n"] == 0
        # pending_tool_logs 也写了 result entry(success=False)
        log_entries = [log for log in agent._run_state.pending_tool_logs
                       if log.get("type") == "result"]
        assert len(log_entries) == 1
        assert log_entries[0]["success"] is False


class TestToolExecuteFixCCleanup:
    """9:Fix C 尾清理 — 工具执行完后,turn_ctx.awaiting_permission 应清空(Step 2, 2026-07-07)。"""

    def test_tool_execute_clears_awaiting_permission_after_exec(self, monkeypatch):
        agent = _make_agent()
        tc = _make_tool_call("echo", {"msg": "x"}, id_="id_clean")
        agent.messages.append({
            "role": "assistant",
            "content": [{"type": "tool_use", "id": "id_clean", "name": "echo",
                         "input": {"msg": "x"}}],
        })
        ctx = TurnContext(run_state=agent._run_state)
        # 模拟 resume 状态:awaiting_permission 已设(用户刚点 Allow)— Step 2 写在 turn_ctx
        _set_resume_state(agent, ctx, [tc])

        # 工具执行成功
        monkeypatch.setattr(_get_perm_handler(agent), "_check_tool_permission", lambda n, i: (True, None, i))
        monkeypatch.setattr(agent.tools, "execute",
                            lambda *a, **kw: {"status": "success", "output": "ok"})

        # 起始状态(模拟 resume 入口)— Step 2 写到 turn_ctx
        assert ctx.awaiting_permission is not None

        list(agent._tool_chain.run(ctx))

        # 工具执行完后,ToolExecuteHandler 必须清 turn_ctx.awaiting_permission
        assert ctx.awaiting_permission is None, (
            "Fix C:resume 后 tool 执行完未清 awaiting_permission,下次 step() 误触发 resume"
        )
        # batch 也清
        assert ctx.awaiting_permission_batch == []


class TestToolExecuteFixCTailClearPendingRequest:
    """10b (2026-07-07):Plan A 回归 — `_is_resume=True` 时,ToolExecuteHandler 尾清理必须
    把 `agent._pending_permission_request` 清空(替代 web/app.py 之前做的提前清空)。

    场景模拟(replay web/app.py 真实流程):
      1. `_ask_user_permission_v2` 设 `agent._pending_permission_request` dict(无 choice)
      2. UI 用户点 Allow → `resolve_permission('allow')` 设 `choice='allow'`(**不**清 dict)
      3. UI 调 `step()` → PermissionCheckHandler `_is_resume=True` 跳 check
      4. ToolExecuteHandler 工具执行完 → 尾清理必须把 agent-level pending 也清

    旧 gate 只看 `ctx.awaiting_permission is not None` —— 而 `_is_resume=True` 路径
    故意不写 ctx.awaiting_permission(避免 LLMThinkingPhase.next 误路由 AWAITING),
    所以旧 tail-clear 漏跑 → agent._pending_permission_request 不清 → 下次 ASK 把旧
    pending 当成当前 pending → resume 检测死循环(2026-07-07 real bug)。

    关键差异 vs TestToolExecuteFixCCleanup:_set_resume_state() 也写 ctx.awaiting_permission,
    所以旧测试只覆盖了"ctx.awaiting_permission 路径";本测试只写 agent._pending_permission_request,
    隔离 `_is_resume=True` gate 分支。
"""

    def test_tail_clear_clears_pending_permission_request_on_resume(self, monkeypatch):
        agent = _make_agent()
        tc = _make_tool_call("echo", {"msg": "resume_tail"}, id_="id_tail_clear")
        agent.messages.append({
            "role": "assistant",
            "content": [{"type": "tool_use", "id": "id_tail_clear", "name": "echo",
                         "input": {"msg": "resume_tail"}}],
        })
        ctx = TurnContext(run_state=agent._run_state)

        # Plan A 模拟:仅写 agent-level pending(UI 不再写 ctx.awaiting_permission)
        agent._pending_permission_request = {
            "tool_name": "echo",
            "tool_input": {"msg": "resume_tail"},
            "reason": "needs approval",
            "message": "Allow?",
            "choice": "allow",  # resolve_permission 设的
        }
        agent._run_state.last_tool_calls = [tc]
        # 关键:ctx.awaiting_permission **不**设,隔离 _is_resume gate 分支
        assert ctx.awaiting_permission is None

        monkeypatch.setattr(_get_perm_handler(agent), "_check_tool_permission",
                            lambda n, i: (True, None, i))
        execute_calls: list = []
        monkeypatch.setattr(
            agent.tools, "execute",
            lambda name, input_, **kw: (execute_calls.append(name)
                                          or {"status": "success", "output": "ok_tail"}),
        )

        # 起始断言:pending 非空(模拟 UI 刚 set choice 后)
        assert agent._pending_permission_request is not None
        assert agent._pending_permission_request.get("choice") == "allow"

        events = list(agent._tool_chain.run(ctx))

        # 工具实际执行了
        assert execute_calls == ["echo"], (
            "_is_resume=True 路径:tool 应该跑起来,实际没跑说明 _is_resume 检测又死了"
        )
        # Plan A 关键断言:工具执行完,尾清理必须把 agent-level pending 也清空
        assert agent._pending_permission_request is None, (
            "Plan A 回归失败:_is_resume=True 路径下 ToolExecuteHandler 尾清理漏清 "
            "agent._pending_permission_request,下次 ASK 会把旧 pending 当当前 pending "
            "用,resume 检测死循环"
        )
        # ctx.awaiting_permission 本来就是 None,仍然应是 None
        assert ctx.awaiting_permission is None
        assert ctx.awaiting_permission_batch == []
        # tool_result event 1 个 success=True
        tool_result_events = [e for e in events if e[0] == "tool_result"]
        assert len(tool_result_events) == 1
        assert tool_result_events[0][1]["success"] is True
        assert tool_result_events[0][1]["output"] == "ok_tail"


class TestToolDispatchResumeSkip:
    """10:resume 路径 ToolDispatchHandler emit tool_call + append action log。

    2026-07-03 修复:原 _is_resume 跳过(注释"action 已 yield 过")前提错误 —
    首次 ASK 路径 PermissionCheckHandler _StopChain 短路,action 从没 emit →
    UI 永远不显示 Action。修复:移除跳过,resume 路径也 emit + append action。
    """

    def test_tool_dispatch_emits_action_on_resume(self, monkeypatch):
        agent = _make_agent()
        tc = _make_tool_call("echo", {"msg": "x"}, id_="id_resume_d")
        agent.messages.append({
            "role": "assistant",
            "content": [{"type": "tool_use", "id": "id_resume_d", "name": "echo",
                         "input": {"msg": "x"}}],
        })
        ctx = TurnContext(run_state=agent._run_state)
        # Step 2:awaiting_permission 写到 turn_ctx
        _set_resume_state(agent, ctx, [tc])

        monkeypatch.setattr(_get_perm_handler(agent), "_check_tool_permission", lambda n, i: (True, None, i))
        monkeypatch.setattr(agent.tools, "execute",
                            lambda *a, **kw: {"status": "success", "output": "ok"})

        # 记录 pending_tool_logs 起始条数
        initial_log_count = len(agent._run_state.pending_tool_logs)

        events = list(agent._tool_chain.run(ctx))

        # resume 路径 emit tool_call event(action)— 见方法 docstring 的 2026-07-03 修复
        tool_call_events = [e for e in events if e[0] == "tool_call"]
        assert len(tool_call_events) == 1, (
            f"resume 路径应 emit 1 个 tool_call event,实际: {tool_call_events}"
        )
        assert tool_call_events[0][1]["name"] == "echo"
        # pending_tool_logs 追加 action + result(原测试只断言 result,现在加 action)
        new_logs = agent._run_state.pending_tool_logs[initial_log_count:]
        assert any(log.get("type") == "action" for log in new_logs), (
            f"resume 路径应追加 action log,实际 new logs: {new_logs}"
        )
        assert any(log.get("type") == "result" for log in new_logs), (
            f"resume 路径应追加 result log,实际 new logs: {new_logs}"
        )
        # tool_result 还是有的(1 个 success)
        tool_result_events = [e for e in events if e[0] == "tool_result"]
        assert len(tool_result_events) == 1
        assert tool_result_events[0][1]["output"] == "ok"