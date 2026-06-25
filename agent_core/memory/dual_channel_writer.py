"""
双通道写入器(Phase 5 重命名)

M2 / Day 2 — A3+A5+A9+A10 五项审查修复一次到位(Phase 4 删 A4 daily/extract 锁)

设计要点:
1. 双通道
   - persist_turn(旧 channel_a):内联写 memory_tasks 表(state=NONE,无 LLM,同步,秒级返回)
   - extract_candidates(旧 channel_b):后台提取(LLM,async,CAS 抢占 INFLIGHT + mark_done_with_candidates)
2. A5: 幂等去重(item_hash via MemoryStore)
   - 同 (session_id, item_hash) UNIQUE 约束
   - 同 hash 已存在 → skip,不写盘
3. A9: executor shutdown
   - atexit 注册 graceful shutdown
   - 主进程退出前等所有 in-flight 任务完成(最多 30s)
   - 支持 explicit shutdown(timeout=)
4. memory_tasks 单表 WAL 状态机(M11 / Phase 4 single source of truth):
   - NONE → PENDING → INFLIGHT → DONE/FAILED
   - 崩溃恢复靠 startup_scan 4 步(INFLIGHT 熔断 + FAILED 重排 + 派工)
   - 取代旧 A3 cursors 表 + A10 pending_writes 表 + 旧 A4 daily/extract 锁

不变量(v2.1 §4.5):
- #3 persist_turn 只写 memory_tasks(state=NONE),不调 LLM
- #4 extract_candidates 推进前必须 cas_grab_task(INFLIGHT) → 成功后 mark_done_with_candidates

Phase 5 rename refactor:
- 旧 channel_a_inline_write → persist_turn
- 旧 channel_b_background_extract → extract_candidates
- 旧 _do_channel_a_write → _do_persist_turn
- 旧 _do_channel_b_extract → _do_extract_candidates
- 日志前缀 "channel A:" / "extract: " → "persist:" / "extract:"
- DualChannelWriter 类名保留(架构概念),方法名换为功能名

已知限制:
- vector_store 必须是 ChromaVectorStore 实现(见 agent_core.memory.chroma_store)
- _do_extract_candidates 是 LLM 提取的实际逻辑(M3 接 LLM router)
"""

from __future__ import annotations

import atexit
import json
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, Future
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional, Protocol, TYPE_CHECKING, Union

from agent_core.exceptions import AgentError
from agent_core.memory.dedup import DedupAction, decide_action, top_similarity
from agent_core.memory.embeddings import EmbedFn
from agent_core.memory.memory_store import (
    MemoryStore,
    MemoryStoreError,
    compute_item_hash,
)
from agent_core.memory.meta_db import MetaDB
from agent_core.memory.path_validator import MemoryPathValidator, PathSecurityError
from agent_core.memory.secret_scanner import SecretScanner
from agent_core.memory.wal_config import TaskWALConfig

if TYPE_CHECKING:
    from agent_core.memory.react_memory_bridge import MemoryEvent

logger = logging.getLogger("memory.dual_channel")


# ──────────────────────────────────────────────────────────────────
# M10 C1.2: 辅助函数(lazy import 避免循环依赖)
# ──────────────────────────────────────────────────────────────────

def _make_secret_event(turn_index: int) -> "MemoryEvent":
    """构造 SECRET_DETECTED MemoryEvent(避免循环 import)"""
    from agent_core.memory.react_memory_bridge import MemoryEvent, MemoryEventKind
    return MemoryEvent(
        kind=MemoryEventKind.SECRET_DETECTED,
        turn_index=turn_index,
        reason="extract_sanitize_unrecoverable",
    )


# ──────────────────────────────────────────────────────────────────
# 异常
# ──────────────────────────────────────────────────────────────────

class DualChannelError(AgentError):
    """双通道写入器异常"""
    code = "DUAL_CHANNEL"


class ExtractionInProgressError(DualChannelError):
    """提取正在进行（防止并发）"""
    code = "EXTRACTION_IN_PROGRESS"


# ──────────────────────────────────────────────────────────────────
# 数据契约
# ──────────────────────────────────────────────────────────────────

@dataclass
class TurnMessage:
    """一条 turn（用户输入 + 助手响应）"""
    turn_index: int
    user_msg: str
    assistant_resp: str
    timestamp: float = field(default_factory=time.time)


@dataclass
class ExtractionCandidate:
    """LLM 提取的记忆候选（A5 + L6）"""
    type: str
    title: str
    body: str
    source_quote: str
    tags: list[str] = field(default_factory=list)
    score: float = 0.5


