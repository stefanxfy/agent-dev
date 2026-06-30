"""
Agent State Machine — Phase-Class 模型

v2 重构引入(详见 docs/agent-state-machine-and-chain-of-responsibility-design.md §3):

核心组件:
1. AgentPhase:7 个 phase 的 enum(SETUP / LLM_THINKING / AWAITING_PERMISSION /
   EXECUTING_TOOLS / FINALIZING / INTERRUPTED / DONE)
2. Phase ABC:每个 phase 有自己的 chain + enter(trigger, ctx) + next(trigger, ctx)
3. StateMachine:路由 trigger + 调 phase.enter + 调 phase.next + 终止检查 + interrupt
4. PhaseContext:phase enter() 时的统一上下文
5. RunState:per-run 累积状态(含 cancel_event)
6. TerminationCondition:显式终止条件(替代散落的 if max_turns)
7. InvalidTransition:终态再 trigger 时抛错
"""

from __future__ import annotations

import logging
import threading
import time

_logger = logging.getLogger("agent_core.agent_state")
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Iterator, Optional


# 抑制 unused import 警告(forward reference)
__all__ = [
    "AgentPhase",
    "Event",
    "Phase",
    "PhaseContext",
    "InvalidTransition",
    "StateMachine",
    "RunState",
    "TerminationCondition",
    "MaxTurnsTermination",
    "CompositeTermination",
    "SetupPhase",
    "LLMThinkingPhase",
    "AwaitingPermissionPhase",
    "ExecutingToolsPhase",
    "FinalizingPhase",
    "InterruptedPhase",
    "DonePhase",
]


# ────────────────────────────────────────────────────────────
# Event 类型
# ────────────────────────────────────────────────────────────

# Event 是 (event_type, content) tuple
# 例:("text", "hello") / ("tool_call", {...}) / ("awaiting_permission", {...})
Event = tuple[str, Any]


# ────────────────────────────────────────────────────────────
# AgentPhase enum
# ────────────────────────────────────────────────────────────

class AgentPhase(Enum):
    """agent 一次 run 的显式阶段。

    状态转移图(详见 Phase 子类):
        SETUP → LLM_THINKING ⇄ AWAITING_PERMISSION
                  ↓ ↑ ↓ ↓
                  ↓ EXECUTING_TOOLS
                  ↓
                FINALIZING → DONE

        (任意 phase ──interrupt──► INTERRUPTED → 终态)

    终态:DONE(自然完成)/ INTERRUPTED(用户主动中断)
    """
    SETUP               = "setup"
    LLM_THINKING        = "llm_thinking"
    AWAITING_PERMISSION = "awaiting_permission"
    EXECUTING_TOOLS     = "executing_tools"
    FINALIZING          = "finalizing"
    INTERRUPTED         = "interrupted"
    DONE                = "done"


# ────────────────────────────────────────────────────────────
# RunState — per-run 累积状态(含 cancel_event)
# ────────────────────────────────────────────────────────────

@dataclass
class RunState:
    """per-run 累积状态(生命周期:start_run → DONE / INTERRUPTED / 抛错)。

    关键字段:
    - cancel_event:threading.Event — 用户中断信号(Stop 按钮 / Esc 触发)
    - turn:当前 turn 计数
    - final_answer:run 终态时的最终答案
    - awaiting_permission:run 暂停时等用户决定(与 AWAITING_PERMISSION phase 配套)

    详见设计文档 §5.3。
    """
    cancel_event: threading.Event = field(default_factory=threading.Event)
    turn: int = 0
    # A1-H2 (review 修复): 记录 run 起点 wall-clock 给 TimeoutTermination 用
    # 用 time.monotonic() 防系统时间漂移。
    created_at: float = field(default_factory=time.monotonic)
    user_message: str = ""
    final_answer: str = ""
    final_stop_reason: Optional[str] = None
    last_input_tokens: int = 0
    last_output_tokens: int = 0
    last_tool_calls: list = field(default_factory=list)
    pending_tool_logs: list = field(default_factory=list)
    pending_tool_results: list = field(default_factory=list)
    pending_thinking: str = ""
    surfaced_memories: set = field(default_factory=set)
    awaiting_permission: Optional[dict] = None
    termination_reason: Optional[str] = None
    logger: logging.Logger = field(default_factory=lambda: logging.getLogger("agent_core.run"))


