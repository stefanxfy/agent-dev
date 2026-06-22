import tempfile
from pathlib import Path
import shutil

from agent_core.memory.memory_store import MemoryStore


def test_list_by_session_filters_by_since_turn():
    tmp = Path(tempfile.mkdtemp(prefix="mem_test_"))
    try:
        store = MemoryStore(tmp)
        # 写 3 条,frontmatter 里 session_id 模拟
        for i, turn in enumerate([1, 5, 10]):
            store.write(
                type="user",
                title=f"记忆 {i}",
                body=f"body {i}",
                source_quote=f"q{i}",
                extra={"session_id": "s1", "turn_index": turn},
            )
        # since_turn=5 应返回 turn=5 和 turn=10 两条
        results = store.list_by_session(session_id="s1", since_turn=5)
        turn_indices = sorted(r["frontmatter"].get("turn_index", -1) for r in results)
        assert turn_indices == [5, 10]
    finally:
        shutil.rmtree(tmp)


def test_list_by_session_empty_when_no_match():
    tmp = Path(tempfile.mkdtemp(prefix="mem_test_"))
    try:
        store = MemoryStore(tmp)
        results = store.list_by_session(session_id="nonexistent", since_turn=0)
        assert results == []
    finally:
        shutil.rmtree(tmp)
