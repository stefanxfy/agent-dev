"""
嵌入函数适配层（v2.1 §九.1）

M3 / Day 3 — L2 修复

设计要点：
1. **embed_fn 注入式**：retriever 通过 Protocol 接收嵌入函数,不耦合具体实现
2. **Lazy loading**：sentence-transformers / bge-m3 仅在首次调用时加载
   - M3 开发期无需下载 2.3GB 模型,使用 MockEmbedFn
   - 生产环境切换到 BGEM3EmbedFn
3. **MockEmbedFn**：基于 SHA-256 hash 的确定性 568 维向量
   - 同样的文本永远产生同样的向量（便于单测 + 调试）
   - 维度与 bge-m3 一致（568），保持切换无感
4. **fallback 链**：
   BGEM3EmbedFn → MiniLMEmbedFn → MockEmbedFn（按优先级）
   任意一个失败,降级到下一个
5. **不引入新依赖**：sentence-transformers / chromadb 都是设计要求的依赖

切换方法：
  - 环境变量：MEMORY_EMBED_PROVIDER=real|mock|auto（默认 auto）
  - 代码：MemoryConfig(embed_provider="mock")  # 强制 Mock
"""

from __future__ import annotations

import hashlib
import math
import os
import threading
from typing import Optional, Protocol, runtime_checkable

from agent_core.exceptions import StorageError


# ──────────────────────────────────────────────────────────────────
# 异常
# ──────────────────────────────────────────────────────────────────

class EmbeddingError(StorageError):
    """嵌入函数失败"""
    code = "EMBEDDING"


# ──────────────────────────────────────────────────────────────────
# Protocol
# ──────────────────────────────────────────────────────────────────

@runtime_checkable
class EmbedFn(Protocol):
    """
    嵌入函数接口（retriever 依赖此抽象）

    实现要求：
    - encode(text) → 568 维 list[float]（与 bge-m3 对齐）
    - 同样的 text 必须产生同样的向量（确定性）
    - normalize_embeddings=True （cosine similarity 友好）
    """
    dimension: int

    def encode(self, text: str) -> list[float]: ...


# ──────────────────────────────────────────────────────────────────
# MockEmbedFn（开发 / 单测用）
# ──────────────────────────────────────────────────────────────────

class MockEmbedFn:
    """
    基于 SHA-256 hash 的确定性嵌入函数

    - 同样的 text → 同样的 568 维向量
    - 不同 text 的向量是"伪随机"的(均匀分布)
    - L2 归一化(cosine friendly)
    - **不是语义向量**，仅用于：
      * 单测(确定性)
      * 开发机无 bge-m3 时
      * CI 环境

    用法：
        embed = MockEmbedFn()
        vec = embed.encode("我叫小明")  # 长度 568,float
    """

    dimension = 568

    def encode(self, text: str) -> list[float]:
        # 1. 扩展到足够长的 hash（SHA-256 输出 32 字节 = 256 bit）
        #    568 维需要 568 * 4 = 2272 字节 = 568 个 uint32
        #    用 SHAKE-256 风格的迭代 hash
        needed_bytes = self.dimension * 4  # 4 bytes per float
        seed = hashlib.sha256(text.encode("utf-8")).digest()

        # 链式扩展:每次 hash(seed + counter)产生新 32 字节
        chunks = []
        counter = 0
        while len(b"".join(chunks)) < needed_bytes:
            chunk = hashlib.sha256(seed + counter.to_bytes(4, "little")).digest()
            chunks.append(chunk)
            counter += 1
        raw = b"".join(chunks)[:needed_bytes]

        # 2. 转换为 float (-1, 1)
        import struct
        vec = []
        for i in range(self.dimension):
            # 4 bytes → uint32 → (0, 1) → (-1, 1)
            u = struct.unpack("<I", raw[i*4:(i+1)*4])[0]
            vec.append((u / 0xFFFFFFFF) * 2 - 1)

        # 3. L2 归一化
        norm = math.sqrt(sum(x*x for x in vec))
        if norm > 0:
            vec = [x / norm for x in vec]
        return vec

    def encode_batch(self, texts: list[str]) -> list[list[float]]:
        return [self.encode(t) for t in texts]


# ──────────────────────────────────────────────────────────────────
# BGEM3EmbedFn（生产用，lazy load）
# ──────────────────────────────────────────────────────────────────

