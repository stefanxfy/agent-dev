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
import time
from dataclasses import asdict
from typing import Any, Dict, List, Optional

from .llm.router import (
    LLMRouter,
    StreamChunk,
    TextDelta,
    ThinkingDelta,
    ToolCallDelta,
    UsageStats,
)
from .tools.base import ToolRegistry

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

# 日志统一走 root handler（由 web/app.py basicConfig 配置）
# agent_core 不再自建 handler，避免 propagate 导致重复输出
_logger.setLevel(logging.DEBUG)  # 自己放开 DEBUG，由 root handler 的 level 控制是否输出


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

    # ── 主循环 ──────────────────────────────────────────────────────

    def run(self, user_message: str):
        """
        执行 ReAct 循环，返回生成器。
        流式 yield 所有中间过程（text / thinking / tool_call / tool_result / usage / system）。
        
        Day 4: 如果启用了 session，run 结束后自动保存到 session。
        """
        # system prompt 不持久化到 JSONL（与 Claude Code 一致：每次 run 动态注入）

        # 追加用户消息（内存）
        self.messages.append({"role": "user", "content": user_message})
        
        # Day 4: 保存 user message 到 session
        if self._session_manager:
            try:
                self._session_manager.add_user_message(user_message)
            except Exception as e:
                _logger.warning(f"Failed to save user message to session: {e}")

        # 重置 pending 状态（每次 run 独立）
        self._pending_thinking = ""
        self._pending_tool_logs = []
        self._pending_tool_results = []

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
                total_tokens = sum(
                    self._estimate_message_tokens(m) for m in self.messages
                )
                tool_count = getattr(self, "cumulative_tool_calls", 0)
                ctx = _TurnContext(
                    messages=self.messages,
                    total_tokens=total_tokens,
                    tool_count=tool_count,
                )
                decision = self.session_memory.should_trigger_compact(ctx)
                _logger.debug(
                    f"SM decision: strategy={decision.strategy}, reason={decision.reason}"
                )
                if decision.strategy == "sm_compact":
                    sm_result = self.session_memory.compact(
                        self.messages,
                        context_window=self.max_context_tokens,
                    )
                    if sm_result is not None:
                        # SM 压缩成功:替换 messages
                        self.messages = [sm_result.summary_message] + list(sm_result.kept_messages)
                        sm_compact_used = True
                        _logger.info(
                            f"SM-compact OK: ~{sm_result.used_tokens_estimate} tokens estimated"
                        )
                        yield (
                            "system",
                            f"📦 [L3 fast path] 上下文已压缩(SM 文件): ~{sm_result.used_tokens_estimate} tokens",
                        )
                    # else: SM 返回 None → fallback 走 ContextManager
                elif decision.strategy == "wait":
                    # 等 extraction 完成(本期简化:记 log,fallback ContextManager)
                    _logger.info(
                        f"SM wait: {decision.reason}, fallback ContextManager"
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

        for turn in range(1, self.max_turns + 1):
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
                        report = self.memory_retriever.search(last_user_msg["content"], top_k=5)
                        hits = report.hits if hasattr(report, "hits") else []
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
                for chunk in llm_chunks:
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
            if len(tool_calls) == 1:
                # 单工具：串行执行
                tc = tool_calls[0]
                yield ("tool_call", {"name": tc.tool_name, "input": tc.tool_input, "parallel": False})
                self._pending_tool_logs.append({"type": "action", "name": tc.tool_name, "input": tc.tool_input})
                
                start_time = time.time()
                result = self.tools.execute(tc.tool_name, tc.tool_input, max_retries=3)
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
                
                # 用 ThreadPoolExecutor 并行执行
                with concurrent.futures.ThreadPoolExecutor() as executor:
                    future_to_tc = {}
                    for tc in tool_calls:
                        future = executor.submit(
                            self.tools.execute,
                            tc.tool_name,
                            tc.tool_input,
                            max_retries=3
                        )
                        future_to_tc[future] = tc

                    # 按提交顺序收集结果（不是完成顺序）
                    results = []
                    for future in concurrent.futures.as_completed(future_to_tc):
                        tc = future_to_tc[future]
                        result = future.result()
                        results.append((tc, result))
                    
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
        if self.react_memory_bridge and last_turn > 0 and len(self.messages) >= 2:
            try:
                # 收集本 run 最后一对的 user/assistant 文本(只取最后一对,避免对历史反复提取)
                last_user = next(
                    (m["content"] for m in reversed(self.messages)
                     if m.get("role") == "user" and isinstance(m.get("content"), str)),
                    None,
                )
                last_assistant = next(
                    (m["content"] for m in reversed(self.messages)
                     if m.get("role") == "assistant" and isinstance(m.get("content"), str)),
                    None,
                )
                if last_user and last_assistant:
                    for event in self.react_memory_bridge.on_turn_end(
                        user_msg=last_user,
                        assistant_resp=last_assistant,
                        turn_index=last_turn,
                        input_tokens=last_input_tokens,
                        output_tokens=last_output_tokens,
                        tool_calls_in_turn=len(last_tool_calls),
                        last_messages=self.messages[-6:],
                        recent_turns=[],  # 简化:从本 run 只产一对,recent_turns 由 bridge 内部维护
                    ):
                        yield ("memory_event", event)
            except Exception as e:
                _logger.warning(f"Memory bridge failed: {e}")

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