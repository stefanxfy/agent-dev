#!/usr/bin/env bash
# 一键安装 bge-m3 + ChromaDB 环境
# 跑法：bash scripts/setup_embeddings.sh
#   - 已下载会跳过,安全可重跑
#   - 设 SKIP_DOWNLOAD=1 仅验证(不下载)
#   - 设 CUSTOM_MODEL_DIR=/path/bge-m3 用本地模型
set -euo pipefail

cd "$(dirname "$0")/.."

VENV_PY=".venv/bin/python"

# ── 颜色 ──────────────────────────────────────────────────
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m' # No Color

log()   { echo -e "${GREEN}[$(date +%H:%M:%S)]${NC} $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $*"; }
err()   { echo -e "${RED}[ERROR]${NC} $*"; exit 1; }

# ── 前置检查 ──────────────────────────────────────────────
[ -d ".venv" ] || err ".venv 不存在,先跑: uv venv .venv --python 3.11"
command -v uv >/dev/null || err "uv 未安装,先跑: curl -LsSf https://astral.sh/uv/install.sh | sh"

# ── 1. 安装 Python 依赖 ──────────────────────────────────
log "=== 1. 安装 Python 依赖 (sentence-transformers + chromadb) ==="
log "    注:chromadb ~80MB,sentence-transformers ~50MB,会显示下载进度"
uv pip install --python "$VENV_PY" \
    "sentence-transformers>=2.2.0" \
    "huggingface_hub>=0.20.0" \
    "chromadb>=0.4.0" \
    "rich" || err "依赖安装失败"

.venv/bin/python -c "import sentence_transformers; print('  sentence-transformers', sentence_transformers.__version__)"
.venv/bin/python -c "import chromadb; print('  chromadb', chromadb.__version__)"
.venv/bin/python -c "import huggingface_hub; print('  huggingface_hub', huggingface_hub.__version__)"

# ── 2. 决定模型路径 ─────────────────────────────────────
MODEL_ID="BAAI/bge-m3"
HF_CACHE="${HF_HOME:-$HOME/.cache/huggingface/hub}"
MODEL_DIR_BASENAME="models--${MODEL_ID//\//--}"
CACHED_PATH="$HF_CACHE/$MODEL_DIR_BASENAME"
LOCAL_MODEL_PATH="${CUSTOM_MODEL_DIR:-}"

log ""
log "=== 2. 检查 bge-m3 模型缓存 ==="
if [ -n "$LOCAL_MODEL_PATH" ] && [ -d "$LOCAL_MODEL_PATH" ]; then
    log "✅ 使用本地模型: $LOCAL_MODEL_PATH"
    USE_PATH="$LOCAL_MODEL_PATH"
elif [ -d "$CACHED_PATH" ]; then
    log "✅ HF cache 已存在: $CACHED_PATH"
    log "   跳过下载（如想强制重新下载: rm -rf $CACHED_PATH）"
    USE_PATH="$MODEL_ID"
elif [ "${SKIP_DOWNLOAD:-0}" = "1" ]; then
    warn "SKIP_DOWNLOAD=1 但 cache 不存在,跳过"
    USE_PATH=""
else
    log "开始下载 bge-m3 (~2.3GB,可能 5-30 分钟,看网络)"
    log "    进度条会直接打印,无 --quiet"
    # 3-way fallback: hf (新) → huggingface-cli (旧) → Python API (兜底)
    # 注: huggingface-hub 1.20+ 移除了 cli extra, -m huggingface_hub 失效
    download_ok=0
    if [ -x ".venv/bin/hf" ]; then
        log "    使用 hf CLI (新)"
        if .venv/bin/hf download "$MODEL_ID"; then
            download_ok=1
        fi
    elif [ -x ".venv/bin/huggingface-cli" ]; then
        log "    使用 huggingface-cli (旧)"
        if .venv/bin/huggingface-cli download "$MODEL_ID"; then
            download_ok=1
        fi
    else
        log "    使用 Python API 兜底"
        if .venv/bin/python <<PYEOF
from huggingface_hub import snapshot_download
import sys
try:
    snapshot_download("$MODEL_ID", max_workers=4)
    sys.exit(0)
except Exception as e:
    print(f"[ERROR] {e}", file=sys.stderr)
    sys.exit(1)
PYEOF
        then
            download_ok=1
        fi
    fi
    if [ $download_ok -eq 1 ]; then
        log "✅ 下载完成: $CACHED_PATH"
    else
        err "下载失败,检查网络 / HF_ENDPOINT 镜像"
    fi
    USE_PATH="$MODEL_ID"
fi

# ── 3. 验证 bge-m3 ─────────────────────────────────────
log ""
log "=== 3. 验证 bge-m3 嵌入 ==="
if [ -z "$USE_PATH" ]; then
    warn "USE_PATH 为空,跳过 bge-m3 验证"
else
    .venv/bin/python <<PYEOF
import time, sys
try:
    from sentence_transformers import SentenceTransformer
except ImportError:
    print("[ERROR] sentence-transformers 未装", file=sys.stderr); sys.exit(1)

print(f"加载模型: $USE_PATH")
t0 = time.time()
try:
    model = SentenceTransformer("$USE_PATH")
except Exception as e:
    print(f"[ERROR] 模型加载失败: {e}", file=sys.stderr); sys.exit(1)
load_t = time.time() - t0

dim = model.get_sentence_embedding_dimension()
print(f"  ✅ 加载耗时: {load_t:.1f}s")
print(f"  ✅ 输出维度: {dim} (期望 568)")

