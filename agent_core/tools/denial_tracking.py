"""
Denial Tracking — 连续/累计 deny 限制

对齐 Claude Code:
- src/utils/permissions/denialTracking.ts(DenialTrackingState / handleDenialLimitExceeded)
- doc §4.5.4 Denial limits + 反 LLM 攻击保护

核心设计:
1. **限制阈值**(对齐 doc §4.5.4 + CC denialTracking.ts):
   - max_consecutive: 3(连续 3 次 deny 触发 fallback → ASK)
   - max_total: 20(单 session 总 deny > 20 触发 fallback)
2. **Fallback 行为**:连续 / 累计超阈值 → ASK(OtherReason("denial_limit"))
   - 让用户显式确认,避免 LLM 被自动 deny 攻击 loop
3. **Per-session 内存 store**:每个 session_id 独立 state
4. **Success reset**:record_success → consecutive 清零
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, replace
from typing import Optional

from .permission_types import (
    OtherReason,
    PermissionBehavior,
    PermissionDecision,
)


logger = logging.getLogger(__name__)


# ────────────────────────────────────────────────────────────────────
# DENIAL_LIMITS — 阈值常量
# ────────────────────────────────────────────────────────────────────

DENIAL_LIMITS = {
    "max_consecutive": 3,    # 连续 deny 次数阈值(对齐 CC)
    "max_total": 20,         # 单 session 累计 deny 次数阈值
}
"""
对齐 doc §4.5.4 + CC denialTracking.ts:
- max_consecutive=3:连续 3 次 deny → 触发 fallback(防止 LLM 反复尝试破坏)
- max_total=20:累计 20 次 deny → 触发 fallback(防止单 session 滥用)
"""


# ────────────────────────────────────────────────────────────────────
# DenialTrackingState — per-session state dataclass
# ────────────────────────────────────────────────────────────────────

@dataclass
class DenialTrackingState:
    """
    单 session 的 deny 计数 state(对齐 CC DenialTrackingState)

    字段:
    - consecutive_denials: 当前连续 deny 次数(success 时清零)
    - total_denials: 累计 deny 次数(永不重置,session 结束才 reset)
    """
    consecutive_denials: int = 0
    total_denials: int = 0


# ────────────────────────────────────────────────────────────────────
# record_denial / record_success — 更新 state
# ────────────────────────────────────────────────────────────────────

def record_denial(state: DenialTrackingState) -> DenialTrackingState:
    """
    记录一次 deny(increment both counters)

    Args:
        state: 当前 state

    Returns:
        新的 state(dataclass frozen 设计选择 → 用 replace 返新对象)
    """
    return replace(
        state,
        consecutive_denials=state.consecutive_denials + 1,
        total_denials=state.total_denials + 1,
    )


def record_success(state: DenialTrackingState) -> DenialTrackingState:
    """
    记录一次成功(reset consecutive,total 不变)

    Args:
        state: 当前 state

    Returns:
        新的 state(consecutive=0)
    """
    return replace(state, consecutive_denials=0)


# ────────────────────────────────────────────────────────────────────
# handle_denial_limit_exceeded — 超阈值 fallback
# ────────────────────────────────────────────────────────────────────

def handle_denial_limit_exceeded(state: DenialTrackingState) -> PermissionDecision:
    """
    超阈值时返 fallback PermissionDecision(对齐 CC handleDenialLimitExceeded)

    行为:
      - 返 ASK(OtherReason("denial_limit"))
      - 让用户显式确认(打破 LLM 自动 deny 攻击 loop)

    Args:
        state: 当前 state(可能已超 max_consecutive 或 max_total)

    Returns:
        PermissionDecision(behavior=ask)
    """
    reason_text = _reason_for_state(state)
    logger.warning("denial limit 触发 → fallback ASK: %s", reason_text)

    return PermissionDecision(
        behavior=PermissionBehavior.ASK.value,
        decision_reason=OtherReason(reason=reason_text),
        message=(
            "Multiple denials detected. Please review the current action carefully "
            "before allowing."
        ),
    )


def _reason_for_state(state: DenialTrackingState) -> str:
    """构造 reason 文本"""
    if state.consecutive_denials >= DENIAL_LIMITS["max_consecutive"]:
        return (
            f"denial_limit: {state.consecutive_denials} consecutive denials "
            f"(limit {DENIAL_LIMITS['max_consecutive']})"
        )
    if state.total_denials >= DENIAL_LIMITS["max_total"]:
        return (
            f"denial_limit: {state.total_denials} total denials "
            f"(limit {DENIAL_LIMITS['max_total']})"
        )
    return "denial_limit"


# ────────────────────────────────────────────────────────────────────
# Per-session state store(内存)
# ────────────────────────────────────────────────────────────────────

class _DenialStateStore:
    """per-session state 存储(线程安全,内存)"""

    def __init__(self):
        self._states: dict[str, DenialTrackingState] = {}
        self._lock = threading.Lock()

    def get(self, session_id: str) -> DenialTrackingState:
        """获取 session state(不存在则创建默认)"""
        with self._lock:
            if session_id not in self._states:
                self._states[session_id] = DenialTrackingState()
            return self._states[session_id]

    def set(self, session_id: str, state: DenialTrackingState) -> None:
        """设置 session state"""
        with self._lock:
            self._states[session_id] = state

    def reset(self, session_id: str) -> None:
        """重置 session state(session 结束)"""
        with self._lock:
            self._states.pop(session_id, None)

    def clear_all(self) -> None:
        """清空所有 state(测试用)"""
        with self._lock:
            self._states.clear()


# 全局单例 store(M1 简化:模块级)
_store = _DenialStateStore()


def get_denial_state(session_id: str) -> DenialTrackingState:
    """获取 session 当前 deny state(per-session 隔离)"""
    return _store.get(session_id)


def set_denial_state(session_id: str, state: DenialTrackingState) -> None:
    """设置 session state"""
    _store.set(session_id, state)


def reset_denial_state(session_id: str) -> None:
    """重置 session state(session 结束时调用)"""
    _store.reset(session_id)


def clear_all_denial_states() -> None:
    """清空所有 state(测试 / 全局 reset 用)"""
    _store.clear_all()


# ────────────────────────────────────────────────────────────────────
# check_denial_limit — 阈值检查
# ────────────────────────────────────────────────────────────────────

def check_denial_limit(state: DenialTrackingState) -> Optional[PermissionDecision]:
    """
    检查是否超阈值;超阈值返 fallback decision,否则返 None

    Args:
        state: 当前 state

    Returns:
        PermissionDecision(behavior=ask) 如果超阈值,否则 None
    """
    if state.consecutive_denials >= DENIAL_LIMITS["max_consecutive"]:
        return handle_denial_limit_exceeded(state)
    if state.total_denials >= DENIAL_LIMITS["max_total"]:
        return handle_denial_limit_exceeded(state)
    return None


def record_denial_for_session(session_id: str) -> DenialTrackingState:
    """record deny + 返新 state"""
    current = get_denial_state(session_id)
    new_state = record_denial(current)
    set_denial_state(session_id, new_state)
    return new_state


def record_success_for_session(session_id: str) -> DenialTrackingState:
    """record success + 返新 state"""
    current = get_denial_state(session_id)
    new_state = record_success(current)
    set_denial_state(session_id, new_state)
    return new_state
