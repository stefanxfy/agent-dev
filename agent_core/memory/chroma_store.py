"""
ChromaDB 向量存储实现（v2.1 §九.1）

生产用 vector_store —— 唯一依赖 ChromaDB,不提供 Mock fallback。

设计要点:
1. **单一接口**:同时实现 VectorStoreProtocol(M2 用:add+count) + VectorSearchable(M3 用:+query)
   - 双通道写入只用 add/count
   - 检索用 query(embedding, top_k)
2. **维度强校验**:create_collection 时锁定 dim,add() 校验 embedding 维度,不匹配立即 raise
   - 防止 bge-m3 1024 维向量被塞进 MiniLM 384 维 collection 的灾难
3. **持久化**:PersistentClient + 路径,跨进程/重启数据保留
4. **幂等**:id 重复 add 自动覆盖(ChromaDB upsert 语义)
5. **不在 import 时强依赖**:try/except ImportError,给出明确指引

调用入口:
    vec = ChromaVectorStore("/path/to/chroma_data", collection="memories")
    vec.add({"id": "...", "embedding": [...], "metadata": {...}, "document": "..."})
    hits = vec.query(embedding=[...], top_k=5)
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any, Optional

from agent_core.exceptions import StorageError
from agent_core.memory.embeddings import EmbedFn, make_embed_fn


# ──────────────────────────────────────────────────────────────────
# 异常
# ──────────────────────────────────────────────────────────────────

class ChromaStoreError(StorageError):
    """ChromaDB 向量存储异常"""
    code = "CHROMA_STORE"


# ──────────────────────────────────────────────────────────────────
# 实现
# ──────────────────────────────────────────────────────────────────

class ChromaVectorStore:
    """
    基于 ChromaDB 的向量存储(v2.1 §九.1 生产实现)

    - 满足 VectorStoreProtocol(add/count) + VectorSearchable(+query)
    - PersistentClient 持久化到磁盘
    - 维度由首次 add 的 embedding 决定,之后 add() 强制校验
    - 失败时 raise ChromaStoreError,不静默 fallback

    Args:
        path: ChromaDB 持久化目录(会自动创建)
        collection: collection 名(同一 path 内唯一)
        dimension: 嵌入维度(可选;若不传,首次 add 时从 embedding 推断并锁定)
        distance: 距离函数,"cosine" / "l2" / "ip" 之一(默认 cosine,与 bge-m3 一致)

    用法:
        vec = ChromaVectorStore("/tmp/chroma", collection="memories")
        vec.add({
            "id": "abc123",
            "embedding": [0.1, 0.2, ...],   # 1024 维 for bge-m3
            "metadata": {"type": "user", "title": "..."},
            "document": "用户叫小明",
        })
        hits = vec.query(embedding=[...], top_k=5)
        # → [{"id": ..., "metadata": ..., "document": ..., "distance": ...}, ...]
    """

    def __init__(
        self,
        path: str | Path,
        collection: str = "memories",
        dimension: Optional[int] = None,
        distance: str = "cosine",
    ):
        # 延迟 import,启动时不强依赖 chromadb
        try:
            import chromadb
        except ImportError as e:
            raise ChromaStoreError(
                "chromadb 未安装。运行: bash scripts/setup_embeddings.sh "
                "(包含 chromadb + bge-m3 安装)",
                cause=e,
            )

        self._path = str(path)
        self._collection_name = collection
        self._declared_dim = dimension  # 用于校验
        self._distance = distance
        self._lock = threading.Lock()   # add/query 串行化

        Path(self._path).mkdir(parents=True, exist_ok=True)
        self._client = chromadb.PersistentClient(path=self._path)

        # 关键:持久化场景下,collection 可能已存在(上次创建)
        # 构造时先尝试 get_collection,失败才 lazy create
        self._collection = self._try_existing_collection()
        self._locked_dim: Optional[int] = dimension
        if self._collection is not None and self._locked_dim is None:
            # 从已存在 collection 反推 dim(peek 第一条 embedding)
            try:
                peek = self._collection.peek(limit=1)
                if peek and peek.get("embeddings") and peek["embeddings"][0]:
                    self._locked_dim = len(peek["embeddings"][0])
            except Exception:
                pass   # peek 失败不影响,后续 add 会校验

    # ──────────────────────────────────────────────
    # 内部:获取/创建 collection
    # ──────────────────────────────────────────────

    def close(self) -> None:
        """显式关闭 client(释放 fd)

        单测 fixture 必须调用,否则 100+ 测试累积 fd 触发
        OSError: [Errno 24] Too many open files

        也支持 with 块:
            with ChromaVectorStore(...) as vec:
                vec.add(...)
        """
        if self._client is None:
            return
        try:
            self._client.reset()  # close 所有 collection handles
        except Exception:
            pass
        # PersistentClient 没有 close,但 reset + 删引用让 GC 回收
        self._client = None
        self._collection = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
        return False

    def _try_existing_collection(self):
        """构造时尝试复用已存在的 collection(持久化场景)"""
        try:
            return self._client.get_collection(name=self._collection_name)
        except Exception:
            # 不存在 / 路径新建 / chromadb 异常 → 都返回 None,走 lazy create
            return None

    def _get_or_create_collection(self, embedding_dim: int):
        """首次 add 时根据 embedding_dim 创建 collection 并锁定"""
        import chromadb

        if self._collection is not None:
            return self._collection

        # 校验 declared_dim 与实际一致
        if self._declared_dim is not None and self._declared_dim != embedding_dim:
            raise ChromaStoreError(
                f"declared dimension {self._declared_dim} 与实际 embedding "
                f"维度 {embedding_dim} 不匹配,collection 拒绝创建"
            )

        try:
            # 新版 chromadb (>=0.4.18) 支持 metadata 锁 distance
            self._collection = self._client.get_or_create_collection(
                name=self._collection_name,
                metadata={"hnsw:space": self._distance},
            )
        except TypeError:
            # 老版 chromadb 不支持 metadata 参数
            self._collection = self._client.get_or_create_collection(
                name=self._collection_name,
            )

        self._locked_dim = embedding_dim
        return self._collection

    def _validate_embedding_dim(self, embedding: list[float]) -> None:
        """add 时校验 embedding 维度与已锁定 dim 一致"""
        actual_dim = len(embedding)
        if self._locked_dim is None:
            return   # 尚未锁定(首次 add 前)
        if actual_dim != self._locked_dim:
            raise ChromaStoreError(
                f"embedding 维度 {actual_dim} 与 collection 已锁定维度 "
                f"{self._locked_dim} 不匹配(可能换了 embed_fn?)"
            )

    # ──────────────────────────────────────────────
    # VectorStoreProtocol 接口
    # ──────────────────────────────────────────────

    def add(self, id: str, embedding: list[float]) -> None:
        """添加一条向量(只存 id + embedding,无 metadata / document)

        Args:
            id: 唯一标识(item_hash,用作 ChromaDB id)
            embedding: 嵌入向量(维度必须一致)

        Raises:
            ChromaStoreError: 字段缺失 / 维度不匹配 / chromadb 错误
        """
        if not isinstance(id, str) or not id:
            raise ChromaStoreError(f"id 必须是非空 str,实际为 {type(id).__name__}")
        if not isinstance(embedding, list) or not embedding:
            raise ChromaStoreError(
                f"embedding 必须是 list[float],实际为 {type(embedding).__name__}"
            )

        self._validate_embedding_dim(embedding)

        with self._lock:
            coll = self._get_or_create_collection(len(embedding))
            try:
                coll.upsert(ids=[id], embeddings=[embedding])
            except Exception as e:
                raise ChromaStoreError(
                    f"ChromaDB add 失败(id={id}): {e}", cause=e,
                )

    def count(self) -> int:
        """返回当前 collection 中的向量数"""
        if self._collection is None:
            return 0
        try:
            return self._collection.count()
        except Exception as e:
            raise ChromaStoreError(f"ChromaDB count 失败: {e}", cause=e)

    def all(self) -> list[dict]:
        """列出所有 doc(调试 / 备份用)"""
        if self._collection is None:
            return []
        try:
            res = self._collection.get()
        except Exception as e:
            raise ChromaStoreError(f"ChromaDB get 失败: {e}", cause=e)
        out: list[dict] = []
        ids = res.get("ids", [])
        embs = res.get("embeddings", []) or []
        metas = res.get("metadatas", []) or []
        docs = res.get("documents", []) or []
        for i, id_ in enumerate(ids):
            out.append({
                "id": id_,
                "embedding": list(embs[i]) if i < len(embs) else None,
                "metadata": metas[i] if i < len(metas) else {},
                "document": docs[i] if i < len(docs) else "",
            })
        return out

    # ──────────────────────────────────────────────
    # VectorSearchable 接口(retriever 用)
    # ──────────────────────────────────────────────

    def query(self, embedding: list[float], top_k: int) -> list[dict]:
        """
        向量检索 top_k

        Args:
            embedding: 查询向量(必须与 collection dim 一致)
            top_k: 返回前 k 条

        Returns:
            [{"id", "metadata", "document", "distance"}, ...] 按 distance 升序
        """
        self._validate_embedding_dim(embedding)

        with self._lock:
            if self._collection is None:
                return []
            try:
                res = self._collection.query(
                    query_embeddings=[embedding],
                    n_results=top_k,
                )
            except Exception as e:
                raise ChromaStoreError(
                    f"ChromaDB query 失败: {e}", cause=e
                )

        # ChromaDB 返回 {ids: [[]], metadatas: [[]], distances: [[]], documents: [[]]}
        if not res or not res.get("ids") or not res["ids"][0]:
            return []
        ids = res["ids"][0]
        metas = (res.get("metadatas") or [[]])[0]
        docs = (res.get("documents") or [[]])[0]
        dists = (res.get("distances") or [[]])[0]
        out: list[dict] = []
        for i, id_ in enumerate(ids):
            out.append({
                "id": id_,
                "metadata": metas[i] if i < len(metas) else {},
                "document": docs[i] if i < len(docs) else "",
                "distance": float(dists[i]) if i < len(dists) else 0.0,
            })
        return out


# ──────────────────────────────────────────────────────────────────
# Factory:embed_fn + vector_store 一站式构造
# ──────────────────────────────────────────────────────────────────

def make_chroma_store(
    path: str | Path,
    collection: str = "memories",
    *,
    embed_provider: str = "auto",
    dimension: Optional[int] = None,
) -> tuple[ChromaVectorStore, EmbedFn]:
    """
    工厂:同时构造 ChromaVectorStore 和 embed_fn,并校验维度对齐

    Args:
        path: ChromaDB 持久化路径
        collection: collection 名
        embed_provider: 传给 make_embed_fn 的 provider(auto/bge-m3/minilm/mock)
        dimension: 显式声明维度(可选;不传则从 embed_fn.dimension 取)

    Returns:
        (vec, embed_fn) 二元组 —— 维度已对齐保证

    Raises:
        ChromaStoreError: 维度对齐失败 / chromadb 未装
        EmbeddingError:    embed_fn 加载失败

    用法:
        vec, embed = make_chroma_store("/tmp/chroma")
        text_emb = embed.encode("用户叫小明")
        vec.add({"id": "h1", "embedding": text_emb, "metadata": {...}})
    """
    embed_fn = make_embed_fn(embed_provider)
    expected_dim = dimension or embed_fn.dimension

    vec = ChromaVectorStore(
        path=path,
        collection=collection,
        dimension=expected_dim,
    )
    return vec, embed_fn


__all__ = [
    "ChromaVectorStore",
    "ChromaStoreError",
    "make_chroma_store",
]