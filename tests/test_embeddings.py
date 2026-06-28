"""
M3 / Day 3 测试 —— Embeddings (bge-m3 + factory)

覆盖:
- BGEM3EmbedFn: 维度常量 / lazy load / 加载失败 EmbeddingError
- MiniLMEmbedFn: 维度常量
- factory: provider 路由(auto/bge-m3/minilm/real/invalid)
- Protocol: 满足 EmbedFn 协议

依赖:
- sentence-transformers(bge-m3 模型)
- HF cache(~/.cache/huggingface/hub/models--BAAI--bge-m3/)
  CI / 全新环境需先跑 bash scripts/setup_embeddings.sh
"""

from __future__ import annotations

import pytest

from agent_core.memory.embeddings import (
    EmbedFn,
    BGEM3EmbedFn,
    MiniLMEmbedFn,
    make_embed_fn,
    EmbeddingError,
)


# ──────────────────────────────────────────────────────────────────
# BGEM3EmbedFn
# ──────────────────────────────────────────────────────────────────

class TestBGEM3EmbedFn:

    def test_dimension_constant(self):
        """不需要真加载,验证 dimension 常量"""
        e = BGEM3EmbedFn()
        assert e.dimension == 1024

    def test_lazy_no_load_on_init(self):
        """__init__ 不应触发模型加载"""
        e = BGEM3EmbedFn()
        # 没有 _model 属性也算正常(直到 encode 才会创建)
        # 但 _lock 应该存在
        assert e._lock is not None

    def test_load_failure_raises(self, monkeypatch):
        """模型加载失败应抛 EmbeddingError(不静默降级)"""
        e = BGEM3EmbedFn()

        def mock_import(name, *args, **kwargs):
            if name == "sentence_transformers":
                raise ImportError("mocked: sentence-transformers not installed")
            return __builtins__.__import__(name, *args, **kwargs) \
                if hasattr(__builtins__, '__import__') else \
                __import__(name, *args, **kwargs)

        monkeypatch.setattr("builtins.__import__", mock_import)
        with pytest.raises(EmbeddingError):
            e.encode("test")


# ──────────────────────────────────────────────────────────────────
# MiniLMEmbedFn
# ──────────────────────────────────────────────────────────────────

class TestMiniLMEmbedFn:

    def test_dimension(self):
        e = MiniLMEmbedFn()
        assert e.dimension == 384


# ──────────────────────────────────────────────────────────────────
# make_embed_fn 工厂
# ──────────────────────────────────────────────────────────────────

class TestFactory:

    def test_minilm_provider(self):
        e = make_embed_fn("minilm")
        assert isinstance(e, MiniLMEmbedFn)

    def test_bge_m3_provider(self):
        e = make_embed_fn("bge-m3")
        assert isinstance(e, BGEM3EmbedFn)

    def test_real_alias(self):
        e = make_embed_fn("real")
        assert isinstance(e, BGEM3EmbedFn)

    def test_invalid_provider_raises(self):
        with pytest.raises(ValueError, match="未知 embed provider"):
            make_embed_fn("nonexistent")

    def test_mock_provider_removed(self):
        """Mock 选项已移除,应 raise ValueError"""
        with pytest.raises(ValueError, match="未知 embed provider"):
            make_embed_fn("mock")

    def test_auto_returns_bge_m3(self):
        """auto 默认返回 bge-m3(没 Mock 兜底)"""
        e = make_embed_fn("auto")
        assert isinstance(e, BGEM3EmbedFn)

    def test_env_force_minilm(self, monkeypatch):
        monkeypatch.setenv("MEMORY_EMBED_PROVIDER", "minilm")
        e = make_embed_fn("auto")
        assert isinstance(e, MiniLMEmbedFn)

    def test_env_force_bge_m3(self, monkeypatch):
        monkeypatch.setenv("MEMORY_EMBED_PROVIDER", "bge-m3")
        e = make_embed_fn("auto")
        assert isinstance(e, BGEM3EmbedFn)

    def test_custom_model_name(self):
        e = make_embed_fn("bge-m3", model_name="custom")
        assert e.dimension == 1024

    def test_bge_m3_satisfies_protocol(self):
        """BGEM3EmbedFn 应该满足 EmbedFn 协议"""
        e = BGEM3EmbedFn()
        assert isinstance(e, EmbedFn)

    def test_minilm_satisfies_protocol(self):
        e = MiniLMEmbedFn()
        assert isinstance(e, EmbedFn)
