"""
嵌入函数适配层（v2.1 §九.1）

M3 / Day 3 —— 生产用嵌入函数

设计要点：
1. **embed_fn 注入式**：retriever 通过 Protocol 接收嵌入函数,不耦合具体实现
2. **Lazy loading**：BGEM3EmbedFn 仅在首次 encode() 时加载模型（节省启动时间）
3. **失败显式**：model 加载/推理失败时直接 raise EmbeddingError,不静默降级
   - 项目非 demo:bge-m3 / MiniLM 任何失败必须让 caller 知道
   - 没有 Mock 兜底,系统不能"看起来在跑但实际语义检索是垃圾"
4. **不引入新依赖**：sentence-transformers 是设计要求的依赖

切换方法:
  - 环境变量:MEMORY_EMBED_PROVIDER=auto|bge-m3|minilm(默认 auto)
  - 代码:make_embed_fn("bge-m3")

注: bge-m3 实际维度是 **1024**(官方发布规格,基于 XLM-RoBERTa-large)
"""

from __future__ import annotations

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

    实现要求:
    - encode(text) → 1024 维 list[float](bge-m3 默认,与其他实现对齐需重新建 collection)
    - 同样的 text 必须产生同样的向量(确定性)
    - normalize_embeddings=True (cosine similarity 友好)
    """
    dimension: int

    def encode(self, text: str) -> list[float]: ...


# ──────────────────────────────────────────────────────────────────
# BGEM3EmbedFn(生产用,默认)
# ──────────────────────────────────────────────────────────────────

class BGEM3EmbedFn:
    """
    BAAI/bge-m3 嵌入函数(生产默认)

    - Lazy load:首次 encode() 才加载模型(节省启动时间)
    - 自动用本地 HF cache(~/.cache/huggingface/hub/)
    - L2 归一化(cosine friendly)
    - 加载失败立即 raise EmbeddingError,不静默

    模型加载:
      sentence_transformers.SentenceTransformer('BAAI/bge-m3')
      # 首次自动下载 ~2.3GB, 之后从 cache 加载 (~5-10s)
    """

    dimension = 1024

    def __init__(self, model_name: str = "BAAI/bge-m3"):
        self.model_name = model_name
        self._model = None
        self._lock = threading.Lock()
        self._load_error: Optional[Exception] = None

    def _ensure_loaded(self):
        """线程安全的 lazy load(首次调用时执行)"""
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
                    f"加载嵌入模型 {self.model_name} 失败: {e}。"
                    f"运行 bash scripts/setup_embeddings.sh 安装模型,或检查 HF cache",
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
# MiniLMEmbedFn(英文轻量 fallback)
# ──────────────────────────────────────────────────────────────────

class MiniLMEmbedFn:
    """
    all-MiniLM-L6-v2 嵌入函数(英文轻量)

    - 80MB 模型(vs bge-m3 的 2.3GB)
    - 仅英文可用,中文质量差
    - 维度 384(与 bge-m3 不一致!retriever 需重新初始化 collection)
    - 加载失败立即 raise EmbeddingError
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
# Factory(无 Mock 兜底,失败显式 raise)
# ──────────────────────────────────────────────────────────────────

def make_embed_fn(
    provider: str = "auto",
    model_name: Optional[str] = None,
) -> EmbedFn:
    """
    工厂函数(按 provider 选择实现)

    Args:
        provider:
            - "auto":   默认 → bge-m3(失败立即 raise,不静默降级)
            - "bge-m3": 强制 BGEM3EmbedFn(生产)
            - "minilm": 强制 MiniLMEmbedFn(英文轻量)
            - "real":   别名 = bge-m3
        model_name: 覆盖默认模型名(可选)

    Returns:
        EmbedFn 实例

    Raises:
        EmbeddingError: 模型加载失败时(不是静默 fallback)
        ValueError: provider 未知

    Examples:
        embed = make_embed_fn()                    # auto, bge-m3
        embed = make_embed_fn("bge-m3")            # 显式
        embed = make_embed_fn("minilm")            # 英文轻量
        embed = make_embed_fn("auto", model_name="BAAI/bge-small-zh-v1.5")
    """
    provider = provider.lower()

    # MiniLM 强制
    if provider == "minilm":
        return MiniLMEmbedFn(model_name or "all-MiniLM-L6-v2")

    # bge-m3 强制 / real
    if provider in ("bge-m3", "real"):
        return BGEM3EmbedFn(model_name or "BAAI/bge-m3")

    # auto: 默认走 bge-m3,失败立即 raise
    if provider == "auto":
        # 兼容旧 env(MEMORY_EMBED_PROVIDER=mock 已废弃,但允许显式走 minilm)
        env_force = os.environ.get("MEMORY_EMBED_PROVIDER", "").lower()
        if env_force == "minilm":
            return MiniLMEmbedFn("all-MiniLM-L6-v2")
        if env_force == "bge-m3":
            return BGEM3EmbedFn(model_name or "BAAI/bge-m3")
        # 默认/auto → bge-m3,加载失败立即 raise
        return BGEM3EmbedFn(model_name or "BAAI/bge-m3")

    raise ValueError(
        f"未知 embed provider: {provider!r}，"
        f"必须为 auto/real/bge-m3/minilm 之一(已移除 mock 选项)"
    )


__all__ = [
    "EmbedFn",
    "BGEM3EmbedFn",
    "MiniLMEmbedFn",
    "make_embed_fn",
    "EmbeddingError",
]