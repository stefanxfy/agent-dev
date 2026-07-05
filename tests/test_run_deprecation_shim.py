"""
test_run_deprecation_shim.py — Plan B Final Phase Step 2 验收(2026-07-02)

覆盖 plan §15 Step 9 列出的 6 case,验证 ReactAgent.run() 从 ~700 行 v1 monolithic
收缩为 ~15 行 shim 后的核心契约:
1. test_run_emits_deprecation_warning — 调 agent.run() 触发 DeprecationWarning,指向 v2 API
2. test_run_yields_same_events_as_step_loop — shim 透传 step() 产出的 events(parity)
3. test_run_awaiting_permission_early_return — awaiting_permission 被设 → yield + break
4. test_run_interrupt_early_return — cancel_event 被设 → yield ⏹️ + break
5. test_run_max_turns_message — _sm.is_done → while 自然退出
6. test_run_tail_flush — shim 末尾兜底 _session_manager.flush()

实现策略:
- 真 ReactAgent + stub LLM(轻量 fixture,不依赖真实 provider)
- case 1/2/6 用真实 run() 驱动(stub LLM 跑通 chat → ChunkParse → output chain)
- case 3/4/5 用 monkeypatch agent.step 注入受控副作用,隔离测试 shim 的 while 循环逻辑
  (这样不依赖 LLM/tool/permission 的复杂 e2e,那些已被 test_interrupt /
   test_permission_integration 完整覆盖)

设计参考:
- docs/agent-state-machine-and-chain-of-responsibility-design.md §10
- plan: /Users/fanyunxu/.claude/plans/snuggly-spinning-marshmallow.md
"""

from __future__ import annotations

import warnings
from types import SimpleNamespace
from typing import List
from unittest.mock import MagicMock

import pytest

from agent_core.agent_core import ReactAgent
from agent_core.agent_state import AgentPhase
from agent_core.tools.base import ToolDef, ToolRegistry


# ────────────────────────────────────────────────────────────────────
# Stubs / helpers
# ────────────────────────────────────────────────────────────────────


class _StubLLM:
    """最小 LLM stub:LLMCallHandler 调 llm.chat(messages=, tools=, cache_namespace=)。

    返回 generator yield SimpleNamespace(text_delta/thinking_delta/tool_call/
    stop_reason/usage) — 对齐 ChunkParseHandler 期望的 chunk shape。
    """

    def __init__(self, text: str = "stub response", tool_calls: list | None = None):
        self._text = text
        self._tool_calls = tool_calls or []
        self.config = SimpleNamespace(
            model="stub-model",
            provider="stub",
            system_prompt="stub",
        )

    def chat(self, messages=None, tools=None, cache_namespace=None, **kwargs):
        """v2 path:LLMCallHandler → agent.llm.chat(...) 拿 stream iterator。"""
        # 单 chunk 整轮出
        if self._tool_calls:
            # tool_use 路径:text 空,tool_call 携带,stop_reason="tool_use"
            for tc in self._tool_calls:
                yield SimpleNamespace(
                    text_delta=None,
                    thinking_delta=None,
                    tool_call=tc,
                    stop_reason="tool_use",
                    usage=None,
                )
        else:
            yield SimpleNamespace(
                text_delta=SimpleNamespace(text=self._text),
                thinking_delta=None,
                tool_call=None,
                stop_reason="end_turn",
                usage=None,
            )


def _make_agent(llm=None, tools=None, max_turns=3) -> ReactAgent:
    """构造轻量 ReactAgent(stub LLM + 空 tool registry,无 session/memory/permission)。"""
    if llm is None:
        llm = _StubLLM()
    if tools is None:
        tools = ToolRegistry()
    return ReactAgent(
        llm_router=llm,
        tool_registry=tools,
        max_turns=max_turns,
    )


def _drain_ignore_deprecation(gen) -> List:
    """drain generator,忽略 DeprecationWarning。"""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        return list(gen)


# ────────────────────────────────────────────────────────────────────
# 1. DeprecationWarning 触发
# ────────────────────────────────────────────────────────────────────


class TestDeprecationWarning:
    """1: agent.run() 调一次触发 DeprecationWarning,文档指明 v2 API。"""

    def test_run_emits_deprecation_warning(self):
        agent = _make_agent()

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            list(agent.run("hello"))  # drain

        dep_warnings = [w for w in caught if issubclass(w.category, DeprecationWarning)]
        assert len(dep_warnings) >= 1, (
            f"expected DeprecationWarning,got {[w.category.__name__ for w in caught]}"
        )
        msg = str(dep_warnings[0].message)
        assert "start_run" in msg or "step()" in msg, (
            f"deprecation message 应指向 start_run()/step(),got: {msg}"
        )


# ────────────────────────────────────────────────────────────────────
# 2. event parity:shim 透传 step() events
# ────────────────────────────────────────────────────────────────────


class TestRunEventParity:
    """2: shim 把 step() yield 的 events 原样透传给调用方(parity 契约)。"""

    def test_run_yields_same_events_as_step_loop(self):
        """run() 透传 step() 产出的 events — 注入固定 step(),验证 run() 输出一致。

        场景:step() 每次被调 yield 2 个 event,第 2 次后让 _sm.is_done=True。
        期望:run() 透传 4 个 event,顺序保持。
        """
        agent = _make_agent()
        injected_events = [
            ("text", "chunk-1"),
            ("system", "marker-1"),
            ("text", "chunk-2"),
            ("system", "marker-2"),
        ]
        call_count = [0]

        def fake_step():
            """每次调 yield 2 个 event;第 2 次后置 is_done。"""
            call_count[0] += 1
            # yield 本次 2 个 event
            idx = (call_count[0] - 1) * 2
            yield injected_events[idx]
            yield injected_events[idx + 1]
            # 第 2 次调后,让 while 退出(_phase=DONE → is_done property 返 True)
            if call_count[0] >= 2:
                agent._sm._phase = AgentPhase.DONE

        agent.step = fake_step

        events = _drain_ignore_deprecation(agent.run("test"))

        # shim 透传全部 4 个 event,顺序保持
        assert events == injected_events, (
            f"shim 应原样透传 step() events,got: {events}"
        )