# ────────────────────────────────────────────────────────────
# TurnContext — per-turn 工作内存(handler 间共享)
# ────────────────────────────────────────────────────────────

@dataclass
class TurnContext:
    """per-turn 工作内存(handler 间共享)。

    关键字段:
    - run_state:RunState(per-run,跨 turn 持久)
    - stage_inputs:StageInputs(inputs_chain 输出)
    - stage_outputs:LLMResult(llm_chain 输出)
    - permission_request:当前 turn 的 permission 请求(AWAITING_PERMISSION 时填)
    - events:本 turn 已产出的 events
    - _stopped:chain 短路 flag
    """
    run_state: RunState
    # A1-H3 (review 修复): 每 turn 的编号,供 phase 区分上下文用
    # (例如 SETUP phase 多次进要分"第 1 次进入" vs "后续"
    # 但实际触发是按 run 触发,这里 turn_number 仅作 debug / 日志标识)
    turn_number: int = 0
    stage_inputs: Optional[Any] = None
    stage_outputs: Optional[Any] = None
    permission_request: Optional[dict] = None
    events: list = field(default_factory=list)
    _stopped: bool = False

    def emit(self, event: Event) -> None:
        self.events.append(event)

    def stop(self) -> None:
        self._stopped = True

    @property
    def is_stopped(self) -> bool:
        return self._stopped


# ────────────────────────────────────────────────────────────
# PhaseContext — phase enter() 时的统一上下文
# ────────────────────────────────────────────────────────────

@dataclass
class PhaseContext:
    """phase enter() 时的统一上下文。

    包含:
    - run_state:RunState(per-run)
    - turn_ctx:TurnContext(per-turn,每次 enter 重新创建)
    - termination:TerminationCondition(给 phase 自主查询终止条件用,None → SM 默认)
    - sm:StateMachine 自身(供 phase 转移用)

    A1-H1 (review 修复): 设计 §3.1 规定 PhaseContext 应持有 termination,
    让 phase 的 enter()/next() 可以 ctx.termination.check(...) 自主查询,
    不必穿透 SM 私有属性。None 表示用 SM 自带 termination(向后兼容
    旧 test fixture / 第三方代码)。
    """
    run_state: RunState
    turn_ctx: TurnContext
    termination: Optional["TerminationCondition"] = None
    sm: "StateMachine" = None  # type: ignore[assignment]  # late-bind in setup


# ────────────────────────────────────────────────────────────
# 异常
# ────────────────────────────────────────────────────────────

class InvalidTransition(Exception):
    """当前 phase 不能被指定 trigger 触发 / 不能 transition。"""


# ────────────────────────────────────────────────────────────
# Phase ABC
# ────────────────────────────────────────────────────────────

class Phase(ABC):
    """agent 阶段基类。

    关键设计:
    - 每个 Phase 有自己的 chain(可空,空 chain 表示该 phase 不做事直接转出)
    - enter(trigger, ctx) 干完活,逐个 yield Event(流式!)
    - next(trigger, ctx) 决定下一 phase(可读 ctx.stage_outputs 决策)
    - StateMachine 把 trigger 事件路由到当前 phase 的 enter
    """

    def __init__(self, chain: Optional["TurnChain"] = None):
        # 避免循环 import(turn_chain 在同包内,延迟引用)
        from agent_core.turn_chain import TurnChain
        self._chain = chain if chain is not None else TurnChain([])

    @property
    def name(self) -> str:
        return self.__class__.__name__

    @abstractmethod
    def enter(self, trigger: str, ctx: PhaseContext) -> Iterator[Event]:
        """在当前 phase 干完活,逐个 yield Event。"""

    @abstractmethod
    def next(self, trigger: str, ctx: PhaseContext) -> tuple[str, AgentPhase]:
        """决定下一 phase。Returns (next_trigger, next_phase)。"""

    def __repr__(self) -> str:
        return f"<Phase {self.name}>"


