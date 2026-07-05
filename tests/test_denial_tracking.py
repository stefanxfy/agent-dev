"""
denial_tracking.py 测试

覆盖:
1. record_denial / record_success 计数器
2. DENIAL_LIMITS 阈值
3. handle_denial_limit_exceeded fallback → ASK
4. check_denial_limit 阈值检查
5. per-session state 隔离 + reset
"""

from __future__ import annotations

import pytest

from agent_core.tools.denial_tracking import (
    DENIAL_LIMITS,
    DenialTrackingState,
    check_denial_limit,
    clear_all_denial_states,
    get_denial_state,
    handle_denial_limit_exceeded,
    record_denial,
    record_denial_for_session,
    record_success,
    record_success_for_session,
    reset_denial_state,
    set_denial_state,
)
from agent_core.tools.permission_types import (
    OtherReason,
    PermissionBehavior,
    PermissionDecision,
)


@pytest.fixture(autouse=True)
def _clear_global_store():
    """每个 test 前后清空全局 store"""
    clear_all_denial_states()
    yield
    clear_all_denial_states()


# ────────────────────────────────────────────────────────────────────
# DENIAL_LIMITS
# ────────────────────────────────────────────────────────────────────

class TestDenialLimits:
    def test_max_consecutive_is_3(self):
        """max_consecutive = 3(对齐 CC)"""
        assert DENIAL_LIMITS["max_consecutive"] == 3

    def test_max_total_is_20(self):
        """max_total = 20(对齐 CC)"""
        assert DENIAL_LIMITS["max_total"] == 20


# ────────────────────────────────────────────────────────────────────
# record_denial / record_success
# ────────────────────────────────────────────────────────────────────

class TestRecordDenial:
    def test_single_denial_increments_both(self):
        """单次 deny → 两个 counter 都 +1"""
        state = DenialTrackingState()
        new = record_denial(state)
        assert new.consecutive_denials == 1
        assert new.total_denials == 1

    def test_multiple_denials_accumulate(self):
        """多次 deny 累计"""
        state = DenialTrackingState()
        state = record_denial(state)
        state = record_denial(state)
        state = record_denial(state)
        assert state.consecutive_denials == 3
        assert state.total_denials == 3

    def test_original_state_unchanged(self):
        """dataclass 不可变:record_denial 不修改原 state"""
        state = DenialTrackingState()
        new = record_denial(state)
        assert state.consecutive_denials == 0
        assert new.consecutive_denials == 1


class TestRecordSuccess:
    def test_success_resets_consecutive(self):
        """success → consecutive=0,total 不变"""
        state = DenialTrackingState(consecutive_denials=5, total_denials=10)
        new = record_success(state)
        assert new.consecutive_denials == 0
        assert new.total_denials == 10

    def test_success_from_zero_state(self):
        """zero state success → 仍 zero"""
        state = DenialTrackingState()
        new = record_success(state)
        assert new.consecutive_denials == 0
        assert new.total_denials == 0


# ────────────────────────────────────────────────────────────────────
# handle_denial_limit_exceeded
# ────────────────────────────────────────────────────────────────────

class TestHandleDenialLimitExceeded:
    def test_returns_ask_decision(self):
        """超阈值 → ASK decision"""
        state = DenialTrackingState(consecutive_denials=3, total_denials=3)
        decision = handle_denial_limit_exceeded(state)
        assert isinstance(decision, PermissionDecision)
        assert decision.behavior == PermissionBehavior.ASK.value

    def test_other_reason(self):
        """reason 是 OtherReason('denial_limit: ...')"""
        state = DenialTrackingState(consecutive_denials=5, total_denials=5)
        decision = handle_denial_limit_exceeded(state)
        assert isinstance(decision.decision_reason, OtherReason)
        assert "denial_limit" in decision.decision_reason.reason
        assert "consecutive" in decision.decision_reason.reason

    def test_total_reason(self):
        """total 超阈值 → reason 含 'total'"""
        state = DenialTrackingState(consecutive_denials=0, total_denials=25)
        decision = handle_denial_limit_exceeded(state)
        assert "total" in decision.decision_reason.reason

    def test_message_non_empty(self):
        """decision 有 message(给用户看)"""
        state = DenialTrackingState(consecutive_denials=3, total_denials=3)
        decision = handle_denial_limit_exceeded(state)
        assert decision.message is not None
        assert len(decision.message) > 0


# ────────────────────────────────────────────────────────────────────
# check_denial_limit
# ────────────────────────────────────────────────────────────────────

