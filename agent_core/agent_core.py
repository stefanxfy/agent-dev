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

# M10 C2.1: L3 SessionMemoryLayer 快路径(字符串前向引用避免循环 import)
from .memory.sm_layer import TurnContext as _TurnContext  # noqa: E402,F401
try:
    from .memory.sm_layer import SessionMemoryLayer as _SessionMemoryLayer  # type: ignore # noqa: F401,E402
except ImportError:  # pragma: no cover
    _SessionMemoryLayer = None  # type: ignore[assignment]

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


# M10-debug (2026-06-30): debug 工具,序列化对象 + 超长截断(避免 LLM REQ/RESP 日志爆炸)。
# TODO-DEBUG: 用户确认信息足够后清理。
def _truncate_json_for_log(obj: Any, max_chars: int = 2000) -> str:
    """json.dumps(obj) 后超 max_chars 截断,加 '...[truncated N chars]' 标记。"""
    try:
        s = json.dumps(obj, ensure_ascii=False, default=str)
    except Exception as e:
        return f"<unserializable: {type(e).__name__}: {e}>"
    if len(s) <= max_chars:
        return s
    return s[:max_chars] + f"...[truncated {len(s) - max_chars} chars]"

# 🛡️ permission 子系统 logger — 与 AGENT_LOG_PERMISSION env 联动
permission_logger = logging.getLogger("agent_core.permission")

# Bug 1e:表示「回答被截断、未完整收尾」的终止原因(跨 provider)。
# Anthropic 用 max_tokens,OpenAI 兼容用 length。命中这些 → 本轮回答不完整,
# 不提取记忆(避免把半句话存成记忆)。未知/None 的 stop_reason 不拦(向后兼容)。
_TRUNCATED_STOP_REASONS = {"max_tokens", "length"}

# 用户拒绝权限时,回写给 LLM 的 tool_result 内容(方案 1, 2026-06-30)。
# 强化措辞:明确告诉 LLM 不要换命令/变体重试,否则部分模型(MiniMax 等)会
# 收到 "Permission denied by user" 后换 touch 路径/加 echo 重试 → 反复触发权限弹窗。
# 被 resume_after_permission(v2 路径)和 _ask_user_permission(legacy 路径)共用。
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


def _format_messages_for_log(messages: list) -> str:
    """格式化 messages 用于日志输出（JSON 美化）"""
    try:
        # 简化输出，只保留关键字段
        simplified = []
        for m in messages:
            role = m.get("role", "unknown")
            content = m.get("content", "")
            
            if isinstance(content, list):
                # 处理多模态 content（如 tool_use/tool_result）
                content_str = json.dumps(content, ensure_ascii=False, indent=2)
                if len(content_str) > 500:
                    content_str = content_str[:500] + "... [truncated]"
            elif isinstance(content, str):
                content_str = content[:200] + "..." if len(content) > 200 else content
            else:
                content_str = str(content)[:200]
            
            simplified.append({"role": role, "content": content_str})
        
        return json.dumps(simplified, ensure_ascii=False, indent=2)
    except Exception:
        return str(messages)[:500]


def _format_tool_calls_for_log(tool_calls: list) -> str:
    """格式化 tool_calls 用于日志输出"""
    if not tool_calls:
        return "[]"
    simplified = []
    for tc in tool_calls:
        simplified.append({
            "name": tc.tool_name,
            "input": tc.tool_input,
            "id": tc.tool_use_id[:8] + "..." if len(tc.tool_use_id) > 8 else tc.tool_use_id
        })
    return json.dumps(simplified, ensure_ascii=False, indent=2)


# ── 工具调用结果（传给 LLM 的 tool_result）──────────────────────────

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


# ── Plan A: handler 共享的数据结构 ──────────────────────────────────────