# ────────────────────────────────────────────────────────────
# TerminationCondition — 显式终止条件
# ────────────────────────────────────────────────────────────

class TerminationCondition(ABC):
    """agent run 终止条件。

    显式建模 vs 散落 if:
    - 散落 if:每个 handler 自己 check,容易漏
    - 显式 TerminationCondition:StateMachine 每次 trigger 前统一 check
    """

    @abstractmethod
    def check(self, run_state: RunState, turn_ctx: TurnContext) -> Optional[str]:
        """返 None = 继续;返 str = 终止原因(转 DONE)。"""


class MaxTurnsTermination(TerminationCondition):
    """达到 max_turns 强制终止。"""

    def __init__(self, max_turns: int):
        self._max = max_turns

    def check(self, run_state: RunState, turn_ctx: TurnContext) -> Optional[str]:
        if run_state.turn >= self._max:
            return f"max_turns_reached ({self._max})"
        return None


class CompositeTermination(TerminationCondition):
    """多个 termination 组合,任一触发即终止。"""

    def __init__(self, *conditions: TerminationCondition):
        self._conditions = conditions

    def check(self, run_state: RunState, turn_ctx: TurnContext) -> Optional[str]:
        for c in self._conditions:
            reason = c.check(run_state, turn_ctx)
            if reason:
                return reason
        return None


class TimeoutTermination(TerminationCondition):
    """A1-H2 (review 修复): wall-clock timeout 终止条件。

    用 time.monotonic() (vs RunState.created_at) 防止系统时钟漂移。
    timeout_s:最大允许运行秒数。
    """

    def __init__(self, timeout_s: float):
        self._timeout_s = timeout_s

    def check(self, run_state: RunState, turn_ctx: TurnContext) -> Optional[str]:
        elapsed = time.monotonic() - run_state.created_at
        if elapsed >= self._timeout_s:
            return f"timeout_reached ({elapsed:.1f}s > {self._timeout_s}s)"
        return None


def build_default_termination(
    max_turns: int = 10,
    timeout_s: Optional[float] = None,
) -> TerminationCondition:
    """A1-H2 (review 修复): 默认 termination 组合工厂。

    显式组装成 CompositeTermination — 把 termination 集中在一处,后续加
    TokenBudgetTermination / ExplicitAbortTermination 等只要在这里拼。
    """
    conds: list[TerminationCondition] = [MaxTurnsTermination(max_turns)]
    if timeout_s is not None:
        conds.append(TimeoutTermination(timeout_s))
    if len(conds) == 1:
        return conds[0]  # 单 condition 不必包 Composite
    return CompositeTermination(*conds)


# ────────────────────────────────────────────────────────────
# StateMachine — 路由 + 终止 + interrupt
# ────────────────────────────────────────────────────────────

