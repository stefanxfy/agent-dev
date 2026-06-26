"""
web/memory_wiring.py 回归测试(2026-06-26 M11 wiring 修复)

覆盖:
1. build_memory_system 必须 hoist MemoryIndex 并传给 DualChannelWriter
   (否则 DualChannelWriter._write_memory 后 mark_dirty 守卫失败,
    MEMORY.md 永远不会自动 rebuild)
2. 写盘后 1s 内 MEMORY.md 必须反映新文件
3. bundle 解构顺序正确(memory_store / vec / embed / retriever / dual / bridge / idx)
4. init 失败时返回 None,不抛

为什么独立于 web/app.py:
- web/app.py 在模块顶层 import streamlit,测试环境无法直接 import
- 把记忆系统 wiring 提到无 streamlit 依赖的纯函数,便于单测
"""

from __future__ import annotations

import shutil
import tempfile
import time
from pathlib import Path

import pytest


@pytest.fixture
def tmp_agent_data(monkeypatch):
    """隔离 ~/.agent_data 到临时目录,避免污染真实数据"""
    tmp = Path(tempfile.mkdtemp())
    yield tmp
    shutil.rmtree(tmp, ignore_errors=True)


@pytest.fixture
def fake_llm_config():
    """最小可用的 LLMConfig,不需要真实 API key(build_memory_system 不调 LLM)"""
    from agent_core.llm.router import LLMConfig

    return LLMConfig(
        provider="minimax",
        model="mock-embed",
        api_key="not-used",
        stream=False,
    )


@pytest.fixture
def memory_config():
    from agent_core.memory.config import MemoryConfig

    return MemoryConfig()


class TestBuildMemorySystemWiring:
    """核心 invariant:build_memory_system 必须把 MemoryIndex 注入 DualChannelWriter"""

    def test_dual_channel_has_memory_index(
        self, tmp_agent_data, fake_llm_config, memory_config
    ):
        """回归保护:dual.memory_index 不为 None"""
        from web.memory_wiring import build_memory_system

        bundle = build_memory_system(
            memory_root=tmp_agent_data / "memory",
            chroma_path=tmp_agent_data / "chroma",
            llm_config=fake_llm_config,
            memory_config=memory_config,
            session_id="s_test",
        )
        assert bundle is not None
        _store, _vec, _embed, _retr, dual_channel, _bridge, _idx = bundle
        assert dual_channel.memory_index is not None, (
            "DualChannelWriter.memory_index 必须非空,否则写盘后 MEMORY.md 不会自动 rebuild"
        )

    def test_bundle_shape(
        self, tmp_agent_data, fake_llm_config, memory_config
    ):
        """bundle 是 7 元组,顺序为 (store, vec, embed, retr, dual, bridge, idx)"""
        from web.memory_wiring import build_memory_system

        bundle = build_memory_system(
            memory_root=tmp_agent_data / "memory",
            chroma_path=tmp_agent_data / "chroma",
            llm_config=fake_llm_config,
            memory_config=memory_config,
            session_id="s_test",
        )
        assert len(bundle) == 7
        store, vec, embed, retr, dual, bridge, idx = bundle
        assert store is not None
        assert vec is not None
        assert embed is not None
        assert retr is not None
        assert dual is not None
        assert bridge is not None
        assert idx is not None

    def test_memory_index_root_matches(
        self, tmp_agent_data, fake_llm_config, memory_config
    ):
        """idx.root 必须 == memory_root(避免后续扫错目录)"""
        from web.memory_wiring import build_memory_system

        mem_root = tmp_agent_data / "memory"
        bundle = build_memory_system(
            memory_root=mem_root,
            chroma_path=tmp_agent_data / "chroma",
            llm_config=fake_llm_config,
            memory_config=memory_config,
            session_id="s_test",
        )
        _store, _vec, _embed, _retr, _dual, _bridge, idx = bundle
        assert idx.root.resolve() == mem_root.resolve()

    def test_retriever_has_llm_router(
        self, tmp_agent_data, fake_llm_config, memory_config
    ):
        """retriever.llm_router 必须非空 —— 否则 sideQuery 模式降级返空

        2026-06-26 反馈:用户 .env 配 MEMORY_RETRIEVAL__MODE=side_query,但日志出现
        'sideQuery 需要 llm_router,当前为 None,降级返空',因为之前没把 router 注入。
        """
        from web.memory_wiring import build_memory_system

        bundle = build_memory_system(
            memory_root=tmp_agent_data / "memory",
            chroma_path=tmp_agent_data / "chroma",
            llm_config=fake_llm_config,
            memory_config=memory_config,
            session_id="s_router",
        )
        _store, _vec, _embed, retriever, _dual, _bridge, _idx = bundle
        assert retriever.llm_router is not None, (
            "MemoryRetriever.llm_router 必须非空,否则 sideQuery 模式会降级返空"
        )

    def test_retriever_config_matches_memory_config(
        self, tmp_agent_data, fake_llm_config, memory_config
    ):
        """retriever.config 必须就是 caller 传入的 memory_config —— 否则
        .env 里的 MEMORY_RETRIEVAL__MIN_SCORE / __TOP_K / __MODE 全部失效

        2026-06-26 二次反馈:用户 .env 配 MEMORY_RETRIEVAL__MIN_SCORE=0.7,
        但 19:18:12 跑 "我叫什么名字" 仍返 5 hits(应该是 3 hits),原因就是
        retriever 用了默认 MemoryConfig(min_score=0.3)。
        """
        from agent_core.memory.config import RetrievalConfig
        from web.memory_wiring import build_memory_system

        # 用一个非默认 min_score 的 config,验证它能传过去
        custom_cfg = type(memory_config)(
            retrieval=RetrievalConfig(min_score=0.42),
        )
        bundle = build_memory_system(
            memory_root=tmp_agent_data / "memory",
            chroma_path=tmp_agent_data / "chroma",
            llm_config=fake_llm_config,
            memory_config=custom_cfg,
            session_id="s_cfg",
        )
        _store, _vec, _embed, retriever, _dual, _bridge, _idx = bundle
        assert retriever.config is custom_cfg, (
            "MemoryRetriever.config 必须就是传入的 memory_config,否则 .env "
            "里的 min_score/top_k/mode 全部失效"
        )
        assert retriever.config.retrieval.min_score == 0.42, (
            f"min_score 应是 0.42,实际 {retriever.config.retrieval.min_score}"
        )


