"""M10 C6.3: latency timeout 异常"""
from __future__ import annotations


class LatencyTimeout(Exception):
    """LLM 调用超过 latency 预算"""
    def __init__(self, timeout: float):
        self.timeout = timeout
        super().__init__(f"LatencyTimeout: LLM call exceeded {timeout}s")


__all__ = ["LatencyTimeout"]