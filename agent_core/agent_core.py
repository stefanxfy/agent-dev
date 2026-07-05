"""
ReAct Agent — 手写 ReAct 循环（支持工具调用 + 流式输出）
Anthropic 显式风格：Thought → Action → Observation → Thought → Final Answer

Day 3 改进：
- History 管理（Token 预算截断）
- 并行工具调用（ThreadPoolExecutor）
- 错误处理完善（网络超时、API 限流）
- Debug 日志输出（便于观察 ReAct 过程）

Day 4 改进：
- SessionManager 融合：可选 session_id 实现 messages 持久化
- 自动从 session 加载 messages（Resume语义）
- 每次交互后实时写入 session（逐条写入，非全量重写）
- 保持向后兼容（不传 session_id = 纯内存模式）

Day 6 改进（对齐 Claude Code）：
- self.history → self.messages（对齐 Claude Code messages: Message[]）
- 压缩成功后持久化 boundary + summary 到 JSONL
- 删除 _save_to_session()（消除双写根因）
- load_history() → load_messages()
"""

from __future__ import annotations

import concurrent.futures
import json
import logging
import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, Iterator, List, Optional, TYPE_CHECKING
from typing_extensions import deprecated  # Plan B Final Phase Step 2 (2026-07-02):run() shim 装饰器

from .llm.router import (
    LLMRouter,
    StreamChunk,
    TextDelta,
    ThinkingDelta,
    ToolCallDelta,
    UsageStats,
)
from .tools.base import ToolRegistry

if TYPE_CHECKING:
    from .tools.permission_engine import PermissionEngine

# Day 5: 上下文管理器
from .context.manager import ContextManager as CM

# ── Debug 日志配置 ───────────────────────────────────────────────

# 创建 logger（使用单例模式防止重复配置）
_logger = logging.getLogger("react_agent")

# Version banner(2026-06-30): 让 streamlit 启动时能验证 agent_core 是否真加载了最新代码。
# 关键:Python 不会在 import 时 print 模块内部符号(我之前让你 grep 是不对的),
# 这个 print 放最外层,每次进程启动 / 重新 import 必打。PID + mtime 让你确认。
import os as _os_v, sys as _sys_v, time as _time_v
print(
    f"[agent_core v2026-06-30-AWAITING-FIX loaded] pid={_os_v.getpid()} "
    f"mtime={_os_v.path.getmtime(__file__):.0f} now={_time_v.time():.0f}",
    file=_sys_v.stderr, flush=True,
)

# 🛡️ permission 子系统 logger — 与 AGENT_LOG_PERMISSION env 联动
permission_logger = logging.getLogger("agent_core.permission")

# Bug 1e:表示「回答被截断、未完整收尾」的终止原因(跨 provider)。
# Anthropic 用 max_tokens,OpenAI 兼容用 length。命中这些 → 本轮回答不完整,
# 不提取记忆(避免把半句话存成记忆)。未知/None 的 stop_reason 不拦(向后兼容)。
_TRUNCATED_STOP_REASONS = {"max_tokens", "length"}

# 用户拒绝权限时,回写给 LLM 的 tool_result 内容(方案 1, 2026-06-30)。
# 强化措辞:明确告诉 LLM 不要换命令/变体重试,否则部分模型(MiniMax 等)会
# 收到 "Permission denied by user" 后换 touch 路径/加 echo 重试 → 反复触发权限弹窗。
# 被 resume_after_permission 用(deny 路径回写 tool_result 给 LLM 看)。
_PERMISSION_DENIED_BY_USER_MSG = (
    "Permission denied by user. Do NOT retry this tool or any similar/variant "
    "command (e.g. different path, added echo, chained ops). Acknowledge the "
    "denial to the user and ask how they would like to proceed."
)

# 日志统一走 root handler（由 web/app.py basicConfig 配置）
# agent_core 不再自建 handler，避免 propagate 导致重复输出
_logger.setLevel(logging.DEBUG)  # 自己放开 DEBUG，由 root handler 的 level 控制是否输出


# M11: TRUSTING_RECALL_SECTION 独立 H2 段
# 借鉴 Claude Code: 提醒 LLM 在依据记忆做推荐前,先验证文件/函数/flag 是否仍存在。
# 记忆是过去的快照,不能假定当下仍为真。
TRUSTING_RECALL_SECTION = """
## Before recommending from memory

A memory that names a specific function, file, or flag is a claim that it existed *when the memory was written*. It may have been renamed, removed, or never merged. Before recommending it:
- If the memory names a file path: check the file exists.
- If the memory names a function or flag: grep for it.
- If the user is about to act on your recommendation (not just asking about history), verify first.

"The memory says X exists" is not the same as "X exists now."
"""