class StateMachine:
    """agent 状态机:路由 trigger + 调 phase.enter + 调 phase.next + 终止检查。

    关键设计:
    - 转移表一次性注册,运行期不修改
    - trigger(event) 是 generator,逐个 yield phase.enter 的 events
    - 每次 trigger 前调 termination.check();命中终止 → 转 DONE
    - on_enter / on_exit hook 用于 logging / metrics(不污染 phase 业务代码)
    - interrupt() 走 cancel 协调机制,通知正在运行的 handler 立即停止
    """

    def __init__(
        self,
        phases: dict[AgentPhase, Phase],
        initial: AgentPhase,
        termination: TerminationCondition,
    ):
        self._phases = phases
        self._phase = initial
        self._history: list[tuple[AgentPhase, str, AgentPhase]] = []
        self._termination = termination
        self._on_enter_hooks: list[Callable[[AgentPhase, str], None]] = []
        self._on_exit_hooks: list[Callable[[AgentPhase, str, AgentPhase], None]] = []

    @property
    def current(self) -> AgentPhase:
        return self._phase

    @property
    def is_done(self) -> bool:
        return self._phase in (AgentPhase.DONE, AgentPhase.INTERRUPTED)

    @property
    def is_interrupted(self) -> bool:
        return self._phase == AgentPhase.INTERRUPTED

    @property
    def history(self) -> list[tuple[AgentPhase, str, AgentPhase]]:
        return list(self._history)

    def on_enter(self, callback: Callable[[AgentPhase, str], None]) -> None:
        """注册 phase enter hook(供 logging / metrics / debug)。"""
        self._on_enter_hooks.append(callback)

    def on_exit(self, callback: Callable[[AgentPhase, str, AgentPhase], None]) -> None:
        self._on_exit_hooks.append(callback)

    def trigger(self, event: str, ctx: PhaseContext) -> Iterator[Event]:
        """路由 trigger 到当前 phase。

        1. INTERRUPTED 是终态:任何 trigger 直接抛错
        2. 调 termination.check();命中 → 转 DONE,yield 终止 event
        3. 调当前 phase.enter(event, ctx) → yield 它产的所有 events
        4. 调 phase.next(event, ctx) → 拿到 (next_trigger, next_phase)
        5. 触发 on_exit / on_enter hooks
        6. 更新 _phase + _history
        7. 自动 trigger 下一 phase(self-trigger 链式推进)
        """
        # 0. INTERRUPTED 终态保护
        if self._phase == AgentPhase.INTERRUPTED:
            raise InvalidTransition(
                "INTERRUPTED 是终态,不能 trigger(请先 start_run 重置)"
            )

        # 1. 终止检查(优先 ctx.termination 透传,fallback 到 self._termination)
        # 这样 phase 可以传自定义 termination(测试 mock、扩展场景),
        # 默认用 SM 自己的 termination。
        _term = ctx.termination or self._termination
        term_reason = _term.check(ctx.run_state, ctx.turn_ctx)
        if term_reason is not None:
            _logger.debug(
                "🛑 [SM trigger] termination hit: %s (from phase=%s)",
                term_reason, self._phase.value,
            )
            yield from self._terminate(term_reason, ctx)
            return

        # 2. 当前 phase 干活
        current = self._phases[self._phase]
        ctx.turn_ctx.events.clear()
        yield from current.enter(event, ctx)

        # 3. 决定下一 phase
        next_trigger, next_phase = current.next(event, ctx)

        # 4. hooks
        for h in self._on_exit_hooks:
            h(self._phase, event, next_phase)
        for h in self._on_enter_hooks:
            h(next_phase, next_trigger)

        # 5. 记录
        self._history.append((self._phase, event, next_phase))
        self._phase = next_phase

        # 6. 流式触发下一 phase(链式,直到 is_done 或 AWAITING_PERMISSION 暂停)
        if not self.is_done:
            try:
                yield from self.trigger(next_trigger, ctx)
            except InvalidTransition:
                # Pause signal:AwaitingPermissionPhase 对非 "permission_resolved"
                # trigger 抛 InvalidTransition。phase 转移已完成(在 line 397-398),
                # 这里只停止 chain 推进,不再向上抛。调用方 (step / _drive) 拿到
                # 已 yield 的 events,然后检查 SM.current == AWAITING_PERMISSION
                # 就知道是在等 resume。
                _logger.debug(
                    "⏸️ [SM trigger] pause at %s (AwaitingPermission needs resume)",
                    self._phase.value,
                )
                return

    def _terminate(self, reason: str, ctx: PhaseContext) -> Iterator[Event]:
        """转 DONE,产终止 event。"""
        ctx.run_state.termination_reason = reason
        yield ("system", f"⚠️ 终止:{reason}")
        self._history.append((self._phase, f"terminate:{reason}", AgentPhase.DONE))
        self._phase = AgentPhase.DONE

    def interrupt(self, ctx: PhaseContext) -> Iterator[Event]:
        """用户主动中断(Stop 按钮 / Esc):从任意 phase 强制转 INTERRUPTED。

        与正常 trigger 的区别:
        - 不调 phase.enter()(不干活)
        - 不调 phase.next()(不转移)
        - 走 cancel 协调机制,通知正在运行的 handler 立即停止
        - 立即转 INTERRUPTED 终态

        关键:
        - ctx.run_state.cancel_event.set() 必须先于 yield,让 handler 下一轮迭代检查到
        - 幂等:多次调用只触发一次 phase 转移
        """
        # 1. 通知正在运行的 handler 立即停止
        ctx.run_state.cancel_event.set()

        # 2. 幂等:已中断,不再 transfer,不再 yield events
        if self._phase == AgentPhase.INTERRUPTED:
            return

        # 3. 记录转移
        old_phase = self._phase
        self._history.append((old_phase, "interrupt", AgentPhase.INTERRUPTED))

        # 4. hooks
        for h in self._on_exit_hooks:
            h(old_phase, "interrupt", AgentPhase.INTERRUPTED)
        for h in self._on_enter_hooks:
            h(AgentPhase.INTERRUPTED, "interrupt")

        # 5. 转 INTERRUPTED
        self._phase = AgentPhase.INTERRUPTED

        # 6. 产 events
        yield ("system", "⏹️ 对话已被用户中断")
        yield ("system", "✅ 对话结束")

    def checkpoint(self) -> dict:
        """序列化当前状态供事后 replay/debug。"""
        return {
            "phase": self._phase.value,
            "history": [(p.value, t, n.value) for p, t, n in self._history],
        }

    def __repr__(self) -> str:
        if not self._history:
            return f"<SM current={self._phase.value}>"
        last_5 = self._history[-5:]
        lines = [f"  {p.value} --[{t}]--> {n.value}" for p, t, n in last_5]
        return (
            f"<SM current={self._phase.value} "
            f"history(last 5):\n" + "\n".join(lines) + ">"
        )


