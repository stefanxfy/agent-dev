#!/usr/bin/env bash
# M2 / Day 2 验收 demo —— 双通道写入器（v2.1 脊柱）
# 跑法：bash scripts/demo_m2.sh   （无需参数）
# 前置：.venv/bin/python 已装好 pydantic / pyyaml / pytest
#       必须：sentence-transformers + bge-m3 + chromadb(由 setup_embeddings.sh 装)
set -euo pipefail

cd "$(dirname "$0")/.."   # 切到仓库根

export HF_HUB_OFFLINE="${HF_HUB_OFFLINE:-1}"

echo "=== M2 / Day 2 验收开始 ==="
echo

# Demo 1-4: 一次性脚本（4 demo 串跑）
.venv/bin/python <<'PYEOF'
import shutil, tempfile
from pathlib import Path
tmp = Path(tempfile.mkdtemp())
memory_root = tmp / "memory"
meta_db_path = str(tmp / "meta.db")
chroma_dir = tmp / "chroma"
chroma_dir.mkdir()

from agent_core.memory import (
    DualChannelWriter, MetaDB, MemoryStore, ChromaVectorStore, make_embed_fn,
    TurnMessage, ExtractionCandidate,
)

# === Demo 1: 基础写入(注意:vector_store + embed_fn 都是必填位置参数)===
db = MetaDB(meta_db_path)
store = MemoryStore(memory_root)
vec = ChromaVectorStore(chroma_dir, collection="demo_m2")
embed = make_embed_fn("bge-m3")
w = DualChannelWriter('demo_s1', db, store, vec, embed)
w.channel_a_inline_write('记住我叫小明', '已记', turn_index=0)
assert w.daily_cursor == 0
print('✅ Demo 1: channel A wrote')

# === Demo 2: 重启恢复 (A3) —— 必须用磁盘 MetaDB(:memory: 不能跨重启)===
w.shutdown(timeout=5)
db2 = MetaDB(meta_db_path)
store2 = MemoryStore(memory_root)
vec2 = ChromaVectorStore(chroma_dir, collection="demo_m2")  # 同 path 复用
embed2 = make_embed_fn("bge-m3")
w2 = DualChannelWriter('demo_s1', db2, store2, vec2, embed2)
assert w2.daily_cursor == 0  # 从磁盘恢复
print('✅ Demo 2: cursor persisted across restart')

# === Demo 3: 通道 B 后台提取 ===
def my_extractor(msgs):
    return [ExtractionCandidate(
        type='user', title='用户名字', body='用户叫小明',
        source_quote="我说'我叫小明'", tags=['identity']
    )]
w2.channel_a_inline_write('我叫小明', '好的', turn_index=1)
future = w2.channel_b_background_extract(
    [TurnMessage(0, '我叫小明', '好的'), TurnMessage(1, '我叫小明', '好的')],
    llm_extractor=my_extractor,
)
result = future.result(timeout=10)
assert result['written'] == 1
print(f"✅ Demo 3: channel B extracted {result}")

# === Demo 4: 崩溃恢复(场景 4)—— 重启后 extract_cursor 仍正确 ===
w2.shutdown(timeout=5)
vec3 = ChromaVectorStore(chroma_dir, collection="demo_m2")
embed3 = make_embed_fn("bge-m3")
w3 = DualChannelWriter('demo_s1', db2, store2, vec3, embed3)
assert w3.extract_cursor == 2  # turn 0 + 1 都已提取
items = w3.memory_store.list_by_type('user')
assert len(items) == 1
print(f'✅ Demo 4: crash recovery — extract_cursor={w3.extract_cursor}, files={len(items)}')

w3.shutdown(timeout=5)
shutil.rmtree(tmp, ignore_errors=True)
print()
print('=== M2 / Day 2 验收: 4/4 demo 全部通过 ===')
PYEOF

echo
echo "=== 并发 + 崩溃恢复 §4.5.1 场景 1/4 pytest 验收 ==="
.venv/bin/python -m pytest \
    tests/test_dual_channel_minimal.py::test_concurrent_channel_a_no_overwrite \
    tests/test_dual_channel_minimal.py::test_channel_b_crash_resume_continues \
    -v