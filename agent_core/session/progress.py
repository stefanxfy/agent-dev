"""
ProgressTracker - 会话进度追踪
参考 Claude Code 实现的实时状态 + 待办事项 + 文件历史追踪

进度追踪内容：
- 文件变更历史（创建/修改/删除）
- 待完成任务列表（用户提出的任务）
- 当前 Agent 状态（思考中/执行中/已完成）
- 执行统计（Turn 数、工具调用次数）
"""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Optional

logger = logging.getLogger("session.progress")


class FileChangeType(str, Enum):
    CREATED = "created"
    MODIFIED = "modified"
    DELETED = "deleted"
    READ = "read"


@dataclass
class FileChange:
    """单次文件变更记录"""
    path: str
    change_type: FileChangeType
    timestamp: str
    tool_use_id: str
    summary: str = ""  # 变更摘要（如 "修改第 5 行"）
    lines_added: int = 0
    lines_removed: int = 0


@dataclass
class TodoItem:
    """待办事项"""
    id: str
    description: str
    status: str = "pending"  # pending / done / cancelled
    created_at: str = ""
    updated_at: str = ""
    completed_at: Optional[str] = None
    priority: int = 0  # 0=普通, 1=重要, 2=紧急


@dataclass
class TurnStats:
    """Turn 执行统计"""
    turn_count: int = 0
    tool_call_count: int = 0
    total_tokens: int = 0
    llm_calls: int = 0
    compactions: int = 0  # 压缩次数
    started_at: Optional[str] = None
    last_turn_at: Optional[str] = None
    # 内部使用
    _defaults_filled: bool = field(default=False, repr=False)


@dataclass
class ProgressSnapshot:
    """进度快照（用于 UI 展示）"""
    session_id: str
    status: str  # idle / running / requires_action
    turn_stats: TurnStats
    file_changes: list[FileChange]
    todo_items: list[TodoItem]
    current_task: str = ""  # 当前正在做的任务
    recent_tool_calls: list[str] = field(default_factory=list)  # 最近 N 次工具调用