# ────────────────────────────────────────────────────────────
# Phase 子类 — 7 个(6 + INTERRUPTED 终态)
# ────────────────────────────────────────────────────────────

# 注意:这些 phase class 的具体 chain 在 v2 实施中由 AgentBuilder 注入。
# 这里只提供默认空 chain,具体业务逻辑在 turn_chain.py 的 handler 里。

class SetupPhase(Phase):
    """SETUP 阶段:append user msg + 准备 system prompt + 准备 tool schemas。

    默认 enter:走 inputs_chain(由 AgentBuilder 注入,通常是
    MemoryRetrievalHandler + SystemPromptHandler + ToolsSchemaPrepareHandler)
    """
    def enter(self, trigger: str, ctx: PhaseContext) -> Iterator[Event]:
        # 默认实现:走 chain(由 AgentBuilder 注入)
        yield from self._chain.run(ctx.turn_ctx)

    def next(self, trigger: str, ctx: PhaseContext) -> tuple[str, AgentPhase]:
        return ("llm_call", AgentPhase.LLM_THINKING)


class LLMThinkingPhase(Phase):
    """LLM_THINKING 阶段:调 LLM + 收 chunks + 决定下一步。

    三条出口:
    - permission_request 非空 → AWAITING_PERMISSION
    - 无 tool_call → FINALIZING
    - 有 tool_call → EXECUTING_TOOLS
    """
    def enter(self, trigger: str, ctx: PhaseContext) -> Iterator[Event]:
        yield from self._chain.run(ctx.turn_ctx)

    def next(self, trigger: str, ctx: PhaseContext) -> tuple[str, AgentPhase]:
        if ctx.turn_ctx.permission_request is not None:
            return ("permission_needed", AgentPhase.AWAITING_PERMISSION)
        # stage_outputs 可能是 None 或缺 tool_calls 字段
        stage_out = ctx.turn_ctx.stage_outputs
        tool_calls = getattr(stage_out, "tool_calls", None) if stage_out else None
        if not tool_calls:
            if stage_out is not None:
                ctx.run_state.final_answer = getattr(stage_out, "full_text", "")
                ctx.run_state.final_stop_reason = getattr(stage_out, "stop_reason", None)
            return ("llm_responded_final", AgentPhase.FINALIZING)
        return ("llm_responded_with_tools", AgentPhase.EXECUTING_TOOLS)


