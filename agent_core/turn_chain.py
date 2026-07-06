"""
Turn Chain — 职责链(Chain of Responsibility)

v2 重构引入(详见 docs/agent-state-machine-and-chain-of-responsibility-design.md §4):

核心组件:
1. HandlerResult:handler 执行结果(stop_chain + next_action)
2. Handler 协议:每个 handler 是 class,__init__ 注入 agent 引用,handle(ctx) -> HandlerResult
3. TurnChain:执行器,按顺序跑 handlers,遇 stop_chain 提前终止
4. TurnContext:per-turn 工作内存(handler 间共享)
5. 11 个内置 handler class(4 个 chain 分类)

Plan A 实施(2026-06-30):handler 接真业务,每个 handler.handle() 通过
agent 引用调对应的 _iter_xxx() helper。run() 也复用同一套 helper,
行为完全一致。
"""

from __future__ import annotations

import concurrent.futures
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterator, List, Literal, Optional, Protocol, runtime_checkable

from agent_core.agent_state import AgentPhase, Event, RunState, TurnContext
from agent_core.agent_core import _TRUNCATED_STOP_REASONS
# 002-skill-secret-injection (T009): SkillsPromptHandler 用 apply_skill_env_overrides 注入 secret
from agent_core.skills.env_overrides import apply_skill_env_overrides


__all__ = [
    "HandlerResult",
    "Handler",
    "TurnChain",
    "SystemPromptAssembler",
    "_LLMResult",
    "TcPermissionDecision",
    "MemoryRetrievalHandler",
    "SkillsPromptHandler",
    "SystemPromptHandler",
    "ToolsSchemaPrepareHandler",
    "LLMCallHandler",
    "ChunkParseHandler",
    "PermissionCheckHandler",
    "ToolDispatchHandler",
    "ToolExecuteHandler",
    "LLMCallPersistHandler",
    "ToolPairPersistHandler",
    "FinalAnswerBookkeepingHandler",
    "FinalAnswerPersistHandler",
    "AuditLogHandler",
    "MemoryBridgeExtractHandler",
    "SessionFlushHandler",
    "L3SMExtractTriggerHandler",
    "EnvCleanupHandler",
]


_logger = logging.getLogger("agent_core.turn_chain")
permission_logger = logging.getLogger("agent_core.permission")  # Plan C: _check_tool_permission 迁入用


# 2026-07-02:tool execute 最大重试次数常量。取代原 _iter_phase_tools L880 + L977 重复字面量 3。
# 2026-07-02 Step 6 后:_iter_phase_tools 已删(thin orchestrator 时代)。字面量只剩本常量 1 处。
DEFAULT_MAX_RETRIES = 3


# ────────────────────────────────────────────────────────────
# 模块级纯函数(2026-07-02 Plan C 从 ReactAgent 迁入)
# ────────────────────────────────────────────────────────────
# 这 4 个 helper 在 agent_core.py 本文件 0 内部调用,只被本模块 handler 用 ——
# SRP 上属于 turn_chain(消费者)而非 ReactAgent。迁入为模块级纯函数(零 agent state),
# 消除 handler 经 agent._xxx() 的反向依赖。
#
# 迁入来源:agent_core/agent_core.py:_detect_provider / _messages_with_ids /
# _estimate_tokens / _estimate_message_tokens(Plan C Step 1)。


def detect_provider(llm) -> str:
    """从 llm_router 的 config 判断当前 provider。

    2026-07-02 Plan C:从 ReactAgent._detect_provider 迁入(原 agent_core.py:1187)。
    LLMCallHandler + ContextCompactionHandler 用(原 agent._detect_provider() 3 处调用)。
    """
    provider = llm.config.provider
    if isinstance(provider, str):
        return provider
    # 如果是枚举
    return str(provider.value) if hasattr(provider, "value") else "anthropic"


def messages_with_ids(messages: list[dict]) -> list[dict]:
    """给 messages 注入 stable id(仅当 m 无 id)。

    2026-07-02 Plan C:从 ReactAgent._messages_with_ids 迁入(原 agent_core.py:478)。

    背景:sm_layer._slice_kept_messages 通过 m.get("id") 找 last_id,
    但 messages 只有 {role, content} 没有 id,导致 _slice_kept 永远走 last_id is None
    全返路径。enumerate 索引当 id ("m0", "m1", ...),纯运行时不写盘,caller 用完即弃。
    ContextCompactionHandler + L3SMExtractTriggerHandler 用。
    """
    out = []
    for i, m in enumerate(messages):
        if "id" not in m:
            m = {**m, "id": f"m{i}"}
        out.append(m)
    return out


def estimate_tokens(text: str) -> int:
    """估算 Token 数(更精确的系数)。

    2026-07-02 Plan C:从 ReactAgent._estimate_tokens 迁入(原 agent_core.py:497)。
    - 中文字符 ≈ 1.4 tokens/字(Anthropic 官方约 1.3~1.5)
    - 英文字符 ≈ 0.25 tokens/字
    - Overhead: 每条消息额外 ~10 tokens(role/结构/markers)
    """
    if not text:
        return 0
    chinese_chars = sum(1 for c in text if '一' <= c <= '鿿')
    english_chars = len(text) - chinese_chars
    return int(chinese_chars * 1.4 + english_chars * 0.25 + 10)


def estimate_message_tokens(msg: dict) -> int:
    """估算单条消息的 Token 数(含 system/assistant/user/tool 不同 role)。

    2026-07-02 Plan C:从 ReactAgent._estimate_message_tokens 迁入(原 agent_core.py:512)。
    ContextCompactionHandler 用(L477 估算 total_tokens 判 SM fast-path 是否触发)。
    - system: ~15 tokens overhead
    - assistant/user: ~10 tokens overhead
    - tool_use: ~30 tokens(tool_use marker + name + input)
    - tool_result: ~30 tokens(tool_result marker + output)
    """
    import json
    role = msg.get("role", "")
    overhead = {"system": 15, "assistant": 10, "user": 10, "tool": 30}.get(role, 10)

    content = msg.get("content", "")
    if isinstance(content, str):
        return estimate_tokens(content) + overhead
    elif isinstance(content, list):
        total = overhead
        for block in content:
            if isinstance(block, dict):
                block_type = block.get("type", "")
                if block_type == "text":
                    text = block.get("text", "")
                    total += estimate_tokens(text)
                elif block_type == "tool_use":
                    # tool_use block: name + input JSON
                    name = block.get("name", "")
                    inp = json.dumps(block.get("input", {}))
                    total += estimate_tokens(name) + estimate_tokens(inp) + 20
                elif block_type == "tool_result":
                    # tool_result block: content
                    text = str(block.get("content", ""))
                    total += estimate_tokens(text) + 15
                else:
                    text = str(block)
                    total += estimate_tokens(text)
        return total
    return 0


# ────────────────────────────────────────────────────────────
# _LLMResult — LLM phase 输出(2026-07-02 从 agent_core.py 迁入)
# ────────────────────────────────────────────────────────────
# LLMCallHandler / ChunkParseHandler 写入,ToolExecuteHandler / LLMCallPersistHandler /
# FinalAnswerPersistHandler 消费。放在 turn_chain.py 因为 LLMCallHandler + ChunkParseHandler
# 是它的主用户(从 agent_core.py:_iter_phase_llm 拆出后,_LLMResult 的写入全部转到这里)。


@dataclass
class _LLMResult:
    """LLM phase 输出(handler 间共享)。"""
    tool_calls: list = field(default_factory=list)
    full_text: str = ""
    thinking_text: str = ""
    stop_reason: Optional[str] = None
    usage: Optional[Any] = None  # UsageStats


# ────────────────────────────────────────────────────────────
# TcPermissionDecision — 单 tool 的 permission 决策
# ────────────────────────────────────────────────────────────
# 2026-07-02 引入(Plan B 完整实现:PermissionCheckHandler 拆分):
# PermissionCheckHandler.handle() 把每个 tool_call 分类到三态(allow / ask / deny),
# 产出 TcPermissionDecision 列表作为 ctx.permission_decisions,供 ToolDispatchHandler
# 和 ToolExecuteHandler 消费(取代原 _iter_phase_tools 单方法内的内联逻辑)。
#
# field 语义:
# - outcome:三态之一。allow → 走 tools.execute;deny → emit 假 tool_result;
#   ask → 写 RunState.awaiting_permission_batch + 强制 SM AWAITING。
# - request:仅 ask 时填充。permission request dict(tool_name/tool_input/reason/message/tool_use_id)。
# - effective_input:仅 allow 时填充。permission engine 可能 modify 的输入(用于重写 sanitize)。
# - error:仅 deny 时填充。错误文案(perm_err 或 "Permission denied" 默认)。


@dataclass(frozen=True)
class TcPermissionDecision:
    """单 tool 的 permission 决策(handler 间传递用)。

    设计要点:
    - frozen=True 强制 immutable,防止下游 handler 误改决策
    - outcome 三态字符串字面量,Literal 类型让下游 switch 编译期检查
    - 字段按 outcome 而非全部 nullable 设计:看 outcome 决定读哪些字段,降低误读
    """
    tc_id: str                                        # tool_use_id(从 tc.tool_use_id 取)
    tc: Any                                           # 原始 ToolCall 对象(LLM 产出的)
    outcome: Literal["allow", "deny", "ask"] = "allow"
    request: Optional[Dict[str, Any]] = None          # 仅 ask:permission request dict
    effective_input: Optional[Dict[str, Any]] = None  # 仅 allow:permission engine 改写后的 input
    error: Optional[str] = None                       # 仅 deny:错误文案


def decision_outcome_counts(decisions: List[TcPermissionDecision]) -> Dict[str, int]:
    """统计决策列表的 outcome 分布(给 PermissionCheckHandler / tests 用)。

    返回示例:{"allow": 2, "ask": 1, "deny": 0}。
    """
    counts: Dict[str, int] = {"allow": 0, "deny": 0, "ask": 0}
    for d in decisions:
        counts[d.outcome] = counts.get(d.outcome, 0) + 1
    return counts


# ────────────────────────────────────────────────────────────
# M11 L1 H2 段:放在 turn_chain.py 因为它是 SystemPromptAssembler.build() 的产物。
# 借鉴 Claude Code:提醒 LLM 在依据记忆做推荐前,先验证文件/函数/flag 是否仍存在。
# 记忆是过去的快照,不能假定当下仍为真。
TRUSTING_RECALL_SECTION = """
## Before recommending from memory

A memory that names a specific function, file, or flag is a claim that it existed *when the memory was written*. It may have been renamed, removed, or never merged. Before recommending it:
- If the memory names a file path: check the file exists.
- If the memory names a function or flag: grep for it.
- If the user is about to act on your recommendation (not just asking about history), verify first.

"The memory says X exists" is not the same as "X exists now."
"""


# ────────────────────────────────────────────────────────────
# HandlerResult — 单个 handler 的执行结果
# ────────────────────────────────────────────────────────────

@dataclass
class HandlerResult:
    """单个 handler 的执行结果。

    关键字段:
    - stop_chain:True → 后续 handler 不跑(短路)
    - next_action:给 state machine 看的信号(可选,目前未使用,保留扩展)
    """
    stop_chain: bool = False
    next_action: Optional[str] = None


# ────────────────────────────────────────────────────────────
# Handler 协议
# ────────────────────────────────────────────────────────────

@runtime_checkable
class Handler(Protocol):
    """职责链节点协议。

    实现要求:
    - name:str(用于 named hook point)
    - handle(ctx: TurnContext) -> HandlerResult
    """
    name: str

    def handle(self, ctx: TurnContext) -> HandlerResult: ...


class PluginHandler:
    """3rd party plugin handler 基类(详见设计文档 §12)。

    权限受限:
    - 只能读 ctx(turn 上下文),不直接碰 self.xxx
    - 只能 emit 白名单内的 event type
    - 只能 append 到 chain 末端(不能在 LLMCall 之前插)

    验证:AgentBuilder.with_plugin_handler() 入口 type check。
    """
    ALLOWED_EVENT_TYPES = {"system", "metric", "telemetry", "ui_hint"}

    name: str = "plugin_handler"

    def handle(self, ctx: TurnContext) -> HandlerResult:
        raise NotImplementedError("PluginHandler 子类必须实现 handle()")

    def emit_validated(self, event: Event, ctx: TurnContext) -> None:
        """emit event 时白名单 check。"""
        if event[0] not in self.ALLOWED_EVENT_TYPES:
            raise SecurityError(
                f"PluginHandler {self.name} 不允许 emit event type='{event[0]}',"
                f"允许的类型:{self.ALLOWED_EVENT_TYPES}"
            )
        ctx.emit(event)