class TestWriteTriggersMemoryIndexRebuild:
    """端到端:dual_channel 暴露的 memory_index 调用 mark_dirty 后,MEMORY.md 必须更新

    不直接调 store.write 触发(那不经过 dual_channel 内部守卫),
    而是通过 dual.memory_index.mark_dirty() 模拟 dual_channel 写盘后的行为,
    验证 timer 触发 → MEMORY.md 重建。这是 memory_index 自身的契约,
    由 test_dual_channel_concurrent.py::test_dual_channel_write_triggers_memory_index_mark_dirty
    保证 dual_channel 在写盘后会调它。
    """

    def test_mark_dirty_triggers_rebuild_within_2s(
        self, tmp_agent_data, fake_llm_config, memory_config
    ):
        """dual.memory_index.mark_dirty() → 1s Timer → MEMORY.md 重建"""
        from web.memory_wiring import build_memory_system

        mem_root = tmp_agent_data / "memory"
        bundle = build_memory_system(
            memory_root=mem_root,
            chroma_path=tmp_agent_data / "chroma",
            llm_config=fake_llm_config,
            memory_config=memory_config,
            session_id="s_e2e",
        )
        store, _vec, _embed, _retr, dual, _bridge, _idx = bundle

        # 写一条记忆(直接走 MemoryStore,模拟 dual_channel 内部已经写完)
        store.write(
            type="user",
            name="wiring_test",
            description="验证 wiring 修复",
            body="body content",
            source_quote="source",
        )

        # 模拟 dual_channel._write_memory 在写盘后调 mark_dirty
        dual.memory_index.mark_dirty()

        # 等 2s(1s Timer + 余量)
        deadline = time.time() + 2.0
        while time.time() < deadline:
            time.sleep(0.1)
            mm = mem_root / "MEMORY.md"
            if mm.exists() and "wiring_test" in mm.read_text(encoding="utf-8"):
                break

        mm = mem_root / "MEMORY.md"
        assert mm.exists(), "MEMORY.md 必须存在"
        content = mm.read_text(encoding="utf-8")
        assert "wiring_test" in content, (
            f"MEMORY.md 1s 内必须包含新条目,但实际内容:\n{content!r}"
        )

        # 清理:取消 timer 防止测试退出时悬挂线程
        dual.memory_index.flush()


class TestBuildMemorySystemErrorHandling:
    """init 失败时返回 None,不抛"""

    def test_invalid_memory_root_does_not_raise(
        self, tmp_agent_data, fake_llm_config, memory_config
    ):
        """memory_root 是 None-like 值时应被 try/except 捕获并返回 None"""
        from web.memory_wiring import build_memory_system

        # 传一个根本不能 mkdir 的路径(/dev/null/foo 在 Linux 是 read-only fs)
        bad_root = Path("/this/path/should/not/exist/and/not/be/creatable/foo")
        bundle = build_memory_system(
            memory_root=bad_root,
            chroma_path=tmp_agent_data / "chroma",
            llm_config=fake_llm_config,
            memory_config=memory_config,
            session_id="s_err",
        )
        # 可能成功(若 /this 实际可写)或返回 None;都不抛
        assert bundle is None or len(bundle) == 7