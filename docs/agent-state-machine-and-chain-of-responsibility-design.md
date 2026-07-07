# ReactAgent 重构 v2:Phase-Class 状态机 + 强类型 Contract

> v2 重写日期:2026-06-29 | v1 → v2 改动:[§一 对照表](#一一-v1--v2-关键变更一览)
> v1 文档: [`agent-state-machine-and-chain-of-responsibility-design.v1.md`](agent-state-machine-and-chain-of-responsibility-design.v1.md)
> 待重构模块: [`agent_core/agent_core.py`](../agent_core/agent_core.py)
> 下游适配 UI: [`web/app.py`](../web/app.py)

---

## 〇、为什么这次重构

### 0.1 现状(2026-06-29)

[`agent_core/agent_core.py:755-1373`](../agent_core/agent_core.py#L755-L1373) 的 `run()` 是 619 行单方法 generator,塞了 6 类互不耦合的职责 + 6 个跨段共享的"隐性"局部变量(`last_turn` / `last_input_tokens` / `last_output_tokens` / `last_tool_calls` / `final_answer` / `final_stop_reason`)。

### 0.2 三个核心痛点

1. **UI 集成困难**: Streamlit 主线程在 `_ask_user_permission` 的 `Event.wait(0.1s)` 上阻塞 100ms 后默认 deny,`@st.dialog` 永远没机会 render(现场:[`logs/app/agent.log`](../logs/app/agent.log) 16:29:56 起,`wait_ms=103.5 → default deny`)。
2. **不可单测**: 无法单独测"单 turn 行为"或"permission 路径",必须跑完整 `run()`。
3. **扩展性差**: 加新阶段(答案验证 / rate limit 退避)必须改 `run()` 主体;加新处理(safety 二次审查 / cost tracking)必须改 `_step_one_turn` 主体。

### 0.3 目标

| # | 目标 | 收益 |
|---|---|---|
| 1 | **可暂停**:`run()` 从"一次性 generator" 改成"turn-by-turn 流式 generator" | Streamlit 集成修复,`wait_ms` 从 100ms → 秒级 |
| 2 | **可单测**:`Phase` / `Handler` / `Stage` 都是 class,可独立 mock | 测试覆盖度↑,bug 定位↓ |
| 3 | **可扩展**:状态机管阶段、职责链管子步骤、强类型 contract 管数据流 | 加新阶段/新处理**不动现有代码** |
| 4 | **可中断**:用户随时可点 Stop / 按 Esc 中断当前 run | 长任务可取消,LLM stream / tool subprocess 协作式停止 |
| 5 | **向后兼容**:公开 API `run()` / `resolve_permission()` 行为不变 | 300+ 测试 / `app_langgraph` / `00_Chat` 全不破 |

---

## 一、v1 → v2 关键变更一览

| 维度 | v1 | v2 | 解决哪个 v1 问题 |
|---|---|---|---|
| **StepResult** | `StepResult(events, is_done, awaiting_permission)` 三字段 dataclass | **删除** —— `step()` 是 generator,直接 `yield Event` | S1(组合歧义)、S6(流式 vs buffer 矛盾) |
| **`step()` 形态** | `def step(self) -> StepResult` 返 list[Event] | `def step(self) -> Iterator[Event]` 逐个 yield | S6 |
| **Phase 模型** | `PhaseTransition(from, trigger, handler, to)` 静态表 | `Phase` 是 class,`enter(trigger, ctx)` + `next(trigger, ctx)` 双向 API | I2(干活+转移混在一起)、S4(handler 拿不到 trigger) |
| **职责链应用范围** | 仅 `llm_thinking` phase 走 chain | **每个 phase 都有自己的 chain**(可空) | S2(不对称) |
| **Handler 形态** | `Callable[[TurnContext], None]` 顶层函数 | `Handler` class,`__init__` 注入依赖,`handle(ctx) -> HandlerResult` | I4(DI 不显式)、S3(TurnContext 字段缺失) |
| **Handler 间 contract** | 隐式(读 ctx.xxx 期望前面 handler 写好) | **强类型 stage dataclass** —— 每阶段一个 output data class | S8(隐式 contract 无检查) |
| **短路机制** | `if ctx.permission_request is not None: break` 硬编码 | `HandlerResult.stop_chain: bool` 通用 | I5(硬编码魔数) |
| **终止条件** | 散落在 handler 内部(`if turn >= max_turns`) | **`TerminationCondition` class 显式建模** | S5(死循环风险) |
| **状态分层** | 4 层(phase/ctx/run/self)边界不清 | **3 层 + 数据流图** —— turn / run / agent 各自的 dataclass | I3(边界不清) |
| **扩展点** | `extra_phase_transitions` / `extra_turn_handlers` 平行 API | **`AgentBuilder` + named hook point**(`after=` / `before=`) | I7(API 不一致)、I8(顺序问题) |
| **start_run 行为** | init + 立刻 trigger "run_started" | **纯 init** —— `step()` 第一次调用时自动 trigger | M6(不纯) |
| **is_done 形式** | `is_done()` method | `@property is_done` | M7 |
| **调试能力** | `self._sm._history` 是 list[tuple](不可读) | **`__repr__` 人话 + `on_enter` hook + `checkpoint()` API** | M5 |
| **3rd party 安全** | 无任何边界 | **白名单 event type + trusted/plugin 分级** | M4 |
| **UI race condition** | 未考虑 | **`session_state._active_run_id` 校验** | M3 |
| **handler 粒度** | 7 个不均 | **4 个** —— InputsPrepared / LLMInteraction / ToolProcessing / OutputPersisted | I6 |
| **INTERRUPTED 状态** | 无 | **新增** `INTERRUPTED` phase + cancel_event + UI Stop 按钮 + Esc 监听 | 用户可中断长任务,LLM stream / subprocess 协作式停止 |
| **§十一** | 列了 7 个"关联文档"(凑数) | **不写** —— 设计文档自洽,不强依赖 cross-reference | (本次审稿追加) |

**核心哲学变化**:
- v1 哲学:"把大函数拆成小函数 + 列表"
- **v2 哲学**:"让 **type system** 表达 **业务不变量**,每个 class 各管一件事,通过 **强类型 contract** 协作"

---

## 二、整体架构:Phase-Class 状态机 + 强类型 Stage

```
┌────────────────────────────────────────────────────────────┐
│  Layer 1 — 公开 API                                          │
│  ────────────────────────────────────────────────────────  │
│  • run(user_input) → Iterator[Event]      # 旧 API,保留     │
│  • start_run(user_input) → None           # 纯 init         │
│  • step() → Iterator[Event]               # 流式 yield      │
│  • resume_after_permission(choice) → None # 解除 ask 暂停   │
└────────────────────────────────────────────────────────────┘
                          ↓
┌────────────────────────────────────────────────────────────┐
│  Layer 2 — Phase-Class 状态机 + 强类型 Stage                 │
│  ────────────────────────────────────────────────────────  │
│  • StateMachine(phase dict + termination)                   │
│  • Phase ABC + 6 个 Phase 子类(enter + next 双向)          │
│  • 每个 Phase 有自己的 chain(可空)                            │
│  • TurnChain = Handler list(每个 Handler 是 class)          │
│  • Stage dataclass = handler 强类型 contract                │
└────────────────────────────────────────────────────────────┘
                          ↓
┌────────────────────────────────────────────────────────────┐
│  Layer 3 — 状态对象(3 层,数据流清晰)                         │
│  ────────────────────────────────────────────────────────  │
│  • TurnContext(per-stage 工作内存)                            │
│  • RunState(per-run 累积状态)                                │
│  • Agent State(per-agent 跨 run:self.messages / session)    │
└────────────────────────────────────────────────────────────┘
                          ↓
┌────────────────────────────────────────────────────────────┐
│  Layer 4 — Termination + Hook + Checkpoint                   │
│  ────────────────────────────────────────────────────────  │
│  • TerminationCondition(max_turns / final_answer)            │
│  • on_enter / on_exit hook(可挂 logging / metrics)           │
│  • checkpoint() API(序列化 _history 供 replay)              │
└────────────────────────────────────────────────────────────┘
```

### 数据流图(3 层状态的 transfer function)

```
┌─────────────── Agent State (self.*) ───────────────┐
│ self.messages          ← source of truth (LLM ctx)  │
│ self._session_manager  ← session 持久化              │
│ self.llm / self.tools  ← 依赖                        │
│ self._audit_logger     ← audit 通道                  │
└─────────────────────────────────────────────────────┘
                    │ snapshot
                    ▼
┌─────────────── RunState (per-run) ─────────────────┐
│ run_state.user_message                              │
│ run_state.turn                                      │
│ run_state.last_input_tokens / final_answer / ...    │
│ run_state.awaiting_permission                       │
│ run_state.termination_reason                        │
└─────────────────────────────────────────────────────┘
                    │ snapshot
                    ▼
┌─────────────── TurnContext (per-turn) ─────────────┐
│ turn_ctx.stage_inputs   (PreparedMessages)          │
│ turn_ctx.stage_outputs  (LLMResult / ...)            │
│ turn_ctx.events         (yielded by handlers)       │
└─────────────────────────────────────────────────────┘
                    │ promote
                    ▼
┌─────────────── Agent State (write-back) ──────────┐
│ self.messages.append(tool_result_block)             │
│ self._session_manager.add_*(...)                    │
│ self._audit_logger.log(...)                         │
└─────────────────────────────────────────────────────┘
```

每条箭头对应一个**显式 method**(`snapshot_messages` / `promote_to_run_state` / `append_to_messages`),不靠"散落各处的隐式 copy"。

---

## 三、状态机设计(Phase-Class 模型)

### 3.1 核心抽象

```python
# agent_core/agent_state.py
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Iterator, Optional, Type, Callable, Any


class AgentPhase(Enum):
    """agent 一次 run 的显式阶段。
    
    状态转移图(详见 §3.3):
        SETUP → LLM_THINKING ⇄ AWAITING_PERMISSION
                  ↓ ↑ ↓ ↓
                  ↓ EXECUTING_TOOLS
                  ↓
                FINALIZING → DONE
        
        (任意 phase ──interrupt──► INTERRUPTED → 终态)
    
    INTERRUPTED: 用户主动中断(Stop 按钮 / Esc)。
    - 任何 phase 接受 `StateMachine.interrupt()` 强制进入
    - 不做事,只产 interrupt event
    - 终态,不可再 trigger
    
    与 AWAITING_PERMISSION 的区别:
    - AWAITING_PERMISSION:正常流程,等用户对**工具调用**的决定 → 允许后继续
    - INTERRUPTED:用户主动取消整个 run,不再继续 LLM 循环 → 不可恢复
    """
    SETUP               = "setup"
    LLM_THINKING        = "llm_thinking"
    AWAITING_PERMISSION = "awaiting_permission"
    EXECUTING_TOOLS     = "executing_tools"
    FINALIZING          = "finalizing"
    INTERRUPTED         = "interrupted"
    DONE                = "done"


Event = tuple[str, Any]   # ("text", str) / ("tool_call", dict) / ...


class Phase(ABC):
    """agent 阶段基类。
    
    关键设计:
    - 每个 Phase 有自己的 chain(可空,空 chain 表示该 phase 不做事直接转出)
    - enter(trigger, ctx) 干完活,逐个 yield Event(流式!)
    - next(trigger, ctx) 决定下一 phase(可读 ctx.stage_outputs 决策)
    - StateMachine 把 trigger 事件路由到当前 phase 的 enter
    """
    
    def __init__(self, chain: "TurnChain" = None):
        self._chain = chain or TurnChain([])
    
    @property
    def name(self) -> str:
        return self.__class__.__name__
    
    @abstractmethod
    def enter(self, trigger: str, ctx: "PhaseContext") -> Iterator[Event]:
        """在当前 phase 干完活,逐个 yield Event。
        
        Args:
            trigger: 触发当前 phase enter 的事件名
            ctx: phase context(含 run_state / turn_ctx / events)
        
        Yields:
            阶段内产出的所有 events
        """
    
    @abstractmethod
    def next(self, trigger: str, ctx: "PhaseContext") -> tuple[str, AgentPhase]:
        """决定下一 phase。
        
        Returns:
            (next_trigger, next_phase) — StateMachine 调 trigger 转出
        """
    
    def __repr__(self) -> str:
        return f"<Phase {self.name}>"


class PhaseContext:
    """phase enter() 时的统一上下文。
    
    包含:
    - run_state: RunState(per-run)
    - turn_ctx: TurnContext(per-turn,每次 enter 重新创建)
    - termination: TerminationCondition(显式终止检查)
    - sm: StateMachine 自身(供 phase 转移用)
    """
    
    def __init__(
        self,
        run_state: "RunState",
        turn_ctx: "TurnContext",
        termination: "TerminationCondition",
        sm: "StateMachine",
    ):
        self.run_state = run_state
        self.turn_ctx = turn_ctx
        self.termination = termination
        self.sm = sm
```

### 3.2 StateMachine — 路由 + 终止

```python
class InvalidTransition(Exception):
    """当前 phase 不能被指定 trigger 触发。"""


class StateMachine:
    """agent 状态机:路由 trigger + 调 phase.enter + 调 phase.next + 终止检查。
    
    关键设计:
    - 转移表一次性注册,运行期不修改
    - trigger(event) 是 generator,逐个 yield phase.enter 的 events
    - 每次 trigger 前调 termination.check();命中终止 → 转 DONE
    - on_enter / on_exit hook 用于 logging / metrics(不污染 phase 业务代码)
    """
    
    def __init__(
        self,
        phases: dict[AgentPhase, Phase],
        initial: AgentPhase,
        termination: "TerminationCondition",
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
        return self._phase == AgentPhase.DONE
    
    @property
    def history(self) -> list[tuple[AgentPhase, str, AgentPhase]]:
        return list(self._history)
    
    def on_enter(self, callback: Callable[[AgentPhase, str], None]) -> None:
        """注册 phase enter hook(供 logging / metrics / debug)。"""
        self._on_enter_hooks.append(callback)
    
    def on_exit(self, callback) -> None:
        self._on_exit_hooks.append(callback)
    
    def trigger(self, event: str, ctx: PhaseContext) -> Iterator[Event]:
        """路由 trigger 到当前 phase。
        
        1. **INTERRUPTED 是终态**:任何 trigger 直接抛错(UI 应调 sm.interrupt 而非 trigger)
        2. 调 termination.check();命中 → 转 DONE,yield 终止 event
        3. 调当前 phase.enter(event, ctx) → yield 它产的所有 events
        4. 调 phase.next(event, ctx) → 拿到 (next_trigger, next_phase)
        5. 触发 on_exit / on_enter hooks
        6. 更新 _phase + _history
        7. 自动 trigger 下一 phase(self-trigger 链式推进)
        """
        # 0. INTERRUPTED 终态保护
        if self._phase == AgentPhase.INTERRUPTED:
            raise InvalidTransition("INTERRUPTED 是终态,不能 trigger(请先 start_run 重置)")
        
        # 1. 终止检查
        term_reason = self._termination.check(ctx.run_state, ctx.turn_ctx)
        if term_reason is not None:
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
            yield from self.trigger(next_trigger, ctx)
    
    def _terminate(self, reason: str, ctx: PhaseContext) -> Iterator[Event]:
        """转 DONE,产终止 event。"""
        ctx.run_state.termination_reason = reason
        yield ("system", f"⚠️ 终止:{reason}")
        self._history.append((self._phase, f"terminate:{reason}", AgentPhase.DONE))
        self._phase = AgentPhase.DONE
    
    def checkpoint(self) -> dict:
        """序列化当前状态供事后 replay/debug。"""
        return {
            "phase": self._phase.value,
            "history": [(p.value, t, n.value) for p, t, n in self._history],
        }
    
    def interrupt(self, ctx: PhaseContext) -> Iterator[Event]:
        """用户主动中断(Stop 按钮 / Esc):从任意 phase 强制转 INTERRUPTED。
        
        与正常 trigger 的区别:
        - 不调 phase.enter()(不干活)
        - 不调 phase.next()(不转移)
        - 走 cancel 协调机制,通知正在运行的 handler 立即停止
        - 立即转 INTERRUPTED 终态
        
        调用路径:
        - UI 端(stop button / Esc)调 `agent.interrupt()` → agent 内部设 cancel_event
        - 任何 handler 在下次循环检查时检测到 `ctx.run_state.cancel_event` → 走这个路径
        - step() 开始时检测到 cancel_event → 调 sm.interrupt(ctx) → 产 interrupt events
        
        关键:
        - `ctx.run_state.cancel_event.set()` 必须先于 yield,让 handler 下一轮迭代检查到
        - 幂等:多次调用只触发一次 phase 转移(转移到 INTERRUPTED 后再调用直接 return)
        """
        # 1. 通知正在运行的 handler 立即停止(LLM stream / subprocess / permission wait)
        ctx.run_state.cancel_event.set()
        
        # 2. 幂等:已中断,不再 transfer,不再 yield events
        if self._phase == AgentPhase.INTERRUPTED:
            return
            yield  # unreachable,但保留使函数为 generator
        
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
    
    def __repr__(self) -> str:
        if not self._history:
            return f"<SM current={self._phase.value}>"
        last_5 = self._history[-5:]
        lines = [f"  {p.value} --[{t}]--> {n.value}" for p, t, n in last_5]
        return (
            f"<SM current={self._phase.value} "
            f"history(last 5):\n" + "\n".join(lines) + ">"
        )
```

### 3.3 Phase 子类 — 7 个(6 + 1 个 INTERRUPTED 终态),各管各的

```python
# ── 1. SetupPhase ─────────────────────────────────────────
class SetupPhase(Phase):
    """SETUP 阶段:append user msg + L3 SM 决策 + ContextManager compact。"""
    
    def enter(self, trigger: str, ctx: PhaseContext) -> Iterator[Event]:
        if trigger != "run_started":
            return
        yield from self._chain.run(ctx.turn_ctx)  # 走 inputs_chain
    
    def next(self, trigger: str, ctx: PhaseContext) -> tuple[str, AgentPhase]:
        return ("llm_call", AgentPhase.LLM_THINKING)


# ── 2. LLMThinkingPhase(主循环) ───────────────────────────
class LLMThinkingPhase(Phase):
    """LLM_THINKING 阶段:调 LLM + 收 chunks + 决定下一步。
    
    三条出口:
    - "permission_needed" → AWAITING_PERMISSION
    - "llm_responded_final"(无 tool_call) → FINALIZING
    - "llm_responded_with_tools" → EXECUTING_TOOLS
    """
    
    def enter(self, trigger: str, ctx: PhaseContext) -> Iterator[Event]:
        yield from self._chain.run(ctx.turn_ctx)  # 走 llm_chain
    
    def next(self, trigger: str, ctx: PhaseContext) -> tuple[str, AgentPhase]:
        if ctx.turn_ctx.permission_request is not None:
            return ("permission_needed", AgentPhase.AWAITING_PERMISSION)
        if not ctx.turn_ctx.stage_outputs.tool_calls:
            ctx.run_state.final_answer = ctx.turn_ctx.stage_outputs.full_text
            ctx.run_state.final_stop_reason = ctx.turn_ctx.stage_outputs.stop_reason
            return ("llm_responded_final", AgentPhase.FINALIZING)
        return ("llm_responded_with_tools", AgentPhase.EXECUTING_TOOLS)


# ── 3. AwaitingPermissionPhase ────────────────────────────
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
        raise InvalidTransition(f"AwaitingPermission 不响应 trigger='{trigger}'")


# ── 4. ExecutingToolsPhase ───────────────────────────────
class ExecutingToolsPhase(Phase):
    """EXECUTING_TOOLS 阶段:执行 tool_call + 写 tool_result。"""
    
    def enter(self, trigger: str, ctx: PhaseContext) -> Iterator[Event]:
        yield from self._chain.run(ctx.turn_ctx)  # 走 tool_chain
        for tid, output in ctx.turn_ctx.stage_outputs.tool_results:
            ctx.run_state.agent.messages.append(
                _make_tool_result_block(tid, output)
            )
    
    def next(self, trigger: str, ctx: PhaseContext) -> tuple[str, AgentPhase]:
        return ("tools_done", AgentPhase.LLM_THINKING)


# ── 5. FinalizingPhase ───────────────────────────────────
class FinalizingPhase(Phase):
    """FINALIZING 阶段:bridge.on_turn_end + L3 SM extract + session flush。"""
    
    def enter(self, trigger: str, ctx: PhaseContext) -> Iterator[Event]:
        yield from self._chain.run(ctx.turn_ctx)  # 走 output_chain
    
    def next(self, trigger: str, ctx: PhaseContext) -> tuple[str, AgentPhase]:
        return ("finalize_done", AgentPhase.DONE)


# ── 6. DonePhase ─────────────────────────────────────────
class DonePhase(Phase):
    """DONE 阶段:不做事,run 结束。chain 留空。"""
    
    def enter(self, trigger: str, ctx: PhaseContext) -> Iterator[Event]:
        return
        yield
    
    def next(self, trigger: str, ctx: PhaseContext) -> tuple[str, AgentPhase]:
        raise InvalidTransition("DONE phase 不应再被 trigger")


# ── 7. InterruptedPhase(用户主动中断) ────────────────────
class InterruptedPhase(Phase):
    """INTERRUPTED 阶段:用户按 Stop 按钮 / Esc 触发的终态。
    
    行为:
    - 不做事(没有 chain)
    - 由 StateMachine.interrupt() 直接进入,不走 trigger
    - 终态,不能 transition 到其他 phase
    
    与 AWAITING_PERMISSION 的区别:
    - AWAITING_PERMISSION:正常流程,等用户对**工具调用**的决定 → 允许后继续
    - INTERRUPTED:用户主动取消整个 run,不再继续 LLM 循环 → 不可恢复
    
    与 DONE 的区别:
    - DONE:正常结束(LLM 给出 final answer 或 max_turns 触发)
    - INTERRUPTED:用户主动终止(可能 LLM 还在 stream、tool 还在跑)
    """
    
    def enter(self, trigger: str, ctx: PhaseContext) -> Iterator[Event]:
        return
        yield  # 显式空 generator
    
    def next(self, trigger: str, ctx: PhaseContext) -> tuple[str, AgentPhase]:
        raise InvalidTransition("INTERRUPTED 是终态,不能 transition(请先 start_run 重置)")
```

### 3.4 状态机实际组装

```python
# agent_core/agent.py
class ReactAgent:
    def __init__(
        self,
        ...,
        termination: TerminationCondition = None,
        inputs_chain: "TurnChain" = None,
        llm_chain: "TurnChain" = None,
        tool_chain: "TurnChain" = None,
        output_chain: "TurnChain" = None,
    ):
        # 默认 chain(4 类)
        inputs_chain = inputs_chain or build_default_inputs_chain(
            memory_retriever, system_prompt, tools,
        )
        llm_chain = llm_chain or build_default_llm_chain(llm, ...)
        tool_chain = tool_chain or build_default_tool_chain(
            permission_engine, tools, audit_logger,
        )
        output_chain = output_chain or build_default_output_chain(
            session_manager, audit_logger,
        )
        
        termination = termination or MaxTurnsTermination(max_turns=10)
        
        # 7 个 phase 实例(6 + INTERRUPTED 终态)
        phases = {
            AgentPhase.SETUP:               SetupPhase(inputs_chain),
            AgentPhase.LLM_THINKING:        LLMThinkingPhase(llm_chain),
            AgentPhase.AWAITING_PERMISSION: AwaitingPermissionPhase(),
            AgentPhase.EXECUTING_TOOLS:     ExecutingToolsPhase(tool_chain),
            AgentPhase.FINALIZING:          FinalizingPhase(output_chain),
            AgentPhase.INTERRUPTED:         InterruptedPhase(),
            AgentPhase.DONE:                DonePhase(),
        }
        self._sm = StateMachine(phases, initial=AgentPhase.SETUP, termination=termination)
```

---

## 四、职责链设计(Chain-of-Responsibility)

### 4.1 Handler 抽象

```python
# agent_core/turn_chain.py
from typing import Protocol, runtime_checkable


@dataclass
class HandlerResult:
    """单个 handler 的执行结果。"""
    stop_chain: bool = False            # True → 后续 handler 不跑
    next_action: Optional[str] = None   # 给 state machine 看的信号


@runtime_checkable
class Handler(Protocol):
    """职责链节点协议。
    
    实现要求:
    - __init__ 显式注入依赖(LLM / Tools / PermissionEngine 等)
    - handle(ctx) 只接 turn 上下文,不直接访问 self.xxx(agent 状态)
    - 通过 ctx.emit() / yield 产 events
    - 返 HandlerResult(stop_chain=True) 让 chain 短路
    """
    name: str  # 用于 named hook point(见 §十一)
    
    def handle(self, ctx: "TurnContext") -> HandlerResult: ...


@dataclass
class TurnContext:
    """per-turn 工作内存(handler 间共享)。"""
    run_state: "RunState"
    stage_inputs: Optional["StageInputs"] = None
    stage_outputs: Optional["LLMResult"] = None
    permission_request: Optional[dict] = None
    events: list[Event] = field(default_factory=list)
    _stopped: bool = False
    
    def emit(self, event: Event) -> None:
        self.events.append(event)
    
    def stop(self) -> None:
        self._stopped = True
    
    @property
    def is_stopped(self) -> bool:
        return self._stopped
```

### 4.2 TurnChain 执行器

```python
class TurnChain:
    """职责链执行器:按顺序跑 handlers,遇 stop_chain 提前终止。"""
    
    def __init__(self, handlers: list[Handler]):
        self._handlers = handlers
        self._name_to_idx = {h.name: i for i, h in enumerate(handlers)}
    
    def run(self, ctx: TurnContext) -> Iterator[Event]:
        """执行链:每个 handler 调一次,遇 stop_chain 停。"""
        for h in self._handlers:
            if ctx._stopped:
                break
            result = h.handle(ctx)
            for ev in ctx.events:
                yield ev
            ctx.events.clear()
            if result.stop_chain:
                break
    
    def add(self, handler: Handler, *, after: str = None, before: str = None, at: int = None) -> None:
        """named hook point:在指定 handler 之后/之前插入,或指定 index。"""
        if at is not None:
            self._handlers.insert(at, handler)
        elif after is not None:
            idx = self._name_to_idx[after]
            self._handlers.insert(idx + 1, handler)
        elif before is not None:
            idx = self._name_to_idx[before]
            self._handlers.insert(idx, handler)
        else:
            self._handlers.append(handler)
        self._name_to_idx = {h.name: i for i, h in enumerate(self._handlers)}
    
    def remove(self, name: str) -> None:
        self._handlers = [h for h in self._handlers if h.name != name]
        self._name_to_idx = {h.name: i for i, h in enumerate(self._handlers)}
    
    def __iter__(self):
        return iter(self._handlers)
    
    def __repr__(self) -> str:
        names = [h.name for h in self._handlers]
        return f"<TurnChain {' → '.join(names)}>"
```

### 4.3 4 个分类(每个一类 chain)

| Chain 名 | 涵盖 handler | 阶段归属 |
|---|---|---|
| `inputs_chain` | MemoryRetrieval + SystemPrompt + ToolsSchemaPrepare | SETUP |
| `llm_chain` | LLMCall + ChunkParse + **InlineXmlFallback** + LLMCallPersist | LLM_THINKING |
| `tool_chain` | PermissionCheck + ToolDispatch + ToolExecute | EXECUTING_TOOLS |
| `output_chain` | SessionPersist + AuditLog + MemoryBridgeExtract | FINALIZING |

> **003-react-inline-xml-fallback-parser 补充(2026-07-07)**:
> 某些 provider (GLM-5.1 等) 不通过结构化 `tool_use` 块而是通过纯文本中的
> inline-XML 表达工具调用:
>
>    <tool_call>{"name":"Bash","input":{"command":"echo hi"}}</tool_call>
>
> ChunkParseHandler 只会 emit 这种文本,不会解析它 → `stage_outputs.tool_calls` 为空
> → LLMThinkingPhase.next 走 FINALIZING → SM 提前收尾。
> InlineXmlFallbackHandler(`agent_core/turn_chain.py`)插在 ChunkParseHandler
> 后、LLMCallPersistHandler 前,扫 `stage_outputs.full_text` 抽 inline-XML 块
> 补回 `stage_outputs.tool_calls`,覆盖 `stop_reason="tool_use"`。详细设计见
> `specs/003-react-inline-xml-fallback-parser/{spec,plan,research,data-model}.md`。

### 4.4 内置 Handler(每个是 class)

```python
# ── 1. MemoryRetrievalHandler(inputs_chain) ───────────────
class MemoryRetrievalHandler:
    name = "memory_retrieval"
    
    def __init__(self, memory_retriever, memory_store):
        self._retriever = memory_retriever
        self._store = memory_store
    
    def handle(self, ctx: TurnContext) -> HandlerResult:
        if not self._retriever:
            return HandlerResult()
        last_user_msg = next(
            (m for m in reversed(ctx.stage_inputs.messages) if m.get("role") == "user"),
            None,
        )
        if not last_user_msg or not isinstance(last_user_msg.get("content"), str):
            return HandlerResult()
        try:
            report = self._retriever.retrieve(last_user_msg["content"])
            hits = report.hits if hasattr(report, "hits") else []
            if hits:
                mem_block = self._build_mem_block(hits)
                ctx.stage_inputs.messages = [
                    {"role": "system", "content": (ctx.stage_inputs.system_prompt or "") + mem_block}
                ] + [m for m in ctx.stage_inputs.messages if m.get("role") != "system"]
                for h in hits:
                    if hasattr(h, "rel_path") and h.rel_path:
                        ctx.run_state.surfaced_memories.add(h.rel_path)
            ctx.emit(("memory_status", {
                "hits": len(hits),
                "stored_total": self._count_stored(),
                "injected_tokens": self._estimate_tokens(hits),
                "zero_hit": len(hits) == 0,
            }))
        except Exception as e:
            ctx.run_state.logger.warning(f"Memory retrieval failed: {e}")
        return HandlerResult()
    
    def _build_mem_block(self, hits) -> str: ...
    def _count_stored(self) -> int: ...
    def _estimate_tokens(self, hits) -> int: ...


# ── 2. SystemPromptHandler(inputs_chain) ──────────────────
class SystemPromptHandler:
    name = "system_prompt"
    
    def __init__(self, system_prompt: str):
        self._system_prompt = system_prompt
    
    def handle(self, ctx: TurnContext) -> HandlerResult:
        if not self._system_prompt:
            return HandlerResult()
        msgs = ctx.stage_inputs.messages
        if not any(m.get("role") == "system" for m in msgs):
            msgs.insert(0, {"role": "system", "content": self._system_prompt})
        return HandlerResult()


# ── 3. ToolsSchemaPrepareHandler(inputs_chain) ────────────
class ToolsSchemaPrepareHandler:
    name = "tools_schema_prepare"
    
    def __init__(self, tools_registry, provider_detector):
        self._tools = tools_registry
        self._provider_detector = provider_detector
    
    def handle(self, ctx: TurnContext) -> HandlerResult:
        ctx.stage_inputs.tool_schemas = self._tools.list_schemas(
            provider=self._provider_detector()
        )
        return HandlerResult()


# ── 4. LLMCallHandler(llm_chain) ──────────────────────────
class LLMCallHandler:
    name = "llm_call"
    
    def __init__(self, llm, cache_namespace_fn: Callable[[], str]):
        self._llm = llm
        self._cache_ns_fn = cache_namespace_fn
    
    def handle(self, ctx: TurnContext) -> HandlerResult:
        ctx.run_state.logger.debug("📤 【发送给 LLM】Turn %d", ctx.run_state.turn)
        try:
            # cancel_event:用户点 Stop 时,LLM SDK 主动 close stream
            chunks = self._llm.chat(
                messages=ctx.stage_inputs.messages,
                tools=ctx.stage_inputs.tool_schemas or None,
                cache_namespace=self._cache_ns_fn(),
                cancel_event=ctx.run_state.cancel_event,  # LLM SDK 内部监听
            )
        except Exception as e:
            error_msg = f"LLM 调用失败: {type(e).__name__}: {e}"
            ctx.run_state.logger.error(error_msg)
            ctx.emit(("system", f"❌ {error_msg}"))
            ctx.emit(("text", f"抱歉,遇到了技术问题无法回答:{error_msg}"))
            ctx.emit(("system", "✅ 回答完成"))
            ctx.stage_outputs = LLMResult(
                chunks=[], full_text="", thinking_text="",
                tool_calls=[], usage=None, stop_reason="error",
            )
            return HandlerResult(stop_chain=True)
        
        ctx.stage_outputs = LLMResult(
            chunks=list(chunks),
            full_text="", thinking_text="",
            tool_calls=[], usage=None, stop_reason=None,
        )
        return HandlerResult()


# ── 5. ChunkParseHandler(llm_chain) ───────────────────────
class ChunkParseHandler:
    name = "chunk_parse"
    
    def handle(self, ctx: TurnContext) -> HandlerResult:
        """流式解析 LLM chunks,emit text/thinking/tool_call/usage events。
        
        Cancel 协作:
        - 每个 chunk 处理前检查 ctx.run_state.cancel_event
        - 已中断 → 立即停止处理(已积累的 text/thinking 不丢弃,会 yield 给 UI)
        - 返回 HandlerResult(stop_chain=True) 阻止后续 handler 跑
        """
        result = ctx.stage_outputs
        try:
            for chunk in result.chunks:
                # ── 中断检查 ──
                if ctx.run_state.cancel_event.is_set():
                    ctx.run_state.logger.debug("🛑 [chunk_parse] cancel_event detected, stop iterating")
                    result.stop_reason = "interrupted"
                    return HandlerResult(stop_chain=True)
                
                if chunk.text_delta:
                    result.full_text += chunk.text_delta.text
                    ctx.emit(("text", chunk.text_delta.text))
                if chunk.thinking_delta:
                    result.thinking_text += chunk.thinking_delta.thinking
                    ctx.run_state.pending_thinking += chunk.thinking_delta.thinking
                    ctx.emit(("thinking", chunk.thinking_delta.thinking))
                if chunk.tool_call:
                    result.tool_calls.append(chunk.tool_call)
                if getattr(chunk, "stop_reason", None):
                    result.stop_reason = chunk.stop_reason
                if chunk.usage:
                    result.usage = chunk.usage
                    ctx.run_state.last_input_tokens = chunk.usage.input_tokens
                    ctx.run_state.last_output_tokens = chunk.usage.output_tokens
                    ctx.emit(("usage", chunk.usage))
                    if ctx.run_state.context_manager:
                        ctx.run_state.context_manager.set_baseline(
                            chunk.usage.input_tokens,
                            len(ctx.run_state.agent.messages),
                        )
        except Exception as e:
            error_msg = f"LLM 流式响应中断: {type(e).__name__}: {e}"
            ctx.run_state.logger.error(error_msg)
            ctx.emit(("system", f"❌ {error_msg}"))
            ctx.emit(("text", f"抱歉,响应被中断:{error_msg}"))
            ctx.emit(("system", "✅ 回答完成"))
            result.stop_reason = "error"
        return HandlerResult()


# ── 6. PermissionCheckHandler(tool_chain,首位) ───────────
class PermissionCheckHandler:
    name = "permission_check"
    
    def __init__(self, permission_engine, ask_user_permission_fn):
        """ask_user_permission_fn:非阻塞,返 'ALLOW' / 'DENY_BY_HOOK' / 'AWAITING_PERMISSION'。"""
        self._engine = permission_engine
        self._ask_fn = ask_user_permission_fn
    
    def handle(self, ctx: TurnContext) -> HandlerResult:
        """permission 决策 + 短路控制。
        
        决策 vs 短路:
        - "ALLOW"         → 不短路,继续 chain
        - "DENY_BY_HOOK"  → 不短路,继续 chain(emit deny tool_result,SessionPersist 落盘)
        - "AWAITING_PERMISSION" → 短路,emit awaiting_permission,stage 暂停
        
        Cancel 协作:
        - 等用户决定时(AWAITING_PERMISSION 后)检查 cancel_event
        - 用户取消 → 清空 awaiting_permission,转 INTERRUPTED(由 sm.interrupt() 处理)
        - 注意:PermissionCheckHandler 自身不能 stop_chain 取消 — 它只短路
          真正转 INTERRUPTED 由 step() 检测到 cancel_event 后调 sm.interrupt()
        """
        if not ctx.stage_outputs.tool_calls:
            return HandlerResult()
        
        for tc in ctx.stage_outputs.tool_calls:
            # ── 取消检查 ──
            if ctx.run_state.cancel_event.is_set():
                ctx.run_state.logger.debug(
                    "🛑 [permission_check] cancel_event detected, abort"
                )
                return HandlerResult(stop_chain=True)
            
            decision = self._engine.check(tc.tool_name, tc.tool_input)
            sentinel = self._ask_fn(tc.tool_name, tc.tool_input, decision)
            if sentinel == "AWAITING_PERMISSION":
                ctx.permission_request = {
                    "tool_name": tc.tool_name,
                    "tool_input": tc.tool_input,
                    "tool_use_id": tc.tool_use_id,
                    "reason": getattr(decision.decision_reason, "reason", ""),
                    "message": decision.message or "",
                }
                ctx.emit(("awaiting_permission", ctx.permission_request))
                return HandlerResult(stop_chain=True)
            elif sentinel == "DENY_BY_HOOK":
                tool_output = decision.message or "Permission denied"
                ctx.emit(("tool_result", {
                    "name": tc.tool_name,
                    "output": tool_output,
                    "success": False,
                    "elapsed": 0.0,
                }))
                ctx.run_state.pending_tool_logs.append({
                    "type": "result", "name": tc.tool_name,
                    "output": tool_output, "success": False,
                })
                ctx.stage_outputs.tool_results.append((tc.tool_use_id, tool_output))
        return HandlerResult()


# ── 7. ToolDispatchHandler(tool_chain) ────────────────────
class ToolDispatchHandler:
    name = "tool_dispatch"
    
    def __init__(self, tools_registry):
        self._tools = tools_registry
    
    def handle(self, ctx: TurnContext) -> HandlerResult:
        if not ctx.stage_outputs.tool_calls or ctx.permission_request:
            return HandlerResult()
        tool_calls = ctx.stage_outputs.tool_calls
        if len(tool_calls) == 1:
            tc = tool_calls[0]
            ctx.emit(("tool_call", {"name": tc.tool_name, "input": tc.tool_input, "parallel": False}))
            ctx.run_state.pending_tool_logs.append({
                "type": "action", "name": tc.tool_name, "input": tc.tool_input,
            })
        else:
            tool_names = [tc.tool_name for tc in tool_calls]
            ctx.emit(("tool_call", {"names": tool_names, "parallel": True}))
            ctx.run_state.pending_tool_logs.append({
                "type": "parallel_start", "names": tool_names,
            })
        return HandlerResult()


# ── 8. ToolExecuteHandler(tool_chain) ─────────────────────
class ToolExecuteHandler:
    name = "tool_execute"
    
    def __init__(self, tools_registry):
        self._tools = tools_registry
    
    def handle(self, ctx: TurnContext) -> HandlerResult:
        """单工具串行 / 多工具并行(原 L1135-1258 段,1:1 搬)。
        
        Cancel 协作:
        - 进入 handle 时检查 cancel_event,已中断直接返回
        - 串行执行:每个工具执行前 + 等待中检查 cancel_event
        - 并行执行:启动每个子进程后,在结果收集循环中检查 cancel_event
        - 中断时:terminate 当前正在跑的 subprocess,skip 剩下的
        """
        # ── 入口检查 ──
        if ctx.run_state.cancel_event.is_set():
            ctx.run_state.logger.debug("🛑 [tool_execute] cancel_event detected, skip")
            return HandlerResult(stop_chain=True)
        
        tool_calls = ctx.stage_outputs.tool_calls
        if not tool_calls or ctx.permission_request:
            return HandlerResult()
        if len(tool_calls) == 1:
            self._execute_serial(tool_calls[0], ctx)
        else:
            self._execute_parallel(tool_calls, ctx)
        return HandlerResult()
    
    def _execute_serial(self, tc, ctx):
        tool = self._tools.get(tc.tool_name)
        process = tool.run(tc.tool_input)  # Popen 实例
        # 等待中检查 cancel_event
        while process.poll() is None:
            if ctx.run_state.cancel_event.is_set():
                process.terminate()
                try:
                    process.wait(timeout=0.5)
                except subprocess.TimeoutExpired:
                    process.kill()  # 兜底
                ctx.run_state.logger.info(
                    "🛑 [tool_execute_serial] cancelled tool=%s",
                    tc.tool_name,
                )
                return
            time.sleep(0.05)
        # ... 处理 output / emit event
    
    def _execute_parallel(self, tool_calls, ctx):
        processes = {}
        for tc in tool_calls:
            tool = self._tools.get(tc.tool_name)
            processes[tc.tool_use_id] = (tc, tool.run(tc.tool_input))
        # 等待所有 + cancel 监控
        while processes:
            if ctx.run_state.cancel_event.is_set():
                for tid, (tc, p) in processes.items():
                    p.terminate()
                for tid, (tc, p) in processes.items():
                    try:
                        p.wait(timeout=0.5)
                    except subprocess.TimeoutExpired:
                        p.kill()
                ctx.run_state.logger.info(
                    "🛑 [tool_execute_parallel] cancelled %d tools",
                    len(processes),
                )
                return
            for tid, (tc, p) in list(processes.items()):
                if p.poll() is not None:
                    # ... 处理 output / emit event
                    del processes[tid]
            time.sleep(0.05)


# ── 9. SessionPersistHandler(output_chain) ────────────────
class SessionPersistHandler:
    name = "session_persist"
    
    def __init__(self, session_manager):
        self._session = session_manager
    
    def handle(self, ctx: TurnContext) -> HandlerResult:
        """Claude Code 风格:assistant+tool_use 一条 Entry,tool_results 一条 Entry。"""
        if not self._session or not ctx.stage_outputs.tool_calls:
            return HandlerResult()
        try:
            tc_list = [{"id": tc.tool_use_id, "name": tc.tool_name, "input": tc.tool_input}
                       for tc in ctx.stage_outputs.tool_calls]
            self._session.add_assistant_with_tools(
                text=ctx.stage_outputs.full_text,
                tool_calls=tc_list,
            )
            results = [{"tool_use_id": tid, "content": output}
                       for tid, output in ctx.stage_outputs.tool_results]
            self._session.add_tool_results(results)
        except Exception as e:
            ctx.run_state.logger.warning(f"Failed to save intermediate turn to session: {e}")
        finally:
            ctx.stage_outputs.tool_results = []
        return HandlerResult()


# ── 10. AuditLogHandler(output_chain) ─────────────────────
class AuditLogHandler:
    name = "audit_log"
    
    def __init__(self, audit_logger_getter: Callable[[], Optional["AuditLogger"]]):
        self._get_logger = audit_logger_getter
    
    def handle(self, ctx: TurnContext) -> HandlerResult:
        al = self._get_logger()
        if al is None:
            return HandlerResult()
        for tc in ctx.stage_outputs.tool_calls:
            pass  # 构造 AuditRecord + al.log()
        return HandlerResult()


# ── 11. MemoryBridgeExtractHandler(output_chain) ─────────
class MemoryBridgeExtractHandler:
    name = "memory_bridge_extract"
    
    def __init__(self, react_memory_bridge):
        self._bridge = react_memory_bridge
    
    def handle(self, ctx: TurnContext) -> HandlerResult:
        """Run 末尾触发 bridge.on_turn_end(Bug 1e:final_answer 非空且 stop_reason 正常才调)。"""
        run_state = ctx.run_state
        if (self._bridge
            and run_state.turn > 0
            and run_state.final_answer
            and run_state.user_message
            and run_state.final_stop_reason not in _TRUNCATED_STOP_REASONS):
            try:
                for event in self._bridge.on_turn_end(
                    user_msg=run_state.user_message,
                    assistant_resp=run_state.final_answer,
                    turn_index=run_state.turn,
                    input_tokens=run_state.last_input_tokens,
                    output_tokens=run_state.last_output_tokens,
                    tool_calls_in_turn=len(run_state.last_tool_calls),
                ):
                    ctx.emit(("memory_event", event))
            except Exception as e:
                run_state.logger.warning(f"Memory bridge failed: {e}")
        return HandlerResult()
```

---

## 五、TurnContext 与 Stage Data Classes(强类型 Contract)

### 5.1 为什么用 Data Class

**v1 问题**:handler 间靠 `ctx.tool_schemas` / `ctx.tool_calls` 等**字段名约定**传递数据,重排 handler 顺序后**静默失败**(空 list 不会报错)。

**v2 解法**:每阶段一个**强类型 output data class**,handler 签名显式声明输入输出。

### 5.2 Stage Data Classes

```python
# agent_core/stages.py
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class StageInputs:
    """inputs_chain 的输出(被 llm_chain 消费)。"""
    messages: list[dict]
    system_prompt: Optional[str] = None
    tool_schemas: list[dict] = field(default_factory=list)


@dataclass
class LLMResult:
    """llm_chain 的输出(被 tool_chain 消费)。"""
    chunks: list                          # 原始 LLM chunks
    full_text: str = ""
    thinking_text: str = ""
    tool_calls: list = field(default_factory=list)
    tool_results: list[tuple[str, str]] = field(default_factory=list)  # (tool_use_id, output)
    usage: Optional["Usage"] = None
    stop_reason: Optional[str] = None


@dataclass
class ToolExecutionResult:
    """tool_chain 的输出(被 output_chain 消费)。"""
    tool_calls: list
    tool_results: list[tuple[str, str]]   # (tool_use_id, output)
    success_count: int
    error_count: int
    total_elapsed: float
```

**类型系统强制**:
- `ToolExecuteHandler.handle(self, ctx: TurnContext) -> HandlerResult` 读 `ctx.stage_outputs.tool_calls`,`stage_outputs` 必须是 `LLMResult` 或兼容类型。
- llm_chain 没跑完(没填 `stage_outputs`),tool_chain 跑就**直接 AttributeError**——bug 立刻暴露,不是静默失败。

### 5.3 RunState — per-run 累积状态(含 cancel_event)

```python
# agent_core/run_state.py
import logging
import threading
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class RunState:
    """per-run 累积状态(生命周期:start_run → DONE / INTERRUPTED / ERROR)。
    
    vs Agent State (self.*):
    - self.messages / self.tools / self.llm → 跨 run 持久(agent 实例级)
    - RunState → 单次 run 内累积(run 级)
    
    vs TurnContext:
    - TurnContext → per-turn 工作内存(handler 间共享)
    - RunState → 整个 run 跨 turn 持久(turn 间共享)
    
    关键字段:
    - cancel_event:用户中断信号(Stop 按钮 / Esc 触发)
    - turn:当前 turn 计数
    - last_input_tokens / last_output_tokens:最近一次 LLM 调用的 token 用量
    - final_answer / final_stop_reason:run 终态时的最终答案
    - pending_tool_logs:还没 flush 到 session 的 tool 日志
    - awaiting_permission:run 暂停时等用户决定(与 AWAITING_PERMISSION phase 配套)
    - termination_reason:正常终止的原因(max_turns / explicit_abort / ...)
    """
    cancel_event: threading.Event = field(default_factory=threading.Event)
    turn: int = 0
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
    agent: Optional["ReactAgent"] = None  # back-ref 供 handler 读 self.xxx
```

**cancel_event 用法**:
```python
# UI 端:Stop 按钮 / Esc 触发
agent._run_state.cancel_event.set()  # 通知所有 handler 准备停止

# Handler 端:每轮循环检查
def handle(self, ctx: TurnContext) -> HandlerResult:
    for chunk in ctx.stage_outputs.chunks:
        if ctx.run_state.cancel_event.is_set():  # 协作式停止
            return HandlerResult(stop_chain=True)
        # ... 处理 chunk
    return HandlerResult()
```

---

## 六、Handler 是 Class,显式 DI

### 6.1 v1 vs v2 handler 形态

```python
# ── v1:顶层 function,DI 不显式 ──
def llm_call_handler(ctx: TurnContext) -> None:
    llm_chunks = ctx.llm.chat(...)  # ctx.llm 哪里来?
    # 测:必须造完整 ctx(llm/tools/perm/...)才能测

# ── v2:class,__init__ 注入依赖 ──
class LLMCallHandler:
    name = "llm_call"
    
    def __init__(self, llm, cache_namespace_fn: Callable[[], str]):
        self._llm = llm
        self._cache_ns_fn = cache_namespace_fn
    
    def handle(self, ctx: TurnContext) -> HandlerResult:
        chunks = self._llm.chat(
            messages=ctx.stage_inputs.messages,
            tools=ctx.stage_inputs.tool_schemas or None,
            cache_namespace=self._cache_ns_fn(),
        )
        # 测:LLMCallHandler(mock_llm, lambda: "test") → 直接 handle(ctx) → assert
```

### 6.2 Handler 协议

```python
class Handler(Protocol):
    """handler 必须实现:
    - name: str(用于 named hook point)
    - handle(ctx: TurnContext) -> HandlerResult
    """
    name: str
    
    def handle(self, ctx: TurnContext) -> HandlerResult: ...


# 验证某个 class 是合法 handler
assert isinstance(LLMCallHandler(llm, ns_fn), Handler)
```

### 6.3 单元测试示例

```python
def test_llm_call_handler_emits_text_chunks():
    """LLMCallHandler 应该把 LLM 流式 chunks 累积到 stage_outputs,emit text events。"""
    mock_llm = MockLLM(chunks=[TextChunk("hello "), TextChunk("world")])
    handler = LLMCallHandler(mock_llm, cache_namespace_fn=lambda: "test_ns")
    ctx = TurnContext(
        run_state=RunState(...),
        stage_inputs=StageInputs(messages=[{"role": "user", "content": "hi"}]),
    )
    
    result = handler.handle(ctx)
    
    assert not result.stop_chain
    assert ctx.stage_outputs.full_text == ""  # ChunkParseHandler 才会填这个
    assert len(ctx.stage_outputs.chunks) == 2
```

---

## 七、Termination 显式建模

### 7.1 TerminationCondition 抽象

```python
class TerminationCondition(ABC):
    """agent run 终止条件。
    
    显式建模 vs 散落 if:
    - 散落 if:每个 handler 自己 check,容易漏
    - 显式 TerminationCondition:StateMachine 每次 trigger 前统一 check
    """
    
    @abstractmethod
    def check(self, run_state: "RunState", turn_ctx: TurnContext) -> Optional[str]:
        """返 None = 继续;返 str = 终止原因(转 DONE)。"""


class MaxTurnsTermination:
    """达到 max_turns 强制终止。"""
    
    def __init__(self, max_turns: int):
        self._max = max_turns
    
    def check(self, run_state, turn_ctx) -> Optional[str]:
        if run_state.turn >= self._max:
            return f"max_turns_reached ({self._max})"
        return None


class CompositeTermination:
    """多个 termination 组合,任一触发即终止。"""
    
    def __init__(self, *conditions: TerminationCondition):
        self._conditions = conditions
    
    def check(self, run_state, turn_ctx) -> Optional[str]:
        for c in self._conditions:
            reason = c.check(run_state, turn_ctx)
            if reason:
                return reason
        return None
```

### 7.2 StateMachine 集成

```python
class StateMachine:
    def trigger(self, event, ctx):
        # 终止检查(在 phase.enter 之前)
        term_reason = self._termination.check(ctx.run_state, ctx.turn_ctx)
        if term_reason is not None:
            yield from self._terminate(term_reason, ctx)
            return
        # ... phase.enter + phase.next
```

### 7.3 默认 termination 组合

```python
def build_default_termination(max_turns: int = 10) -> TerminationCondition:
    return CompositeTermination(
        MaxTurnsTermination(max_turns),
        # 以后可加:TimeoutTermination / TokenBudgetTermination / ExplicitAbortTermination
    )
```

---

## 八、`step()` 是 Generator,流式 Yield

### 8.1 v1 错误设计

```python
# v1:step() 返 StepResult,events 一次性 buffer
def step(self) -> StepResult:
    events = []
    while not self._sm.is_done():
        turn_events = self._sm.trigger(...)  # 返 list
        for ev in turn_events:
            events.append(ev)  # ← buffer
    return StepResult(events=events)  # ← 一次性返
```

### 8.2 v2 正确设计

```python
class ReactAgent:
    def step(self) -> Iterator[Event]:
        """流式推进:逐个 yield events,遇 awaiting_permission 暂停。"""
        if self._run_state is None:
            raise RuntimeError("必须先调 start_run()")
        
        # 第一次进入:从 SETUP 触发 "run_started"
        if self._sm.current == AgentPhase.SETUP and not self._sm.history:
            yield from self._drive("run_started")
            return
        
        # resume_after_permission 后:从 AWAITING_PERMISSION 触发 "permission_resolved"
        yield from self._drive()
    
    def _drive(self, trigger_event: str = None) -> Iterator[Event]:
        """驱 state machine 链式 trigger,直到暂停点 / 终止。"""
        ctx = PhaseContext(
            run_state=self._run_state,
            turn_ctx=self._new_turn_ctx(),
            termination=self._termination,
            sm=self._sm,
        )
        
        if trigger_event:
            yield from self._sm.trigger(trigger_event, ctx)
        else:
            yield from self._sm.trigger("permission_resolved", ctx)
        
        if ctx.turn_ctx.permission_request is not None:
            self._run_state.awaiting_permission = ctx.turn_ctx.permission_request
        else:
            self._run_state.awaiting_permission = None
    
    def resume_after_permission(self, choice: str) -> None:
        """UI 决定后调:写入 choice,允许下次 step() 继续。"""
        if self._run_state is None or not self._run_state.awaiting_permission:
            raise RuntimeError("当前没有 awaiting_permission 状态")
        self._run_state.awaiting_permission["choice"] = choice
```

### 8.3 流式 vs 暂停点的协同

```python
# web/app.py 调用方式
result = agent.step()        # ← generator,不是 list
for ev in result:            # ← 流式
    if ev[0] == "text":
        st.write(ev[1])      # 实时显示
    elif ev[0] == "awaiting_permission":
        st.session_state._run_phase = "awaiting_permission"
        st.session_state._pending = ev[1]
        st.rerun()
        return  # ← step() 提前结束(generator 暂停语义)
```

**关键**:`step()` 返 generator,UI 端 `for ev in result:` 流式处理;遇 `awaiting_permission` → 提前 `return`(generator 暂停,但 state machine 状态保留在 self._sm);UI 调 `resume_after_permission` 后下次 `step()` 继续推。

---

## 九、`_ask_user_permission` 非阻塞 + Sentinel

### 9.1 v1 vs v2

```python
# ── v1:阻塞,主线程被 Event.wait 占用 ──
def _ask_user_permission(self, tool_name, tool_input, decision) -> str:
    self._pending_permission_request = {...}
    got_response = self._permission_resolved.wait(timeout=0.1)  # ← 阻塞 100ms
    if not got_response:
        return "deny"  # ← 默认 deny
    return self._pending_permission_request.get("choice", "deny")

# ── v2:非阻塞,返 sentinel ──
def _ask_user_permission(self, tool_name, tool_input, decision) -> str:
    """返 'ALLOW' / 'DENY_BY_HOOK' / 'AWAITING_PERMISSION' 三态 sentinel。
    
    'AWAITING_PERMISSION' → 调用方(PermissionCheckHandler)立即
    短路 + emit awaiting_permission event,主线程不再被 Event.wait 阻塞。
    """
    hook_decision = self._run_permission_request_hook(tool_name, tool_input)
    if hook_decision == "allow":
        return "ALLOW"
    if hook_decision == "deny":
        return "DENY_BY_HOOK"
    
    self._pending_permission_request = {
        "tool_name": tool_name,
        "tool_input": tool_input,
        "tool_use_id": getattr(decision, "tool_use_id", None),
        "reason": getattr(decision.decision_reason, "reason", ""),
        "message": decision.message or "",
    }
    if self._permission_resolved is None:
        self._permission_resolved = threading.Event()
    return "AWAITING_PERMISSION"
```

### 9.2 调用方(PermissionCheckHandler)处理

```python
class PermissionCheckHandler:
    def handle(self, ctx: TurnContext) -> HandlerResult:
        for tc in ctx.stage_outputs.tool_calls:
            decision = self._engine.check(tc.tool_name, tc.tool_input)
            sentinel = self._ask_fn(tc.tool_name, tc.tool_input, decision)
            if sentinel == "AWAITING_PERMISSION":
                ctx.permission_request = {...}
                ctx.emit(("awaiting_permission", ctx.permission_request))
                return HandlerResult(stop_chain=True)  # ← 短路
            elif sentinel == "DENY_BY_HOOK":
                # emit deny tool_result,继续 chain(SessionPersist 落盘 deny)
                ...
        return HandlerResult()
```

### 9.3 Resume 路径

```python
# UI 点 Allow → 调 agent.resume_after_permission("allow")
agent.resume_after_permission("allow")
# 内部:
#   1. 写入 choice 到 _pending_permission_request["choice"]
#   2. 设置 _permission_resolved Event(供 legacy 用)
# 下次 step():
#   1. state machine trigger("permission_resolved", ctx)
#   2. AwaitingPermissionPhase.next() 返 ("execute_tools", EXECUTING_TOOLS)
#   3. EXECUTING_TOOLS 走 tool_chain(执行 tool_call)
```

---

## 十、向下游 `web/app.py` 的契约

### 10.1 新 API 形态

```python
# web/app.py — 简化版伪代码
agent = st.session_state.agent

# 1. 用户发消息
if prompt := st.chat_input("..."):
    # session_state 校验:无 active run
    if st.session_state.get("_active_run_id") is not None:
        st.error("已有 run 在进行中,请先完成或取消")
        st.stop()
    
    st.session_state.messages.append({"role": "user", "content": prompt})
    st.session_state._active_run_id = uuid.uuid4().hex
    st.session_state._run_accum = {"text": "", "thinking": "", "turn": 0, ...}
    
    agent.start_run(prompt)
    
    st.rerun()

# 2. 顶部"run 推进段"在每次 rerun 跑
if st.session_state.get("_active_run_id") is not None:
    try:
        for ev in agent.step():    # ← generator,流式
            _apply_event_to_accum(ev, st.session_state._run_accum)
            if ev[0] == "awaiting_permission":
                st.session_state._run_phase = "awaiting_permission"
                st.session_state._pending = ev[1]
                st.rerun()
                return
        st.session_state._active_run_id = None
        st.session_state._run_phase = "idle"
        st.rerun()
    except Exception as e:
        st.error(f"Run failed: {e}")
        st.session_state._active_run_id = None
        st.session_state._run_phase = "idle"

# 3. Dialog click handler
elif st.session_state.get("_run_phase") == "awaiting_permission":
    pending = st.session_state._pending
    
    @st.dialog("🔐 权限请求")
    def _ask_dialog():
        st.write(f"Tool: {pending['tool_name']}")
        st.json(pending["tool_input"])
        col1, col2, col3 = st.columns(3)
        with col1:
            if st.button("✅ Allow once"):
                agent.resume_after_permission("allow")
                st.session_state._run_phase = "running"
                st.rerun()
        with col2:
            if st.button("❌ Deny"):
                agent.resume_after_permission("deny")
                st.session_state._run_phase = "running"
                st.rerun()
        with col3:
            if st.button("✅ Always Allow"):
                agent.resume_after_permission("allow_session")
                st.session_state._run_phase = "running"
                st.rerun()
    
    _ask_dialog()
```

### 10.2 状态机 × Streamlit Rerun 时序

```
Streamlit rerun N+1                        Streamlit rerun N+2
        │                                          │
        ▼                                          ▼
┌──────────────────┐                 ┌──────────────────┐
│ _active_run_id:  │                 │ _run_phase:      │
│   <uuid>         │  step()         │ "awaiting_perm"  │
│ _run_phase:      │  yields:        │                  │
│   "running"      │ ─────────►     │ @st.dialog 渲染  │
│                  │  ("text", ...)  │ 用户点 Allow     │
│ for ev in        │  ("awaiting_   │   → resume_after │
│   agent.step():  │   permission", │     _permission  │
│   ...            │   {...})       │   → phase=running│
│ st.rerun()       │                 │   → st.rerun()  │
└──────────────────┘                 └──────────────────┘
```

### 10.3 Race Condition 处理

```python
# session_state 校验:防止"用户连续输入两条 prompt 覆盖 _active_run_id"
if st.session_state.get("_active_run_id") is not None:
    st.error("上一轮对话还未完成,请先回复或取消")
    st.stop()
```

### 10.4 Stop 按钮 + Esc 监听(INTERRUPTED 触发入口)

**核心思路**:用户随时可中断 run,触发 `agent.interrupt()`,LLM stream / subprocess 协作式停止,状态机转 INTERRUPTED 终态。

```python
# ── 在 run 推进段旁加 Stop 按钮 ──
if st.session_state.get("_active_run_id") is not None:
    col_run, col_stop = st.columns([0.85, 0.15])
    
    with col_stop:
        # 关键:Stop 按钮总是可点(即使在 awaiting_permission 时也能中断)
        if st.button("⏹ 停止", key="stop_button", help="中断当前对话(等价于按 Esc)"):
            _interrupt_run(agent)
            st.rerun()
    
    with col_run:
        # ... 原有 run 推进逻辑(text_placeholder / thinking_placeholder / ...)
        for ev in agent.step():
            _apply_event_to_accum(ev, st.session_state._run_accum)
            
            # ── 检查 interrupt events ──
            if ev[0] == "system" and "已被用户中断" in ev[1]:
                st.session_state._run_phase = "interrupted"
                st.session_state._active_run_id = None
                st.rerun()
                return
            
            if ev[0] == "awaiting_permission":
                st.session_state._run_phase = "awaiting_permission"
                st.session_state._pending = ev[1]
                st.rerun()
                return


def _interrupt_run(agent) -> None:
    """统一中断入口(Stop 按钮 + Esc 监听都调这个)。
    
    行为:
    1. 设 cancel_event,通知所有 handler 协作式停止
    2. 不立即调 sm.interrupt()(让当前 handler 跑完当前 chunk / 工具)
    3. 下次 step() 入口检测到 cancel_event → 调 sm.interrupt(ctx) → 转 INTERRUPTED
    """
    if hasattr(agent, "_run_state") and agent._run_state is not None:
        agent._run_state.cancel_event.set()
        # 关闭 LLM SDK 的 stream(让 Anthropic SDK 主动 stop iteration)
        if hasattr(agent, "llm") and hasattr(agent.llm, "close_active_stream"):
            agent.llm.close_active_stream()
```

**Esc 监听**(Streamlit 没有原生 Esc 监听,用 components.html + JS hack):

```python
import streamlit.components.v1 as components

def _setup_esc_listener(agent) -> None:
    """注入全局 Esc 监听器(浏览器层)。"""
    components.html(
        """
        <script>
        document.addEventListener('keydown', function(e) {
            if (e.key === 'Escape') {
                // Streamlit 不能直接调 Python,通过 query param 触发
                // 下次 rerun 时 streamlit 读到 ?_interrupt=1
                const url = new URL(window.location);
                url.searchParams.set('_interrupt', '1');
                window.location.href = url.toString();
            }
        });
        </script>
        """,
        height=0,
    )


# 在 chat input 后检查 query param
if st.query_params.get("_interrupt") == "1":
    _interrupt_run(agent)
    st.query_params.clear()
    st.rerun()
```

**Esc 监听的现实限制**:
- Streamlit 不会自动捕获浏览器层 Esc(需要 JS hack)
- 焦点在 textarea/button 时,Esc 会被浏览器拦截
- **更可靠**:只保留 Stop 按钮,Esc 作为 best-effort

**完整时序**(Stop 按钮路径):
```
用户点 ⏹ 停止
    │
    ▼
_interrupt_run(agent)
    │  set cancel_event
    │  close_active_stream(LLM SDK)
    ▼
agent.step() 当前 chunk 处理完 → 检测 cancel_event → return
    │
    ▼
下次 step() 入口(sm.trigger 开头)
    │  cancel_event 仍 set
    │  → sm.interrupt(ctx)
    │  → transfer to INTERRUPTED
    │  → yield ("system", "⏹️ 对话已被用户中断")
    ▼
UI 段:检测到 interrupt event
    │  _run_phase = "interrupted"
    │  _active_run_id = None
    │  st.rerun()
    ▼
streamlit rerun → 显示"对话已中断" + 允许用户输入新 prompt
```

---

## 十一、扩展点设计(Builder + Named Hook)

### 11.1 AgentBuilder — 统一扩展点 API

```python
class AgentBuilder:
    """统一 agent 组装入口。
    
    解决 v1 问题:
    - 扩展点 API 不一致(extra_phase_transitions vs extra_turn_handlers)
    - 顺序问题(extra 永远在最后,无法指定插入位置)
    """
    
    def __init__(self):
        self._phase_overrides: dict[AgentPhase, Phase] = {}
        self._handler_inserts: list[tuple[Handler, dict]] = []
        self._termination_override: Optional[TerminationCondition] = None
    
    def with_phase_override(self, phase: AgentPhase, override: Phase) -> "AgentBuilder":
        """完全替换某个 phase(高级用法:换 phase 自己的 chain 或 enter/next 行为)。"""
        self._phase_overrides[phase] = override
        return self
    
    def with_handler(self, handler: Handler, *, after: str = None, before: str = None, at: int = None) -> "AgentBuilder":
        """named hook point:在指定 handler 之后/之前插入,或指定 index。
        
        Args:
            handler: 要插入的 handler
            after: 在名为 after 的 handler 之后插入
            before: 在名为 before 的 handler 之前插入
            at: 在指定 index 插入(优先级最低)
        """
        self._handler_inserts.append((handler, {"after": after, "before": before, "at": at}))
        return self
    
    def with_termination(self, termination: TerminationCondition) -> "AgentBuilder":
        self._termination_override = termination
        return self
    
    def build(self, agent_kwargs: dict) -> "ReactAgent":
        agent = ReactAgent(**agent_kwargs)
        for phase, override in self._phase_overrides.items():
            agent._sm._phases[phase] = override
        for chain in [agent._inputs_chain, agent._llm_chain, agent._tool_chain, agent._output_chain]:
            for handler, opts in self._handler_inserts:
                if any(opt is not None for opt in opts.values()):
                    chain.add(handler, **opts)
                else:
                    chain.add(handler)
        if self._termination_override:
            agent._sm._termination = self._termination_override
        return agent
```

### 11.2 用法示例

```python
# ── 例 1:加 cost tracking handler(在 llm_call 之后) ──
agent = (AgentBuilder()
    .with_handler(CostTrackingHandler(pricing_table), after="llm_call")
    .build(agent_kwargs={"llm": llm, "tools": tools, ...}))

# ── 例 2:加 ANSWER_VERIFYING 阶段(完全替换 FinalizingPhase) ──
class VerifyingFinalizingPhase(FinalizingPhase):
    def enter(self, trigger, ctx):
        yield ("system", "🔍 验证答案中...")
        verdict = ctx.run_state.safety_checker.check(ctx.run_state.final_answer)
        if verdict == "needs_review":
            yield ("system", "⚠️ 答案需审查")
            ctx.run_state.needs_review = True
        else:
            yield from super().enter(trigger, ctx)

agent = (AgentBuilder()
    .with_phase_override(AgentPhase.FINALIZING, VerifyingFinalizingPhase(output_chain))
    .build(agent_kwargs={...}))

# ── 例 3:替换 termination 条件(加 timeout) ──
agent = (AgentBuilder()
    .with_termination(CompositeTermination(
        MaxTurnsTermination(max_turns=10),
        TimeoutTermination(timeout_s=300.0),
    ))
    .build(agent_kwargs={...}))
```

---

## 十二、3rd Party Handler 安全边界

### 12.1 问题

3rd party handler 可以:
- 读 `self.messages`(完整 LLM context,可能含 secrets)
- 写 `self.messages`(污染 LLM 输入)
- emit 任意 event(`("system", ...)` / `("memory_event", ...)` — 伪造!)
- 调 `self.llm.chat` 直接绕过 chain

### 12.2 解决方案:trusted vs plugin 分级

```python
class TrustedHandler(Handler):
    """trusted handler:agent 自己或团队写的 handler。
    
    权限:可访问 self.xxx(agent 状态),可 emit 任意 event type。
    验证:不需要额外检查。
    """


class PluginHandler(Handler):
    """plugin handler:3rd party 注入的 handler。
    
    权限受限:
    - 只能读 ctx(turn 上下文),不直接碰 self.xxx
    - 只能 emit 白名单内的 event type
    - 只能 append 到 chain 末端(不能在 LLMCall 之前插)
    
    验证:构造时 type check,运行期 emit 时白名单 check。
    """
    
    ALLOWED_EVENT_TYPES = {
        "system", "metric", "telemetry", "ui_hint",
    }
    
    def emit_validated(self, event: Event, ctx: TurnContext) -> None:
        if event[0] not in self.ALLOWED_EVENT_TYPES:
            raise SecurityError(
                f"PluginHandler {self.name} 不允许 emit event type='{event[0]}',"
                f"允许的类型:{self.ALLOWED_EVENT_TYPES}"
            )
        ctx.emit(event)


class AgentBuilder:
    def with_plugin_handler(self, handler: Handler, *, after: str = None) -> "AgentBuilder":
        """注册 plugin handler(受限)。"""
        if not isinstance(handler, PluginHandler):
            raise TypeError("plugin handler 必须继承 PluginHandler")
        self._handler_inserts.append((handler, {"after": after}))
        return self
```

### 12.3 Event Type 白名单

```python
# agent_core/events.py
class EventType:
    """event type 白名单(trusted handler 可用,plugin handler 受限)。"""
    
    # core(trusted only)
    TEXT = "text"
    THINKING = "thinking"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    USAGE = "usage"
    MEMORY_EVENT = "memory_event"
    MEMORY_STATUS = "memory_status"
    AWAITING_PERMISSION = "awaiting_permission"   # trusted only
    
    # system(允许 plugin)
    SYSTEM = "system"
    METRIC = "metric"
    TELEMETRY = "telemetry"
    UI_HINT = "ui_hint"
    
    TRUSTED_ONLY = {TEXT, THINKING, TOOL_CALL, TOOL_RESULT, USAGE, MEMORY_EVENT, MEMORY_STATUS, AWAITING_PERMISSION}
    PLUGIN_ALLOWED = {SYSTEM, METRIC, TELEMETRY, UI_HINT}
```

---

## 十三、调试 & Checkpoint

### 13.1 StateMachine `__repr__` 人话

```python
class StateMachine:
    def __repr__(self) -> str:
        if not self._history:
            return f"<SM current={self._phase.value}>"
        last_5 = self._history[-5:]
        lines = [f"  {p.value} --[{t}]--> {n.value}" for p, t, n in last_5]
        return (
            f"<SM current={self._phase.value} "
            f"history(last 5):\n" + "\n".join(lines) + ">"
        )

# 用法
print(agent._sm)
# 输出:
# <SM current=awaiting_permission history(last 5):
#   setup --[run_started]--> llm_thinking
#   llm_thinking --[llm_responded]--> llm_thinking
#   llm_thinking --[llm_responded]--> llm_thinking
#   llm_thinking --[permission_needed]--> awaiting_permission
#   awaiting_permission --[..]--> awaiting_permission>
```

### 13.2 on_enter / on_exit hook

```python
sm.on_enter(lambda phase, trigger: logger.debug(f"[SM] enter {phase.value} via {trigger}"))
sm.on_enter(lambda phase, trigger: metrics.incr(f"sm.enter.{phase.value}"))
sm.on_exit(lambda from_p, trigger, to_p: logger.debug(f"[SM] exit {from_p.value} to {to_p.value}"))
```

### 13.3 checkpoint API

```python
import json
with open("debug_checkpoint.json", "w") as f:
    json.dump(agent._sm.checkpoint(), f, indent=2)
```

---

## 十四、向后兼容性边界

### 14.1 兼容性保证(精确版)

**100% 不变**:
- 公开 API 签名:`start_run()` / `step()` / `resume_after_permission()` / `interrupt()` / `resolve_permission()`
  - **注:`agent.run()` 已删**(Plan B Step 7,2026-07-01)— 唯一入口是 `start_run() + step()`
  - v1 `run()` 的 v2 等价调用:`agent.start_run(msg); for ev in agent.step(): ...`
- `step()` yield 的 event tuple 顺序、类型、内容(与原 `run()` 完全一致)
- `self.messages` 类型 + 值
- `self._run_state.last_turn_usage` / `last_input_tokens` / `last_output_tokens` 类型 + 值
- session 持久化文件格式(`data/sessions/<id>/session.jsonl`)
- audit logger 写入字段(`audit.jsonl`)
- 所有 logger 输出(🛡️/⚙️/🪝/📋/🧪/🤖 的 `[xxx]` 标记 + log level)
- 公开方法:`fork()` / `close()` / `reset()` / `load_messages()` / `add_compact_boundary()` 等

**会变**(但有 fallback):
- `resolve_permission()` 内部实现(委托给 `resume_after_permission`)
- 内部状态组织:`pending_*` 三字段从 `self._pending_thinking / _pending_tool_logs / _pending_tool_results` → `self._run_state.pending_thinking / pending_tool_logs / pending_tool_results`(Plan B Step 4)
- `_ask_user_permission` 默认行为(从"100ms 后默认 deny" → "立即返 AWAITING_PERMISSION sentinel";legacy 路径由 `resolve_permission` 兼容)
- **8 处 v1 直接 `add_*` session 写入已全部归并到 3 个 v2 sub-handler**(Plan B Step 1-3 + Step 5):
  - Stage A (`LLMCallPersistHandler`) 接管 #1/#2/#3/#4/#5/#6/#8
  - Stage B (`ToolPairPersistHandler`) 接管原 `_iter_phase_tools` 普通 tool_result 路径
  - Stage C (`FinalAnswerPersistHandler`) 接管 #7 final answer 4 字段
  - **唯一例外**:`resume_after_permission(choice="deny")` 路径仍 `add_tool_results` immediate flush(在 `if choice == "deny"` 分支)— 因 deny 直接转 LLM_THINKING 不重跑 EXECUTING_TOOLS,Stage B 不会触发

**不保证**(回归测试若发现,补 compat shim):
- `_pending_thinking / _pending_tool_logs / _pending_tool_results` 实例字段已迁到 `RunState.pending_*`(Plan B Step 4)— 实例属性访问返 AttributeError
- 异常 traceback 深度
- 任何依赖 `frame.f_locals` 的代码
- `agent.run()`(已删)
- `SessionPersistMode` / `use_real_session_persist` / `SessionPersistHandler` / `_make_session_persist_delegate`(Plan B Step 8 删 DELEGATE toggle)

### 14.2 兼容性测试

```python
# tests/test_compat.py
def test_start_run_step_yield_order_unchanged():
    """v2 step() yield 的 event 顺序必须与 v1 run() 等价。"""
    agent = ReactAgent(...)
    agent.start_run("test prompt")  # Plan B Step 7:agent.run() 已删
    events = list(agent.step())     # 用 step() 代替 for ev in agent.run()
    assert events == LOAD_FIXTURE("v1_baseline_events.json")


def test_resolve_permission_legacy_path():
    """旧测试 threading.Thread 调 resolve_permission() 仍工作(Event 路径)。"""
    # 1. 主线程开 agent.start_run(msg); agent.step() loop
    # 2. 测试线程等 permission_request → 调 agent.resolve_permission("allow")
    # 3. 验证 step() 继续 yield,最终 event 序列符合预期


def test_all_logger_outputs_present():
    """所有 6 个子 logger 的 [xxx] 标记仍在 caplog。"""
    # 1. caplog at_level(DEBUG) 跑一遍完整 start_run+step loop
    # 2. 验证 caplog records 含 🛡️ / ⚙️ / 🪝 / 📋 / 🧪 / 🤖 的所有 [xxx] 标记


def test_session_jsonl_format_unchanged():
    """session.jsonl 的 schema 和写入字段不变。
    Plan B Step 1-3 后写入路径全走 Stage A/B/C handler,但 schema 跟 v1 完全一致。
    """
    # 1. 跑新版 start_run+step
    # 2. 读 session.jsonl,对比 v1 fixture 的每条 entry 字段(包括 entry 3 的
    #    thinking / usage / tool_logs / Observation 完整性)
```

**新增**(Plan B Step 6 引入):
```python
# tests/test_compat.py::TestStartRunStepCompat(7 case)— v2 start_run()+step() 镜像
# tests/test_recovery_v2_persist.py(6 case)— Stage A/B/C 接管后的 crash-recovery
```

---

## 十五、实施清单

| 步骤 | 改动 | 估计行数 | 风险 |
|---|---|---|---|
| 1 | 新建 `agent_core/agent_state.py`:`AgentPhase` / `Phase` ABC / 6 个 Phase 子类 / `PhaseContext` / `StateMachine` / `TerminationCondition` | +350 | 中 |
| 2 | 新建 `agent_core/turn_chain.py`:`Handler` 协议 / `HandlerResult` / `TurnContext` / `TurnChain` / 11 个内置 Handler class | +600 | 中 |
| 3 | 新建 `agent_core/stages.py`:`StageInputs` / `LLMResult` / `ToolExecutionResult` dataclass | +60 | 低 |
| 4 | 新建 `agent_core/builder.py`:`AgentBuilder` + named hook API | +150 | 低 |
| 5 | `agent_core/agent_core.py` 加 `start_run/step/resume_after_permission` + `ReactAgent` 重构(用 state machine + 4 chains) | +250 / -570 | 中 |
| 6 | 改 `_ask_user_permission` 返 sentinel | -10 / +20 | 中 |
| 7 | `run()` 改薄壳(调 start_run + step + 兼容 legacy Event) | -570 / +50 | 低 |
| 8 | `web/app.py` 改 state machine 集成 | +200 / -100 | 中 |
| 9 | 新建 `tests/test_agent_state_machine.py` | +300 | 低 |
| 10 | 新建 `tests/test_stage_contract.py` | +100 | 低 |
| 11 | `tests/test_permission_integration.py` 加旧 `run()` + `resolve_permission` 兼容性测试 | +50 | 低 |
| 12 | `tests/test_compat.py`:v1 baseline 对比 | +150 | 低 |
| 13 | 跑全量回归(300+ 测试) | — | — |
| 14 | **INTERRUPTED 状态机扩展**:`AgentPhase` 加 `INTERRUPTED` enum + `InterruptedPhase` class + `StateMachine.interrupt()` 方法 | +120 | 中 |
| 15 | **RunState 新增 `cancel_event`**:`agent_core/run_state.py` + 所有 handler 入口检查 `cancel_event.is_set()` | +60 / -10 | 低 |
| 16 | **LLM SDK 集成 cancel_event**:`AnthropicClient.chat(..., cancel_event=...)` 内部 listen event + 主动 `stream.close()` | +30 | 中 |
| 17 | **ToolExecuteHandler cancel 协作**:`subprocess.Popen.terminate()` + 0.5s 后 `.kill()` 兜底(串行 / 并行都支持) | +80 | 中 |
| 18 | **`web/app.py` 加 Stop 按钮 + Esc 监听**:`_interrupt_run()` 统一入口 + `components.html` JS hack + query param 触发 | +120 / -20 | 中 |
| 19 | **`tests/test_interrupt.py`**:流式中断 / tool 终止 / permission 取消 / 幂等性测试 | +200 | 低 |
| 20 | **跑全量回归 + interrupt e2e**(`streamlit` 启动 + 点 Stop 验证) | — | — |

**Plan B (2026-07-01)— 彻底合并 v2,删 v1 双轨**:

| 步骤 | 改动 | 估计行数 | 风险 |
|---|---|---|---|
| 21 | **Stage A — `LLMCallPersistHandler`**:接管 v1 #1/#2/#3/#4/#5/#6/#8 7 处 `add_assistant*`,根据 `stop_reason` 派发 A1/A2/A3(A1:tool_calls 非空 → add_assistant_with_tools;A2:stop_reason="llm_error";A3:stop_reason="interrupted") | +140 | 中 |
| 22 | **Stage B — `ToolPairPersistHandler`**:接管 v1 `_iter_phase_tools` 普通 tool_result 路径,读 `RunState.pending_tool_results` → flush `add_tool_results` + 清空(幂等) | +60 | 中 |
| 23 | **Stage C — `FinalAnswerPersistHandler`**:接管 v1 #7 final answer 4 字段 `add_assistant_message`(`full_text + thinking + tool_logs + usage`),skip `max_tokens/length/has_tool_calls/empty` | +90 | 中 |
| 24 | **`RunState.pending_*` 字段迁移**:`_pending_thinking / _pending_tool_logs / _pending_tool_results` 3 实例字段整体迁到 `RunState.pending_*`(`agent_state.py:115-117` 已就位) | -10 / +24 | 低 |
| 25 | **删 `_iter_phase_llm` / `_iter_phase_tools` 内 8 处 `add_*` 全删** + acceptance gate(grep "add_assistant" 在 agent_core.py 内 0 行) | -40 | 高(R3:surgery 漏删风险) |
| 26 | **测试迁移**:`test_compat.py` `TestRunBackwardCompat` 6 case 改 `TestStartRunStepCompat` 7 case(用 `start_run()+step()`) + 新增 `test_recovery_v2_persist.py` 6 case(Stage A/B/C 接管后 crash / resume / orphan detection) | +200 / -100 | 中 |
| 27 | **删 `agent.run()` 定义**(`agent_core/agent_core.py:1862-2534`,670 行)+ 改 2 个 web 调用方到 `start_run()+step()` + 改 test_compat / test_react_agent_bridge / test_interrupt / test_sm_layer_integration 调用 | -670 / +50 | 高(暴露 2 个 Plan A gap,见 §17 R4/R5) |
| 28 | **删 `SessionPersistMode` DELEGATE + `use_real_session_persist` toggle**(`agent_core/builder.py`)+ 整文件删 `test_session_persist_handler.py`(Step 3 起 import 失败,被 stage_a/b/c + recovery 覆盖) | -120 / -540 | 中 |
| 29 | **文档改写**:§14.1(plan 已改)/ §14.2(`test_run_yield_order` → `test_start_run_step_yield_order`)/ §15 实施清单追加 step 21-28/ §16.2 回归 test 列表加 3 个 recovery 文件/ §17 风险表加 R1/R2/R3 + R4/R5(Plan A 遗留) | +80 / -30 | 低 |
| 30 | **全量回归 + v1 baseline 比对 + 文档 acceptance walk-through**:§16.2 全 300+ 测试通过 + §16.3 E2E Streamlit + chat_input + tool + Stop 验证 + §10/§11/§14/§15/§16/§17 逐条 walk-through | — | — |

**总计**:30 步,~10 步 × ~2h = ~30h,分 3-4 个 full day。

**总计**:20 步,~2860 行新增 / ~710 行删减(净 +2150 行,**含 INTERRUPTED 完整支持**)。

---

## 十六、测试设计

### 16.1 单元测试(快,~15s)

```bash
python -m pytest tests/test_agent_state_machine.py tests/test_stage_contract.py -v --no-header
```

覆盖:
- **StateMachine**: trigger 正常转移 / InvalidTransition / is_done 终止 / on_enter hook / checkpoint
- **Phase 6 子类**:每个 phase 的 enter / next 行为(mock ctx,assert events)
- **TurnChain**: 顺序执行 / stop_chain 短路 / add(after/before/at) / remove
- **Handler 11 个**:每个独立测试(mock 依赖,assert events + stage_outputs 字段)
- **Stage dataclass**: handler 间 contract(LLMCallHandler 写 stage_outputs.chunks → ChunkParseHandler 读它,中间 type check)

### 16.2 回归测试(必须全过,~60s)

```bash
python -m pytest tests/ -q --no-header
```

重点关注(Plan B Step 1-9 后):
- `test_react_agent_bridge.py` — `MemoryBridgeExtractHandler` 路径(已全过,无需迁移)
- `test_interrupt.py` — INTERRUPTED 端到端(已全过,`interrupt()` v2 API 行为不变)
- `test_permission_integration.py` — 旧 `resolve_permission` 兼容性 + 权限决策(Plan B Step 7 迁 `_pending_*` 到 RunState)
- `test_compat.py` — v1 baseline 对比(Plan B Step 6 新增 `TestStartRunStepCompat` 7 case 镜像原 `TestRunBackwardCompat`)
- `test_stage_a_persist.py` / `test_stage_b_persist.py` / `test_stage_c_persist.py` — Plan B Stage A/B/C 接管 8 处 v1 写入(各 5-7 case)
- `test_run_state_pending_migration.py` — Plan B Step 4 RunState.pending_* 字段迁移(3 case)
- `test_step5_v1_deletion_acceptance.py` — Plan B Step 5 v1 add_* 删除静态 acceptance gate(6 case)
- `test_recovery_v2_persist.py` — Plan B Step 6 crash-recovery(6 case:Stage A partial write / resume replay / orphan detection)
- `test_builder.py` / `test_builder_e2e.py` — Plan B Step 8 删 `SessionPersistMode` DELEGATE + `use_real_session_persist` toggle 后 builder API 表面
- `test_sm_layer_integration.py` — 2 case 标记保留为 `xfail(strict=False)`(原 R4 Plan A 遗留,2026-07-01 修复后转 XPASS,保留 marker 当 regression guard,见 §17 R4)
- `test_react_agent_bridge.py` — 1 case 标记保留为 `xfail(strict=False)`(原 R5 Plan A 遗留,2026-07-01 修复后转 XPASS,保留 marker 当 regression guard,见 §17 R5)

**注意**:`test_streaming.py` / `test_checkpointer.py`(repo root,非 `tests/`)用 `langgraph_agent.LangGraphAgent`(独立类,非 ReactAgent),`agent.run()` 调的是 LangGraphAgent,跟 Plan B 无关。pytest 尝试 collect 它们时 import 失败是 pre-existing(Step 7 调查时确认),不在 Plan B 范围。

### 16.3 E2E 验证(手工,~30s)

```bash
AGENT_LOG_PERMISSION=DEBUG AGENT_LOG_SANDBOX=INFO \
  HF_HUB_OFFLINE=1 .venv/bin/streamlit run web/app.py
```

1. `chat_input` 输入 `请运行 echo hello`
2. 期望:🔐 dialog 弹出,**不** 再出现 `🛡️ [ask_user_timeout]`
3. 点 "✅ Allow once"
4. 期望:agent 收到 `Bash` tool_result,继续生成最终回答,显示 `hello`
5. 看 `logs/app/agent.log` 应有:
   ```
   🛡️ [step_7_default_ask] behavior=ask
   🛡️ [ask_user_permission_entry]
   🪝 [permission_request_no_hooks]
   🛡️ [ask_user_response] tool=Bash choice=allow wait_ms=4500.0   ← 不再 103.5
   ⚙️ [bash_handler_entry] command=echo hello
   ⚙️ [bash_subprocess_done] exit_code=0 stdout_len=6
   ```

### 16.4 并发测试(显式不保证)

```python
def test_state_machine_not_thread_safe():
    """StateMachine 不是 thread-safe,文档明确,测试也要覆盖。"""
    # 1. 2 个 thread 并发调 sm.trigger(...)
    # 2. 期望:至少一个抛异常,或 history 出现非法的 transition
    # 3. 文档 §十七 明确"不保证 thread-safety"
```

### 16.5 性能测试

```python
def test_state_machine_overhead_below_1ms():
    """state machine 转移 + chain 执行的开销 < 1ms(per turn)。"""
    import time
    start = time.perf_counter()
    for _ in range(1000):
        list(sm.trigger("llm_responded", ctx))
    elapsed = time.perf_counter() - start
    assert elapsed / 1000 < 0.001
```

### 16.6 边界 case 测试

```python
def test_empty_messages_handler_does_not_crash(): ...
def test_max_turns_termination(): ...
def test_handler_exception_doesnt_crash_chain(): ...
def test_state_machine_fuzz_no_deadlock(): ...
```

---

## 十七、风险表

| 风险 | 等级 | 缓解 |
|---|---|---|
| 状态机 + 职责链 双重抽象,新人理解成本 | 中 | 文档 + 调试 `print(sm)` + checkpoint API |
| 流式 `step()` 在 Streamlit rerun cycle 下的边界 | 中 | 单元测试 + E2E + race condition `active_run_id` 校验 |
| 11 个 handler class,改动时容易碰错 | 中 | 强类型 stage dataclass 强制 contract;CI 跑全套测试 |
| 3rd party plugin handler 安全边界执行不严 | 中 | AgentBuilder 入口 type check + emit 白名单 RuntimeError |
| 并发 / 重入 安全 | 低 | 文档明确"不保证 thread-safety",只支持单 step() 顺序调用 |
| 性能(state machine 转移 + chain 调度) | 低 | §16.5 性能测试 + 实测开销 < 1ms/turn |
| 过渡设计风险 | 低 | v2 比 v1 多了 3 层抽象,但每个都解决 v1 明确问题 |
| **LLM stream 取消不彻底(Anthropic SDK 限制)** | 中 | `stream.close()` 让 SDK 主动停止 iteration,server 端可能仍在处理但客户端不再 yield;cancel_event 兜底检查,已 yield 的 chunk 仍 emit 给 UI |
| **Subprocess 终止留 orphan**(子进程逃逸) | 中 | `process.terminate()` 0.5s 后 `.kill()` 兜底;并行场景下逐个 wait 收集,不留 zombie |
| **并行 tool 取消时部分完成** | 中 | 已 emit tool_result 的 tool 仍落 session.jsonl(用户可看到"部分结果"),未启动的 skip;提供 `_pending_partial_results` 字段记录"中断时已部分完成"的 tool |
| **用户连续点 Stop × N** | 低 | `StateMachine.interrupt()` 幂等,转移到 INTERRUPTED 后再调直接 return |
| **Esc 监听不可靠**(Streamlit 限制) | 低 | 优先 Stop 按钮(永远有效),Esc 仅 best-effort;焦点在 textarea 时 Esc 会被浏览器拦截 |
| **cancel_event 误用**(handler 忘记检查) | 中 | 所有 handler 在 handle() 入口第一行检查 `cancel_event.is_set()`,代码 review checklist 强制要求;测试覆盖每个 handler 的 cancel 路径 |
| **INTERRUPTED 后 session 状态不完整** | 低 | FINALIZING chain 的 SessionPersistHandler 在 INTERRUPTED 前**不会**跑(因为 INTERRUPTED 直接转,跳过 FINALIZING);若要保留部分结果,改 FINALIZING → INTERRUPTED 之间也跑 SessionPersist |
| **interrupt 在 AWAITING_PERMISSION 时** | 低 | PermissionCheckHandler 检测 cancel_event 后 stop_chain,UI 端 Stop 按钮 → 调 `_interrupt_run` → 关闭 dialog + 切 INTERRUPTED |

**Plan B (2026-07-01) 新增风险**:

| 风险 | 等级 | 缓解 |
|---|---|---|
| **R1: Stage A 写盘后,Stage B 前 crash → orphan tool_use** | 中 | `test_recovery_v2_persist.py::test_case1` + `test_case4_orphan_tool_use_detection` 提供 inline detector(扫 jsonl 找无对应 tool_result 的 tool_use block);UI 决策:放弃/补齐/删除。生产 web 端 `_detect_orphan_tool_use` 整合待 Step 9/独立 task |
| **R2: AWAITING_PERMISSION 拆分路径恢复一致性** | 中 | Stage A 写过 → 等用户 → resume 后 Stage B 写 tool_result;`test_recovery_v2_persist.py::test_case2_resume_after_permission_replays_stage_b` 覆盖 |
| **R3: surgery 漏删一处 v1 写入 → double-write 或 missing write** | 高 | grep 反向断言当 acceptance gate(`test_step5_v1_deletion_acceptance.py` 静态 grep 6 case 守门);每删一处 v1 立即 git commit 标注对应 Stage handler。**当前状态**:`add_assistant*` 在 agent_core.py 内 0 行,`add_tool_results` 仅 1 处 deny 路径 |
| **R4: ContextManager compaction + L3 SM fast path 未接 step() 路径(Plan A 遗留)** | ~~中~~ **已修复(2026-07-01)** | 自 Plan A (2026-06-30) 把 web 切到 `start_run()+step()` 起,`check_and_compact` / `session_memory.should_trigger_compact` / `sm_compact` 只在已删的 `agent.run()` 里,生产 token 预算不受控 → 长对话超 context window。**修复**:`ContextCompactionHandler` 加到 `inputs_chain` 首位(SETUP phase 入口),在每个 turn 入口做 compression 决策(L3 SM 快路径 + ContextManager fallback),通过 `_persist_compacted_messages` 持久化 + emit `("system", "📦 上下文已压缩: ...")`。**当前状态**:`test_sm_layer_integration.py` 2 case 标记保留为 `xfail(strict=False)` — **现 XPASS**(plan 接受定义,2026-07-01 验证 `4 passed, 2 xpassed`);输入 outputs_chain `["context_compaction", "memory_retrieval", "system_prompt", "tools_schema_prepare"]` |
| **R5: v2 SM intra-drive max_turns 不终止(Plan A 遗留)** | ~~低~~ **已修复(2026-07-01)** | `agent_state.py:406` SM 链式 self-trigger 在单个 `step()/drive()` 内 LLM_THINKING ⇄ EXECUTING_TOOLS 无限循环,`run_state.turn` 只在 `_new_turn_ctx()` 递增(每 drive +1),链内不递增 → `MaxTurnsTermination.check` 永不命中。`run()` 的 `for turn in range(1, max_turns+1)` 显式循环掩盖了它。**修复**:RunState 加 `tool_call_cycles` 字段,LLMThinkingPhase.enter 入口增 1,MaxTurnsTermination.check 改看 `turn >= max OR cycles >= max` — 同时兼容 v1 (turn) + 修 v2 链式 (cycles)。**当前状态**:`test_react_agent_bridge.py::test_no_mispair_when_tool_run_has_no_final_answer` xfail(strict=False) 标记保留当 regression guard — 验证 `1 xpassed`(2026-07-01) |

---

## 附录:核心学习要点(从 v1 → v2)

> **类型系统表达不变量** — `StepResult` 三态用 sum type,`HandlerResult.stop_chain` 表达短路,**别用 docstring + assert**

> **抽象要对称** — 职责链别只在一个 phase 用,要么每个 phase 都用,要么不用

> **DI 要显式** — handler 是 class 不是 function,`__init__` 注入依赖,可独立 mock 测试

> **Contract 要类型化** — handler 之间"上一步输出 = 下一步输入"用 dataclass 强类型,**别靠"重排会崩"当护栏**

> **流式就是 generator** — `step()` 返 generator 逐个 yield,不是 list buffer;遇暂停点直接 `return`(generator 暂停语义)

> **状态分层清晰** — turn / run / agent 三层 dataclass,数据流图显式画出 transfer function

> **终止条件显式建模** — `TerminationCondition` class,StateMachine 每次 trigger 前统一 check,防止死循环

> **Phase 是 class,不是 dict** — `enter()` 干活 + `next()` 转移,而不是 transition 表里"上一个 handler 决定下一个 phase"

> **扩展点统一 API** — `AgentBuilder` + named hook point(`after=` / `before=`),不平行多个 extra_xxx 参数

> **3rd party 权限分级** — trusted / plugin 两类,plugin 受限(读 ctx only + emit 白名单 + 只能 append)

> **设计文档自洽** — 不写"关联文档"凑数,真依赖写进正文
