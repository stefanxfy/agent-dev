"""
CompactOrchestrator — 压缩编排器
参考：Claude Code src/services/compact/compact.ts
适配：删除 Forked Agent / cache_control，保留 PTL 防御

LLMRouter.chat() 是同步生成器（非 async），本模块保持同步接口。
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Optional

from agent_core.config import config

logger = logging.getLogger("context.compact")

# DEBUG 日志辅助函数
def _truncate(text: str, max_len: int = 200) -> str:
    """截断长文本避免刷屏"""
    if not text:
        return ""
    if len(text) <= max_len:
        return text
    return text[:max_len] + f"... [+{len(text) - max_len} chars]"


def _debug_box(title: str, content: str, max_width: int = 80):
    """DEBUG 打印一个带标题的盒子"""
    lines = content.split('\n')
    box = f"\n{'=' * max_width}\n"
    box += f"🔍 {title}\n"
    box += f"{'=' * max_width}\n"
    for line in lines:
        if len(line) > max_width:
            box += f"  {line[:max_width - 5]}...\n"
        else:
            box += f"  {line}\n"
    box += f"{'=' * max_width}"
    logger.debug(box)


# ── 常量 ────────────────────────────────────────────────────────

# PTL 防御：剥洋葱策略，最多重试 N 次
MAX_PTL_RETRIES = 3

# 每次剥掉最旧的 20% 消息
TRUNCATE_RATIO = 0.2

# 压缩后保留最近 N 条原始消息
PRESERVED_HEAD_MESSAGES = 6

# 工具结果截断上限（字符数）
TOOL_RESULT_TRUNCATE_CHARS = 8000

# 压缩请求最大输出 tokens
COMPACT_MAX_OUTPUT_TOKENS = 4096


# ── 压缩 Prompt ─────────────────────────────────────────────────

COMPACT_SYSTEM_PROMPT = """⚠️ CRITICAL: Respond with TEXT ONLY. Do NOT call any tools.

- Do NOT use Read, Bash, Grep, Glob, Edit, Write, or ANY other tool.
- You already have all the context you need in the conversation above.
- Tool calls will be REJECTED and will waste your only turn — you will fail the task.
- Your entire response must be plain text: an <analysis> block followed by a <summary> block.
- Use 中文 in your summary (本项目是中文场景).

你是对话摘要生成器。你的任务是为一个被压缩的会话创建详细摘要，以供后续 context 延续使用。

Before providing your final summary, wrap your analysis in <analysis> tags to organize your thoughts and ensure you've covered all necessary points. In your analysis process:

1. Chronologically analyze each message and section of the conversation. For each section thoroughly identify:
   - The user's explicit requests and intents
   - Your approach to addressing the user's requests
   - Key decisions, technical concepts and code patterns
   - Specific details like:
     - file names
     - full code snippets
     - function signatures
     - file edits
   - Errors that you ran into and how you fixed them
   - Pay special attention to specific user feedback that you received, especially if the user told you to do something differently.
2. Double-check for technical accuracy and completeness, addressing each required element thoroughly.

Your summary should include the following sections (中文友好, 4 段结构 = Claude Code 9 段 + 中文场景融合):

