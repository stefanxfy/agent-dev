"""
双通道写入器（v2.1 §4.1 脊柱）

M2 / Day 2 — A3+A4+A5+A9+A10 五项审查修复一次到位

设计要点：
1. 双通道
   - 通道 A：内联写 daily log（无 LLM，同步，秒级返回）
   - 通道 B：后台提取（LLM，async，进度持久化）
2. A3: cursor 持久化到 MetaDB（SQLite WAL）
   - daily_cursor: 已写入的 turn index
   - extract_cursor: 已提取的 turn index（≤ daily_cursor）
   - 重启时从 MetaDB 读回 → 幂等恢复
3. A4: 跨进程锁（flock via ipc_lock.IPCLock）
   - daily_lock: 防止 daily log 跨进程交叉写
   - extract_lock: 防止 LLM 提取跨进程重复
4. A5: 幂等去重（item_hash via MemoryStore）
   - 同 (session_id, item_hash) UNIQUE 约束
   - 同 hash 已存在 → skip，不写盘
5. A9: executor shutdown
   - atexit 注册 graceful shutdown
   - 主进程退出前等所有 in-flight 任务完成（最多 30s）
   - 支持 explicit shutdown(timeout=)
6. A10: transactional write
   - "先记 pending → 实际操作 → 成功后删 pending"
   - 崩溃时 pending 残留 → 启动时扫描 + 重试 / 丢弃

不变量（v2.1 §4.5）:
- #3 通道 A 只写 daily log，不调 LLM
- #4 通道 B 推进 extract_cursor 前必须成功写入

已知限制:
- vector_store 必须是 ChromaVectorStore 实现(见 agent_core.memory.chroma_store)
- _do_extract 是 LLM 提取的实际逻辑(M3 接 LLM router)
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
from agent_core.memory.ipc_lock import IPCLock, LockBusy, make_daily_lock, make_extract_lock
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
        reason="channel_b_sanitize_unrecoverable",
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

        # 通道 A: 内联写 daily log
        w.channel_a_inline_write("记住我叫小明", "已记", turn_index=0)

        # 通道 B: 后台提取(异步)
        w.channel_b_background_extract([TurnMessage(0, "我叫小明", "已记")])
        w.shutdown(timeout=30)

        # 重启恢复(A3)
        w2 = DualChannelWriter("s1", db, store, vec, embed)
        assert w2.daily_cursor == 0
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
        # embed_fn 必须非 None:channel B 写入 vector_store 需要先编码
        # 失败时立即 EmbeddingError,不静默(不再有 Mock 兜底)
        if embed_fn is None:
            raise DualChannelError(
                "DualChannelWriter 必须传入 embed_fn(用于向量化 memory)。"
                "用 make_embed_fn() 构造 bge-m3 实例。"
            )
        self.embed_fn = embed_fn

        # A3: cursor 从 MetaDB 加载
        self.daily_cursor: int = meta_db.get_cursor(session_id, "daily")
        self.extract_cursor: int = meta_db.get_cursor(session_id, "extract")

        # A4: 跨进程锁
        self._ipc_daily = make_daily_lock(memory_store.root)
        self._ipc_extract = make_extract_lock(memory_store.root)

        # A9: 后台 executor
        self._executor = ThreadPoolExecutor(
            max_workers=executor_workers,
            thread_name_prefix=f"chB-{session_id[:8]}",
        )
        self._shutdown = False
        self._inflight: set[Future] = set()
        self.extraction_timeout_seconds = extraction_timeout_seconds

        # A10: extraction_in_progress 标志（防止 channel_b 并发）
        self._extraction_in_progress = False
        self._extraction_in_progress_lock = threading.Lock()
        # M6 场景 8: 跟踪本次提取开始时间,实现 watchdog(防止 extractor hang 永久阻塞)
        self._extraction_started_at: Optional[float] = None

        # M10 C1.2: secret sanitize 配置
        self.scanner = SecretScanner()
        self.event_callback = event_callback

        # M10 C1.4: §14.1 防御纵深 — Channel B 入口 path validator
        # 即使 MemoryStore.write 已调 validator(C1.1 前置),
        # 这里再加一道,捕获任何 future regression。
        self._path_validator = MemoryPathValidator(memory_store.root)

        # atexit 优雅退出（A9）
        atexit.register(self._graceful_shutdown)

    # ──────────────────────────────────────────────────────
    # 通道 A：内联写 daily log（无 LLM，同步）
    # ──────────────────────────────────────────────────────

    def channel_a_inline_write(
        self,
        user_msg: str,
        assistant_resp: str,
        turn_index: Optional[int] = None,
    ) -> int:
        """
        通道 A：内联写一条 turn 到 memory_tasks(state=NONE)

        M11 / Phase 1.2.6:不再走 JSONL / pending / cursor 三 IO,直接写新表。
        state='NONE' 表示 Channel A 刚落盘,Channel B 取走时再 PENDING → INFLIGHT → DONE/FAILED。
        daily_cursor 属性仍维护(同步推进),Phase 4 才删。

        Args:
            user_msg: 用户消息
            assistant_resp: 助手响应
            turn_index: turn 在 session 内的全局索引。
                传 None(默认)= 用 daily_cursor + 1;
                显式传 int= 用 caller 提供的值(向后兼容,<= daily_cursor 时幂等跳过)。

        Returns:
            写入后的 daily_cursor

        不变量 #3: 不调 LLM，必须同步 + 快速（< 10ms）
        """
        if self._shutdown:
            raise DualChannelError("writer 已 shutdown，禁止写入")

        # Bug 1 修复:turn_index 缺省时用 daily_cursor + 1
        if turn_index is None:
            turn_index = self.daily_cursor + 1

        if turn_index <= self.daily_cursor:
            # 幂等：同 turn 已写过(caller 显式传了重复 turn_index)
            return self.daily_cursor

        # M11:直接走 memory_tasks 表,state='NONE' 标记 Channel A 刚落盘
        self.meta_db.insert_task(
            session_id=self.session_id,
            turn_index=turn_index,
            user_msg=user_msg,
            assistant_resp=assistant_resp,
            state="NONE",
            max_attempts=self.task_wal_config.max_retry,
        )

        # 同步推进 daily_cursor 属性 + 持久化到 cursors 表
        # (Phase 4 删 cursor 前保持兼容 — 旧测试期望重启时 daily_cursor 仍能恢复)
        self.daily_cursor = turn_index
        self.meta_db.set_cursor(self.session_id, "daily", turn_index)

        logger.debug(
            f"channel A: turn {turn_index} 写入 memory_tasks "
            f"(daily_cursor={self.daily_cursor})"
        )
        return self.daily_cursor

    def _do_channel_a_write(self, user_msg: str, assistant_resp: str, turn_index: int) -> None:
        """实际写盘（M2: 简化为 JSONL 追加；M4 接 DailyLogger）"""
        log_path = self.memory_store.root.parent / "logs" / f"{self.session_id}.jsonl"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        entry = {
            "turn_index": turn_index,
            "user_msg": user_msg,
            "assistant_resp": assistant_resp,
            "ts": time.time(),
        }
        # append + flush
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            f.flush()
            os.fsync(f.fileno())

    @staticmethod
    def _calc_next_at(attempts: int, retry_backoff_seconds: int) -> float:
        """退避公式:next_at = now + retry_backoff_seconds × 2^(attempts - 1)。

        attempts=1 → 基准间隔(默认 60s)
        attempts=2 → 2 倍
        attempts=3 → 4 倍
        attempts=0 → 0.5 × 基准(立即可重试,边界行为可接受)

        Phase 2 / Step 2.2.3:Channel B 失败重试时使用,startup_scan 步骤 3
        也会读 next_at 决定是否到时间重排 PENDING。
        """
        return time.time() + retry_backoff_seconds * (2 ** (attempts - 1))

    # ──────────────────────────────────────────────────────
    # 通道 B：后台 LLM 提取（异步 + cursor 持久化）
    # ──────────────────────────────────────────────────────

    def channel_b_background_extract(
        self,
        messages: list[TurnMessage],
        *,
        llm_extractor: Optional[Callable[[list[TurnMessage]], list[ExtractionCandidate]]] = None,
        advance_cursor: bool = True,
    ) -> Future:
        """
        通道 B：后台异步提取

        Args:
            messages: 要处理的 turn 列表（通常 [extract_cursor+1, daily_cursor]）
            llm_extractor: LLM 提取函数(M3 接 router;不传则用空列表占位)
            advance_cursor: 成功后是否推进 extract_cursor 到 daily_cursor + 1
                - True(默认):真实 extract,推进 cursor
                - False:recovery 重试,只清 stuck pending 不推进 cursor
                  (Bug 2 修复:否则会把 extract_cursor 推到 daily+1,
                  下次真实 extract 看到 to_process=[] 就 no-op)

        Returns:
            Future（调用方 shutdown(timeout=) 等完成）
        """
        if self._shutdown:
            raise DualChannelError("writer 已 shutdown，禁止提交")

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
                    f"session {self.session_id} 提取已在进行，避免并发"
                )
            self._extraction_in_progress = True
            self._extraction_started_at = time.time()

        future = self._executor.submit(
            self._do_channel_b_extract,
            messages,
            llm_extractor or (lambda _msgs: []),  # 默认空列表
            advance_cursor,  # Bug 2 修复
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
                    f"channel B: 语义重复(auto, sim={_sim_str} >= "
                    f"{cfg.auto_threshold}) 跳过 [{cand.type}] {cand.title!r} "
                    f"≈ 已有 {_top_title!r}"
                )
                return True

            if action is DedupAction.NEEDS_JUDGE:
                if self.dedup_judge is None:
                    logger.debug(
                        f"channel B: 可疑相似(sim={_sim_str})但无 dedup_judge,放行写盘"
                    )
                    return False
                is_dup = bool(self.dedup_judge(cand, hits))
                logger.info(
                    f"channel B: LLM 去重判定(sim={_sim_str}) → "
                    f"{'重复跳过' if is_dup else '新增写盘'} [{cand.type}] {cand.title!r}"
                )
                return is_dup

            return False  # NEW
        except Exception as e:
            logger.warning(
                f"channel B: 语义去重出错(放行写盘,不阻断): {type(e).__name__}: {e}"
            )
            return False

    def _do_channel_b_extract(
        self,
        messages: list[TurnMessage],
        extractor: Callable[[list[TurnMessage]], list[ExtractionCandidate]],
        advance_cursor: bool = True,
    ) -> dict[str, Any]:
        """
        实际执行提取（在 executor 线程）

        A4: 取 extract_lock
        A10: 先记 pending → 提取 → 成功后逐条写 MemoryStore → 推进 extract_cursor

        advance_cursor=False(Bug 2 修复):
        recovery 重试路径,只清 stuck pending 不推进 cursor。
        让下次真实 extract 能自然处理被 recovery 跳过的 turn range。
        """
        # 1. 计算待处理范围（[extract_cursor, daily_cursor] inclusive）
        #    extract_cursor 是 "下一个待处理 turn"（exclusive 下界）
        #    daily_cursor 是 "最后一个已写 turn"（inclusive 上界）
        start = self.extract_cursor
        to_process = [m for m in messages if start <= m.turn_index <= self.daily_cursor]
        if not to_process:
            # 被显式 dispatch 却没有可处理 turn = 一次提取被丢弃,属异常,升到 WARNING。
            # (DEBUG 级别会在 INFO 生产日志里完全隐身,Bug 1b 当初就是这样漏掉的)
            _incoming = [m.turn_index for m in messages]
            logger.warning(
                f"channel B: to_process 为空,提取被跳过 — "
                f"窗口=[{start},{self.daily_cursor}] (extract_cursor..daily_cursor), "
                f"传入 turn_index={_incoming}。"
                f"传入 turn 不在窗口内 → 该轮记忆不会被提取"
            )
            return {"extracted": 0, "written": 0, "skipped": 0}

        logger.info(
            f"channel B 提取开始: session={self.session_id}, "
            f"turn_range=[{start},{self.daily_cursor}], {len(to_process)} turns"
        )

        # A10 transactional(Phase 4 才删,新表状态机是 single source of truth)
        pending_id = self.meta_db.add_pending(self.session_id, {
            "action": "channel_b_extract",
            "session_id": self.session_id,
            "turn_range": [start, self.daily_cursor],
            "created_in_recovery": False,  # B 修复:标记来源,便于诊断
        })

        # M11 / Phase 2.2.10b:新表状态机 — 抢占每个 turn 的 task → INFLIGHT
        # (per-turn CAS,不抢到也不阻断 — 可能别的 writer 已在 INFLIGHT,
        # 失败由 startup_scan 的 melt_stuck_inflight 兜底)
        wal_inflight_ids: list[int] = []
        for m in to_process:
            task_row = self.meta_db.get_task_by_turn(self.session_id, m.turn_index)
            if task_row is None:
                # 没注册过的 turn(理论上 Channel A 写后必有,容错 skip)
                logger.debug(
                    f"channel B: turn={m.turn_index} 不在 memory_tasks(可能未走 Channel A),跳过 CAS"
                )
                continue
            if self.meta_db.cas_grab_task(
                task_row["task_id"], ["PENDING", "NONE"], "INFLIGHT"
            ):
                wal_inflight_ids.append(task_row["task_id"])
            else:
                logger.debug(
                    f"channel B: turn={m.turn_index} task#{task_row['task_id']} "
                    f"state={task_row['state']} 不在 (PENDING/NONE),跳过 CAS"
                )

        written = 0
        skipped = 0
        try:
            # A4: extract 跨进程锁（陈旧锁自动强占）
            with self._ipc_extract:
                # 2. LLM 提取（M3 接 router）
                candidates = extractor(to_process)
                logger.debug(
                    f"channel B: extractor 返回 {len(candidates)} candidates"
                )

                # 2026-06-24 Bug 1c 修复:candidates 与 to_process 不是 1:1。
                # bridge 的 extractor 忽略入参 messages,直接返回 gate 对"本轮整段最近
                # 对话"评出的 0-N 条 candidates —— 它们全是本次提取事件的产物,与
                # to_process 里的 turn 没有位置对应关系。
                # 旧代码 zip(to_process, candidates) 按位置配对:to_process 通常只有 1 条
                # (当前 turn)时,zip 只取 candidates[0](LLM 按对话顺序返回 → 最旧的
                # 未持久化项),其余 candidates 被静默截断 → 表现为"每轮都只存上一条/最旧
                # 的记忆,当前消息永远丢失"。
                # 正解:写入全部 candidates,统一盖上本次实际处理的最新 turn_index。
                stamp_turn = max(m.turn_index for m in to_process)

                # 3. 逐条写 MemoryStore（A5 幂等）
                #    将 session_id + turn_index 写入 frontmatter extra,
                #    供 list_by_session 查询
                for cand in candidates:
                    try:
                        # 候选记忆全貌(DEBUG):type / title / tags / score
                        # + body/source_quote 预览,排查"存的是什么"
                        _body_preview = cand.body[:80].replace("\n", " ")
                        _quote_preview = (cand.source_quote or "")[:60].replace("\n", " ")
                        logger.debug(
                            f"channel B: 候选记忆 turn={stamp_turn} type={cand.type!r} "
                            f"title={cand.title!r} tags={cand.tags} score={cand.score} "
                            f"| body[{len(cand.body)}]={_body_preview!r} "
                            f"| source_quote={_quote_preview!r}"
                        )

                        # M10 C1.4: §14.1 防御纵深 — Channel B 入口 path validator
                        # 用占位符 .md 构造 rel_path,只验证 type 是否在白名单 + 类型检查
                        rel_path = f"{cand.type}/__channel_b_preflight__.md"
                        try:
                            self._path_validator.validate(rel_path)
                        except PathSecurityError as e:
                            logger.warning(
                                f"channel_b: 拒绝 candidate(cand.title={cand.title!r}): "
                                f"type={cand.type!r} 触发 path validator: {e}"
                            )
                            if self.event_callback:
                                try:
                                    # 复用 SECRET_DETECTED 事件太重 — 改用 channel_b_path_rejected
                                    # 但目前 MemoryEventKind 没这个值,跳过 event 推送,只 log + skip
                                    pass
                                except Exception as cb_err:
                                    logger.error(f"event_callback 抛错: {cb_err}")
                            skipped += 1
                            continue

                        # M10 C1.2: §14.4 secret sanitize(写盘前)
                        # redact() 可能改 cand.body 中的 secret 区间
                        redacted_body = self.scanner.redact(cand.body)
                        if redacted_body != cand.body:
                            remaining = self.scanner.scan(redacted_body)
                            if not remaining.is_clean:
                                # redact 后仍命中(罕见,如双重编码)→ 整条丢弃 + 推事件
                                logger.warning(
                                    f"channel_b: 丢弃 candidate(cand.title={cand.title!r}): "
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
                                f"channel B: candidate {cand.title!r} 命中 secret,"
                                f"已 redact body 后继续写盘"
                            )
                            cand = replace(cand, body=redacted_body)

                        # ── 先向量化(候选 embedding,既用于语义去重召回,也用于写 vector store)──
                        #    计算 embedding 失败 → 整个 candidate 失败(此时尚未写 .md,无残留)
                        try:
                            text_for_emb = f"{cand.title}\n{cand.body}"
                            _emb_model = getattr(
                                self.embed_fn, "model_name", type(self.embed_fn).__name__
                            )
                            logger.debug(
                                f"channel B: 向量化中 model={_emb_model} "
                                f"text[{len(text_for_emb)}] chars (title={cand.title!r})"
                            )
                            embedding = self.embed_fn.encode(text_for_emb)
                        except Exception as enc_err:
                            raise DualChannelError(
                                f"向量化失败(candidate={cand.title}): {enc_err}",
                                cause=enc_err,
                            )

                        # ── 语义去重(向量召回 + LLM 判定)——写盘前拦重复 ──
                        #    dedup_config 为空 → 整段跳过,行为同旧版(只靠 item_hash 精确幂等)
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
                        # 落盘位置(DEBUG):.md 的绝对路径,排查"存在哪里"
                        md_path = self.memory_store.root / cand.type / f"{item_hash}.md"
                        logger.debug(
                            f"channel B: MemoryStore 已写 {md_path} "
                            f"(hash={item_hash[:12]}, body[{len(cand.body)}] chars)"
                        )

                        # 4. 写 vector store(复用上面算好的 embedding)
                        logger.debug(
                            f"channel B: 向量化完成 dim={len(embedding)} → chroma "
                            f"path={getattr(self.vector_store, '_path', '?')} "
                            f"collection={getattr(self.vector_store, '_collection_name', '?')} "
                            f"id={item_hash[:12]}"
                        )
                        self.vector_store.add({
                            "id": item_hash,
                            "embedding": embedding,
                            "metadata": {
                                "type": cand.type,
                                "title": cand.title,
                                "tags": cand.tags,
                                "session_id": self.session_id,
                            },
                            "document": text_for_emb,
                        })
                        logger.debug(
                            f"channel B: 向量已写入 chroma (id={item_hash[:12]})"
                        )
                        written += 1
                        logger.info(
                            f"channel B: 已持久化 [{cand.type}] {cand.title!r} "
                            f"(hash={item_hash[:8]}, turn={stamp_turn})"
                        )
                    except MemoryStoreError as e:
                        # A5: 同 hash 已存在 → skip（幂等）
                        if "已存在" in str(e):
                            skipped += 1
                            logger.debug(
                                f"channel B: [{cand.type}] {cand.title!r} 已存在, 跳过(幂等)"
                            )
                            continue
                        raise

                # 4. 推进 extract_cursor（不变量 #4：仅在成功后）
                #    推进到"本次实际处理的最大 turn_index + 1"
                # Bug 2 修复:recovery 路径 advance_cursor=False,这里不推进
                # → 让下次真实 extract 能处理被 recovery 跳过的 turn range
                #
                # 2026-06-24 race 修复:原来推到 daily_cursor + 1。正常流里
                # daily_cursor == 当前 turn,两者相等。但 deferred turn 排队后
                # 串行 flush 时,daily_cursor 可能已被后续 turn 推到更高:
                #   turn2,turn3 同时 defer(daily=3)→ flush turn2 推 cursor 到
                #   daily+1=4 → flush turn3 时 [4,3] 空集 → turn3 被静默丢弃。
                # 改用 max(to_process)+1 → flush turn2 只推到 3,turn3 仍在 [3,3]
                # 范围内,不丢。batch 场景 max==daily 等价,无回归。
                if advance_cursor:
                    max_processed = max(m.turn_index for m in to_process)
                    self.extract_cursor = max_processed + 1
                    self.meta_db.set_cursor(self.session_id, "extract", self.extract_cursor)
                    logger.debug(f"channel B: extract_cursor → {self.extract_cursor}")

            # 成功 → 删 pending
            self.meta_db.remove_pending(pending_id)

            # M11 / Phase 2.2.10b:写新表状态机 — 抢占过的 turn → DONE
            # candidates JSON 写到本次处理的最大 turn_index(对应 stamp_turn)的 task。
            # 退避公式用 _calc_next_at(虽然成功路径不写 next_at,留作 mark_failed 用)。
            if wal_inflight_ids and candidates is not None:
                try:
                    candidates_json = json.dumps(
                        [c.model_dump() if hasattr(c, "model_dump") else c.__dict__
                         for c in candidates],
                        ensure_ascii=False,
                    )
                except Exception as dump_err:
                    logger.warning(f"channel B: candidates JSON 序列化失败: {dump_err}")
                    candidates_json = "[]"
                # 优先:把 candidates 写到 stamp_turn 对应的 task(INFLIGHT 状态)
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
                f"channel B 提取完成: extracted={len(candidates)}, "
                f"written={written}, skipped={skipped}"
            )
            return {"extracted": len(candidates), "written": written, "skipped": skipped}

        except Exception as e:
            # 失败：保留 pending（崩溃恢复时扫描）
            # C 修复:失败时 bump attempts 计数 — 原代码 attempts 永远 = 0,
            # 导致重试次数不可见,recover_pending 无法做退避策略
            logger.error(
                f"channel B 提取失败 turn_range=[{start},{self.daily_cursor}]: {e}"
            )
            try:
                self.meta_db.bump_pending_attempts(pending_id)
            except Exception as bump_err:
                logger.error(f"bump_pending_attempts 失败: {bump_err}")

            # M11 / Phase 2.2.10b:写新表状态机 — 抢占过的 turn → FAILED
            # 用退避公式计算 next_at(下次 startup_scan 步骤 3 才会重排)
            for tid in wal_inflight_ids:
                task_now = self.meta_db.get_task(tid)
                if task_now and task_now["state"] == "INFLIGHT":
                    new_attempts = (task_now["attempts"] or 0) + 1
                    next_at = self._calc_next_at(
                        new_attempts, self.task_wal_config.retry_backoff_seconds
                    )
                    try:
                        self.meta_db.mark_failed(
                            tid,
                            attempts=new_attempts,
                            next_at=next_at,
                            error=f"{type(e).__name__}: {e}",
                        )
                    except Exception as mf_err:
                        logger.error(
                            f"mark_failed 失败 task#{tid}: {mf_err}"
                        )

            raise DualChannelError(f"channel B extract 失败: {e}", cause=e)

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
                f"channel B extract future 异常(已被 fire-and-forget 吞掉): "
                f"{type(exc).__name__}: {exc}",
                exc_info=exc,
            )
        else:
            logger.debug("channel B extract future 正常完成")

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

    # 重试上限:连续失败 N 次后放弃,删除 pending 行避免永久卡住
    MAX_RETRY_ATTEMPTS = 3

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

    def stats(self) -> dict[str, Any]:
        """运行统计（用于 UI / 日志）"""
        return {
            "session_id": self.session_id,
            "daily_cursor": self.daily_cursor,
            "extract_cursor": self.extract_cursor,
            "extraction_in_progress": self._extraction_in_progress,
            "inflight_tasks": len(self._inflight),
            "shutdown": self._shutdown,
        }

    # 重试上限:连续失败 N 次后放弃,删除 pending 行避免永久卡住
    MAX_RETRY_ATTEMPTS = 3

    def recover_pending(self) -> dict[str, Any]:
        """启动时调用：扫描 pending_writes,实际重试或丢弃

        B 修复:原实现仅返回 report,从来不重试,导致
        - 任何 channel B 异常残留的 pending 行永远卡住 attempts=0
        - 用户体感:"记了但永远检索不出来"(vector_store 是空的)

        新行为:
        1. 列出 session 全部 pending
        2. 对 channel_b_extract 类型:
           - attempts < MAX:重新提交一个空 extractor 的 _do_channel_b_extract
             (重新走 extract_cursor 推进 + remove_pending 的成功路径)
           - attempts >= MAX:log error + remove_pending(放弃)
        3. channel_a_write 类型:仅 report(因为 log 文件已经写了,cursor 没推进,
           真正重试需要重新解析 log + 重写 .md,超出本修复 scope)

        注意:无 LLM extractor 时重试 = no-op extract(空 candidates)+ 推进 cursor +
        删除 pending。等价于"跳过这段 stuck 区间",损失的只是这段区间的 candidates,
        但能把 cursor 推进过去,系统不再永久卡住。
        """
        pending = self.meta_db.list_pending(self.session_id)

        report: dict[str, Any] = {
            "session_id": self.session_id,
            "pending_count": len(pending),
            "retried": [],
            "dropped": [],
            "skipped": [],  # channel_a_write 或其他 action
        }

        # 防御性重置 _extraction_in_progress:
        # 启动时调 recover_pending,旧 future 的 _on_extract_done callback 可能
        # 还没跑(线程池未及时调度),导致 flag 仍为 True → channel_b_background_extract
        # 抛 ExtractionInProgressError → retry 失败。
        # 不变:只在 _inflight 已空(没有 in-flight 任务)时重置,绝不覆盖真实运行中任务。
        with self._extraction_in_progress_lock:
            if not self._inflight and self._extraction_in_progress:
                logger.warning(
                    "recover_pending: _extraction_in_progress=True 但 _inflight 已空,"
                    "防御性重置(对应旧 future 的 _on_extract_done 未及时跑)"
                )
                self._extraction_in_progress = False
                self._extraction_started_at = None

        for p in pending:
            payload = p["payload"]
            action = payload.get("action")

            if action != "channel_b_extract":
                report["skipped"].append({
                    "id": p["id"],
                    "action": action,
                    "reason": "暂不重试(只支持 channel_b_extract)",
                })
                continue

            # 检查 attempts 上限
            if p["attempts"] >= self.MAX_RETRY_ATTEMPTS:
                logger.error(
                    f"channel_b_extract pending#{p['id']} 已重试 {p['attempts']} 次,"
                    f"超过上限 {self.MAX_RETRY_ATTEMPTS},放弃 → remove_pending"
                )
                self.meta_db.remove_pending(p["id"])
                report["dropped"].append({
                    "id": p["id"],
                    "attempts": p["attempts"],
                    "reason": "max_retries_exceeded",
                })
                continue

            # 重试:走一遍 _do_channel_b_extract(用空 extractor,等价于 no-op)
            # 副作用:extract_cursor 推进到 daily_cursor + 1,pending 删除
            #
            # Bug 2 修复(2026-06-24):recovery 路径 advance_cursor=False,
            # 否则 extract_cursor 推进到 daily_cursor+1 会让下次真实 extract 看到
            # to_process=[] → no-op,新 turn 永远没机会被处理。
            #
            # 重要:先 remove_pending(old_id) 再 re-submit。原因是
            # _do_channel_b_extract 在成功路径只会 remove_pending 它自己 add 的新行,
            # 不会清旧的。所以不先 remove,旧的 stuck 行永远清不掉。
            try:
                # 重新构造 TurnMessage(从 daily log 读;读不到就用占位)
                msgs = self._load_messages_for_retry(payload)
                # 删旧 pending(无它 _do_channel_b_extract 不会知道要清它)
                self.meta_db.remove_pending(p["id"])
                future = self.channel_b_background_extract(
                    msgs,
                    llm_extractor=lambda _m: [],  # 空 → 等价于跳过 stuck 区间
                    advance_cursor=False,  # Bug 2 修复:recovery 不推进 cursor
                )
                future.add_done_callback(
                    lambda f, pid=p["id"]: self._on_recovery_done(f, pid, report)
                )
                report["retried"].append({
                    "id": p["id"],
                    "previous_attempts": p["attempts"],
                })
            except ExtractionInProgressError:
                # 别的 extract 正在跑,跳过这次 retry,下轮 recover_pending 再试
                logger.warning(
                    f"channel_b_extract pending#{p['id']} 重试跳过:extract_in_progress"
                )
                report["skipped"].append({
                    "id": p["id"],
                    "action": action,
                    "reason": "extraction_in_progress",
                })

        return report

    def _load_messages_for_retry(
        self, payload: dict[str, Any],
    ) -> list[TurnMessage]:
        """从 daily log 重新加载 messages(给 recovery 用)

        payload 里只有 turn_range,真实数据在 ~/.agent_data/logs/{session_id}.jsonl。
        读不到 → 返回占位(extract 会 no-op,但 pending 仍会被清掉)。
        """
        log_path = self.memory_store.root.parent / "logs" / f"{self.session_id}.jsonl"
        if not log_path.exists():
            # log 没了 — 用占位 TurnMessage 跑 no-op extract,清掉 pending
            turn_range = payload.get("turn_range", [0, 0])
            return [
                TurnMessage(
                    turn_index=turn_range[0],
                    user_msg="[recovery: log missing]",
                    assistant_resp="[recovery: log missing]",
                ),
            ]

        # 读 daily log,取 payload.turn_range 内的 turn
        import json as _json
        turn_range = payload.get("turn_range", [0, self.daily_cursor])
        start, end = turn_range
        msgs: list[TurnMessage] = []
        try:
            with open(log_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    entry = _json.loads(line)
                    idx = entry.get("turn_index", -1)
                    if start <= idx <= end:
                        msgs.append(TurnMessage(
                            turn_index=idx,
                            user_msg=entry.get("user_msg", ""),
                            assistant_resp=entry.get("assistant_resp", ""),
                            timestamp=entry.get("ts", 0.0),
                        ))
        except Exception as e:
            logger.warning(f"recovery 读 log 失败: {e},用占位")
            return [
                TurnMessage(
                    turn_index=start,
                    user_msg="[recovery: log read error]",
                    assistant_resp="[recovery: log read error]",
                ),
            ]
        return msgs

    def _on_recovery_done(
        self,
        future: Future,
        pending_id: int,
        report: dict[str, Any],
    ) -> None:
        """recovery 重试完成回调 — 给 report 打结果

        成功:pending 已被 _do_channel_b_extract remove_pending
        失败:attempts 已 bump +1,下一轮 recover_pending 接着试
        """
        exc = future.exception()
        if exc is None:
            logger.info(
                f"recovery pending#{pending_id} 重试成功(cursor 推进 + pending 删除)"
            )
        else:
            logger.warning(
                f"recovery pending#{pending_id} 重试仍失败:{type(exc).__name__}: {exc}"
            )


__all__ = [
    "DualChannelWriter",
    "DualChannelError",
    "ExtractionInProgressError",
    "TurnMessage",
    "ExtractionCandidate",
    "VectorStoreProtocol",
]