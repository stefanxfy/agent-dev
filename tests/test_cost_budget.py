"""M10 C6.2: 成本预算守卫 — 4 测试用例"""
from unittest.mock import MagicMock

import pytest

from agent_core.memory.cost_tracker import CostTracker, BudgetExceeded


def test_cost_tracker_add_and_todays_total():
    """add() 累加,todays_total() 返回累计"""
    ct = CostTracker(daily_budget_usd=10.0)
    ct.add(input_tokens=1000, output_tokens=500)
    total = ct.todays_total()
    assert total > 0
    # 1500 tokens * 0.001/1000 = 0.0015
    assert abs(total - 0.0015) < 1e-9


def test_cost_tracker_disabled_does_not_accumulate():
    """enabled=False → add() 不累计"""
    ct = CostTracker(enabled=False)
    ct.add(1000, 500)
    assert ct.todays_total() == 0.0


def test_cost_tracker_check_budget_returns_exception_when_exceeded():
    """超预算 → check_budget() 返回 BudgetExceeded 实例"""
    ct = CostTracker(daily_budget_usd=0.0001)  # 极小预算
    ct.add(input_tokens=1000, output_tokens=1000)
    err = ct.check_budget()
    assert isinstance(err, BudgetExceeded)
    assert err.budget == 0.0001
    assert err.today_total > 0.0001


def test_extraction_gate_raises_budget_exceeded():
    """gate._call_llm 检到超预算 → 抛 BudgetExceeded"""
    from agent_core.memory.extraction_gate import ExtractionGate

    ct = CostTracker(daily_budget_usd=0.0001)
    ct.add(1000, 1000)  # 立即超预算
    gate = ExtractionGate(
        llm_router=MagicMock(),
        memory_store=MagicMock(),
        session_id="s1",
        cost_tracker=ct,
    )
    with pytest.raises(BudgetExceeded):
        gate._call_llm("test prompt")