"""
会话管理模块（Session Management）
参考 Claude Code sessionStorage.ts / sessionState.ts / sessionRestore.ts 实现的 JSONL 持久化会话管理

主要组件：
- SessionStorage  : JSONL 持久化（append-only / parentUuid 链 / 写队列 / 原子性）
- SessionMetadata : 会话元数据（标题 / 标签 / Agent 类型 / 模式）
- SessionState    : 状态机（idle / running / requires_action）
- SessionManager  : Facade 聚合层（统一 API）
- ProgressTracker : 进度追踪（文件变更 / 待办 / Turn 统计）
- SessionCleanup  : 清理归档（TTL / 归档 / 磁盘统计）

会话恢复：
- resume_session   : 从断链处恢复（只加载摘要 + 最新消息）
- continue_session : 继续会话（加载全部消息）
- fork_session     : 分叉会话（复制消息 + 新 UUID + 独立演进）

典型用法：
    from agent_core.session import SessionManager

    manager = SessionManager()
    manager.add_user_message("帮我写排序函数")
    manager.add_assistant_message("好的...")

    # Fork
    new_id = manager.fork()

    # Resume
    messages, meta = resume_session(manager.session_id)
"""

from .storage import SessionStorage
from .metadata import SessionMetadata
from .state import SessionState, SessionStatus, RequiresActionDetails
from .progress import ProgressTracker, ProgressSnapshot, FileChange, TodoItem, TurnStats
from .cleanup import SessionCleanup
from .restore import resume_session, continue_session, fork_session, list_sessions, delete_session, compact_session
from .manager import SessionManager

__all__ = [
    # 核心存储
    "SessionStorage",
    # 元数据
    "SessionMetadata",
    # 状态机
    "SessionState",
    "SessionStatus",
    "RequiresActionDetails",
    # 进度追踪
    "ProgressTracker",
    "ProgressSnapshot",
    "FileChange",
    "TodoItem",
    "TurnStats",
    # 清理归档
    "SessionCleanup",
    # 会话恢复
    "resume_session",
    "continue_session",
    "fork_session",
    "list_sessions",
    "delete_session",
    "compact_session",
    # Facade
    "SessionManager",
]
