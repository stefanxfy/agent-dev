"""
Task 5: 验证 DualChannelWriter.channel_b_background_extract 写入记忆时
会在 frontmatter extra 中带 session_id 和 turn_index,这样后续
MemoryStore.list_by_session() 能查到。

RED → GREEN 流程:
1. 初始状态:write 路径没传 session_id 到 extra → 断言失败
2. 修复 _do_channel_b_extract 的写盘段 → 断言通过
"""
import shutil
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

from agent_core.memory.dual_channel_writer import (
    DualChannelWriter,
    ExtractionCandidate,
    TurnMessage,
)
from agent_core.memory.memory_store import MemoryStore
from agent_core.memory.meta_db import MetaDB


def test_channel_b_writes_session_id_and_turn_index():
    tmp = Path(tempfile.mkdtemp(prefix="dual_test_"))
    try:
        meta = MetaDB(":memory:")
        store = MemoryStore(tmp)
        embed = MagicMock()
        embed.encode.return_value = [0.1] * 4
        vec = MagicMock()
        w = DualChannelWriter(
            session_id="sess-1",
            meta_db=meta,
            memory_store=store,
            vector_store=vec,
            embed_fn=embed,
        )

        candidates = [
            ExtractionCandidate(
                type="user",
                title="用户姓名",
                body="张三",
                source_quote="我叫张三",
            )
        ]
        msg = TurnMessage(turn_index=7, user_msg="u", assistant_resp="a")
        # 先把 turn 7 写到 daily log,让 daily_cursor 推进,这样 channel_b
        # 的 to_process 过滤器(start=0, end=daily_cursor=7)会包含 msg
        w.channel_a_inline_write("u", "a", turn_index=7)
        future = w.channel_b_background_extract(
            messages=[msg],
            llm_extractor=lambda _msgs: candidates,
        )
        future.result(timeout=10)

        # list_by_session 应能查到
        results = store.list_by_session(session_id="sess-1", since_turn=0)
        assert len(results) == 1
        assert results[0]["frontmatter"]["session_id"] == "sess-1"
        assert results[0]["frontmatter"]["turn_index"] == 7
    finally:
        w.shutdown(timeout=5)
        shutil.rmtree(tmp)
