"""Chroma 严格分离契约回归测试 — 防止 metadata/document 偷偷回来。

T1+T2 锁定的契约:ChromaVectorStore 只存 {id, embedding},
query() 只返回 [{id, distance}]。
本测试是契约保护,任何对 chroma_store.add() / query() 形状的修改
如果偷偷带回 metadata/document 字段,会失败。
"""
import pytest
from agent_core.memory.chroma_store import ChromaVectorStore, ChromaStoreError
from conftest import FakeEmbedFn


def test_add_payload_no_metadata_no_document(tmp_path):
    """Chroma 只存 id + embedding;不应有 metadata/document 字段。"""
    embed = FakeEmbedFn()
    vec = ChromaVectorStore(tmp_path / "c", collection="t_con")
    vec.add("user/abc123", embed.encode("hello"))

    # 用 chromadb 低层 API 验证
    raw = vec._collection.get(include=["metadatas", "documents"])
    metadatas = raw.get("metadatas") or []
    documents = raw.get("documents") or []
    for m in metadatas:
        # 允许 None 或空 dict,但不允许 type/title/tags/session_id 等结构化字段
        if m is None:
            continue
        assert not any(k in m for k in ("type", "title", "tags", "session_id")), (
            f"意外 metadata: {m}"
        )
    for d in documents:
        # Chroma upsert 不传 document 时,document 字段为 None 或空字符串
        assert d in (None, ""), f"意外 document: {d!r}"


def test_query_returns_only_id_and_distance_keys(tmp_path):
    embed = FakeEmbedFn()
    vec = ChromaVectorStore(tmp_path / "c", collection="t_con")
    vec.add("a", embed.encode("hi"))
    hits = vec.query(embed.encode("hi"), top_k=1)
    assert set(hits[0].keys()) == {"id", "distance"}


def test_add_rejects_legacy_dict_shape(tmp_path):
    """防止有人误用旧 dict-shape 调用 vec.add({id, embedding, metadata, document})"""
    vec = ChromaVectorStore(tmp_path / "c", collection="t_con")
    with pytest.raises(TypeError):
        vec.add({"id": "a", "embedding": [0.1, 0.2, 0.3, 0.4]})


def test_add_rejects_metadata_kwarg(tmp_path):
    vec = ChromaVectorStore(tmp_path / "c", collection="t_con")
    with pytest.raises(TypeError):
        vec.add("a", [0.1, 0.2, 0.3, 0.4], metadata={"title": "t"})


def test_add_rejects_document_kwarg(tmp_path):
    vec = ChromaVectorStore(tmp_path / "c", collection="t_con")
    with pytest.raises(TypeError):
        vec.add("a", [0.1, 0.2, 0.3, 0.4], document="body")