@dataclass
class _LLMResult:
    """LLM phase 输出(handler 间共享)。"""
    tool_calls: list = field(default_factory=list)
    full_text: str = ""
    thinking_text: str = ""
    stop_reason: Optional[str] = None
    usage: Optional[Any] = None  # UsageStats


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

    def _cache_namespace(self) -> str:
        """LLM cache namespace(供 LLMCallHandler 用)。

        Returns:
            session_id 存在 → "agent/{session_id}",否则 "agent/default"
        """
        if self._session_manager is not None:
            try:
                sid = self._session_manager.session_id
                if sid:
                    return f"agent/{sid}"
            except Exception:
                pass
        return "agent/default"

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

    # ── M11 L1: MEMORY.md 启动加载 ────────────────────────────────

    def _build_system_prompt_with_memory(self) -> str:
        """M11 L1:启动加载 MEMORY.md + 拼到 system prompt(独立 H1 段)

        借鉴 Claude Code appendSystemPrompt:
        - base system prompt 在前
        - MEMORY.md 内容作为独立 H1 段追加(若有)
        - 末尾追加 TRUSTING_RECALL_SECTION H2 段(无论是否有 index 都加)
        - 加载失败 → base + TRUSTING_RECALL_SECTION,不抛

        M2 增量:若 permission_engine 的 context.sandbox_enabled,注入 sandbox 规则段
        (对齐 doc §5.4 BashTool/prompt.ts getSimpleSandboxSection)
        """
        base = self.system_prompt or ""

        # M2: 注入 sandbox 规则段(对齐 doc §5.4)
        sandbox_section = self._get_sandbox_prompt_section()
        if sandbox_section:
            base = base + "\n\n" + sandbox_section

        if self.memory_index is None:
            return base + "\n" + TRUSTING_RECALL_SECTION
        try:
            index_content = self.memory_index.load_index()
        except Exception as e:
            _logger.warning(f"MEMORY.md 加载失败,跳过: {e}")
            return base + "\n" + TRUSTING_RECALL_SECTION
        if not index_content:
            return base + "\n" + TRUSTING_RECALL_SECTION
        return f"{base}\n\n{index_content}\n\n{TRUSTING_RECALL_SECTION}"

    def _get_sandbox_prompt_section(self) -> str:
        """
        获取 sandbox 规则 prompt 段(对齐 doc §5.4)

        - permission_engine 未注入 → ""(向后兼容)
        - sandbox 未启用 → ""
        - 否则返 sandbox_prompt.get_sandbox_prompt_section()

        异常不抛(graceful)
        """
        if self.permission_engine is None:
            return ""
        try:
            from .tools.sandbox_prompt import get_sandbox_prompt_section
            # 确保沙箱已 load_config + 初始化(若 web/app.py 没注入,这里也不报错)
            return get_sandbox_prompt_section()
        except Exception as e:
            _logger.debug("sandbox prompt 注入失败,跳过: %s", e)
            return ""

    # ── M11: 检索 wiring(从 .env → memory_config → retriever.search) ──

    def _call_memory_retriever(self, query: str):
        """调 memory_retriever.search(),mode/top_k 从 self.memory_config 读。

        为什么独立成方法(2026-06-26 修复):
        - 之前直接 inline 在 run() 里 hardcode top_k=5、不传 mode,
          导致 .env 里 MEMORY_RETRIEVAL__MODE / __TOP_K 改了不生效
        - 抽成方法后单测可验证 wiring,且未来加 side_query 二次精选 hook 也有落点

        Args:
            query: 用户消息文本

        Returns:
            retriever.search() 返回值(通常是 RetrievalReport)
        """
        import logging
        logger = logging.getLogger(__name__)
        if self.memory_config is not None:
            mode = self.memory_config.retrieval.mode
            top_k = self.memory_config.retrieval.top_k
            cfg_min_score = self.memory_config.retrieval.min_score
        else:
            # 向后兼容:老 caller 不传 memory_config
            mode = "semantic"
            top_k = 5
            cfg_min_score = 0.3
        logger.debug(
            f"[_call_memory_retriever] query={query!r} (len={len(query)}) | "
            f"resolved mode={mode!r} top_k={top_k} min_score={cfg_min_score} | "
            f"already_surfaced_count={len(self._surfaced_memories)} "
            f"already_surfaced_paths={list(self._surfaced_memories)[:5]}"
            f"{'...' if len(self._surfaced_memories) > 5 else ''}"
        )
        return self.memory_retriever.search(
            query,
            top_k=top_k,
            mode=mode,
            already_surfaced=self._surfaced_memories,
        )

    def _messages_with_ids(self) -> list[dict]:
        """M11 (2026-06-26):给 self.messages 注入 stable id(仅当 m 无 id)。

        背景:sm_layer._slice_kept_messages 通过 m.get("id") 找 last_id,
        但 self.messages 只有 {role, content} 没有 id,导致 _slice_kept
        永远走 last_id is None 全返路径,SM 实际上丢不掉任何消息。

        实现:enumerate 索引当 id ("m0", "m1", ...)。纯运行时,不写盘不持久化。
        caller 拿到结果后用完即弃,id 字段不要回写到 self.messages。
        """
        out = []
        for i, m in enumerate(self.messages):
            if "id" not in m:
                m = {**m, "id": f"m{i}"}
            out.append(m)
        return out

    # ── Token 估算（粗略）──────────────────────────────────────────────

    def _estimate_tokens(self, text: str) -> int:
        """
        估算 Token 数（更精确的系数）。
        - 中文字符 ≈ 1.4 tokens/字（Anthropic 官方约 1.3~1.5）
        - 英文字符 ≈ 0.25 tokens/字
        - Overhead: 每条消息额外 ~10 tokens（role/结构/markers）
        """
        if not text:
            return 0
        # 中文字符数
        chinese_chars = sum(1 for c in text if '\u4e00' <= c <= '\u9fff')
        # 英文字符数
        english_chars = len(text) - chinese_chars
        return int(chinese_chars * 1.4 + english_chars * 0.25 + 10)

    def _estimate_message_tokens(self, msg: dict) -> int:
        """
        估算单条消息的 Token 数（含 system/assistant/user/tool 不同 role）。
        - system: ~15 tokens overhead
        - assistant/user: ~10 tokens overhead
        - tool_use: ~30 tokens（包含 tool_use marker + name + input）
        - tool_result: ~30 tokens（包含 tool_result marker + output）
        """
        role = msg.get("role", "")
        overhead = {"system": 15, "assistant": 10, "user": 10, "tool": 30}.get(role, 10)

        content = msg.get("content", "")
        if isinstance(content, str):
            return self._estimate_tokens(content) + overhead
        elif isinstance(content, list):
            total = overhead
            for block in content:
                if isinstance(block, dict):
                    block_type = block.get("type", "")
                    if block_type == "text":
                        text = block.get("text", "")
                        total += self._estimate_tokens(text)
                    elif block_type == "tool_use":
                        # tool_use block: name + input JSON
                        name = block.get("name", "")
                        import json
                        inp = json.dumps(block.get("input", {}))
                        total += self._estimate_tokens(name) + self._estimate_tokens(inp) + 20
                    elif block_type == "tool_result":
                        # tool_result block: content
                        text = str(block.get("content", ""))
                        total += self._estimate_tokens(text) + 15
                    else:
                        text = str(block)
                        total += self._estimate_tokens(text)
            return total
        return 0

    def _trim_messages(self):
        """
        Day 3 新增：按 Token 预算截断 history。
        保留：系统消息 + 最近的消息
        策略：从最老的消息开始删，直到 Token 预算内
        """
        if not self.messages:
            return

        # 估算当前总 Token
        total_tokens = sum(self._estimate_message_tokens(m) for m in self.messages)
        
        if total_tokens <= self.max_context_tokens:
            return  # 不需要截断

        # 需要截断：保留最近 80% 的消息（按 Token 计）
        target_tokens = int(self.max_context_tokens * 0.8)
        
        # 从最老的消息开始删
        while total_tokens > target_tokens and len(self.messages) > 2:
            removed = self.messages.pop(0)
            total_tokens -= self._estimate_message_tokens(removed)

    # ── 权限系统 helper(M12 增量)───────────────────────────────

    def _check_tool_permission(
        self,
        tool_name: str,
        tool_input: dict,
    ) -> tuple[bool, Optional[str], dict]:
        """
        同步检查单工具权限(对齐 doc §6.3)

        Returns:
            (allowed, error_message, effective_input)
            - allowed=True: 允许执行,error_message=None
            - allowed=False: 拒绝执行,error_message 含拒绝原因,仍要让 LLM 看到
            - effective_input: hook 可能改写后的 input(目前 M12 简化:返回原 input)
        """
        if self.permission_engine is None:
            # 未注入 permission_engine → 向后兼容,允许
            return True, None, tool_input

        # 取出 tool_def(duck-typed:只要有 name 即可)
        tool_def = self.tools.get(tool_name)
        if tool_def is None:
            # 工具不存在 — 让 execute() 自己返 error,这里放过
            return True, None, tool_input

        permission_logger.debug(
            "🛡️ [check_tool_permission_entry] tool=%s input=%s",
            tool_name, tool_input,
        )

        # 调 permission_engine 决策
        # 注:audit log 由 PermissionEngine._log_and_return 内部统一写(唯一审计点,
        # 对齐 doc §4.8),agent_core 不重复写,避免 double-logging。
        decision = self.permission_engine.check_permissions(
            tool_def, tool_input, list(self.messages),
        )

        from .tools.permission_types import PermissionBehavior

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
            # 给模型"为什么被拒 + 怎么换种方式重试"上下文(对齐 CC retry: true)
            permission_logger.debug("🛡️ [permission_denied_hook_fire] tool=%s", tool_name)
            retry_hint = self._run_permission_denied_hook(tool_name, tool_input, decision)
            if retry_hint:
                err = f"{err}\n💡 Retry hint: {retry_hint}"
            return False, err, tool_input
        if decision.behavior == PermissionBehavior.ASK.value:
            # M12 简化:auto_allow_ask=True → 自动 ALLOW(测试用)
            if self.auto_allow_ask:
                permission_logger.info(
                    "🛡️ [auto_allow_ask_skip_ui] tool=%s auto_allow_ask=True",
                    tool_name,
                )
                return True, None, decision.updated_input or tool_input
            # auto_allow_ask=False → 走 UI 路径
            permission_logger.info(
                "🛡️ [decision_ask_to_ui] tool=%s → _ask_user_permission",
                tool_name,
            )
            # Bug fix (2026-06-30): _ask_user_permission 返字符串 sentinel "AWAITING_PERMISSION"
            # (legacy v1 Event.wait 路径用),但本函数签名是 tuple[bool, str|None, dict]。
            # 直接 return string → L1143 拆包时 `allowed, perm_err, _ = "AWAITING_PERMISSION"`
            # 得到 allowed="A" / perm_err="W" → L1147 `perm_err == "__AWAITING_PERMISSION__"`
            # 永远 False,走 L1180 把 "W" 当 tool_output 写出 → LLM 收不到"权限还在等"
            # 信号 → 死循环重调同一 tool。修:调 _ask_user_permission 副作用(设
            # _pending_permission_request + 跑 hook)后,显式返 marker tuple
            # "__AWAITING_PERMISSION__",让 L1147 AWAITING block 正确走。
            self._ask_user_permission(tool_name, tool_input, decision)
            return False, "__AWAITING_PERMISSION__", tool_input
        # passthrough 或其他 → 当作 ASK(同上原因:返 marker tuple 而非 string)
        permission_logger.debug(
            "🛡️ [passthrough_to_ask] tool=%s behavior=%s",
            tool_name, decision.behavior,
        )
        return False, "__AWAITING_PERMISSION__", tool_input

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

    def _run_permission_denied_hook(
        self,
        tool_name: str,
        tool_input: dict,
        decision: Any,
    ) -> Optional[str]:
        """
        跑 PermissionDenied hook(M3 Task 3,对齐 doc §4.4)

        Returns:
            retry_prompt 字符串(hook 给出的重试提示)或 None
            异常时返 None(不阻断 deny,只是不加 hint)
        """
        if self.permission_engine is None:
            return None
        hook_registry = getattr(self.permission_engine, "hook_registry", None)
        if hook_registry is None:
            return None
        try:
            denied = hook_registry.run_permission_denied(
                tool_name, tool_input, self.permission_engine.context, decision,
            )
            return getattr(denied, "retry_prompt", None)
        except Exception as e:
            _logger.warning("PermissionDenied hook 异常: %s", e)
            return None

    def _ask_user_permission(
        self,
        tool_name: str,
        tool_input: dict,
        decision: Any,
    ) -> tuple[bool, Optional[str], dict]:
        """
        弹权限请求(v2 入口 — 非阻塞返 marker,UI 在 streamlit rerun 间决定)

        P1 (UI 弹窗 104ms 老 bug 修复):
        - v2 路径下,收到 AWAITING_PERMISSION sentinel 时**不阻塞**
        - 返 marker tuple: error_message = "__AWAITING_PERMISSION__"
        - run() 检测到 marker → yield ("awaiting_permission", req) + break
        - streamlit rerun → @st.dialog 渲染 → 用户点 Allow/Deny
        - UI 调 resume_after_permission(choice) → 续 run()

        旧 v1 路径已废弃 — Event.wait(0.1s) 轮询会让 streamlit 主线程
        一直 block,@st.dialog 永远没机会 render。

        Returns:
            (allowed, error_message, effective_input)
            - ALLOW         → (True, None, tool_input)
            - DENY_BY_HOOK  → (False, "Permission denied by hook", tool_input)
            - AWAITING      → (False, "__AWAITING_PERMISSION__", tool_input)  ← marker
        """
        sentinel = self._ask_user_permission_v2(tool_name, tool_input, decision)
        if sentinel == "ALLOW":
            return True, None, tool_input
        if sentinel == "DENY_BY_HOOK":
            return False, "Permission denied by PermissionRequest hook", tool_input
        # AWAITING_PERMISSION → 返 marker 不阻塞,run() 收到后 yield event + break
        return False, "__AWAITING_PERMISSION__", tool_input

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

    def _wait_for_permission_legacy(
        self,
        tool_name: str,
        tool_input: dict,
    ) -> tuple[bool, Optional[str], dict]:
        """v1 legacy 路径:Event.wait(0.1s) 轮询,没回复当 deny。

        仅 v1 _ask_user_permission tuple 接口用 — v2 PermissionCheckHandler 不调它。
        保留逻辑:测试 + 旧 UI 仍走 Event 路径(向后兼容)。
        """
        if self._permission_resolved is None:
            self._permission_resolved = threading.Event()

        _ask_t0 = time.time()
        got_response = self._permission_resolved.wait(timeout=0.1)
        _ask_wait_ms = (time.time() - _ask_t0) * 1000
        if got_response and self._pending_permission_request:
            choice = self._pending_permission_request.get("choice", "deny")
            self._pending_permission_request = None
            self._permission_resolved = None
            permission_logger.info(
                "🛡️ [ask_user_response] tool=%s choice=%s wait_ms=%.1f",
                tool_name, choice, _ask_wait_ms,
            )
            if choice in ("allow", "always_allow"):
                return True, None, tool_input
            return False, _PERMISSION_DENIED_BY_USER_MSG, tool_input
        # 超时:默认 deny
        self._pending_permission_request = None
        self._permission_resolved = None
        permission_logger.warning(
            "🛡️ [ask_user_timeout] tool=%s wait_ms=%.1f → default deny",
            tool_name, _ask_wait_ms,
        )
        return False, "Permission ask timed out (no user response)", tool_input

    def resolve_permission(self, choice: str) -> None:
        """
        外部(UI)在用户选择后调这个,解锁 _ask_user_permission

        Args:
            choice: "allow" | "deny" | "always_allow"
        """
        if self._pending_permission_request is not None:
            self._pending_permission_request["choice"] = choice
        if self._permission_resolved is not None:
            self._permission_resolved.set()

    # ── v2 per-phase helpers(Plan A)─────────────────────────────────
    # 这些是 handler 调用的真业务。run() 也复用这些,行为完全一致。
    # 每个 helper 是 generator,yield 事件供 phase.enter() 流式转出。

    def _iter_phase_setup(self, turn_ctx) -> Iterator["Event"]:
        """SETUP phase:准备 messages + 记忆检索 → emit turn indicator + memory_status。

        写入 turn_ctx:
        - messages_for_llm(后续 LLM phase 用)

        Yields:
            ("system", "🔄 Turn N/M") / ("memory_status", {...})
        """
        from agent_core.agent_state import Event
        turn = turn_ctx.turn_number
        yield ("system", f"🔄 Turn {turn}/{self.max_turns}")

        # 准备 messages_for_llm(后续 LLM phase 用)
        messages_for_llm = list(self.messages)
        if self.system_prompt:
            has_system = any(m.get("role") == "system" for m in messages_for_llm)
            if not has_system:
                messages_for_llm.insert(0, {"role": "system", "content": self.system_prompt})

        # 记忆检索 → emit memory_status
        if self.memory_retriever:
            last_user_msg = next(
                (m for m in reversed(messages_for_llm) if m.get("role") == "user"),
                None,
            )
            if last_user_msg and isinstance(last_user_msg.get("content"), str):
                try:
                    report = self._call_memory_retriever(last_user_msg["content"])
                    hits = report.hits if hasattr(report, "hits") else []
                    for h in hits:
                        if hasattr(h, "rel_path") and h.rel_path:
                            self._surfaced_memories.add(h.rel_path)
                    if hits:
                        mem_block = "\n\n[记忆库 / {} hits]\n".format(len(hits))
                        for h in hits:
                            mem_block += (
                                f"- [{getattr(h, 'type', '?')}] "
                                f"{getattr(h, 'title', '')}: "
                                f"{(getattr(h, 'body', '') or '')[:200]}\n"
                            )
                        messages_for_llm = [
                            {"role": "system", "content": (self.system_prompt or "") + mem_block}
                        ] + [m for m in messages_for_llm if m.get("role") != "system"]
                    stored_total = 0
                    if self.memory_store:
                        try:
                            counts = self.memory_store.count_by_type()
                            stored_total = sum(counts.values()) if isinstance(counts, dict) else 0
                        except Exception:
                            pass
                    injected_tokens = sum(
                        len((getattr(h, "body", "") or "")) // 4 for h in hits
                    )
                    yield ("memory_status", {
                        "hits": len(hits),
                        "stored_total": stored_total,
                        "injected_tokens": injected_tokens,
                        "zero_hit": len(hits) == 0,
                    })
                except Exception as e:
                    _logger.warning(f"Memory retrieval failed: {e}")

        # 把 messages 写到 turn_ctx 给 LLM phase 用
        turn_ctx.stage_inputs = messages_for_llm

    def _iter_phase_llm(self, turn_ctx) -> Iterator["Event"]:
        """LLM_THINKING phase:调 LLM + 解析 chunks → emit text/thinking/tool_call/usage。

        写入 turn_ctx.stage_outputs:
        - tool_calls / full_text / thinking_text / stop_reason / usage

        Yields:
            ("text", str) / ("thinking", str) / ("usage", UsageStats)
            tool_call chunks 累积到 stage_outputs,不单独 yield(由后续 tool phase dispatch)
        """
        from agent_core.agent_state import Event
        messages_for_llm = getattr(turn_ctx, "stage_inputs", None) or list(self.messages)

        tool_schemas = self.tools.list_schemas(provider=self._detect_provider())

        # M10-debug (2026-06-30): 临时调试日志,帮助理解 ReAct 循环中 LLM 行为。
        # TODO-DEBUG: 用户确认信息足够后清理。
        try:
            _logger.debug(
                "🤖 [LLM REQ] turn=%d msgs=%d tools=%d\n  messages=%s\n  tools=%s",
                getattr(turn_ctx, "turn_number", 0),
                len(messages_for_llm),
                len(tool_schemas) if tool_schemas else 0,
                _truncate_json_for_log(messages_for_llm, max_chars=2000),
                _truncate_json_for_log(tool_schemas, max_chars=1000),
            )
        except Exception as _log_e:
            _logger.debug("🤖 [LLM REQ] log failed: %s", _log_e)

        tool_calls: list = []
        full_text = ""
        thinking_text = ""
        stop_reason_this_turn: Optional[str] = None

        try:
            llm_chunks = self.llm.chat(
                messages=messages_for_llm,
                tools=tool_schemas or None,
                cache_namespace=(
                    f"react:{self._session_manager.session_id if self._session_manager else 'default'}"
                ),
            )
        except Exception as e:
            error_msg = f"LLM 调用失败: {type(e).__name__}: {e}"
            _logger.error(error_msg)
            yield ("system", f"❌ {error_msg}")
            yield ("text", f"抱歉,遇到了技术问题无法回答:{error_msg}")
            yield ("system", "✅ 回答完成")
            self.messages.append({"role": "assistant", "content": f"抱歉,遇到了技术问题:{e}"})
            if self._session_manager:
                try:
                    self._session_manager.add_assistant_message(
                        f"抱歉,遇到了技术问题:{e}",
                        usage=asdict(self._last_turn_usage) if self._last_turn_usage else None,
                    )
                except Exception:
                    pass
            # 标记 LLM 失败,终止 run
            turn_ctx.stage_outputs = _LLMResult(
                tool_calls=[], full_text="", thinking_text="",
                stop_reason="llm_error", usage=None,
            )
            return

        try:
            _llm_chunk_iter = iter(llm_chunks)
            _exhausted = False
            while not _exhausted:
                try:
                    chunk = next(_llm_chunk_iter)
                except StopIteration:
                    _exhausted = True
                    break

                if (
                    self._run_state is not None
                    and self._run_state.cancel_event.is_set()
                ):
                    stop_reason_this_turn = stop_reason_this_turn or "interrupted"
                    break

                if chunk.text_delta:
                    full_text += chunk.text_delta.text
                    yield ("text", chunk.text_delta.text)
                if chunk.thinking_delta:
                    thinking_text += chunk.thinking_delta.thinking
                    self._pending_thinking += chunk.thinking_delta.thinking
                    yield ("thinking", chunk.thinking_delta.thinking)
                if chunk.tool_call:
                    tool_calls.append(chunk.tool_call)
                if getattr(chunk, "stop_reason", None):
                    stop_reason_this_turn = chunk.stop_reason
                if chunk.usage:
                    self._last_turn_usage = chunk.usage
                    yield ("usage", chunk.usage)
                    if self.context_manager:
                        self.context_manager.set_baseline(
                            chunk.usage.input_tokens,
                            len(self.messages),
                        )
        except Exception as e:
            error_msg = f"LLM 流式响应中断: {type(e).__name__}: {e}"
            _logger.error(error_msg)
            yield ("system", f"❌ {error_msg}")
            yield ("text", f"抱歉,响应被中断:{error_msg}")
            yield ("system", "✅ 回答完成")
            partial = full_text or f"[响应中断:{e}]"
            self.messages.append({"role": "assistant", "content": partial})
            if self._session_manager:
                try:
                    self._session_manager.add_assistant_message(
                        partial,
                        usage=asdict(self._last_turn_usage) if self._last_turn_usage else None,
                    )
                except Exception:
                    pass
            turn_ctx.stage_outputs = _LLMResult(
                tool_calls=[], full_text="", thinking_text="",
                stop_reason="llm_error", usage=None,
            )
            return
        finally:
            try:
                llm_chunks.close()
            except Exception:
                pass

        # 写 stage_outputs 给后续 tool phase 用
        turn_ctx.stage_outputs = _LLMResult(
            tool_calls=tool_calls,
            full_text=full_text,
            thinking_text=thinking_text,
            stop_reason=stop_reason_this_turn,
            usage=self._last_turn_usage,
        )

        # M10-debug (2026-06-30): dump LLM 响应(完整 tool_calls + text + thinking + usage)
        # 注:tool_calls 元素可能是 ToolCallDelta / ToolCall,字段名可能缺(.id 是常见问题),
        # 用 getattr + vars 防御性访问。
        try:
            _tc_reprs = []
            for tc in tool_calls:
                if hasattr(tc, "id") or hasattr(tc, "name") or hasattr(tc, "input"):
                    _tc_reprs.append({
                        "id": getattr(tc, "id", None),
                        "name": getattr(tc, "name", None),
                        "input": getattr(tc, "input", None),
                    })
                else:
                    # fallback: dump 所有字段
                    _tc_reprs.append({k: getattr(tc, k, None) for k in vars(tc)})
            _logger.debug(
                "🤖 [LLM RESP] turn=%d stop=%s text=%s\n  thinking=%s\n  tool_calls=%s\n  usage=%s",
                getattr(turn_ctx, "turn_number", 0),
                stop_reason_this_turn,
                full_text or "(empty)",
                thinking_text or "(empty)",
                json.dumps(_tc_reprs, ensure_ascii=False, default=str),
                asdict(self._last_turn_usage) if self._last_turn_usage else None,
            )
        except Exception as _log_e:
            _logger.debug("🤖 [LLM RESP] log failed: %s", _log_e)

        # 累积最后一轮 usage 计数(run() 末尾的 bridge.on_turn_end 用)
        if self._last_turn_usage is not None:
            self._run_state.last_input_tokens = self._last_turn_usage.input_tokens
            self._run_state.last_output_tokens = self._last_turn_usage.output_tokens
        self._run_state.last_tool_calls = list(tool_calls)

    def _iter_phase_tools(self, turn_ctx) -> Iterator["Event"]:
        """EXECUTING_TOOLS phase:权限检查 + 执行工具 → emit tool_call/tool_result。

        关键:
        - 写入 self.messages(tool_use blocks + tool_result blocks)
        - 检测 AWAITING_PERMISSION marker → 设置 turn_ctx.permission_request + yield + return
        - 单 tool 串行 / 多 tool 并行

        读取 turn_ctx.stage_outputs(由 LLM phase 写入)
        """
        from agent_core.agent_state import AgentPhase, Event

        stage_out = getattr(turn_ctx, "stage_outputs", None)

        # ── Fix C (2026-06-30): resume-after-permission 续跑恢复 ──────
        # 标志:stage_outputs 为空(新 turn_ctx 没跑 LLM)+ run_state.last_tool_calls
        # 非空(上次 _iter_phase_tools 进入前已 LLM 跑过,缓存了 tool_calls)+
        # run_state.awaiting_permission 已设(说明 _iter_phase_tools 写过 pending)。
        # 上次 yield awaiting_permission event 后,run_agent() 早退 → generator 被销毁;
        # resume_after_permission 把 SM 转回 EXECUTING_TOOLS,UI 调 step() → 新 turn_ctx
        # (stage_outputs=None)。原代码此时直接 return → SM 又走 tools_done →
        # LLM_THINKING → LLM 重生成 bash → 死循环。
        # 这里从 run_state.last_tool_calls + self.messages[-1] 重建 stage_out,
        # 让工具继续执行,而不是再次走 LLM。
        _is_resume = (
            stage_out is None
            and self._run_state is not None
            and bool(self._run_state.last_tool_calls)
            and self._run_state.awaiting_permission is not None
        )
        if _is_resume:
            # 从 self.messages[-1](上次 append 的 assistant tool_use msg)提取 text
            _last_msg = self.messages[-1] if self.messages else None
            _full_text_recovered = ""
            if _last_msg and isinstance(_last_msg.get("content"), list):
                for _blk in _last_msg["content"]:
                    if isinstance(_blk, dict) and _blk.get("type") == "text":
                        _full_text_recovered = _blk.get("text", "")
                        break
            stage_out = _LLMResult(
                tool_calls=list(self._run_state.last_tool_calls),
                full_text=_full_text_recovered,
            )
            turn_ctx.stage_outputs = stage_out
            # ── 不要把 permission_request 搬到 turn_ctx! ──
            # ExecutingToolsPhase.next() 看 turn_ctx.permission_request:
            # - 不是 None → 转 AWAITING_PERMISSION(用户首次触发暂停时的逻辑)
            # - None → 转 LLM_THINKING(正常 ReAct 闭环)
            # Fix C 是 resume-after-permission:用户已点 Allow,工具这次就该执行。
            # 如果把 run_state.awaiting_permission 拷过来,next() 又会把 SM 拨回
            # AWAITING_PERMISSION → 永远不进 LLM_THINKING → LLM 收不到 tool_result
            # → 没 ReAct 闭环(Stop 按钮一直亮)。
            # 所以这里 turn_ctx.permission_request 保持 None,让 next() 走 tools_done。
            _logger.debug(
                "🔄 [Fix C resume] 重建 stage_out: %d tool_call(s) | full_text=%r",
                len(stage_out.tool_calls), _full_text_recovered[:80],
            )

        if stage_out is None:
            return
        tool_calls = stage_out.tool_calls or []
        if not tool_calls:
            return

        full_text = stage_out.full_text or ""

        # ── Fix C: resume 路径下,assistant message + pending log 之前已 append/yield,
        # 不重做(否则 messages 重复 + jsonl 重复 entry)。其他路径照常 append。
        if not _is_resume:
            # 先把 assistant 的 tool_use blocks 加到 history
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
            self.messages.append({"role": "assistant", "content": assistant_content})

        _cancel_evt = (
            self._run_state.cancel_event if self._run_state is not None else None
        )

        if len(tool_calls) == 1:
            # 单工具:串行
            tc = tool_calls[0]
            # ── Fix C: resume 路径下 tool_call event + action log 之前已 yield/append,
            # 不重做(否则 UI 重复 tool_call bubble)。permission 也跳过(用户已 allow)。
            if not _is_resume:
                yield ("tool_call", {"name": tc.tool_name, "input": tc.tool_input, "parallel": False})
                self._pending_tool_logs.append(
                    {"type": "action", "name": tc.tool_name, "input": tc.tool_input}
                )

                allowed, perm_err, effective_input = self._check_tool_permission(
                    tc.tool_name, tc.tool_input
                )
                # M10-debug (2026-06-30): 临时 — 直接看拆包结果 + AWAITING 块是否进。
                # banner 确认 streamlit 加载了最新代码,但 permission_request 仍 None,
                # 说明 L1219 条件没 True(perm_err 不是 marker),看实际值。
                _logger.debug(
                    "🔍 [_iter_phase_tools single] allowed=%s perm_err=%r type(perm_err)=%s",
                    allowed, perm_err, type(perm_err).__name__,
                )
            else:
                # resume: 用户已点 Allow,直接 allowed,跳过 permission check
                allowed = True
                effective_input = tc.tool_input
                _logger.debug(
                    "🔄 [_iter_phase_tools single resume] skip yield tool_call + "
                    "permission check | tc.tool_name=%s tool_use_id=%s",
                    tc.tool_name, tc.tool_use_id,
                )
            if not allowed:
                if perm_err == "__AWAITING_PERMISSION__":
                    req = self._pending_permission_request or {
                        "tool_name": tc.tool_name,
                        "tool_input": tc.tool_input,
                        "reason": "",
                        "message": "",
                    }
                    # Deny-loop fix (2026-06-30): 把 tool_use_id 加到 pending req,
                    # resume_after_permission 写 tool_result 时要用。
                    req["tool_use_id"] = tc.tool_use_id
                    if self._run_state is not None:
                        self._run_state.awaiting_permission = req
                    turn_ctx.permission_request = req
                    # M10-debug (2026-06-30): 临时 id 追踪 — phase.next 看到 None
                    # 但这里设了,验证 turn_ctx id 是否跟 phase.next 的 ctx.turn_ctx 相同。
                    _logger.debug(
                        "🔍 [AWAITING set] id(turn_ctx)=%d permission_request=%r",
                        id(turn_ctx), req,
                    )
                    # M10-AWAITING fix (2026-06-30): 同步强制 SM 转 AWAITING_PERMISSION。
                    # 背景:run_agent() 看到 awaiting_permission event 后早退 + st.rerun(),
                    # _drive/step() generator 被暂停在 yield awaiting_permission 处 →
                    # SM.trigger 内 yield from current.enter() 之后的 current.next()
                    # 永远不跑 → SM 卡在 EXECUTING_TOOLS →
                    # resume_after_permission() 的 `if _sm.current == AWAITING_PERMISSION`
                    # 检查永远 False → v2 推进 block 整个跳过 →
                    # 下次 step() 直接转 LLM_THINKING → LLM 看到 orphan tool_use
                    # 重生成 bash 命令 → 又触发 ASK → 用户点 Allow 死循环。
                    # 这里同步写 SM._phase + _history,即使外层 generator 被销毁,
                    # SM 状态仍正确,resume_after_permission 检查会通过。
                    if self._sm is not None and self._sm.current != AgentPhase.AWAITING_PERMISSION:
                        _old_phase = self._sm.current
                        self._sm._phase = AgentPhase.AWAITING_PERMISSION
                        self._sm._history.append((
                            _old_phase, "permission_needed",
                            AgentPhase.AWAITING_PERMISSION,
                        ))
                        _logger.debug(
                            "🔒 [SM force-transition] %s --[permission_needed]--> awaiting_permission",
                            _old_phase.value,
                        )
                    # Deny-loop fix (2026-06-30): 在 yield + return 前先把
                    # assistant 的 tool_use 持久化到 session(否则 _iter_phase_finalize
                    # 不跑,jsonl 只有 user msg,看不到 LLM 决策历史)。
                    # tool_result 等用户决策后由 resume_after_permission 追加。
                    if self._session_manager:
                        try:
                            tc_list = [{
                                "id": tc.tool_use_id,
                                "name": tc.tool_name,
                                "input": tc.tool_input,
                            }]
                            self._session_manager.add_assistant_with_tools(
                                text=full_text, tool_calls=tc_list,
                            )
                        except Exception as e:
                            _logger.warning(
                                f"Failed to persist assistant tool_use before awaiting_permission: {e}"
                            )
                    yield ("awaiting_permission", req)
                    return  # 暂停 state machine
                tool_output = perm_err or "Permission denied"
                yield ("tool_result", {
                    "name": tc.tool_name,
                    "output": tool_output,
                    "success": False,
                    "elapsed": 0.0,
                })
                self._pending_tool_logs.append({
                    "type": "result", "name": tc.tool_name,
                    "output": tool_output, "success": False,
                })
                self.messages.append(_make_tool_result_block(tc.tool_use_id, tool_output))
                self._pending_tool_results.append((tc.tool_use_id, tool_output))
                return  # 本 turn 结束

            start_time = time.time()
            result = self.tools.execute(
                tc.tool_name, effective_input, max_retries=3, cancel_event=_cancel_evt,
            )
            elapsed = time.time() - start_time

            if result["status"] == "success":
                tool_output = result["output"]
                yield ("tool_result", {
                    "name": tc.tool_name, "output": tool_output,
                    "success": True, "elapsed": elapsed,
                })
                self._pending_tool_logs.append({
                    "type": "result", "name": tc.tool_name,
                    "output": tool_output, "success": True,
                })
            else:
                tool_output = f"工具执行失败: {result['error']}"
                yield ("tool_result", {
                    "name": tc.tool_name, "output": tool_output,
                    "success": False, "elapsed": elapsed,
                })
                self._pending_tool_logs.append({
                    "type": "result", "name": tc.tool_name,
                    "output": tool_output, "success": False,
                })
            self.messages.append(_make_tool_result_block(tc.tool_use_id, tool_output))
            self._pending_tool_results.append((tc.tool_use_id, tool_output))
            # ── Fix C (2026-06-30): resume 路径工具执行完成,清 pending 标志。
            # 否则下次 step() 还会走 resume 分支(检测到 awaiting_permission != None)
            # 重入工具执行路径,导致重复执行 + 重复 append tool_result。
            if _is_resume and self._run_state is not None:
                self._run_state.awaiting_permission = None
        else:
            # 多工具:并行
            tool_names = [tc.tool_name for tc in tool_calls]
            # ── Fix C: resume 路径下 tool_call event + parallel_start log 之前已 yield,
            # 不重做。permission 也跳过(用户已 allow)。
            if not _is_resume:
                yield ("tool_call", {"names": tool_names, "parallel": True})
                self._pending_tool_logs.append(
                    {"type": "parallel_start", "names": tool_names}
                )

            pre_results: list = []
            for tc in tool_calls:
                if _is_resume:
                    # resume: 用户已点 Allow,所有 tool 直接 allowed,跳过 permission check。
                    # pre_result=None → 下游 executor 跑 tool(self.tools.execute 用 tc.tool_input)。
                    pre_results.append((tc, None))
                    continue
                allowed, perm_err, effective_input = self._check_tool_permission(
                    tc.tool_name, tc.tool_input
                )
                if not allowed:
                    if perm_err == "__AWAITING_PERMISSION__":
                        req = self._pending_permission_request or {
                            "tool_name": tc.tool_name,
                            "tool_input": tc.tool_input,
                            "reason": "",
                            "message": "",
                        }
                        # Deny-loop fix (2026-06-30): tool_use_id 透传
                        req["tool_use_id"] = tc.tool_use_id
                        if self._run_state is not None:
                            self._run_state.awaiting_permission = req
                        turn_ctx.permission_request = req
                        # M10-debug (2026-06-30): 临时 id 追踪(并行 tool 路径)
                        _logger.debug(
                            "🔍 [AWAITING set parallel] id(turn_ctx)=%d permission_request=%r",
                            id(turn_ctx), req,
                        )
                        # M10-AWAITING fix (2026-06-30): 同步强制 SM 转 AWAITING_PERMISSION。
                        # 见单 tool 分支的同款注释。两条路径必须都修,否则 parallel
                        # tool 仍会卡。
                        if self._sm is not None and self._sm.current != AgentPhase.AWAITING_PERMISSION:
                            _old_phase = self._sm.current
                            self._sm._phase = AgentPhase.AWAITING_PERMISSION
                            self._sm._history.append((
                                _old_phase, "permission_needed",
                                AgentPhase.AWAITING_PERMISSION,
                            ))
                            _logger.debug(
                                "🔒 [SM force-transition parallel] %s --[permission_needed]--> awaiting_permission",
                                _old_phase.value,
                            )
                        # Deny-loop fix (2026-06-30): yield 前持久化 assistant tool_use
                        if self._session_manager:
                            try:
                                tc_list = [{
                                    "id": tc.tool_use_id,
                                    "name": tc.tool_name,
                                    "input": tc.tool_input,
                                }]
                                self._session_manager.add_assistant_with_tools(
                                    text=full_text, tool_calls=tc_list,
                                )
                            except Exception as e:
                                _logger.warning(
                                    f"Failed to persist assistant tool_use before awaiting_permission: {e}"
                                )
                        yield ("awaiting_permission", req)
                        return
                    pre_results.append((tc, {
                        "status": "error",
                        "error": perm_err or "Permission denied",
                    }))
                else:
                    pre_results.append((tc, None))

            start_time = time.time()
            with concurrent.futures.ThreadPoolExecutor() as executor:
                future_to_tc = {}
                for tc, pre_result in pre_results:
                    if pre_result is not None:
                        continue
                    future = executor.submit(
                        self.tools.execute,
                        tc.tool_name, tc.tool_input,
                        max_retries=3, cancel_event=_cancel_evt,
                    )
                    future_to_tc[future] = tc
                results = []
                for future in concurrent.futures.as_completed(future_to_tc):
                    tc = future_to_tc[future]
                    result = future.result()
                    results.append((tc, result))
                for tc, pre_result in pre_results:
                    if pre_result is not None:
                        results.append((tc, pre_result))

            results.sort(key=lambda x: tool_calls.index(x[0]))
            for tc, result in results:
                elapsed = time.time() - start_time
                if result["status"] == "success":
                    tool_output = result["output"]
                    yield ("tool_result", {
                        "name": tc.tool_name, "output": tool_output,
                        "success": True, "elapsed": elapsed,
                    })
                    self._pending_tool_logs.append({
                        "type": "result", "name": tc.tool_name,
                        "output": tool_output, "success": True,
                    })
                else:
                    tool_output = f"工具执行失败: {result['error']}"
                    yield ("tool_result", {
                        "name": tc.tool_name, "output": tool_output,
                        "success": False, "elapsed": elapsed,
                    })
                    self._pending_tool_logs.append({
                        "type": "result", "name": tc.tool_name,
                        "output": tool_output, "success": False,
                    })
                self.messages.append(_make_tool_result_block(tc.tool_use_id, tool_output))
                self._pending_tool_results.append((tc.tool_use_id, tool_output))
            # ── Fix C (2026-06-30): parallel resume 路径工具执行完成,清 pending。
            if _is_resume and self._run_state is not None:
                self._run_state.awaiting_permission = None

    def _iter_phase_finalize(self, turn_ctx) -> Iterator["Event"]:
        """FINALIZING phase:memory bridge extract + session flush。

        Yields:
            ("memory_event", event) / ("system", "✅ 回答完成")

        职责切分(2026-06-30,SessionPersistHandler 真实现落地后):
            - assistant 消息(assistant_with_tools / add_assistant_message)由 v1 streaming 路径
              (_iter_phase_tools:1340 / _iter_phase_llm:2298)写,这里不重复,避免 double-write
            - tool_results 由 output_chain 的 SessionPersistHandler 写(无条件刷 _pending_tool_results),
              这里不再负责
            - 本函数只剩 memory_bridge extract + session flush
        """
        from agent_core.agent_state import Event
        stage_out = getattr(turn_ctx, "stage_outputs", None)
        full_text = stage_out.full_text if stage_out else ""
        stop_reason = stage_out.stop_reason if stage_out else None

        # bookkeeping:L3 phase 在 transition 前已设 _run_state.final_answer/stop_reason
        # (agent_state.py:LLMThinkingPhase.next:524-525),此处再赋值只为 v1 streaming
        # 路径兜底(v1 run() 不走 SM phase,这条赋值是 _run_state.final_answer 的
        # 唯一来源)。assistant_message 已由 _iter_phase_llm:2298 写入,不再 add_assistant_message。
        if stage_out and not stage_out.tool_calls:
            self.messages.append({"role": "assistant", "content": full_text})
            self._run_state.final_answer = full_text
            self._run_state.final_stop_reason = stop_reason
            yield ("system", "✅ 回答完成")
        else:
            # 有 tool_call:assistant_with_tools + tool_results 已分别由
            # _iter_phase_tools:1340 / output_chain.SessionPersistHandler 写
            self._run_state.final_answer = full_text

        # Memory bridge extract(run 末尾 / 收尾 turn)
        if (
            self.react_memory_bridge
            and self._run_state.turn > 0
            and self._run_state.final_answer
            and self._run_state.final_stop_reason not in _TRUNCATED_STOP_REASONS
        ):
            try:
                for event in self.react_memory_bridge.on_turn_end(
                    user_msg=self._run_state.user_message,
                    assistant_resp=self._run_state.final_answer,
                    turn_index=self._run_state.turn,
                    input_tokens=self._run_state.last_input_tokens,
                    output_tokens=self._run_state.last_output_tokens,
                    tool_calls_in_turn=len(self._run_state.last_tool_calls),
                ):
                    yield ("memory_event", event)
            except Exception as e:
                _logger.warning(f"Memory bridge failed: {e}")

        # 注(2026-06-30):intermediate-turn session persistence(add_assistant_with_tools
        # + add_tool_results 段)整体迁出本函数。tool_results 刷新由
        # output_chain.SessionPersistHandler(handle)接管;assistant_with_tools
        # 仍由 _iter_phase_tools:1340 在 awaiting_permission 前写。
        # 这里不再调 session_manager,避免 double-write。

        # Day 4: run 末尾 flush session
        if self._session_manager and self._sm.is_done:
            try:
                self._session_manager.flush()
            except Exception as e:
                _logger.warning(f"Failed to flush session: {e}")

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
        """v2 API:初始化一次 run(重置 state machine + per-run state)。

        与现有 run() 顶部的状态初始化等价(L838-850 段):
        - 追加 user msg 到 self.messages
        - 持久化 user msg 到 session
        - 重置 _pending_thinking / _pending_tool_logs / _pending_tool_results
        - 新增:重置 _run_state(含 cancel_event)+ 重建 StateMachine

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
        1. 把 choice 写进 _pending_permission_request(供 legacy _ask_user_permission 读)
        2. set _permission_resolved Event(供 legacy 路径)
        3. 重新 trigger 一次 phase 让 state machine 知道 permission 已 resolved

        Deny-loop fix (2026-06-30): 如果 choice=="deny",在 transition 前先把
        tool_result "Permission denied by user" append 到 self.messages。
        否则下次 LLM 调用看不到 denial,会重新生成相同的 tool_call 死循环。

        Args:
            choice: "allow" | "deny" | "always_allow"
        """
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
                # 同时记到 _pending_tool_results(供 _iter_phase_finalize 持久化)
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

    # ── 主循环 ──────────────────────────────────────────────────────

    def run(self, user_message: str):
        """
        执行 ReAct 循环，返回生成器。
        流式 yield 所有中间过程（text / thinking / tool_call / tool_result / usage / system）。

        Day 4: 如果启用了 session，run 结束后自动保存到 session。

        B7(v2 重构): 顶部初始化委托给 start_run()(消除重复),
        主循环保留 v1 逻辑(handlers 还是空 stub,无法用 state machine 驱动)。
        """
        # system prompt 不持久化到 JSONL（与 Claude Code 一致：每次 run 动态注入）

        # ── B7: 委托给 v2 start_run()(替代原 1018-1031 行的 init 逻辑)───
        self.start_run(user_message)

        # Day 5：用 ContextManager 替代 _trim_messages
        # 检查 token 预算，必要时自动压缩
        # Fork 模式：传入主 agent 的 system_prompt + tools，复用 cache prefix
        tool_schemas = self.tools.list_schemas(provider=self._detect_provider())

        # M10 C2.1: L3 SM 快路径决策(§4.3/§4.4)
        # 在 ContextManager 之前先问 SM:命中 sm_compact 走零 LLM 快路径
        # 否则 fallback 到 ContextManager(原有逻辑)
        sm_compact_used = False
        if self.session_memory is not None:
            try:
                # M11 (2026-06-26):_slice_kept_messages 通过 m.get("id") 找 last_id
                # 但 self.messages 里只有 {role, content} 没有 id,导致 _slice_kept
                # 永远走 last_id is None 全返路径,SM 实际上无法丢消息
                # 修复:在传给 SM 之前用 enumerate 注入 stable id(仅当 m 无 id)
                msgs_with_id = self._messages_with_ids()

                total_tokens = sum(
                    self._estimate_message_tokens(m) for m in self.messages
                )
                tool_count = getattr(self, "cumulative_tool_calls", 0)
                ctx = _TurnContext(
                    messages=msgs_with_id,
                    total_tokens=total_tokens,
                    tool_count=tool_count,
                )
                _logger.debug(
                    f"[L3 SM] 决策入口: msgs={len(msgs_with_id)} total_tokens={total_tokens} "
                    f"tool_count={tool_count} | "
                    f"sm_path={self.session_memory.sm_path} | "
                    f"sm.last_compacted_msg_id={self.session_memory.last_compacted_msg_id}"
                )
                decision = self.session_memory.should_trigger_compact(ctx)
                _logger.debug(
                    f"[L3 SM] 决策结果: strategy={decision.strategy} reason={decision.reason!r} "
                    f"timeout_ms={decision.timeout_ms}"
                )
                if decision.strategy == "sm_compact":
                    sm_result = self.session_memory.compact(
                        msgs_with_id,
                        context_window=self.max_context_tokens,
                    )
                    if sm_result is not None:
                        # SM 压缩成功:替换 messages
                        # kept_messages 是 msgs_with_id 的子集,剥离注入的 id(避免污染
                        # 后续 self.messages 持久化逻辑;id 是 SM 内部用)
                        kept = [
                            {k: v for k, v in m.items() if k != "id"}
                            for m in sm_result.kept_messages
                        ]
                        self.messages = [sm_result.summary_message] + kept
                        sm_compact_used = True
                        _logger.info(
                            f"SM-compact OK: ~{sm_result.used_tokens_estimate} tokens estimated"
                        )
                        _logger.debug(
                            f"[L3 SM] 应用 compact 结果: "
                            f"summary_message.role={sm_result.summary_message['role']!r} "
                            f"summary_chars={len(sm_result.summary_message['content'])} | "
                            f"kept_count={len(sm_result.kept_messages)} "
                            f"self.messages 新长度={len(self.messages)}"
                        )
                        yield (
                            "system",
                            f"📦 [L3 fast path] 上下文已压缩(SM 文件): ~{sm_result.used_tokens_estimate} tokens",
                        )
                    else:
                        _logger.debug(
                            f"[L3 SM] compact() 返 None(SM 文件不可用),fallback ContextManager"
                        )
                    # else: SM 返回 None → fallback 走 ContextManager
                elif decision.strategy == "wait":
                    # 等 extraction 完成(本期简化:记 log,fallback ContextManager)
                    _logger.info(
                        f"[L3 SM] wait: {decision.reason} timeout_ms={decision.timeout_ms}, "
                        f"fallback ContextManager"
                    )
                elif decision.strategy == "traditional":
                    _logger.debug(
                        f"[L3 SM] strategy=traditional fallback ContextManager "
                        f"(reason={decision.reason!r})"
                    )
            except Exception as e:
                # SM 决策/压缩异常 → 安全回退到 ContextManager
                _logger.warning(f"SM fast path 异常,fallback ContextManager: {e}")

        if not sm_compact_used:
            compacted, compact_result = self.context_manager.check_and_compact(
                self.messages,
                parent_system=self.system_prompt,
                parent_tools=tool_schemas or None,
                parent_messages=self.messages,
            )
            if compact_result:
                if compact_result.success:
                    self.messages = compacted
                    _logger.info(f"Context compacted: {compact_result.summary_str()}")

                    # 对齐 Claude Code buildPostCompactMessages 单一构造点
                    # 顺序: boundary → summary → preserved head
                    # 之前 P0 bug: 只写了 boundary + summary 两方法，preserved head 6 条不写盘
                    # 现场: data/sessions/7f071c62.jsonl
                    if self._session_manager:
                        try:
                            self._persist_compacted_messages(self.messages, compact_result)
                        except Exception as e:
                            _logger.warning(f"Failed to persist compaction to session: {e}")

                    yield ("system", f"📦 上下文已压缩: {compact_result.tokens_freed:,} tokens 释放")
                else:
                    _logger.warning(f"Context compact failed: {compact_result.error}")
                    # 压缩失败时回退到旧的截断策略
                    self._trim_messages()
        # Day 3 旧逻辑（保留作为 fallback）
        # self._trim_messages()

        # Task 7 (Path A): 跟踪最后一轮的 usage/tool_calls,供 run 末尾调 bridge.on_turn_end 使用
        last_turn: int = 0
        last_input_tokens: int = 0
        last_output_tokens: int = 0
        last_tool_calls: list = []
        # Bug 1e 修复(2026-06-24):只有本轮真正走到"最终文本回答"分支(无 tool_call、
        # 正常收尾)才记下这条文本。None 表示本轮没有完整回答(还在工具循环里被
        # max_turns 截断 / 中断),此时末尾不调 on_turn_end,避免拿 reversed 扫描误配
        # 到上一轮的回答。
        final_answer: Optional[str] = None
        # 收尾那一轮的终止原因(end_turn/stop=正常,max_tokens/length=被截断)。
        # 用于在「无 tool_call 但被 max_tokens 截断」时也跳过提取。
        final_stop_reason: Optional[str] = None

        for turn in range(1, self.max_turns + 1):
            # ── B7: INTERRUPTED 检查(D14-D20 准备)─────────────────────
            # 用户调 agent.interrupt() → cancel_event.set() → 本轮立即停。
            # 状态机转 INTERRUPTED,本 loop break,run() 自然终止。
            if self._run_state is not None and self._run_state.cancel_event.is_set():
                _logger.info("⏹️ [run loop] cancel_event detected, 退出 loop at turn=%d", turn)
                yield ("system", "⏹️ 对话已被用户中断")
                yield ("system", "✅ 对话结束")
                break

            yield ("system", f"🔄 Turn {turn}/{self.max_turns}")

            # ── 准备发送给 LLM 的 messages ──────────────────────────
            messages_for_llm = list(self.messages)

            # P2 新增：如果有 system_prompt，添加到消息开头
            if self.system_prompt:
                # 检查是否已经有 system message
                has_system = any(m.get("role") == "system" for m in messages_for_llm)
                if not has_system:
                    messages_for_llm.insert(0, {"role": "system", "content": self.system_prompt})

            # M7 ported: 记忆检索(若启用)→ 拼成 system 片段 + 推送 memory_status
            if self.memory_retriever:
                # 用最后一条 user message 做 query
                last_user_msg = next(
                    (m for m in reversed(messages_for_llm) if m.get("role") == "user"),
                    None,
                )
                if last_user_msg and isinstance(last_user_msg.get("content"), str):
                    try:
                        # M11 (2026-06-26 修复):mode/top_k 从 memory_config 读
                        # (.env → MemoryConfig.from_env()),不再硬编码
                        # M11: 传 already_surfaced 让 retriever 过滤已展示过的
                        report = self._call_memory_retriever(
                            last_user_msg["content"],
                        )
                        hits = report.hits if hasattr(report, "hits") else []
                        # 记录已展示的 hit(用于下一轮去重)
                        for h in hits:
                            if hasattr(h, "rel_path") and h.rel_path:
                                self._surfaced_memories.add(h.rel_path)
                        if hits:
                            mem_block = "\n\n[记忆库 / {} hits]\n".format(len(hits))
                            for h in hits:
                                mem_block += f"- [{getattr(h, 'type', '?')}] {getattr(h, 'title', '')}: {(getattr(h, 'body', '') or '')[:200]}\n"
                            # 追加到 system prompt(不覆盖,只是叠加)
                            messages_for_llm = [{"role": "system", "content": (self.system_prompt or "") + mem_block}] + [
                                m for m in messages_for_llm if m.get("role") != "system"
                            ]
                        # 推送 memory_status 给 UI(stream chunk)
                        stored_total = 0
                        if self.memory_store:
                            try:
                                counts = self.memory_store.count_by_type()
                                stored_total = sum(counts.values()) if isinstance(counts, dict) else 0
                            except Exception:
                                pass
                        injected_tokens = sum(len((getattr(h, "body", "") or "")) // 4 for h in hits)
                        yield ("memory_status", {
                            "hits": len(hits),
                            "stored_total": stored_total,
                            "injected_tokens": injected_tokens,
                            "zero_hit": len(hits) == 0,
                        })
                    except Exception as e:
                        _logger.warning(f"Memory retrieval failed: {e}")

            # ── 调用 LLM（流式）─────────────────────────────────────
            tool_schemas = self.tools.list_schemas(provider=self._detect_provider())

            # 收集本轮响应中的 tool_call chunks
            tool_calls = []  # list of ToolCallDelta
            full_text = ""
            thinking_text = ""
            stop_reason_this_turn: Optional[str] = None  # 本轮 LLM 终止原因

            # === 日志：发送给 LLM 的原始消息 ===
            _logger.debug("\n" + "=" * 60)
            _logger.debug(f"📤 【发送给 LLM】Turn {turn}/{self.max_turns}")
            _logger.debug("=" * 60)
            _logger.debug(_format_messages_for_log(messages_for_llm))
            if tool_schemas:
                _logger.debug(f"\n📋 可用工具: {[t['name'] for t in tool_schemas]}")

            try:
                llm_chunks = self.llm.chat(
                    messages=messages_for_llm,
                    tools=tool_schemas or None,
                    cache_namespace=f"react:{self._session_manager.session_id if self._session_manager else 'default'}",  # M7 ported: prompt cache namespace
                )
            except Exception as e:
                # LLM 调用异常，优雅降级
                error_msg = f"LLM 调用失败: {type(e).__name__}: {e}"
                _logger.error(error_msg)
                yield ("system", f"❌ {error_msg}")
                yield ("text", f"抱歉，遇到了技术问题无法回答：{error_msg}")
                yield ("system", "✅ 回答完成")
                self.messages.append({"role": "assistant", "content": f"抱歉，遇到了技术问题：{e}"})
                # Day 4: 保存到 session
                if self._session_manager:
                    try:
                        self._session_manager.add_assistant_message(
                            f"抱歉，遇到了技术问题：{e}",
                            usage=asdict(self._last_turn_usage) if self._last_turn_usage else None,
                        )
                    except Exception:
                        pass
                return

            # P1-2 修复：for chunk 循环也要 catch 异常（生成器 yield 过程中异常）
            # 之前只 catch 了 llm.chat() 调用异常，生成器中途中断（如 OpenAI 网络中断）会逃出 run() 循环
            try:
                # R3 (review 修复): 把 generator close 包到 finally — break
                # 提前退出时 llm_chunks generator 仍持有 SDK stream,
                # 必须 close() 让 SDK 主动关闭 TCP socket。否则 SDK
                # 继续 drain socket,可能持续计费 + 占用 fd。
                _llm_chunk_iter = iter(llm_chunks)
                _llm_iter_exhausted = False
                while not _llm_iter_exhausted:
                    try:
                        chunk = next(_llm_chunk_iter)
                    except StopIteration:
                        _llm_iter_exhausted = True
                        break

                    # D15 (INTERRUPTED 集成): 中途检查 cancel_event
                    # 若用户在 LLM 流式返回期间按了 Stop,这里提前 break。
                    # stop_reason 留 None 给 chat_completed() 处理,或写入 'interrupted'。
                    if (
                        self._run_state is not None
                        and self._run_state.cancel_event.is_set()
                    ):
                        _logger.info("⏹️ [llm_stream] cancel_event set mid-stream")
                        stop_reason_this_turn = (
                            stop_reason_this_turn or "interrupted"
                        )
                        break

                    # 文本增量 → 转发给 UI
                    if chunk.text_delta:
                        full_text += chunk.text_delta.text
                        yield ("text", chunk.text_delta.text)

                    # 思考过程 → 转发给 UI
                    if chunk.thinking_delta:
                        thinking_text += chunk.thinking_delta.thinking
                        self._pending_thinking += chunk.thinking_delta.thinking
                        yield ("thinking", chunk.thinking_delta.thinking)

                    # 工具调用 → 收集（不立即执行，等本轮 LLM 响应结束）
                    if chunk.tool_call:
                        tool_calls.append(chunk.tool_call)

                    # 终止原因(流末尾 yield 一次)→ 记下本轮的
                    if getattr(chunk, "stop_reason", None):
                        stop_reason_this_turn = chunk.stop_reason

                    # Token 消耗 → 转发给 UI + 回传给 context manager
                    if chunk.usage:
                        # Day 7 改进：保存到 self，供持久化时写入 jsonl
                        self._last_turn_usage = chunk.usage
                        # Task 7 (Path A): 捕获最后一轮 token,供 bridge.on_turn_end 用
                        last_input_tokens = chunk.usage.input_tokens
                        last_output_tokens = chunk.usage.output_tokens
                        yield ("usage", chunk.usage)
                        # 对齐 Claude Code tokenCountWithEstimation：
                        # 用 API 权威数字作增量基准，减少全量估算开销
                        if self.context_manager:
                            self.context_manager.set_baseline(
                                chunk.usage.input_tokens,
                                len(self.messages),
                            )
            except Exception as e:
                # 生成器 yield 过程中异常（如 OpenAI/Anthropic 网络中断、stream context 关闭）
                # 关键修复：之前这种情况会让 run() 主循环爆掉，整个 session 失败
                error_msg = f"LLM 流式响应中断: {type(e).__name__}: {e}"
                _logger.error(error_msg)
                yield ("system", f"❌ {error_msg}")
                yield ("text", f"抱歉，响应被中断：{error_msg}")
                yield ("system", "✅ 回答完成")
                # 保存中断状态到 session（让用户能看到部分内容）
                partial = full_text or f"[响应中断：{e}]"
                self.messages.append({"role": "assistant", "content": partial})
                if self._session_manager:
                    try:
                        self._session_manager.add_assistant_message(
                            partial,
                            usage=asdict(self._last_turn_usage) if self._last_turn_usage else None,
                        )
                    except Exception:
                        pass
                return
            finally:
                # R3 (review 修复): 关 SDK stream — break/exception/正常退出
                # 三种路径都必须 close,否则 TCP socket 泄漏 + 可能继续计费。
                try:
                    llm_chunks.close()
                except Exception as e:
                    _logger.debug(
                        "⚠️ [llm_stream] llm_chunks.close() failed: %s", e
                    )

            # === 日志：LLM 返回的原始内容 ===
            _logger.debug("\n" + "=" * 60)
            _logger.debug(f"📥 【LLM 返回】Turn {turn}/{self.max_turns}")
            _logger.debug("=" * 60)
            if full_text:
                _logger.debug(f"💬 文本输出:\n{full_text}")
            if thinking_text:
                _logger.debug(f"💭 思考过程:\n{thinking_text}")
            if tool_calls:
                _logger.debug(f"\n🔧 工具调用 ({len(tool_calls)} 个):")
                _logger.debug(_format_tool_calls_for_log(tool_calls))

            # Task 7 (Path A): 记录最后一轮 turn 索引 + tool_calls,供 bridge.on_turn_end 用
            last_turn = turn
            last_tool_calls = list(tool_calls)

            # ── 如果没有 tool_call → 最终回答 ───────────────────────
            if not tool_calls:
                # 最终回答已通过 text_delta 流式输出
                # 把 assistant 消息追加到 history
                self.messages.append({
                    "role": "assistant",
                    "content": full_text,
                })
                # Bug 1e:本轮正常收尾,记下这条文本作为 on_turn_end 的 assistant_resp
                # (直接用 full_text,不靠末尾 reversed 扫描,避免误配上一轮)
                final_answer = full_text
                final_stop_reason = stop_reason_this_turn
                # Day 4: 保存最终回答到 session（含本轮累积的全部 thinking/tool_logs）
                if self._session_manager:
                    try:
                        self._session_manager.add_assistant_message(
                            full_text,
                            thinking=self._pending_thinking,
                            tool_logs=self._pending_tool_logs,
                            # Day 7：把本轮 API 返回的 usage 持久化到 jsonl
                            # 下次 F5 刷新后能从这里恢复 baseline，0 跳变
                            usage=asdict(self._last_turn_usage) if self._last_turn_usage else None,
                        )
                        # 保存后重置，避免跨 run 累积
                        self._pending_thinking = ""
                        self._pending_tool_logs = []
                        self._last_turn_usage = None  # Day 7：用完清零
                    except Exception:
                        pass
                yield ("system", "✅ 回答完成")
                break

            # ── 有 tool_call → 执行工具（Day 3：并行执行）─────────────
            # 先把 assistant 的 tool_use blocks 加到 history
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

            self.messages.append({"role": "assistant", "content": assistant_content})

            # Day 3：并行执行所有工具（如果工具之间无依赖）
            # 如果只有一个工具，串行执行更简单
            # 注意：先执行工具，再保存 assistant message（确保 tool_logs 完整）

            # R1 (review 修复): 显式传 cancel_event kwarg 代替 ContextVar
            # (ContextVar 在 ThreadPoolExecutor worker thread 中看不到父 thread set)。
            # hoist 到分支前 — 串行 + 并行 branch 都需要。
            _cancel_evt = (
                self._run_state.cancel_event
                if self._run_state is not None
                else None
            )

            if len(tool_calls) == 1:
                # 单工具：串行执行
                tc = tool_calls[0]
                yield ("tool_call", {"name": tc.tool_name, "input": tc.tool_input, "parallel": False})
                self._pending_tool_logs.append({"type": "action", "name": tc.tool_name, "input": tc.tool_input})

                # M12: 权限检查(对齐 doc §6.3)
                allowed, perm_err, effective_input = self._check_tool_permission(tc.tool_name, tc.tool_input)
                if not allowed:
                    # P1 (UI 弹窗 104ms 老 bug 修复): 检测 AWAITING_PERMISSION marker
                    # 不再走 _wait_for_permission_legacy Event.wait 0.1s 轮询(会
                    # block streamlit 主线程,@st.dialog 永远没机会 render)。
                    # 改为:设 _run_state.awaiting_permission + yield event + break
                    # → streamlit rerun → @st.dialog 渲染 → 用户决策
                    # → resume_after_permission 续 run()。
                    if perm_err == "__AWAITING_PERMISSION__":
                        req = self._pending_permission_request or {
                            "tool_name": tc.tool_name,
                            "tool_input": tc.tool_input,
                            "reason": "",
                            "message": "",
                        }
                        if self._run_state is not None:
                            self._run_state.awaiting_permission = req
                        yield ("awaiting_permission", req)
                        return  # 早退 → streamlit rerun → 弹 dialog
                    tool_output = perm_err or "Permission denied"
                    yield ("tool_result", {
                        "name": tc.tool_name,
                        "output": tool_output,
                        "success": False,
                        "elapsed": 0.0,
                    })
                    self._pending_tool_logs.append({"type": "result", "name": tc.tool_name, "output": tool_output, "success": False})
                    self.messages.append(_make_tool_result_block(tc.tool_use_id, tool_output))
                    self._pending_tool_results.append((tc.tool_use_id, tool_output))
                    # 不 return,继续让 LLM 收到 tool_result 后做下一轮决策
                    continue

                start_time = time.time()
                result = self.tools.execute(
                    tc.tool_name,
                    effective_input,
                    max_retries=3,
                    cancel_event=_cancel_evt,
                )
                elapsed = time.time() - start_time

                if result["status"] == "success":
                    tool_output = result["output"]
                    yield ("tool_result", {
                        "name": tc.tool_name,
                        "output": tool_output,
                        "success": True,
                        "elapsed": elapsed,
                    })
                    self._pending_tool_logs.append({"type": "result", "name": tc.tool_name, "output": tool_output, "success": True})
                else:
                    tool_output = f"工具执行失败: {result['error']}"
                    yield ("tool_result", {
                        "name": tc.tool_name,
                        "output": tool_output,
                        "success": False,
                        "elapsed": elapsed,
                    })
                    self._pending_tool_logs.append({"type": "result", "name": tc.tool_name, "output": tool_output, "success": False})
                    if turn >= self.max_turns:
                        yield ("system", f"⚠️ 工具执行失败且达到最大轮次，结束循环")
                        return

                self.messages.append(_make_tool_result_block(tc.tool_use_id, tool_output))
                self._pending_tool_results.append((tc.tool_use_id, tool_output))
            else:
                # Day 3：多工具并行执行
                tool_names = [tc.tool_name for tc in tool_calls]
                yield ("tool_call", {"names": tool_names, "parallel": True})
                self._pending_tool_logs.append({"type": "parallel_start", "names": tool_names})

                start_time = time.time()

                # M12: 先做权限检查,denied 的 tool 不进入 ThreadPoolExecutor
                pre_results: list[tuple[Any, Optional[dict]]] = []  # [(tc, result_or_None)]
                for tc in tool_calls:
                    allowed, perm_err, effective_input = self._check_tool_permission(tc.tool_name, tc.tool_input)
                    if not allowed:
                        # P1: 同串行分支 — AWAITING_PERMISSION marker 早退
                        if perm_err == "__AWAITING_PERMISSION__":
                            req = self._pending_permission_request or {
                                "tool_name": tc.tool_name,
                                "tool_input": tc.tool_input,
                                "reason": "",
                                "message": "",
                            }
                            if self._run_state is not None:
                                self._run_state.awaiting_permission = req
                            yield ("awaiting_permission", req)
                            return  # 早退 → streamlit rerun → 弹 dialog
                        pre_results.append((tc, {
                            "status": "error",
                            "error": perm_err or "Permission denied",
                        }))
                    else:
                        pre_results.append((tc, None))

                # 用 ThreadPoolExecutor 并行执行(只对 allowed 的)
                with concurrent.futures.ThreadPoolExecutor() as executor:
                    future_to_tc = {}
                    for tc, pre_result in pre_results:
                        if pre_result is not None:
                            # denied → 跳过 execute
                            continue
                        # R2 (review 修复): 显式传 cancel_event 到 worker
                        # thread,且 executor 内部捕获父 thread 的 self._run_state
                        # (闭包)— 不依赖 ContextVar(在 worker 中不传播)。
                        future = executor.submit(
                            self.tools.execute,
                            tc.tool_name,
                            tc.tool_input,
                            max_retries=3,
                            cancel_event=_cancel_evt,  # noqa: F821
                        )
                        future_to_tc[future] = tc

                    # 按提交顺序收集结果（不是完成顺序）
                    results = []
                    for future in concurrent.futures.as_completed(future_to_tc):
                        tc = future_to_tc[future]
                        result = future.result()
                        results.append((tc, result))

                    # 把 denied 的 pre_results 加进去
                    for tc, pre_result in pre_results:
                        if pre_result is not None:
                            results.append((tc, pre_result))

                    # 按提交顺序 yield 结果
                    results.sort(key=lambda x: tool_calls.index(x[0]))

                    for tc, result in results:
                        elapsed = time.time() - start_time

                        if result["status"] == "success":
                            tool_output = result["output"]
                            yield ("tool_result", {
                                "name": tc.tool_name,
                                "output": tool_output,
                                "success": True,
                                "elapsed": elapsed,
                            })
                            self._pending_tool_logs.append({"type": "result", "name": tc.tool_name, "output": tool_output, "success": True})
                        else:
                            tool_output = f"工具执行失败: {result['error']}"
                            yield ("tool_result", {
                                "name": tc.tool_name,
                                "output": tool_output,
                                "success": False,
                                "elapsed": elapsed,
                            })
                            self._pending_tool_logs.append({"type": "result", "name": tc.tool_name, "output": tool_output, "success": False})

                        self.messages.append(_make_tool_result_block(tc.tool_use_id, tool_output))
                        self._pending_tool_results.append((tc.tool_use_id, tool_output))
            if self._session_manager:
                try:
                    # Claude Code 风格：assistant+tool_use 一条 Entry，tool_results 一条 Entry
                    # 1. assistant 消息（包含 text + tool_use blocks）
                    tc_list = [{"id": tc.tool_use_id, "name": tc.tool_name, "input": tc.tool_input} for tc in tool_calls]
                    self._session_manager.add_assistant_with_tools(
                        text=full_text,
                        tool_calls=tc_list,
                    )
                    # 2. tool_results（一条 user Entry 包含所有 tool_result）
                    results = []
                    for tc in tool_calls:
                        for tid, output in self._pending_tool_results:
                            if tid == tc.tool_use_id:
                                results.append({"tool_use_id": tc.tool_use_id, "content": output})
                                break
                    self._session_manager.add_tool_results(results)
                except Exception as e:
                    _logger.warning(f"Failed to save intermediate turn to session: {e}")
                finally:
                    # 关键：无论成功还是失败，都清空本轮结果
                    # 防止 write 异常时本轮 _pending_tool_results 残留污染下一轮 tool_call
                    # （LLM 会因重复 tool_use_id 的 tool_result 报 protocol error）
                    self._pending_tool_results = []
            # 继续下一轮循环（让 LLM 看到 tool_result）

        else:
            # 循环正常结束（没 break）→ 达到 max_turns
            yield ("system", f"⚠️ 达到最大轮次（{self.max_turns}），强制结束")

        # Task 7: run 末尾调 bridge.on_turn_end(取代 Option C 同步提取)
        # 失败不阻断:try/except 兜底,只 log + yield 错误事件
        #
        # Bug 1e 修复(2026-06-24):只在本轮「正常产出完整文本回答」时才提取。
        # - final_answer 为空 → 本轮在工具循环里被 max_turns 截断、或流式中断
        #   (中断路径已提前 return),此时没有本轮的完整回答,跳过提取,
        #   绝不退回去拿 reversed 扫描误配上一轮的回答。
        # - final_stop_reason 命中 max_tokens/length → 回答被 token 上限切断、不完整,
        #   跳过提取(避免把半句话存成记忆)。end_turn/stop/未知 → 视为正常收尾。
        # - user_msg 直接用本 run 的入参 user_message,assistant_resp 用 final_answer,
        #   两者都来自「本轮」,不再做全局 reversed 扫描。
        if (
            self.react_memory_bridge
            and last_turn > 0
            and final_answer
            and user_message
            and final_stop_reason not in _TRUNCATED_STOP_REASONS
        ):
            try:
                for event in self.react_memory_bridge.on_turn_end(
                    user_msg=user_message,
                    assistant_resp=final_answer,
                    turn_index=last_turn,
                    input_tokens=last_input_tokens,
                    output_tokens=last_output_tokens,
                    tool_calls_in_turn=len(last_tool_calls),
                ):
                    yield ("memory_event", event)
            except Exception as e:
                _logger.warning(f"Memory bridge failed: {e}")

        # M10 C2.2 (2026-06-26):L3 SessionMemory extract 触发点
        # turn 结束后把 messages 喂给 sm_layer.extract_incremental
        # 走后台 ThreadPoolExecutor,不阻塞主对话(零延迟)
        #
        # M11.7 (2026-06-28): 对齐 Claude Code SessionMemory 抽取节流
        # 在触发前调 should_extract_now() 走 dual-gate
        # (token Δ ≥ 5K AND tool Δ ≥ 3) OR (token Δ ≥ 5K AND last_turn tool=0)
        # 不通过则完全跳过 extract_incremental
        if self.session_memory is not None and last_turn > 0 and final_answer:
            try:
                _msgs_with_id = self._messages_with_ids()
                # M11.7:dual-gate(token + tool)
                _current_tokens = last_input_tokens + last_output_tokens
                _tool_delta = len(last_tool_calls)
                # last_turn tool 数 = 本轮的 tool_delta(简化:用本轮代替)
                _gate_ok = self.session_memory.should_extract_now(
                    current_token_count=_current_tokens,
                    tool_count_delta=_tool_delta,
                    tool_count_last_turn=_tool_delta,
                )
                if not _gate_ok:
                    _logger.debug(
                        f"[L3 SM extract trigger] gate 拦住,跳过 extract | "
                        f"current_tokens={_current_tokens} tool_delta={_tool_delta}"
                    )
                else:
                    _logger.debug(
                        f"[L3 SM extract trigger] run() 末尾触发 extract_incremental | "
                        f"msgs={len(_msgs_with_id)} last_turn={last_turn} | "
                        f"sm.last_id(before)={self.session_memory.last_compacted_msg_id} | "
                        f"current_tokens={_current_tokens} tool_delta={_tool_delta}"
                    )
                    future = self.session_memory.extract_incremental(
                        _msgs_with_id,
                        llm_callback=None,
                        current_token_count=_current_tokens,
                        tool_count_delta=_tool_delta,
                        tool_count_last_turn=_tool_delta,
                    )
                    # 不 .result() block(后台线程,主流程不等)
                    # 但记录 future 引用,方便测试或后续 block 等待
                    self._pending_sm_extract_future = future
            except Exception as e:
                _logger.warning(
                    f"[L3 SM extract trigger] 失败(不影响主流程): {e}"
                )

        # Day 4: run 结束后 flush session
        if self._session_manager:
            try:
                self._session_manager.flush()
                _logger.debug(f"Session saved: {self._session_manager.session_id}")
            except Exception as e:
                _logger.warning(f"Failed to flush session: {e}")

    # ── 辅助方法 ────────────────────────────────────────────────────

    def _detect_provider(self) -> str:
        """从 llm_router 的 config 判断当前 provider"""
        provider = self.llm.config.provider
        if isinstance(provider, str):
            return provider
        # 如果是枚举
        return str(provider.value) if hasattr(provider, "value") else "anthropic"

    # ── history 管理 ──────────────────────────────────────────────────

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

    def _persist_compacted_messages(self, compacted: list[dict], compact_result):
        """压缩后消息持久化到 JSONL

        对齐 Claude Code buildPostCompactMessages (src/services/compact/compact.ts:325-338)
        顺序: boundary → summary → preserved head (preserved head = compacted 跳过 system 和 summary)

        Claude Code 实际行为 (query.ts:528-535):
            const postCompactMessages = buildPostCompactMessages(compactionResult)
            for (const message of postCompactMessages) {
                yield message  // ← 逐条 yield，sessionStorage 接管落盘
            }

        agent-dev 之前 P0 bug: 只调 add_compact_boundary + add_summary 两方法，
        preserved head 6 条消息永久不写盘，重启后上下文残缺。
        现场: data/sessions/7f071c62.jsonl

        Args:
            compacted: CompactOrchestrator._build_compacted_messages 输出，tuple[list[dict], list[dict]]
                - compacted[0]: [system, summary, ...preserved_head]
                - compacted[1] (recent): 最近 N 条非 system 消息
                结构:
                - [0] system: 动态注入不持久化（每次 run 重新构造）
                - [1] summary: user role + content 以 "[Previous conversation summarized]" 开头
                - [2..] preserved: 最近 N 条原始 user/assistant 消息
            compact_result: CompactionResult 实例
        """
        if not self._session_manager:
            return

        # 直接调 storage 层（不走 manager.add_user_message 等高阶方法）：
        # 1) manager.add_user_message 会触发 _on_user_message 标题生成（不必要的 LLM 调用）
        # 2) preserved head 是历史数据，不需要标题重新生成
        storage = self._session_manager.storage

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

        _logger.debug(f"💾 [Sync] manager._last_uuid: {self._session_manager._last_uuid} → {storage.last_uuid}")

        # 5. P1 修复：同步 manager 的 _last_uuid 到 preserved head 最后一条
        # 为什么需要：storage.add_* 只更新 storage 内部状态，不调 manager.add_user_message 等
        # 高阶方法。manager._last_uuid 仍是压缩前的最后一条（如 838f3b94）。
        # 下次 manager.add_assistant_message/add_user_message 写后续对话时，
        # parent = self._last_uuid 会链到旧链（错位）。
        # 现场: 7f071c62.jsonl 后续 assistant parent 指向 boundary 之前的 user message。
        # 修复后: manager._last_uuid 同步到 storage.last_uuid（preserved head 最后一条）。
        self._session_manager._last_uuid = storage.last_uuid