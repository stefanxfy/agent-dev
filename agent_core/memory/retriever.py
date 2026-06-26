"""
记忆检索器(M11)

设计要点：
1. **二选一模式**(M11):
   - semantic: 向量相似度
   - side_query: LLM 二次精选(见 T7)
2. **L4**: 检索时即时跑 SecretScanner → 命中即标记 has_secret
3. **L8**: 检索结果含 secret 字段时报警
4. **可观测**:
   - 返回 score 解释(每个 hit 的模式得分)
   - 检索耗时、过滤数

调用入口:
    retriever = MemoryRetriever(store, vec, embed_fn, config)
    report = retriever.search("用户叫什么", top_k=5, mode="semantic")
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, Protocol

from agent_core.exceptions import StorageError
from agent_core.memory.config import MemoryConfig
from agent_core.memory.memory_store import MemoryStore
from agent_core.memory.secret_scanner import (
    SecretScanner,
    get_default_scanner,
)
from agent_core.memory.tracing import tracer

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────
# 异常
# ──────────────────────────────────────────────────────────────────

class RetrievalError(StorageError):
    """检索失败"""
    code = "RETRIEVAL"


class RetrievalMode(str, Enum):
    """检索模式(M11 二选一)"""
    SEMANTIC = "semantic"
    # SIDE_QUERY = "side_query"  # T7 加


# ──────────────────────────────────────────────────────────────────
# 数据结构
# ──────────────────────────────────────────────────────────────────

@dataclass
class MemoryHit:
    """单条检索结果"""
    item_hash: str
    type: str
    title: str
    body: str
    rel_path: str
    score: float                                # 总分(0-1)
    breakdown: dict[str, float] = field(default_factory=dict)  # 模式→得分
    tags: list[str] = field(default_factory=list)
    importance: int = 5
    has_secret: bool = False                    # L4: 命中后被扫出含 secret


@dataclass
class RetrievalReport:
    """检索结果(含元数据)"""
    query: str
    mode: RetrievalMode
    hits: list[MemoryHit]
    total_candidates: int = 0
    secret_filtered: int = 0
    elapsed_ms: float = 0.0

    def __len__(self) -> int:
        return len(self.hits)

    def __iter__(self):
        return iter(self.hits)

    def __getitem__(self, i):
        return self.hits[i]

    def top(self, k: int = 5) -> list[MemoryHit]:
        return self.hits[:k]


# ──────────────────────────────────────────────────────────────────
# VectorStoreProtocol (retriever 视角)
# ──────────────────────────────────────────────────────────────────

# retriever 期望的 vector_store 接口
# ChromaVectorStore 必须实现 query()(见 chroma_store.py)
class VectorSearchable(Protocol):
    """retriever 用的 vector 接口(方案 A 严格分离契约)

    add() 只接 (id, embedding) 两个位置参数,不接 metadata/document。
    query() 只返回 [{id, distance}, ...],不返 metadata/document。
    """
    def add(self, id: str, embedding: list[float]) -> None: ...
    def count(self) -> int: ...
    def query(self, embedding: list[float], top_k: int) -> list[dict]: ...


# ──────────────────────────────────────────────────────────────────
# MemoryRetriever
# ──────────────────────────────────────────────────────────────────

class MemoryRetriever:
    """
    记忆检索器(M11 二选一)

    模式:
    - semantic: 向量相似度
    - side_query: LLM 二次精选(T7 接入)

    用法:
        retriever = MemoryRetriever(store, vec, embed_fn, config)
        report = retriever.search("用户叫什么", top_k=5, mode="semantic")
        for hit in report:
            print(hit.title, hit.score, hit.breakdown)
    """

    def __init__(
        self,
        memory_store: MemoryStore,
        vector_store,
        embed_fn,
        config: Optional[MemoryConfig] = None,
        secret_scanner: Optional[SecretScanner] = None,
    ):
        self.memory_store = memory_store
        self.vector_store = vector_store
        self.embed_fn = embed_fn
        self.config = config or MemoryConfig()
        self.secret_scanner = secret_scanner or get_default_scanner()

    # ── 公开 API ─────────────────────────────────────────

    def search(
        self,
        query: str,
        top_k: int = 5,
        mode: str | RetrievalMode = "semantic",
        types: Optional[list[str]] = None,
        min_score: float = 0.0,
        already_surfaced: Optional[set[str]] = None,  # M11 新增
    ) -> RetrievalReport:
        """
        检索记忆

        Args:
            query: 查询文本
            top_k: 返回 top N
            mode: semantic | side_query
            types: 限定类型(user/feedback/event/project/reference), None=全部
            min_score: 最低分阈值(过滤低相关)
            already_surfaced: M11:已展示过的 rel_path 集合,用于 side_query 去重

        Returns:
            RetrievalReport(hits=sorted by score desc)
        """
        with tracer.start_as_current_span("memory.search") as span:
            span.set_attribute("memory.search.query_len", len(query))
            span.set_attribute("memory.search.top_k", top_k)
            span.set_attribute("memory.search.mode", str(mode) if not isinstance(mode, str) else mode)

            if not query or not query.strip():
                empty = RetrievalReport(query=query, mode=RetrievalMode(mode), hits=[])
                span.set_attribute("memory.search.hits_count", 0)
                return empty

            if isinstance(mode, str):
                try:
                    mode = RetrievalMode(mode)
                except ValueError:
                    raise RetrievalError(f"未知检索模式: {mode!r}")

            t0 = time.time()
            # 多召 → 重排 → L4 过滤 → 截 top_k
            candidates = self._retrieve_candidates(query, top_k, mode, types, already_surfaced)
            ranked = self._rerank(candidates, mode)
            filtered = self._filter_secrets(ranked)
            final = filtered[:top_k]

            elapsed_ms = (time.time() - t0) * 1000

            report = RetrievalReport(
                query=query,
                mode=mode,
                hits=final,
                total_candidates=len(candidates),
                secret_filtered=len(ranked) - len(filtered),
                elapsed_ms=elapsed_ms,
            )
            span.set_attribute("memory.search.hits_count", len(final))
            span.set_attribute("memory.search.elapsed_ms", elapsed_ms)
            logger.debug(
                f"retriever.search({query!r}, {mode.value}, top_k={top_k}) → "
                f"{len(final)} hits ({report.secret_filtered} secret-filtered, "
                f"{elapsed_ms:.1f}ms)"
            )
            return report

    def get_by_hash(self, item_hash: str, type: str) -> Optional[MemoryHit]:
        """按 hash 精确获取(检索辅助 / 调试用)"""
        rel_path = f"{type}/{item_hash}.md"
        try:
            data = self.memory_store.read(rel_path)
        except Exception:
            return None
        return MemoryHit(
            item_hash=item_hash,
            type=type,
            title=data.get("frontmatter", {}).get("title", ""),
            body=data.get("body", ""),
            rel_path=rel_path,
            score=1.0,
            breakdown={"exact": 1.0},
        )

    # ── 候选召回调 ─────────────────────────────────────

    def _retrieve_candidates(
        self,
        query: str,
        top_k: int,
        mode: RetrievalMode,
        types: Optional[list[str]],
        already_surfaced: Optional[set[str]] = None,
    ) -> list[MemoryHit]:
        """根据 mode 选择召回调,返回候选列表(M11 二选一)"""
        if mode == RetrievalMode.SEMANTIC:
            return self._semantic_search(query, top_k, types)
        if mode == RetrievalMode.SIDE_QUERY:
            return self._side_query_search(query, top_k, types, already_surfaced)
        raise RetrievalError(f"未知检索模式: {mode!r}")

    def _semantic_search(
        self, query: str, top_k: int, types: Optional[list[str]]
    ) -> list[MemoryHit]:
        """向量检索(依赖 vector_store.query)。

        Chroma 只返回 {id, distance};MemoryHit 的其他字段全部从 MemoryStore
        .md frontmatter 读(参见 MemoryStore.read 契约)。
        """
        if not hasattr(self.vector_store, "query"):
            logger.warning("vector_store 不支持 query(),返空")
            return []

        q_emb = self.embed_fn.encode(query)

        try:
            raw = self.vector_store.query(q_emb, top_k=top_k * 2)
        except Exception as e:
            logger.warning(f"vector_store.query 失败: {e},返空")
            return []

        hits: list[MemoryHit] = []
        for doc in raw:
            item_hash = doc.get("id", "")
            if not item_hash:
                continue
            # Chroma 不再存 type,需遍历常见 type 找到对应 .md
            type_ = self._resolve_type(item_hash, types)
            if type_ is None:
                continue
            if types and type_ not in types:
                continue
            rel_path = f"{type_}/{item_hash}.md"
            try:
                data = self.memory_store.read(rel_path)
            except Exception:
                # 文件已被删,跳过
                continue
            fm = data.get("frontmatter", {}) or {}
            body = data.get("body", "")
            title = fm.get("title", "")
            hit_type = fm.get("type", type_)
            hit_tags = fm.get("tags", []) or []
            hit_importance = fm.get("importance", 5)
            distance = doc.get("distance", 0.0)
            sim = max(0.0, 1.0 - distance / 2.0)
            hits.append(MemoryHit(
                item_hash=item_hash,
                type=hit_type,
                title=title,
                body=body,
                rel_path=rel_path,
                score=sim,
                breakdown={"semantic": sim},
                tags=hit_tags,
                importance=hit_importance,
            ))
        return hits

    def _resolve_type(self, item_hash: str, types: Optional[list[str]]) -> Optional[str]:
        """在标准 5 type 中扫描,找到 .md 存在的 type。

        Chroma 不再存 type 字段,retriever 需遍历常见 type 找到对应 .md。
        慢路径,但语义搜索本来就在 top_k 小集合上跑;只 stat 文件,不读内容。
        """
        candidates = types or ["user", "feedback", "event", "project", "reference"]
        for t in candidates:
            if (self.memory_store.root / t / f"{item_hash}.md").exists():
                return t
        return None

    def _side_query_search(
        self, query: str, top_k: int, types: Optional[list[str]],
        already_surfaced: Optional[set[str]],
    ) -> list[MemoryHit]:
        """side_query 模式:扫描 MEMORY.md manifest → LLM 选 path → 读全文

        T7 实现细节,这里先 stub 返空(避免循环 import)。
        """
        return []

    def _rerank(
        self, candidates: list[MemoryHit], mode: RetrievalMode
    ) -> list[MemoryHit]:
        """M11 简化:所有模式都用原 score 排序(无 hybrid 加权)"""
        return sorted(candidates, key=lambda h: h.score, reverse=True)

    def _filter_secrets(
        self, hits: list[MemoryHit]
    ) -> list[MemoryHit]:
        """
        L4 过滤: 扫 body 含密钥的 hit,标记 has_secret=True

        设计决策（M3 §3.4）：
        - 默认行为: 保留 hit, 仅在 hit.has_secret=True 报警
          (LLM 上层拿到结果后自己决定是否过滤/告警用户)
        - 理由: 误杀风险高,宁可"返 + 标",不可"滤 + 失"
        - caller 可通过 hits[i].has_secret 决定后续动作
        """
        out: list[MemoryHit] = []
        for h in hits:
            full_text = f"{h.title}\n{h.body}"
            sr = self.secret_scanner.scan(full_text)
            if not sr.is_clean:
                h.has_secret = True
                logger.warning(
                    f"retriever 发现含密钥 hit: {h.item_hash[:8]}... "
                    f"({len(sr.hits)} 处命中, pattern={sr.hits[0].pattern_name})"
                )
            out.append(h)
        return out


__all__ = [
    "MemoryRetriever",
    "MemoryHit",
    "RetrievalReport",
    "RetrievalMode",
    "RetrievalError",
]
