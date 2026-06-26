"""
web/app.py 的记忆系统 wiring 辅助模块(2026-06-26 拆出)

为什么独立成模块:
- web/app.py 在模块顶层 import streamlit,测试环境无法直接 import
- 把记忆系统 wiring 提到无 streamlit 依赖的纯函数,便于单测
- 任何忘记传 memory_index 的 wiring bug 都会被回归测试抓到

用法(web/app.py):
    from web.memory_wiring import build_memory_system
    ms = build_memory_system(
        memory_root=mem_root,
        chroma_path=chroma_path,
        llm_config=config,
        memory_config=memory_config,
        session_id=session_id or "default",
    )
    if ms is None:
        # init 失败,降级为无记忆模式
        ...
    else:
        memory_store, vec_store, embed_fn, retriever, dual, bridge, idx = ms
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional, Tuple

from agent_core.config import config as _agent_config
from agent_core.llm.router import LLMConfig, LLMRouter
from agent_core.memory.config import MemoryConfig
from agent_core.memory.dedup import make_llm_dedup_judge

logger = logging.getLogger(__name__)


# 7 元组: memory_store, vec_store, embed_fn, retriever, dual_channel, bridge, memory_index
MemorySystemBundle = Tuple[
    "object",  # MemoryStore
    "object",  # ChromaVectorStore
    "object",  # EmbedFn
    "object",  # MemoryRetriever
    "object",  # DualChannelWriter
    "object",  # ReactMemoryBridge
    "object",  # MemoryIndex
]


def resolve_agent_data_dir() -> Path:
    """AGENT_DATA_DIR 为空时 fallback 到 ~/.agent_data(与 config.py 默认约定一致)"""
    return Path(
        _agent_config.agent_data_dir or str(Path.home() / ".agent_data")
    )


def build_memory_system(
    memory_root: Path,
    chroma_path: Path,
    llm_config: LLMConfig,
    memory_config: MemoryConfig,
    session_id: str,
    collection_name: str = "react_demo",
) -> Optional[MemorySystemBundle]:
    """
    构造 Streamlit app 的完整记忆系统(M11 wiring)。

    关键约束(2026-06-26 修复):必须把 memory_index 传给 DualChannelWriter,
    否则写盘后 DualChannelWriter._write_memory 路径的 mark_dirty() 守卫失败,
    MEMORY.md 永远不会自动更新(只有手动 rebuild 才生效)。

    Returns:
        7 元组 (memory_store, vec_store, embed_fn, retriever, dual_channel,
                react_memory_bridge, memory_index);任意一步失败返回 None。
    """
    try:
        from agent_core.memory import (
            ChromaVectorStore,
            DualChannelWriter,
            ExtractionGate,
            MemoryRetriever,
            MemoryStore,
            MetaDB,
            ReactMemoryBridge,
            make_embed_fn,
        )
        from agent_core.memory.memory_index import MemoryIndex

        memory_root.mkdir(parents=True, exist_ok=True)
        chroma_path.mkdir(parents=True, exist_ok=True)

        # 1. MemoryStore(per-file .md frontmatter)
        memory_store = MemoryStore(memory_root)

        # 2. Chroma 向量库(只存 {id, embedding},M11 协议)
        vec_store = ChromaVectorStore(str(chroma_path), collection=collection_name)

        # 3. Embed fn(默认 MiniLM,无 bge 下载)
        memory_embed_fn = make_embed_fn()

        # 4. 独立 router 实例(避免主 agent 路由的 retry/backoff 干扰)
        #    同时供给 retriever(sideQuery 二次精选)+ gate(extraction 决策)
        extractor_router = LLMRouter(llm_config)

        # 5. Retriever(读 .md frontmatter + Chroma k-NN 混合打分)
        #    M11: 注入 llm_router,否则 side_query 模式降级返空(2026-06-26 反馈)
        memory_retriever = MemoryRetriever(
            memory_store=memory_store,
            vector_store=vec_store,
            embed_fn=memory_embed_fn,
            llm_router=extractor_router,
        )

        # 6. MetaDB + CostTracker + Gate
        agent_data_dir = resolve_agent_data_dir()
        meta_db_path = agent_data_dir / "meta.db"
        meta_db = MetaDB(meta_db_path)

        from agent_core.memory.cost_tracker import CostTracker
        cost_tracker = CostTracker(
            daily_budget_usd=memory_config.cost.daily_budget_usd,
            enabled=memory_config.cost.enabled,
        )

        gate = ExtractionGate(
            llm_router=extractor_router,
            memory_store=memory_store,
            session_id=session_id,
            cost_tracker=cost_tracker,
        )

        # 7. 语义去重 judge(可选)
        dedup_judge = make_llm_dedup_judge(extractor_router)

        # 8. MemoryIndex —— 关键:必须 hoist 并传给 DualChannelWriter,
        #    否则写盘后 mark_dirty() 守卫失败,MEMORY.md 不会自动更新
        memory_index = MemoryIndex(memory_root)
        memory_index.rebuild()  # 兜底:首次启动 MEMORY.md 可能不存在

        # 9. DualChannelWriter(双通道脊柱,异步提取)
        dual_channel = DualChannelWriter(
            session_id=session_id,
            meta_db=meta_db,
            memory_store=memory_store,
            vector_store=vec_store,
            embed_fn=memory_embed_fn,
            dedup_config=memory_config.dedup,
            dedup_judge=dedup_judge,
            memory_index=memory_index,  # M11: 写盘后 mark_dirty → 1s 内异步 rebuild
        )

        # 10. ReactMemoryBridge(严格双通道事件桥)
        react_memory_bridge = ReactMemoryBridge(
            dual_channel=dual_channel,
            gate=gate,
            memory_store=memory_store,
            session_id=session_id,
        )

        # 11. event_callback 绑定(让 bridge 能接收 secret/edit/precondition 事件)
        dual_channel.event_callback = (
            lambda evt: react_memory_bridge._enqueue_secret_event(evt)
            if react_memory_bridge
            else None
        )

        return (
            memory_store,
            vec_store,
            memory_embed_fn,
            memory_retriever,
            dual_channel,
            react_memory_bridge,
            memory_index,
        )
    except Exception as e:
        logger.warning(f"Memory system init failed: {e}")
        return None


def wire_memory_into_agent(
    agent_kwargs: dict,
    bundle: MemorySystemBundle,
) -> dict:
    """
    把 build_memory_system 返回的 bundle 注入到 ReactAgent 构造参数里。

    之所以独立成函数:让 web/app.py 一行调用即可,避免在主入口重复 6 行解构。
    """
    memory_store, _vec, _embed, retriever, _dual, bridge, _idx = bundle
    agent_kwargs["memory_store"] = memory_store
    agent_kwargs["memory_retriever"] = retriever
    agent_kwargs["react_memory_bridge"] = bridge
    return agent_kwargs