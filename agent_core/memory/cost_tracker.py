"""M10 C6.2: 成本累计 + 预算守卫

简化实现:每 1000 tokens = $0.001(Anthropic 通用 placeholder)。
精确 provider rates(M3.5/M4 Sonnet 等)留后续 task 加。
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Optional


class BudgetExceeded(Exception):
    """当日累计 cost 超过 daily_budget_usd"""
    def __init__(self, today_total: float, budget: float):
        self.today_total = today_total
        self.budget = budget
        super().__init__(f"BudgetExceeded: ${today_total:.4f} > ${budget:.4f}")


# 简化成本估算:每 1000 tokens = $0.001(Anthropic Claude 通用)
_COST_PER_1K_TOKENS_USD = 0.001


@dataclass
class CostTracker:
    """线程安全的当日 cost 累计器"""
    daily_budget_usd: float = 1.0
    enabled: bool = True
    _todays_total: float = 0.0
    _last_reset_at: float = field(default_factory=time.time)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def add(self, input_tokens: int, output_tokens: int) -> float:
        """累计一次 LLM 调用的 cost(以 USD 计)"""
        if not self.enabled:
            return 0.0
        with self._lock:
            self._maybe_reset()
            cost = (input_tokens + output_tokens) / 1000.0 * _COST_PER_1K_TOKENS_USD
            self._todays_total += cost
            return cost

    def todays_total(self) -> float:
        """返回当日累计 cost(USD)"""
        with self._lock:
            self._maybe_reset()
            return self._todays_total

    def check_budget(self) -> Optional[BudgetExceeded]:
        """若超预算 → 返回 BudgetExceeded 异常;否则 None"""
        if not self.enabled:
            return None
        total = self.todays_total()
        if total > self.daily_budget_usd:
            return BudgetExceeded(today_total=total, budget=self.daily_budget_usd)
        return None

    def _maybe_reset(self) -> None:
        """跨日重置(简化:24 小时一周期)"""
        if time.time() - self._last_reset_at > 86400:
            self._todays_total = 0.0
            self._last_reset_at = time.time()


__all__ = ["CostTracker", "BudgetExceeded"]