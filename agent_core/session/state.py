"""
SessionState - 会话状态机
参考 Claude Code sessionState.ts 实现的 ReAct Agent 状态管理

状态定义：
- idle: 空闲，等待用户输入
- running: 正在执行（LLM 调用中 / 工具执行中）
- requires_action: 等待外部操作（如 Permission Mode）

SDK 事件：
- session_state_changed: 状态变更事件
- permission_requested: 权限请求事件
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Callable, Literal, Optional

logger = logging.getLogger("session.state")

# 类型别名
SessionStatus = Literal["idle", "running", "requires_action"]


class RequiresActionDetails:
    """
    requires_action 状态的详细描述

    用于追踪当前阻塞 Agent 继续执行的操作：
    - permission_request: 等待用户授权（如危险的 write 操作）
    - user_input: 等待用户输入
    - tool_execution: 等待外部工具完成
    """

    def __init__(
        self,
        action_type: str,
        message: str,
        tool_name: Optional[str] = None,
        tool_input: Optional[dict] = None,
        urgency: str = "normal",
        **extra,
    ):
        self.action_type = action_type
        self.message = message
        self.tool_name = tool_name
        self.tool_input = tool_input
        self.urgency = urgency
        self.extra = extra
        self._timestamp = None

    def to_dict(self) -> dict:
        return {
            "action_type": self.action_type,
            "message": self.message,
            "tool_name": self.tool_name,
            "tool_input": self.tool_input,
            "urgency": self.urgency,
            **self.extra,
        }

    def __repr__(self):
        return (
            f"RequiresActionDetails(type={self.action_type!r}, "
            f"message={self.message!r})"
        )


class SessionState:
    """
    会话状态机

    管理 Agent 的三种状态及状态间的转换：

    ```
    ┌─────────────────────────────────────────────────────────┐
    │                                                         │
    │   ┌──────┐                                             │
    │   │ idle │◀────────────────┐                           │
    │   └──┬───┘                 │                           │
    │      │ set_running()       │ set_idle()               │
    │      ▼                     │                           │
    │   ┌──────────┐             │                           │
    │   │ running  │─────────────┤                           │
    │   └──┬───────┘             │                           │
    │      │                    │                           │
    │      │ set_requires_action()                            │
    │      │ (需要授权等)                                      │
    │      ▼                                                │
    │   ┌────────────────┐                                  │
    │   │ requires_action │──────┘                           │
    │   └────────────────┘  (授权后 set_idle)                 │
    │                                                         │
    └─────────────────────────────────────────────────────────┘
    ```

    SDK 事件：
    - session_state_changed: (old_state, new_state) → 触发 UI 更新
    - permission_requested: (details) → 触发权限弹窗
    """

    def __init__(self, session_id: str):
        self.session_id = session_id
        self._status: SessionStatus = "idle"
        self._requires_action: Optional[RequiresActionDetails] = None

        # 事件回调
        self._state_changed_callbacks: list[Callable[[SessionStatus, SessionStatus], None]] = []
        self._permission_callbacks: list[Callable[[RequiresActionDetails], None]] = []

        # 锁（多线程安全）
        self._lock = threading.RLock()

        # 历史（用于调试）
        self._history: list[tuple[str, str]] = []  # [(old, new), ...]

        logger.info(f"[{session_id[:8]}] SessionState initialized: idle")

    # ── 状态属性 ─────────────────────────────────────────────────

    @property
    def status(self) -> SessionStatus:
        return self._status

    @property
    def is_idle(self) -> bool:
        return self._status == "idle"

    @property
    def is_running(self) -> bool:
        return self._status == "running"

    @property
    def is_requires_action(self) -> bool:
        return self._status == "requires_action"

    @property
    def requires_action_details(self) -> Optional[RequiresActionDetails]:
        return self._requires_action

    @property
    def history(self) -> list[tuple[str, str]]:
        return list(self._history)

    # ── 状态转换 ─────────────────────────────────────────────────

    def set_idle(self):
        """切换到 idle 状态"""
        with self._lock:
            old = self._status
            if old == "idle":
                return

            self._status = "idle"
            self._requires_action = None
            self._history.append((old, "idle"))

            logger.info(f"[{self.session_id[:8]}] State: {old} → idle")
            self._emit_state_changed(old, "idle")

    def set_running(self, reason: str = ""):
        """
        切换到 running 状态

        Args:
            reason: 运行原因（用于日志）
        """
        with self._lock:
            old = self._status
            if old == "running":
                logger.debug(f"[{self.session_id[:8]}] Already running: {reason}")
                return

            self._status = "running"
            self._requires_action = None
            self._history.append((old, "running"))

            logger.info(f"[{self.session_id[:8]}] State: {old} → running {reason!r}")
            self._emit_state_changed(old, "running")

    def set_requires_action(self, details: RequiresActionDetails):
        """
        切换到 requires_action 状态

        Args:
            details: 需要操作的详情
        """
        with self._lock:
            old = self._status
            self._status = "requires_action"
            self._requires_action = details
            self._history.append((old, "requires_action"))

            logger.info(
                f"[{self.session_id[:8]}] State: {old} → requires_action "
                f"({details.action_type})"
            )
            self._emit_state_changed(old, "requires_action")
            self._emit_permission_requested(details)

    # ── 便捷快捷方法 ────────────────────────────────────────────

    def request_permission(
        self,
        message: str,
        tool_name: Optional[str] = None,
        tool_input: Optional[dict] = None,
        urgency: str = "normal",
    ) -> RequiresActionDetails:
        """
        请求权限（便捷方法）

        等价于：
            details = RequiresActionDetails("permission_request", message, ...)
            self.set_requires_action(details)
        """
        details = RequiresActionDetails(
            action_type="permission_request",
            message=message,
            tool_name=tool_name,
            tool_input=tool_input,
            urgency=urgency,
        )
        self.set_requires_action(details)
        return details

    def wait_for_permission(self, timeout: float = None) -> bool:
        """
        等待权限授权（阻塞当前线程）

        Returns:
            True: 已授权（状态变为 idle）
            False: 超时
        """
        import time

        start = time.time()
        while self._status == "requires_action":
            if timeout and (time.time() - start) >= timeout:
                return False
            time.sleep(0.1)
        return True

    # ── 事件系统 ─────────────────────────────────────────────────

    def on_state_changed(
        self,
        callback: Callable[[SessionStatus, SessionStatus], None],
    ):
        """注册状态变更回调"""
        self._state_changed_callbacks.append(callback)

    def on_permission_requested(
        self,
        callback: Callable[[RequiresActionDetails], None],
    ):
        """注册权限请求回调"""
        self._permission_callbacks.append(callback)

    def _emit_state_changed(self, old: SessionStatus, new: SessionStatus):
        """触发状态变更事件"""
        for cb in self._state_changed_callbacks:
            try:
                cb(old, new)
            except Exception as e:
                logger.error(f"State changed callback error: {e}")

    def _emit_permission_requested(self, details: RequiresActionDetails):
        """触发权限请求事件"""
        for cb in self._permission_callbacks:
            try:
                cb(details)
            except Exception as e:
                logger.error(f"Permission requested callback error: {e}")

    # ── 诊断 ─────────────────────────────────────────────────────

    def snapshot(self) -> dict:
        """获取当前状态的完整快照（用于调试/持久化）"""
        return {
            "session_id": self.session_id,
            "status": self._status,
            "requires_action": self._requires_action.to_dict()
                if self._requires_action else None,
            "history": self._history,
        }

    def __repr__(self):
        return f"SessionState({self.session_id[:8]}, {self._status})"
