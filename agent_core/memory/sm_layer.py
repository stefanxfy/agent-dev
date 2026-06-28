"""
L3 会话内滚动摘要（v2.1 §4.3 + §4.4）

SessionMemoryLayer — 仿照 Claude Code SessionMemory (TypeScript)
核心思想:**Extract vs Compact 分层**
- extract = 后台 LLM 增量更新 SM 文件 (慢路径, 不阻塞主对话)
- compact = 直接读 SM 文件拼成 summary 消息 (快路径, 零 LLM, 毫秒级)

**关键不变量** (v2.1 §4.5):
1. SM 文件永不被重新生成 —— 只通过 Edit 增量更新
2. compact 不调 LLM —— 只读 SM 文件 + 截断,零延迟
3. 已 compact 的消息信息保留在 SM 文件里,不丢
4. extraction 推进 last_compacted_msg_id 前必须成功,失败回滚 (A10)

**5 条回退条件** (§4.4,与 Claude Code `shouldUseSessionMemoryCompaction` 一一对应):
| 条件 | 含义 | 回退策略 |
| --- | --- | --- |
| gate 关 | 用户禁用 SM | 走传统 |
| 无 SM 文件 | 提取还没跑过(短对话) | 走传统 |
| SM 文件过大 | summary 本身超限 | 走传统 |
| extraction 正在跑 | 避免读写冲突 | 等 ≤15s |
| SM-compact 后仍超阈值 | SM 不够精简 | 走传统 |

调用入口:
    sm = SessionMemoryLayer(session_id, sm_path, config)
    decision = sm.should_trigger_compact(ctx)
    if decision.strategy == "sm_compact":
        result = sm.compact(messages, context_window)
        # result.summary_message 拼到 messages[0]
        # result.kept_messages 是 last_compacted_msg_id 之后的消息
"""

from __future__ import annotations

import json
import logging
import re
import threading
from concurrent.futures import Future
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Literal, Optional

from agent_core.exceptions import StorageError
from agent_core.memory.config import CompactConfig
from agent_core.memory.tracing import tracer

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────
# 异常
# ──────────────────────────────────────────────────────────────────

class SessionMemoryError(StorageError):
    """L3 SessionMemory 异常"""
    code = "SESSION_MEMORY"


# ──────────────────────────────────────────────────────────────────
# 数据结构
# ──────────────────────────────────────────────────────────────────

@dataclass
class TurnContext:
    """当前 turn 的上下文信息(用于 should_trigger_compact 决策)"""
    messages: list[dict]              # 当前所有消息
    total_tokens: int = 0             # 当前总 token 数
    tool_count: int = 0               # 已调用的工具次数


@dataclass
class CompactDecision:
    """压缩策略决策

    strategy:
        - sm_compact:   走 L3 SM 文件(零 LLM, 快路径)
        - traditional:  走传统 LLM 压缩(回退)
        - wait:         等 extraction 完成(超时后由 caller 决定)
        - disabled:     功能 gate 关闭,不压缩
    """
    strategy: Literal["sm_compact", "traditional", "wait", "disabled"]
    reason: str
    timeout_ms: int = 0               # 仅 wait 时有意义


@dataclass
class CompactResult:
    """compact() 的结果

    summary_message:
        拼好的 summary 消息(direct 插入到 messages[0] 位置,
        role="user", content 含 SM 文件摘要)

    kept_messages:
        last_compacted_msg_id 之后的原始消息
        (前面的消息信息已在 SM 文件里,不丢)

    used_tokens_estimate:
        压缩后预估总 token 数,用于 caller 决策(若仍超阈值,可再次触发)

    strategy:
        用的 strategy(透传 CompactDecision.strategy,便于 caller 决策)
    """
    summary_message: dict
    kept_messages: list[dict]
    used_tokens_estimate: int
    strategy: str = "sm_compact"


# ──────────────────────────────────────────────────────────────────
# SessionMemoryLayer
# ──────────────────────────────────────────────────────────────────