class ProgressTracker:
    """
    会话进度追踪器

    追踪内容：
    - 文件变更历史
    - 待办事项
    - Turn 统计
    - 当前任务状态

    提供：
    - 实时快照（用于 UI 更新）
    - 文件历史查询
    - 待办管理（添加/完成/取消）
    """

    MAX_FILE_HISTORY = 200   # 最多保留 200 条文件变更
    MAX_TODO_ITEMS = 50      # 最多保留 50 条待办
    MAX_RECENT_TOOLS = 10    # 最近工具调用数量

    def __init__(self, session_id: str):
        self.session_id = session_id

        # 文件变更历史
        self._file_changes: list[FileChange] = []

        # 待办事项
        self._todo_items: list[TodoItem] = []

        # 执行统计
        self._stats = TurnStats()

        # 当前任务
        self._current_task: str = ""
        self._current_task_started_at: Optional[str] = None

        # 最近工具调用（用于 UI 展示）
        self._recent_tool_calls: list[dict] = []

    # ── 文件变更追踪 ─────────────────────────────────────────────

    def track_file_change(
        self,
        path: str,
        change_type: FileChangeType,
        tool_use_id: str,
        summary: str = "",
        lines_added: int = 0,
        lines_removed: int = 0,
    ):
        """追踪文件变更"""
        change = FileChange(
            path=str(Path(path).resolve()),  # 标准化路径
            change_type=change_type,
            timestamp=datetime.now().isoformat(),
            tool_use_id=tool_use_id,
            summary=summary,
            lines_added=lines_added,
            lines_removed=lines_removed,
        )
        self._file_changes.append(change)
        logger.debug(f"File change: {change_type.value} {path}")
        self._trim_file_changes()

    def track_file_created(self, path: str, tool_use_id: str, summary: str = ""):
        self.track_file_change(path, FileChangeType.CREATED, tool_use_id, summary)

    def track_file_modified(
        self, path: str, tool_use_id: str, summary: str = "",
        lines_added: int = 0, lines_removed: int = 0
    ):
        self.track_file_change(
            path, FileChangeType.MODIFIED, tool_use_id,
            summary, lines_added, lines_removed
        )

    def track_file_deleted(self, path: str, tool_use_id: str, summary: str = ""):
        self.track_file_change(path, FileChangeType.DELETED, tool_use_id, summary)

    def _trim_file_changes(self):
        """裁剪文件变更历史（保留最近 MAX_FILE_HISTORY 条）"""
        if len(self._file_changes) > self.MAX_FILE_HISTORY:
            self._file_changes = self._file_changes[-self.MAX_FILE_HISTORY:]

    # ── 待办事项 ─────────────────────────────────────────────────

    def add_todo(
        self,
        description: str,
        priority: int = 0,
    ) -> TodoItem:
        """添加待办事项"""
        now = datetime.now().isoformat()
        todo = TodoItem(
            id=str(uuid.uuid4()),
            description=description,
            status="pending",
            created_at=now,
            updated_at=now,
            priority=priority,
        )
        self._todo_items.append(todo)
        logger.debug(f"Todo added: {description[:50]}")
        return todo

    def complete_todo(self, todo_id: str):
        """标记待办为已完成"""
        for todo in self._todo_items:
            if todo.id == todo_id:
                todo.status = "done"
                todo.completed_at = datetime.now().isoformat()
                todo.updated_at = todo.completed_at
                logger.debug(f"Todo completed: {todo.description[:50]}")
                break

    def cancel_todo(self, todo_id: str):
        """取消待办"""
        for todo in self._todo_items:
            if todo.id == todo_id:
                todo.status = "cancelled"
                todo.updated_at = datetime.now().isoformat()
                break

    def get_pending_todos(self) -> list[TodoItem]:
        """获取所有待处理的待办"""
        return [t for t in self._todo_items if t.status == "pending"]

    # ── Turn 统计 ────────────────────────────────────────────────

    def start_turn(self):
        """开始新 Turn"""
        self._stats.turn_count += 1
        self._stats.last_turn_at = datetime.now().isoformat()
        if not self._stats.started_at:
            self._stats.started_at = self._stats.last_turn_at
        logger.debug(f"Turn {self._stats.turn_count} started")

    def record_tool_call(self, tool_name: str):
        """记录一次工具调用"""
        self._stats.tool_call_count += 1
        self._recent_tool_calls.append({
            "tool": tool_name,
            "at": datetime.now().isoformat(),
        })
        # 裁剪
        if len(self._recent_tool_calls) > self.MAX_RECENT_TOOLS:
            self._recent_tool_calls = self._recent_tool_calls[-self.MAX_RECENT_TOOLS:]

    def record_llm_call(self, tokens: int = 0):
        """记录一次 LLM 调用"""
        self._stats.llm_calls += 1
        self._stats.total_tokens += tokens

    def record_compaction(self):
        """记录一次压缩"""
        self._stats.compactions += 1

    # ── 当前任务 ─────────────────────────────────────────────────

    def set_current_task(self, task: str):
        """设置当前任务（用于 UI 展示）"""
        if self._current_task != task:
            self._current_task = task
            self._current_task_started_at = datetime.now().isoformat()

    def clear_current_task(self):
        """清除当前任务"""
        self._current_task = ""
        self._current_task_started_at = None

    # ── 快照 ─────────────────────────────────────────────────────

    def snapshot(self, status: str = "idle") -> ProgressSnapshot:
        """生成进度快照（用于 UI 实时更新）"""
        return ProgressSnapshot(
            session_id=self.session_id,
            status=status,
            turn_stats=self._stats,
            file_changes=list(self._file_changes),
            todo_items=[t for t in self._todo_items if t.status == "pending"],
            current_task=self._current_task,
            recent_tool_calls=[
                tc["tool"] for tc in self._recent_tool_calls[-self.MAX_RECENT_TOOLS:]
            ],
        )

    def to_entry(self) -> dict:
        """转为 JSONL Entry（用于持久化到会话存储）"""
        return {
            "type": "progress-snapshot",
            "turn_count": self._stats.turn_count,
            "tool_call_count": self._stats.tool_call_count,
            "compaction_count": self._stats.compactions,
            "file_changes_count": len(self._file_changes),
            "todo_pending_count": len(self.get_pending_todos()),
            "timestamp": datetime.now().isoformat(),
        }

    # ── 恢复 ─────────────────────────────────────────────────────

    @classmethod
    def from_entry(cls, session_id: str, entry: dict) -> "ProgressTracker":
        """从 JSONL Entry 恢复进度（最小化恢复）"""
        tracker = cls(session_id=session_id)
        tracker._stats.turn_count = entry.get("turn_count", 0)
        tracker._stats.tool_call_count = entry.get("tool_call_count", 0)
        tracker._stats.compactions = entry.get("compaction_count", 0)
        return tracker

    def __repr__(self):
        return (
            f"ProgressTracker(turn={self._stats.turn_count}, "
            f"tools={self._stats.tool_call_count}, "
            f"files={len(self._file_changes)}, "
            f"todos={len(self.get_pending_todos())})"
        )
