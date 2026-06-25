"""
记忆检索器（v2.1 §九.2）

M3 / Day 3 — 检索 + 安全

设计要点：
1. **三模式**（设计 §九.2）：
   - semantic: 向量相似度
   - keyword: 关键词/全文匹配（BM25 / LIKE）
   - hybrid: 加权融合（默认）
2. **L4**: 检索时即时跑 SecretScanner → 命中即过滤掉
3. **L8**: 检索结果含 secret 字段时报警
4. **降级链**：
   hybrid → semantic → keyword → 兜底全量
5. **可观测**：
   - 返回 score 解释（每个 hit 的各模式得分）
   - 检索耗时、过滤数

调用入口:
    retriever = MemoryRetriever(store, vec, embed_fn, config)
    hits = retriever.search("用户叫什么", top_k=5, mode="hybrid")
    # → [MemoryHit(type, title, body, score, breakdown), ...]
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
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
    """检索模式"""
    SEMANTIC = "semantic"
    KEYWORD = "keyword"
    HYBRID = "hybrid"


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
# 关键词打分（轻量 BM25 简化版）
# ──────────────────────────────────────────────────────────────────

def _tokenize(text: str) -> list[str]:
    """中英混合分词:
    - 英文: 单词
    - 中文: 单字 + 二元 bigram
    """
    text = text.lower().strip()
    tokens: list[str] = []

    # 1) 英文/数字/下划线
    for m in re.finditer(r"[a-z0-9_]+", text):
        tokens.append(m.group(0))

    # 2) 中文字符: 单字 + 二元
    zh_chars = re.findall(r"[一-鿿]", text)
    tokens.extend(zh_chars)
    for i in range(len(zh_chars) - 1):
        tokens.append(zh_chars[i] + zh_chars[i+1])

    return tokens


def _keyword_score(query_tokens: list[str], doc_text: str) -> float:
    """
    简化 BM25: 计算 query 在 doc 中的覆盖度

    score = matched_query_tokens / total_query_tokens (0-1)
    加权: 重复出现得更高分
    """
    if not query_tokens:
        return 0.0
    doc_tokens = _tokenize(doc_text)
    if not doc_tokens:
        return 0.0
    doc_set = set(doc_tokens)
    doc_count = {t: doc_tokens.count(t) for t in doc_set}

    matched = 0
    weight = 0.0
    for qt in query_tokens:
        if qt in doc_set:
            matched += 1
            # log(1 + tf) 加权
            weight += 1.0 + (doc_count[qt] - 1) * 0.1

    if matched == 0:
        return 0.0
    # 归一化: matched 比例 * 平均 tf weight
    coverage = matched / len(query_tokens)
    avg_weight = weight / matched
    return min(1.0, coverage * (0.5 + 0.5 * min(avg_weight, 1.0)))


# ──────────────────────────────────────────────────────────────────
# MemoryRetriever
# ──────────────────────────────────────────────────────────────────

class MemoryRetriever:
    """
    记忆检索器（v2.1 §九.2）

    三模式：
    - semantic:  cos similarity
    - keyword:   简化 BM25
    - hybrid:    weighted sum, 默认 0.7 * semantic + 0.3 * keyword

    用法:
        retriever = MemoryRetriever(store, vec, embed_fn, config)
        report = retriever.search("用户叫什么", top_k=5, mode="hybrid")
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
        mode: str | RetrievalMode = "hybrid",
        types: Optional[list[str]] = None,
        min_score: float = 0.0,
    ) -> RetrievalReport:
        """
        检索记忆

        Args:
            query: 查询文本
            top_k: 返回 top N
            mode: semantic | keyword | hybrid
            types: 限定类型(user/feedback/event/project/reference), None=全部
            min_score: 最低分阈值(过滤低相关)

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
            # 多召 → 融合 → 重排 → L4 过滤 → 截 top_k
            candidates = self._retrieve_candidates(query, top_k * 3, mode, types)
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
    ) -> list[MemoryHit]:
        """根据 mode 选择召回调,返回候选列表"""
        if mode == RetrievalMode.SEMANTIC:
            return self._semantic_search(query, top_k, types)
        if mode == RetrievalMode.KEYWORD:
            return self._keyword_search(query, top_k, types)
        # HYBRID: 两个模式都跑,合并去重
        sem = self._semantic_search(query, top_k, types)
        kw = self._keyword_search(query, top_k, types)
        merged = self._merge_hits(sem, kw)
        return merged

    def _semantic_search(
        self, query: str, top_k: int, types: Optional[list[str]]
    ) -> list[MemoryHit]:
        """向量检索(依赖 vector_store.query)。

        Chroma 只返回 {id, distance};MemoryHit 的其他字段全部从 MemoryStore
        .md frontmatter 读(参见 MemoryStore.read 契约)。
        """
        if not hasattr(self.vector_store, "query"):
            logger.warning("vector_store 不支持 query(),降级为 keyword 模式")
            return self._keyword_search(query, top_k, types)

        q_emb = self.embed_fn.encode(query)

        try:
            raw = self.vector_store.query(q_emb, top_k=top_k * 2)
        except Exception as e:
            logger.warning(f"vector_store.query 失败: {e},降级 keyword")
            return self._keyword_search(query, top_k, types)

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

    def _keyword_search(
        self, query: str, top_k: int, types: Optional[list[str]]
    ) -> list[MemoryHit]:
        """关键词检索（基于简化 BM25）"""
        # 1) 列出所有 memory(只拿 frontmatter 索引)
        all_items: list[tuple[str, str, dict]] = []  # (type, hash, listing)
        for t in (types or ["user", "feedback", "event", "project", "reference"]):
            try:
                items = self.memory_store.list_by_type(t)
            except Exception:
                continue
            for it in items:
                all_items.append((t, it.get("hash", ""), it))

        if not all_items:
            return []

        # 2) 分词 query
        q_tokens = _tokenize(query)

        # 3) 打分(对每个 item 读取完整 body)
        hits: list[MemoryHit] = []
        for type_, h, listing in all_items:
            # list_by_type 不带 body,需要 read
            rel_path = listing.get("path", f"{type_}/{h}.md")
            try:
                data = self.memory_store.read(rel_path)
            except Exception:
                continue
            body = data.get("body", "")
            title = listing.get("title", "") or data.get("frontmatter", {}).get("title", "")
            text = f"{title}\n{body}"
            score = _keyword_score(q_tokens, text)
            if score <= 0:
                continue
            hits.append(MemoryHit(
                item_hash=h,
                type=type_,
                title=title,
                body=body,
                rel_path=rel_path,
                score=score,
                breakdown={"keyword": score},
                tags=listing.get("tags", []),
                importance=data.get("frontmatter", {}).get("importance", 5),
            ))
        # 排序
        hits.sort(key=lambda x: x.score, reverse=True)
        return hits[:top_k]

    def _merge_hits(
        self, sem_hits: list[MemoryHit], kw_hits: list[MemoryHit]
    ) -> list[MemoryHit]:
        """
        合并 semantic + keyword 候选(按 hash 去重, 累加 breakdown)
        """
        by_hash: dict[str, MemoryHit] = {}
        for h in sem_hits:
            by_hash[h.item_hash] = h
        for h in kw_hits:
            if h.item_hash in by_hash:
                # 已有 semantic,补 keyword score
                existing = by_hash[h.item_hash]
                existing.breakdown["keyword"] = h.score
            else:
                by_hash[h.item_hash] = h
        return list(by_hash.values())

    def _rerank(
        self, candidates: list[MemoryHit], mode: RetrievalMode
    ) -> list[MemoryHit]:
        """
        重排: 按 mode 算最终 score

        - hybrid: 0.7 * semantic + 0.3 * keyword (config 可调)
        - semantic/keyword: 保持原 score
        """
        if mode != RetrievalMode.HYBRID:
            return sorted(candidates, key=lambda h: h.score, reverse=True)

        sem_w = self.config.retrieval.semantic_weight  # 0.7
        kw_w = self.config.retrieval.lexical_weight    # 0.3
        for h in candidates:
            sem_s = h.breakdown.get("semantic", 0.0)
            kw_s = h.breakdown.get("keyword", 0.0)
            h.score = sem_w * sem_s + kw_w * kw_s
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