class SessionMemoryLayer:
    """
    L3 会话内滚动摘要

    用法:
        sm = SessionMemoryLayer(
            session_id="s1",
            sm_path=Path("/data/sessions/s1/sm.md"),
            config=MemoryConfig().compact,
        )

        # 慢路径: 后台 LLM 增量更新 SM 文件
        future = sm.extract_incremental(messages, llm_callback=my_llm_call)

        # 快路径: 触发压缩时直接读 SM 文件
        decision = sm.should_trigger_compact(ctx)
        if decision.strategy == "sm_compact":
            result = sm.compact(messages, context_window=128000)
            new_messages = [result.summary_message] + result.kept_messages
    """

    # ── SM 文件 frontmatter schema(version 1) ──
    SM_SCHEMA_VERSION = 1
    SM_TEMPLATE = """\
---
session_id: {session_id}
schema_version: {schema_version}
last_compacted_msg_id: null
last_compacted_at: null
---

# Session Memory

> 此文件由 L3 SessionMemoryLayer 维护(v2.1 §4.4)。
> - extract 路径: 后台 LLM 增量更新,只 Edit 不重写
> - compact 路径: 直接读此文件 + 按 section 截断,零 LLM 调用
> - 信息永不丢失:每条消息提取后写入对应 section

## Context
<!-- 当前会话目标、约束、已知事实 -->

## Decisions
<!-- 已做的决策(用户偏好 + 系统决策) -->

## Technical
<!-- 技术细节、依赖、API 行为 -->

## Open Questions
<!-- 待澄清的问题 -->

## User Preferences
<!-- 用户偏好(显式 + 隐式) -->
"""

    def __init__(
        self,
        session_id: str,
        sm_path: Path | str,
        config: Optional[CompactConfig] = None,
        llm_callback: Optional[Callable[[str], str]] = None,
    ):
        """
        Args:
            session_id: 会话 ID
            sm_path: SM 文件路径(.md)
            config: CompactConfig 实例(默认走 MemoryConfig().compact 默认值)
            llm_callback: LLM 调用函数 (prompt) → response_text。
                注入到 self 上,extract_incremental 时调用。
                None 表示走测试路径(只推进 last_id,不真更新 sections)。
                生产环境由 caller 注入真实 callback(如 make_sm_extract_callback(router=...))。
        """
        self.session_id = session_id
        self.sm_path = Path(sm_path)
        self.config = config or CompactConfig()
        self._llm_callback = llm_callback

        # 内部状态
        self._extraction_in_progress = False
        self._extraction_lock = threading.Lock()
        self._last_compacted_msg_id: Optional[str] = None
        self._last_compacted_at: Optional[str] = None

        # M11.7 (2026-06-28): 对齐 Claude Code SessionMemory 抽取节流(差异 1+3)
        # _initialized: latch once,会话累计 < minimum_message_tokens_to_init 时不创建 SM 文件
        # _last_extract_token_count: 记录上次抽取的累计 token,用来算 token Δ 门槛
        self._initialized: bool = False
        self._last_extract_token_count: Optional[int] = None

        # 从 frontmatter 恢复 last_compacted_msg_id(如果 SM 文件存在)
        if self.sm_exists():
            self._load_state_from_frontmatter()

        logger.debug(
            f"SessionMemoryLayer.__init__ session_id={self.session_id} | "
            f"sm_path={self.sm_path} has_callback={llm_callback is not None}"
        )

    # ──────────────────────────────────────────────
    # SM 文件 IO
    # ──────────────────────────────────────────────

    def sm_exists(self) -> bool:
        """SM 文件是否存在"""
        return self.sm_path.exists()

    def sm_is_template(self) -> bool:
        """SM 文件是否还是初始 template(占位符未填)

        检测方法:扫描所有 section 内容,如果有非空非占位符的实质内容,说明已被编辑
        """
        if not self.sm_exists():
            return True

        content = self.read_sm() or ""
        # 移除 frontmatter
        body = re.sub(r"^---\n.*?\n---\n", "", content, count=1, flags=re.DOTALL)

        # 检测每个 section 是否只有占位符 `<!-- ... -->` 或为空
        sections = re.split(r"(^## .+$)", body, flags=re.MULTILINE)
        for i in range(1, len(sections), 2):
            header = sections[i]
            body_section = sections[i + 1] if i + 1 < len(sections) else ""
            # 去掉 HTML 注释和空白
            cleaned = re.sub(r"<!--.*?-->", "", body_section, flags=re.DOTALL).strip()
            if cleaned:
                return False
        return True

    def sm_token_count(self) -> int:
        """估算 SM 文件的 token 数(纯文本,启发式)"""
        if not self.sm_exists():
            return 0
        content = self.read_sm() or ""
        # 去掉 frontmatter 和 HTML 注释
        body = re.sub(r"^---\n.*?\n---\n", "", content, count=1, flags=re.DOTALL)
        body = re.sub(r"<!--.*?-->", "", body, flags=re.DOTALL)
        # 简单启发式:中文 0.45 tok/字,英文 0.22 tok/字(与 SimpleTokenCounter 一致)
        chinese = len(re.findall(r"[一-鿿]", body))
        english = len(re.findall(r"[a-zA-Z]", body))
        other = max(0, len(body) - chinese - english)
        return int(chinese * 0.45 + english * 0.22 + other * 0.22)

    def read_sm(self) -> Optional[str]:
        """读 SM 文件完整内容(含 frontmatter)"""
        if not self.sm_exists():
            return None
        try:
            return self.sm_path.read_text(encoding="utf-8")
        except Exception as e:
            raise SessionMemoryError(f"读 SM 文件失败: {self.sm_path}: {e}", cause=e)

    def write_sm_template(self) -> None:
        """初始化 SM 文件(写 template)

        用于首次 extract 前,先占位 template。后续 extract 通过 Edit 增量更新。
        """
        if self.sm_exists():
            return
        content = self.SM_TEMPLATE.format(
            session_id=self.session_id,
            schema_version=self.SM_SCHEMA_VERSION,
        )
        try:
            self.sm_path.parent.mkdir(parents=True, exist_ok=True)
            self.sm_path.write_text(content, encoding="utf-8")
            logger.info(f"SM template 初始化: {self.sm_path}")
        except Exception as e:
            raise SessionMemoryError(f"写 SM template 失败: {e}", cause=e)

    def _load_state_from_frontmatter(self) -> None:
        """从 SM 文件 frontmatter 恢复 last_compacted_msg_id / at"""
        content = self.read_sm() or ""
        m = re.match(r"^---\n(.*?)\n---\n", content, re.DOTALL)
        if not m:
            return
        fm = m.group(1)
        for line in fm.splitlines():
            if ":" not in line:
                continue
            k, _, v = line.partition(":")
            k = k.strip()
            v = v.strip()
            if k == "last_compacted_msg_id":
                self._last_compacted_msg_id = v if v and v != "null" else None
            elif k == "last_compacted_at":
                self._last_compacted_at = v if v and v != "null" else None

    def _apply_sm_operations(self, ops: list[dict]) -> int:
        """把 LLM 返回的 op 列表应用到 SM 文件。

        支持 3 种 op:
        - append: 把 content 追加到 section 末尾
        - replace: 替换整个 section 内容
        - delete: 把 section 置空(写成 <!-- -->)

        策略:
        - ops 为空 → 不动文件,返 0
        - 多个 op 串行应用,每条 op 之后重新 parse 文件,避免 section 位置漂移
        - 任何一条 op 失败 → 跳过该条,继续后续 op(不让一条坏数据毁掉整个 extract)
        - 写盘:原子写(写到 .tmp 再 rename),防止半写状态

        Args:
            ops: 形如 [{"op":"append","section":"Context","content":"..."}, ...]

        Returns:
            int: 成功应用的 op 数量
        """
        if not ops:
            logger.debug("[sm._apply_sm_operations] ops 为空,不动文件")
            return 0

        logger.debug(
            f"[sm._apply_sm_operations] 应用 {len(ops)} 条 op: "
            + ", ".join(f"{o['op']}:{o['section']}" for o in ops)
        )

        applied = 0
        for op in ops:
            try:
                content = self.read_sm() or ""
                section_name = op["section"]
                op_type = op["op"]
                op_content = op.get("content", "")

                # 找 section 的位置(从 ## Context 一直到下一个 ## ... 或文末)
                section_pattern = re.compile(
                    rf"(^## {re.escape(section_name)}\s*\n)(.*?)(?=^## |\Z)",
                    re.MULTILINE | re.DOTALL,
                )
                m = section_pattern.search(content)
                if not m:
                    logger.warning(
                        f"[sm._apply_sm_operations] section 不存在,跳过: {section_name!r}"
                    )
                    continue

                header = m.group(1)
                old_body = m.group(2)
                new_body = self._compute_new_section_body(
                    op_type, old_body, op_content,
                )
                new_content = content[: m.start()] + header + new_body + content[m.end():]

                # 原子写:tmp + rename
                tmp_path = self.sm_path.with_suffix(self.sm_path.suffix + ".tmp")
                tmp_path.write_text(new_content, encoding="utf-8")
                tmp_path.replace(self.sm_path)

                applied += 1
                logger.debug(
                    f"[sm._apply_sm_operations] applied {op_type}:{section_name} "
                    f"(old_body_chars={len(old_body)} new_body_chars={len(new_body)})"
                )
            except Exception as e:
                logger.warning(
                    f"[sm._apply_sm_operations] op 失败,跳过: "
                    f"{op.get('op')}:{op.get('section')} err={e}"
                )
                continue

        logger.debug(f"[sm._apply_sm_operations] 完成,成功 {applied}/{len(ops)} 条")
        return applied

    @staticmethod
    def _compute_new_section_body(op_type: str, old_body: str, new_content: str) -> str:
        """算 section 的新 body(op 应用的纯函数,好测)。"""
        if op_type == "delete":
            # delete → 清空,只留占位符
            return "<!-- -->\n"
        if op_type == "replace":
            # replace → 整个 body 换掉(去掉旧占位符 + 旧内容)
            return f"{new_content.strip()}\n"
        if op_type == "append":
            # append → 追加到末尾;若旧 body 只有占位符,替换占位符
            cleaned_old = re.sub(r"<!--.*?-->", "", old_body, flags=re.DOTALL).strip()
            if cleaned_old:
                return f"{old_body.rstrip()}\n{new_content.strip()}\n"
            return f"{new_content.strip()}\n"
        # 未知 op 类型 → 原样返(防御)
        return old_body

    # ──────────────────────────────────────────────
    # 触发决策(5 条回退条件)
    # ──────────────────────────────────────────────

    def should_trigger_compact(self, ctx: TurnContext) -> CompactDecision:
        """判断是否走 L3 SM-compact

        5 条回退条件(按顺序检查):
        1. gate 关
        2. SM 文件不存在或仍是 template
        3. SM 文件过大(本身超限)
        4. extraction 正在跑
        5. SM-compact 后预估仍超阈值

        Args:
            ctx: 当前 turn 上下文

        Returns:
            CompactDecision(strategy, reason, timeout_ms)
        """
        # ── DEBUG L3:入口快照 + 触发条件 0 ──
        logger.debug(
            f"[sm.should_trigger] ENTER session_id={self.session_id} "
            f"sm_path={self.sm_path} | "
            f"ctx: msgs={len(ctx.messages)} total_tokens={ctx.total_tokens} tool_count={ctx.tool_count} | "
            f"config: enabled={self.config.enabled} "
            f"sm_token_threshold={self.config.sm_token_threshold} "
            f"tool_count_threshold={self.config.tool_count_threshold} "
            f"max_sm_tokens_for_compact={self.config.max_sm_tokens_for_compact} "
            f"sm_insufficient_buffer_ratio={self.config.sm_insufficient_buffer_ratio}"
        )

        # 0. 基础触发条件(token > 阈值 OR tool > 阈值)
        trigger_by_token = ctx.total_tokens >= self.config.sm_token_threshold
        trigger_by_tool = ctx.tool_count >= self.config.tool_count_threshold
        if not (trigger_by_token or trigger_by_tool):
            decision = CompactDecision(
                strategy="traditional",
                reason=f"未达触发阈值(token={ctx.total_tokens} < {self.config.sm_token_threshold}, "
                       f"tool={ctx.tool_count} < {self.config.tool_count_threshold})",
            )
            logger.debug(
                f"[sm.should_trigger] COND 0 not triggered (token={trigger_by_token} tool={trigger_by_tool}) "
                f"→ {decision.strategy} ({decision.reason})"
            )
            return decision

        # 1. gate 关
        if not self.config.enabled:
            decision = CompactDecision(strategy="traditional", reason="gate_disabled")
            logger.debug(f"[sm.should_trigger] COND 1 gate_disabled → {decision.strategy}")
            return decision

        # 2. SM 文件不存在或还是 template
        sm_exists = self.sm_exists()
        sm_is_tpl = self.sm_is_template() if sm_exists else None
        if not sm_exists or sm_is_tpl:
            decision = CompactDecision(strategy="traditional", reason="no_sm_file")
            logger.debug(
                f"[sm.should_trigger] COND 2 no_sm_file (exists={sm_exists} is_template={sm_is_tpl}) "
                f"→ {decision.strategy}"
            )
            return decision

        # 3. SM 文件过大
        sm_tokens = self.sm_token_count()
        if sm_tokens > self.config.max_sm_tokens_for_compact:
            decision = CompactDecision(
                strategy="traditional",
                reason=f"sm_too_large({sm_tokens} > {self.config.max_sm_tokens_for_compact})",
            )
            logger.debug(
                f"[sm.should_trigger] COND 3 sm_too_large "
                f"sm_tokens={sm_tokens} > max={self.config.max_sm_tokens_for_compact} "
                f"→ {decision.strategy}"
            )
            return decision

        # 4. extraction 正在跑 → 等
        if self._extraction_in_progress:
            decision = CompactDecision(
                strategy="wait",
                reason="extract_running",
                timeout_ms=self.config.extraction_wait_timeout_ms,
            )
            logger.debug(
                f"[sm.should_trigger] COND 4 extract_running "
                f"timeout_ms={decision.timeout_ms} → {decision.strategy}"
            )
            return decision

        # 5. SM-compact 后预估仍超阈值
        #    用 sm_token_threshold 作为目标线,buffer_ratio 留余量
        projected = self._estimate_post_compact_tokens(ctx, sm_tokens)
        threshold = self.config.sm_token_threshold * self.config.sm_insufficient_buffer_ratio
        if projected >= threshold:
            decision = CompactDecision(
                strategy="traditional",
                reason=f"sm_insufficient(projected={projected} >= {threshold:.0f})",
            )
            logger.debug(
                f"[sm.should_trigger] COND 5 sm_insufficient "
                f"sm_tokens={sm_tokens} kept_tokens={projected - sm_tokens - 50} "
                f"overhead=50 projected={projected} threshold={threshold:.0f} "
                f"→ {decision.strategy}"
            )
            return decision

        # 所有检查通过 → 走 SM-compact
        decision = CompactDecision(strategy="sm_compact", reason="ok")
        logger.debug(
            f"[sm.should_trigger] PASS all conditions → {decision.strategy} | "
            f"sm_tokens={sm_tokens} projected_post_compact={projected} "
            f"threshold={threshold:.0f} buffer={self.config.sm_insufficient_buffer_ratio}"
        )
        return decision

    def _estimate_post_compact_tokens(self, ctx: TurnContext, sm_tokens: int) -> int:
        """预估 SM-compact 后的总 token 数

        公式: sm_tokens(替换早期消息) + kept_messages_tokens(last_id 之后的)
        """
        # kept_messages 的 token 数(粗估:启发式)
        kept = self._slice_kept_messages(ctx.messages)
        kept_tokens = self._estimate_messages_tokens(kept)
        # SM summary 消息本身的 overhead(约 50 tokens:role + framing)
        return sm_tokens + kept_tokens + 50

    def _slice_kept_messages(self, messages: list[dict]) -> list[dict]:
        """根据 last_compacted_msg_id 切出保留的消息"""
        if self._last_compacted_msg_id is None:
            return messages
        try:
            idx = next(
                i for i, m in enumerate(messages)
                if m.get("id") == self._last_compacted_msg_id
            )
            return messages[idx + 1:]
        except StopIteration:
            # last_id 不在 messages 里 → 全保留
            return messages

    def _estimate_messages_tokens(self, messages: list[dict]) -> int:
        """估算消息列表的 token 数(启发式,无 LLM)

        修复记录(2026-06-21):旧实现 chinese/english 跨消息累积,other 用
        ``len(content) - chinese - english`` 计算,其中 chinese/english 是
        累积值,导致第二条消息起 other 变负数,total token 错算为负数。
        修正为按消息独立统计 chinese/english/other 再累加。
        """
        chinese = english = other = 0
        for msg in messages:
            content = msg.get("content", "")
            if isinstance(content, str):
                msg_chinese = len(re.findall(r"[一-鿿]", content))
                msg_english = len(re.findall(r"[a-zA-Z]", content))
                msg_other = len(content) - msg_chinese - msg_english
                chinese += msg_chinese
                english += msg_english
                other += msg_other
            # 每条消息 role overhead(5 tokens,与 SimpleTokenCounter 一致)
        return int(
            chinese * 0.45
            + english * 0.22
            + other * 0.22
            + len(messages) * 5
        )

    # ──────────────────────────────────────────────
    # 快路径: compact(零 LLM)
    # ──────────────────────────────────────────────

    def compact(
        self,
        messages: list[dict],
        context_window: int,
    ) -> Optional[CompactResult]:
        """
        触发压缩(零 LLM)

        1. 读 SM 文件
        2. 按 section 截断到 max_per_section_chars
        3. 拼 summary 消息
        4. 返回 kept_messages(last_id 之后)

        Args:
            messages: 当前所有消息
            context_window: 模型的 context window 大小(用于日志记录)

        Returns:
            CompactResult 或 None(SM 文件不可用 → 让 caller 走传统)
        """
        with tracer.start_as_current_span("memory.sm.compact") as span:
            span.set_attribute("memory.sm.input_count", len(messages))
            span.set_attribute("memory.sm.context_window", context_window)

            # ── DEBUG L3:compact 入口 ──
            logger.debug(
                f"[sm.compact] ENTER session_id={self.session_id} | "
                f"input_count={len(messages)} context_window={context_window} | "
                f"last_compacted_msg_id={self._last_compacted_msg_id} | "
                f"sm_path={self.sm_path}"
            )

            if not self.sm_exists() or self.sm_is_template():
                span.set_attribute("memory.sm.result", "no_sm_file")
                logger.debug(
                    f"[sm.compact] EXIT early: SM file missing or template "
                    f"(exists={self.sm_exists()} is_template={self.sm_is_template() if self.sm_exists() else 'N/A'})"
                )
                return None

            # 1. 读 SM 文件 + 截断
            sm_content = self.read_sm() or ""
            sm_tokens_before = self.sm_token_count()
            truncated = self._truncate_sections(sm_content, self.config.max_per_section_chars)
            truncated_tokens = self._estimate_post_compact_tokens(
                TurnContext(messages=messages, total_tokens=0, tool_count=0),
                sm_tokens_before,
            )
            logger.debug(
                f"[sm.compact] STEP 1 read+truncate: "
                f"sm_file_chars={len(sm_content)} sm_tokens={sm_tokens_before} | "
                f"after_truncate_chars={len(truncated)} max_per_section_chars={self.config.max_per_section_chars}"
            )

            # 2. 拼 summary 消息
            summary_message = {
                "role": "user",
                "content": (
                    f"[Session memory summary]\n\n"
                    f"The following is a condensed summary of our session so far. "
                    f"Full SM file: {self.sm_path}\n\n"
                    f"{truncated}"
                ),
            }
            logger.debug(
                f"[sm.compact] STEP 2 built summary_message: "
                f"role={summary_message['role']!r} content_chars={len(summary_message['content'])}"
            )

            # 3. kept_messages + token 估算
            kept_messages = self._slice_kept_messages(messages)
            used_tokens_estimate = (
                self.sm_token_count()
                + self._estimate_messages_tokens(kept_messages)
                + 50  # summary overhead
            )
            logger.debug(
                f"[sm.compact] STEP 3 slice kept: "
                f"input_count={len(messages)} kept_count={len(kept_messages)} "
                f"dropped={len(messages) - len(kept_messages)} (last_id={self._last_compacted_msg_id}) | "
                f"used_tokens_estimate={used_tokens_estimate} "
                f"(sm={self.sm_token_count()} + kept={self._estimate_messages_tokens(kept_messages)} + 50)"
            )

            result = CompactResult(
                summary_message=summary_message,
                kept_messages=kept_messages,
                used_tokens_estimate=used_tokens_estimate,
                strategy="sm_compact",
            )

            # 4. 持久化
            self._persist_compact_result(result)
            logger.debug(
                f"[sm.compact] STEP 4 persist_compact_result done "
                f"(frontmatter last_compacted_at 推进)"
            )

            span.set_attribute("memory.sm.kept_count", len(result.kept_messages))
            span.set_attribute("memory.sm.used_tokens_estimate", result.used_tokens_estimate)
            span.set_attribute("memory.sm.strategy", result.strategy)
            logger.debug(
                f"[sm.compact] EXIT success: "
                f"kept={len(result.kept_messages)}/{len(messages)} "
                f"used_tokens_estimate={result.used_tokens_estimate} "
                f"context_window={context_window} "
                f"compression_ratio={used_tokens_estimate / max(1, len(messages) * 100):.2f}"
            )
            return result

    def _persist_compact_result(self, result: CompactResult) -> None:
        """M10 C2.2: 持久化 compact 结果

        写两个东西:
        1. 更新 .md frontmatter(last_compacted_at)
        2. 写 .json 文件(snapshot,供 C2.3 distiller 跨会话读)

        Args:
            result: compact() 返回的 CompactResult
        """
        now_iso = datetime.now(timezone.utc).isoformat()

        # 1. 更新 .md frontmatter(只更新 last_compacted_at,body 不动)
        try:
            if self.sm_exists():
                content = self.read_sm() or ""
                if "last_compacted_at:" in content:
                    new_content = re.sub(
                        r"(last_compacted_at:\s*)([^\n]+|\n)?",
                        lambda m: f"last_compacted_at: {now_iso}\n",
                        content,
                        count=1,
                    )
                else:
                    # 插在 closing `---` 之后
                    new_content = re.sub(
                        r"(---\n)",
                        f"---\nlast_compacted_at: {now_iso}\n",
                        content,
                        count=1,
                    )
                self.sm_path.write_text(new_content, encoding="utf-8")
                self._last_compacted_at = now_iso
                logger.debug(f"SM .md frontmatter 更新: {self.sm_path}")
        except Exception as e:
            logger.warning(f"更新 SM .md frontmatter 失败: {e}")

        # 2. 写 .json snapshot
        try:
            json_path = self.sm_path.with_suffix(".json")
            json_path.parent.mkdir(parents=True, exist_ok=True)
            json_path.write_text(
                json.dumps({
                    "session_id": self.session_id,
                    "summary_message_content": result.summary_message.get("content", ""),
                    "kept_messages_count": len(result.kept_messages),
                    "used_tokens_estimate": result.used_tokens_estimate,
                    "strategy": result.strategy,
                    "updated_at": now_iso,
                }, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            logger.debug(f"SM .json snapshot 写入: {json_path}")
        except Exception as e:
            logger.warning(f"写 SM .json 失败: {e}")

    def _truncate_sections(self, sm: str, max_per_section: int) -> str:
        """按 `## Section` 切,每个 section 单独截断到 max_per_section 字符

        与 §4.3 设计一致:每个 section 单独截断,总文件大小无硬上限
        """
        # 保留 frontmatter 不变
        m = re.match(r"^(---\n.*?\n---\n)", sm, re.DOTALL)
        frontmatter = m.group(1) if m else ""
        body = sm[len(frontmatter):] if frontmatter else sm

        sections = re.split(r"(^## .+$)", body, flags=re.MULTILINE)
        result: list[str] = []
        # sections[0] 是第一个 header 之前的内容(若有)
        if sections and sections[0].strip():
            result.append(sections[0].rstrip())
        for i in range(1, len(sections), 2):
            header = sections[i]
            body_section = sections[i + 1] if i + 1 < len(sections) else ""
            if len(body_section) > max_per_section:
                body_section = (
                    body_section[:max_per_section]
                    + "\n\n[... truncated for brevity ...]"
                )
            result.append(header + body_section)
        return frontmatter + "\n\n".join(result).lstrip()

    # ──────────────────────────────────────────────
    # 慢路径: extract(LLM,后台)
    # ──────────────────────────────────────────────

    def extract_incremental(
        self,
        messages: list[dict],
        llm_callback: Optional[Callable[[str], str]] = None,
        current_token_count: int = 0,
        tool_count_delta: int = 0,
        tool_count_last_turn: int = 0,
    ) -> Future:
        """
        后台增量更新 SM 文件(慢路径,LLM 调用)

        1. 取 last_compacted_msg_id 之后的新消息
        2. 构造 extract prompt
        3. 调用 LLM 让它 Edit SM 文件(不重写!)
        4. 成功后推进 last_compacted_msg_id

        Args:
            messages: 当前所有消息
            llm_callback: LLM 调用函数(msg) → response
                          (测试时可传 mock;实际生产用 LangGraphAgent)
            current_token_count: 当前会话累计 token 数(M11.7 接入 Claude Code 节流 gate)
                0 = 绕过 gate(向后兼容旧测试)
                >0 = 走 _should_extract() dual-gate
            tool_count_delta: 本次调用的 tool 数(M11.7 gate 的 AND 分支)
            tool_count_last_turn: 上一轮的 tool 数(0 时放行的 OR 分支)

        Returns:
            Future[bool]: True 表示成功推进 last_id
            False 表示 gate 拦住(未触发实际抽取,future 仍 resolve 为 False)
        """
        from concurrent.futures import ThreadPoolExecutor

        future: Future = Future()

        # ── DEBUG L3:extract 入口 ──
        logger.debug(
            f"[sm.extract_incremental] ENTER session_id={self.session_id} | "
            f"input_count={len(messages)} last_id={self._last_compacted_msg_id} | "
            f"has_callback={llm_callback is not None} "
            f"in_progress_before={self._extraction_in_progress} "
            f"current_token_count={current_token_count} tool_delta={tool_count_delta} "
            f"tool_last_turn={tool_count_last_turn}"
        )

        # M11.7 (2026-06-28): Claude Code SessionMemory dual-gate
        # current_token_count=0 → 绕过 gate(向后兼容旧测试,行为不变)
        if current_token_count > 0:
            gate_ok, gate_reason = self._should_extract(
                current_token_count, tool_count_delta, tool_count_last_turn
            )
            if not gate_ok:
                logger.debug(
                    f"[sm.extract_incremental] GATE 拦住: reason={gate_reason} "
                    f"(skips extract, no file write, no last_id advance)"
                )
                future.set_result(False)
                return future

        def _runner() -> None:
            with self._extraction_lock:
                self._extraction_in_progress = True
            logger.debug(f"[sm.extract._runner] thread start, in_progress=True")
            try:
                self._do_extract(
                    messages, llm_callback, current_token_count=current_token_count
                )
                future.set_result(True)
                logger.debug(f"[sm.extract._runner] thread done, set_result(True)")
            except Exception as e:
                logger.warning(f"sm extract failed: {e}")
                future.set_exception(e)
            finally:
                with self._extraction_lock:
                    self._extraction_in_progress = False
                logger.debug(f"[sm.extract._runner] thread cleanup, in_progress=False")

        # 后台 thread 跑(daemon,不阻塞主线程)
        executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="sm-extract")
        executor.submit(_runner)
        executor.shutdown(wait=False)
        logger.debug(
            f"[sm.extract_incremental] EXIT: thread submitted, future returned to caller "
            f"(主线程不阻塞)"
        )
        return future

    def _should_extract(
        self,
        current_token_count: int,
        tool_count_delta: int,
        tool_count_last_turn: int,
    ) -> tuple[bool, str]:
        """M11.7 (2026-06-28): 对齐 Claude Code SessionMemory 抽取节流(差异 1+3)

        双重门:
        1. 初始化门槛:会话累计 token < minimum_message_tokens_to_init → 不创建 SM 文件
        2. 增量门槛(token Δ + tool Δ dual-gate):
           - token Δ < minimum_tokens_between_update → 跳过
           - met_tokens AND met_tools → 抽
           - met_tokens AND last_turn tool=0 → 抽(纯文本对话的 OR 旁路)
           - 否则 → 跳过

        Returns:
            (decision: bool, reason: str)
            reason 形如 "init" / "below_init_threshold" / "below_token_delta" /
            "below_tool_or_last_turn" / "ok"
        """
        # 1. 初始化门槛(latch once)
        if not self._initialized:
            if current_token_count < self.config.minimum_message_tokens_to_init:
                logger.debug(
                    f"[sm._should_extract] 初始化门槛拦住: "
                    f"current={current_token_count} < "
                    f"min_init={self.config.minimum_message_tokens_to_init}"
                )
                return (False, "below_init_threshold")
            self._initialized = True
            logger.debug(
                f"[sm._should_extract] 初始化门槛通过(写 SM 文件): "
                f"current={current_token_count} >= min_init={self.config.minimum_message_tokens_to_init}"
            )
            return (True, "init")

        # 2. 增量门槛 — token Δ
        token_delta = current_token_count - (self._last_extract_token_count or 0)
        if token_delta < self.config.minimum_tokens_between_update:
            logger.debug(
                f"[sm._should_extract] token Δ 门槛拦住: "
                f"token_delta={token_delta} < min={self.config.minimum_tokens_between_update} "
                f"(current={current_token_count} last={self._last_extract_token_count})"
            )
            return (False, "below_token_delta")

        # 3. dual-gate: (token AND tools) OR (token AND last_turn_tool=0)
        met_tokens = token_delta >= self.config.minimum_tokens_between_update
        met_tools = tool_count_delta >= self.config.tool_calls_between_updates
        last_turn_no_tools = tool_count_last_turn == 0

        if (met_tokens and met_tools) or (met_tokens and last_turn_no_tools):
            logger.debug(
                f"[sm._should_extract] dual-gate 通过: "
                f"token_delta={token_delta} (>= {self.config.minimum_tokens_between_update}) "
                f"tool_delta={tool_count_delta} (>= {self.config.tool_calls_between_updates}? {met_tools}) "
                f"last_turn_tools={tool_count_last_turn} (OR 旁路: {last_turn_no_tools})"
            )
            return (True, "ok")

        logger.debug(
            f"[sm._should_extract] dual-gate 拦住: token Δ OK 但 "
            f"tool_delta={tool_count_delta} < {self.config.tool_calls_between_updates} "
            f"且 last_turn_tools={tool_count_last_turn} > 0"
        )
        return (False, "below_tool_or_last_turn")

    def should_extract_now(
        self,
        *,
        current_token_count: int,
        tool_count_delta: int,
        tool_count_last_turn: int,
    ) -> bool:
        """M11.7 (2026-06-28): 公开 API,供 agent_core.run() 判定本轮要不要触发后台抽取

        内部委托给 _should_extract()(返回 decision + reason 元组),
        这里只返 bool + 简化日志,便于 caller 调用。

        Args:
            current_token_count: 当前会话累计 token 数(input + output)
            tool_count_delta: 本轮 tool 调用次数
            tool_count_last_turn: 上一轮 tool 调用次数(0 时放行 OR 旁路)
        """
        decision, reason = self._should_extract(
            current_token_count, tool_count_delta, tool_count_last_turn
        )
        logger.debug(
            f"[sm.should_extract_now] decision={decision} reason={reason} "
            f"current={current_token_count} tool_delta={tool_count_delta} "
            f"last_turn={tool_count_last_turn}"
        )
        return decision

    def _do_extract(
        self,
        messages: list[dict],
        llm_callback: Optional[Callable[[str], str]],
        current_token_count: int = 0,
    ) -> None:
        """实际跑 extract 的内部方法

        注意:这里不直接调 LLM,而是构造 prompt + 调用 callback
        让 caller(M5+ 集成)提供具体的 LLM 实现 + Edit 工具

        M11.7: 如果 current_token_count > 0,跑完后更新 _last_extract_token_count
        """
        logger.debug(
            f"[sm._do_extract] ENTER session_id={self.session_id} | "
            f"input_count={len(messages)} sm_exists={self.sm_exists()} "
            f"current_token_count={current_token_count}"
        )
        logger.debug(
            f"[sm._do_extract] ENTER session_id={self.session_id} | "
            f"input_count={len(messages)} sm_exists={self.sm_exists()}"
        )

        if not self.sm_exists():
            logger.debug(f"[sm._do_extract] SM file 不存在,写 template → {self.sm_path}")
            self.write_sm_template()
            logger.debug(f"[sm._do_extract] template 写完")

        new_messages = self._slice_kept_messages(messages)
        logger.debug(
            f"[sm._do_extract] slice: input={len(messages)} new={len(new_messages)} "
            f"dropped_by_last_id={len(messages) - len(new_messages)} "
            f"last_id={self._last_compacted_msg_id}"
        )
        if not new_messages:
            logger.debug("sm._do_extract: 无新消息,跳过")
            return

        # 构造 prompt
        sm_content = self.read_sm() or ""
        prompt = self._build_extract_prompt(sm_content, new_messages)
        logger.debug(
            f"[sm._do_extract] prompt built: sm_chars={len(sm_content)} new_msgs={len(new_messages)} "
            f"prompt_chars={len(prompt)}"
        )

        # M11.5 (2026-06-27): 优先用 self._llm_callback(注入的实例),参数 callback 向后兼容
        cb = llm_callback or self._llm_callback
        if cb is None:
            # 无 callback → 仅推进 last_id,不实际调 LLM(测试路径)
            logger.debug("sm._do_extract: 无 llm_callback,仅推进 last_id(测试路径)")
        else:
            # 有 callback → 真实调 LLM,解析响应,应用到 SM sections
            from .sm_callback import call_sm_extract
            from .sm_prompts import parse_sm_response

            sm_full_text = self.read_sm() or ""
            import time as _t
            _t0 = _t.time()
            logger.debug(
                f"[sm._do_extract] STEP LLM: 调 callback(prompt) (prompt_chars={len(prompt)})..."
            )
            try:
                response = call_sm_extract(
                    cb,
                    sm_full_text=sm_full_text,
                    new_messages=new_messages,
                    last_compacted_msg_id=self._last_compacted_msg_id,
                )
            except Exception as e:
                logger.warning(
                    f"[sm._do_extract] callback 抛异常,不更新 sections(保持 last_id 不推进): {e}"
                )
                return  # 不推进 last_id,保留数据下次重试
            _llm_ms = (_t.time() - _t0) * 1000
            logger.debug(
                f"[sm._do_extract] STEP LLM done ({_llm_ms:.1f}ms) "
                f"response_chars={len(response)}"
            )
            # ── DEBUG L3:LLM 真实响应(前 500 字符)──
            logger.debug(
                f"[sm._do_extract] LLM response (前 500 chars):\n"
                f"{response[:500]}{'...[truncated]' if len(response) > 500 else ''}"
            )
            # 解析 ops + 写到 sm.md
            ops = parse_sm_response(response)
            logger.debug(
                f"[sm._do_extract] 解析出 {len(ops)} 条 op: "
                + (", ".join(f"{o['op']}:{o['section']}" for o in ops) or "(空)")
            )
            applied = self._apply_sm_operations(ops)
            logger.debug(f"[sm._do_extract] 成功应用 {applied}/{len(ops)} 条 op 到 sm.md")

        # 推进 last_compacted_msg_id(不变量 #4:推进前必须成功)
        if new_messages:
            last_msg = new_messages[-1]
            old_id = self._last_compacted_msg_id
            self._last_compacted_msg_id = last_msg.get("id")
            logger.debug(
                f"[sm._do_extract] 推进 last_compacted_msg_id: {old_id} → {self._last_compacted_msg_id} "
                f"(last_msg.role={last_msg.get('role')!r} content_chars={len(str(last_msg.get('content','')))})"
            )
            # M11.7: gate 跑通后,更新 token 锚点(给下次 gate 算 Δ)
            if current_token_count > 0:
                self._last_extract_token_count = current_token_count
                logger.debug(
                    f"[sm._do_extract] 更新 _last_extract_token_count={current_token_count} "
                    f"(下次 gate 的 token Δ 基准)"
                )

    def _build_extract_prompt(self, current_sm: str, new_messages: list[dict]) -> str:
        """构造 extract prompt(给 LLM 用,带 Edit 工具调用)"""
        new_text = "\n".join(
            f"[{m.get('role', 'user')}] {m.get('content', '')}"
            for m in new_messages
        )
        prompt = (
            f"# Session Memory Extract Task\n\n"
            f"## Current SM file\n\n{current_sm}\n\n"
            f"## New messages to integrate\n\n{new_text}\n\n"
            f"## Task\n\n"
            f"Use the Edit tool to incrementally update the SM file. "
            f"Do NOT rewrite the file — only add new information to appropriate sections. "
            f"Preserve existing content unless it's been contradicted.\n"
        )
        # ── DEBUG L3:prompt 内容(给看 LLM 实际看到什么)──
        logger.debug(
            f"[sm._build_extract_prompt] built prompt: chars={len(prompt)} "
            f"new_messages_count={len(new_messages)} sm_content_chars={len(current_sm)}"
        )
        return prompt

    # ──────────────────────────────────────────────
    # 状态查询(供 caller 决策)
    # ──────────────────────────────────────────────

    @property
    def extraction_in_progress(self) -> bool:
        """是否有 extraction 在跑(供 caller 检查 SM 文件读写冲突)"""
        return self._extraction_in_progress

    @property
    def last_compacted_msg_id(self) -> Optional[str]:
        """最后被 compact 过的消息 ID"""
        return self._last_compacted_msg_id


__all__ = [
    "SessionMemoryLayer",
    "SessionMemoryError",
    "CompactDecision",
    "CompactResult",
    "TurnContext",
]