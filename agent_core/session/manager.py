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
└── (cleanup: SessionCleanup)   # 清理（静态工具类）
```

提供统一的会话管理 API：
- 创建 / 切换 / 删除 / Fork 会话
- 读写消息和元数据
- 状态监控和事件回调
"""

from __future__ import annotations

import logging
import time
import uuid
from pathlib import Path
from typing import Callable, Generator, Literal, Optional

from .storage import SessionStorage
from .metadata import SessionMetadata
from .state import SessionState, SessionStatus, RequiresActionDetails
from .progress import ProgressTracker
from .cleanup import SessionCleanup

logger = logging.getLogger("session.manager")


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
        self._message_cache: list[dict] = []
        self._last_uuid: Optional[str] = None

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

    def switch(self, session_id: str):
        """
        切换到指定会话（当前 SessionManager 实例切换会话）

        Args:
            session_id: 目标会话 ID
        """
        # 刷新当前会话
        self.flush()

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

        # 恢复消息缓存
        messages = self.storage.get_messages(stop_at_boundary=False)
        self._message_cache = messages
        self._last_uuid = messages[-1]["uuid"] if messages else None

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
        self._message_cache = messages
        self._last_uuid = messages[-1]["uuid"] if messages else None

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
        """添加用户消息"""
        self.state.set_running("user input")
        self.metadata.update_last_prompt(content)
        uuid_ = self.storage.add_message("user", content, parent_uuid=self._last_uuid)
        self._last_uuid = uuid_
        self._message_cache.append({"role": "user", "content": content, "uuid": uuid_})
        return uuid_

    def add_assistant_message(
        self,
        content: str,
        tool_calls: Optional[list[dict]] = None,
        **extra,
    ) -> str:
        """添加助手消息"""
        entry_type = extra.pop("entry_type", "assistant")
        uuid_ = self.storage.add_message(
            "assistant",
            content,
            entry_type=entry_type,
            parent_uuid=self._last_uuid,
            tool_calls=tool_calls or [],
            **extra,
        )
        self._last_uuid = uuid_
        self._message_cache.append({
            "role": "assistant",
            "content": content,
            "uuid": uuid_,
            "tool_calls": tool_calls or [],
        })
        self.state.set_idle()
        return uuid_

    def add_tool_use(
        self,
        name: str,
        tool_input: dict,
        tool_use_id: str,
        **extra,
    ) -> str:
        """添加 tool_use"""
        self.progress.record_tool_call(name)
        uuid_ = self.storage.add_tool_use(name, tool_input, tool_use_id, parent_uuid=self._last_uuid)
        self._last_uuid = uuid_
        self._message_cache.append({
            "type": "tool_use",
            "name": name,
            "input": tool_input,
            "uuid": uuid_,
        })
        return uuid_

    def add_tool_result(
        self,
        tool_use_id: str,
        content: str,
        **extra,
    ) -> str:
        """添加 tool_result"""
        uuid_ = self.storage.add_tool_result(tool_use_id, content, parent_uuid=self._last_uuid)
        self._last_uuid = uuid_
        self._message_cache.append({
            "type": "tool_result",
            "tool_use_id": tool_use_id,
            "content": content,
            "uuid": uuid_,
        })
        return uuid_

    def add_summary(
        self,
        summary: str,
        tokens_saved: int = 0,
        format: str = "BASE",
        **extra,
    ) -> str:
        """添加摘要"""
        self.progress.record_compaction()
        uuid_ = self.storage.add_summary(summary, tokens_saved, format, parent_uuid=self._last_uuid)
        self._last_uuid = uuid_
        return uuid_

    def add_compact_boundary(self, **extra) -> str:
        """添加压缩边界"""
        uuid_ = self.storage.add_compact_boundary(parent_uuid=None)  # ← 断链
        # 边界后的消息应该指向 boundary 自己（作为独立根节点）
        self._last_uuid = uuid_
        return uuid_

    # ── 元数据写入 ──────────────────────────────────────────────

    def update_title(self, title: str):
        """更新会话标题"""
        self.metadata.update_title(title)
        self._reappend_metadata()

    def update_ai_title(self, ai_title: str):
        """更新 AI 标题"""
        self.metadata.update_ai_title(ai_title)
        self._reappend_metadata()

    def add_tag(self, tag: str):
        """添加标签"""
        self.metadata.add_tag(tag)
        self._reappend_metadata()

    def update_last_prompt(self, prompt: str):
        """更新最后一条用户消息"""
        self.metadata.update_last_prompt(prompt)
        self._reappend_metadata()

    def update_mode(self, mode: Literal["plan", "read", "write"]):
        """更新模式"""
        self.metadata.update_mode(mode)
        self._reappend_metadata()

    def _reappend_metadata(self):
        """重新追加元数据到 JSONL 尾部（保证最新）"""
        entries = self.metadata.to_entries()
        for entry in entries:
            self.storage.append_raw_entry(entry)
        self.storage.flush()

    # ── 读取 ───────────────────────────────────────────────────

    def get_messages(
        self,
        stop_at_boundary: bool = True,
        include_pending: bool = False,
    ) -> list[dict]:
        """
        获取消息列表

        Args:
            stop_at_boundary: 是否在压缩边界处停止
            include_pending: 是否包含 pending 队列中的未刷写消息
                             （默认 False，直接读磁盘，适合冷启动场景）
        """
        self.storage.flush()

        # 读磁盘
        disk_messages = self.storage.get_messages(stop_at_boundary=stop_at_boundary)

        if include_pending:
            # 合并 pending 队列（排除已在磁盘的）
            disk_uuids = {m["uuid"] for m in disk_messages}
            pending_messages = [
                m for m in self._message_cache
                if m.get("uuid") not in disk_uuids
            ]
            return disk_messages + pending_messages

        return disk_messages

    def get_history(self) -> list[dict]:
        """Get conversation history for agent_core compatibility.

        从 message 字段直接提取 role + content，零转换。
        """
        result = []
        for m in self.get_messages(stop_at_boundary=False):
            msg = m.get("message")
            if msg and msg.get("role") in ("user", "assistant"):
                result.append({"role": msg["role"], "content": msg.get("content", "")})
        return result


    def get_metadata(self) -> SessionMetadata:
        """获取元数据"""
        return self.metadata

    def get_state(self) -> SessionState:
        """获取状态机"""
        return self.state

    def get_progress(self) -> ProgressSnapshot:
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
        """
        获取适合传给 LLM 的消息格式（直接取 message 字段，零转换）
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
        return (
            f"SessionManager(session_id={self.session_id[:8]}, "
            f"status={self.state.status}, "
            f"messages={len(self._message_cache)})"
        )
