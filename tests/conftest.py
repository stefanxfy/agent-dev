"""
共享测试夹具(Phase 4 / Step 4.4.7 抽出)

集中放置:
- FakeEmbedFn:确定性 1024 维向量(避开 bge-m3 模型加载)
- 8 个 pytest fixture:memory_root / logs_dir / meta_db_path / chroma_dir /
  meta_db / memory_store / writer / config

约定:所有 fixture 用 `tmp_path`(pytest 内置),不依赖真实磁盘路径。
"""
from __future__ import annotations

import hashlib
import os
import threading

import pytest

from agent_core.memory import DistillationConfig
from agent_core.memory.chroma_store import ChromaVectorStore
from agent_core.memory.dual_channel_writer import DualChannelWriter
from agent_core.memory.memory_store import MemoryStore
from agent_core.memory.meta_db import MetaDB


# ──────────────────────────────────────────────────────────────────
# 假 EmbedFn —— 确定性 1024 维向量,无模型加载
# ──────────────────────────────────────────────────────────────────

class FakeEmbedFn:
    """确定性伪嵌入(用于并发测试,避免 bge-m3 模型加载开销)

    同样的 text 产生同样的向量(hash → 展开成 1024 维)
    """
    dimension = 1024

    def encode(self, text: str) -> list[float]:
        digest = hashlib.sha256(text.encode("utf-8")).digest()
        # 32 字节 → 扩展到 1024 维(每字节重复 32 次)
        vec = []
        for _ in range(32):
            for b in digest:
                vec.append(b / 255.0)
        return vec


# ──────────────────────────────────────────────────────────────────
# Fixtures
# ──────────────────────────────────────────────────────────────────

@pytest.fixture
def memory_root(tmp_path):
    root = tmp_path / "memory"
    root.mkdir()
    return root


@pytest.fixture
def logs_dir(tmp_path):
    """logs 是 memory_root.parent 的子目录(双通道写入器约定)"""
    d = tmp_path / "logs"
    d.mkdir()
    return d


@pytest.fixture
def meta_db_path(tmp_path):
    return tmp_path / "meta.db"


@pytest.fixture
def chroma_dir(tmp_path):
    d = tmp_path / "chroma"
    d.mkdir()
    return d


@pytest.fixture
def meta_db(meta_db_path):
    return MetaDB(meta_db_path)


@pytest.fixture
def memory_store(memory_root):
    return MemoryStore(memory_root)


@pytest.fixture
def writer(meta_db, memory_store, memory_root, chroma_dir):
    """DualChannelWriter with FakeEmbedFn + Ephemeral chroma"""
    embed = FakeEmbedFn()
    chroma_path = chroma_dir / f"conftest_{os.getpid()}_{threading.get_ident()}"
    with ChromaVectorStore(str(chroma_path), collection="conftest_test") as vec:
        w = DualChannelWriter("s1", meta_db, memory_store, vec, embed)
        yield w
        w.shutdown(timeout=3)
        vec.close()


@pytest.fixture
def config():
    return DistillationConfig()
