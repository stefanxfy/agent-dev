"""
Chroma add() 边界契约测试 — 严格分离(方案 A)
Chroma 只存 {id, embedding},metadata/document/dict-shape 一律拒绝
"""
import os
import sys
import threading
from pathlib import Path

import pytest

# 让 tests/ 能 import conftest
sys.path.insert(0, str(Path(__file__).parent))

from agent_core.memory.chroma_store import (
    ChromaVectorStore,
    ChromaStoreError,
)
from conftest import FakeEmbedFn


def _make_vec(tmp_path, name: str = "test_coll") -> ChromaVectorStore:
    """每个测试一个独立 chroma 目录(pid+tid 避免并发冲突)"""
    chroma_dir = tmp_path / f"{name}_{os.getpid()}_{threading.get_ident()}"
    # 不传 dimension:由首次 add 的 embedding 决定(避免 embed.encode() 1024-dim 与
    # 字面量 [0.1]*4 测试在 _declared_dim 上冲突)
    return ChromaVectorStore(
        chroma_dir, collection=name
    )


def test_add_accepts_only_id_and_embedding(tmp_path):
    """新契约:vec.add(id, embedding) 位置参数只接 2 个,内部只 upsert 这两列"""
    embed = FakeEmbedFn()
    vec = _make_vec(tmp_path)
    vec.add("a", embed.encode("hi"))
    assert vec.count() == 1


def test_add_rejects_metadata_kwarg(tmp_path):
    """metadata= 必须 TypeError(防止有人误传)"""
    vec = _make_vec(tmp_path)
    with pytest.raises(TypeError):
        vec.add("a", [0.1, 0.2, 0.3, 0.4], metadata={"title": "t"})


def test_add_rejects_document_kwarg(tmp_path):
    """document= 必须 TypeError"""
    vec = _make_vec(tmp_path)
    with pytest.raises(TypeError):
        vec.add("a", [0.1, 0.2, 0.3, 0.4], document="body")


def test_add_dict_shape_legacy_rejected(tmp_path):
    """旧 dict-shape 调用 vec.add({id, embedding, metadata, document}) 必须 TypeError"""
    vec = _make_vec(tmp_path)
    with pytest.raises(TypeError):
        vec.add({"id": "a", "embedding": [0.1, 0.2, 0.3, 0.4]})


def test_add_rejects_empty_embedding(tmp_path):
    """embedding=[] 必须 ChromaStoreError"""
    vec = _make_vec(tmp_path)
    with pytest.raises(ChromaStoreError):
        vec.add("a", [])


def test_add_rejects_empty_id(tmp_path):
    """id='' 必须 ChromaStoreError"""
    vec = _make_vec(tmp_path)
    with pytest.raises(ChromaStoreError):
        vec.add("", [0.1, 0.2, 0.3, 0.4])


def test_add_rejects_non_string_id(tmp_path):
    """id 非 str 必须 ChromaStoreError"""
    vec = _make_vec(tmp_path)
    with pytest.raises(ChromaStoreError):
        vec.add(123, [0.1, 0.2, 0.3, 0.4])


def test_add_stores_only_id_and_embedding_under_the_hood(tmp_path):
    """内部 chromadb collection 只写 ids + embeddings,不该有 metadatas/documents 字段"""
    embed = FakeEmbedFn()
    vec = _make_vec(tmp_path)
    vec.add("user/abc", embed.encode("hello"))

    raw = vec._collection.get(include=["metadatas", "documents"])
    metas = raw.get("metadatas", [])
    docs = raw.get("documents", [])
    # 新代码不传 metadata/document → chromadb 应返回空 / None
    for m in metas:
        assert m in (None, {}), f"metadata 残留: {m!r}"
    for d in docs:
        assert d in (None, ""), f"document 残留: {d!r}"
