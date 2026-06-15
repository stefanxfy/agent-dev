"""
SessionManager - 会话管理 Facade
聚合 session 模块所有组件的统一入口

架构：
```
SessionManager
├── storage: SessionStorage      # JSONL 持久化
├── metadata: SessionMetadata    # 元数据
├── state: SessionState          # 状态机
├── progress: ProgressTracker    # 进度追踪
├── (cleanup: SessionCleanup)   # 清理（静态工具类）
└── title: TitleState 状态机     # 标题生成（两阶段 + 防乱序）
```

提供统一的会话管理 API：
- 创建 / 切换 / 删除 / Fork 会话
- 读写消息和元数据
- 状态监控和事件回调
- 标题自动生成（第1条 + 第3条消息触发，异步 fire-and-forget）
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import re
import threading
import uuid
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Literal, Optional

from .storage import SessionStorage
from .metadata import SessionMetadata
from .state import SessionState, SessionStatus, RequiresActionDetails
from .progress import ProgressTracker
from .cleanup import SessionCleanup

logger = logging.getLogger("session.manager")


# ── 标题状态机 ──────────────────────────────────────────────────────

class TitleState(Enum):
    """标题生成状态机

    状态转移：
    NEED_TITLE ──(消息1)──→ AI_PENDING ──(AI返回)──→ AI_SET ──(消息3)──→ AI_PENDING ──(AI返回)──→ FINALIZED
                                                                              │                         │
                                                                       (用户改名)               (用户改名)
                                                                              ↓                         ↓
                                                                          USER_SET ←────────────── USER_SET
    """
    NEED_TITLE = "need_title"       # 无标题，等待生成
    AI_PENDING = "ai_pending"       # AI 请求已发出，未返回
    AI_SET = "ai_set"               # AI 标题已设置（第1条生成完成）
    USER_SET = "user_set"            # 用户手动改过，永久锁定
    FINALIZED = "finalized"         # 第3条后锁定，不再自动生成


class SessionManager:
    """
    会话管理器 Facade

    封装 session 模块的所有组件，提供统一的会话管理 API。

    示例：
    ```python
    manager = SessionManager()

    # 写消息
    manager.add_user_message("帮我写一个排序函数")
    manager.add_assistant_message("好的...")

    # Fork
    new_id = manager.fork()

    # 切换
    manager.switch(session_id)

    # 清理
    report = SessionCleanup().full_cleanup()
    ```
    """

    def __init__(
        self,
        session_id: Optional[str] = None,
        data_dir: Optional[str] = None,
    ):
        # Session ID
        self.session_id = session_id or str(uuid.uuid4())

        # 数据目录
        if data_dir:
            self.data_dir = Path(data_dir)
        else:
            self.data_dir = self._get_default_data_dir()

        # ── 核心组件 ──
        self.storage = SessionStorage(
            session_id=self.session_id,
            data_dir=str(self.data_dir),
        )
        self.metadata = SessionMetadata(session_id=self.session_id)
        self.state = SessionState(session_id=self.session_id)
        self.progress = ProgressTracker(session_id=self.session_id)

        # ── 内部缓存 ──
        # 不再维护 _message_cache 镜像（与 disk 漂移风险，唯一真相源是 JSONL）
        # _last_uuid 是对话链尾部指针，用于 parentUuid 链接（对齐 Claude Code MessageState.lastUuid）
        self._last_uuid: Optional[str] = None
        self._closed = False

        # ── 标题生成状态 ──
        self._title_state = TitleState.NEED_TITLE
        self._user_msg_count: int = 0
        self._gen_seq: int = 0
        self._title_cache: Optional[str] = None
        self._restore_title_state()

        logger.info(f"SessionManager created: {self.session_id}")

    # ── 路径 ───────────────────────────────────────────────────

    @staticmethod
    def _get_default_data_dir() -> Path:
        cwd = Path.cwd()
        if (cwd / ".git").exists() or (cwd / "agent_core").exists():
            return cwd / ".agent_data" / "sessions"
        return Path.home() / ".agent_data" / "sessions"

    @property
    def jsonl_path(self) -> Optional[Path]:
        return self.storage._jsonl_path

    # ── 会话生命周期 ────────────────────────────────────────────

    def create(self, name: str = "新会话") -> str:
        """
        创建新会话

        Args:
            name: 会话名称

        Returns:
            新 session_id
        """
        new_id = str(uuid.uuid4())
        logger.info(f"Creating new session: {new_id}")
        return new_id

    def close(self):
        """关闭会话，刷新待写缓冲到磁盘"""
        if self._closed:
            return
        self._closed = True
        self.storage.flush()

    def switch(self, session_id: str):
        """
        切换到指定会话（当前 SessionManager 实例切换会话）

        Args:
            session_id: 目标会话 ID
        """
        # 关闭当前会话（刷新缓冲）
        self.close()

        # 切换到新会话
        self.session_id = session_id
        self.storage = SessionStorage(
            session_id=session_id,
            data_dir=str(self.data_dir),
        )

        # 恢复元数据
        tail = self.storage.read_tail()
        self.metadata = SessionMetadata.from_tail(tail, session_id)
        self.state = SessionState(session_id=session_id)
        self.progress = ProgressTracker(session_id=session_id)

        # 恢复消息缓存（不维护 _message_cache，唯一真相源是 JSONL）
        # 加载全部历史（含 boundary）以正确恢复 _last_uuid 链式指针
        messages = self.storage.get_messages(stop_at_boundary=False)
        self._last_uuid = messages[-1]["uuid"] if messages else None

        # 恢复标题状态
        self._title_state = TitleState.NEED_TITLE
        self._user_msg_count = 0
        self._gen_seq = 0
        self._title_cache = None
        self._closed = False  # 重置 close 标志
        self._restore_title_state()

        logger.info(f"Switched to session: {session_id}")

    def fork(self, new_name: Optional[str] = None) -> str:
        """
        Fork 当前会话（创建新会话，复制消息）

        Args:
            new_name: 新会话名称

        Returns:
            新 session_id
        """
        from .restore import fork_session

        # Fork 前先 flush，确保父会话所有消息已写入磁盘
        self.flush()

        new_session_id, new_storage = fork_session(
            parent_session_id=self.session_id,
            data_dir=str(self.data_dir),
            new_name=new_name,
        )

        logger.info(f"Forked session: {self.session_id} -> {new_session_id}")
        return new_session_id

    def resume(self) -> list[dict]:
        """
        Resume 当前会话（从断链处恢复）

        Returns:
            从断链处开始的最新消息链
        """
        from .restore import resume_session

        messages, metadata = resume_session(
            session_id=self.session_id,
            data_dir=str(self.data_dir),
        )
        self.metadata = metadata
        # 不维护 _message_cache 镜像，唯一真相源是 JSONL
        self._last_uuid = messages[-1]["uuid"] if messages else None

        # 恢复标题状态
        self._restore_title_state()

        # Resume 时显式 re-append 元数据（保证 tail 窗口可见）
        # 这是唯一应该 re-append 的地方
        if self.metadata.title:
            self._reappend_metadata()

        logger.info(f"Resumed session: {self.session_id}, got {len(messages)} messages")
        return messages

    def delete(self):
        """删除当前会话"""
        self.storage.delete()
        logger.info(f"Deleted session: {self.session_id}")

    @classmethod
    def delete_session(cls, session_id: str, data_dir: Optional[str] = None):
        """删除指定会话"""
        dd = data_dir or cls._get_default_data_dir()
        SessionStorage.delete_session(session_id, str(dd))

    # ── 消息写入 ────────────────────────────────────────────────

    def add_user_message(self, content: str, **extra) -> str:
        """添加用户消息（同时触发标题生成逻辑）"""
        self.state.set_running("user input")
        uuid_ = self.storage.add_message("user", content, parent_uuid=self._last_uuid)
        self._last_uuid = uuid_

        # 触发标题生成（fire-and-forget，不阻塞主流程）
        self._on_user_message(content)

        return uuid_

    def add_assistant_message(
        self,
        content=None,
        message: Optional[dict] = None,
        **extra,
    ) -> str:
        """添加助手消息

        两种用法（Claude Code 风格）:
        1. add_assistant_message("纯文本") → message = {role: assistant, content: "纯文本"}
        2. add_assistant_message(message={role: assistant, content: [{type:text,...}, {type:tool_use,...}]})
           → 直接存 API 原始格式，零转换
        """
        entry_type = extra.pop("entry_type", "assistant")
        if message is None:
            message = {"role": "assistant", "content": content}
        uuid_ = self.storage.add_message(
            "assistant",
            entry_type=entry_type,
            message=message,
            parent_uuid=self._last_uuid,
            **extra,
        )
        self._last_uuid = uuid_
        self.state.set_idle()
        return uuid_

    def add_assistant_with_tools(
        self,
        text: str,
        tool_calls: list,
        **extra,
    ) -> str:
        """添加包含 tool_use 的 assistant 消息（Claude Code 风格：一条 Entry）

        存储格式：message = {role: assistant, content: [{type:text}, {type:tool_use}, ...]}
        和 LLM API 返回的格式完全一致，零转换。
        """
        content_blocks = []
        if text:
            content_blocks.append({"type": "text", "text": text})
        for tc in tool_calls:
            content_blocks.append({
                "type": "tool_use",
                "id": tc["id"],
                "name": tc["name"],
                "input": tc["input"],
            })
            self.progress.record_tool_call(tc["name"])

        return self.add_assistant_message(
            message={"role": "assistant", "content": content_blocks},
            **extra,
        )

    def add_tool_results(
        self,
        results: list[dict],
        **extra,
    ) -> str:
        """添加 tool_results（Claude Code 风格：一条 user Entry 包含所有 tool_result）

        存储格式：message = {role: user, content: [{type:tool_result, ...}, ...]}
        和 LLM API 格式完全一致，零转换。

        Args:
            results: [{"tool_use_id": "...", "content": "..."}, ...]
        """
        content_blocks = []
        for r in results:
            content_blocks.append({
                "type": "tool_result",
                "tool_use_id": r["tool_use_id"],
                "content": r["content"],
            })

        uuid_ = self.storage.add_message(
            "user",
            entry_type="user",
            message={"role": "user", "content": content_blocks},
            parent_uuid=self._last_uuid,
            **extra,
        )
        self._last_uuid = uuid_
        return uuid_

    def add_summary(
        self,
        summary: str,
        tokens_saved: int = 0,
        **extra,
    ) -> str:
        """添加摘要（对齐 Claude Code: 写普通 user message + isCompactSummary）

        在 add_compact_boundary 之后调用，作为新链的起点。
        """
        self.progress.record_compaction()
        uuid_ = self.storage.add_summary(
            summary=summary,
            tokens_saved=tokens_saved,
            parent_uuid=self._last_uuid,
            **extra,
        )
        self._last_uuid = uuid_
        return uuid_

    def add_compact_boundary(
        self,
        trigger: str = "auto",
        pre_tokens: int = 0,
        messages_summarized: int = 0,
        **extra,
    ) -> str:
        """添加压缩边界（对齐 Claude Code createCompactBoundaryMessage）

        parentUuid 链接到最后一条旧消息（不是 None）。
        加载时反向扫描找最后一个 compact_boundary 取其后缀。
        """
        uuid_ = self.storage.add_compact_boundary(
            parent_uuid=self._last_uuid,  # ← 链接最后一条消息
            trigger=trigger,
            pre_tokens=pre_tokens,
            messages_summarized=messages_summarized,
            **extra,
        )
        self._last_uuid = uuid_
        return uuid_

    # ── 标题生成系统 ─────────────────────────────────────────────

    def _restore_title_state(self):
        """从 JSONL 恢复标题状态和用户消息计数（重启恢复）

        读取策略（学 Claude Code）：
        1. 先读 tail 64KB（快速，覆盖绝大多数场景）
        2. tail 没找到 custom-title 且文件 > 64KB → 回退读 head 64KB
        3. 仍找不到 → NEED_TITLE

        恢复后，如果有 custom-title，重新追加到文件尾部（re-append）。
        这保证 tail 窗口始终能读到最新标题（学 Claude Code 的 reAppendSessionMetadata）。
        ai-title 不 re-append——它是临时的，允许被挤出 tail 窗口。

        规则（优先级从高到低）：
        - 有 custom-title → USER_SET（用户改过，永久锁定）
        - 有 ai-title 且 genSeq>=2（已重新生成过）→ FINALIZED
        - 有 ai-title 且 genSeq==1（仅首轮生成）→ AI_SET（允许第3条消息重新生成）
        - 都没有 → NEED_TITLE（需要生成）
        """
        try:
            entries = self.storage.read_tail(kb=64)
        except Exception:
            return

        # tail 扫描
        custom_title = None
        ai_title = None
        ai_title_entries = []
        user_msg_count = 0

        for entry in reversed(entries):
            etype = entry.get("type")
            if etype == "custom-title" and custom_title is None:
                custom_title = entry
            elif etype == "ai-title":
                ai_title_entries.append(entry)
                if ai_title is None:
                    ai_title = entry
            elif etype == "user":
                msg = entry.get("message", {})
                content = msg.get("content", "")
                if isinstance(content, str) and content:
                    user_msg_count += 1

        # tail 没找到 custom-title，回退到 head 读取
        if not custom_title:
            try:
                head_entries = self.storage.read_head(kb=64)
                for entry in head_entries:
                    if entry.get("type") == "custom-title":
                        custom_title = entry
                        break
            except Exception:
                pass

        # 恢复计数器
        self._user_msg_count = user_msg_count

        # 恢复 gen_seq：取所有 ai-title 条目中最大的 genSeq
        max_gen_seq = 0
        for entry in ai_title_entries:
            gs = entry.get("genSeq", 1)
            if gs > max_gen_seq:
                max_gen_seq = gs
        self._gen_seq = max_gen_seq

        # 决策：custom-title > ai-title > NEED_TITLE
        if custom_title:
            self._title_state = TitleState.USER_SET
            self._title_cache = custom_title.get("customTitle") or custom_title.get("title")
            # 同步到 metadata（仅内存，不写盘）
            self.metadata.title = self._title_cache
            # 注意：不在 __init__ 路径调用 _reappend_metadata()
            # re-append 只应在 resume() 中显式调用，避免每次创建实例都写盘
            return

        # ai-title 场景不需要 re-append

        if ai_title:
            self._title_cache = ai_title.get("aiTitle") or ai_title.get("title")
            gen_seq = ai_title.get("genSeq", 1)
            if gen_seq >= 2:
                self._title_state = TitleState.FINALIZED
            else:
                self._title_state = TitleState.AI_SET
            return

        self._title_state = TitleState.NEED_TITLE

    def _on_user_message(self, text: str) -> None:
        """每条用户消息调用，决定是否触发标题生成

        触发策略（参考 Claude Code sessionTitle.ts）：
        - 第1条消息：即时占位 + 异步 AI 生成
        - 第3条消息：用完整对话重新生成（覆盖第1条的粗略标题）
        - 第4条起：不再生成
        - 用户手动改名后：永久阻止自动生成
        """
        # 守卫：用户改过或已锁定 → 永不自动生成
        if self._title_state in (TitleState.USER_SET, TitleState.FINALIZED):
            return

        self._user_msg_count += 1
        count = self._user_msg_count

        if count == 1:
            # 即时占位（不写 JSONL，只在缓存）
            placeholder = self._derive_title(text)
            if placeholder:
                self._title_cache = placeholder

            # 异步 AI 生成
            self._gen_seq += 1
            self._fire_and_forget_title(text, self._gen_seq)
            self._title_state = TitleState.AI_PENDING

        elif count == 3 and self._title_state == TitleState.AI_SET:
            # 第3条：用完整对话重新生成
            self._gen_seq += 1
            context = self._extract_conversation_text()
            self._fire_and_forget_title(context, self._gen_seq)
            self._title_state = TitleState.AI_PENDING
            # 无论 AI 是否成功，第3条后锁定（在回调里设 FINALIZED）

    def _fire_and_forget_title(self, input_text: str, gen_seq: int) -> None:
        """异步调用轻量模型生成标题，fire-and-forget

        使用新线程 + 新事件循环，避免阻塞同步主流程。
        """
        def _run():
            try:
                loop = asyncio.new_event_loop()
                loop.run_until_complete(
                    self._generate_ai_title(input_text, gen_seq)
                )
                loop.close()
            except Exception as e:
                logger.warning(f"Title generation failed: {e}")

        thread = threading.Thread(target=_run, daemon=True)
        thread.start()

    async def _generate_ai_title(self, input_text: str, gen_seq: int) -> None:
        """异步生成标题，genSeq 防乱序

        防乱序：只有最新 genSeq 的结果才写入，旧请求晚返回则丢弃。
        """
        try:
            title = await asyncio.wait_for(
                self._call_llm_for_title(input_text),
                timeout=15.0,
            )
            if not title:
                return

            # genSeq 防乱序：只有最新序号才写入
            if gen_seq != self._gen_seq:
                return

            # 用户改过就不覆盖
            if self._title_state == TitleState.USER_SET:
                return

            # 写入 ai-title 条目（内含状态转移）
            self._save_ai_title(title, gen_seq)

        except asyncio.TimeoutError:
            # 超时 → 如果还在 PENDING，回退到 NEED_TITLE
            if self._title_state == TitleState.AI_PENDING:
                self._title_state = TitleState.NEED_TITLE
        except Exception as e:
            logger.warning(f"AI title generation error: {e}")
            if self._title_state == TitleState.AI_PENDING:
                self._title_state = TitleState.NEED_TITLE

    # 标题生成 prompt（学 Claude Code 的 sessionTitle.ts）
    TITLE_SYSTEM_PROMPT = (
        "生成一个简洁的会话标题（3-7个词），要求：\n"
        "1. 准确概括对话的主题或目标\n"
        "2. 只返回标题文本，不要引号、不要解释、不要多余内容\n"
        "3. 使用自然的中文表达\n"
        "\n"
        "好的示例：\n"
        "- 用户问候和自我介绍\n"
        "- 并行执行三个计算和搜索任务\n"
        "- 调试登录按钮无响应问题\n"
        "- 重构API客户端错误处理\n"
        "\n"
        "差的示例：\n"
        "- 问候时刻（太模糊，没有信息量）\n"
        "- 三个任务执行（太简略，缺少具体内容）\n"
        "- 代码修改（太泛，无法区分会话）\n"
        "- 调查并修复移动设备上登录按钮无响应的问题（太长）"
    )

    async def _call_llm_for_title(self, input_text: str) -> Optional[str]:
        """调用轻量模型（GLM-4-flash）生成 3-7 词标题

        Prompt 参考自 Claude Code 的 sessionTitle.ts:
        - 强调"概括主题或目标"，而非"提取关键词"
        - 提供好坏示例做 few-shot 引导
        - 输入是完整对话文本（最多1000字符），而非单条消息
        """
        def _sync_call():
            try:
                from ..llm.router import LLMRouter, LLMConfig, LLMProvider, LLMModel

                # 使用 GLM-4-flash 作为标题生成模型（轻量快速）
                config = LLMConfig(
                    provider=LLMProvider.ZHIPU,
                    model="GLM-4-flash",
                )
                router = LLMRouter(config)

                # 收集完整响应
                full_text = ""
                for chunk in router.chat(
                    messages=[
                        {"role": "system", "content": self.TITLE_SYSTEM_PROMPT},
                        {"role": "user", "content": input_text[:1000]},
                    ],
                ):
                    if chunk.text_delta:
                        full_text += chunk.text_delta.text

                # 清理：去除引号和多余空白
                title = full_text.strip().strip('"').strip("'").strip()
                if len(title) > 50:
                    title = title[:50] + "..."
                return title if title else None

            except Exception as e:
                logger.warning(f"LLM title call failed: {e}")
                return None

        # 在线程池中运行同步调用
        with concurrent.futures.ThreadPoolExecutor() as executor:
            future = executor.submit(_sync_call)
            return future.result(timeout=15)

    def _derive_title(self, text: str) -> str:
        """即时占位标题：取第一句话，截断20字符"""
        match = re.match(r'^(.*?[。！？.!?])', text)
        first_sentence = match.group(1) if match else text[:20]
        # 超过20字符截断（注意：原始文本被[:20]截断时长度恰好20，不需要再加...）
        if len(first_sentence) > 20:
            first_sentence = first_sentence[:20] + "..."
        elif not match and len(text) > 20:
            # 无句号且原文本超过20字符，说明截断了
            first_sentence = first_sentence + "..."
        return first_sentence

    def _extract_conversation_text(self) -> str:
        """提取对话文本用于第3条标题生成"""
        messages = self.get_messages_for_llm()
        parts = []
        for msg in messages:
            role = msg.get("role", "")
            content = msg.get("content", "")
            if isinstance(content, list):
                # 提取 text 类型的内容
                text_parts = []
                for item in content:
                    if isinstance(item, dict) and item.get("type") == "text":
                        text_parts.append(item.get("text", ""))
                content = " ".join(text_parts)
            if isinstance(content, str) and content:
                parts.append(f"{role}: {content[:200]}")
        return "\n".join(parts)

    def _save_ai_title(self, title: str, gen_seq: int) -> None:
        """写入 ai-title 条目到 JSONL，并更新状态"""
        entry = {
            "uuid": str(uuid.uuid4()),
            "parentUuid": None,
            "sessionId": self.session_id,
            "type": "ai-title",
            "aiTitle": title,
            "genSeq": gen_seq,
            "timestamp": datetime.now().isoformat(),
        }
        self.storage.append_raw_entry(entry)
        self.storage.flush()
        self._title_cache = title
        # 同步更新 metadata
        self.metadata.update_ai_title(title)

        # 状态转移：AI_PENDING → AI_SET / FINALIZED
        if self._title_state == TitleState.AI_PENDING:
            if self._user_msg_count >= 3:
                self._title_state = TitleState.FINALIZED
            else:
                self._title_state = TitleState.AI_SET

        logger.info(f"AI title saved: {title!r} (genSeq={gen_seq})")

    def rename_session(self, new_title: str) -> None:
        """用户手动改名，写入 custom-title，永久锁定

        优先级：custom-title > ai-title > deriveTitle 占位
        """
        entry = {
            "uuid": str(uuid.uuid4()),
            "parentUuid": None,
            "sessionId": self.session_id,
            "type": "custom-title",
            "customTitle": new_title,
            "timestamp": datetime.now().isoformat(),
        }
        self.storage.append_raw_entry(entry)
        self.storage.flush()
        self._title_cache = new_title
        self._title_state = TitleState.USER_SET
        # 同步更新 metadata
        self.metadata.update_title(new_title)
        logger.info(f"Session renamed: {new_title!r}")

    def get_title(self) -> Optional[str]:
        """获取当前标题

        优先级：custom-title > ai-title (最新genSeq) > 缓存占位
        """
        # 先看缓存
        if self._title_cache:
            return self._title_cache

        # 从 JSONL 读
        try:
            entries = self.storage.read_tail(kb=64)
        except Exception:
            return None

        custom_title = None
        ai_title = None
        ai_gen_seq = -1

        for entry in entries:
            if entry.get("type") == "custom-title" and not custom_title:
                custom_title = entry.get("customTitle") or entry.get("title")
            elif entry.get("type") == "ai-title":
                if entry.get("genSeq", 0) > ai_gen_seq:
                    ai_title = entry.get("aiTitle") or entry.get("title")
                    ai_gen_seq = entry.get("genSeq", 0)

        return custom_title or ai_title

    # ── 元数据写入 ──────────────────────────────────────────────

    def update_title(self, title: str):
        """更新会话标题（保留旧接口兼容，转向 rename_session）"""
        self.rename_session(title)

    def update_ai_title(self, ai_title: str):
        """更新 AI 标题（保留旧接口兼容）"""
        self._save_ai_title(ai_title, gen_seq=0)

    def add_tag(self, tag: str):
        """添加标签"""
        self.metadata.add_tag(tag)
        self._reappend_metadata()

    def update_mode(self, mode: Literal["plan", "read", "write"]):
        """更新模式"""
        self.metadata.update_mode(mode)
        self._reappend_metadata()

    def _reappend_metadata(self):
        """重新追加元数据到 JSONL 尾部（保证最新）

        学 Claude Code reAppendSessionMetadata:
        只 re-append 需要保活在 tail 窗口的字段:
        1. custom-title
        2. tag
        3. agent-name
        4. mode

        只 re-append: custom-title, tag, agent-name, mode
        """
        timestamp = datetime.now().isoformat()
        reappend_order = []  # 按写入顺序收集

        if self.metadata.title:
            reappend_order.append({
                "uuid": str(uuid.uuid4()),
                "parentUuid": None,
                "sessionId": self.session_id,
                "type": "custom-title",
                "customTitle": self.metadata.title,
                "timestamp": timestamp,
            })

        for tag in self.metadata.tags:
            reappend_order.append({
                "uuid": str(uuid.uuid4()),
                "parentUuid": None,
                "sessionId": self.session_id,
                "type": "tag",
                "tag": tag,
                "timestamp": timestamp,
            })

        reappend_order.append({
            "uuid": str(uuid.uuid4()),
            "parentUuid": None,
            "sessionId": self.session_id,
            "type": "agent-name",
            "agentName": self.metadata.agent_name,
            "timestamp": timestamp,
        })

        reappend_order.append({
            "uuid": str(uuid.uuid4()),
            "parentUuid": None,
            "sessionId": self.session_id,
            "type": "mode",
            "mode": self.metadata.mode,
            "timestamp": timestamp,
        })

        for entry in reappend_order:
            self.storage.append_raw_entry(entry)
        self.storage.flush()

    # ── 读取 ───────────────────────────────────────────────────

    def get_messages(
        self,
        stop_at_boundary: bool = True,
    ) -> list[dict]:
        """
        获取消息列表（直接读 JSONL，唯一真相源）

        Args:
            stop_at_boundary: 是否在压缩边界处停止（默认 True，给 LLM 用）
        """
        self.storage.flush()
        return self.storage.get_messages(stop_at_boundary=stop_at_boundary)

    def get_metadata(self) -> SessionMetadata:
        """获取元数据"""
        return self.metadata

    def get_state(self) -> SessionState:
        """获取状态机"""
        return self.state

    def get_progress(self) -> "ProgressSnapshot":
        """获取进度快照"""
        return self.progress.snapshot(status=self.state.status)

    # ── 会话列表 ────────────────────────────────────────────────

    @classmethod
    def list_sessions(cls, data_dir: Optional[str] = None) -> list[dict]:
        """列出所有会话"""
        dd = data_dir or cls._get_default_data_dir()
        return SessionStorage.list_sessions(str(dd))

    # ── 持久化 ─────────────────────────────────────────────────

    def flush(self):
        """刷新写队列"""
        self.storage.flush()

    # ── 上下文管理集成接口 ─────────────────────────────────────

    def get_messages_for_llm(
        self,
        stop_at_boundary: bool = True,
    ) -> list[dict]:
        """获取适合传给 LLM 的消息格式（直接取 message 字段，零转换）

        Claude Code 风格：tool_use 包在 assistant content 数组里，
        tool_result 包在 user content 数组里，存储即 API 格式。
        """
        messages = self.get_messages(stop_at_boundary=stop_at_boundary)
        result = []
        for m in messages:
            msg = m.get("message")
            if msg and msg.get("role") in ("user", "assistant", "system"):
                result.append(msg)
        return result

    # ── 诊断 ───────────────────────────────────────────────────

    def __repr__(self):
        # 不再依赖 _message_cache 计数，改用 storage 的 entry cache
        return (
            f"SessionManager(session_id={self.session_id[:8]}, "
            f"status={self.state.status}, "
            f"title_state={self._title_state.value}, "
            f"entries={len(self.storage._entry_cache)})"
        )