# Day 3 改进：工具结果最大长度（防止 Token 爆炸）
MAX_TOOL_RESULT_LENGTH = 2000  # 最多 2000 字符


def _make_tool_result_block(tool_use_id: str, content: str) -> dict:
    """构造 Anthropic 格式的 tool_result message，自动截断超长内容"""
    truncated_content = content
    truncated = False
    if len(content) > MAX_TOOL_RESULT_LENGTH:
        truncated_content = content[:MAX_TOOL_RESULT_LENGTH]
        truncated = True
    
    block = {
        "role": "user",  # Anthropic 要求 tool_result 放在 user message 里
        "content": [
            {
                "type": "tool_result",
                "tool_use_id": tool_use_id,
                "content": truncated_content,
            }
        ],
    }
    
    # 如果截断了，添加提示（作为额外的 user 消息）
    if truncated:
        block["content"].append({
            "type": "text",
            "text": f"\n[结果过长，已截断至 {MAX_TOOL_RESULT_LENGTH} 字符]",
        })
    
    return block

# ── ReAct Agent ─────────────────────────────────────────────────────────

class ReactAgent:
    """
    ReAct 循环 Agent（Anthropic 显式风格）。

    循环：
      User → LLM → Thought (text) → Action (tool_use) →
      Tool Result → LLM → ... → Final Answer (text, stop)
    
    Day 4: 支持 SessionManager 融合，实现历史持久化。
    - 传入 session_id → 自动从 session 加载历史，每次交互后保存
    - 不传 session_id → 纯内存模式（向后兼容）
    """

    def __init__(
        self,
        llm_router: LLMRouter,
        tool_registry: ToolRegistry,
        max_turns: int = 10,
        max_context_tokens: int = 100_000,  # Day 3: Token 预算（已被 ContextManager 替代，保留向后兼容）
        session_id: Optional[str] = None,   # Day 4: 会话 ID（可选）
        session_data_dir: Optional[str] = None,  # Day 4: session 数据目录
        memory_retriever: Optional["MemoryRetriever"] = None,  # M7 ported: 记忆检索
        memory_store: Optional["MemoryStore"] = None,           # M7 ported: 库内计数
        react_memory_bridge: Optional["ReactMemoryBridge"] = None,  # Task 7: 双通道记忆桥接器(取代 Option C)
        session_memory: Optional["SessionMemoryLayer"] = None,  # M10 C2.1: L3 SM 快路径
        memory_config: Optional["MemoryConfig"] = None,  # M10 C6.4: 运行时切换 hook(set_runtime 用)
        permission_engine: Optional["PermissionEngine"] = None,  # M12: 权限引擎(可选,None=不启用)
        audit_logger: Optional[Any] = None,  # M12: 审计日志(可选,None=不写)
        auto_allow_ask: bool = True,  # M12: ASK 时是否自动 ALLOW(测试用,UI 路径会 yield 等待)
    ):
        self.llm = llm_router
        self.tools = tool_registry
        self.max_turns = max_turns
        self.max_context_tokens = max_context_tokens  # 保留向后兼容
        self.messages: list[dict] = []  # 当前对话消息列表（对齐 Claude Code messages: Message[]）

        # Day 5: ContextManager（替代 _trim_messages）
        self.context_manager = CM(
            llm_router=llm_router,
            model=getattr(llm_router.config, 'model', 'glm-4'),
        )

        # P2 新增：从 LLMConfig 读取 system_prompt
        self.system_prompt = self.llm.config.system_prompt

        # M7 ported: 记忆系统 hooks(若注入,则每次 LLM 调用前检索 + 推送 memory_status)
        self.memory_retriever = memory_retriever
        self.memory_store = memory_store
        # Task 7: 双通道记忆桥接器(取代 Option C,run() 末尾调 bridge.on_turn_end)
        self.react_memory_bridge = react_memory_bridge
        # M10 C2.1: L3 SM 快路径(可选注入,None 时走 ContextManager 传统路径)
        self.session_memory = session_memory
        # M10 C6.4: 运行时配置切换 hook — UI expander 用 set_runtime 改字段不重建 agent
        self.memory_config = memory_config  # type: ignore[assignment]
        # M11: MEMORY.md 物理索引(L1 启动加载 + 写盘后异步 rebuild)
        self.memory_index = None
        if memory_store is not None:
            try:
                from agent_core.memory.memory_index import MemoryIndex
                self.memory_index = MemoryIndex(memory_store.root)
                self.memory_index.rebuild()  # lazy rebuild 兜底
            except Exception as e:
                _logger.warning(f"MEMORY.md lazy rebuild 失败: {e}")
        # M11: 已展示过的记忆 rel_path 集合(用于 sideQuery 去重)
        self._surfaced_memories: set[str] = set()
        # ── Day 4: SessionManager 融合 ──────────────────────────────
        self._session_manager: Optional["SessionManager"] = None
        if session_id:
            from .session.manager import SessionManager
            self._session_manager = SessionManager(
                session_id=session_id,
                data_dir=session_data_dir,
            )
            # 从 session 加载历史（Resume 语义：只加载断链后的消息）
            self.messages = self._session_manager.get_messages_for_llm()
            _logger.info(f"Session loaded: {session_id}, {len(self.messages)} messages")

        # ── 流式过程中记录 thinking/tool_logs，用于 session 持久化 ───
        self._pending_thinking: str = ""
        self._pending_tool_logs: list = []
        self._pending_tool_results: list = []  # [(tool_use_id, output), ...]
        # Day 7 改进：记录本轮 LLM 返回的 usage 统计，用于持久化到 jsonl
        # 解决 F5 刷新后 baseline 从 API 真实值（33,345）跳变到字面估算（58,406）的 bug
        self._last_turn_usage: Optional[UsageStats] = None

        # Day 7 改进：从 session 历史最后一条带 usage 的 entry 恢复 baseline
        # 优先级：API 真实数字 > 字面估算。F5 刷新后仍能保持 33,345 而不是 58,406。
        self._restore_usage_baseline()

        # M10 C3.1: DistillationLoop 注入位(由 web/app.py:get_agent() 挂上)
        self._distillation_loop: Optional["DistillationLoop"] = None

        # M12: 权限引擎 + 审计日志(可选,None=不启用权限系统,向后兼容)
        self.permission_engine = permission_engine
        self.audit_logger = audit_logger
        self.auto_allow_ask = auto_allow_ask
        # M12: 权限决策待审批请求(给 UI 用)
        self._pending_permission_request: Optional[dict] = None
        self._permission_resolved: Optional[Any] = None  # threading.Event 初始为 None

        # Plan B SRP 接线修复(2026-07-03):实例化 SystemPromptAssembler 并重建
        # system_prompt = base + sandbox section + MEMORY.md + TRUSTING_RECALL。
        # 此前 Plan B 拆分写好了 assembler 类、把 SystemPromptHandler 改成依赖
        # agent._assembler,但 __init__ 漏了实例化 → handler 恒因 _assembler is None
        # 早退 → ctx.stage_inputs 留 None → MemoryRetrievalHandler 把 None 传给
        # _merge_memory_into_system 炸 'NoneType' object is not iterable。
        # 必须在 system_prompt(L180 base)+ memory_index + permission_engine 都赋值后调。
        from agent_core.turn_chain import SystemPromptAssembler
        self._assembler = SystemPromptAssembler(self)
        self.system_prompt = self._assembler.build()

        # 会话级计数器(2026-07-03):tool/token 的中立宿主,累加在"资源发生点"
        # (tool → ToolExecuteHandler,token → LLM handler),消费者(extraction /
        # compaction)读 since(metric, consumer) 触发、mark 推进各自水位。
        # 详见 agent_core/session_counter.py。不依赖 bridge(计数下沉意图),
        # 必须在 _tool_chain 构造前就绪(handler 通过 self.session_counter 访问)。
        from agent_core.session_counter import SessionCounter
        self.session_counter = SessionCounter()

        # ── v2 状态机集成(B 块 B5)────────────────────────────────────
        # 注:B7 才会把 run() 改用 state machine。当前 state machine 构造完成
        # 但 run() 仍用 v1 逻辑;state machine 是 dormant 基础设施(可通过新
        # 的 start_run/step API 显式使用)。
        from agent_core.agent_state import (
            AgentPhase, StateMachine,
            build_default_termination,
            SetupPhase, LLMThinkingPhase, AwaitingPermissionPhase,
            ExecutingToolsPhase, FinalizingPhase, InterruptedPhase, DonePhase,
            RunState as _RunState,
        )
        from agent_core.builder import (
            build_default_inputs_chain,
            build_default_llm_chain,
            build_default_tool_chain,
            build_default_output_chain,
        )

        # Plan A: 4 chains(由 builder.py factory 统一构造,D6-1 抽离)。
        # 每个 chain 由「主 handler」一手包办(stop_chain=True),其它 handler 留作扩展点。
        # 行为与重构前一致(同 11 handler 同顺序);只是构造位置从内联变 factory。
        self._inputs_chain = build_default_inputs_chain(self)
        self._llm_chain = build_default_llm_chain(self)
        self._tool_chain = build_default_tool_chain(self)
        self._output_chain = build_default_output_chain(self)

        # 7 个 phase(6 + INTERRUPTED 终态)
        self._phases = {
            AgentPhase.SETUP:               SetupPhase(self._inputs_chain),
            AgentPhase.LLM_THINKING:        LLMThinkingPhase(self._llm_chain),
            AgentPhase.AWAITING_PERMISSION: AwaitingPermissionPhase(),
            AgentPhase.EXECUTING_TOOLS:     ExecutingToolsPhase(self._tool_chain),
            AgentPhase.FINALIZING:          FinalizingPhase(self._output_chain),
            AgentPhase.INTERRUPTED:         InterruptedPhase(),
            AgentPhase.DONE:                DonePhase(),
        }
        # A1-H2 (review 修复): 用 build_default_termination factory,新加的
        # TimeoutTermination / 后续 TokenBudgetTermination 等扩展点都
        # 在 factory 集中,ReactAgent 不必每次手写 CompositeTermination。
        self._termination = build_default_termination(
            max_turns=max_turns,
            timeout_s=None,  # 默认不启用,显式需要时再开
        )
        self._sm = StateMachine(
            self._phases,
            initial=AgentPhase.SETUP,
            termination=self._termination,
        )

        # Per-run state(每次 start_run 重置)
        self._run_state: Optional[_RunState] = None

    def _restore_usage_baseline(self):
        """
        从 jsonl 历史最后一条带 usage 的 entry 恢复 context_manager baseline。

        为什么需要：
        - F5 刷新后，agent 重建会重新走 _estimate_used_tokens 字面估算路径
        - 字面估算不准（灌水内容 SimpleTokenCounter 高估 ~43%）
        - 如果 jsonl 最后一条 entry 里有 usage 字段（API 返回的 input_tokens），
          用真实数字作 baseline，0 跳变。

        复杂度：O(1) read_tail(64KB) 快路径 + O(n) read_entries() 兑底。
        - 99% 场景：read_tail 一次搞定（O(1)）
        - 1% 场景（灌水 entry > 64KB 在结尾）：fallback 到全量扫（O(n)）

        兼容性：
        - 老 jsonl（没 usage 字段）：entry.get("usage") 返回 None，fallback 到原路径
        - 新 jsonl（有 usage 字段）：用 API 真实数字
        - 天然平滑升级，无需迁移脚本
        """
        if not self.context_manager or not self._session_manager:
            return

        try:
            from .session.storage import SessionStorage
            storage = self._session_manager.storage

            # 快路径：O(1) tail 64KB 窗口
            try:
                tail_entries = storage.read_tail(kb=64)
            except Exception:
                tail_entries = []

            for entry in reversed(tail_entries):
                usage = entry.get("usage")
                if usage and usage.get("input_tokens"):
                    # entry 是最后一条 assistant，它的 input_tokens 不含自己
                    # 刷新后 self.messages 包含这条，所以 baseline_msg_count = len - 1
                    msg_count = len(self.messages) - 1
                    self.context_manager.set_baseline(
                        usage["input_tokens"],
                        msg_count,
                    )
                    _logger.debug(
                        f"🔄 [Restore O(1)] baseline={usage['input_tokens']:,} "
                        f"msg_count={msg_count} "
                        f"from tail (uuid={entry.get('uuid', '?')[:8]})"
                    )
                    return

            # Fallback：tail 没找到（灌水 entry > 64KB 或老 jsonl 无 usage）
            # 降级到全量扫，极少触发
            all_entries = storage.read_entries(include_compact_boundary=True)
            for entry in reversed(all_entries):
                usage = entry.get("usage")
                if usage and usage.get("input_tokens"):
                    msg_count = len(self.messages) - 1
                    self.context_manager.set_baseline(
                        usage["input_tokens"],
                        msg_count,
                    )
                    _logger.debug(
                        f"🔄 [Restore fallback] baseline={usage['input_tokens']:,} "
                        f"msg_count={msg_count} "
                        f"from full scan (uuid={entry.get('uuid', '?')[:8]})"
                    )
                    return

            _logger.debug("🔄 [Restore] no usage in history, baseline stays 0")
        except Exception as e:
            _logger.debug(f"🔄 [Restore] failed (silent fallback): {e}")

    # ── 权限系统 helper(M12 增量)───────────────────────────────

    def _run_permission_request_hook(
        self,
        tool_name: str,
        tool_input: dict,
    ) -> Optional[str]:
        """
        跑 PermissionRequest hook(M3 Task 2,对齐 doc §4.4)

        Returns:
            hook 决策("allow"/"deny")或 None(未决策 → 走 UI)
            异常时返 None(走默认 UI,不阻断)
        """
        if self.permission_engine is None:
            return None
        hook_registry = getattr(self.permission_engine, "hook_registry", None)
        if hook_registry is None:
            return None
        try:
            req_result = hook_registry.run_permission_request(
                tool_name, tool_input, self.permission_engine.context,
            )
            if getattr(req_result, "has_decision", False):
                return req_result.decision
        except Exception as e:
            _logger.warning("PermissionRequest hook 异常,走默认 UI: %s", e)
        return None

    def _ask_user_permission_v2(
        self,
        tool_name: str,
        tool_input: dict,
        decision: Any,
    ) -> str:
        """
        v2 非阻塞权限审批(详见设计文档 §9.1)。

        Returns:
            sentinel:
              - "ALLOW"             → 允许执行
              - "DENY_BY_HOOK"      → PermissionRequest hook 拒绝
              - "AWAITING_PERMISSION" → 等 UI 决定(主线程不阻塞)
        """
        permission_logger.info(
            "🛡️ [ask_user_permission_entry] tool=%s reason=%s",
            tool_name,
            getattr(decision.decision_reason, "reason", "") if decision.decision_reason else "",
        )

        # M3 Task 2: 先跑 PermissionRequest hook(后台 agent / webhook 外部决策)
        hook_decision = self._run_permission_request_hook(tool_name, tool_input)
        if hook_decision == "allow":
            permission_logger.info(
                "🛡️ [ask_hook_allow] tool=%s via PermissionRequest hook",
                tool_name,
            )
            return "ALLOW"
        if hook_decision == "deny":
            permission_logger.info(
                "🛡️ [ask_hook_deny] tool=%s via PermissionRequest hook",
                tool_name,
            )
            return "DENY_BY_HOOK"

        # hook 未决策 → 设 pending request + 返 AWAITING_PERMISSION(主线程不阻塞)
        permission_logger.debug("🛡️ [ask_hook_passthrough] tool=%s → AWAITING_PERMISSION", tool_name)

        if self._permission_resolved is None:
            self._permission_resolved = threading.Event()

        self._pending_permission_request = {
            "tool_name": tool_name,
            "tool_input": tool_input,
            "reason": getattr(decision.decision_reason, "reason", "") if decision.decision_reason else "",
            "message": decision.message or "",
        }

        return "AWAITING_PERMISSION"

    def resolve_permission(self, choice: str) -> None:
        """
        外部(UI)在用户选择后调这个,解锁 _ask_user_permission_v2

        Args:
            choice: "allow" | "deny" | "always_allow"
        """
        if self._pending_permission_request is not None:
            self._pending_permission_request["choice"] = choice
        if self._permission_resolved is not None:
            self._permission_resolved.set()

    # 注(2026-07-02):_iter_phase_finalize 已删除,3 真业务段已拆为 output_chain
    # 4 handler(FinalAnswerBookkeepingHandler / FinalAnswerPersistHandler /
    # MemoryBridgeExtractHandler / SessionFlushHandler)。FINALIZING phase 由
    # FinalizingPhase.enter() 直接调 agent._output_chain.run(ctx.turn_ctx)。

    # ── v2 状态机 API(B 块 B5)────────────────────────────────────
    # 注:这 4 个方法是 v2 重构暴露的新 API。当前 run() 仍用 v1 逻辑,
    # 这些方法是 dormant 基础设施,供后续 web/app.py C8 + 未来 handler
    # 实施时使用。不破坏现有任何行为。

    def _log_sm_transition(self, old_phase, trigger: str, new_phase) -> None:
        """SM on_exit hook:记录每次 phase 转换(诊断 phase 卡死 / Stop 按钮问题)。

        注册点:start_run 里 SM 重建后。SM 每次 start_run 新建,所以 hook
        必须每次重新注册。设计文档 §3 规定 on_enter/on_exit hook 专供 logging,
        不污染 phase 业务代码。

        注意:on_exit hook 在 SM 更新 self._phase 之前调用,所以 is_done 必须
        基于 new_phase 判断(不能用 self._sm.is_done,那读的是 old phase)。
        """
        new_val = getattr(new_phase, "value", new_phase)
        # interrupted/done 都是终态
        is_terminal = new_val in ("done", "interrupted")
        _logger.debug(
            "🔄 [SM transition] %s --[%s]--> %s | is_terminal=%s",
            getattr(old_phase, "value", old_phase),
            trigger,
            new_val,
            is_terminal,
        )

    def start_run(self, user_input: str) -> None:
        """
        Args:
            user_input: 用户消息
        """
        from agent_core.agent_state import (
            AgentPhase, RunState as _RunState, StateMachine,
        )

        # 1. 复用 v1 初始化逻辑
        self.messages.append({"role": "user", "content": user_input})
        if self._session_manager:
            try:
                self._session_manager.add_user_message(user_input)
            except Exception as e:
                _logger.warning(f"Failed to save user message to session: {e}")

        # 2. 重置 per-run 累积状态(v1 字段)
        self._pending_thinking = ""
        self._pending_tool_logs = []
        self._pending_tool_results = []

        # 3. 重置 _run_state(v2)— 关键:新建 cancel_event
        self._run_state = _RunState(user_message=user_input)

        # 4. 重建 StateMachine(每次 start_run 拿新的 SM 实例)
        # 这样确保上一次的 INTERRUPTED 终态不会污染下一次 run
        self._sm = StateMachine(
            self._phases,
            initial=AgentPhase.SETUP,
            termination=self._termination,
        )
        # 注册 phase 转换日志 hook(诊断 phase 卡死 / Stop 按钮问题)。
        # SM 每次 start_run 新建,hook 必须在此重新注册。
        self._sm.on_exit(self._log_sm_transition)

        _logger.debug("🚀 [v2 start_run] user_input=%r sm=%r", user_input[:80], self._sm)

    def _new_turn_ctx(self):
        """A1-H3 (review 修复): 每次推进新建 turn-level 状态。

        递增 self._run_state.turn 并返回新 TurnContext。
        """
        from agent_core.agent_state import TurnContext
        self._run_state.turn += 1
        return TurnContext(
            run_state=self._run_state,
            turn_number=self._run_state.turn,
        )

    def _drive(self, trigger_event: Optional[str] = None):
        """A1-H3 (review 修复): 推 state machine 直到暂停点 / 终止。

        trigger_event:
        - None → 用 phase 自己的"self-progress" trigger(自动检测)
        - 显式传入 → "run_started" / "permission_resolved" 等特殊事件
        """
        from agent_core.agent_state import PhaseContext

        if self._run_state is None:
            raise RuntimeError("start_run() must be called before _drive()")

        ctx = PhaseContext(
            run_state=self._run_state,
            turn_ctx=self._new_turn_ctx(),
            termination=self._termination,
            sm=self._sm,
        )

        # 决定 trigger 名:
        # - 显式传 → 用之(SETUP 首次进用 "run_started",resume 用 "permission_resolved")
        # - 当前 phase 已有 history(history 非空说明刚链式推进过)→ "start"(普通推进)
        # - 否则 → "start" 默认
        if trigger_event is None:
            trigger_event = "start"

        try:
            for ev in self._sm.trigger(trigger_event, ctx):
                # 检查 cancel_event:若被 interrupt() 设置了,提前停
                if self._run_state.cancel_event.is_set():
                    _logger.debug("⏹️ [v2 _drive] cancel_event set, 提前 stop")
                    break
                yield ev
        except StopIteration:
            pass

        # 结束状态(诊断 Stop 按钮卡死的关键:step 结束时 SM 是否真到 DONE/INTERRUPTED)。
        # 若 web 层 _run_phase 还停在 "running" 但此处 is_done=True → web 层清理 bug。
        _logger.debug(
            "✅ [v2 _drive] done: sm.current=%s is_done=%s is_interrupted=%s history_len=%d",
            self._sm.current.value, self._sm.is_done,
            self._sm.is_interrupted, len(self._sm.history),
        )

    def step(self):
        """v2 API:推进一次触发,generator 式 yield events。

        A1-H3 (review 修复): step() 改走 _drive() helper,通过 trigger 名字
        路由 SETUP 首次进(run_started)/ resume(permission_resolved)/ 普通推进(start)。

        语义:
        - 首次调用且 SM 在 SETUP → trigger "run_started"
        - SM 在 AWAITING_PERMISSION(刚 resume) → trigger "permission_resolved"
        - 其他 → trigger "start"

        若遇 AWAITING_PERMISSION → 自动暂停(yield awaiting_permission event 后 return)。
        若遇 DONE / INTERRUPTED → 终止(yield 终止 event 后 return)。
        若遇 max_turns → 转 DONE。

        Yields:
            Event tuple,例 ("text", "...")/ ("tool_call", {...})/ ("awaiting_permission", req)
        """
        if self._run_state is None:
            raise RuntimeError("start_run() must be called before step()")

        # A1-H3: 根据 SM 当前 phase 选 trigger 名字
        from agent_core.agent_state import AgentPhase
        trigger_event = "start"  # 默认
        if (
            self._sm.current == AgentPhase.SETUP
            and not self._sm.history
        ):
            trigger_event = "run_started"
        elif self._sm.current == AgentPhase.AWAITING_PERMISSION:
            trigger_event = "permission_resolved"

        _logger.debug(
            "▶️ [v2 step] trigger=%s sm.current=%s history_len=%d",
            trigger_event, self._sm.current.value, len(self._sm.history),
        )
        yield from self._drive(trigger_event=trigger_event)

    def resume_after_permission(self, choice: str) -> None:
        """v2 API:UI 在 permission 弹窗点 Allow/Deny 后调这个。

        行为:
        1. 把 choice 写进 _pending_permission_request(供 resume 路径读)
        2. set _permission_resolved Event(供 legacy 路径)
        3. 重新 trigger 一次 phase 让 state machine 知道 permission 已 resolved

        Deny-loop fix (2026-06-30): 如果 choice=="deny",在 transition 前先把
        tool_result "Permission denied by user" append 到 self.messages。
        否则下次 LLM 调用看不到 denial,会重新生成相同的 tool_call 死循环。

        Args:
            choice: "allow" | "deny" | "always_allow"
        """
        # 诊断(2026-07-03):确认 resume 是否被调 + 入口 SM 状态。用 WARNING 级
        # 确保在主 logger(agent_core)也显示——主 logger DEBUG 没开,INFO 可能也不显示。
        _logger.warning(
            "▶️ [resume entry] choice=%s sm=%s pending=%s",
            choice,
            self._sm.current.value if self._sm else None,
            self._pending_permission_request is not None,
        )
        # 1. 复用 resolve_permission 逻辑(legacy 路径仍可工作)
        self.resolve_permission(choice)

        # Deny-loop fix (2026-06-30): deny 路径补 tool_result 给 LLM 看
        # 注意:必须先 append,再 transition — 否则下次 LLM 调用看不到 denial
        if choice == "deny" and self._pending_permission_request is not None:
            tool_use_id = self._pending_permission_request.get("tool_use_id")
            if tool_use_id:
                denial_msg = _PERMISSION_DENIED_BY_USER_MSG
                self.messages.append(
                    _make_tool_result_block(tool_use_id, denial_msg)
                )
                # 同时记到 _pending_tool_results(供 output_chain ToolPairPersistHandler (Stage B) 持久化)
                self._pending_tool_results.append(
                    (tool_use_id, denial_msg)
                )
                # 同步持久化到 session(jsonl trace 友好)— finalize 不会重复写
                # 因为下次 LLM 响应大概率不含 tool_call(stage_out.tool_calls=[])
                if self._session_manager:
                    try:
                        self._session_manager.add_tool_results([
                            {"tool_use_id": tool_use_id, "content": denial_msg}
                        ])
                    except Exception as e:
                        _logger.warning(
                            f"Failed to persist denial tool_result to session: {e}"
                        )

        # 2. v2 推进:从 AWAITING_PERMISSION 转下一 phase
        # 注意:不能直接调 trigger() — 会驱动 SM 链式推到 DONE。
        # 只调 AwaitingPermission.next() 决策下一 phase,然后手动 transition。
        # 注意:不能直接调 trigger() — 会驱动 SM 链式推到 DONE。
        # 只调 AwaitingPermission.next() 决策下一 phase,然后手动 transition。
        from agent_core.agent_state import AgentPhase

        # Fix B (2026-06-30) 防御性补:如果 SM 当前不在 AWAITING_PERMISSION,
        # 但 _run_state.awaiting_permission 已设(说明 _iter_phase_tools 写了 pending),
        # 说明之前 yield generator 被提前销毁,SM 没正确转入 AWAITING_PERMISSION。
        # 这里强制补一次转换,让下面的主 if 块能进。
        # 通常这种情况不应该发生(Fix A 已在 _iter_phase_tools 同步转过),
        # 但作为兜底,防止未来再有类似 yield-暂停 bug 把用户卡死。
        if (
            self._sm.current != AgentPhase.AWAITING_PERMISSION
            and self._run_state is not None
            and self._run_state.awaiting_permission is not None
        ):
            _old_phase_recovery = self._sm.current
            self._sm._phase = AgentPhase.AWAITING_PERMISSION
            self._sm._history.append((
                _old_phase_recovery,
                "permission_needed(recovered)",
                AgentPhase.AWAITING_PERMISSION,
            ))
            _logger.warning(
                "⚠️ [SM recovery] %s --> awaiting_permission (resume_after_permission 兜底;通常 Fix A 已处理)",
                _old_phase_recovery.value,
            )

        if self._sm.current == AgentPhase.AWAITING_PERMISSION:
            # Deny-loop fix (2026-06-30): choice=="deny" 时直接转 LLM_THINKING,
            # 不重跑 EXECUTING_TOOLS(否则同一 tool_call 又会触发 ASK → 死循环)。
            # LLM 看到刚 append 的 tool_result "Permission denied by user",
            # 会用文本回复用户(而不是重新生成 mktemp)。
            if choice == "deny":
                self._sm._phase = AgentPhase.LLM_THINKING
                self._sm._history.append((
                    AgentPhase.AWAITING_PERMISSION,
                    "permission_resolved(deny)",
                    AgentPhase.LLM_THINKING,
                ))
                _logger.debug(
                    "▶️ [v2 resume_after_permission] choice=deny → phase=LLM_THINKING (skip tool re-exec)"
                )
                return
            # allow / always_allow → 正常走 EXECUTING_TOOLS 重跑 tool 执行
            current_phase = self._sm._phases[self._sm.current]
            next_trigger, next_phase = current_phase.next(
                "permission_resolved",
                # 临时 PhaseContext — next() 只读它的字段,不需要完整构造
                type("_StubCtx", (), {"turn_ctx": type("_StubTC", (), {
                    "events": [], "is_stopped": False, "emit": lambda self, e: None,
                    "stop": lambda self: None,
                    "permission_request": None, "stage_outputs": None, "stage_inputs": None,
                })()})(),
            )
            # 手动 transition(不调 trigger — 避免链式推到 DONE)
            self._sm._phase = next_phase
            self._sm._history.append((AgentPhase.AWAITING_PERMISSION, "permission_resolved", next_phase))
            _logger.debug(
                "▶️ [v2 resume_after_permission] choice=%s → phase=%s",
                choice, self._sm.current,
            )

    def interrupt(self) -> None:
        """v2 API:用户主动中断(Stop 按钮 / Esc)。

        行为:
        1. set cancel_event(让正在跑的 handler 下一轮迭代检查到,自然停)
        2. 调 StateMachine.interrupt() 转 INTERRUPTED 终态

        幂等:已 INTERRUPTED 时不重复处理。
        """
        if self._run_state is None:
            _logger.warning("⚠️ [v2 interrupt] no active run, 忽略")
            return
        if self._sm.is_interrupted:
            _logger.debug("⏹️ [v2 interrupt] already interrupted, 忽略")
            return

        from agent_core.agent_state import PhaseContext, TurnContext

        # 1. 通知正在运行的 handler 立即停止
        self._run_state.cancel_event.set()

        # 2. 转 INTERRUPTED 终态
        turn_ctx = TurnContext(run_state=self._run_state)
        phase_ctx = PhaseContext(
            run_state=self._run_state,
            turn_ctx=turn_ctx,
            sm=self._sm,
        )
        # interrupt() 也是 generator,consume 掉产出的 events
        for _ in self._sm.interrupt(phase_ctx):
            pass

        _logger.info("⏹️ [v2 interrupt] user cancelled, phase=%s", self._sm.current)

    def close(self):
        """关闭当前会话，刷新缓冲到磁盘。

        在切换会话或销毁 Agent 前显式调用。
        """
        # M10 C3.1: 停蒸馏 loop(若有)
        if getattr(self, "_distillation_loop", None) is not None:
            try:
                self._distillation_loop.stop(timeout=5.0)
            except Exception as e:
                _logger.warning(f"DistillationLoop.stop 失败: {e}")
            self._distillation_loop = None

        if self._session_manager:
            try:
                self._session_manager.close()
            except Exception as e:
                _logger.warning(f"Agent.close() failed: {e}")

    def reset(self):
        """重置会话历史"""
        self.messages.clear()
        # Day 4: 同时清空 session
        if self._session_manager:
            try:
                self._session_manager.clear()
            except Exception:
                pass

    def load_messages(self, history: list[dict]):
        self.messages = list(history)

    # ── Day 4: Session 相关 ───────────────────────────────────────────

    @property
    def session_id(self) -> Optional[str]:
        """获取当前 session_id（如果有）"""
        return self._session_manager.session_id if self._session_manager else None

    def fork(self, new_session_id: Optional[str] = None) -> Optional[str]:
        """
        Fork 当前 session 到新 session。
        返回新 session_id。
        如果未启用 session，返回 None。
        """
        if self._session_manager is None:
            return None
        return self._session_manager.fork(new_session_id)

    def add_compact_boundary(self, **kwargs):
        """在当前会话中添加压缩边界标记（委托给 SessionManager）"""
        if self._session_manager:
            self._session_manager.add_compact_boundary(**kwargs)

    def get_session_manager(self) -> Optional["SessionManager"]:
        """获取 SessionManager 实例（用于高级操作）"""
        return self._session_manager