class SecurityError(Exception):
    """PluginHandler emit 不在白名单 event type 时抛错。"""


# ────────────────────────────────────────────────────────────
# SystemPromptAssembler — system_prompt 装配的统一入口
# ────────────────────────────────────────────────────────────
# build() (startup 一次):装配 base + MEMORY.md + sandbox section + TRUSTING。
#   由 ReactAgent.__init__ 调一次,产物缓存到 self._cached,赋给 agent.system_prompt。
#   必须等 permission_engine + memory_index 都 ready 后调(__init__ 末尾位置)。


class SystemPromptAssembler:
    """system_prompt 装配的统一入口。

    Usage(ReactAgent.__init__):
        self._assembler = SystemPromptAssembler(self)
        self.system_prompt = self._assembler.build()
    """
    name = "system_prompt_assembler"

    def __init__(self, agent):
        self._agent = agent
        self._cached: Optional[str] = None  # build() 产物

    def build(self) -> str:
        """L1 startup:base + sandbox section + MEMORY.md + TRUSTING_RECALL_SECTION。

        Returns:装配好的 system_prompt 字符串(同时缓存到 self._cached)。
        必须 agent.permission_engine + agent.memory_index 都 ready 后调。
        """
        agent = self._agent
        if agent is None:
            return ""
        base = (getattr(agent, "system_prompt", "") or "")
        sandbox_section = self._get_sandbox_section()
        if sandbox_section:
            base = base + "\n\n" + sandbox_section
        if getattr(agent, "memory_index", None) is None:
            result = base + "\n" + TRUSTING_RECALL_SECTION
        else:
            try:
                index_content = agent.memory_index.load_index()
            except Exception as e:
                _logger.warning(f"MEMORY.md 加载失败,跳过: {e}")
                result = base + "\n" + TRUSTING_RECALL_SECTION
            else:
                if not index_content:
                    result = base + "\n" + TRUSTING_RECALL_SECTION
                else:
                    result = f"{base}\n\n{index_content}\n\n{TRUSTING_RECALL_SECTION}"
        self._cached = result
        return result

    def _get_sandbox_section(self) -> str:
        """获取 sandbox 规则 prompt 段(对齐 doc §5.4)。

        - permission_engine 未注入 → ""(向后兼容)
        - sandbox 未启用 / loader 抛错 → ""
        """
        agent = self._agent
        if agent is None or getattr(agent, "permission_engine", None) is None:
            return ""
        try:
            from .tools.sandbox_prompt import get_sandbox_prompt_section
            return get_sandbox_prompt_section()
        except Exception as e:
            _logger.debug("sandbox prompt 注入失败,跳过: %s", e)
            return ""


# ────────────────────────────────────────────────────────────
# TurnChain — 执行器
# ────────────────────────────────────────────────────────────

class TurnChain:
    """职责链执行器:按顺序跑 handlers,遇 stop_chain 提前终止。"""

    def __init__(self, handlers: list[Handler]):
        self._handlers = list(handlers)
        self._name_to_idx = {h.name: i for i, h in enumerate(self._handlers)}

    def run(self, ctx: TurnContext) -> Iterator[Event]:
        """执行链:每个 handler 调一次,遇 stop_chain 停。

        注意:这是 generator,逐个 yield events(供 phase.enter 流式调用)。

        设计:handler 通过 ctx.emit() 累计事件,TurnChain.run() 每次 handler
        调用后清空 ctx.events 再 yield 出去。这样 handler 调多少次,事件都按
        handler 调用顺序流出。
        """
        for h in self._handlers:
            if ctx.is_stopped:
                break
            result = h.handle(ctx)
            for ev in ctx.events:
                yield ev
            ctx.events.clear()
            if result.stop_chain:
                break

    def add(
        self,
        handler: Handler,
        *,
        after: Optional[str] = None,
        before: Optional[str] = None,
        at: Optional[int] = None,
    ) -> None:
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

    def __len__(self) -> int:
        return len(self._handlers)

    def __repr__(self) -> str:
        names = [h.name for h in self._handlers]
        return f"<TurnChain {' → '.join(names)}>"


# ════════════════════════════════════════════════════════════
# 内置 Handler(11 个,4 个 chain 分类)
# ════════════════════════════════════════════════════════════
# Plan A: 每个 handler 接 agent 引用,handle() 调 agent._iter_phase_xxx() 真业务。
# Stop-chain 用法:每个 chain 的「主 handler」处理完后 stop_chain=True,后续 handler 跳过。
# 这样 chain 中多个 handler 都能保留作为扩展点,但默认只有一个干活。

_StopChain = HandlerResult(stop_chain=True)


def _usage_asdict(usage):
    """UsageStats dataclass → dict,Stage A/C 落盘前必调。

    session.storage.flush 不支持 dataclass 序列化,plan A L2263 / L1039 等都用 asdict。
    不转 → 报 "Object of type UsageStats is not JSON serializable"。
    """
    if usage is None:
        return None
    if hasattr(usage, "__dataclass_fields__"):
        from dataclasses import asdict
        return asdict(usage)
    return usage


# ── 0. ContextCompactionHandler(inputs_chain 首位) ─────────
class ContextCompactionHandler:
    """inputs_chain 首位:SETUP phase 入口做 token 预算压缩 + L3 SM 快路径。

    Plan B R4 修复 (2026-07-01):接管原 agent.run() body(L1862-L2534,已被 Step 7 删)
    内的 L3 SM fast path + ContextManager.check_and_compact 逻辑。

    决策路径(对应原 v1 run() L1879-1908):
    1. agent.session_memory 非空 → 调 should_trigger_compact:
       - strategy="sm_compact" + compact() 成功 → 替换 agent.messages + emit
       - strategy="wait"/"traditional"/compact 失败 → fallback ContextManager
    2. agent.session_memory 异常 / 决策失败 → fallback ContextManager
    3. agent.session_memory 是 None → 直接走 ContextManager.check_and_compact

    ContextManager fallback(对应原 v1 L1909-1930):
    - check_and_compact 成功 → 替换 agent.messages + _persist_compacted_messages
      持久化 + emit
    - 失败 → _trim_messages() 兜底(旧 v1 策略)

    装配位置:inputs_chain 首位(MemoryRetrieval 之前)。
    每个 turn 开始时先压缩,MemoryRetrieval 检索压缩后的 messages,
    SystemPrompt / ToolsSchemaPrepare 兜底用压缩后的 messages(若未 short-circuit)。

    失败安全:任何异常 → 静默 fallback,不让压缩 bug 阻断整个 run。
    """
    name = "context_compaction"

    def __init__(self, agent):
        self._agent = agent

    def handle(self, ctx: TurnContext) -> HandlerResult:
        agent = self._agent
        if agent is None:
            return HandlerResult()

        # 1. L3 SM fast path(如果 session_memory 启用)
        if self._try_sm_compact(agent, ctx):
            return HandlerResult()

        # 2. fallback ContextManager
        self._try_context_manager(agent, ctx)
        return HandlerResult()

    def _try_sm_compact(self, agent, ctx) -> bool:
        """尝试 L3 SM 快路径。成功返 True,失败/不适用返 False。"""
        sm = getattr(agent, "session_memory", None)
        if sm is None:
            return False
        try:
            from agent_core.memory.sm_layer import TurnContext as _SMTurnContext
            from agent_core.session_counter import COMPACTION

            # M11 (2026-06-26):注入 stable id 让 SM _slice_kept_messages 能定位 last_id
            msgs_with_id = messages_with_ids(agent.messages)
            total_tokens = sum(
                estimate_message_tokens(m) for m in agent.messages
            )
            tool_count = agent.session_counter.since_tool(COMPACTION)
            sm_ctx = _SMTurnContext(
                messages=msgs_with_id,
                total_tokens=total_tokens,
                tool_count=tool_count,
            )
            _logger.debug(
                f"[L3 SM] 决策入口: msgs={len(msgs_with_id)} "
                f"total_tokens={total_tokens} tool_count={tool_count}"
            )
            decision = sm.should_trigger_compact(sm_ctx)
            _logger.debug(
                f"[L3 SM] 决策结果: strategy={decision.strategy} "
                f"reason={decision.reason!r}"
            )

            if decision.strategy != "sm_compact":
                return False

            sm_result = sm.compact(
                msgs_with_id,
                context_window=getattr(agent, "max_context_tokens", 100_000),
            )
            if sm_result is None:
                _logger.debug("[L3 SM] compact() 返 None,fallback ContextManager")
                return False

            # SM 压缩成功:剥离注入的 id(避免污染持久化逻辑)
            kept = [
                {k: v for k, v in m.items() if k != "id"}
                for m in sm_result.kept_messages
            ]
            agent.messages = [sm_result.summary_message] + kept
            # tool 计数:推进 compaction 水位(2026-07-03)。compact 成功后,
            # 下次 since_tool(COMPACTION) 从此点重新计。
            agent.session_counter.mark_tool(COMPACTION)
            ctx.emit((
                "system",
                f"📦 [L3 fast path] 上下文已压缩(SM 文件): "
                f"~{sm_result.used_tokens_estimate} tokens",
            ))
            return True
        except Exception as e:
            _logger.warning(f"SM fast path 异常,fallback ContextManager: {e}")
            return False

    def _try_context_manager(self, agent, ctx) -> None:
        """ContextManager fallback check_and_compact + 持久化 + emit。"""
        cm = getattr(agent, "context_manager", None)
        if cm is None:
            return
        try:
            tool_schemas = None
            if getattr(agent, "tools", None) is not None:
                tool_schemas = agent.tools.list_schemas(
                    provider=detect_provider(agent.llm)
                )

            compacted, compact_result = cm.check_and_compact(
                agent.messages,
                parent_system=getattr(agent, "system_prompt", None),
                parent_tools=tool_schemas or None,
                parent_messages=agent.messages,
            )
            if compact_result is None:
                return  # 不需要压缩

            if compact_result.success:
                agent.messages = compacted
                _logger.info(f"Context compacted: {compact_result.summary_str()}")
                if getattr(agent, "_session_manager", None):
                    try:
                        self._persist_compacted(agent, agent.messages, compact_result)
                    except Exception as e:
                        _logger.warning(f"Failed to persist compaction to session: {e}")
                ctx.emit((
                    "system",
                    f"📦 上下文已压缩: {compact_result.tokens_freed:,} tokens 释放",
                ))
            else:
                _logger.warning(f"Context compact failed: {compact_result.error}")
                # Plan C (2026-07-02):_trim_messages 死代码已删(ContextManager 已全面
                # 替代 Day3 旧 token 截断策略)。压缩失败时只 log,不再 fallback 截断。
        except Exception as e:
            _logger.warning(f"ContextManager fallback 异常: {e}")

    def _persist_compacted(self, agent, compacted: list[dict], compact_result):
        """压缩后消息持久化到 JSONL。

        2026-07-02 Plan C:从 ReactAgent._persist_compacted_messages 迁入(原 agent_core.py:1111)。

        对齐 Claude Code buildPostCompactMessages (src/services/compact/compact.ts:325-338)
        顺序: boundary → summary → preserved head (preserved head = compacted 跳过 system 和 summary)

        agent-dev 之前 P0 bug: 只调 add_compact_boundary + add_summary 两方法，
        preserved head 6 条消息永久不写盘，重启后上下文残缺。
        现场: data/sessions/7f071c62.jsonl

        Args:
            agent: ReactAgent 实例(读 agent._session_manager)
            compacted: CompactOrchestrator._build_compacted_messages 输出
                - [0] system: 动态注入不持久化
                - [1] summary: user role + "[Previous conversation summarized]" 开头
                - [2..] preserved: 最近 N 条原始 user/assistant 消息
            compact_result: CompactionResult 实例
        """
        if not agent._session_manager:
            return

        # 直接调 storage 层（不走 manager.add_user_message 等高阶方法）：
        # 1) manager.add_user_message 会触发 _on_user_message 标题生成（不必要的 LLM 调用）
        # 2) preserved head 是历史数据，不需要标题重新生成
        storage = agent._session_manager.storage

        # 1. boundary（parent 链到最后一条旧消息，由 add_compact_boundary 内部 _get_last_uuid 决定）
        boundary_uuid = storage.add_compact_boundary(
            trigger="auto",
            pre_tokens=compact_result.tokens_before,
            messages_summarized=len(compacted) - 1,  # 含 summary 的 compacted 长度减 1
        )
        _logger.debug(f"💾 [Persist] boundary written: uuid={boundary_uuid}, parent→旧链末尾")

        # 2. summary（parent 链到 boundary）
        summary_uuid = storage.add_summary(
            summary=compact_result.summary,
            tokens_saved=compact_result.tokens_freed,
        )
        _logger.debug(f"💾 [Persist] summary written: uuid={summary_uuid}, len={len(compact_result.summary)} chars")

        # 3. preserved head（跳过 system[0] 和 summary）
        preserved_count = 0
        # compacted 结构: [system, summary, ...preserved]
        # - 跳过 system（[0]，动态注入不持久化）
        # - 跳过 summary（已由 add_summary 写）
        # - tool_use/tool_result 也不持久化（与未压缩前 add_user_message 行为一致）
        for msg in compacted[1:]:
            role = msg.get("role")
            if role not in ("user", "assistant"):
                continue

            # 跳过已写过的 summary（用内容前缀识别，与 compact.py 输出对齐）
            content = msg.get("content", "")
            if role == "user" and isinstance(content, str) \
               and content.startswith("[Previous conversation summarized]"):
                continue

            # 调底层 storage.append_entry，parent 链到上一条写入（用 _get_last_uuid 自动算，
            # 该方法已修复跳过元数据 entry 88c28c5）
            storage.append_entry(
                entry_type=role,
                message=msg,  # 原样存整个 message dict
            )
            preserved_count += 1

        _logger.debug(f"💾 [Persist] preserved head: {preserved_count} messages")

        # 4. flush 确保落盘
        storage.flush()
        _logger.debug(f"💾 [Persist] flush done, storage.last_uuid={storage.last_uuid}")

        _logger.debug(f"💾 [Sync] manager._last_uuid: {agent._session_manager._last_uuid} → {storage.last_uuid}")

        # 5. P1 修复：同步 manager 的 _last_uuid 到 preserved head 最后一条
        # 为什么需要：storage.add_* 只更新 storage 内部状态，不调 manager.add_user_message 等
        # 高阶方法。manager._last_uuid 仍是压缩前的最后一条（如 838f3b94）。
        # 下次 manager.add_assistant_message/add_user_message 写后续对话时，
        # parent = self._last_uuid 会链到旧链（错位）。
        # 现场: 7f071c62.jsonl 后续 assistant parent 指向 boundary 之前的 user message。
        # 修复后: manager._last_uuid 同步到 storage.last_uuid（preserved head 最后一条）。
        agent._session_manager._last_uuid = storage.last_uuid


