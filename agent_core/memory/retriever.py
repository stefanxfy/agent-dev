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
    SIDE_QUERY = "side_query"  # M11 新增


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
        llm_router=None,  # M11 新增(sideQuery 用)
    ):
        self.memory_store = memory_store
        self.vector_store = vector_store
        self.embed_fn = embed_fn
        self.config = config or MemoryConfig()
        self.secret_scanner = secret_scanner or get_default_scanner()
        self.llm_router = llm_router  # M11:sideQuery 模式注入主 LLM router

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

            # ── DEBUG:入口全参数 + config snapshot ──
            embed_model = getattr(self.embed_fn, "model_name", type(self.embed_fn).__name__)
            cfg_min = self.config.retrieval.min_score
            cfg_top_k = self.config.retrieval.top_k
            cfg_mode = self.config.retrieval.mode
            vec_count = self.vector_store.count() if hasattr(self.vector_store, "count") else -1
            logger.debug(
                f"[retriever.search] ENTER query={query!r} "
                f"top_k_arg={top_k} mode_arg={mode!r} types={types} "
                f"min_score_arg={min_score} already_surfaced={already_surfaced} | "
                f"config(retrieval): mode={cfg_mode!r} top_k={cfg_top_k} min_score={cfg_min} | "
                f"embed_fn={embed_model} vec.count={vec_count}"
            )

            if not query or not query.strip():
                empty = RetrievalReport(query=query, mode=RetrievalMode(mode), hits=[])
                span.set_attribute("memory.search.hits_count", 0)
                logger.debug(f"[retriever.search] EXIT (空 query) hits=0")
                return empty

            if isinstance(mode, str):
                try:
                    mode = RetrievalMode(mode)
                except ValueError:
                    raise RetrievalError(f"未知检索模式: {mode!r}")

            t0 = time.time()
            # 多召 → 重排 → L4 过滤 → 截 top_k
            logger.debug(f"[retriever.search] STAGE 1 retrieve_candidates mode={mode.value}")
            candidates = self._retrieve_candidates(query, top_k, mode, types, already_surfaced)
            logger.debug(
                f"[retriever.search] STAGE 1 done: "
                f"candidates={len(candidates)} → "
                f"{[h.rel_path for h in candidates[:5]]}"
            )

            logger.debug(f"[retriever.search] STAGE 2 rerank")
            ranked = self._rerank(candidates, mode)
            logger.debug(
                f"[retriever.search] STAGE 2 done: ranked={len(ranked)} → "
                f"top3 by score: {[(h.title[:20], round(h.score, 3)) for h in ranked[:3]]}"
            )

            logger.debug(f"[retriever.search] STAGE 3 secret_filter")
            filtered = self._filter_secrets(ranked)
            secret_n = len(ranked) - len(filtered)
            logger.debug(
                f"[retriever.search] STAGE 3 done: filtered={len(filtered)} "
                f"(secret_marked={secret_n})"
            )

            final = filtered[:top_k]
            logger.debug(
                f"[retriever.search] STAGE 4 slice top_k={top_k}: "
                f"final={len(final)} titles={[h.title[:20] for h in final]}"
            )

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
                f"[retriever.search] EXIT → {len(final)} hits "
                f"({report.secret_filtered} secret-filtered, {elapsed_ms:.1f}ms) "
                f"final_titles={[h.title for h in final]} "
                f"final_scores={[round(h.score, 3) for h in final]}"
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

        t0 = time.time()
        q_emb = self.embed_fn.encode(query)
        emb_norm = sum(x * x for x in q_emb) ** 0.5
        logger.debug(
            f"[_semantic_search] query={query!r} (len={len(query)} chars) | "
            f"embed_fn={getattr(self.embed_fn, 'model_name', type(self.embed_fn).__name__)} | "
            f"q_emb: dim={len(q_emb)} l2_norm={emb_norm:.4f} "
            f"first5={[round(x, 4) for x in q_emb[:5]]}"
        )

        try:
            raw = self.vector_store.query(q_emb, top_k=top_k * 2)
        except Exception as e:
            logger.warning(f"vector_store.query 失败: {e},返空")
            return []

        logger.debug(
            f"[_semantic_search] vector_store.query top_k={top_k * 2} → "
            f"raw={len(raw)} candidates (按 distance 升序):"
        )
        for i, doc in enumerate(raw):
            logger.debug(
                f"  raw[{i}] id={doc.get('id', '')[:12]}... "
                f"distance={doc.get('distance', 0.0):.4f}"
            )

        hits: list[MemoryHit] = []
        min_score_thr = self.config.retrieval.min_score
        for i, doc in enumerate(raw):
            item_hash = doc.get("id", "")
            if not item_hash:
                logger.debug(f"  raw[{i}] SKIP: id 为空")
                continue
            # Chroma 不再存 type,需遍历常见 type 找到对应 .md
            type_ = self._resolve_type(item_hash, types)
            if type_ is None:
                logger.debug(
                    f"  raw[{i}] SKIP: id={item_hash[:12]}... 5 个 type dir 都找不到 .md"
                )
                continue
            if types and type_ not in types:
                logger.debug(
                    f"  raw[{i}] SKIP: type={type_!r} 不在 types={types} 过滤范围"
                )
                continue
            rel_path = f"{type_}/{item_hash}.md"
            try:
                data = self.memory_store.read(rel_path)
            except Exception as e:
                logger.debug(
                    f"  raw[{i}] SKIP: id={item_hash[:12]}... read({rel_path}) 失败: {e}"
                )
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
            # M11 (2026-06-26 修复):min_score 从 MemoryConfig 读
            # 之前 search() 收了 min_score 但 _semantic_search 没用,dead param
            # (导致 "我是谁" 跟 "我喜欢斗破苍穹" 距离不算近也被召回)
            pass_min_score = sim >= min_score_thr
            if not pass_min_score:
                logger.debug(
                    f"  raw[{i}] SKIP: sim={sim:.4f} < min_score={min_score_thr:.4f} "
                    f"(title={title!r} rel_path={rel_path})"
                )
                continue
            logger.debug(
                f"  raw[{i}] KEEP: sim={sim:.4f} >= min_score={min_score_thr:.4f} "
                f"title={title!r} type={hit_type!r} importance={hit_importance} "
                f"tags={hit_tags} body_chars={len(body)}"
            )
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
        elapsed_ms = (time.time() - t0) * 1000
        logger.debug(
            f"[_semantic_search] DONE: kept {len(hits)}/{len(raw)} raw → "
            f"{elapsed_ms:.1f}ms"
        )
        return hits

    def _resolve_type(self, item_hash: str, types: Optional[list[str]]) -> Optional[str]:
        """在标准 5 type 中扫描,找到 .md 存在的 type。

        Chroma 不再存 type 字段,retriever 需遍历常见 type 找到对应 .md。
        慢路径,但语义搜索本来就在 top_k 小集合上跑;只 stat 文件,不读内容。
        """
        candidates = types or ["user", "feedback", "event", "project", "reference"]
        for t in candidates:
            p = self.memory_store.root / t / f"{item_hash}.md"
            exists = p.exists()
            logger.debug(
                f"[_resolve_type] id={item_hash[:12]}... stat({p.relative_to(self.memory_store.root)}) "
                f"→ {exists}"
            )
            if exists:
                return t
        return None

    def _side_query_search(
        self, query: str, top_k: int, types: Optional[list[str]],
        already_surfaced: Optional[set[str]],
    ) -> list[MemoryHit]:
        """side_query 模式:扫描 MEMORY.md manifest → LLM 选 path → 读全文

        M11 设计: 不走向量召回, 直接读 <memory_root>/MEMORY.md manifest,
        让 LLM 从 manifest 里挑 ≤ max_select 个最相关的 path, 然后读全文。
        """
        from agent_core.memory.memory_index import (
            scan_memory_files, format_memory_manifest,
        )
        from agent_core.memory.prompt_templates import (
            SIDE_QUERY_SYSTEM_PROMPT, build_side_query_prompt,
        )

        max_files = getattr(
            self.config.retrieval, "side_query_max_files", 200
        )
        max_select = getattr(
            self.config.retrieval, "side_query_max_select", top_k
        )

        logger.debug(
            f"[_side_query_search] ENTER query={query!r} top_k={top_k} "
            f"max_files={max_files} max_select={max_select} "
            f"types={types} already_surfaced_count={len(already_surfaced or set())}"
        )

        t0 = time.time()
        entries = scan_memory_files(
            self.memory_store.root, max_files=max_files,
            types_filter=types,
        )
        scan_ms = (time.time() - t0) * 1000
        logger.debug(
            f"[_side_query_search] scan_memory_files → {len(entries)} entries "
            f"({scan_ms:.1f}ms)"
        )
        # 列前 10 条给个概览
        for i, e in enumerate(entries[:10]):
            # MemoryFileEntry 字段叫 name(对应 frontmatter.name 或 .title)不是 title
            # 之前用 getattr(e, 'title', '?') 永远返 '?',side_query 模式 debug log 看不到
            # 真实标题,排查时不便(2026-06-26 用户反馈)
            logger.debug(
                f"  entry[{i}] rel_path={e.rel_path} "
                f"name={getattr(e, 'name', '?')!r} "
                f"type={getattr(e, 'type', '?')!r}"
            )
        if len(entries) > 10:
            logger.debug(f"  ... and {len(entries) - 10} more entries")

        if already_surfaced:
            before = len(entries)
            entries = [e for e in entries if e.rel_path not in already_surfaced]
            logger.debug(
                f"[_side_query_search] already_surfaced filter: "
                f"{before} → {len(entries)} (-{before - len(entries)})"
            )
        if not entries:
            logger.debug(f"[_side_query_search] EXIT: 0 entries after filter,返空")
            return []

        manifest = format_memory_manifest(entries)
        logger.debug(
            f"[_side_query_search] manifest built: {len(manifest)} chars "
            f"(max ~{max_files * 80} expected)"
        )
        # 列 manifest 头部 800 字,方便看 LLM 实际看到的内容
        logger.debug(
            f"[_side_query_search] manifest preview (前 800 chars):\n"
            f"{manifest[:800]}{'...[truncated]' if len(manifest) > 800 else ''}"
        )

        selected = self._call_side_query(query, manifest, max_select)
        logger.debug(
            f"[_side_query_search] LLM 选了 {len(selected)} 个 path: {selected}"
        )

        hits: list[MemoryHit] = []
        for path in selected:
            try:
                data = self.memory_store.read(path)
            except Exception as e:
                logger.debug(
                    f"[_side_query_search] SKIP path={path!r} read 失败: {e}"
                )
                continue
            fm = data.get("frontmatter", {}) or {}
            body = data.get("body", "")
            title = fm.get("name") or fm.get("title", "")
            hit = MemoryHit(
                item_hash=fm.get("item_hash", ""),
                type=fm.get("type", "user"),
                title=title,
                body=body,
                rel_path=path,
                score=1.0,
                breakdown={"side_query": 1.0},
                tags=fm.get("tags", []) or [],
                importance=fm.get("importance", 5),
            )
            logger.debug(
                f"[_side_query_search]   KEEP path={path} title={title!r} "
                f"body_chars={len(body)} tags={hit.tags} importance={hit.importance}"
            )
            hits.append(hit)
        logger.debug(
            f"[_side_query_search] DONE: {len(hits)}/{len(selected)} path 读全文成功"
        )
        return hits

    def _call_side_query(
        self, query: str, manifest: str, max_select: int
    ) -> list[str]:
        """调 LLM router 选 path(LLM 失败时降级返空)"""
        import json
        from agent_core.memory.prompt_templates import (
            SIDE_QUERY_SYSTEM_PROMPT, build_side_query_prompt,
        )
        if not self.llm_router:
            logger.warning(
                "sideQuery 需要 llm_router,当前为 None,降级返空"
            )
            return []
        prompt = build_side_query_prompt(query, manifest, max_select)
        logger.debug(
            f"[_call_side_query] query={query!r} max_select={max_select} | "
            f"manifest_chars={len(manifest)} prompt_chars={len(prompt)}"
        )
        logger.debug(
            f"[_call_side_query] SIDE_QUERY_SYSTEM_PROMPT (full):\n"
            f"{SIDE_QUERY_SYSTEM_PROMPT}"
        )
        logger.debug(
            f"[_call_side_query] user prompt (full {len(prompt)} chars):\n{prompt}"
        )
        text = ""
        t0 = time.time()
        try:
            # llm_router.chat 是 stream 协议
            for chunk in self.llm_router.chat(
                messages=[
                    {"role": "system", "content": SIDE_QUERY_SYSTEM_PROMPT},
                    {"role": "user", "content": prompt},
                ],
                cache_namespace="memory_side_query",
            ):
                if getattr(chunk, "text_delta", None):
                    text += chunk.text_delta.text
            llm_ms = (time.time() - t0) * 1000
            logger.debug(
                f"[_call_side_query] LLM 流式返回完成 ({llm_ms:.1f}ms) "
                f"raw_text_chars={len(text)}"
            )
            logger.debug(
                f"[_call_side_query] LLM raw response (full):\n{text}"
            )
            data = json.loads(_strip_code_fence(text))
            logger.debug(
                f"[_call_side_query] parsed JSON: {data}"
            )
            selected = data.get("selected_paths", [])[:max_select]
            logger.debug(
                f"[_call_side_query] selected_paths (sliced to max_select={max_select}): "
                f"{selected}"
            )
            return selected
        except Exception as e:
            llm_ms = (time.time() - t0) * 1000
            logger.warning(
                f"sideQuery 失败 ({llm_ms:.1f}ms),降级返空: {e}\n"
                f"  raw_text={text!r}"
            )
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


def _strip_code_fence(text: str) -> str:
    """剥掉 LLM 输出外的 ``` ``` markdown fence"""
    text = text.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines)
    return text.strip()


__all__ = [
    "MemoryRetriever",
    "MemoryHit",
    "RetrievalReport",
    "RetrievalMode",
    "RetrievalError",
]
