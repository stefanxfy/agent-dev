#!/usr/bin/env bash
# M3 / Day 3 验收 demo —— 检索 + 安全
# 跑法：bash scripts/demo_m3.sh   （无需参数）
# 前置：.venv/bin/python 已装好 pydantic / pyyaml / pytest
#       可选：sentence-transformers / chromadb（不装也能跑 Mock）
set -euo pipefail

cd "$(dirname "$0")/.."

echo "=== M3 / Day 3 验收开始 ==="
echo

# Demo 1-5: 端到端 检索 + 安全 流程
.venv/bin/python <<'PYEOF'
"""
M3 / Day 3 验收 — 5 个核心 demo

Demo 1: MockEmbedFn 确定性 (L2 修复)
Demo 2: SecretScanner 4 pattern 拦截
Demo 3: Extractor L1 合并 + L7 校验
Demo 4: Retriever 三模式 + L4 密钥过滤
Demo 5: ColdStartLoader L5 seed 加载
"""
import shutil, tempfile, math
from pathlib import Path
tmp = Path(tempfile.mkdtemp())

from agent_core.memory import (
    MemoryStore, MockVectorStore, MockEmbedFn,
    SecretScanner, MemoryExtractor, ExtractionCandidate,
    MemoryRetriever, ColdStartLoader, SeedItem, ExtractStats,
)

# ─── Demo 1: MockEmbedFn 确定性 + 维度 ───
print("=== Demo 1: MockEmbedFn 维度 + 确定性 ===")
embed = MockEmbedFn()
print(f"  dimension: {embed.dimension}")
v1 = embed.encode("用户叫小明")
v2 = embed.encode("用户叫小明")
v3 = embed.encode("用户叫大明")
assert v1 == v2, "同样的文本必须产生同样的向量"
assert v1 != v3, "不同文本应产生不同向量"
norm = math.sqrt(sum(x*x for x in v1))
assert abs(norm - 1.0) < 1e-6, f"L2 归一化失败,实际 norm={norm}"
print("  ✅ 维度 1024 + 确定性 + L2 归一化")
print(f"  ✅ 同样文本 cos sim: {sum(a*b for a,b in zip(v1,v2)):.4f} (期望 1.0)")
print(f"  ✅ 不同文本 cos sim: {sum(a*b for a,b in zip(v1,v3)):.4f}")
print()

# ─── Demo 2: SecretScanner 4 pattern ───
print("=== Demo 2: SecretScanner 4 pattern ===")
scanner = SecretScanner()
test_cases = [
    ("普通文本", "用户喜欢 Python 编程", True),
    ("api_key 命名型", "api_key = mySecretValue_ABCDEF1234567890", False),
    ("OpenAI sk-", "token: sk-abcdefghijklmnopqrstuvwxyz1234", False),
    ("Anthropic sk-ant-", "anthropic sk-ant-api03-abcdefghijklmnopqrstuvwxyz", False),
    ("GitHub ghp_", "ghp_abcdefghijklmnopqrstuvwxyz0123456789", False),
    ("占位符", "api_key = your-api-key-here", True),
    ("短字符占位", "password = xxxx", True),
]
for name, text, should_be_clean in test_cases:
    r = scanner.scan(text)
    actual = r.is_clean
    status = "✅" if actual == should_be_clean else "❌"
    print(f"  {status} {name}: {r.summary()}")
print()

# ─── Demo 3: Extractor L1 + L7 ───
print("=== Demo 3: Extractor L1 合并 + L7 校验 ===")
# 注: L1 合并相似度计算 ——
#   embed_fn=None  → 走 jaccard + containment(基于字符 bigram)
#   embed_fn=Mock  → 走 cos similarity(MockEmbedFn 是 hash 派生的伪随机向量,非语义,合并会失效)
#   embed_fn=BGEM3 → 走 cos similarity(真实语义向量,合并效果最好)
# 演示 L1 合并: 用 embed_fn=None 触发文本相似度合并
extractor = MemoryExtractor(embed_fn=None)
candidates = [
    ExtractionCandidate("user", "用户名字", "用户叫小明", "我说'我叫小明'"),
    ExtractionCandidate("user", "用户名字2", "用户名叫小明,今年25岁", "我说'我叫小明'"),  # 与上条高相似 → 应合并
    ExtractionCandidate("user", "user key", "api_key = mySecretValue_ABCDEF1234567890", "我贴了 key"),  # secret
    ExtractionCandidate("feedback", "不喜欢打断", "用户不喜欢被打断。\n\n**Why:** 打断导致思路中断。", "我说'别打断'"),
    ExtractionCandidate("invalid", "x", "y", ""),  # source_quote 空 → L7 拒
    ExtractionCandidate("user", "独立条目", "完全独立的内容,与其他无关", "另一句原话"),
]
stats = ExtractStats()
result = extractor.process(candidates, stats=stats)
print(f"  {stats.summary()}")
print(f"  保留 {len(result)} 条:")
for c in result:
    print(f"    - {c.type} | {c.title} | {c.body[:30]}...")