class TestCheckDenialLimit:
    def test_under_threshold_returns_none(self):
        """未超阈值返 None"""
        state = DenialTrackingState(consecutive_denials=2, total_denials=10)
        assert check_denial_limit(state) is None

    def test_consecutive_limit_triggers(self):
        """consecutive = 3 → 触发"""
        state = DenialTrackingState(consecutive_denials=3, total_denials=3)
        decision = check_denial_limit(state)
        assert decision is not None
        assert decision.behavior == PermissionBehavior.ASK.value

    def test_consecutive_over_threshold_triggers(self):
        """consecutive > 3 → 触发"""
        state = DenialTrackingState(consecutive_denials=10, total_denials=10)
        decision = check_denial_limit(state)
        assert decision is not None

    def test_total_limit_triggers(self):
        """total = 20 → 触发(consecutive 不超)"""
        state = DenialTrackingState(consecutive_denials=0, total_denials=20)
        decision = check_denial_limit(state)
        assert decision is not None

    def test_total_over_threshold_triggers(self):
        """total > 20 → 触发"""
        state = DenialTrackingState(consecutive_denials=0, total_denials=100)
        decision = check_denial_limit(state)
        assert decision is not None

    def test_consecutive_priority(self):
        """consecutive 超阈值时优先 consecutive reason(不是 total)"""
        state = DenialTrackingState(consecutive_denials=5, total_denials=25)
        decision = check_denial_limit(state)
        assert "consecutive" in decision.decision_reason.reason


# ────────────────────────────────────────────────────────────────────
# per-session state store
# ────────────────────────────────────────────────────────────────────

class TestSessionStateStore:
    def test_get_creates_default(self):
        """get_denial_state 不存在则创建默认"""
        state = get_denial_state("session-1")
        assert state.consecutive_denials == 0
        assert state.total_denials == 0

    def test_get_returns_same_instance(self):
        """同 session 多次 get 返同一 state 对象"""
        s1 = get_denial_state("session-1")
        s2 = get_denial_state("session-1")
        # 注意:dataclass 不可变,可能每次返新对象(state 是 frozen 副本)
        # 验证状态一致即可
        assert s1 == s2

    def test_sessions_isolated(self):
        """不同 session 的 state 隔离"""
        record_denial_for_session("session-A")
        record_denial_for_session("session-A")
        record_denial_for_session("session-B")  # 只有 1 次

        state_a = get_denial_state("session-A")
        state_b = get_denial_state("session-B")
        assert state_a.consecutive_denials == 2
        assert state_b.consecutive_denials == 1

    def test_reset_denial_state(self):
        """reset 清空指定 session"""
        record_denial_for_session("session-1")
        assert get_denial_state("session-1").consecutive_denials == 1
        reset_denial_state("session-1")
        # reset 后 get 返新默认 state
        state = get_denial_state("session-1")
        assert state.consecutive_denials == 0

    def test_clear_all(self):
        """clear_all 清空所有"""
        record_denial_for_session("session-A")
        record_denial_for_session("session-B")
        clear_all_denial_states()
        assert get_denial_state("session-A").consecutive_denials == 0
        assert get_denial_state("session-B").consecutive_denials == 0


# ────────────────────────────────────────────────────────────────────
# record_denial_for_session / record_success_for_session
# ────────────────────────────────────────────────────────────────────

class TestRecordForSession:
    def test_denial_for_session_persists(self):
        """denial 写回 store"""
        result = record_denial_for_session("session-x")
        assert result.consecutive_denials == 1
        # 重新读 store 也应是 1
        assert get_denial_state("session-x").consecutive_denials == 1

    def test_success_for_session_resets_consecutive(self):
        """success 写回 store,consecutive 清零"""
        record_denial_for_session("session-y")
        record_denial_for_session("session-y")
        assert get_denial_state("session-y").consecutive_denials == 2
        result = record_success_for_session("session-y")
        assert result.consecutive_denials == 0
        assert get_denial_state("session-y").consecutive_denials == 0
        # total 仍是 2
        assert get_denial_state("session-y").total_denials == 2


# ────────────────────────────────────────────────────────────────────
# set_denial_state
# ────────────────────────────────────────────────────────────────────

class TestSetDenialState:
    def test_set_overwrites(self):
        """set 直接覆盖"""
        record_denial_for_session("session-z")
        set_denial_state("session-z", DenialTrackingState(consecutive_denials=100, total_denials=200))
        state = get_denial_state("session-z")
        assert state.consecutive_denials == 100
        assert state.total_denials == 200