# ── 1. MemoryRetrievalHandler(inputs_chain) ───────────────
class MemoryRetrievalHandler:
    """inputs_chain:检索相关记忆 → append 到 ctx.system_prompt + emit memory_status。

    选项 A 重构 (2026-07-06):
    - 不再读写 stage_inputs;mem_block 通过 ctx.append_system 累加进 ctx.system_prompt
    - 装配逻辑内聚为类内私有方法(_find_last_user_query / _retrieve_with_config /
      _format_block / _count_stored / _emit_memory_status),不调模块级 helper
    - ★ 修复 side_query bug:_retrieve_with_config 读 agent.memory_config.retrieval.mode/top_k
      (方案 D 的 LlmInputAssembler 漏传 mode/top_k,导致 .env SIDE_QUERY 永不生效)
    - _surfaced 跨轮去重封装在 retriever 内部(search 时自动过滤+更新),handler 不维护

    谁检索谁 emit:memory_status 事件由本 handler emit(格式不变,web/app.py 零改动)。
    """
    name = "memory_retrieval"

    def __init__(self, agent):
        self._agent = agent

    def handle(self, ctx: TurnContext) -> HandlerResult:
        agent = self._agent
        if agent is None or getattr(agent, "memory_retriever", None) is None:
            return HandlerResult()

        query = self._find_last_user_query()
        if not query:
            return HandlerResult()

        try:
            hits, stored_total, injected = self._retrieve_and_format(query)
            if hits:
                ctx.append_system(self._format_block(hits))
            self._emit_memory_status(ctx, hits, stored_total, injected)
        except Exception as e:
            _logger.warning(f"🧩 MemoryRetrievalHandler: retrieve failed: {e}")
        return HandlerResult()

    # ── 类内私有方法(内聚,不调外部 helper)──────────────────────

    def _find_last_user_query(self) -> Optional[str]:
        """从 agent.messages 反查最近一条 user 消息文本(memory 检索 query)。"""
        agent = self._agent
        for m in reversed(agent.messages):
            if m.get("role") == "user" and isinstance(m.get("content"), str):
                return m["content"]
        return None

    def _retrieve_with_config(self, query: str):
        """调 retriever.search,mode/top_k 从 agent.memory_config 读(★ side_query bug 修复)。

        2026-07-06:方案 D 的 LlmInputAssembler._build_memory_part 调 retriever.search(query)
        没传 mode/top_k → 永远走默认 semantic。本方法恢复从 config 读取,与 .env
        MEMORY_RETRIEVAL__MODE / __TOP_K 对齐(原 _call_memory_retriever 的正确逻辑)。
        """
        agent = self._agent
        cfg = getattr(agent, "memory_config", None)
        if cfg is not None:
            mode = cfg.retrieval.mode
            top_k = cfg.retrieval.top_k
        else:
            # 向后兼容:老 caller 不传 memory_config
            mode = "semantic"
            top_k = 5
        _logger.debug(
            f"[_retrieve_with_config] query={query!r} (len={len(query)}) | "
            f"resolved mode={mode!r} top_k={top_k}"
        )
        return agent.memory_retriever.search(query, top_k=top_k, mode=mode)

    def _retrieve_and_format(self, query: str):
        """retrieve → 返回 (hits, stored_total, injected_tokens)。search 失败降级 hits=[]。"""
        agent = self._agent
        try:
            report = self._retrieve_with_config(query)
        except Exception as e:
            _logger.warning(f"🧩 MemoryRetrievalHandler: search failed: {e}")
            return ([], self._count_stored(agent), 0)
        hits = getattr(report, "hits", None) or []
        stored_total = self._count_stored(agent)
        injected = sum(len((getattr(h, "body", "") or "")) // 4 for h in hits)
        return (hits, stored_total, injected)

    def _format_block(self, hits) -> str:
        """拼 [记忆库 / N hits] 文本块。"""
        mem_block = "\n\n[记忆库 / {} hits]\n".format(len(hits))
        for h in hits:
            title = getattr(h, "title", "") or ""
            body = (getattr(h, "body", "") or "")[:200]
            mem_block += f"- [{getattr(h, 'type', '?')}] {title}: {body}\n"
        return mem_block

    def _count_stored(self, agent) -> int:
        """库内 memory 总数(cumulative 计数器)。失败返 0。"""
        store = getattr(agent, "memory_store", None)
        if store is None:
            return 0
        try:
            counts = store.count_by_type()
            return sum(counts.values()) if isinstance(counts, dict) else 0
        except Exception:
            return 0

    def _emit_memory_status(self, ctx: TurnContext, hits, stored_total: int, injected: int) -> None:
        ctx.emit(("memory_status", {
            "hits": len(hits),
            "stored_total": stored_total,
            "injected_tokens": injected,
            "zero_hit": len(hits) == 0,
        }))


# ── 2. SystemPromptHandler(inputs_chain) ──────────────────
class SystemPromptHandler:
    """inputs_chain:把 base system_prompt append 到 ctx.system_prompt。

    agent.system_prompt 由 SystemPromptAssembler.build() 在 ReactAgent.__init__
    构造(base + sandbox + MEMORY.md + TRUSTING_RECALL),本 handler 只负责把它
    累加进 ctx.system_prompt,供 llm_chain 的 LLMCallHandler 读取。

    选项 A 重构 (2026-07-06):不再调 SystemPromptAssembler.place()(那是把 system
    放进 messages 头部,依赖已删除的 stage_inputs);改为 ctx.append_system 累加。
    SystemPromptAssembler.build() 仍被 __init__ 用 → 类保留。

    顺序:inputs_chain 中 ContextCompaction 之后、MemoryRetrievalHandler 之前。
    """
    name = "system_prompt"

    def __init__(self, agent):
        self._agent = agent

    def handle(self, ctx: TurnContext) -> HandlerResult:
        agent = self._agent
        if agent is None or not getattr(agent, "system_prompt", None):
            return HandlerResult()
        ctx.append_system(agent.system_prompt)
        return HandlerResult()


# ── 4. SkillsPromptHandler(inputs_chain, 在 MemoryRetrieval 之后) ──
class SkillsPromptHandler:
    """inputs_chain:把 ## Skills 段 append 到 ctx.system_prompt(FR-007)。

    选项 A 重构 (2026-07-06):
    - 不再读写 stage_inputs / 调 _merge_skills_into_system
    - 用 ctx.append_system 累加 skills 段;幂等性自检(section 已在 ctx.system_prompt 则跳过)

    002-skill-secret-injection (T009):
    - 在 section 渲染前, 先调 apply_skill_env_overrides 注入 secret 到 os.environ
    - 返 reverter 注册到 agent._pending_env_reverter(覆盖式)
    - **不在 finally 调 reverter** —— 由 EnvCleanupHandler (T034) 在 outputs_chain 末尾兜底调用
      (解决 run-end 泄漏 C2;同 run 内多次 turn 不重复 snapshot)
    - guard: agent.skills_config is None or entries is None → 跳过 env 注入(legacy 模式)

    guard(C2):Read 工具不在 toolset → 跳过(无 Read 则模型读不到 SKILL.md,注入目录只会误导)。
    顺序:MemoryRetrievalHandler 之后(后者 append mem_block,本 handler append skills 段)。
    """
    name = "skills_prompt"

    def __init__(self, agent):
        self._agent = agent

    def handle(self, ctx: TurnContext) -> HandlerResult:
        agent = self._agent
        if agent is None:
            return HandlerResult()
        registry = getattr(agent, "skills_registry", None)
        if registry is None:
            return HandlerResult()
        # C2 guard:Read 工具必须可用(否则模型无法读 SKILL.md)
        tools = getattr(agent, "tools", None)
        if tools is None or "Read" not in tools.list_names():
            _logger.debug("🧩 SkillsPrompt skip: Read tool not in toolset (C2 guard)")
            return HandlerResult()

        # T009: 注入 secret 到 os.environ(在 snapshot 渲染前, 这样 Bash tool 跑时能看到)
        # guard: skills_config 缺失或 entries 为空 → 跳过 env 注入(legacy 模式)
        skills_config = getattr(agent, "skills_config", None)
        if skills_config is not None and getattr(skills_config, "entries", None):
            try:
                # 用 registry.entries(list[SkillEntry])喂给 apply_skill_env_overrides
                # — 后者需要每个 entry 的 metadata.requires.env 来过滤 key
                # (SkillSnapshot 没有 entries 字段, 它只含 prompt + summary)
                skill_entries = registry.entries
                reverter = apply_skill_env_overrides(skill_entries, skills_config)
                # 注册 reverter 给 EnvCleanupHandler(T034)在 outputs_chain 末位兜底
                # 覆盖式:同 run 多次 turn 时,新 reverter 替换旧的(EnvCleanupHandler
                # 会在每个 turn 末位调一次,不会跨 turn 重复还原)
                agent._pending_env_reverter = reverter
            except Exception as e:
                _logger.warning(f"🧩 SkillsPrompt env inject failed: {e}")
                # 不阻断 — 后续 snapshot 仍跑,只是 secret 没注入

        # 取 skills 段(类内私有,不调外部 helper)
        try:
            section = self._build_section(registry)
        except Exception as e:
            _logger.warning(f"🧩 SkillsPrompt snapshot failed: {e}")
            return HandlerResult()
        if not section:
            return HandlerResult()
        # 幂等:已在 ctx.system_prompt 则跳过(防 chain 重跑翻倍)
        if section in ctx.system_prompt:
            return HandlerResult()
        ctx.append_system(section)
        _logger.debug(f"🧩 SkillsPrompt injected: section_len={len(section)}")
        return HandlerResult()

    def _build_section(self, registry) -> str:
        """调 registry.snapshot() 取已渲染的 skills prompt 文本。空则返 ''。"""
        snap = registry.snapshot()
        return snap.prompt or ""


# ── 0. TurnIndicatorHandler(inputs_chain 首位) ────────────
class TurnIndicatorHandler:
    """inputs_chain 首位:emit `("system", "🔄 Turn N/M")` event。

    Plan B (2026-07-02 SRP 重构):从 _iter_phase_setup 拆出。
    Turn indicator 是独立职责(纯 emit,不动 messages),不混入 memory retrieval
    或 system prompt 注入 — 拆出后 _iter_phase_setup 可被删除。
    """
    name = "turn_indicator"

    def __init__(self, agent):
        self._agent = agent

    def handle(self, ctx: TurnContext) -> HandlerResult:
        agent = self._agent
        if agent is None:
            return HandlerResult()
        ctx.emit(("system", f"🔄 Turn {ctx.turn_number}/{agent.max_turns}"))
        return HandlerResult()


# ── 3. ToolsSchemaPrepareHandler(inputs_chain) ────────────
class ToolsSchemaPrepareHandler:
    """inputs_chain 末位:准备 tool schemas(按 provider 格式)。

    Plan A: 与 MemoryRetrievalHandler 协同 — 默认被 stop_chain 短路。
    本 handler 仅作扩展点,可被独立激活(用户传 disable_memory=True 时)。
    """
    name = "tools_schema_prepare"

    def __init__(self, agent):
        self._agent = agent

    def handle(self, ctx: TurnContext) -> HandlerResult:
        agent = self._agent
        if agent is None:
            return HandlerResult()
        # 把 tool_schemas 缓存到 turn_ctx(LLMCallHandler 复用)
        try:
            ctx.tool_schemas = agent.tools.list_schemas(provider=detect_provider(agent.llm))
        except Exception as e:
            _logger.warning(f"ToolsSchemaPrepare failed: {e}")
        return HandlerResult()


# ── 4. LLMCallHandler(llm_chain) ──────────────────────────
class LLMCallHandler:
    """llm_chain 首位:发起 LLM chat 调用,获得 chunk stream。

    选项 A 重构 (2026-07-06):只保留 LLM 请求发起 + LLM 错误兜底 2 件事(SRP):
    - 从 ctx 读 system_prompt(inputs_chain 累加)+ tool_schemas(ToolsSchemaPrepare)
      + agent.messages(live)→ 拼 messages_for_llm
    - 调 self.llm.chat(...) 拿 stream iterator
    - 成功:把 stream 存到 ctx._llm_stream,让 ChunkParseHandler 消费
    - 失败(LLM API 抛):emit 错误 events + 写 stage_outputs(stop_reason="llm_error")
      + 追加 fallback assistant message,ChunkParseHandler 见 stage_outputs 已设就跳过

    不再做(已归位):装配 system/memory/skills(→ inputs_chain 的 3 个 handler)、
    emit memory_status(→ MemoryRetrievalHandler)、取 tool_schemas(→ ToolsSchemaPrepareHandler)。

    ChunkParseHandler 接管剩余职责:stream consumption + cancel_event check +
    4 流(text/thinking/tool_call/usage)聚合 + interrupt 兜底 + stream 异常兜底 +
    写 stage_outputs(完整) + 更新 _run_state(cost tracking)。

    顺序:LLMCall → ChunkParse → LLMCallPersist(Stage A),不 stop_chain。
    """
    name = "llm_call"

    def __init__(self, agent):
        self._agent = agent

    def handle(self, ctx: TurnContext) -> HandlerResult:
        agent = self._agent
        if agent is None:
            return HandlerResult()

        # 1. 拼 messages_for_llm:ctx.system_prompt(inputs_chain 累加)+ agent.messages(live)
        #    选项 A 重构 (2026-07-06):不再调 LlmInputAssembler;system_prompt 由
        #    inputs_chain 的 SystemPrompt/MemoryRetrieval/SkillsPrompt handler append。
        system_prompt = ctx.system_prompt
        if system_prompt:
            messages_for_llm = [{"role": "system", "content": system_prompt}] + list(agent.messages)
        else:
            messages_for_llm = list(agent.messages)

        # 诊断(2026-07-03):打印 messages role 序列 + tool_use/tool_result id,
        # 确认 _is_resume 后的 LLM 调用 messages 是否含上一轮 tool_result(allow-loop 排查)。
        _roles = []
        for _m in messages_for_llm:
            _r = _m.get("role", "?") if isinstance(_m, dict) else "?"
            _c = _m.get("content") if isinstance(_m, dict) else None
            if isinstance(_c, list):
                for _b in _c:
                    if isinstance(_b, dict):
                        if _b.get("type") == "tool_use":
                            _r += f"/tu:{(_b.get('id') or '')[:10]}"
                        elif _b.get("type") == "tool_result":
                            _r += f"/tr:{(_b.get('tool_use_id') or '')[:10]}"
            _roles.append(_r)
        _logger.warning("▶️ [LLMCall entry] messages roles=%s", _roles)
        tool_schemas = ctx.tool_schemas
        cache_namespace = (
            f"react:{agent._session_manager.session_id if agent._session_manager else 'default'}"
        )

        # 2. 调 LLM(stream 还没消费,只是发起请求)
        try:
            stream = agent.llm.chat(
                messages=messages_for_llm,
                tools=tool_schemas or None,
                cache_namespace=cache_namespace,
            )
        except Exception as e:
            # 3. LLM 错误兜底:写 stage_outputs(stop_reason="llm_error")让 ChunkParseHandler 跳过
            error_msg = f"LLM 调用失败: {type(e).__name__}: {e}"
            _logger.error(error_msg)
            ctx.emit(("system", f"❌ {error_msg}"))
            ctx.emit(("text", f"抱歉,遇到了技术问题无法回答:{error_msg}"))
            ctx.emit(("system", "✅ 回答完成"))
            agent.messages.append({
                "role": "assistant",
                "content": f"抱歉,遇到了技术问题:{e}",
            })
            ctx.stage_outputs = _LLMResult(
                tool_calls=[], full_text=f"抱歉,遇到了技术问题:{e}",
                thinking_text="", stop_reason="llm_error",
                usage=getattr(agent, "_last_turn_usage", None),
            )
            return HandlerResult()

        # 4. 成功:把 stream 留给 ChunkParseHandler
        ctx._llm_stream = stream
        return HandlerResult()


# ── 5. ChunkParseHandler(llm_chain) ───────────────────────
class ChunkParseHandler:
    """llm_chain 中段:消费 LLM stream,emit events + 写 stage_outputs。

    2026-07-02 SRP 重构:从 agent._iter_phase_llm() 拆出,接管 chunk-level 工作:
    - iter stream,逐 chunk 解析(text_delta / thinking_delta / tool_call / usage / stop_reason)
    - cancel_event 检查:用户中断时立刻 break,partial 落盘(stop_reason="interrupted")
    - stream 异常兜底:chunk 迭代抛错时,partial text + stop_reason="interrupted"
    - 4 流聚合:full_text / thinking_text / tool_calls / usage
    - 写 stage_outputs = _LLMResult(...) 给 tool_chain + output_chain 消费
    - 更新 _run_state:last_input_tokens / last_output_tokens / last_tool_calls
      (给 cost tracking + ToolExecuteHandler 读)

    skip 条件:LLMCallHandler 已写 stage_outputs(LLM 错误路径)→ 本 handler no-op。
    """
    name = "chunk_parse"

    def __init__(self, agent):
        self._agent = agent

    def handle(self, ctx: TurnContext) -> HandlerResult:
        agent = self._agent
        if agent is None:
            return HandlerResult()
        # LLMCallHandler 错误路径已写 stage_outputs → 本 handler 跳过
        if ctx.stage_outputs is not None:
            # 诊断(2026-07-03):stage_outputs 残留(_is_resume 跨 phase 设的)会让
            # ChunkParse 误跳过 LLM stream 消费 → stage_outputs 不更新 → allow-loop。
            _tc = ctx.stage_outputs.tool_calls or []
            _logger.warning(
                "⚠️ [ChunkParse SKIP] stage_outputs 已设(tool_calls=%d ids=%s)→ "
                "跳过 stream 消费(若非 LLM 错误路径,即为 stage_outputs 残留 bug)",
                len(_tc),
                [getattr(t, "tool_use_id", "?")[:12] for t in _tc][:3],
            )
            return HandlerResult()
        stream = getattr(ctx, "_llm_stream", None)
        if stream is None:
            return HandlerResult()

        tool_calls: list = []
        full_text = ""
        thinking_text = ""
        stop_reason_this_turn: Optional[str] = None

        # chunk 迭代 + 4 流聚合 + cancel check
        try:
            _stream_iter = iter(stream)
            _exhausted = False
            while not _exhausted:
                try:
                    chunk = next(_stream_iter)
                except StopIteration:
                    _exhausted = True
                    break

                # cancel check: 用户按 Stop 立刻 break
                if (agent._run_state is not None
                        and agent._run_state.cancel_event.is_set()):
                    stop_reason_this_turn = stop_reason_this_turn or "interrupted"
                    break

                if chunk.text_delta:
                    full_text += chunk.text_delta.text
                    ctx.emit(("text", chunk.text_delta.text))
                if chunk.thinking_delta:
                    thinking_text += chunk.thinking_delta.thinking
                    if agent._run_state is not None:
                        agent._run_state.pending_thinking += chunk.thinking_delta.thinking
                    ctx.emit(("thinking", chunk.thinking_delta.thinking))
                if chunk.tool_call:
                    tool_calls.append(chunk.tool_call)
                if getattr(chunk, "stop_reason", None):
                    stop_reason_this_turn = chunk.stop_reason
                if chunk.usage:
                    agent._last_turn_usage = chunk.usage
                    ctx.emit(("usage", chunk.usage))
                    if agent.context_manager:
                        agent.context_manager.set_baseline(
                            chunk.usage.input_tokens,
                            len(agent.messages),
                        )
        except Exception as e:
            # stream 异常兜底:partial text + stop_reason="interrupted"
            error_msg = f"LLM 流式响应中断: {type(e).__name__}: {e}"
            _logger.error(error_msg)
            ctx.emit(("system", f"❌ {error_msg}"))
            partial = full_text or f"[响应中断:{e}]"
            ctx.emit(("text", f"抱歉,响应被中断:{error_msg}"))
            ctx.emit(("system", "✅ 回答完成"))
            agent.messages.append({"role": "assistant", "content": partial})
            ctx.stage_outputs = _LLMResult(
                tool_calls=[], full_text=partial,
                thinking_text=thinking_text, stop_reason="interrupted",
                usage=getattr(agent, "_last_turn_usage", None),
            )
            return HandlerResult()
        finally:
            try:
                stream.close()
            except Exception:
                pass

        # 正常完成:写完整 stage_outputs + 更新 _run_state(cost tracking)
        ctx.stage_outputs = _LLMResult(
            tool_calls=tool_calls,
            full_text=full_text,
            thinking_text=thinking_text,
            stop_reason=stop_reason_this_turn,
            usage=getattr(agent, "_last_turn_usage", None),
        )
        if (agent._last_turn_usage is not None
                and agent._run_state is not None):
            agent._run_state.last_input_tokens = agent._last_turn_usage.input_tokens
            agent._run_state.last_output_tokens = agent._last_turn_usage.output_tokens
            # token 计数累加(2026-07-03):turn 正常完成、usage 确定后记账。
            # 放 turn 完成点(非 chunk.usage 解析点 L1085)避免多 chunk usage 重复加;
            # 异常/中断分支不进此块 → 自动不记(不完整 turn 的 token 不计)。
            agent.session_counter.add_token(
                agent._last_turn_usage.input_tokens + agent._last_turn_usage.output_tokens
            )
        if agent._run_state is not None:
            agent._run_state.last_tool_calls = list(tool_calls)
        return HandlerResult()


# ── 6. PermissionCheckHandler(tool_chain,首位) ───────────
class PermissionCheckHandler:
    """tool_chain 首位:permission 决策。

    Plan A: 默认被 ToolExecuteHandler 的 stop_chain 短路。
    仅作扩展点 — 若用户想在 tool 执行前单独跑 permission 检查,
    可禁用 ToolExecuteHandler 的 stop_chain,让本 handler 先跑。

    2026-07-02 完整实现(Plan B 拆分):
    handle() 把每个 tool_call 分类到三态(allow / ask / deny),
    产出 TcPermissionDecision 列表作为 ctx.permission_decisions。
    若任一 ASK → 写 RunState.awaiting_permission_batch + 强制 SM AWAITING
    + emit("awaiting_permission", batch[0]) + return _StopChain。
    """
    name = "permission_check"

    def __init__(self, agent):
        self._agent = agent

    def _check_tool_permission(
        self,
        tool_name: str,
        tool_input: dict,
    ) -> tuple[bool, Optional[str], dict]:
        """同步检查单工具权限(对齐 doc §6.3)。

        2026-07-02 Plan C:从 ReactAgent._check_tool_permission 迁入(原 agent_core.py:439)。
        调 self._agent._ask_user_permission_v2(ASK 公共入口留 agent ——
        被 resume_after_permission / tests 直接调,是公共契约)。

        Returns:
            (allowed, error_message, effective_input)
            - allowed=True: 允许执行,error_message=None
            - allowed=False: 拒绝,error_message 含拒绝原因(仍要让 LLM 看到)
            - effective_input: hook 可能改写后的 input
        """
        agent = self._agent
        if agent.permission_engine is None:
            # 未注入 permission_engine → 向后兼容,允许
            return True, None, tool_input

        # 取出 tool_def(duck-typed:只要有 name 即可)
        tool_def = agent.tools.get(tool_name)
        if tool_def is None:
            # 工具不存在 — 让 execute() 自己返 error,这里放过
            return True, None, tool_input

        permission_logger.debug(
            "🛡️ [check_tool_permission_entry] tool=%s input=%s",
            tool_name, tool_input,
        )

        # 调 permission_engine 决策
        # 注:audit log 由 PermissionEngine._log_and_return 内部统一写(唯一审计点,
        # 对齐 doc §4.8),不重复写,避免 double-logging。
        decision = agent.permission_engine.check_permissions(
            tool_def, tool_input, list(agent.messages),
        )

        from agent_core.tools.permission_types import PermissionBehavior

        permission_logger.info(
            "🛡️ [check_tool_permission_decision] tool=%s behavior=%s "
            "has_updated_input=%s",
            tool_name, decision.behavior, decision.updated_input is not None,
        )

        if decision.behavior == PermissionBehavior.ALLOW.value:
            return True, None, decision.updated_input or tool_input
        if decision.behavior == PermissionBehavior.DENY.value:
            reason = decision.decision_reason
            reason_str = ""
            if reason is not None and hasattr(reason, 'reason'):
                reason_str = reason.reason
            elif decision.message:
                reason_str = decision.message
            err = f"Permission denied: {reason_str or 'no reason'}"
            # M3 Task 3: 跑 PermissionDenied hook,追加 retry_prompt 到 err
            permission_logger.debug("🛡️ [permission_denied_hook_fire] tool=%s", tool_name)
            retry_hint = self._run_permission_denied_hook(tool_name, tool_input, decision)
            if retry_hint:
                err = f"{err}\n💡 Retry hint: {retry_hint}"
            return False, err, tool_input
        if decision.behavior == PermissionBehavior.ASK.value:
            # auto_allow_ask=True → 自动 ALLOW(测试用)
            if agent.auto_allow_ask:
                permission_logger.info(
                    "🛡️ [auto_allow_ask_skip_ui] tool=%s auto_allow_ask=True",
                    tool_name,
                )
                return True, None, decision.updated_input or tool_input
            # auto_allow_ask=False → 走 UI 路径
            permission_logger.info(
                "🛡️ [decision_ask_to_ui] tool=%s → _ask_user_permission_v2",
                tool_name,
            )
            # _ask_user_permission_v2 留 agent(ASK 公共入口)。调它只为副作用
            # (设 _pending_permission_request + 跑 hook);返回的 sentinel 不读
            # (ASK 路径已定,handler 自己返 marker tuple 让三态分流)。
            agent._ask_user_permission_v2(tool_name, tool_input, decision)
            return False, "__AWAITING_PERMISSION__", tool_input
        # passthrough 或其他 → 当作 ASK(返 marker tuple 而非 string)
        permission_logger.debug(
            "🛡️ [passthrough_to_ask] tool=%s behavior=%s",
            tool_name, decision.behavior,
        )
        return False, "__AWAITING_PERMISSION__", tool_input

    def _run_permission_denied_hook(
        self,
        tool_name: str,
        tool_input: dict,
        decision: Any,
    ) -> Optional[str]:
        """跑 PermissionDenied hook(M3 Task 3,对齐 doc §4.4)。

        2026-07-02 Plan C:从 ReactAgent._run_permission_denied_hook 迁入(原 agent_core.py:557)。

        Returns:
            retry_prompt 字符串(hook 给出的重试提示)或 None
            异常时返 None(不阻断 deny,只是不加 hint)
        """
        agent = self._agent
        if agent.permission_engine is None:
            return None
        hook_registry = getattr(agent.permission_engine, "hook_registry", None)
        if hook_registry is None:
            return None
        try:
            denied = hook_registry.run_permission_denied(
                tool_name, tool_input, agent.permission_engine.context, decision,
            )
            return getattr(denied, "retry_prompt", None)
        except Exception as e:
            _logger.warning("PermissionDenied hook 异常: %s", e)
            return None

    def _build_request(self, tc, perm_err: str) -> Dict[str, Any]:
        """构造 permission request dict(ASK 路径专用)。

        字段从 `self._agent._pending_permission_request` 取(由 _ask_user_permission_v2 写入),
        fallback 时用 tc 字段填充。
        Deny-loop fix (2026-06-30):tool_use_id 必须透传,resume_after_permission 用它构造 tool_result。

        Args:
            tc:原始 ToolCall 对象(LLM 产出);提供 tool_name / tool_input / tool_use_id。
            perm_err:`__AWAITING_PERMISSION__` 字符串 sentinel(实际不读,只是签名占位)。

        Returns:
            带 tool_use_id 字段的 permission request dict。
        """
        agent = self._agent
        req = agent._pending_permission_request or {
            "tool_name": tc.tool_name,
            "tool_input": tc.tool_input,
            "reason": "",
            "message": "",
        }
        # Deny-loop fix (2026-06-30): 把 tool_use_id 透传。
        # 注:不能 dict 直接 mutate(可能复用 _pending_permission_request 实例),用 dict()/setdefault 改写。
        if "tool_use_id" not in req:
            req = {**req, "tool_use_id": tc.tool_use_id}
        return req

    def handle(self, ctx: TurnContext) -> HandlerResult:
        """PermissionCheckHandler 真业务(2026-07-02 完整实现)。

        流程(7 步):
        1. 取 ctx.stage_outputs;若 None + _is_resume 条件 → 重建(从 run_state.last_tool_calls),
           写回 ctx.stage_outputs。不写 ctx.permission_request(防 await loop)。
        2. stage_out 仍 None 或 tool_calls 空 → no-op return。
        3. _is_resume=True → 整 batch pre-ALLOW(tool_calls 各 tc outcome="allow"),
           跳到第 7 步(直接灌 ctx.permission_decisions)。
        4. 首次 entry:把 assistant tool_use blocks 推 self.messages(_is_resume 跳过)。
        5. 对每 tc 调 self._check_tool_permission 三态分流(Plan C 迁入 handler):
           - allowed=True → outcome="allow", effective_input=返回
           - perm_err=="__AWAITING_PERMISSION__" → outcome="ask", req=自构造
           - 其他 → outcome="deny", error=perm_err or "Permission denied"
        6. 若有 ASK(asks 非空):
           - run_state.awaiting_permission_batch = asks
           - run_state.awaiting_permission = asks[0](back-compat web/app.py)
           - ctx.permission_request = asks[0]
           - SM 强制转 AWAITING_PERMISSION(同 agent_core.py:847-857 逻辑)
           - ctx.emit(("awaiting_permission", asks[0]))
           - return _StopChain
        7. 否则:ctx.permission_decisions = decisions;写 ctx._is_resume 给后续 handler 用;
           return HandlerResult()(让 ToolDispatchHandler 继续)。

        委托链(2026-07-02 SRP 拆分后):
            ExecutingToolsPhase.enter() → self._chain.run(ctx.turn_ctx)
                → PermissionCheckHandler.handle(ctx)
                    → self._check_tool_permission(tc, input)  # 真业务(Plan C 从 agent 迁入)
                ← TcPermissionDecision 列表
            测试入口:list(agent._tool_chain.run(turn_ctx))(2026-07-02 删 _iter_phase_tools 后)

        Fix C 契约(_is_resume):
            触发条件 `stage_out is None and last_tool_calls and awaiting_permission != None`。
            resume 后:不重 yield tool_call event(已 yield 过)+ 不 append action log,
            整 batch pre-fill ALLOW。用户已点 Allow/Always allow 才进入此分支。
        """
        agent = self._agent
        if agent is None:
            return HandlerResult()
        run_state = getattr(agent, "_run_state", None)

        # ── 步 1:取 stage_outputs,resume 重建 ──
        stage_out = getattr(ctx, "stage_outputs", None)
        _is_resume = (
            stage_out is None
            and run_state is not None
            and bool(run_state.last_tool_calls)
            and run_state.awaiting_permission is not None
        )

        if _is_resume:
            # 从 run_state.last_tool_calls 重建(原始 assistant message 已在 messages 尾部)
            full_text_recovered = ""
            try:
                if agent.messages and agent.messages[-1].get("role") == "assistant":
                    last = agent.messages[-1]
                    content = last.get("content")
                    if isinstance(content, list):
                        for blk in content:
                            if isinstance(blk, dict) and blk.get("type") == "text":
                                full_text_recovered += blk.get("text", "")
                    elif isinstance(content, str):
                        full_text_recovered = content
            except (AttributeError, IndexError):
                full_text_recovered = ""

            stage_out = _LLMResult(
                tool_calls=list(run_state.last_tool_calls),
                full_text=full_text_recovered,
                thinking_text="",
                stop_reason="end_turn",
                usage=None,
            )
            ctx.stage_outputs = stage_out
            # 注意:不写 ctx.permission_request(Fix C:否则 LLMThinkingPhase.next()
            # 会看 permission_request 非空 → 路由回 AWAITING_PERMISSION,死循环)。

        # ── 步 2:无可执行 tool_calls → no-op ──
        if stage_out is None:
            ctx._is_resume = _is_resume
            return HandlerResult()
        tool_calls = stage_out.tool_calls or []
        if not tool_calls:
            ctx._is_resume = _is_resume
            return HandlerResult()

        # ── 步 3:resume 路径 → 整 batch pre-ALLOW ──
        if _is_resume:
            ctx.permission_decisions = [
                TcPermissionDecision(
                    tc_id=tc.tool_use_id,
                    tc=tc,
                    outcome="allow",
                    effective_input=tc.tool_input,
                )
                for tc in tool_calls
            ]
            ctx._is_resume = True
            # 不 clear run_state.awaiting_permission — ToolExecuteHandler 完成后在尾清
            return HandlerResult()

        # ── 步 4:首次 entry — append assistant + tool_use blocks ──
        full_text = stage_out.full_text or ""
        assistant_content = []
        for tc in tool_calls:
            assistant_content.append({
                "type": "tool_use",
                "id": tc.tool_use_id,
                "name": tc.tool_name,
                "input": tc.tool_input,
            })
        if full_text:
            assistant_content.insert(0, {"type": "text", "text": full_text})
        agent.messages.append({"role": "assistant", "content": assistant_content})

        # ── 步 5:对每个 tc 三态分流 ──
        decisions: List[TcPermissionDecision] = []
        asks: List[Dict[str, Any]] = []
        for tc in tool_calls:
            try:
                allowed, perm_err, effective_input = self._check_tool_permission(
                    tc.tool_name, tc.tool_input,
                )
            except Exception as e:
                # permission_engine 异常 → 兜底 deny,不让 chain 阻塞
                _logger.warning(
                    "[PermissionCheckHandler] _check_tool_permission 抛错,降级 deny: %s",
                    e,
                )
                decisions.append(TcPermissionDecision(
                    tc_id=tc.tool_use_id, tc=tc, outcome="deny",
                    error=f"Permission check failed: {type(e).__name__}: {e}",
                ))
                continue

            if allowed:
                decisions.append(TcPermissionDecision(
                    tc_id=tc.tool_use_id, tc=tc, outcome="allow",
                    effective_input=effective_input,
                ))
            elif perm_err == "__AWAITING_PERMISSION__":
                req = self._build_request(tc, perm_err)
                decisions.append(TcPermissionDecision(
                    tc_id=tc.tool_use_id, tc=tc, outcome="ask",
                    request=req,
                ))
                asks.append(req)
            else:
                decisions.append(TcPermissionDecision(
                    tc_id=tc.tool_use_id, tc=tc, outcome="deny",
                    error=perm_err or "Permission denied",
                ))

        # ── 步 6:若有 ASK → 暂停 chain ──
        if asks:
            if run_state is not None:
                run_state.awaiting_permission_batch = asks
                run_state.awaiting_permission = asks[0]  # back-compat web/app.py
            ctx.permission_request = asks[0]
            # 同步强制 SM 转 AWAITING_PERMISSION(避免 yield 暂停后 SM 卡在 EXECUTING_TOOLS)
            sm = getattr(agent, "_sm", None)
            if sm is not None and sm.current != AgentPhase.AWAITING_PERMISSION:
                _old_phase = sm.current
                sm._phase = AgentPhase.AWAITING_PERMISSION
                sm._history.append((
                    _old_phase, "permission_needed", AgentPhase.AWAITING_PERMISSION,
                ))
                _logger.debug(
                    "🔒 [PermissionCheckHandler SM force-transition] %s --[permission_needed]--> awaiting_permission",
                    _old_phase.value,
                )
            # emit 单 req(back-compat;后续 multi-UI 升级读 awaiting_permission_batch)
            ctx.emit(("awaiting_permission", asks[0]))
            ctx._is_resume = False
            return _StopChain

        # ── 步 7:无 ASK → decisions 灌 ctx.permission_decisions,继续 chain ──
        ctx.permission_decisions = decisions
        ctx._is_resume = False
        return HandlerResult()


# ── 7. ToolDispatchHandler(tool_chain) ────────────────────
class ToolDispatchHandler:
    """tool_chain 中段:emit tool_call event(单/并行) + 灌 dispatch decisions。

    2026-07-02 完整实现(Plan B 拆分):
    1. 取 ctx.permission_decisions(由 PermissionCheckHandler 写)。
       - 空 → no-op
    2. resume 路径(ctx._is_resume True)→ 跳过 emit + pending log(action 已 yield 过)。
    3. 否则:
       - 单决策(len==1)→ emit ('tool_call', {name, input, parallel: False}) + log action
       - 多决策(len≥2)→ emit ('tool_call', {names, parallel: True}) + log parallel_start
    4. ctx._dispatch_decisions = decisions(给 ToolExecuteHandler 消费)。

    stop_chain:不主动 stop — 让 ToolExecuteHandler 继续(它需要 _dispatch_decisions)。
    """
    name = "tool_dispatch"

    def __init__(self, agent):
        self._agent = agent

    def handle(self, ctx: TurnContext) -> HandlerResult:
        agent = self._agent
        decisions = getattr(ctx, "permission_decisions", None) or []
        if not decisions:
            return HandlerResult()

        run_state = getattr(agent, "_run_state", None)

        # emit tool_call(Action) + pending log(2026-07-03 修 Action 不显示)。
        # 原先 if not _is_resume 跳过(注释假设"action 已 yield 过"),但 ASK 路径
        # PermissionCheckHandler _StopChain 短路 → 首次 ToolDispatchHandler 没跑、
        # action 从没 emit → UI 永远不显示 Action。emit 点唯一(本 handler),首次
        # 没跑、resume 跑一次,移除跳过不会重复。
        if len(decisions) == 1:
            d = decisions[0]
            ctx.emit(("tool_call", {
                "name": d.tc.tool_name,
                "input": d.tc.tool_input,
                "parallel": False,
            }))
            if run_state is not None:
                run_state.pending_tool_logs.append({
                    "type": "action",
                    "name": d.tc.tool_name,
                    "input": d.tc.tool_input,
                })
        else:
            tool_names = [d.tc.tool_name for d in decisions]
            ctx.emit(("tool_call", {
                "names": tool_names,
                "parallel": True,
            }))
            if run_state is not None:
                run_state.pending_tool_logs.append({
                    "type": "parallel_start",
                    "names": tool_names,
                    "parallel": True,
                })

        ctx._dispatch_decisions = decisions
        return HandlerResult()


# ── 8. ToolExecuteHandler(tool_chain 中位) ────────────────
class ToolExecuteHandler:
    """tool_chain 中位:消费 dispatch decisions → DENY pre-fill + ALLOW execute → emit tool_result。

    2026-07-02 完整实现(Plan B 拆分):
    取代原 `agent._iter_phase_tools()` 单方法内的执行+post-process 逻辑
    (2026-07-02 Cleanup:orchestrator 也已删除,handler 直接由 tool_chain 调),
    保持 emit shape 一致(events + messages + pending_tool_results + pending_tool_logs 4 个 sink),
    让 ToolPairPersistHandler(Stage B)在 chain 末位统一落盘。

    委托链:
        ExecutingToolsPhase.enter() → self._chain.run(ctx.turn_ctx)
        → ToolExecuteHandler.handle(ctx)
            → agent.tools.execute(name, input, max_retries, cancel_event)  # 真业务
            → agent.messages + run_state.pending_tool_results(Stage B 消费)
        ← emit tool_result events

    DENY 路径:对每个 outcome="deny" 的 decision,emit 假 tool_result(error)同步,不抛错。
    ALLOW 路径:
        - 1 个 decision → tools.execute() 同步调用
        - ≥2 个 decisions → ThreadPoolExecutor 并行,as_completed 后按 LLM 顺序 emit
    每个结果:emit tool_result + log result + push self.messages(tool_result block)+ push pending。

    Fix C 尾清理:RunState.awaiting_permission = None + 清 batch(permission ASK 路径完成后必须清,
    否则下个 turn 又触发 resume 路径)。

    cancellation:每个 tool.execute 接收 cancel_event(由 run_state.cancel_event 传入,
    ThreadPoolExecutor + cancel_event kwarg 契约保留)。ToolRegistry 内做真实 cancel。

    异常兜底:tools.execute 抛 → 不连带回 abort chain,降级为 {"status":"error","error": str(e)} 假 result
    继续 emit(避免一个 tool 崩整个 batch fail)。
    """
    name = "tool_execute"

    def __init__(self, agent):
        self._agent = agent

    def handle(self, ctx: TurnContext) -> HandlerResult:
        agent = self._agent
        if agent is None:
            return HandlerResult()

        # Lazy imports — turn_chain ⇄ agent_core 互相 import,得在 handle() 内做(模块 load 时)
        from agent_core.agent_core import _make_tool_result_block

        decisions = (
            getattr(ctx, "_dispatch_decisions", None)
            or getattr(ctx, "permission_decisions", None)
            or []
        )
        if not decisions:
            return HandlerResult()

        denied = [d for d in decisions if d.outcome == "deny"]
        allowed = [d for d in decisions if d.outcome == "allow"]

        run_state = getattr(agent, "_run_state", None)
        cancel_evt = run_state.cancel_event if run_state is not None else None

        # ── DENY pre-result 路径(synthetic error result) ──
        for d in denied:
            output = d.error or "Permission denied"
            ctx.emit(("tool_result", {
                "name": d.tc.tool_name,
                "output": output,
                "success": False,
                "elapsed": 0.0,
            }))
            if run_state is not None:
                run_state.pending_tool_logs.append({
                    "type": "result",
                    "name": d.tc.tool_name,
                    "output": output,
                    "success": False,
                    "elapsed": 0.0,
                })
            agent.messages.append(_make_tool_result_block(d.tc.tool_use_id, output))
            if run_state is not None:
                run_state.pending_tool_results.append((d.tc.tool_use_id, output))

        # ── ALLOW 执行路径(单 / 多) ──
        results_ordered: List[tuple] = []  # [(decision, result_dict, elapsed), ...]

        if not allowed:
            pass
        elif len(allowed) == 1:
            d = allowed[0]
            tc = d.tc
            inp = d.effective_input if d.effective_input is not None else tc.tool_input
            try:
                start = time.time()
                result = agent.tools.execute(
                    tc.tool_name, inp,
                    max_retries=DEFAULT_MAX_RETRIES,
                    cancel_event=cancel_evt,
                )
                elapsed = time.time() - start
            except Exception as e:
                _logger.error(
                    "[ToolExecuteHandler] tool execute exception name=%s err=%s",
                    tc.tool_name, e,
                )
                result = {"status": "error", "error": str(e)}
                elapsed = 0.0
            results_ordered.append((d, result, elapsed))
        else:
            # 并行 — ThreadPoolExecutor
            # 注:保留 LLM 原始顺序(用 as_completed 收集后 sort 回 tc_order)
            tc_order = [d.tc for d in allowed]

            def _safe_exec(decision) -> tuple:
                tc = decision.tc
                inp = decision.effective_input if decision.effective_input is not None else tc.tool_input
                try:
                    start = time.time()
                    r = agent.tools.execute(
                        tc.tool_name, inp,
                        max_retries=DEFAULT_MAX_RETRIES,
                        cancel_event=cancel_evt,
                    )
                    return decision, r, time.time() - start
                except Exception as e:
                    _logger.error(
                        "[ToolExecuteHandler] parallel tool exception name=%s err=%s",
                        tc.tool_name, e,
                    )
                    return decision, {"status": "error", "error": str(e)}, 0.0

            with concurrent.futures.ThreadPoolExecutor() as executor:
                futures = [executor.submit(_safe_exec, d) for d in allowed]
                collected: List[tuple] = []
                for fut in futures:
                    collected.append(fut.result())
                # Restore LLM order
                tc_to_decision = {d.tc.tool_use_id: d for d in allowed}
                # sort by position of tc in tc_order
                tc_order_index = {id(tc): i for i, tc in enumerate(tc_order)}
                collected.sort(
                    key=lambda x: tc_order_index.get(id(x[0].tc), len(tc_order)),
                )
                results_ordered = collected

        # ── Emit + 4 sinks 写入 ALLOW 结果 ──
        for d, result, elapsed in results_ordered:
            # tool 计数累加(2026-07-03):每个 ALLOW 执行结果(success/error)+1。
            # DENY 在上方分支(L1597-1616)不调 execute、不进 results_ordered,天然不计。
            # ASK 路径 PermissionCheckHandler _StopChain 短路,本 handler 不跑 → resume 后首次执行才累加。
            agent.session_counter.add_tool(1)
            tc = d.tc
            if result.get("status") == "success":
                output = result.get("output", "")
                success = True
            else:
                output = f"工具执行失败: {result.get('error', 'unknown')}"
                success = False

            ctx.emit(("tool_result", {
                "name": tc.tool_name,
                "output": output,
                "success": success,
                "elapsed": elapsed,
            }))
            if run_state is not None:
                run_state.pending_tool_logs.append({
                    "type": "result",
                    "name": tc.tool_name,
                    "output": output,
                    "success": success,
                    "elapsed": elapsed,
                })
            agent.messages.append(_make_tool_result_block(tc.tool_use_id, output))
            if run_state is not None:
                run_state.pending_tool_results.append((tc.tool_use_id, output))

        # ── Fix C 尾清理 ──
        if run_state is not None and run_state.awaiting_permission is not None:
            run_state.awaiting_permission = None
            run_state.awaiting_permission_batch = []

        return HandlerResult()


# ── 9. LLMCallPersistHandler(llm_chain 末位) ── Stage A ───
class LLMCallPersistHandler:
    """Stage A — LLM 响应后立即落 assistant 块,挂 LLMCallHandler 之后。

    Plan B (2026-07-01 删 v1 双轨):
    接管 v1 streaming 路径 8 处 add_* 中的 7 处(agent_core.py 写入点 #1/#2/#3/#4/#5/#6/#8):

    - Stage A1 (stop_reason 正常 + 有 tool_calls):
        写 assistant_with_tools(text + tool_use blocks) — Claude Code 风格一条 entry
    - Stage A2 (stop_reason="llm_error"):
        写 partial / fallback error 文本
    - Stage A3 (stop_reason="interrupted"):
        写 partial interrupt 文本(LLM 流被 cancel_event 打断)

    不写的场景(no-op,留给其他 handler):
    - 正常 final text (stop_reason="end_turn" + 无 tool_calls + 有 full_text):
        SessionPersistHandler (B) 段在 FINALIZING 触发写 — 跟本 handler 不重复
    - stage_outputs=None:意味着 EXECUTING_TOOLS/FINALIZING 触发,不是 LLM_THINKING 末尾

    crash-safety:Stage A 在 LLMCallHandler 末位跑,LLM 响应完立即落盘,后续
    tool 执行 crash 不会丢 assistant block(对比 v1 已知丢数据场景)。
    """
    name = "llm_call_persist"

    def __init__(self, agent):
        self._agent = agent

    def handle(self, ctx: TurnContext) -> HandlerResult:
        agent = self._agent
        if agent is None or agent._session_manager is None:
            return HandlerResult()
        stage_out = ctx.stage_outputs
        if stage_out is None:
            return HandlerResult()
        try:
            stop_reason = getattr(stage_out, "stop_reason", None)
            tool_calls = getattr(stage_out, "tool_calls", None) or []
            full_text = getattr(stage_out, "full_text", "") or ""

            if stop_reason in ("llm_error", "interrupted"):
                # Stage A2 / A3: partial / error / interrupt 文本兜底写盘
                # Plan A: v1 _iter_phase_llm error / interrupt path 写过同样的 fallback
                text = full_text or f"[{stop_reason}]"
                kwargs = {}
                thinking = getattr(stage_out, "thinking_text", None)
                if thinking:
                    kwargs["thinking"] = thinking
                usage = _usage_asdict(getattr(stage_out, "usage", None))
                if usage is not None:
                    kwargs["usage"] = usage
                agent._session_manager.add_assistant_message(text, **kwargs)
            elif tool_calls:
                # Stage A1: assistant + tool_use 一条 entry(对齐 Claude Code 风格)
                # 覆盖 v1 #3 / #4 / #8 (add_assistant_with_tools)
                tc_list = [
                    {
                        "id": getattr(tc, "tool_use_id", getattr(tc, "id", "")),
                        "name": getattr(tc, "tool_name", getattr(tc, "name", "")),
                        "input": getattr(tc, "tool_input", getattr(tc, "input", {})),
                    }
                    for tc in tool_calls
                ]
                agent._session_manager.add_assistant_with_tools(
                    text=full_text,
                    tool_calls=tc_list,
                )
            # 正常 final text(stop_reason="end_turn" + 无 tool_calls + 有 full_text):
            # 留给 SessionPersistHandler (B) 段 FINALIZING 触发写
        except Exception as e:
            _logger.warning(f"LLMCallPersistHandler (Stage A) failed: {e}")
        return HandlerResult()


# ── 9c-pre. FinalAnswerBookkeepingHandler(output_chain 首位) ─ in-memory state ─
class FinalAnswerBookkeepingHandler:
    """FINALIZING phase 首位:更新 in-memory 状态(非持久化)。

    Plan B Final Phase (2026-07-02):从 v1 _iter_phase_finalize L945-L953 拆出。
    Stage C (FinalAnswerPersistHandler) 只写 session.jsonl,本 handler
    更新 self.messages + _run_state.final_answer/final_stop_reason + emit system event。

    拆分的必要性(避免误判为 dead code):
      - Stage C 只调 add_assistant_message 落 session.jsonl
      - self.messages.append 和 _run_state.final_answer 赋值是 in-memory 状态,
        Stage C 不覆盖,本 handler 接管
      - emit ("system", "✅ 回答完成") 是 UI event,Stage C 不 emit
      - 三者职责正交,本 handler 必须独立存在

    位置:output_chain 首位(在 Stage C 之前)—
      让 _run_state.final_answer 在 persist 前就设好,后续 MemoryBridgeExtract
      可直接读 gate。

    不写的场景(no-op):
      - ctx.stage_outputs 缺失(EXECUTING_TOOLS 续 turn 时)
      - agent._run_state 缺失(老调用方 v1 path,RunState 在 start_run() 才创建)

    对应 v1 写入点:
      v1 _iter_phase_finalize L945-L953(原 in-memory bookkeeping 段)
    """
    name = "final_answer_bookkeeping"

    def __init__(self, agent):
        self._agent = agent

    def handle(self, ctx: TurnContext) -> HandlerResult:
        agent = self._agent
        if agent is None:
            return HandlerResult()
        stage_out = getattr(ctx, "stage_outputs", None)
        if stage_out is None:
            return HandlerResult()
        full_text = getattr(stage_out, "full_text", "") or ""
        stop_reason = getattr(stage_out, "stop_reason", None)
        tool_calls = getattr(stage_out, "tool_calls", None) or []

        run_state = getattr(agent, "_run_state", None)
        if not tool_calls:
            # 无 tool_calls:append assistant message + 设 run_state + emit system event
            agent.messages.append({"role": "assistant", "content": full_text})
            if run_state is not None:
                run_state.final_answer = full_text
                run_state.final_stop_reason = stop_reason
            ctx.emit(("system", "✅ 回答完成"))
        else:
            # 有 tool_calls:仍设 final_answer(供后续 MemoryBridgeExtract 读)
            # 不 append 不 emit — Stage A1 (LLMCallPersistHandler) 已写 assistant+tool_use
            if run_state is not None:
                run_state.final_answer = full_text
        return HandlerResult()


# ── 9c. FinalAnswerPersistHandler(output_chain 首位) ─ Stage C ─
class FinalAnswerPersistHandler:
    """Stage C — FINALIZING phase 触发:写 final assistant text + 4 字段。

    Plan B (2026-07-01):替代原 SessionPersistHandler (B) 分支。final answer
    4 字段对齐 v1 _iter_phase_llm:2263 add_assistant_message 签名:
      - full_text:必有(非空才写)
      - thinking:可选(stage_out.thinking_text)
      - tool_logs:可选(RunState.pending_tool_logs)
      - usage:可选(stage_out.usage,dataclass 用 _usage_asdict 转 dict)

    触发位置:FINALIZING phase 进入 output_chain 时(output_chain 首位)。
    触发条件:ctx.stage_outputs 存在 + 无 tool_calls + 有 full_text +
              stop_reason NOT IN ("max_tokens", "length")

    对应 v1 写入点:
      #7  agent_core/agent_core.py:L2226 (run() final answer 4 字段)

    不写的场景(no-op):
      - 有 tool_calls → Stage A1 (LLMCallPersistHandler) 已写
      - full_text 为空(LLM 拒绝了 / thinking-only 响应)
      - stop_reason="max_tokens" 或 "length" → 视为截断,不进 finalize 抽取
        (对齐 test_react_agent_bridge L100/L116:max_tokens/length 路径
        bridge.on_turn_end 不调,本 handler 同样不写 final)
      - stage_out is None(EXECUTING_TOOLS 续 turn 时)

    不写 tool_result (Stage B 接);不写 assistant+tool_use (Stage A 接)。

    crash-safety:Stage C 在 FINALIZING phase 末尾,LLM 给出 final text 后
    立即 4 字段落盘。后续 bridge.on_turn_end 或 memory extract crash 不会丢
    final answer。
    """
    name = "final_answer_persist"

    def __init__(self, agent):
        self._agent = agent

    def handle(self, ctx: TurnContext) -> HandlerResult:
        agent = self._agent
        if agent is None or agent._session_manager is None:
            return HandlerResult()
        stage_out = getattr(ctx, "stage_outputs", None)
        if stage_out is None:
            return HandlerResult()
        # 有 tool_calls → Stage A1 已经写过 assistant+tool_use,这里不重复
        tool_calls = getattr(stage_out, "tool_calls", None) or []
        if tool_calls:
            return HandlerResult()
        full_text = getattr(stage_out, "full_text", "") or ""
        if not full_text:
            return HandlerResult()
        # max_tokens / length 视为截断,不写 final(对齐 §10 + bridge 行为)
        stop_reason = getattr(stage_out, "stop_reason", None)
        if stop_reason in ("max_tokens", "length"):
            return HandlerResult()

        try:
            kwargs = {}
            thinking = getattr(stage_out, "thinking_text", None) or None
            if thinking:
                kwargs["thinking"] = thinking
            usage = _usage_asdict(getattr(stage_out, "usage", None))
            if usage is not None:
                kwargs["usage"] = usage
            tool_logs = getattr(
                getattr(agent, "_run_state", None), "pending_tool_logs", None
            )
            if tool_logs:
                kwargs["tool_logs"] = tool_logs
            agent._session_manager.add_assistant_message(full_text, **kwargs)
            # 写后清 RunState.pending_tool_logs,避免跨 turn double-write
            if tool_logs and getattr(agent, "_run_state", None) is not None:
                agent._run_state.pending_tool_logs = []
        except Exception as e:
            _logger.warning(f"FinalAnswerPersistHandler (Stage C) failed: {e}")
        return HandlerResult()


# ── 10. AuditLogHandler(output_chain) ─────────────────────
class AuditLogHandler:
    """output_chain 中段:写 audit log(tool 决策 + 工具结果)。

    Plan A: 默认 no-op(permission_engine 内部已统一写 audit)。
    仅作扩展点 — 用户可在此 hook 自己的 audit 逻辑。
    """
    name = "audit_log"

    def __init__(self, agent):
        self._agent = agent

    def handle(self, ctx: TurnContext) -> HandlerResult:
        # 默认 no-op(permission_engine 内部 audit 已统一写)
        return HandlerResult()


# ── 9b. ToolPairPersistHandler(tool_chain 末位) ─ Stage B ───────
class ToolPairPersistHandler:
    """Stage B — tool 执行完立即落 tool_result,挂 ToolExecuteHandler 之后。

    Plan B (2026-07-01):
    接管 v1 _iter_phase_tools 普通 tool_result 路径 — 把 `add_tool_results()`
    调用从 `_iter_phase_tools` 散落代码集中到本 handler,语义统一。

    写入:session_manager.add_tool_results([{tool_use_id, content}, ...])
          — 每条 tool_result 一条 user entry,对齐 Anthropic API 协议。

    数据源:agent._run_state.pending_tool_results(由 ToolExecuteHandler 在 tool
          执行完后 fill [(tool_use_id, output), ...],DENY pre-fill 也写)。
          Stage B 读完即清,避免跨 turn 累加。

    幂等性(resume_after_permission 路径):
        - Stage A 写完 assistant+tool_use → AWAITING_PERMISSION(turn 暂停)
        - 用户决策 allow → resume_after_permission 续 turn
        - tool_chain 重跑 → ToolExecuteHandler 调 _iter_phase_tools (单 tool 路径
          走允许分支) → 同一 (tool_use_id, output) 再 append 到 RunState.pending_tool_results
        - Stage B 在续 turn 的 tool_chain 末位被触发,读 + 清 RunState,**会写一次**
        - 这是预期行为(用户决策后才出 tool_result),不是 double-write

    对应 v1 写入点:
        原 _iter_phase_tools L2490-2511 普通 tool_result 路径(add_tool_results)
        原 _iter_phase_tools L1745 deny immediate flush(保留,**不**经 Stage B —
        该路径有 "防止 deny orphan tool_use" 特殊语义,L1745 仍写 immediate)

    不写的场景(no-op):
        - pending_tool_results 为空(没有 tool 调用结果)— 普通文本回复轮
        - agent._run_state 为 None(老调用方 v1 path)— RunState 在 start_run() 才创建

    crash-safety:Stage B 在 ToolExecuteHandler 末位,tool 跑完立即写 tool_result,
    后续 LLM 调用 / 决策 crash 不会丢 tool_result(对比 Plan B 之前 finalize
    阶段的延迟 flush 风险)。
    """
    name = "tool_pair_persist"

    def __init__(self, agent):
        self._agent = agent

    def handle(self, ctx: TurnContext) -> HandlerResult:
        agent = self._agent
        if agent is None or agent._session_manager is None:
            return HandlerResult()
        run_state = getattr(agent, "_run_state", None)
        if run_state is None:
            return HandlerResult()
        pending = getattr(run_state, "pending_tool_results", None)
        if not pending:
            return HandlerResult()
        try:
            results = [
                {"tool_use_id": tid, "content": output}
                for tid, output in pending
            ]
            agent._session_manager.add_tool_results(results)
        except Exception as e:
            _logger.warning(f"ToolPairPersistHandler (Stage B) failed: {e}")
        finally:
            # 不论成败都清空 — 失败时重新走会再 yield events 触发 Stage B 重写
            # (虽然可能丢 tool_result,但可观察;不清空会让 Stage B 每 turn 都重复 flush)
            run_state.pending_tool_results = []
        return HandlerResult()


# ── 11. MemoryBridgeExtractHandler(output_chain) ─────────
class MemoryBridgeExtractHandler:
    """output_chain 末位:触发 react_memory_bridge.on_turn_end。

    Plan B Final Phase (2026-07-02):从 v1 _iter_phase_finalize L956-L973 拆出。
    不再 delegate 给 _iter_phase_finalize,直调 bridge.on_turn_end + emit memory_event。

    Gate 条件(同 v1):
      1. react_memory_bridge 不为 None
      2. _run_state.turn > 0(初始 turn 不抽取)
      3. _run_state.final_answer 不为空
      4. _run_state.final_stop_reason 不在 _TRUNCATED_STOP_REASONS
         (避免把 max_tokens/length 截断的半句话存成记忆)

    位置:output_chain 第 4 位(Persist → AuditLog → 本 handler → SessionFlush)
      - 在 Stage C (Persist) 之后:确保 final_answer 已落盘再触发外部抽取
      - 在 SessionFlush 之前:让 SessionFlushHandler 跑(不再 return _StopChain)

    对应 v1 写入点:
      v1 _iter_phase_finalize L956-L973(memory bridge extract 段)

    异常处理:bridge.on_turn_end 抛异常时 _logger.warning,不 crash
      chain — 继续让 SessionFlushHandler 跑兜底 flush。
    """
    name = "memory_bridge_extract"

    def __init__(self, agent):
        self._agent = agent

    def handle(self, ctx: TurnContext) -> HandlerResult:
        agent = self._agent
        if agent is None:
            return HandlerResult()
        bridge = getattr(agent, "react_memory_bridge", None)
        run_state = getattr(agent, "_run_state", None)
        if bridge is None or run_state is None:
            return HandlerResult()
        if run_state.turn <= 0:
            return HandlerResult()
        if not run_state.final_answer:
            return HandlerResult()
        if run_state.final_stop_reason in _TRUNCATED_STOP_REASONS:
            return HandlerResult()

        try:
            for event in bridge.on_turn_end(
                user_msg=run_state.user_message,
                assistant_resp=run_state.final_answer,
                turn_index=run_state.turn,
                counter=agent.session_counter,
            ):
                ctx.emit(("memory_event", event))
        except Exception as e:
            _logger.warning(f"Memory bridge failed: {e}")
        return HandlerResult()  # 不再 _StopChain,让 SessionFlushHandler 跑


# ── 12. SessionFlushHandler(output_chain 末位) ─────────
class SessionFlushHandler:
    """FINALIZING phase 末位:run 末尾兜底 flush session。

    Plan B Final Phase (2026-07-02):从 v1 _iter_phase_finalize L982-L986 拆出。
    仅当 _sm.is_done 为真时调 session_manager.flush()(FINALIZING phase 跑完
    即 DONE,等价"run 末尾")。Stage A/B/C 已在 chain 内部即时持久化,本 handler
    是兜底 — 确保 stream buffer / 任何延迟写入都 flush 到磁盘。

    位置:output_chain 末位(MemoryBridgeExtract 之后)
      - 让 MemoryBridgeExtract 先跑完(它可能 emit memory_event,UI 消费者
        先看到 event 再看到 flush done)
      - 兜底是设计意图,与 v1 `_iter_phase_finalize` L982-986 行为完全一致

    不 flush 的场景(no-op):
      - session_manager 缺失(老调用方 v1 path)
      - state_machine 缺失
      - state_machine.is_done 为 False(FINALIZING phase 还没跑完)

    异常处理:flush 抛异常时 _logger.warning,不 crash chain — RunState
      持久化已在 Stage A/B/C 完成,flush 失败不丢已有数据。

    对应 v1 写入点:
      v1 _iter_phase_finalize L982-L986(session flush 段)
    """
    name = "session_flush"

    def __init__(self, agent):
        self._agent = agent

    def handle(self, ctx: TurnContext) -> HandlerResult:
        agent = self._agent
        if agent is None:
            return HandlerResult()
        sm = getattr(agent, "_session_manager", None)
        state_machine = getattr(agent, "_sm", None)
        if sm is None or state_machine is None:
            return HandlerResult()
        if not state_machine.is_done:
            return HandlerResult()
        try:
            sm.flush()
        except Exception as e:
            _logger.warning(f"Failed to flush session: {e}")
        return HandlerResult()  # 不再 _StopChain,让 SessionFlushHandler 跑


# ── 14. EnvCleanupHandler(output_chain 最末位, T034) ───────
class EnvCleanupHandler:
    """outputs_chain 最末位:run 结束兜底调 env reverter(FR-010, 解决 run-end 泄漏 C2)。

    设计动机:
      SkillsPromptHandler.handle() 在每个 turn 入口调 apply_skill_env_overrides
      注入 secret 到 os.environ + 返 reverter。**reverter 不在 handler 内调**
      (由本 handler 兜底) —— 这样:
      - 同 run 多次 turn 时,每个 turn 都能保持 secret 注入(Bash tool 任意 turn 可读)
      - run 真正结束时(本 handler),统一还原 env 到 run 前状态

    行为:
      - 取 agent._pending_env_reverter;非 None 则调之 + 置 None
      - 幂等(连续两次调无副作用 —— 第二次 reverter 已 None)
      - reverter 抛异常时 log warning,不 crash chain

    位置:outputs_chain 末位(SessionFlushHandler 之后)。
      SessionFlush 先跑(session 已落盘),最后再还原 env —— 即使下游 handler
      异常也保证 env 不残留(防御式务实工程)。

    不还原的场景(no-op):
      - agent is None
      - agent._pending_env_reverter 为 None(无注入或已还原过)

    对应 C2 修复(2026-07-06):原先 reverter 在 SkillsPromptHandler 内 try/finally
    调 → run 最后一个 turn 结束后 env 永久注入,污染后续 run。C2 修复:延迟到
    outputs_chain 最末位统一还原,跨 turn 复用、跨 run 隔离。
    """
    name = "env_cleanup"

    def __init__(self, agent):
        self._agent = agent

    def handle(self, ctx: TurnContext) -> HandlerResult:
        agent = self._agent
        if agent is None:
            return HandlerResult()
        reverter = getattr(agent, "_pending_env_reverter", None)
        if reverter is None:
            return HandlerResult()  # 幂等:已还原过
        try:
            reverter()
            _logger.debug("🧩 env reverted: keys=%d", len(getattr(reverter, "_injected_keys", [])))
        except Exception as e:
            _logger.warning(f"🧩 EnvCleanupHandler reverter failed: {e}")
        finally:
            # 无论成败都置 None —— 失败时不能再调(防止重复 pop 误改 env)
            agent._pending_env_reverter = None
        return HandlerResult()


# ── 13. L3SMExtractTriggerHandler(output_chain 第 5 位) ─────
class L3SMExtractTriggerHandler:
    """output_chain 第 5 位:触发 L3 SessionMemory extract(fire-and-forget)。

    Plan B Final Phase Step 2 (2026-07-02):从 v1 run() L1776-L1821 拆出。
    走 sm_layer 后台 ThreadPoolExecutor,不阻塞主对话(零延迟)。
    双重 gate (M11.7 dual-gate):token Δ ≥ 5K AND tool Δ ≥ 3 不通过则完全跳过
    extract_incremental(对齐 v1 run() L1788-L1795 行为)。

    Gate 条件(同 v1 L1784-L1795,全部满足才 extract):
      1. session_memory 不为 None
      2. _run_state.turn > 0(初始 turn 不抽)
      3. _run_state.final_answer truthy(没拿到完整回答就不抽)
      4. _run_state.final_stop_reason not in _TRUNCATED_STOP_REASONS
         (避免把 max_tokens/length 截断的半句话存成记忆)

    位置:output_chain 第 5 位(MemoryBridgeExtract 之后,SessionFlush 之前)
      - 在 MBE 之后:final_answer / final_stop_reason 已确定(MBE 读它们)
      - 在 SessionFlush 之前:SessionFlushHandler 仍是末位, flush 兜底不被破坏

    对应 v1 写入点:
      v1 run() L1776-L1821(L3 SM extract trigger 段)

    异常处理:extract_incremental 抛异常时 _logger.warning, 不 crash
      chain — SessionFlushHandler 继续跑兜底 flush。Future 抛异常
      在后台线程,不污染 agent 主循环。
    """
    name = "l3_sm_extract_trigger"

    def __init__(self, agent):
        self._agent = agent

    def handle(self, ctx: TurnContext) -> HandlerResult:
        agent = self._agent
        if agent is None:
            return HandlerResult()
        sm = getattr(agent, "session_memory", None)
        run_state = getattr(agent, "_run_state", None)
        if sm is None or run_state is None:
            return HandlerResult()
        # Gate 条件 2: turn > 0
        if run_state.turn <= 0:
            return HandlerResult()
        # Gate 条件 3: final_answer truthy
        if not run_state.final_answer:
            return HandlerResult()
        # Gate 条件 4: 截断 stop_reason → 跳过(避免存半句话)
        if run_state.final_stop_reason in _TRUNCATED_STOP_REASONS:
            return HandlerResult()

        try:
            msgs_with_id = messages_with_ids(agent.messages)
            current_tokens = (
                run_state.last_input_tokens + run_state.last_output_tokens
            )
            tool_delta = len(run_state.last_tool_calls)
            # Dual-gate(M11.7):sm_layer 内部 token+tool 节流判断
            gate_ok = sm.should_extract_now(
                current_token_count=current_tokens,
                tool_count_delta=tool_delta,
                tool_count_last_turn=tool_delta,
            )
            if not gate_ok:
                _logger.debug(
                    f"[L3 SM extract trigger] gate 拦住,跳过 extract | "
                    f"current_tokens={current_tokens} tool_delta={tool_delta}"
                )
                return HandlerResult()
            _logger.debug(
                f"[L3 SM extract trigger] 触发 extract_incremental | "
                f"msgs={len(msgs_with_id)} last_turn={run_state.turn} | "
                f"current_tokens={current_tokens} tool_delta={tool_delta}"
            )
            # Fire-and-forget:extract_incremental 内部起后台线程,这里不 .result()
            # block。future 写到 run_state.pending_sm_extract_future,供测试或
            # 后续 block 等待(v1 L1817 等价语义)。
            future = sm.extract_incremental(
                msgs_with_id,
                llm_callback=None,
                current_token_count=current_tokens,
                tool_count_delta=tool_delta,
                tool_count_last_turn=tool_delta,
            )
            run_state.pending_sm_extract_future = future
        except Exception as e:
            _logger.warning(
                f"[L3 SM extract trigger] 失败(不影响主流程): {e}"
            )
        return HandlerResult()