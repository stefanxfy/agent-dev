"""SessionCounter 单测:单调 total + 双维度(metric × consumer)watermark。"""
from __future__ import annotations

from agent_core.session_counter import (
    COMPACTION,
    EXTRACTION,
    SessionCounter,
    TOOL,
    TOKEN,
)


class TestSessionCounter:
    def test_add_and_total_tool(self):
        c = SessionCounter()
        c.add_tool(1)
        c.add_tool(2)
        assert c.total_tool() == 3

    def test_since_default_zero_watermark_for_new_consumer(self):
        """新 consumer watermark 默认 0 → since = 全部历史值。"""
        c = SessionCounter()
        c.add_tool(5)
        assert c.since_tool(EXTRACTION) == 5
        assert c.since_tool(COMPACTION) == 5  # 多 consumer 各自从 0

    def test_mark_advances_watermark_and_resets_since(self):
        c = SessionCounter()
        c.add_tool(5)
        delta = c.mark_tool(EXTRACTION)
        assert delta == 5
        assert c.since_tool(EXTRACTION) == 0  # mark 后 since 归零
        c.add_tool(3)
        assert c.since_tool(EXTRACTION) == 3  # 只算新增量
        assert c.total_tool() == 8  # total 单调

    def test_multi_consumer_independent_watermarks(self):
        """各 consumer 水位独立,互不干扰。"""
        c = SessionCounter()
        c.add_tool(10)
        c.mark_tool(EXTRACTION)  # extraction 消费到 10
        assert c.since_tool(COMPACTION) == 10  # compaction 未消费,仍全量
        c.add_tool(5)
        assert c.since_tool(EXTRACTION) == 5   # 自 extraction 以来 +5
        assert c.since_tool(COMPACTION) == 15  # 自 compaction(从未)以来全量
        c.mark_tool(COMPACTION)
        assert c.since_tool(EXTRACTION) == 5   # compaction 的 mark 不影响 extraction
        assert c.since_tool(COMPACTION) == 0

    def test_multi_metric_independent(self):
        """tool / token 两条 metric 独立,mark 一条不影响另一条。"""
        c = SessionCounter()
        c.add_tool(3)
        c.add_token(100)
        assert c.total_tool() == 3
        assert c.total_token() == 100
        assert c.since_tool(EXTRACTION) == 3
        assert c.since_token(EXTRACTION) == 100
        c.mark_tool(EXTRACTION)
        assert c.since_token(EXTRACTION) == 100  # mark tool 不影响 token
        assert c.since_tool(EXTRACTION) == 0

    def test_token_api(self):
        c = SessionCounter()
        c.add_token(50)
        c.add_token(30)
        assert c.total_token() == 80
        assert c.since_token(EXTRACTION) == 80
        c.mark_token(EXTRACTION)
        assert c.since_token(EXTRACTION) == 0

    def test_generic_api_still_works_for_new_metric(self):
        """通用 API 供未来新 metric 使用。"""
        c = SessionCounter()
        c.add("widgets", 2)
        assert c.total("widgets") == 2
        assert c.since("widgets", "consumer_a") == 2
        c.mark("widgets", "consumer_a")
        assert c.since("widgets", "consumer_a") == 0
        c.add("widgets", 3)
        assert c.since("widgets", "consumer_a") == 3