class VectorStoreProtocol(Protocol):
    """vector_store 接口(必须由 ChromaVectorStore 实现,见 chroma_store.py)

    字段约定 add(doc: dict):
        - id:        str 唯一标识(item_hash)
        - embedding: list[float] 向量(维度由 embed_fn 决定)
        - metadata:  dict (type/title/tags/...)
        - document:  str 原始文本(可选)
    """
    def add(self, doc: dict[str, Any]) -> None: ...
    def count(self) -> int: ...


# ──────────────────────────────────────────────────────────────────
# DualChannelWriter
# ──────────────────────────────────────────────────────────────────

class DualChannelWriter:
    """
    双通道写入器(v2.1 §4.1)

    用法:
        db = MetaDB(":memory:")
        store = MemoryStore(Path("/tmp/memory"))
        vec, embed = make_chroma_store("/tmp/chroma")  # ChromaVectorStore + bge-m3
        w = DualChannelWriter("s1", db, store, vec, embed)

        # persist_turn: 内联写 turn 到 memory_tasks
        w.persist_turn("记住我叫小明", "已记", turn_index=0)

        # extract_candidates: 后台提取(异步)
        w.extract_candidates([TurnMessage(0, "我叫小明", "已记")])
        w.shutdown(timeout=30)

        # 重启恢复(A3):startup_scan 自动恢复
        w2 = DualChannelWriter("s1", db, store, vec, embed)
        result = w2.startup_scan()  # 清理旧 DONE/FAILED + 熔断 INFLIGHT + 派工
    """

    def __init__(
        self,
        session_id: str,
        meta_db: MetaDB,
        memory_store: MemoryStore,
        vector_store: VectorStoreProtocol,
        embed_fn: EmbedFn,
        *,
        extraction_timeout_seconds: int = 60,
        executor_workers: int = 2,
        event_callback: Optional[Callable[["MemoryEvent"], None]] = None,  # M10 C1.2
        dedup_config: Optional[Any] = None,   # DedupConfig;None → 不做语义去重(向后兼容)
        dedup_judge: Optional[Callable[[Any, list[dict]], bool]] = None,  # 可疑带 LLM 判定器
        task_wal_config: TaskWALConfig = TaskWALConfig(),  # M11: WAL 状态机配置
    ):
        if task_wal_config is None:
            raise ValueError("task_wal_config 不能为 None,使用 TaskWALConfig() 取默认")
        self.session_id = session_id
        self.meta_db = meta_db
        self.memory_store = memory_store
        self.vector_store = vector_store
        # 语义去重(向量召回 + LLM 判定)。dedup_config=None 时整体关闭,行为同旧版。
        self.dedup_config = dedup_config
        self.dedup_judge = dedup_judge
        # M11: WAL 状态机配置(Phase 1/2 用,Phase 3 env 接入)
        self.task_wal_config = task_wal_config
        # embed_fn 必须非 None:extract_candidates 写入 vector_store 需要先编码
        # 失败时立即 EmbeddingError,不静默(不再有 Mock 兜底)
        if embed_fn is None:
            raise DualChannelError(
                "DualChannelWriter 必须传入 embed_fn(用于向量化 memory)。"
                "用 make_embed_fn() 构造 bge-m3 实例。"
            )
        self.embed_fn = embed_fn

        # A3/A4 旧 cursor / daily/extract 锁已删 (Phase 4)
        # memory_tasks 单表是 single source of truth,task_state 自带 idempotency

        # A9: 后台 executor
        self._executor = ThreadPoolExecutor(
            max_workers=executor_workers,
            thread_name_prefix=f"chB-{session_id[:8]}",
        )
        self._shutdown = False
        self._inflight: set[Future] = set()
        self.extraction_timeout_seconds = extraction_timeout_seconds

        # A10: extraction_in_progress 标志(防止 extract_candidates 并发)
        self._extraction_in_progress = False
        self._extraction_in_progress_lock = threading.Lock()
        # M6 场景 8: 跟踪本次提取开始时间,实现 watchdog(防止 extractor hang 永久阻塞)
        self._extraction_started_at: Optional[float] = None

        # M10 C1.2: secret sanitize 配置
        self.scanner = SecretScanner()
        self.event_callback = event_callback

        # M10 C1.4: §14.1 防御纵深 — extract_candidates 入口 path validator
        # 即使 MemoryStore.write 已调 validator(C1.1 前置),
        # 这里再加一道,捕获任何 future regression。
        self._path_validator = MemoryPathValidator(memory_store.root)

        # atexit 优雅退出（A9）
        atexit.register(self._graceful_shutdown)

    # ──────────────────────────────────────────────────────
    # 通道 A：内联写 daily log（无 LLM，同步）
    # ──────────────────────────────────────────────────────

    def persist_turn(
        self,
        user_msg: str,
        assistant_resp: str,
        turn_index: Optional[int] = None,
    ) -> int:
        """
        持久化一条 turn 到 memory_tasks(state=NONE)

        Phase 5 rename:旧 channel_a_inline_write
        M11 / Phase 4 single source of truth:不再写 JSONL / pending / cursors 三 IO,
        直接写新表。
        state='NONE' 表示 persist_turn 刚落盘,extract_candidates 取走时再 CAS → INFLIGHT → DONE/FAILED。

        Args:
            user_msg: 用户消息
            assistant_resp: 助手响应
            turn_index: turn 在 session 内的全局索引。
                传 None(默认)= 用 max(已写 turn_index) + 1;
                显式传 int= 用 caller 提供的值(向后兼容,<= 已写 max 时幂等跳过)。

        Returns:
            写入后的 turn_index

        不变量 #3: 不调 LLM,必须同步 + 快速(< 10ms)
        """
        if self._shutdown:
            raise DualChannelError("writer 已 shutdown,禁止写入")

        # turn_index 缺省时:从 max(已写 turn_index) 派生
        if turn_index is None:
            turn_index = self._next_turn_index()

        # 幂等:turn_index <= max(已写 turn_index) → 跳过
        # (覆盖同 turn 重复 + 显式传小于已 max 的 turn_index 两种情况)
        # 空表时 max_idx=None,不应跳过
        max_idx = self._max_turn_index()
        if max_idx is not None and turn_index <= max_idx:
            logger.debug(
                f"persist: turn {turn_index} <= max({max_idx}),跳过(幂等)"
            )
            return max_idx

        # 显式 turn 已被写过(turn_index == 某已存在 task):也跳过
        existing = self.meta_db.get_task_by_turn(self.session_id, turn_index)
        if existing is not None:
            logger.debug(
                f"persist: turn {turn_index} 已写过(task#{existing['task_id']}),跳过"
            )
            return existing["turn_index"]

        # 直接走 memory_tasks 表,state='NONE' 标记 persist_turn 刚落盘
        self.meta_db.insert_task(
            session_id=self.session_id,
            turn_index=turn_index,
            user_msg=user_msg,
            assistant_resp=assistant_resp,
            state="NONE",
            max_attempts=self.task_wal_config.max_retry,
        )

        logger.debug(f"persist: turn {turn_index} 写入 memory_tasks (state=NONE)")
        return turn_index

    def _next_turn_index(self) -> int:
        """返回 max(memory_tasks.turn_index) + 1(同 session);空表 → 1"""
        max_idx = self._max_turn_index()
        return (max_idx + 1) if max_idx is not None else 1

    def _max_turn_index(self) -> Optional[int]:
        """返回 max(memory_tasks.turn_index)(同 session);空表 → None

        返回 None 而非 0 是为了与「显式 turn_index=0」区分:
        空表时,turn_index=0 应作为首条写入(不该被幂等跳过)。
        """
        with self.meta_db.transaction() as conn:
            row = conn.execute(
                "SELECT MAX(turn_index) FROM memory_tasks WHERE session_id = ?",
                (self.session_id,),
            ).fetchone()
        if row is None or row[0] is None:
            return None
        return int(row[0])

    @staticmethod
    def _calc_next_at(attempts: int, retry_backoff_seconds: int) -> float:
        """退避公式:next_at = now + retry_backoff_seconds × 2^(attempts - 1)。

        attempts=1 → 基准间隔(默认 60s)
        attempts=2 → 2 倍
        attempts=3 → 4 倍
        attempts=0 → 0.5 × 基准(立即可重试,边界行为可接受)

        Phase 2 / Step 2.2.3:extract_candidates 失败重试时使用,startup_scan 步骤 3
        也会读 next_at 决定是否到时间重排 PENDING。
        """
        return time.time() + retry_backoff_seconds * (2 ** (attempts - 1))

    # ──────────────────────────────────────────────────────
    # 通道 B：后台 LLM 提取（异步 + cursor 持久化）
    # ──────────────────────────────────────────────────────

    def extract_candidates(
        self,
        messages: list[TurnMessage],
        *,
        llm_extractor: Optional[Callable[[list[TurnMessage]], list[ExtractionCandidate]]] = None,
    ) -> Future:
        """
        后台异步提取 candidates(LLM)

        Phase 5 rename:旧 channel_b_background_extract

        Args:
            messages: 要处理的 turn 列表(由 caller 决定范围;bridge 通常传单个 turn_msg)
            llm_extractor: LLM 提取函数(M3 接 router;不传则用空列表占位)

        Returns:
            Future(调用方 shutdown(timeout=) 等完成)
        """
        if self._shutdown:
            raise DualChannelError("writer 已 shutdown,禁止提交")

        # M6 场景 8: watchdog 检测 extractor hang 导致的卡死
        # 如果上次提取已 _extraction_in_progress=True 但超过 timeout,强制重置
        with self._extraction_in_progress_lock:
            if self._extraction_in_progress and self._extraction_started_at is not None:
                age = time.time() - self._extraction_started_at
                if age > self.extraction_timeout_seconds:
                    logger.warning(
                        f"extraction_in_progress 卡死 {age:.1f}s "
                        f"(>{self.extraction_timeout_seconds}s), 强制重置"
                    )
                    self._extraction_in_progress = False
                    self._extraction_started_at = None

        # A10: extraction_in_progress 标志
        with self._extraction_in_progress_lock:
            if self._extraction_in_progress:
                raise ExtractionInProgressError(
                    f"session {self.session_id} 提取已在进行,避免并发"
                )
            self._extraction_in_progress = True
            self._extraction_started_at = time.time()

        future = self._executor.submit(
            self._do_extract_candidates,
            messages,
            llm_extractor or (lambda _msgs: []),  # 默认空列表
        )
        self._inflight.add(future)
        future.add_done_callback(self._on_extract_done)
        return future

    def _is_semantic_duplicate(
        self, cand: ExtractionCandidate, embedding: list[float], cand_text: str
    ) -> bool:
        """语义去重:用候选 embedding 在库里召回最相似记忆,判断是否重复。

        三档(见 dedup.decide_action):
          - 相似度 >= auto_threshold      → 直接判重复(不调 LLM,省 token)
          - judge_floor <= 相似度 < auto  → 调 dedup_judge(LLM)判重复/新增
          - 否则                          → 不是重复,正常写盘
        去重失败(向量库异常等)绝不阻断持久化:返回 False(宁可多存,不可丢)。
        """
        cfg = self.dedup_config
        if cfg is None or not getattr(cfg, "enabled", False):
            logger.debug(f"语义去重: 功能未启用, 放行 [{cand.type}] {cand.title!r}")
            return False
        query_fn = getattr(self.vector_store, "query", None)
        if query_fn is None:
            logger.debug(f"语义去重: 向量库不支持 query, 放行 [{cand.type}] {cand.title!r}")
            return False

        logger.debug(
            f"语义去重检查开始: [{cand.type}] {cand.title!r} "
            f"(body={cand.body[:50]!r}...)"
        )

        try:
            hits = query_fn(embedding, getattr(cfg, "top_k", 5))
            sim = top_similarity(hits)
            action = decide_action(sim, cfg)
            _sim_str = f"{sim:.4f}" if sim is not None else "none"

            if action is DedupAction.AUTO_DUPLICATE:
                _top_title = (hits[0].get("metadata") or {}).get("title", "?")
                logger.info(
                    f"extract:  语义重复(auto, sim={_sim_str} >= "
                    f"{cfg.auto_threshold}) 跳过 [{cand.type}] {cand.title!r} "
                    f"≈ 已有 {_top_title!r}"
                )
                return True

            if action is DedupAction.NEEDS_JUDGE:
                if self.dedup_judge is None:
                    logger.debug(
                        f"extract:  可疑相似(sim={_sim_str})但无 dedup_judge,放行写盘"
                    )
                    return False
                # 预解析 hits:caller 负责把 [{id, distance}] 转为 [{id, title, body, distance}]
                # 喂给 dedup_judge / build_dedup_prompt(Chroma 不再存 metadata/document)
                resolved_hits = self._resolve_hits_for_prompt(hits)
                is_dup = bool(self.dedup_judge(cand, resolved_hits))
                logger.info(
                    f"extract:  LLM 去重判定(sim={_sim_str}) → "
                    f"{'重复跳过' if is_dup else '新增写盘'} [{cand.type}] {cand.title!r}"
                )
                return is_dup

            return False  # NEW
        except Exception as e:
            logger.warning(
                f"extract:  语义去重出错(放行写盘,不阻断): {type(e).__name__}: {e}"
            )
            return False

    def _resolve_hits_for_prompt(self, hits: list[dict]) -> list[dict]:
        """把 vec.query() 返回的 [{id, distance}] 预解析为
        [{id, title, body, distance}],title/body 从 MemoryStore 读。

        失败(文件已被删)用空字符串占位,避免去重流程中断。
        """
        resolved: list[dict] = []
        for h in hits:
            item_hash = h.get("id", "")
            distance = h.get("distance", 0.0)
            if not item_hash:
                resolved.append({"id": "", "title": "?", "body": "", "distance": distance})
                continue
            type_ = None
            for t in ("user", "feedback", "event", "project", "reference"):
                rel = self.memory_store.root / t / f"{item_hash}.md"
                if rel.exists():
                    type_ = t
                    break
            title, body = "", ""
            if type_:
                try:
                    data = self.memory_store.read(f"{type_}/{item_hash}.md")
                    fm = data.get("frontmatter", {}) or {}
                    title = fm.get("title", "")
                    body = data.get("body", "")
                except Exception:
                    pass
            resolved.append({
                "id": item_hash,
                "title": title,
                "body": body,
                "distance": distance,
            })
        return resolved

    def _do_extract_candidates(
        self,
        messages: list[TurnMessage],
        extractor: Callable[[list[TurnMessage]], list[ExtractionCandidate]],
    ) -> dict[str, Any]:
        """
        实际执行提取(在 executor 线程)

        Phase 5 rename:旧 _do_channel_b_extract
        Phase 4:不写 pending_writes、不推进 extract_cursor、不取 _ipc_extract 锁
        — memory_tasks 单表 CAS + startup_scan 兜底是 single source of truth。

        流程:
        1. CAS 抢占每个 turn 的 task → INFLIGHT(per-turn CAS,允许部分失败)
        2. extractor(messages) → 0-N candidates
        3. 逐条写 MemoryStore + vector_store(stamp_turn = max(messages).turn_index)
        4. 成功 → mark_done_with_candidates(stamp_task)
        5. 失败 → mark_failed(所有 wal_inflight_ids,退避公式)
        """
        to_process = messages
        if not to_process:
            _incoming = [m.turn_index for m in messages]
            logger.warning(
                f"extract:  messages 为空,提取被跳过 — 传入 turn_index={_incoming}"
            )
            return {"extracted": 0, "written": 0, "skipped": 0}

        logger.info(
            f"extract 提取开始: session={self.session_id}, "
            f"turns={len(to_process)} (turn_index={[m.turn_index for m in to_process]})"
        )

        # M11:per-turn CAS — 抢占每个 turn 的 task → INFLIGHT
        # (不抢到也不阻断 — 可能别的 writer 已在 INFLIGHT,
        # 失败由 startup_scan 的 melt_stuck_inflight 兜底)
        wal_inflight_ids: list[int] = []
        for m in to_process:
            task_row = self.meta_db.get_task_by_turn(self.session_id, m.turn_index)
            if task_row is None:
                # 没注册过的 turn(理论上 persist_turn 写后必有,容错 skip)
                logger.debug(
                    f"extract:  turn={m.turn_index} 不在 memory_tasks(可能未走 persist_turn),跳过 CAS"
                )
                continue
            # 偏差 1 修复:from_states 含 FAILED,设计文档 § 5.1 阶段 1 伪代码
            # 允许 runtime 期间失败的 turn 自动恢复,不必等 startup_scan 重排
            if self.meta_db.cas_grab_task(
                task_row["task_id"], ["PENDING", "NONE", "FAILED"], "INFLIGHT"
            ):
                wal_inflight_ids.append(task_row["task_id"])
            else:
                logger.debug(
                    f"extract:  turn={m.turn_index} task#{task_row['task_id']} "
                    f"state={task_row['state']} 不在 (PENDING/NONE),跳过 CAS"
                )

        written = 0
        skipped = 0
        try:
            # 1. LLM 提取(M3 接 router)
            candidates = extractor(to_process)
            logger.debug(
                f"extract:  extractor 返回 {len(candidates)} candidates"
            )

            # 2026-06-24 Bug 1c 修复:candidates 与 messages 不是 1:1。
            # bridge 的 extractor 忽略入参 messages,直接返回 gate 对"本轮整段最近
            # 对话"评出的 0-N 条 candidates。
            # 写入全部 candidates,统一盖上本次实际处理的最新 turn_index。
            stamp_turn = max(m.turn_index for m in to_process)

            # 2. 逐条写 MemoryStore(A5 幂等)
            for cand in candidates:
                try:
                    _body_preview = cand.body[:80].replace("\n", " ")
                    _quote_preview = (cand.source_quote or "")[:60].replace("\n", " ")
                    logger.debug(
                        f"extract:  候选记忆 turn={stamp_turn} type={cand.type!r} "
                        f"title={cand.title!r} tags={cand.tags} score={cand.score} "
                        f"| body[{len(cand.body)}]={_body_preview!r} "
                        f"| source_quote={_quote_preview!r}"
                    )

                    # M10 C1.4: §14.1 防御纵深 — extract_candidates 入口 path validator
                    rel_path = f"{cand.type}/__extract_preflight__.md"
                    try:
                        self._path_validator.validate(rel_path)
                    except PathSecurityError as e:
                        logger.warning(
                            f"extract:  拒绝 candidate(cand.title={cand.title!r}): "
                            f"type={cand.type!r} 触发 path validator: {e}"
                        )
                        skipped += 1
                        continue

                    # M10 C1.2: §14.4 secret sanitize(写盘前)
                    redacted_body = self.scanner.redact(cand.body)
                    if redacted_body != cand.body:
                        remaining = self.scanner.scan(redacted_body)
                        if not remaining.is_clean:
                            logger.warning(
                                f"extract:  丢弃 candidate(cand.title={cand.title!r}): "
                                f"redact 后仍命中 {len(remaining.hits)} 个 secret"
                            )
                            if self.event_callback:
                                try:
                                    self.event_callback(_make_secret_event(stamp_turn))
                                except Exception as cb_err:
                                    logger.error(f"event_callback 抛错: {cb_err}")
                            skipped += 1
                            continue
                        logger.debug(
                            f"extract:  candidate {cand.title!r} 命中 secret,"
                            f"已 redact body 后继续写盘"
                        )
                        cand = replace(cand, body=redacted_body)

                    # ── 先向量化 ──
                    try:
                        text_for_emb = f"{cand.title}\n{cand.body}"
                        _emb_model = getattr(
                            self.embed_fn, "model_name", type(self.embed_fn).__name__
                        )
                        logger.debug(
                            f"extract:  向量化中 model={_emb_model} "
                            f"text[{len(text_for_emb)}] chars (title={cand.title!r})"
                        )
                        embedding = self.embed_fn.encode(text_for_emb)
                    except Exception as enc_err:
                        raise DualChannelError(
                            f"向量化失败(candidate={cand.title}): {enc_err}",
                            cause=enc_err,
                        )

                    # ── 语义去重 ──
                    if self._is_semantic_duplicate(cand, embedding, text_for_emb):
                        skipped += 1
                        continue

                    item_hash = self.memory_store.write(
                        type=cand.type,
                        title=cand.title,
                        body=cand.body,
                        source_quote=cand.source_quote,
                        tags=cand.tags,
                        extra={
                            "session_id": self.session_id,
                            "turn_index": stamp_turn,
                        },
                    )
                    md_path = self.memory_store.root / cand.type / f"{item_hash}.md"
                    logger.debug(
                        f"extract:  MemoryStore 已写 {md_path} "
                        f"(hash={item_hash[:12]}, body[{len(cand.body)}] chars)"
                    )

                    self.vector_store.add(item_hash, embedding)
                    written += 1
                    logger.info(
                        f"extract:  已持久化 [{cand.type}] {cand.title!r} "
                        f"(hash={item_hash[:8]}, turn={stamp_turn})"
                    )
                except MemoryStoreError as e:
                    if "已存在" in str(e):
                        skipped += 1
                        logger.debug(
                            f"extract:  [{cand.type}] {cand.title!r} 已存在, 跳过(幂等)"
                        )
                        continue
                    raise

            # 3. 写新表状态机 — 抢占过的 turn → DONE
            if wal_inflight_ids:
                try:
                    candidates_json = json.dumps(
                        [c.model_dump() if hasattr(c, "model_dump") else c.__dict__
                         for c in candidates],
                        ensure_ascii=False,
                    )
                except Exception as dump_err:
                    logger.warning(f"extract:  candidates JSON 序列化失败: {dump_err}")
                    candidates_json = "[]"
                # 把 candidates 写到 stamp_turn 对应的 task(INFLIGHT 状态)
                stamp_task = self.meta_db.get_task_by_turn(self.session_id, stamp_turn)
                if stamp_task and stamp_task["state"] == "INFLIGHT" \
                        and stamp_task["task_id"] in wal_inflight_ids:
                    self.meta_db.mark_done_with_candidates(
                        stamp_task["task_id"], candidates_json
                    )
                # 其余抢到 INFLIGHT 的 turn 没 candidates → 也标 DONE(空 candidates)
                for tid in wal_inflight_ids:
                    if stamp_task and tid == stamp_task["task_id"]:
                        continue
                    task_now = self.meta_db.get_task(tid)
                    if task_now and task_now["state"] == "INFLIGHT":
                        self.meta_db.mark_done_with_candidates(tid, "[]")

            logger.info(
                f"extract 提取完成: extracted={len(candidates)}, "
                f"written={written}, skipped={skipped}"
            )
            return {"extracted": len(candidates), "written": written, "skipped": skipped}

        except Exception as e:
            # 失败 → 所有 wal_inflight_ids 标 FAILED(退避公式 → startup_scan 重排)
            # 偏差 3 修复:与设计文档 § 5.1 阶段 5 伪代码对齐 — 终态判别
            # attempts >= max_attempts → next_at=None(终态,等人工);否则 → 退避公式
            logger.error(
                f"extract 提取失败 turn_index={[m.turn_index for m in to_process]}: {e}"
            )
            max_attempts = self.task_wal_config.max_retry
            for tid in wal_inflight_ids:
                task_now = self.meta_db.get_task(tid)
                if task_now and task_now["state"] == "INFLIGHT":
                    # 偏差 2 修复:CAS 已 +1,这里读 task_now["attempts"] 即可
                    new_attempts = task_now["attempts"] or 0
                    if new_attempts >= max_attempts:
                        # 终态:重试用尽,next_at=None 让 cleanup_failed_tasks 接管
                        next_at_ts: Optional[float] = None
                        logger.error(
                            f"task#{tid} attempts={new_attempts} >= max={max_attempts} "
                            f"→ 终态 FAILED(需人工): {e}"
                        )
                    else:
                        # 可重试:指数退避
                        next_at_ts = self._calc_next_at(
                            new_attempts, self.task_wal_config.retry_backoff_seconds
                        )
                    try:
                        self.meta_db.mark_failed(
                            tid,
                            attempts=new_attempts,
                            next_at=next_at_ts,
                            error=f"{type(e).__name__}: {e}",
                        )
                    except Exception as mf_err:
                        logger.error(
                            f"mark_failed 失败 task#{tid}: {mf_err}"
                        )

            raise DualChannelError(f"extract extract 失败: {e}", cause=e)

    def _on_extract_done(self, future: Future) -> None:
        """提取完成回调（无论成功失败，释放 in_progress 标志）

        D 修复:原代码只 discard future,不读 future.exception() —
        导致任何在 executor 线程抛的异常都被静默吞掉,正是 3 条 stuck pending_writes
        永远 attempts=0 的根因。这里读 exception 并 log,
        让沉默的失败重新可见。
        """
        self._inflight.discard(future)
        with self._extraction_in_progress_lock:
            self._extraction_in_progress = False
            self._extraction_started_at = None

        # D 修复:fire-and-forget 的异常必须 log,否则 caller 完全看不见
        exc = future.exception()
        if exc is not None:
            logger.error(
                f"extract extract future 异常(已被 fire-and-forget 吞掉): "
                f"{type(exc).__name__}: {exc}",
                exc_info=exc,
            )
        else:
            logger.debug("extract extract future 正常完成")

    # ──────────────────────────────────────────────────────
    # A9: 优雅 shutdown
    # ──────────────────────────────────────────────────────

    def shutdown(self, timeout: float = 30.0) -> bool:
        """
        优雅 shutdown

        1. 拒绝新提交
        2. 等所有 in-flight 任务完成（最多 timeout 秒）
        3. 关 executor

        Returns: 是否所有任务完成（False = 有超时）
        """
        if self._shutdown:
            return True
        self._shutdown = True

        # 等所有 future
        all_done = True
        deadline = time.time() + timeout
        for future in list(self._inflight):
            remaining = max(0.0, deadline - time.time())
            try:
                future.result(timeout=remaining)
            except Exception:
                pass  # 已记录
            if not future.done():
                all_done = False

        self._executor.shutdown(wait=True, cancel_futures=not all_done)
        return all_done

    def _graceful_shutdown(self) -> None:
        """atexit 钩子（主进程退出时调用）"""
        try:
            self.shutdown(timeout=10.0)
        except Exception:
            pass

    # ──────────────────────────────────────────────────────
    # 诊断 / 重启恢复
    # ──────────────────────────────────────────────────────

    def startup_scan(self) -> dict[str, Any]:
        """启动时调用:4 步恢复流程,基于 memory_tasks 单一真相。

        Phase 2 / Step 2.2.10:替代旧 recover_pending(读 pending_writes)。
        新流程直接走 memory_tasks 表,不依赖 pending_writes。

        步骤:
          1a. delete_done_tasks  — 清理 >done_retention_seconds 的 DONE
          1b. delete_failed_tasks — 清理 >failed_retention_seconds 的 FAILED 终态
          2.  melt_stuck_inflight  — 熔断超 30 min 的 INFLIGHT
          3.  reschedule_retryable_failed — FAILED 退避到期 → PENDING
          4.  list_dispatchable_tasks — 返派工列表(NONE / PENDING,本 session)

        Returns:
            {
                "done_deleted": int,
                "failed_deleted": int,
                "inflight_melted": int,
                "failed_rescheduled": int,
                "dispatchable": list[dict],
            }
        """
        now = time.time()
        cfg = self.task_wal_config

        # 1a. 清理旧 DONE
        done_cutoff = now - cfg.done_retention_seconds
        done_deleted = self.meta_db.delete_done_tasks(before_timestamp=done_cutoff)

        # 1b. 清理旧 FAILED 终态
        failed_cutoff = now - cfg.failed_retention_seconds
        failed_deleted = self.meta_db.delete_failed_tasks(before_timestamp=failed_cutoff)

        # 2. 熔断 stuck INFLIGHT(30 min 阈值)
        #    与 inflight_at 比较:max_age = 30 min
        INFLIGHT_TIMEOUT_SECONDS = 1800
        inflight_melted = self.meta_db.melt_stuck_inflight(
            max_age_seconds=INFLIGHT_TIMEOUT_SECONDS
        )

        # 3. 重排退避到期的 FAILED
        failed_rescheduled = self.meta_db.reschedule_retryable_failed()

        # 4. 派工列表(本 session 的 NONE / PENDING)
        dispatchable = self.meta_db.list_dispatchable_tasks(
            session_id=self.session_id, limit=100
        )

        logger.info(
            f"startup_scan 完成: session={self.session_id} "
            f"done_deleted={done_deleted} failed_deleted={failed_deleted} "
            f"inflight_melted={inflight_melted} "
            f"failed_rescheduled={failed_rescheduled} "
            f"dispatchable={len(dispatchable)}"
        )

        return {
            "done_deleted": done_deleted,
            "failed_deleted": failed_deleted,
            "inflight_melted": inflight_melted,
            "failed_rescheduled": failed_rescheduled,
            "dispatchable": dispatchable,
        }

    def cleanup_done_tasks(self, retention_seconds: Optional[int] = None) -> int:
        """公开封装:清理旧 DONE 行,可在任何时间点手动调用。

        Phase 3 / Step 3.3.3:用 self.task_wal_config.done_retention_seconds 计算
        before_timestamp,调 meta_db.delete_done_tasks。允许传 retention_seconds
        覆盖(运维 / 测试用)。

        Args:
            retention_seconds: 覆盖默认 retention(可选)。

        Returns:
            删除行数。
        """
        retention = (
            retention_seconds
            if retention_seconds is not None
            else self.task_wal_config.done_retention_seconds
        )
        before_ts = time.time() - retention
        return self.meta_db.delete_done_tasks(before_timestamp=before_ts)

    def cleanup_failed_tasks(self, retention_seconds: Optional[int] = None) -> int:
        """公开封装:清理终态 FAILED 行(attempts >= max_attempts)。

        Phase 3 / Step 3.3.4:用 self.task_wal_config.failed_retention_seconds 计算
        before_timestamp,调 meta_db.delete_failed_tasks。退避中 FAILED
        (attempts < max_attempts)保留,等下次重试。

        Args:
            retention_seconds: 覆盖默认 retention(可选)。

        Returns:
            删除行数。
        """
        retention = (
            retention_seconds
            if retention_seconds is not None
            else self.task_wal_config.failed_retention_seconds
        )
        before_ts = time.time() - retention
        return self.meta_db.delete_failed_tasks(before_timestamp=before_ts)

    def stats(self) -> dict[str, Any]:
        """运行统计(用于 UI / 日志)

        Phase 4:删 daily_cursor / extract_cursor(已无 cursor 表,turn 进度看 memory_tasks)
        """
        return {
            "session_id": self.session_id,
            "extraction_in_progress": self._extraction_in_progress,
            "inflight_tasks": len(self._inflight),
            "shutdown": self._shutdown,
        }


__all__ = [
    "DualChannelWriter",
    "DualChannelError",
    "ExtractionInProgressError",
    "TurnMessage",
    "ExtractionCandidate",
    "VectorStoreProtocol",
]