class BGEM3EmbedFn:
    """
    BAAI/bge-m3 嵌入函数（生产用）

    - Lazy load：首次 encode() 才加载模型（节省启动时间）
    - 自动用本地 HF cache（~/.cache/huggingface/hub/）
    - L2 归一化
    - 失败 fallback 到 MockEmbedFn（不阻塞系统）

    模型加载：
      sentence_transformers.SentenceTransformer('BAAI/bge-m3')
      # 首次自动下载 ~2.3GB, 之后从 cache 加载 (~5-10s)
    """

    dimension = 568

    def __init__(self, model_name: str = "BAAI/bge-m3"):
        self.model_name = model_name
        self._model = None
        self._lock = threading.Lock()
        self._load_error: Optional[Exception] = None

    def _ensure_loaded(self):
        """线程安全的 lazy load（首次调用时执行）"""
        if self._model is not None:
            return
        with self._lock:
            if self._model is not None:
                return
            try:
                from sentence_transformers import SentenceTransformer
                self._model = SentenceTransformer(self.model_name)
            except Exception as e:
                self._load_error = e
                raise EmbeddingError(
                    f"加载嵌入模型 {self.model_name} 失败: {e}",
                    cause=e,
                )

    def encode(self, text: str) -> list[float]:
        self._ensure_loaded()
        vec = self._model.encode(text, normalize_embeddings=True)
        # numpy.ndarray → list[float]
        return vec.tolist() if hasattr(vec, "tolist") else list(vec)

    def encode_batch(self, texts: list[str]) -> list[list[float]]:
        self._ensure_loaded()
        vecs = self._model.encode(texts, normalize_embeddings=True, batch_size=32)
        return [v.tolist() if hasattr(v, "tolist") else list(v) for v in vecs]


# ──────────────────────────────────────────────────────────────────
# MiniLMEmbedFn（轻量 fallback,英文场景）
# ──────────────────────────────────────────────────────────────────

class MiniLMEmbedFn:
    """
    all-MiniLM-L6-v2 嵌入函数（轻量 fallback）

    - 80MB 模型（vs bge-m3 的 2.3GB）
    - 仅英文可用，中文质量差
    - 维度 384（与 bge-m3 不一致！retriever 需重新初始化 collection）
    """

    dimension = 384

    def __init__(self, model_name: str = "all-MiniLM-L6-v2"):
        self.model_name = model_name
        self._model = None
        self._lock = threading.Lock()

    def _ensure_loaded(self):
        if self._model is not None:
            return
        with self._lock:
            if self._model is not None:
                return
            try:
                from sentence_transformers import SentenceTransformer
                self._model = SentenceTransformer(self.model_name)
            except Exception as e:
                raise EmbeddingError(
                    f"加载嵌入模型 {self.model_name} 失败: {e}",
                    cause=e,
                )

    def encode(self, text: str) -> list[float]:
        self._ensure_loaded()
        vec = self._model.encode(text, normalize_embeddings=True)
        return vec.tolist() if hasattr(vec, "tolist") else list(vec)

    def encode_batch(self, texts: list[str]) -> list[list[float]]:
        self._ensure_loaded()
        vecs = self._model.encode(texts, normalize_embeddings=True, batch_size=32)
        return [v.tolist() if hasattr(v, "tolist") else list(v) for v in vecs]


# ──────────────────────────────────────────────────────────────────
# Factory
# ──────────────────────────────────────────────────────────────────

def make_embed_fn(
    provider: str = "auto",
    model_name: Optional[str] = None,
) -> EmbedFn:
    """
    工厂函数（按 provider 选择实现）

    Args:
        provider:
            - "auto":   先试 bge-m3 → MiniLM → Mock
            - "bge-m3": 强制 BGEM3EmbedFn（生产）
            - "minilm": 强制 MiniLMEmbedFn（英文轻量）
            - "mock":   强制 MockEmbedFn（开发 / 单测）
            - "real":   别名 = bge-m3
        model_name: 覆盖默认模型名（可选）

    Returns:
        EmbedFn 实例

    Examples:
        embed = make_embed_fn()                    # auto, 优先 bge-m3
        embed = make_embed_fn("mock")              # 单测
        embed = make_embed_fn("auto", model_name="BAAI/bge-small-zh-v1.5")
    """
    provider = provider.lower()

    # 显式 Mock：直接返回
    if provider == "mock":
        return MockEmbedFn()

    # MiniLM 强制
    if provider == "minilm":
        return MiniLMEmbedFn(model_name or "all-MiniLM-L6-v2")

    # bge-m3 强制 / real
    if provider in ("bge-m3", "real"):
        return BGEM3EmbedFn(model_name or "BAAI/bge-m3")

    # auto: 优先级链
    # 1) 尝试 BGEM3EmbedFn（如果模型已在 cache 或可下载）
    # 2) 失败 → MiniLMEmbedFn
    # 3) 再失败 → MockEmbedFn（保证系统总能跑）
    if provider == "auto":
        # 检查 env 是否强制 Mock
        env_force = os.environ.get("MEMORY_EMBED_PROVIDER", "").lower()
        if env_force == "mock":
            return MockEmbedFn()

        try:
            return BGEM3EmbedFn(model_name or "BAAI/bge-m3")
        except EmbeddingError:
            try:
                return MiniLMEmbedFn("all-MiniLM-L6-v2")
            except EmbeddingError:
                return MockEmbedFn()

    raise ValueError(
        f"未知 embed provider: {provider!r}，"
        f"必须为 auto/mock/real/bge-m3/minilm 之一"
    )


__all__ = [
    "EmbedFn",
    "MockEmbedFn",
    "BGEM3EmbedFn",
    "MiniLMEmbedFn",
    "make_embed_fn",
    "EmbeddingError",
]