1. 用户目标 (Primary Request and Intent): Capture all of the user's explicit requests and intents in detail
2. 关键决策 (Key Technical Concepts + Files and Code Sections): List all important technical concepts, technologies, frameworks discussed. Enumerate specific files and code sections examined, modified, or created. Include full code snippets where applicable and include a summary of why this file read or edit is important.
3. 当前状态 (Current Work + Errors and fixes): Describe in detail precisely what was being worked on immediately before this summary request, paying close attention to the most recent messages from both user and assistant. List all errors that you ran into, and how you fixed them. Pay special attention to specific user feedback that you received, especially if the user told you to do something differently.
4. 待办事项 (Pending Tasks + All user messages): List ALL user messages that are not tool results (these are critical for understanding the users' feedback and changing intent). Outline any pending tasks.

防漂移规则 (重要):
- 用户消息必须逐字引用 (verbatim quotes), 不要改写
- Next Step 必须与用户最近显式请求直接相关
- 不要捡起旧的已完成任务

Here's an example of how your output should be structured:

<example>
<analysis>
用户是一位中文母语者，在一次会话中依次提出了多个独立任务：数学计算、搜索、历史问答、以及反复要求将指定段落逐字重复指定次数。最后几条消息是问候和自我介绍，但尚未获得助手回复。
关键决策是：用户明确要求"一次性调用三个工具，不要逐个调用"，助手照做了。
需要防漂移的点：后续可能误以为"我是小白"是名字重点，但实际只是闲聊，应避免在摘要中拾起未完成的"重复请求"等已完成任务。
</analysis>
<summary>
1. 用户目标:
   用户依次要求并行执行三项任务（数学计算+搜索）、获取历史知识（秦始皇）、以及多次重复指定文字段落（LangChain介绍 1次×50、《活着》结尾共7次×50/40/50/10/10/10/5）。最后发送了问候和自我介绍。

2. 关键决策:
   - 首次请求即要求"一次性调用三个工具，不要逐个调用"，助手按要求并行执行
   - 用户多次反复要求重复同一段《活着》文字，重复次数从 50 逐步降到 5
   - 未涉及具体技术框架或代码文件

3. 当前状态:
   所有计算、搜索、文字重复任务均已完成。最后三条用户消息（"你好"、"我是小白"、"我是小黑"）尚未获得助手回复。
   未遇到错误。

4. 待办事项:
   用户消息:
   - "你好"
   - "我是小白"
   - "我是小黑"
   待回应用户最新的自我介绍消息"我是小黑"（以及可能的"我是小白"）。无其他未完成的显式任务请求。
</summary>
</example>

⚠️ REMINDER: Do NOT call any tools. Respond with plain text only — an <analysis> block followed by a <summary> block. Tool calls will be rejected and you will fail the task.
"""

COMPACT_USER_PROMPT_TEMPLATE = """Your task is to create a detailed summary of the conversation so far, paying close attention to the user's explicit requests and your previous actions.

This summary should be thorough in capturing technical details, code patterns, and architectural decisions that would be essential for continuing development work without losing context.

对话内容：
{conversation}

⚠️ REMINDER: Do NOT call any tools. Respond with plain text only — an <analysis> block followed by a <summary> block. Tool calls will be rejected and you will fail the task.
"""


# ── 压缩结果 ────────────────────────────────────────────────────

COMPACT_FORK_PROMPT = """Please summarize the conversation above.

Follow the same format as your system prompt instructions:
1. <analysis> — free-form thinking about what matters
2. <summary> — the actual summary

Key rules:
- Quote user messages verbatim (do not paraphrase)
- Next Step must relate to the user's most recent request
- Do NOT pick up old completed tasks
- Be specific about technical details, code patterns, and decisions
"""


@dataclass
class CompactionResult:
    """压缩结果"""
    success: bool
    summary: str
    compacted_messages: list[dict]
    tokens_before: int
    tokens_after: int
    error: Optional[str] = None
    compact_time_ms: float = 0
    ptl_retries: int = 0
    usage_stats: Optional[UsageStats] = None
    fork_fallback: bool = False  # True = Fork 失败后由旧模式兜底

    @property
    def tokens_freed(self) -> int:
        return max(self.tokens_before - self.tokens_after, 0)

    def summary_str(self) -> str:
        """生成可读的结果摘要"""
        if not self.success:
            return f"Compact failed: {self.error}"
        fb = " (Fork降级)" if self.fork_fallback else ""
        return (
            f"Compact{fb} OK: {self.tokens_before:,} → {self.tokens_after:,} tokens "
            f"(freed {self.tokens_freed:,}), "
            f"{len(self.compacted_messages)} messages, "
            f"{self.compact_time_ms:.0f}ms, "
            f"PTL retries: {self.ptl_retries}"
        )


# ── 压缩编排器 ─────────────────────────────────────────────────

class CompactOrchestrator:
    """
    压缩编排器

    职责：
    1. 消息预处理（脱水：截断长工具结果、移除图片占位）
    2. 构建压缩 prompt
    3. 调用 LLM 生成摘要（同步）
    4. PTL 防御（压缩请求超限时剥洋葱重试）
    5. 组装压缩后的消息列表

    不负责：
    - 状态追踪与重建（agent-dev 当前不需要）
    - Prompt Cache 管理（GLM 不支持 cache_control）
    - Forked Agent（依赖 cache_control，不适用）

    用法：
        compactor = CompactOrchestrator(llm_router, budget_manager, token_counter)
        result = compactor.compact(messages)
        if result.success:
            messages = result.compacted_messages
    """

    def __init__(self, llm_router, budget_manager, token_counter):
        """
        Args:
            llm_router: LLMRouter 实例（同步 chat 接口）
            budget_manager: ContextBudgetManager 实例
            token_counter: SimpleTokenCounter 实例
        """
        self.llm = llm_router
        self.budget = budget_manager
        self.token_counter = token_counter

    def compact(
        self,
        messages: list[dict],
        parent_system: Optional[str] = None,
        parent_tools: Optional[list[dict]] = None,
        parent_messages: Optional[list[dict]] = None,
    ) -> CompactionResult:
        """
        执行压缩

        流程：
        1. 预处理消息（脱水）
        2. 构建压缩 prompt
        3. 调 LLM 生成摘要（含 PTL 防御）
        4. 组装压缩后消息

        Fork 模式（仿照 Claude Code runForkedAgent）：
        当 parent_system + parent_tools 传入时，使用 Fork 模式：
        - system prompt = 主 agent 的 system prompt（字节级一致，命中 cache）
        - tools = 主 agent 的 tools schema
        - messages = 主对话全部 messages + 摘要指令
        - cache 命中率预期 80-95%

        未传入时（向后兼容）：
        - system prompt = COMPACT_SYSTEM_PROMPT（短）
        - tools = None
        - messages = 仅 2 条（system + prompt）
        - cache 命中率 0%
        """
        start = time.time()
        tokens_before = self.token_counter.count_messages(messages)

        # ── DEBUG: 压缩起点 ────────────────────────────────
        logger.info(
            f"🔧 [Compact START] messages={len(messages)}, "
            f"tokens_before={tokens_before:,}"
        )
        logger.debug(
            f"  ├─ First msg: {messages[0].get('role', '?')} "
            f"content={_truncate(str(messages[0].get('content', ''))[:80])}"
        )
        logger.debug(
            f"  └─ Last msg: {messages[-1].get('role', '?')} "
            f"content={_truncate(str(messages[-1].get('content', ''))[:80])}"
        )

        try:
            # 1. 预处理
            preprocessed = self._preprocess(messages)
            logger.debug(
                f"📦 [Preprocess] {len(messages)} → {len(preprocessed)} messages"
            )

            # 2-3. 生成摘要（含 PTL 防御）
            summary, ptl_retries, fork_fallback, usage_stats = self._generate_summary_with_ptl(
                preprocessed,
                parent_system=parent_system,
                parent_tools=parent_tools,
                parent_messages=parent_messages,
            )
            cached = 0
            if usage_stats:
                ptd = getattr(usage_stats, 'prompt_tokens_details', None)
                if isinstance(ptd, dict):
                    cached = ptd.get('cached_tokens', 0)
            logger.debug(
                f"🔧 [Compact] usage_stats: input={usage_stats.input_tokens if usage_stats else 'N/A'}, "
                f"cached={cached}"
            )

            if not summary:
                raise ValueError("LLM 返回空摘要")

            # 4. 组装压缩后消息
            compacted, recent = self._build_compacted_messages(summary, messages)

            tokens_after = self.token_counter.count_messages(compacted)
            elapsed = (time.time() - start) * 1000

            self.budget.record_compact_success()

            # recent 直接来自 _build_compacted_messages，不再依赖 len(compacted)-2 公式
            logger.info(
                f"🔧 [Compact DONE] {tokens_before:,} → {tokens_after:,} tokens "
                f"(freed {tokens_before - tokens_after:,}), "
                f"PTL retries: {ptl_retries}, {elapsed:.0f}ms, "
                f"preserved_head={len(recent)}"
            )
            logger.debug(
                f"  └─ Final structure: [system] + [summary] + "
                f"[{len(recent)} preserved head]"
            )

            return CompactionResult(
                success=True,
                summary=summary,
                compacted_messages=compacted,
                tokens_before=tokens_before,
                tokens_after=tokens_after,
                compact_time_ms=elapsed,
                ptl_retries=ptl_retries,
                usage_stats=usage_stats,
                fork_fallback=fork_fallback,
            )

        except Exception as e:
            logger.error(f"❌ [Compact FAILED] {e}")
            self.budget.record_compact_failure()
            elapsed = (time.time() - start) * 1000
            return CompactionResult(
                success=False,
                summary="",
                compacted_messages=messages,
                tokens_before=tokens_before,
                tokens_after=tokens_before,
                error=str(e),
                compact_time_ms=elapsed,
            )

    # ── 消息预处理 ──────────────────────────────────────────────

    def _preprocess(self, messages: list[dict]) -> list[dict]:
        """
        消息脱水：

        1. 截断超长工具结果（> TOOL_RESULT_TRUNCATE_CHARS 字符）
        2. 移除图片/文档内容，替换为占位符
        3. 跳过 thinking blocks
        4. 保留消息结构
        """
        result = []

        for msg in messages:
            content = msg.get("content", "")

            if isinstance(content, str):
                result.append(msg)

            elif isinstance(content, list):
                new_blocks = []
                for block in content:
                    if not isinstance(block, dict):
                        new_blocks.append(block)
                        continue

                    btype = block.get("type", "text")

                    if btype == "text":
                        new_blocks.append(block)

                    elif btype in ("image", "document"):
                        # 替换为文本占位
                        new_blocks.append({
                            "type": "text",
                            "text": f"[{btype} content removed for compression]"
                        })

                    elif btype == "tool_use":
                        # 保留 tool_use 结构（让 LLM 知道调了什么工具）
                        new_blocks.append(block)

                    elif btype == "tool_result":
                        # 截断超长结果
                        rc = block.get("content", "")
                        if isinstance(rc, list):
                            text_parts = []
                            for item in rc:
                                if isinstance(item, dict) and item.get("type") == "text":
                                    text_parts.append(item.get("text", ""))
                            text = "\n".join(text_parts)
                        elif isinstance(rc, str):
                            text = rc
                        else:
                            text = str(rc)

                        if len(text) > TOOL_RESULT_TRUNCATE_CHARS:
                            text = text[:TOOL_RESULT_TRUNCATE_CHARS] + "\n... [truncated]"

                        new_blocks.append({
                            "type": "text",
                            "text": f"[RESULT] {text}"
                        })

                    elif btype == "thinking":
                        # thinking block 不发给摘要 LLM
                        pass

                    else:
                        new_blocks.append(block)

                result.append({**msg, "content": new_blocks})

            else:
                result.append(msg)

        return result

    # ── 摘要生成（含 PTL 防御）──────────────────────────────────

    def _generate_summary_with_ptl(
        self,
        messages: list[dict],
        parent_system: Optional[str] = None,
        parent_tools: Optional[list[dict]] = None,
        parent_messages: Optional[list[dict]] = None,
    ) -> tuple[str, int, bool]:
        """
        生成摘要，含 PTL（Prompt-Too-Long）防御

        如果压缩请求本身触发 PTL 错误，
        逐层截断最旧的消息分组，最多重试 MAX_PTL_RETRIES 次。

        参考：Claude Code compact.ts 的 PTL retry loop

        Fork 模式：
        parent_system/parent_tools/parent_messages 传入时，
        LLM 调用复用主 agent 的 cache-key params，
        messages = [...parent_messages, summaryRequest]，
        仿照 Claude Code runForkedAgent。

        Fork 失败时降级（FORK_FALLBACK）：
        当 Fork 模式触发非 PTL 错误（如网络超时、API 报错、模型不支持），
        自动降级到旧模式重试，保证压缩不因 Fork 而完全失败。

        Returns:
            (summary, attempt_count, fork_fallback)
            fork_fallback=True 表示 Fork 模式失败后由旧模式兜底成功
        """
        to_summarize = messages
        last_error = ""

        for attempt in range(MAX_PTL_RETRIES + 1):
            # Fork 模式：parent_messages 已包含完整对话，不需要重复塞文本
            # 仿照 Claude Code compact.ts 的 summaryRequest：
            # 只发一条简短的摘要指令，LLM 从上下文 messages 中读取对话
            if parent_system is not None and parent_messages is not None:
                prompt = COMPACT_FORK_PROMPT
            else:
                conversation_text = self._messages_to_text(to_summarize)
                prompt = COMPACT_USER_PROMPT_TEMPLATE.format(
                    conversation=conversation_text
                )

            # ── DEBUG: 每轮 LLM 调用起点 ────────────────────────────
            logger.debug(
                f"🤖 [LLM Call] attempt={attempt + 1}/{MAX_PTL_RETRIES + 1}, "
                f"to_summarize={len(to_summarize)} msgs, "
                f"prompt_len={len(prompt)} chars"
            )
            logger.debug(
                f"  ├─ Conversation preview:\n{_truncate(prompt, 300)}"
            )

            try:
                summary, raw_text, usage_stats = self._call_llm_for_summary(
                    prompt,
                    parent_system=parent_system,
                    parent_tools=parent_tools,
                    parent_messages=parent_messages,
                )
                if summary:
                    # ── DEBUG: 提取成功 ─────────────────────────────
                    logger.debug(
                        f"  ├─ LLM raw output ({len(raw_text)} chars):\n"
                        f"{_truncate(raw_text, 500)}"
                    )
                    logger.debug(
                        f"  └─ Extracted summary ({len(summary)} chars):\n"
                        f"{_truncate(summary, 500)}"
                    )
                    # Fork 模式成功（非降级）
                    return summary, attempt, False, usage_stats  # (summary, retries, fork_fallback, usage_stats)

                # 空响应，可能是 LLM 异常
                last_error = "LLM returned empty summary"
                logger.warning(
                    f"⚠️  [Empty Summary] attempt {attempt + 1}, "
                    f"raw_output={_truncate(raw_text, 200)}"
                )

            except Exception as e:
                last_error = str(e)
                err_lower = last_error.lower()

                # 检测 PTL 错误关键词
                is_ptl = any(kw in err_lower for kw in [
                    "prompt too long",
                    "context length",
                    "maximum context",
                    "too many tokens",
                    "token limit",
                    "输入过长",
                ])

                # 非 PTL 错误：Fork 模式降级重试
                use_fork = (
                    parent_system is not None
                    and parent_messages is not None
                )
                if use_fork and not is_ptl:
                    logger.warning(
                        f"⚠️  [Fork FAILED] non-PTL error, "
                        f"falling back to legacy mode: {e}"
                    )
                    # Fork 降级：清空 Fork 参数，用旧模式兜底
                    parent_system = None
                    parent_tools = None
                    parent_messages = None
                    to_summarize = messages  # 恢复完整消息，不截断
                    # 用旧模式再跑一轮
                    conversation_text = self._messages_to_text(to_summarize)
                    prompt = COMPACT_USER_PROMPT_TEMPLATE.format(
                        conversation=conversation_text
                    )
                    try:
                        summary, raw_text, usage_stats = self._call_llm_for_summary(
                            prompt,
                            parent_system=None,
                            parent_tools=None,
                            parent_messages=None,
                        )
                        if summary:
                            logger.info(
                                f"✅ [Fork Fallback OK] legacy mode succeeded, "
                                f"summary={len(summary)} chars"
                            )
                            return summary, attempt + 1, True, usage_stats  # fork_fallback=True
                        last_error = "Legacy mode also returned empty summary"
                    except Exception as fallback_error:
                        # 旧模式也失败了，不再重试，直接抛出原始错误
                        logger.error(
                            f"❌ [Fork FALLBACK FAILED] legacy mode also errored: "
                            f"{fallback_error}"
                        )
                        raise fallback_error from e

                if is_ptl and attempt < MAX_PTL_RETRIES:
                    truncate_count = max(
                        1, int(len(to_summarize) * TRUNCATE_RATIO)
                    )
                    # 保留 system（第一条），截断其后最旧的消息
                    if len(to_summarize) > truncate_count + 2:
                        to_summarize = (
                            to_summarize[:1]  # system
                            + to_summarize[truncate_count + 1:]  # 去掉最旧的
                        )
                    logger.warning(
                        f"🥝 [PTL Retry {attempt + 1}/{MAX_PTL_RETRIES}] "
                        f"truncated {truncate_count} oldest messages, "
                        f"{len(to_summarize)} remaining, "
                        f"error={_truncate(last_error, 100)}"
                    )
                    continue

                # 非 PTL 错误或重试用完，抛出
                logger.error(f"❌ [LLM Call FAILED] {e}")
                raise

        raise ValueError(
            f"PTL defense exhausted after {MAX_PTL_RETRIES} retries: {last_error}"
        )

    def _call_llm_for_summary(
        self,
        prompt: str,
        parent_system: Optional[str] = None,
        parent_tools: Optional[list[dict]] = None,
        parent_messages: Optional[list[dict]] = None,
    ) -> tuple[str, str]:
        """
        调用 LLM 生成摘要

        LLMRouter.chat() 返回同步生成器（StreamChunk），
        需要消费生成器收集文本。

        Fork 模式（仿照 Claude Code runForkedAgent）：
        当 parent_system + parent_messages 传入时：
        - messages = [主对话 messages...] + [摘要指令 user message]
        - system = parent_system（字节级一致，命中 prompt cache）
        - tools = parent_tools（字节级一致，命中 prompt cache）
        - cache 命中率预期 80-95%

        未传入时（向后兼容）：
        - messages = [COMPACT_SYSTEM_PROMPT, prompt]
        - tools = None
        - cache 命中率 0%

        Returns:
            (extracted_summary, raw_llm_output)
        """
        use_fork = parent_system is not None and parent_messages is not None

        if use_fork:
            # ── Fork 模式 ──────────────────────────────────
            # 仿照 Claude Code forkedAgent.ts:524
            # initialMessages = [...forkContextMessages, ...promptMessages]
            forked_messages = list(parent_messages) + [
                {"role": "user", "content": prompt}
            ]

            logger.info(
                f"🔀 [Fork Compact] Using parent cache: "
                f"system={len(parent_system)} chars, "
                f"tools={len(parent_tools) if parent_tools else 0}, "
                f"parent_msgs={len(parent_messages)}, "
                f"total_msgs={len(forked_messages)}"
            )

            chunks = self.llm.chat(
                messages=forked_messages,
                tools=parent_tools,
                system_prompt_override=parent_system,
            )
        else:
            # ── 原模式（向后兼容）──────────────────────────
            chunks = self.llm.chat(
                messages=[
                    {"role": "system", "content": COMPACT_SYSTEM_PROMPT},
                    {"role": "user", "content": prompt},
                ],
                tools=None,
            )

        full_text = ""
        last_usage = None
        for chunk in chunks:
            if chunk.text_delta:
                full_text += chunk.text_delta.text
            if chunk.usage:
                last_usage = chunk.usage
            # 忽略 thinking/tool_call chunks

        summary = self._extract_summary(full_text)


        return summary, full_text, last_usage

    # ── 辅助方法 ────────────────────────────────────────────────

    def _messages_to_text(self, messages: list[dict]) -> str:
        """将消息列表转为可读文本格式"""
        lines = []

        for msg in messages:
            role = msg.get("role", "user").upper()
            content = msg.get("content", "")

            if isinstance(content, str):
                lines.append(f"[{role}]\n{content}")

            elif isinstance(content, list):
                parts = []
                for block in content:
                    if not isinstance(block, dict):
                        continue

                    btype = block.get("type", "text")

                    if btype == "text":
                        parts.append(block.get("text", ""))

                    elif btype == "tool_use":
                        name = block.get("name", "unknown")
                        inp = json.dumps(
                            block.get("input", {}),
                            ensure_ascii=False
                        )[:200]
                        parts.append(f"[TOOL: {name}] {inp}")

                    elif btype == "tool_result":
                        rc = block.get("content", "")
                        if isinstance(rc, list):
                            text_parts = []
                            for item in rc:
                                if isinstance(item, dict) and item.get("type") == "text":
                                    text_parts.append(item.get("text", ""))
                            text = "\n".join(text_parts)
                        elif isinstance(rc, str):
                            text = rc
                        else:
                            text = str(rc)
                        parts.append(f"[RESULT] {text[:500]}")

                lines.append(f"[{role}]\n" + "\n".join(parts))

        return "\n\n---\n\n".join(lines)

    def _extract_summary(self, text: str) -> str:
        """从 LLM 响应中提取摘要"""
        if not text:
            return ""

        # <summary> 标签优先
        if "<summary>" in text:
            start = text.find("<summary>") + len("<summary>")
            end = text.find("</summary>")
            if end > start:
                summary = text[start:end].strip()
                logger.debug(
                    f"  🏷️  [Extract] <summary> 标签提取成功, "
                    f"len={len(summary)} chars"
                )
                return summary
            else:
                logger.debug(
                    f"  🏷️  [Extract] <summary> 开标签有但缺闭标签, "
                    f"fallback 到 <analysis>"
                )

        # <analysis> 标签兜底（包含 analysis + 后续内容）
        if "<analysis>" in text:
            start = text.find("<analysis>")
            summary = text[start:].strip()
            logger.debug(
                f"  🏷️  [Extract] <analysis> 标签提取, "
                f"len={len(summary)} chars"
            )
            return summary

        # 纯文本兜底
        logger.debug(
            f"  🏷️  [Extract] 无 XML 标签, 纯文本兜底, "
            f"len={len(text)} chars"
        )
        return text.strip()

    def _build_compacted_messages(
        self,
        summary: str,
        original: list[dict],
        preserved_head: Optional[int] = None,
    ) -> list[dict]:
        """
        组装压缩后的消息列表

        结构：[system] + [summary message] + [最近 N 条原始消息]

        参考 Claude Code 的压缩后消息结构：
        [System 边界宣告] + [精简摘要] + [最近对话]

        策略：
        1. 保留原始消息中的 system prompt（第一条）
        2. 插入摘要消息（role=user，标记为之前对话的摘要）
        3. 保留最近 N 条非 system 消息
        """
        result = []

        # 1. 提取 system prompt
        non_system = []
        for msg in original:
            if msg.get("role") == "system":
                result.append(msg)
            else:
                non_system.append(msg)

        # 2. 插入摘要
        result.append({
            "role": "user",
            "content": f"[Previous conversation summarized]\n\n{summary}"
        })

        # 3. 保留最近 N 条消息
        head_count = preserved_head if preserved_head is not None else config.int("PRESERVED_HEAD_MESSAGES", 6)
        recent = non_system[-head_count:] if len(non_system) > head_count else non_system
        result.extend(recent)

        # ── DEBUG: 构建结果 ─────────────────────────────────────
        logger.debug(
            f"🏗️  [Build Compacted] {len(original)} → {len(result)} messages"
        )
        logger.debug(
            f"  ├─ [0] system: "
            f"{_truncate(str(result[0].get('content', ''))[:80])}"
        )
        logger.debug(
            f"  ├─ [1] summary: {len(summary)} chars, "
            f"prefix={summary[:30]!r}..."
        )
        # DEBUG: 直接用 recent 列表，不依赖 result[2:]（可能有多个 system msg）
        logger.debug(f"  └─ recent ({len(recent)} msgs):")
        for i, msg in enumerate(recent):
            role = msg.get("role", "?")
            content = str(msg.get("content", ""))[:60].replace("\n", " ")
            logger.debug(f"      [{i+2}] {role}: {content!r}")

        return result, recent