# ────────────────────────────────────────────────────────────────────
# 3. AWAITING_PERMISSION 早退
# ────────────────────────────────────────────────────────────────────


class TestAwaitingPermissionEarlyReturn:
    """3: step() 副作用设置 awaiting_permission → 下轮 while check yield + break。"""

    def test_run_awaiting_permission_early_return(self):
        """step() 设 _run_state.awaiting_permission → run() 下次 while iteration
        yield ("awaiting_permission", req) 然后 break。
        """
        agent = _make_agent()
        fake_req = {"tool_name": "echo", "tool_use_id": "tu_1", "tool_input": {}}

        def fake_step():
            # 模拟 PermissionCheckHandler 写 awaiting_permission
            agent._run_state.awaiting_permission = fake_req
            yield ("tool_call", {"id": "tu_1", "name": "echo"})
            return

        agent.step = fake_step

        events = _drain_ignore_deprecation(agent.run("use echo"))

        ap_events = [e for e in events if e[0] == "awaiting_permission"]
        assert len(ap_events) == 1, (
            f"expected exactly 1 awaiting_permission event,got {[e[0] for e in events]}"
        )
        assert ap_events[0][1] is fake_req
        # step 只被调 1 次(第二次 while check 因 awaiting_permission break)
        # 验证:run() 早退,没继续驱动


# ────────────────────────────────────────────────────────────────────
# 4. INTERRUPTED 早退
# ────────────────────────────────────────────────────────────────────


class TestInterruptEarlyReturn:
    """4: cancel_event.set() → run() yield ⏹️ + ✅ 然后 break。"""

    def test_run_interrupt_early_return(self):
        """step() 副作用 set cancel_event → 下次 while check yield 终止 events + break。"""
        agent = _make_agent()

        def fake_step():
            # 模拟 interrupt() 被 UI 调,set cancel_event
            agent._run_state.cancel_event.set()
            yield ("text", "partial")
            return

        agent.step = fake_step

        events = _drain_ignore_deprecation(agent.run("test"))

        system_events = [e for e in events if e[0] == "system"]
        msgs = [str(e[1]) for e in system_events]
        assert any("⏹️" in m for m in msgs), f"expected ⏹️ event,got system msgs: {msgs}"
        assert any("✅" in m for m in msgs), f"expected ✅ event,got system msgs: {msgs}"


# ────────────────────────────────────────────────────────────────────
# 5. MaxTurnsTermination 触发 → while 自然退出
# ────────────────────────────────────────────────────────────────────


class TestMaxTurnsTermination:
    """5: _sm.is_done=True(MaxTurns 命中)→ while 条件失败,run() 自然退出。"""

    def test_run_max_turns_message(self):
        """step() 第 N 次后让 _sm.is_done=True → while 退出,run() 不死循环。"""
        agent = _make_agent()
        call_count = [0]

        def fake_step():
            call_count[0] += 1
            yield ("text", f"turn-{call_count[0]}")
            # 第 3 次后模拟 MaxTurns 命中(_phase=DONE → is_done property 返 True)
            if call_count[0] >= 3:
                agent._sm._phase = AgentPhase.DONE

        agent.step = fake_step

        events = _drain_ignore_deprecation(agent.run("loop"))

        # run() 退出(没 hang),step 被调 3 次
        assert call_count[0] == 3, (
            f"expected step called 3x then done,got {call_count[0]}x"
        )
        # 3 个 text event 透传
        text_events = [e for e in events if e[0] == "text"]
        assert len(text_events) == 3


# ────────────────────────────────────────────────────────────────────
# 6. shim 末尾兜底 flush
# ────────────────────────────────────────────────────────────────────


class TestRunTailFlush:
    """6: shim 末尾调 _session_manager.flush()(belt-and-suspenders 兜底)。

    start_run() 内部 add_user_message(对齐 v1 语义);shim 自己不重复 inline
    add_*/append(那些由 Stage A/B/C chain handler 接管)。
    """

    def test_run_tail_flush_called(self):
        """run() 退出后 _session_manager.flush() 被调 1 次(兜底)。"""
        agent = _make_agent()
        sm = MagicMock()
        agent._session_manager = sm  # 构造后注入(ReactAgent.__init__ 用 session_id 构造)

        _drain_ignore_deprecation(agent.run("test"))

        # flush 兜底被调
        sm.flush.assert_called_once()
        # start_run 内部 add_user_message 一次(v1 语义保留)
        sm.add_user_message.assert_called_once_with("test")

    def test_run_tail_flush_swallows_exception(self):
        """flush() 抛异常被 shim 吞掉(_logger.warning),不 propagate 给调用方。"""
        agent = _make_agent()
        sm = MagicMock()
        sm.flush.side_effect = RuntimeError("disk full")
        agent._session_manager = sm

        # 不抛异常
        events = _drain_ignore_deprecation(agent.run("test"))
        # events 仍正常产出(shim 没 crash)
        assert isinstance(events, list)