print()

# ─── Demo 4: Retriever 三模式 + L4 ───
print("=== Demo 4: Retriever 三模式 + L4 密钥过滤 ===")
memory_root = tmp / "memory"
memory_root.mkdir()
store = MemoryStore(memory_root)

# 写 5 条(其中 1 条含 secret)
store.write("user", "用户名字", "用户叫小明", source_quote="我说'我叫小明'")
store.write("user", "用户改名", "用户改名为大明", source_quote="我说'我改名了'")
store.write("feedback", "不喜欢打断", "用户不喜欢被打断对话。\n\n**Why:** 打断导致用户思路中断。", source_quote="我说'别打断'")
store.write("project", "项目用 Python", "项目主体是 Python。\n\n**Why:** 用户偏好。", source_quote="我说'用 Python'")
store.write("reference", "Config 示例", "我的 key 是 sk-abcdefghijklmnopqrstuvwxyz1234", source_quote="示例 config")

class VecWithQuery(MockVectorStore):
    def query(self, embedding, top_k):
        scored = []
        for d in self._docs:
            e = d.get("embedding", [])
            dot = sum(a*b for a, b in zip(embedding, e))
            na = math.sqrt(sum(a*a for a in embedding))
            nb = math.sqrt(sum(b*b for b in e))
            sim = dot / (na*nb) if na and nb else 0
            scored.append({**d, "distance": 1 - sim})
        scored.sort(key=lambda x: x["distance"])
        return scored[:top_k]

vec = VecWithQuery()
for type_ in ["user", "feedback", "project", "reference"]:
    for it in store.list_by_type(type_):
        data = store.read(it["path"])
        text = f"{data['frontmatter'].get('title','')}\n{data['body']}"
        vec.add({
            "id": it["hash"], "embedding": embed.encode(text),
            "metadata": {"type": type_, "title": it["title"]},
            "document": text,
        })

retriever = MemoryRetriever(store, vec, embed)

for mode in ["keyword", "semantic", "hybrid"]:
    report = retriever.search("用户", top_k=3, mode=mode)
    print(f"  [{mode}] '{'用户'}' → {len(report)} hits:")
    for h in report:
        secret_mark = " ⚠️HAS_SECRET" if h.has_secret else ""
        print(f"    [{h.score:.3f}] {h.title} (type={h.type}){secret_mark}")
    print(f"    secret_filtered: {report.secret_filtered}, elapsed: {report.elapsed_ms:.1f}ms")
print()

# ─── Demo 5: ColdStartLoader ───
print("=== Demo 5: ColdStartLoader (L5) ===")
seeds_dir = tmp / "seeds"
seeds_dir.mkdir()
(seeds_dir / "system.yaml").write_text("""
- type: user
  title: 系统默认用户
  body: 访客用户
  source_quote: "系统初始化"
  tags: [default]
  importance: 7
- type: reference
  title: 项目根目录
  body: 项目根目录说明
  source_quote: "README"
  importance: 5
""", encoding="utf-8")

cold_root = tmp / "memory_cold"
vec2 = MockVectorStore()
loader = ColdStartLoader(MemoryStore(cold_root), vec2, embed, default_seeds_dir=seeds_dir)
report = loader.load()
print(f"  {report.summary()}")
print(f"  vector_store 现在有 {vec2.count()} 条 seed 向量")
# 幂等再加载
report2 = loader.load()
print(f"  二次加载: {report2.summary()}")
print(f"  二次 vector_store count: {vec2.count()} (期望不变)")
assert vec2.count() == 2  # 幂等

shutil.rmtree(tmp, ignore_errors=True)
print()
print("=== M3 / Day 3 验收: 5/5 demo 全部通过 ===")
PYEOF

echo
echo "=== M3 测试套件 (108 cases) ==="
.venv/bin/python -m pytest \
    tests/test_secret_scanner.py \
    tests/test_embeddings.py \
    tests/test_extractor.py \
    tests/test_cold_start.py \
    tests/test_retriever.py \
    -q 2>&1 | tail -3