class AwaitingPermissionPhase(Phase):
    """AWAITING_PERMISSION 阶段:不做事,等 UI 调 resume_after_permission。

    chain 留空。转移由外部触发:UI 点 Allow 后,UI 调 agent.resume_after_permission()
    → agent 内部 trigger('permission_resolved') → 转 EXECUTING_TOOLS。
    """
    def enter(self, trigger: str, ctx: PhaseContext) -> Iterator[Event]:
        return
        yield  # 显式空 generator(语法要求)

    def next(self, trigger: str, ctx: PhaseContext) -> tuple[str, AgentPhase]:
        if trigger == "permission_resolved":
            return ("execute_tools", AgentPhase.EXECUTING_TOOLS)
        raise InvalidTransition(
            f"AwaitingPermission 不响应 trigger='{trigger}'"
        )


class ExecutingToolsPhase(Phase):
    """EXECUTING_TOOLS 阶段:执行 tool_call + 写 tool_result。

    Plan A: 检测 turn_ctx.permission_request(由 ToolExecuteHandler 在
    _iter_phase_tools 写入)→ 若有,转 AWAITING_PERMISSION 暂停。
    否则正常回 LLM_THINKING 走下一轮。
    """
    def enter(self, trigger: str, ctx: PhaseContext) -> Iterator[Event]:
        yield from self._chain.run(ctx.turn_ctx)
        # 写 tool_result 到 self.messages 由具体 handler 处理(暂时保留为 v1 行为)

    def next(self, trigger: str, ctx: PhaseContext) -> tuple[str, AgentPhase]:
        # TODO-DEBUG (2026-06-30): 临时 debug log,定位 "SM 不转 AWAITING_PERMISSION" bug。
        # 现象:permission_request 已设(L1159)但 next 没检测到,SM 循环 llm_thinking ⇄ executing_tools。
        # 拿到下次复现日志后,确认 permission_request 在 next 时是否仍设 / 是否被清。
        _logger.debug(
            "🔍 [ExecutingToolsPhase.next] trigger=%s ctx.turn_ctx.id=%s permission_request=%s",
            trigger, id(ctx.turn_ctx),
            getattr(ctx.turn_ctx, "permission_request", "<no attr>"),
        )
        if ctx.turn_ctx.permission_request is not None:
            return ("permission_needed", AgentPhase.AWAITING_PERMISSION)
        return ("tools_done", AgentPhase.LLM_THINKING)


class FinalizingPhase(Phase):
    """FINALIZING 阶段:session 持久化 + audit + memory bridge extract。"""
    def enter(self, trigger: str, ctx: PhaseContext) -> Iterator[Event]:
        yield from self._chain.run(ctx.turn_ctx)

    def next(self, trigger: str, ctx: PhaseContext) -> tuple[str, AgentPhase]:
        return ("finalize_done", AgentPhase.DONE)


class InterruptedPhase(Phase):
    """INTERRUPTED 阶段:用户按 Stop 按钮 / Esc 触发的终态。

    行为:
    - 不做事(没有 chain)
    - 由 StateMachine.interrupt() 直接进入,不走 trigger
    - 终态,不能 transition 到其他 phase
    """
    def enter(self, trigger: str, ctx: PhaseContext) -> Iterator[Event]:
        return
        yield  # 显式空 generator

    def next(self, trigger: str, ctx: PhaseContext) -> tuple[str, AgentPhase]:
        raise InvalidTransition(
            "INTERRUPTED 是终态,不能 transition(请先 start_run 重置)"
        )


class DonePhase(Phase):
    """DONE 阶段:不做事,run 结束。chain 留空。"""
    def enter(self, trigger: str, ctx: PhaseContext) -> Iterator[Event]:
        return
        yield  # 显式空 generator

    def next(self, trigger: str, ctx: PhaseContext) -> tuple[str, AgentPhase]:
        raise InvalidTransition("DONE phase 不应再被 trigger")


# 延迟 import TurnChain(避免循环)在 Phase.__init__ 中处理(见 Phase 定义)
