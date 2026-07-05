# ReactAgent 重构:状态机 + 职责链设计

> 整理时间:2026-06-29 | 关联文档: [`TODO.md`](TODO.md) §1 计划
> 关联模块: [`agent_core/agent_core.py`](../agent_core/agent_core.py) (待重构)
> 关联 UI: [`web/app.py`](../web/app.py) (下游适配)

---

## 〇、为什么这次重构

### 0.1 现状(2026-06-29)

[`agent_core/agent_core.py:755-1373`](../agent_core/agent_core.py#L755-L1373) 的 `run()` 方法是一个 **619 行的单方法 generator**,内部实际塞了 6 类互不耦合的职责:

| 段 | 行号 | 职责 |
|---|---|---|
| A. Setup | 762-893 | append user msg + 重置 pending + L3 SM 决策 + ContextManager compact + 持久化 |
| B. Turn loop init | 895-907 | 初始化 `last_turn` / `last_input_tokens` / `final_answer` / `final_stop_reason` |
| C. 单 turn 执行 | 909-1283 | 准备 messages → LLM 流式调用 → 收集 chunks → 处理 tool_calls(串行/并行) |
| D. Run 收尾 | 1285-1327 | 触发 `bridge.on_turn_end` 记忆提取 |
| E. L3 SM extract | 1320-1365 | 触发后台 `session_memory.extract_incremental` |
| F. Session flush | 1367-1373 | `session_manager.flush()` |

`run()` 内部还有 6 个跨段共享的"隐性"局部变量(`last_turn` / `last_input_tokens` / `last_output_tokens` / `last_tool_calls` / `final_answer` / `final_stop_reason`),通过 turn loop 累积、在 run 末尾使用 — **必须活在 `run()` locals 里**,无法被其他方法共享,这是 `run()` 不能拆的根本原因。

### 0.2 三个核心痛点

1. **UI 集成困难**: `run()` 是"一次性同步 generator",Streamlit 主线程在 `_ask_user_permission` 的 `Event.wait(0.1s)` 上阻塞 100ms 后默认 deny,**`@st.dialog` 永远没机会 render**(现场: [logs/app/agent.log](../logs/app/agent.log) 16:29:56 起,每次都是 `wait_ms=103.5 → default deny`)。
2. **不可单测**: 无法单独测"单 turn 行为"或"permission 路径",必须跑完整 `run()`。
3. **扩展性差**: 想加"答案验证" / "rate limit 退避" / "请求澄清" 等新阶段,必须改 `run()` 主体;想加"safety 二次审查" / "cost tracking" / "tool telemetry" 等新处理,必须改 `_step_one_turn` 主体。

### 0.3 目标

| 目标 | 收益 |
|---|---|
| 1. **可暂停**: 把"一次性 generator"改成"turn-by-turn 状态机",UI 可以在两个 turn 之间插一刀(让用户点按钮) | Streamlit 集成修复,`wait_ms` 从 100ms 涨到秒级 |
| 2. **可单测**: 把"大方法"拆成"小方法",每个 handler / 每个 transition 独立可测 | 测试覆盖度↑,bug 定位↓ |
| 3. **可扩展**: 用状态机管阶段、职责链管子步骤,加新阶段/新处理**不动现有代码** | 后续 M12+ / 3rd party 集成成本↓ |
| 4. **向后兼容**: 公开 API `run()` / `resolve_permission()` 行为不变 | 300+ 测试 / `web/app_langgraph.py` / `web/pages/00_Chat.py` 全不破 |

---

## 一、整体架构:三层 method + 状态对象

```
┌────────────────────────────────────────────────────────────┐
│  Layer 1 — 公开 API (向 UI / 测试暴露)                       │
│  ────────────────────────────────────────────────────────  │
│  • run(user_input) → Iterator[Event]        # 旧 API,保留     │
│  • start_run(user_input) → None             # 新: 初始化       │
│  • step() → StepResult                      # 新: 推一 turn   │
│  • resume_after_permission(choice) → None   # 新: 解锁 ask    │
└────────────────────────────────────────────────────────────┘
                          ↓
┌────────────────────────────────────────────────────────────┐
│  Layer 2 — 状态机 + 职责链 (扩展点)                          │
│  ────────────────────────────────────────────────────────  │
│  • AgentPhase 枚举 + StateMachine(transition 表)            │
│  • TurnChain(handler 链) + 内置 6 个 TurnHandler             │
│  • 3rd party 扩展: extra_handlers=[] 注入自定义 handler     │
└────────────────────────────────────────────────────────────┘
                          ↓
┌────────────────────────────────────────────────────────────┐
│  Layer 3 — 内部 method (run() 拆出来的,private)             │
│  ────────────────────────────────────────────────────────  │
│  • _setup_run(user_message) → Iterator[Event]   # A 段      │
│  • _step_one_turn() → Iterator[Event]           # C 段      │
│  • _finalize_run() → Iterator[Event]            # D + E + F │
└────────────────────────────────────────────────────────────┘
                          ↓
┌────────────────────────────────────────────────────────────┐
│  状态对象 (替代 run() 内 6 个隐式 locals)                     │
│  ────────────────────────────────────────────────────────  │
│  • _RunContext dataclass: turn / last_input_tokens / ...    │
│  • 挂在 self._run_ctx,各 method 共读共写                    │
└────────────────────────────────────────────────────────────┘
```

---

## 二、状态机设计 (Layer 2 上半)

### 2.1 AgentPhase 枚举 — 6 个粗粒度阶段

```python
# agent_core/agent_state.py
from enum import Enum


class AgentPhase(Enum):
    """agent 一次 run 的显式阶段。
    
    状态转移图:
        SETUP → LLM_THINKING → AWAITING_PERMISSION
                        ↑   ↓           ↓
                        ↑ EXECUTING_TOOLS
                        ↑   ↓
                        ←──┘
                    FINALIZING → DONE
    """
    SETUP               = "setup"                # 初始化、压缩上下文
    LLM_THINKING        = "llm_thinking"         # 调 LLM + 收 chunks
    AWAITING_PERMISSION = "awaiting_permission"  # 等 UI 决定
    EXECUTING_TOOLS     = "executing_tools"      # 跑 tool_calls
    FINALIZING          = "finalizing"           # 记忆 + flush
    DONE                = "done"                 # run 结束
```

### 2.2 StateMachine — 转移表执行器

```python
from dataclasses import dataclass
from typing import Callable, Any


PhaseHandler = Callable[["_RunContext"], list[tuple[str, Any]]]
"""handler 签名:接 _RunContext,返 (events, may set awaiting_permission)。"""


@dataclass(frozen=True)
class PhaseTransition:
    """一次状态转移定义(from + trigger + handler + to)。"""
    from_phase: AgentPhase
    trigger:   str             # "llm_responded" / "permission_resolved" / ...
    handler:   PhaseHandler    # 真正干活 + 返 events
    to_phase:  AgentPhase


class InvalidTransition(Exception):
    """当前 phase 不能被指定 trigger 触发时抛。"""


class StateMachine:
    """agent 状态机:管 phase + 转移表 + 执行。
    
    设计要点:
    - 转移表一次性注册,运行期不修改(便于调试)
    - trigger(event) 返 (events, new_phase),调用方继续推进
    - _history 记录所有状态转移,调试时一键打印
    """
    
    def __init__(
        self,
        transitions: list[PhaseTransition],
        initial: AgentPhase,
    ):
        self._transitions: dict[tuple[AgentPhase, str], PhaseTransition] = {
            (t.from_phase, t.trigger): t for t in transitions
        }
        self._phase = initial
        self._history: list[tuple[AgentPhase, str, AgentPhase]] = []
    
    @property
    def current(self) -> AgentPhase:
        return self._phase
    
    @property
    def history(self) -> list[tuple[AgentPhase, str, AgentPhase]]:
        return list(self._history)
    
    def trigger(self, event: str, ctx: "_RunContext") -> list[tuple[str, Any]]:
        """触发转移:找 transition,调 handler,更新 phase,返 events。
        
        Args:
            event: 触发信号(如 "llm_responded" / "permission_resolved")
            ctx: 共享状态对象
        
        Returns:
            handler 产出的 events(空 list 表示无声 transition)
        
        Raises:
            InvalidTransition: 当前 phase 不能被 event 触发
        """
        t = self._transitions.get((self._phase, event))
        if t is None:
            raise InvalidTransition(
                f"phase={self._phase.value} 不能被 trigger='{event}' 触发"
            )
        events = t.handler(ctx)
        self._history.append((self._phase, event, t.to_phase))
        self._phase = t.to_phase
        return events
    
    def is_done(self) -> bool:
        return self._phase == AgentPhase.DONE
```

### 2.3 PhaseHandler 协议 — 每阶段干活的函数

```python
# 每个 handler 是普通函数,签名 PhaseHandler
# 挂 _RunContext 上读写状态,返 events 给调用方 yield

def _do_setup(ctx: _RunContext) -> list[tuple[str, Any]]:
    """SETUP 阶段:append user msg + L3 SM 决策 + ContextManager compact。"""
    events: list[tuple[str, Any]] = []
    # ... A 段逻辑(原 L762-893 段, 1:1 搬)
    return events


def _handle_llm_response(ctx: _RunContext) -> list[tuple[str, Any]]:
    """LLM_THINKING 阶段出口:判断下一步是 EXECUTING_TOOLS / FINALIZING / AWAITING_PERMISSION。
    
    走职责链(§三)处理 turn 内子步骤。
    """
    events: list[tuple[str, Any]] = []
    # 走 turn chain
    for ev_type, ev_content in ctx.turn_chain.run(ctx.turn_ctx):
        events.append((ev_type, ev_content))
        if ev_type == "awaiting_permission":
            ctx.awaiting_permission = ev_content
            return events  # 提前返,让 state machine 决定转 AWAITING_PERMISSION
    return events


def _dispatch_permission_choice(ctx: _RunContext) -> list[tuple[str, Any]]:
    """AWAITING_PERMISSION → EXECUTING_TOOLS:把 UI 决定应用到 tool_call。"""
    events: list[tuple[str, Any]] = []
    choice = ctx.awaiting_permission.get("choice", "deny")
    if choice == "allow":
        # 走 normal execute
        ...
    elif choice == "deny":
        # 写 deny tool_result,让 LLM 看到
        ...
    return events


def _save_session(ctx: _RunContext) -> list[tuple[str, Any]]:
    """FINALIZING 阶段:bridge.on_turn_end + L3 SM extract + session flush。"""
    # D + E + F 段(原 L1285-1373 段, 1:1 搬)
    ...
```

### 2.4 PhaseTransition 表 — 一次性注册

```python
# 在 ReactAgent.__init__ 里组装
def __init__(self, ..., extra_phase_transitions: list[PhaseTransition] = None):
    base_transitions = [
        # SETUP 阶段
        PhaseTransition(AgentPhase.SETUP, "run_started", _do_setup, AgentPhase.LLM_THINKING),
        
        # LLM_THINKING 阶段 — 三条出口
        PhaseTransition(AgentPhase.LLM_THINKING, "llm_responded",
                        _handle_llm_response, AgentPhase.LLM_THINKING),  # 默认循环
        PhaseTransition(AgentPhase.LLM_THINKING, "permission_needed",
                        _stash_permission_request, AgentPhase.AWAITING_PERMISSION),
        PhaseTransition(AgentPhase.LLM_THINKING, "llm_responded_final",
                        _handle_final_answer, AgentPhase.FINALIZING),
        
        # AWAITING_PERMISSION 阶段
        PhaseTransition(AgentPhase.AWAITING_PERMISSION, "permission_resolved",
                        _dispatch_permission_choice, AgentPhase.EXECUTING_TOOLS),
        
        # EXECUTING_TOOLS 阶段
        PhaseTransition(AgentPhase.EXECUTING_TOOLS, "tools_done",
                        _save_tool_results, AgentPhase.LLM_THINKING),
        
        # FINALIZING 阶段
        PhaseTransition(AgentPhase.FINALIZING, "finalize_done",
                        _save_session, AgentPhase.DONE),
    ]
    all_transitions = base_transitions + (extra_phase_transitions or [])
    self._sm = StateMachine(all_transitions, initial=AgentPhase.SETUP)
```

### 2.5 状态机驱动:start_run / step / resume_after_permission

```python
@dataclass
class StepResult:
    """agent.step() 一次推进的结果。"""
    events: list[tuple[str, Any]]                # 本 step 产出的所有 events
    is_done: bool                                # 整个 run 是否结束
    awaiting_permission: Optional[dict] = None   # 非 None → UI 需决定


class ReactAgent:
    def start_run(self, user_input: str) -> None:
        """初始化一次 run:创建 _RunContext + 重置 SM + append user msg。"""
        self._run_ctx = _RunContext(
            user_message=user_input,
            turn=0,
            turn_chain=self._turn_chain,    # Layer 2 下半,见 §三
        )
        self._sm = StateMachine(
            transitions=[...],              # 重新注册
            initial=AgentPhase.SETUP,
        )
        # 触发 SETUP
        self._sm.trigger("run_started", self._run_ctx)
    
    def step(self) -> StepResult:
        """推进一个 LLM cycle(LLM call + tool execution batch)。
        
        若遇 permission ask → return StepResult(awaiting_permission=req)
        若 run 结束 → return StepResult(is_done=True)
        否则 return StepResult(events=...)
        """
        events: list[tuple[str, Any]] = []
        
        # 一次 step 包含 N 个 LLM cycle(N = 直到 ask / final / max_turns)
        # 这里"step"定义为"推到下一个暂停点"——可以是 awaiting_permission / done
        while not self._sm.is_done():
            # LLM_THINKING → 走 _handle_llm_response
            turn_events = self._sm.trigger(
                "llm_responded", self._run_ctx
            ) if self._sm.current == AgentPhase.LLM_THINKING else []
            
            for ev in turn_events:
                events.append(ev)
                if ev[0] == "awaiting_permission":
                    # 暂停,等 UI 决定
                    return StepResult(
                        events=events,
                        is_done=False,
                        awaiting_permission=self._run_ctx.awaiting_permission,
                    )
        
        return StepResult(events=events, is_done=True)
    
    def resume_after_permission(self, choice: str) -> None:
        """UI 决定后调:把 choice 注入 _RunContext + 触发 state machine。"""
        if self._run_ctx is None or not self._run_ctx.awaiting_permission:
            raise RuntimeError("当前没有 awaiting_permission 状态")
        self._run_ctx.awaiting_permission["choice"] = choice
        self._sm.trigger("permission_resolved", self._run_ctx)
```

### 2.6 状态机扩展场景:加新阶段零改动

| 场景 | 现状(改 run() 主体) | 状态机(加 transition) |
|---|---|---|
| 加"**答案验证**" (LLM 回答后查 hallucination) | 改 `_step_one_turn` + 加 if-else 分支 | 加 `VERIFYING_ANSWER` 枚举 + 1 handler + 3 transitions,**不动现有** |
| 加"**rate limited 退避**" (429 退避 30s) | 改 LLM call 段 + 加 sleep | 加 `BACKING_OFF` 枚举 + handler + transition |
| 加"**请求澄清**" (LLM 觉得问题不清) | 改 LLM chunks 解析 | 加 `AWAITING_CLARIFICATION` 枚举 + transition |
| 加"**多 provider 路由**" (Anthropic/OpenAI 走不同 tool executor) | 改 EXECUTING_TOOLS 内部 if-else | 替换 `tools_done` 这个 transition 的 handler |

**示例:加 VERIFYING_ANSWER 阶段**

```python
# 1. 加新阶段枚举
class AgentPhase(Enum):
    ...
    VERIFYING_ANSWER = "verifying_answer"

# 2. 写新 handler
def _do_answer_verification(ctx: _RunContext) -> list[tuple[str, Any]]:
    """检查 LLM 最终回答是否含敏感信息 / 是否需要二次确认。"""
    events: list[tuple[str, Any]] = []
    verdict = ctx.safety_checker.check(ctx.final_answer)
    if verdict == "needs_review":
        events.append(("system", "⚠️ 答案需审查"))
        ctx.next_action = "verification_failed"
    else:
        ctx.next_action = "verification_passed"
    return events

# 3. 加 transitions(不动现有任何 transition)
PhaseTransition(AgentPhase.LLM_THINKING, "llm_responded_final",
                _prepare_for_verify, AgentPhase.VERIFYING_ANSWER),
PhaseTransition(AgentPhase.VERIFYING_ANSWER, "verification_passed",
                _save_final_answer, AgentPhase.FINALIZING),
PhaseTransition(AgentPhase.VERIFYING_ANSWER, "verification_failed",
                _ask_user_to_confirm, AgentPhase.AWAITING_PERMISSION),
```

**改动 = 1 枚举 + 1 handler + 3 transition 行,0 个现有代码修改。**

调试时打印 `self._sm.history`:
```
SETUP              → run_started           → LLM_THINKING
LLM_THINKING       → llm_responded         → EXECUTING_TOOLS
EXECUTING_TOOLS    → tools_done            → LLM_THINKING
LLM_THINKING       → permission_needed     → AWAITING_PERMISSION
AWAITING_PERMISSION → permission_resolved  → EXECUTING_TOOLS
EXECUTING_TOOLS    → tools_done            → LLM_THINKING
LLM_THINKING       → llm_responded_final   → VERIFYING_ANSWER
VERIFYING_ANSWER   → verification_passed   → FINALIZING
FINALIZING         → finalize_done         → DONE
```

---

## 三、职责链设计 (Layer 2 下半)

### 3.1 TurnContext — 共享读写对象

```python
# agent_core/turn_chain.py
from dataclasses import dataclass, field
from typing import Any, Optional, Protocol


@dataclass
class TurnContext:
    """一次 turn 处理的共享上下文(各 handler 读写)。"""
    # 输入
    messages: list[dict]                          # 准备给 LLM 的 messages
    system_prompt: Optional[str] = None
    tool_schemas: list[dict] = field(default_factory=list)
    
    # 中间累积(handler 之间共享)
    chunks: list = field(default_factory=list)    # LLM 流式 chunks
    full_text: str = ""
    thinking_text: str = ""
    tool_calls: list = field(default_factory=list)
    tool_results: list = field(default_factory=list)
    
    # 输出 / 状态
    events: list[tuple[str, Any]] = field(default_factory=list)
    permission_request: Optional[dict] = None
    stop_reason: Optional[str] = None
    
    # 自由扩展点
    metadata: dict = field(default_factory=dict)
    
    def emit(self, etype: str, content: Any) -> None:
        """handler 产 event 的标准方式(链终汇总时再 yield)。"""
        self.events.append((etype, content))
```

### 3.2 TurnHandler 协议 + HandlerResult

```python
class TurnHandler(Protocol):
    """职责链节点协议:处理一段,可能产生 events,可能让链短路。"""


@dataclass
class HandlerResult:
    """单个 handler 的执行结果。"""
    stop_chain: bool = False           # True → 后面的 handler 不跑
    next_action: Optional[str] = None  # 给 state machine 看的信号("permission_needed" 等)
    
    # 注:events 由 handler 直接 emit 到 ctx.events,不返这里
    # (避免每次返新 list 的内存开销)


# 实际 handler 签名
TurnHandlerFn = Callable[[TurnContext], None]
"""handler 签名:接 TurnContext,emit events / 设 stop_chain via ctx.next_action。"""
```

### 3.3 TurnChain 执行器

```python
class TurnChain:
    """职责链执行器:按顺序跑 handlers,遇 stop_action 提前终止。"""
    
    def __init__(self, handlers: list[TurnHandlerFn]):
        self._handlers = handlers
    
    def run(self, ctx: TurnContext) -> None:
        """执行链:handler 直接 emit 到 ctx.events,遇 stop_chain / next_action 提前终止。"""
        for h in self._handlers:
            h(ctx)
            if ctx.permission_request is not None:
                # PermissionCheckHandler 触发短路,后续 handler 不跑
                # state machine 看 ctx.permission_request 决定转 AWAITING_PERMISSION
                break
    
    def __iter__(self):
        """支持 for handler in chain 用法(测试用)。"""
        return iter(self._handlers)
```

### 3.4 内置 6 个 Handler — 1:1 抽自 `_step_one_turn`

```python
# ── 1. MemoryRetrievalHandler:检索记忆注入 messages ─────────
def memory_retrieval_handler(ctx: TurnContext) -> None:
    """M7 ported:记忆检索 → 拼成 system 片段 + emit memory_status。"""
    if not ctx.memory_retriever:
        return
    last_user_msg = next(
        (m for m in reversed(ctx.messages) if m.get("role") == "user"),
        None,
    )
    if not last_user_msg or not isinstance(last_user_msg.get("content"), str):
        return
    try:
        report = ctx.memory_retriever.retrieve(last_user_msg["content"])
        hits = report.hits if hasattr(report, "hits") else []
        if hits:
            mem_block = "\n\n[记忆库 / {} hits]\n".format(len(hits))
            for h in hits:
                mem_block += f"- [{getattr(h, 'type', '?')}] {getattr(h, 'title', '')}: {(getattr(h, 'body', '') or '')[:200]}\n"
            ctx.messages = [{"role": "system", "content": (ctx.system_prompt or "") + mem_block}] + [
                m for m in ctx.messages if m.get("role") != "system"
            ]
        ctx.emit("memory_status", {
            "hits": len(hits),
            "stored_total": ctx.stored_total,
            "injected_tokens": ctx.injected_tokens,
            "zero_hit": len(hits) == 0,
        })
    except Exception as e:
        ctx.logger.warning(f"Memory retrieval failed: {e}")


# ── 2. SystemPromptHandler:把 system_prompt 注入 messages 开头 ──
def system_prompt_handler(ctx: TurnContext) -> None:
    """P2 新增:如果有 system_prompt,添加到消息开头。"""
    if not ctx.system_prompt:
        return
    has_system = any(m.get("role") == "system" for m in ctx.messages)
    if not has_system:
        ctx.messages.insert(0, {"role": "system", "content": ctx.system_prompt})


# ── 3. LLMCallHandler:调 LLM + 收 chunks ──────────────────
def llm_call_handler(ctx: TurnContext) -> None:
    """调 LLM 流式 chat,emit text/thinking/tool_call/usage,设 stop_reason。"""
    ctx.logger.debug("\n" + "=" * 60)
    ctx.logger.debug(f"📤 【发送给 LLM】Turn {ctx.turn}/{ctx.max_turns}")
    ctx.logger.debug("=" * 60)
    ctx.logger.debug(_format_messages_for_log(ctx.messages))
    if ctx.tool_schemas:
        ctx.logger.debug(f"\n📋 可用工具: {[t['name'] for t in ctx.tool_schemas]}")
    
    try:
        llm_chunks = ctx.llm.chat(
            messages=ctx.messages,
            tools=ctx.tool_schemas or None,
            cache_namespace=ctx.cache_namespace,
        )
    except Exception as e:
        error_msg = f"LLM 调用失败: {type(e).__name__}: {e}"
        ctx.logger.error(error_msg)
        ctx.emit("system", f"❌ {error_msg}")
        ctx.emit("text", f"抱歉，遇到了技术问题无法回答：{error_msg}")
        ctx.emit("system", "✅ 回答完成")
        ctx.stop_reason = "error"
        return
    
    # 流式 chunk 循环(原 L1013-1047 段,1:1 搬)
    try:
        for chunk in llm_chunks:
            if chunk.text_delta:
                ctx.full_text += chunk.text_delta.text
                ctx.emit("text", chunk.text_delta.text)
            if chunk.thinking_delta:
                ctx.thinking_text += chunk.thinking_delta.thinking
                ctx.thinking += chunk.thinking_delta.thinking
                ctx.emit("thinking", chunk.thinking_delta.thinking)
            if chunk.tool_call:
                ctx.tool_calls.append(chunk.tool_call)
            if getattr(chunk, "stop_reason", None):
                ctx.stop_reason = chunk.stop_reason
            if chunk.usage:
                ctx.usage = chunk.usage
                ctx.emit("usage", chunk.usage)
    except Exception as e:
        error_msg = f"LLM 流式响应中断: {type(e).__name__}: {e}"
        ctx.logger.error(error_msg)
        ctx.emit("system", f"❌ {error_msg}")
        ctx.emit("text", f"抱歉，响应被中断：{error_msg}")
        ctx.emit("system", "✅ 回答完成")
        ctx.stop_reason = "error"


# ── 4. ToolDispatchHandler:解析 tool_call,emit tool_call event ──
def tool_dispatch_handler(ctx: TurnContext) -> None:
    """Day 3:并行执行所有工具(单工具走串行路径)。
    
    emit tool_call event,执行工具,emit tool_result event。
    PermissionCheckHandler 决定要不要短路。
    """
    if not ctx.tool_calls:
        return
    
    if len(ctx.tool_calls) == 1:
        tc = ctx.tool_calls[0]
        ctx.emit("tool_call", {"name": tc.tool_name, "input": tc.tool_input, "parallel": False})
        ctx.tool_log({"type": "action", "name": tc.tool_name, "input": tc.tool_input})
        # Permission check 在下一个 handler(短路控制)
    else:
        tool_names = [tc.tool_name for tc in ctx.tool_calls]
        ctx.emit("tool_call", {"names": tool_names, "parallel": True})
        ctx.tool_log({"type": "parallel_start", "names": tool_names})


# ── 5. PermissionCheckHandler:permission 短路控制点 ────────
def permission_check_handler(ctx: TurnContext) -> None:
    """M12:权限检查(对齐 doc §6.3)。
    
    - decision=allow → 不短路,正常走 tool_execute
    - decision=deny → emit tool_result(deny),但仍继续 chain(让 SessionPersist 落盘)
    - decision=ask → 短路,设 ctx.permission_request,emit "awaiting_permission"
    """
    if not ctx.tool_calls:
        return
    
    for tc in ctx.tool_calls:
        decision = ctx.permission_engine.check(tc.tool_name, tc.tool_input)
        if decision.behavior == "ask":
            ctx.permission_request = {
                "tool_name": tc.tool_name,
                "tool_input": tc.tool_input,
                "reason": getattr(decision.decision_reason, "reason", ""),
                "message": decision.message or "",
                "tool_use_id": tc.tool_use_id,
            }
            ctx.emit("awaiting_permission", ctx.permission_request)
            return  # 短路,后续 handler 不跑
        elif decision.behavior == "deny":
            tool_output = decision.message or "Permission denied"
            ctx.emit("tool_result", {
                "name": tc.tool_name,
                "output": tool_output,
                "success": False,
                "elapsed": 0.0,
            })
            ctx.tool_log({"type": "result", "name": tc.tool_name, "output": tool_output, "success": False})
            ctx.messages.append(_make_tool_result_block(tc.tool_use_id, tool_output))
            ctx.tool_results.append((tc.tool_use_id, tool_output))


# ── 6. ToolExecuteHandler:实际跑 tool_call ─────────────────
def tool_execute_handler(ctx: TurnContext) -> None:
    """Day 3:并行执行(单工具走串行)。"""
    if not ctx.tool_calls or ctx.permission_request is not None:
        return  # 被 permission 短路了
    
    if len(ctx.tool_calls) == 1:
        # 串行(原 L1135-1185 段)
        tc = ctx.tool_calls[0]
        start_time = time.time()
        result = ctx.tools.execute(tc.tool_name, tc.tool_input, max_retries=3)
        elapsed = time.time() - start_time
        # ... emit tool_result / append to messages
    else:
        # 并行(原 L1186-1258 段,ThreadPoolExecutor)
        ...


# ── 7. SessionPersistHandler:写 session ─────────────────────
def session_persist_handler(ctx: TurnContext) -> None:
    """Claude Code 风格:assistant+tool_use 一条 Entry,tool_results 一条 Entry。"""
    if not ctx.session_manager or not ctx.tool_calls:
        return
    try:
        tc_list = [{"id": tc.tool_use_id, "name": tc.tool_name, "input": tc.tool_input}
                   for tc in ctx.tool_calls]
        ctx.session_manager.add_assistant_with_tools(
            text=ctx.full_text,
            tool_calls=tc_list,
        )
        results = [{"tool_use_id": tid, "content": output}
                   for tid, output in ctx.tool_results]
        ctx.session_manager.add_tool_results(results)
    except Exception as e:
        ctx.logger.warning(f"Failed to save intermediate turn to session: {e}")
    finally:
        ctx.tool_results = []  # 防残留
```

### 3.5 TurnChain 实际组装

```python
class ReactAgent:
    def __init__(
        self,
        ...,
        extra_turn_handlers: list[TurnHandlerFn] = None,
    ):
        # 内置 7 个 handler(可被 extra 覆盖)
        base_handlers = [
            memory_retrieval_handler,
            system_prompt_handler,
            llm_call_handler,
            tool_dispatch_handler,
            permission_check_handler,
            tool_execute_handler,
            session_persist_handler,
        ]
        all_handlers = base_handlers + (extra_turn_handlers or [])
        self._turn_chain = TurnChain(all_handlers)
```

### 3.6 职责链扩展场景:加新处理零改动

| 场景 | 现状(改 `_step_one_turn`) | 职责链(写新 handler) |
|---|---|---|
| 加"**LLM 响应后 safety 二次审查**" (检测 prompt injection) | 改 chunks 解析段 + 加 if | 写 `SafetyRecheckHandler` 插在 `LLMCallHandler` 之后 |
| 加"**cost tracking**" (每次 LLM call 累加 USD) | 改 LLM call 段 + 加累加 | 写 `CostTrackingHandler`,插在 `LLMCallHandler` 前后 |
| 加"**rate limiter**" (per-minute token 限速) | 改 LLM call 段 + 加 sleep | 写 `RateLimitHandler`,emit 限速 event + 设 stop_chain |
| 加"**tool call telemetry**" (每个 tool 写 audit) | 改 tool execute 段 | 写 `ToolTelemetryHandler` 插在 `ToolExecuteHandler` 之后 |
| 加"**多 provider 路由**" (Anthropic 走 Claude tool format,OpenAI 走 function call) | 改 chunks 解析段 | 替换 `LLMCallHandler` 这一个 handler(其他不动) |
| 加"**prompt A/B test**" (同 prompt 走两 provider 比对) | 改 LLM call 段 | 写 `ABTestHandler` 插在 `LLMCallHandler` 之前(改写 messages) |

**3rd party 集成**: `extra_turn_handlers=[]` 参数让外部(插件 / 实验)可以在不 fork agent 的前提下,挂自己的处理。

**示例:加 CostTrackingHandler**

```python
# 1. 写新 handler(不动现有 7 个 handler 中的任何一个)
def cost_tracking_handler(ctx: TurnContext) -> None:
    """每次 LLM call 后累加 USD,emit usage_cost event 给 UI 展示。"""
    if ctx.usage:
        cost_usd = ctx.usage.input_tokens * 0.000003 + ctx.usage.output_tokens * 0.000015
        ctx.metadata["total_cost_usd"] = ctx.metadata.get("total_cost_usd", 0) + cost_usd
        ctx.emit("usage_cost", {
            "input_tokens": ctx.usage.input_tokens,
            "output_tokens": ctx.usage.output_tokens,
            "cost_usd": cost_usd,
            "total_cost_usd": ctx.metadata["total_cost_usd"],
        })

# 2. 在 agent 构造时注入
agent = ReactAgent(
    ...,
    extra_turn_handlers=[cost_tracking_handler],  # 注入 1 个 handler
)
# 现有 7 个 handler、StateMachine、_RunContext 全部不动
```

---

## 四、Layer 3 — `run()` 拆出来的私有 method

### 4.1 `_RunContext` — 替代 6 个隐式 locals

```python
# agent_core/agent_state.py
@dataclass
class _RunContext:
    """一次 run 的共享状态(替代 run() 顶层 locals)。"""
    user_message: str
    turn: int = 0
    max_turns: int = 10
    
    # last turn tracking(供 run 末尾 bridge.on_turn_end 使用)
    last_input_tokens: int = 0
    last_output_tokens: int = 0
    last_tool_calls: list = field(default_factory=list)
    
    # final answer tracking
    final_answer: Optional[str] = None
    final_stop_reason: Optional[str] = None
    
    # state machine 推进
    sm: Optional[StateMachine] = None
    turn_chain: Optional[TurnChain] = None
    turn_ctx: Optional[TurnContext] = None  # 当前 turn 的上下文(链中累积)
    
    # 跨 turn 共享
    awaiting_permission: Optional[dict] = None
    is_done: bool = False
```

### 4.2 `_setup_run` / `_step_one_turn` / `_finalize_run`

```python
class ReactAgent:
    # ── A 段:Setup ──────────────────────────────────────────
    def _setup_run(self, user_message: str) -> Iterator[tuple[str, Any]]:
        """初始化:append user msg + 重置 pending + L3 SM 决策 + ContextManager compact。
        
        原 run() L762-893 段,1:1 搬。
        """
        self.messages.append({"role": "user", "content": user_message})
        # ... 持久化 user msg
        self._pending_thinking = ""
        self._pending_tool_logs = []
        self._pending_tool_results = []
        
        # L3 SM 决策 + ContextManager compact
        # ... (原 L779-893)
        
        yield from iter(self._sm.trigger("run_started", self._run_ctx))
    
    # ── C 段:Step one turn ──────────────────────────────────
    def _step_one_turn(self) -> Iterator[tuple[str, Any]]:
        """执行一个 LLM cycle。
        
        原 run() L909-1283 段(去掉 B 段 turn loop init)。
        内部走 turn chain(职责链)。
        """
        self._run_ctx.turn += 1
        yield ("system", f"🔄 Turn {self._run_ctx.turn}/{self._run_ctx.max_turns}")
        
        # 准备 turn context
        turn_ctx = TurnContext(
            messages=list(self.messages),
            system_prompt=self.system_prompt,
            tool_schemas=self.tools.list_schemas(provider=self._detect_provider()),
            turn=self._run_ctx.turn,
            max_turns=self._run_ctx.max_turns,
            llm=self.llm,
            tools=self.tools,
            permission_engine=self.permission_engine,
            session_manager=self._session_manager,
            memory_retriever=self.memory_retriever,
            cache_namespace=f"react:{self._session_manager.session_id if self._session_manager else 'default'}",
        )
        self._run_ctx.turn_ctx = turn_ctx
        
        # 走 turn chain
        self._turn_chain.run(turn_ctx)
        yield from turn_ctx.events
        
        # 处理 chain 结果
        if turn_ctx.permission_request is not None:
            self._run_ctx.awaiting_permission = turn_ctx.permission_request
            # permission check 短路 → 留给 state machine 转 AWAITING_PERMISSION
        
        # 更新 _RunContext
        self._run_ctx.last_input_tokens = turn_ctx.usage.input_tokens if turn_ctx.usage else 0
        self._run_ctx.last_output_tokens = turn_ctx.usage.output_tokens if turn_ctx.usage else 0
        self._run_ctx.last_tool_calls = list(turn_ctx.tool_calls)
        
        if not turn_ctx.tool_calls:
            # final answer 路径
            self._run_ctx.final_answer = turn_ctx.full_text
            self._run_ctx.final_stop_reason = turn_ctx.stop_reason
            self._run_ctx.is_done = True
        else:
            # 有 tool_call → 继续 next turn(等 state machine 决策)
            pass
    
    # ── D + E + F 段:Finalize ───────────────────────────────
    def _finalize_run(self) -> Iterator[tuple[str, Any]]:
        """run 末尾:bridge.on_turn_end + L3 SM extract + session flush。
        
        原 run() L1285-1373 段,1:1 搬。
        """
        # D 段:bridge.on_turn_end(记忆提取)
        if (self.react_memory_bridge
            and self._run_ctx.turn > 0
            and self._run_ctx.final_answer
            and self._run_ctx.user_message
            and self._run_ctx.final_stop_reason not in _TRUNCATED_STOP_REASONS):
            try:
                for event in self.react_memory_bridge.on_turn_end(
                    user_msg=self._run_ctx.user_message,
                    assistant_resp=self._run_ctx.final_answer,
                    turn_index=self._run_ctx.turn,
                    input_tokens=self._run_ctx.last_input_tokens,
                    output_tokens=self._run_ctx.last_output_tokens,
                    tool_calls_in_turn=len(self._run_ctx.last_tool_calls),
                ):
                    yield ("memory_event", event)
            except Exception as e:
                _logger.warning(f"Memory bridge failed: {e}")
        
        # E 段:L3 SM extract(后台)
        # ... 原 L1320-1365 段,1:1 搬
        
        # F 段:Session flush
        if self._session_manager:
            try:
                self._session_manager.flush()
                _logger.debug(f"Session saved: {self._session_manager.session_id}")
            except Exception as e:
                _logger.warning(f"Failed to flush session: {e}")
```

### 4.3 `run()` 改薄壳(向后兼容)

```python
class ReactAgent:
    def run(self, user_input: str) -> Iterator[tuple[str, Any]]:
        """向后兼容:旧 caller(测试 + app_langgraph + 00_Chat)继续可 for-loop 用。
        
        内部调 start_run + step,遇 awaiting_permission 走 _wait_for_permission_legacy
        (复用现有 Event.wait 0.1s × N poll 路径)。
        """
        self.start_run(user_input)
        
        # A 段
        for ev in self._setup_run(user_input):
            yield ev
        
        # C 段循环
        while not self._run_ctx.is_done:
            for ev in self._step_one_turn():
                yield ev
            
            if self._run_ctx.awaiting_permission is not None:
                # 旧路径:测试线程调 resolve_permission() 触发 Event.set()
                self._wait_for_permission_legacy(self._run_ctx.awaiting_permission)
                # 继续 next step()
        
        # D + E + F 段
        for ev in self._finalize_run():
            yield ev
```

### 4.4 旧 API `resolve_permission` 保留

```python
class ReactAgent:
    def resolve_permission(self, choice: str) -> None:
        """旧 API,内部委托给 resume_after_permission。
        
        测试 + app_langgraph + 00_Chat 仍可调这个方法(Event 路径)。
        """
        self.resume_after_permission(choice)
    
    def resume_after_permission(self, choice: str) -> None:
        """新 API:UI 决定后调,等价于旧 resolve_permission。
        
        把 choice 注入 _RunContext,触发 state machine 走 permission_resolved。
        """
        if self._run_ctx is None or not self._run_ctx.awaiting_permission:
            raise RuntimeError("当前没有 awaiting_permission 状态")
        self._run_ctx.awaiting_permission["choice"] = choice
        # 触发 state machine
        if self._sm.current == AgentPhase.AWAITING_PERMISSION:
            events = self._sm.trigger("permission_resolved", self._run_ctx)
            for ev in events:
                # 注:旧 Event 路径仍保留(测试用),新 UI 不用
                ...
    
    def _wait_for_permission_legacy(self, request: dict) -> None:
        """旧路径:Event.wait 0.1s × N poll,直到测试线程调 resolve_permission 触发 Event.set()。"""
        ...
```

---

## 五、_ask_user_permission 改非阻塞 + sentinel

### 5.1 现状(L664-739)

```python
def _ask_user_permission(self, tool_name, tool_input, decision) -> str:
    """阻塞版:在主线程上 Event.wait(0.1s) 100ms,默认 deny。"""
    # ... hook 早期决策 ...
    self._pending_permission_request = {...}
    got_response = self._permission_resolved.wait(timeout=0.1)  # ← 阻塞主线程
    if not got_response:
        return "deny"  # 默认 deny(Streamlit 上这就是 bug 现场)
    return self._pending_permission_request.get("choice", "deny")
```

### 5.2 改后(非阻塞,统一 sentinel)

```python
def _ask_user_permission(self, tool_name, tool_input, decision) -> str:
    """非阻塞版:返 'ALLOW' / 'DENY_BY_HOOK' / 'AWAITING_PERMISSION' 三态 sentinel。
    
    'AWAITING_PERMISSION' 表示需要 UI 决定,调用方(step)立即 return。
    主线程不再被 Event.wait 阻塞。
    """
    # hook 早期决策保留(行为不变)
    hook_decision = self._run_permission_request_hook(tool_name, tool_input)
    if hook_decision == "allow":
        return "ALLOW"
    if hook_decision == "deny":
        return "DENY_BY_HOOK"
    
    # 存 pending(供 step() + resume_after_permission 读)
    self._pending_permission_request = {
        "tool_name": tool_name,
        "tool_input": tool_input,
        "reason": getattr(decision.decision_reason, "reason", "") if decision.decision_reason else "",
        "message": decision.message or "",
        "tool_use_id": getattr(decision, "tool_use_id", None),
    }
    # 保留 Event 给 legacy 路径(测试 / app_langgraph / 00_Chat 用)
    if self._permission_resolved is None:
        self._permission_resolved = threading.Event()
    return "AWAITING_PERMISSION"
```

**调用方(在职责链 `permission_check_handler` 中)处理 sentinel**:

```python
def permission_check_handler(ctx: TurnContext) -> None:
    """M12:权限检查(对齐 doc §6.3)。
    
    _check_tool_permission 内部调 _ask_user_permission,可能返:
    - "ALLOW" → 不短路,正常 tool execute
    - "DENY_BY_HOOK" → emit deny tool_result,继续 chain(让 SessionPersist 落盘)
    - "AWAITING_PERMISSION" → 短路,emit "awaiting_permission" event
    """
    for tc in ctx.tool_calls:
        allowed, perm_err, effective_input = ctx.permission_checker.check(
            tc.tool_name, tc.tool_input
        )
        if allowed == "AWAITING_PERMISSION":
            # 非阻塞路径:UI 决定后,UI 调 agent.resume_after_permission("allow")
            ctx.permission_request = ctx.permission_checker.pending_request
            ctx.emit("awaiting_permission", ctx.permission_request)
            return
        elif allowed == "DENY_BY_HOOK":
            tool_output = perm_err or "Permission denied"
            ctx.emit("tool_result", {...})
            ...
        # else: ALLOW → 继续
```

---

## 六、向下游 `web/app.py` 的契约

### 6.1 新 API 形态

```python
# web/app.py 调用方式(伪代码)
agent = st.session_state.agent  # ReactAgent 实例

# 1. 用户发消息
agent.start_run(prompt)
st.session_state._run_phase = "running"
st.rerun()  # 立即返回顶部,让 step() 在下次 rerun 执行

# 2. 顶部"run 推进段"在每次 rerun 跑
if st.session_state._run_phase == "running":
    result = agent.step()
    # 把 events 应用到 _run_accum(累积状态)
    for ev_type, ev_content in result.events:
        _apply_event_to_accum(ev_type, ev_content, st.session_state._run_accum)
    # 渲染当前累积状态
    _render_accumulated_run(st.session_state._run_accum, ...)
    
    if result.awaiting_permission is not None:
        st.session_state._run_phase = "awaiting_permission"
    elif result.is_done:
        st.session_state._run_phase = "idle"
    
    if st.session_state._run_phase in ("running", "awaiting_permission"):
        st.rerun()  # 继续推进 / 让 dialog 渲染

elif st.session_state._run_phase == "awaiting_permission":
    # dialog 已通过 @st.dialog 装饰,在 click handler 触发
    # click handler:
    #   agent.resume_after_permission("allow")
    #   st.session_state._run_phase = "running"
    #   st.rerun()
    pass
```

### 6.2 状态机 × Streamlit rerun 配合

```
streamlit rerun N+1                  streamlit rerun N+2
        │                                    │
        ▼                                    ▼
┌──────────────────┐               ┌──────────────────┐
│ _run_phase=      │               │ _run_phase=      │
│   "running"      │  step()       │ "awaiting_perm"  │
│                  │ ─────────►   │                  │
│ agent.step()     │  returns:    │ @st.dialog 渲染  │
│   → events       │   awaiting_  │ 用户点 Allow     │
│   → awaiting_    │   permission │   → resume_after │
│     permission   │   = {...}    │     _permission  │
│ st.rerun()       │               │   → phase=running│
└──────────────────┘               │   → st.rerun()  │
                                   └──────────────────┘
```

---

## 七、实施清单(预估工作量)

| 步骤 | 改动 | 估计行数 | 风险 |
|---|---|---|---|
| 1. 抽 `_RunContext` + `AgentPhase` + `PhaseTransition` + `StateMachine` | 新文件 `agent_core/agent_state.py` | +180 | 低 |
| 2. 抽 `TurnContext` + `TurnChain` + 7 个内置 handler | 新文件 `agent_core/turn_chain.py` | +350 | 中(handler 边界划分需谨慎) |
| 3. `agent_core.py` 加 `start_run/step/resume_after_permission` + `StepResult` | 改 `agent_core.py` | +200 | 中(公开 API 增多,需向后兼容) |
| 4. `_ask_user_permission` 改非阻塞 + sentinel | 改 `agent_core.py` | -10 / +20 | 中(默认行为变化:不再"自动 deny"→"等 UI 决定") |
| 5. `run()` 改薄壳(调 step) | 改 `agent_core.py` | -570 / +50 | 低(公开 API 兼容) |
| 6. `web/app.py` 改 state machine(session_state 驱动) | 改 `web/app.py` | +180 / -100 | 中(Streamlit rerun cycle 调试) |
| 7. 新加 e2e 测试 | 新文件 `tests/test_agent_state_machine.py` | +200 | 低 |
| 8. 新加兼容性测试(旧 `run()` + `resolve_permission` 路径) | `tests/test_permission_integration.py` | +30 | 低 |
| 9. 跑全量回归(300+ 测试) | 验证 | — | — |

**总计**: 9 步,~900 行新增 / ~680 行删减(净 +220 行,职责更清晰)

---

## 八、测试 / 验证

### 8.1 单元测试(快,~10s)

```bash
python -m pytest tests/test_agent_state_machine.py -v --no-header
```

覆盖:
- `StateMachine.trigger` 正常转移 / InvalidTransition
- `TurnChain` 顺序执行 / 短路 / events 累积
- `start_run` 后 `_run_ctx` 状态正确
- `step()` 一次 LLM turn 返 `StepResult`(mock LLM)
- 遇 `ask` → `step()` 返 `awaiting_permission`,不再 emit `tool_result`
- `resume_after_permission("allow")` 后下次 `step()` 继续
- `final_answer` 路径 `step()` 返 `is_done=True`

### 8.2 回归测试(必须全过,~60s)

```bash
python -m pytest tests/ -q --no-header
```

重点关注:
- `test_streaming.py`(走 `run()` 的 yield 路径,验证事件顺序不变)
- `test_checkpointer.py`(走 `run()` 的 session flush 路径)
- `test_react_agent_bridge.py`(走 `_finalize_run` 的 `bridge.on_turn_end` 路径)
- `test_sm_layer_integration.py`(走 `_setup_run` + `_finalize_run` 的 L3 SM 路径)
- `test_permission_logging.py`(走职责链 `permission_check_handler` 的日志)
- `test_permission_integration.py`(旧 `run()` + `resolve_permission` 兼容性)

### 8.3 E2E 验证(手工,~30s)

```bash
AGENT_LOG_PERMISSION=DEBUG AGENT_LOG_SANDBOX=INFO \
  HF_HUB_OFFLINE=1 .venv/bin/streamlit run web/app.py
```

1. `chat_input` 输入 `请运行 echo hello`
2. 期望:🔐 dialog 弹出,**不** 再出现 `🛡️ [ask_user_timeout]`
3. 点 "✅ Allow once"
4. 期望:agent 收到 `Bash` tool_result,继续生成最终回答,显示 `hello`
5. 看 [`logs/app/agent.log`](../logs/app/agent.log) 应有:
   ```
   🛡️ [step_7_default_ask] behavior=ask
   🛡️ [ask_user_permission_entry]
   🪝 [permission_request_no_hooks]
   🛡️ [ask_user_response] tool=Bash choice=allow wait_ms=4500.0   ← 不再 103.5
   ⚙️ [bash_handler_entry] command=echo hello
   ⚙️ [bash_subprocess_done] exit_code=0 stdout_len=6
   ```
6. 重复几次(不同 prompt),确认 `wait_ms` 持续在秒级,UI 真在响应

---

## 九、兼容性 / 风险

### 9.1 兼容性保证

- **公开 API 不变**: `run()` / `resolve_permission()` 签名 + 行为兼容,300+ 测试 / `web/app_langgraph.py` / `web/pages/00_Chat.py` 全不破
- **现有 logger 不变**: 🛡️ `[ask_user_permission_entry]` / `[ask_user_response]` / `[ask_user_timeout]` 仍工作,只是 `wait_ms` 不再卡在 100ms
- **`auto_allow_ask=True` 路径不变**: 仍走"立即 allow",不弹 dialog
- **PermissionRequest hook 行为不变**: hook 返 allow/deny 时仍立即决策
- **event tuple 格式不变**: `("text", str)` / `("tool_call", dict)` 等保持不变,向后兼容所有 caller

### 9.2 风险

| 风险 | 等级 | 缓解 |
|---|---|---|
| turn 内部 4 个 yield 路径,抽到 `_step_one_turn` 时 yield 顺序必须一致 | 中 | 已被 300+ 测试覆盖,逐个回归 |
| `_pending_tool_results` 的清空时机(L1282)与 session 持久化有强耦合 | 中 | 抽到 `session_persist_handler` 时保留原 finally 块结构 |
| Streamlit 端的 state machine 改动大 | 中 | agent_core 状态机**完全独立可用**(只测 agent_core 也成立) |
| 状态机/职责链抽象的"过渡设计"风险 | 低 | 已确定产品会持续演进(M12+ 加阶段 / 3rd party 集成) |
| 多一层抽象(8 个 class / 3 个协议)对新人理解成本 | 低 | 文档 + 调试时打印 `self._sm.history` 一目了然 |

### 9.3 性能

- 状态机 transition 字典查找 O(1)
- 职责链 for 循环(同现状)
- 无 regression

---

## 十、不做(显式排除)

- ❌ **不**把单工具串行/多工具并行合并(用户没要,风险高)
- ❌ **不**动 event tuple schema
- ❌ **不**改 `_check_tool_permission` 的 7-step pipeline 内部(只把"blocking Event.wait"替换为"sentinel return",行为兼容)
- ❌ **不**改 L3 SM / ContextManager 内部逻辑
- ❌ **不**重构 `_handle_permission_dialog` UI(只改 click handler 3 行)
- ❌ **不**动 `web/app_langgraph.py` / `web/pages/00_Chat.py`
- ❌ **不**改 `_pending_permission_request` / `_permission_resolved` 字段名 / 结构(legacy 路径用)
