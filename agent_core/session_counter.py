"""会话级计数器:单调 total + 双维度(metric × consumer)watermark。

设计背景(2026-07-03 重构):
tool/token 等计数是**跨子系统的通用原语**(extraction / compaction / 未来扩展
都需要),不应挂在任何单一消费者上(原挂在 react_memory_bridge 上导致
ContextCompactionHandler 读不到、token 受 memory gate 漏记)。

本模块提供中立宿主 `SessionCounter`,挂 agent(session 级):
- 累加在"资源发生点":tool → ToolExecuteHandler,token → LLM handler
- 消费者(extraction / compaction)读 `since(metric, consumer)` 做触发判断,
  处理后 `mark(metric, consumer)` 推进自己的水位,各消费者互不干扰。

两层 API:
- 通用(add/since/mark/total,带 metric 参数):供未来新 metric + 内部实现
- 专用(add_tool/add_token/since_tool/...):调用方免传 metric,常用

详见 docs/agent-state-machine-and-chain-of-responsibility-design.md 及
plans/merry-jingling-hellman.md。
"""
from __future__ import annotations

from collections import defaultdict
from typing import Dict, Tuple

# ── metric 常量 ──
TOOL = "tool"
TOKEN = "token"

# ── consumer 常量 ──
EXTRACTION = "extraction"
COMPACTION = "compaction"


class SessionCounter:
    """会话级计数器:每个 metric 一个单调递增 total,每个 (metric, consumer) 一个水位。

    语义:
    - `total(metric)`:自 session 开始以来该 metric 的累计总量(从不清零)。
    - `since(metric, consumer)`:该消费者自上次 `mark` 以来的增量(= total − watermark)。
      新消费者 watermark 默认 0 → 首次 since = 全部历史值;若要"从接入时刻开始计",
      接入时先 mark 一次把水位拉到当前 total。
    - `mark(metric, consumer)`:推进水位到当前 total,返回本次 delta。
    """

    def __init__(self) -> None:
        self._totals: Dict[str, int] = defaultdict(int)
        self._watermarks: Dict[Tuple[str, str], int] = {}

    # ── 通用 API(带 metric 参数)──
    def add(self, metric: str, n: int = 1) -> None:
        """累加某 metric(默认 +1)。"""
        self._totals[metric] += n

    def since(self, metric: str, consumer: str) -> int:
        """该消费者自上次 mark 以来的 metric 增量。"""
        return self._totals[metric] - self._watermarks.get((metric, consumer), 0)

    def mark(self, metric: str, consumer: str) -> int:
        """推进该消费者水位到当前 total,返回本次 delta。"""
        delta = self.since(metric, consumer)
        self._watermarks[(metric, consumer)] = self._totals[metric]
        return delta

    def total(self, metric: str) -> int:
        """该 metric 的累计总量(单调递增,从不清零)。"""
        return self._totals[metric]

    # ── 专用 API:tool ──
    def add_tool(self, n: int = 1) -> None:
        self.add(TOOL, n)

    def since_tool(self, consumer: str) -> int:
        return self.since(TOOL, consumer)

    def mark_tool(self, consumer: str) -> int:
        return self.mark(TOOL, consumer)

    def total_tool(self) -> int:
        return self.total(TOOL)

    # ── 专用 API:token ──
    def add_token(self, n: int) -> None:
        self.add(TOKEN, n)

    def since_token(self, consumer: str) -> int:
        return self.since(TOKEN, consumer)

    def mark_token(self, consumer: str) -> int:
        return self.mark(TOKEN, consumer)

    def total_token(self) -> int:
        return self.total(TOKEN)