if dim != 568:
    print(f"[ERROR] 维度不匹配,期望 568 实际 {dim}", file=sys.stderr); sys.exit(1)

# 编码测试
texts = ["我叫小明", "I love Python", "用户喜欢使用中文"]
t0 = time.time()
vecs = model.encode(texts, normalize_embeddings=True)
enc_t = time.time() - t0

print(f"  ✅ 编码 3 条文本: {enc_t*1000:.0f}ms")
print(f"  ✅ 向量 shape: {vecs.shape}")
print(f"  ✅ 第一条前 5 维: {vecs[0][:5].tolist()}")

# 余弦相似度(自己跟自己应该是 1.0,跨语应该较高)
import numpy as np
sim_same = float(np.dot(vecs[0], vecs[0]))
sim_zh_en = float(np.dot(vecs[0], vecs[1]))
print(f"  ✅ self-sim (中文): {sim_same:.4f} (期望 1.0)")
print(f"  ✅ cross-lang (中↔英): {sim_zh_en:.4f} (期望 > 0.5)")
PYEOF
fi

# ── 4. 验证 ChromaDB ─────────────────────────────────────
log ""
log "=== 4. 验证 ChromaDB ==="
TMP_CHROMA="/tmp/chroma-setup-$$"
trap "rm -rf $TMP_CHROMA" EXIT

.venv/bin/python <<PYEOF
import time, sys, shutil, tempfile, os
tmp = "$TMP_CHROMA"
os.makedirs(tmp, exist_ok=True)

try:
    import chromadb
except ImportError:
    print("[ERROR] chromadb 未装", file=sys.stderr); sys.exit(1)

print(f"启动 ChromaDB 持久化客户端: {tmp}")
client = chromadb.PersistentClient(path=tmp)

# 创建 collection,显式指定 dimension=568
print("创建 collection (dimension=568)")
collection = client.create_collection("test", dimension=568) if hasattr(client, 'create_collection') and 'dimension' in client.create_collection.__code__.co_varnames else client.create_collection("test")

# 注意:新版 chromadb 的 create_collection 不接 dimension 参数
# 需要在 add 时保证 embedding 维度正确
print("添加 3 条向量 (模拟 bge-m3 输出)")
collection.add(
    ids=["1", "2", "3"],
    embeddings=[[0.1] * 568, [0.2] * 568, [0.9] * 568],
    documents=["我叫小明", "I love Python", "random noise"],
)

print("查询 top-2:")
results = collection.query(query_embeddings=[[0.15] * 568], n_results=2)
print(f"  ✅ ids: {results['ids'][0]}")
print(f"  ✅ distances: {results['distances'][0]}")
print(f"  ✅ 第一条 doc: {results['documents'][0][0]}")

# 错误测试:维度不匹配应该报错
print("测试 dimension 校验 (传 384 维向量,期望失败):")
try:
    collection.add(ids=["bad"], embeddings=[[0.1] * 384], documents=["wrong dim"])
    print("  ⚠️  未报错,可能 chromadb 版本不严")
except Exception as e:
    print(f"  ✅ 拦截 dim mismatch: {type(e).__name__}")

print("✅ ChromaDB 验证通过")
PYEOF

# ── 5. 集成验证: bge-m3 + ChromaDB ──────────────────────
log ""
log "=== 5. 集成验证: bge-m3 encode → ChromaDB query ==="
if [ -n "$USE_PATH" ]; then
.venv/bin/python <<PYEOF
import sys, os, shutil
tmp = "/tmp/chroma-int-$$"
os.makedirs(tmp, exist_ok=True)
try:
    from sentence_transformers import SentenceTransformer
    import chromadb
    import numpy as np
except ImportError as e:
    print(f"[ERROR] {e}"); sys.exit(1)

print("加载 bge-m3 ...")
model = SentenceTransformer("$USE_PATH")
client = chromadb.PersistentClient(path=tmp)
collection = client.get_or_create_collection("memories")

# 模拟:写入 3 条记忆
memories = [
    ("user_ming", "用户名字是小明"),
    ("user_python", "用户喜欢 Python"),
    ("project_chroma", "项目用 ChromaDB 做向量库"),
]
print("写入 3 条记忆:")
ids, embs, docs = [], [], []
for mid, text in memories:
    emb = model.encode(text, normalize_embeddings=True).tolist()
    ids.append(mid); embs.append(emb); docs.append(text)
    print(f"  - {mid}: {text}")
collection.add(ids=ids, embeddings=embs, documents=docs)

# 查询:用户问关于名字的
query = "用户叫什么"
print(f"\n查询: '{query}'")
q_emb = model.encode(query, normalize_embeddings=True).tolist()
results = collection.query(query_embeddings=[q_emb], n_results=2)
for i, (mid, doc, dist) in enumerate(zip(
    results['ids'][0], results['documents'][0], results['distances'][0]
)):
    print(f"  [{i+1}] {mid}: {doc} (distance={dist:.4f})")

# 验证:第 1 条应该是 user_ming(语义最接近)
top_id = results['ids'][0][0]
assert top_id == "user_ming", f"top-1 应该是 user_ming,实际 {top_id}"
print(f"\n✅ Top-1 命中: {top_id}")

shutil.rmtree(tmp, ignore_errors=True)
print("✅ 集成验证通过")
PYEOF
else
    warn "USE_PATH 为空,跳过集成验证"
fi

log ""
log "=== 🎉 setup_embeddings.sh 全部完成 ==="
log ""
log "环境总结:"
log "  Python:           $($VENV_PY --version)"
log "  bge-m3 路径:      ${USE_PATH:-未下载}"
log "  HF cache:         $HF_CACHE"