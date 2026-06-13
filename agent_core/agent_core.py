"""
ReAct Agent — 手写 ReAct 循环（支持工具调用 + 流式输出）
Anthropic 显式风格：Thought → Action → Observation → Thought → Final Answer

Day 3 改进：
- History 管理（Token 预算截断）
- 并行工具调用（ThreadPoolExecutor）
- 错误处理完善（网络超时、API 限流）
- Debug 日志输出（便于观察 ReAct 过程）

Day 4 改进：
- SessionManager 融合：可选 session_id 实现历史持久化
- 自动从 session 加载历史（Resume语义）
- 每次交互后自动保存到 session
- 保持向后兼容（不传 session_id = 纯内存模式）
"""

from __future__ import annotations

import concurrent.futures
import json
import logging
import time
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

# ── Debug 日志配置 ───────────────────────────────────────────────

# 创建 logger（使用单例模式防止重复配置）
_logger = logging.getLogger("react_agent")

# 防重复：检查是否已有同名 StreamHandler（最可靠的方式）
_has_stream_handler = any(
    isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler)
    for h in _logger.handlers
)
if not _has_stream_handler:
    # 先清除所有旧 handler（包括热重载残留的）
    for h in list(_logger.handlers):
        try:
            _logger.removeHandler(h)
        except Exception:
            pass
    
    _logger.setLevel(logging.DEBUG)
    
    handler = logging.StreamHandler()
    handler.setLevel(logging.DEBUG)
    formatter = logging.Formatter(
        "[%(asctime)s] %(message)s",
        datefmt="%H:%M:%S"
    )
    handler.setFormatter(formatter)
    _logger.addHandler(handler)
    
    # 标记已配置
    _logger._configured = True


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
        max_context_tokens: int = 100_000,  # Day 3 新增：Token 预算（默认 100K）
        session_id: Optional[str] = None,   # Day 4 新增：会话 ID（可选）
        session_data_dir: Optional[str] = None,  # Day 4 新增：session 数据目录
    ):
        self.llm = llm_router
        self.tools = tool_registry
        self.max_turns = max_turns
        self.max_context_tokens = max_context_tokens
        self.history: list[dict] = []  # LLM messages 格式
        
        # P2 新增：从 LLMConfig 读取 system_prompt
        self.system_prompt = self.llm.config.system_prompt
        
        # ── Day 4: SessionManager 融合 ──────────────────────────────
        self._session_manager: Optional["SessionManager"] = None
        if session_id:
            from .session.manager import SessionManager
            self._session_manager = SessionManager(
                session_id=session_id,
                data_dir=session_data_dir,
            )
            # 从 session 加载历史（Resume 语义：只加载断链后的消息）
            self.history = self._session_manager.get_messages_for_llm()
            _logger.info(f"Session loaded: {session_id}, {len(self.history)} messages")

        # ── 流式过程中记录 thinking/tool_logs，用于 session 持久化 ───
        self._pending_thinking: str = ""
        self._pending_tool_logs: list = []
        self._pending_tool_results: list = []  # [(tool_use_id, output), ...]

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

    def _trim_history(self):
        """
        Day 3 新增：按 Token 预算截断 history。
        保留：系统消息 + 最近的消息
        策略：从最老的消息开始删，直到 Token 预算内
        """
        if not self.history:
            return

        # 估算当前总 Token
        total_tokens = sum(self._estimate_message_tokens(m) for m in self.history)
        
        if total_tokens <= self.max_context_tokens:
            return  # 不需要截断

        # 需要截断：保留最近 80% 的消息（按 Token 计）
        target_tokens = int(self.max_context_tokens * 0.8)
        
        # 从最老的消息开始删
        while total_tokens > target_tokens and len(self.history) > 2:
            removed = self.history.pop(0)
            total_tokens -= self._estimate_message_tokens(removed)

    def _save_to_session(self):
        """Day 4: 将当前 history 保存到 session（如果启用了 session）"""
        if self._session_manager is None:
            return
        try:
            # 追加所有未保存的消息到 session
            for msg in self.history:
                role = msg.get("role", "")
                if role == "user":
                    content = msg.get("content", "")
                    if isinstance(content, str):
                        self._session_manager.add_user_message(content)
                    else:
                        # tool_results 包在 user 的 content 数组里
                        self._session_manager.storage.add_message(
                            "user", entry_type="user",
                            message=msg,
                            parent_uuid=self._session_manager._last_uuid,
                        )
                elif role == "assistant":
                    self._session_manager.add_assistant_message(message=msg)
                elif role == "tool":
                    # tool 角色转为 Claude Code 风格 user Entry
                    self._session_manager.storage.add_message(
                        "user", entry_type="user",
                        message={"role": "user", "content": [{
                            "type": "tool_result",
                            "tool_use_id": msg.get("tool_call_id", ""),
                            "content": msg.get("content", ""),
                        }]},
                        parent_uuid=self._session_manager._last_uuid,
                    )
            self._session_manager.flush()
        except Exception as e:
            _logger.warning(f"Failed to save to session: {e}")

    # ── 主循环 ──────────────────────────────────────────────────────

    def run(self, user_message: str):
        """
        执行 ReAct 循环，返回生成器。
        流式 yield 所有中间过程（text / thinking / tool_call / tool_result / usage / system）。
        
        Day 4: 如果启用了 session，run 结束后自动保存到 session。
        """
        # system prompt 不持久化到 JSONL（与 Claude Code 一致：每次 run 动态注入）

        # 追加用户消息（内存）
        self.history.append({"role": "user", "content": user_message})
        
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

        # Day 3：检查 Token 预算，必要时截断 history
        self._trim_history()

        for turn in range(1, self.max_turns + 1):
            yield ("system", f"🔄 Turn {turn}/{self.max_turns}")

            # ── 准备发送给 LLM 的 messages ──────────────────────────
            messages_for_llm = list(self.history)
            
            # P2 新增：如果有 system_prompt，添加到消息开头
            if self.system_prompt:
                # 检查是否已经有 system message
                has_system = any(m.get("role") == "system" for m in messages_for_llm)
                if not has_system:
                    messages_for_llm.insert(0, {"role": "system", "content": self.system_prompt})

            # ── 调用 LLM（流式）─────────────────────────────────────
            tool_schemas = self.tools.list_schemas(provider=self._detect_provider())

            # 收集本轮响应中的 tool_call chunks
            tool_calls = []  # list of ToolCallDelta
            full_text = ""
            thinking_text = ""

            # === 日志：发送给 LLM 的原始消息 ===
            _logger.info("\n" + "=" * 60)
            _logger.info(f"📤 【发送给 LLM】Turn {turn}/{self.max_turns}")
            _logger.info("=" * 60)
            _logger.info(_format_messages_for_log(messages_for_llm))
            if tool_schemas:
                _logger.info(f"\n📋 可用工具: {[t['name'] for t in tool_schemas]}")

            try:
                llm_chunks = self.llm.chat(
                    messages=messages_for_llm,
                    tools=tool_schemas or None,
                )
            except Exception as e:
                # LLM 调用异常，优雅降级
                error_msg = f"LLM 调用失败: {type(e).__name__}: {e}"
                _logger.error(error_msg)
                yield ("system", f"❌ {error_msg}")
                yield ("text", f"抱歉，遇到了技术问题无法回答：{error_msg}")
                yield ("system", "✅ 回答完成")
                self.history.append({"role": "assistant", "content": f"抱歉，遇到了技术问题：{e}"})
                # Day 4: 保存到 session
                if self._session_manager:
                    try:
                        self._session_manager.add_assistant_message(f"抱歉，遇到了技术问题：{e}")
                    except Exception:
                        pass
                return

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

                # Token 消耗 → 转发给 UI
                if chunk.usage:
                    yield ("usage", chunk.usage)

            # === 日志：LLM 返回的原始内容 ===
            _logger.info("\n" + "=" * 60)
            _logger.info(f"📥 【LLM 返回】Turn {turn}/{self.max_turns}")
            _logger.info("=" * 60)
            if full_text:
                _logger.info(f"💬 文本输出:\n{full_text}")
            if thinking_text:
                _logger.info(f"💭 思考过程:\n{thinking_text}")
            if tool_calls:
                _logger.info(f"\n🔧 工具调用 ({len(tool_calls)} 个):")
                _logger.info(_format_tool_calls_for_log(tool_calls))

            # ── 如果没有 tool_call → 最终回答 ───────────────────────
            if not tool_calls:
                # 最终回答已通过 text_delta 流式输出
                # 把 assistant 消息追加到 history
                self.history.append({
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
                        )
                        # 保存后重置，避免跨 run 累积
                        self._pending_thinking = ""
                        self._pending_tool_logs = []
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

            self.history.append({"role": "assistant", "content": assistant_content})

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

                self.history.append(_make_tool_result_block(tc.tool_use_id, tool_output))
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

                        self.history.append(_make_tool_result_block(tc.tool_use_id, tool_output))
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
                    # 清空本轮结果
                    self._pending_tool_results = []
                except Exception as e:
                    _logger.warning(f"Failed to save intermediate turn to session: {e}")
            # 继续下一轮循环（让 LLM 看到 tool_result）

        else:
            # 循环正常结束（没 break）→ 达到 max_turns
            yield ("system", f"⚠️ 达到最大轮次（{self.max_turns}），强制结束")

        # Day 4: run 结束后 flush session
        if self._session_manager:
            try:
                self._session_manager.flush()
                _logger.info(f"Session saved: {self._session_manager.session_id}")
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
        if self._session_manager:
            try:
                self._session_manager.close()
            except Exception as e:
                _logger.warning(f"Agent.close() failed: {e}")

    def reset(self):
        """重置会话历史"""
        self.history.clear()
        # Day 4: 同时清空 session
        if self._session_manager:
            try:
                self._session_manager.clear()
            except Exception:
                pass

    def load_history(self, history: list[dict]):
        self.history = list(history)

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

    def add_compact_boundary(self):
        """在当前 history 中添加压缩边界标记（用于触发压缩）"""
        if self._session_manager:
            self._session_manager.add_compact_boundary()

    def get_session_manager(self) -> Optional["SessionManager"]:
        """获取 SessionManager 实例（用于高级操作）"""
        return self._session_manager