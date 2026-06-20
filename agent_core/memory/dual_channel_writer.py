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
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, Future
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional, Protocol, Union

from agent_core.exceptions import AgentError
from agent_core.memory.embeddings import EmbedFn
from agent_core.memory.ipc_lock import IPCLock, LockBusy, make_daily_lock, make_extract_lock
from agent_core.memory.memory_store import (
    MemoryStore,
    MemoryStoreError,
    compute_item_hash,
)
from agent_core.memory.meta_db import MetaDB


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
    ):
        self.session_id = session_id
        self.meta_db = meta_db
        self.memory_store = memory_store
        self.vector_store = vector_store
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

        # atexit 优雅退出（A9）
        atexit.register(self._graceful_shutdown)

    # ──────────────────────────────────────────────────────
    # 通道 A：内联写 daily log（无 LLM，同步）
    # ──────────────────────────────────────────────────────

    def channel_a_inline_write(
        self,
        user_msg: str,
        assistant_resp: str,
        turn_index: int,
    ) -> int:
        """
        通道 A：内联写一条 turn 到 daily log

        Returns:
            写入后的 daily_cursor

        不变量 #3: 不调 LLM，必须同步 + 快速（< 10ms）
        不变量 #4: 推进 cursor 前必须成功写盘（A10 transactional）
        """
        if self._shutdown:
            raise DualChannelError("writer 已 shutdown，禁止写入")

        if turn_index <= self.daily_cursor:
            # 幂等：同 turn 已写过
            return self.daily_cursor

        # A10 transactional: 先记 pending
        pending_id = self.meta_db.add_pending(self.session_id, {
            "action": "channel_a_write",
            "turn_index": turn_index,
            "user_msg": user_msg,
            "assistant_resp": assistant_resp,
        })

        # A4: 跨进程锁 + 进程内 lock 双层保护
        with self._ipc_daily:
            try:
                self._do_channel_a_write(user_msg, assistant_resp, turn_index)
                # A3: 推进 cursor（仅在写成功后）
                self.daily_cursor = turn_index
                self.meta_db.set_cursor(self.session_id, "daily", turn_index)
            except Exception:
                # 失败：保留 pending（下次启动可重试）
                raise
            else:
                # 成功：删除 pending
                self.meta_db.remove_pending(pending_id)
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

    # ──────────────────────────────────────────────────────
    # 通道 B：后台 LLM 提取（异步 + cursor 持久化）
    # ──────────────────────────────────────────────────────

    def channel_b_background_extract(
        self,
        messages: list[TurnMessage],
        *,
        llm_extractor: Optional[Callable[[list[TurnMessage]], list[ExtractionCandidate]]] = None,
    ) -> Future:
        """
        通道 B：后台异步提取

        Args:
            messages: 要处理的 turn 列表（通常 [extract_cursor+1, daily_cursor]）
            llm_extractor: LLM 提取函数(M3 接 router;不传则用空列表占位)

        Returns:
            Future（调用方 shutdown(timeout=) 等完成）
        """
        if self._shutdown:
            raise DualChannelError("writer 已 shutdown，禁止提交")

        # A10: extraction_in_progress 标志
        with self._extraction_in_progress_lock:
            if self._extraction_in_progress:
                raise ExtractionInProgressError(
                    f"session {self.session_id} 提取已在进行，避免并发"
                )
            self._extraction_in_progress = True

        future = self._executor.submit(
            self._do_channel_b_extract,
            messages,
            llm_extractor or (lambda _msgs: []),  # 默认空列表
        )
        self._inflight.add(future)
        future.add_done_callback(self._on_extract_done)
        return future

    def _do_channel_b_extract(
        self,
        messages: list[TurnMessage],
        extractor: Callable[[list[TurnMessage]], list[ExtractionCandidate]],
    ) -> dict[str, Any]:
        """
        实际执行提取（在 executor 线程）

        A4: 取 extract_lock
        A10: 先记 pending → 提取 → 成功后逐条写 MemoryStore → 推进 extract_cursor
        """
        # 1. 计算待处理范围（[extract_cursor, daily_cursor] inclusive）
        #    extract_cursor 是 "下一个待处理 turn"（exclusive 下界）
        #    daily_cursor 是 "最后一个已写 turn"（inclusive 上界）
        start = self.extract_cursor
        to_process = [m for m in messages if start <= m.turn_index <= self.daily_cursor]
        if not to_process:
            return {"extracted": 0, "written": 0, "skipped": 0}

        # A10 transactional
        pending_id = self.meta_db.add_pending(self.session_id, {
            "action": "channel_b_extract",
            "session_id": self.session_id,
            "turn_range": [start, self.daily_cursor],
        })

        written = 0
        skipped = 0
        try:
            # A4: extract 跨进程锁（陈旧锁自动强占）
            with self._ipc_extract:
                # 2. LLM 提取（M3 接 router）
                candidates = extractor(to_process)

                # 3. 逐条写 MemoryStore（A5 幂等）
                for cand in candidates:
                    try:
                        item_hash = self.memory_store.write(
                            type=cand.type,
                            title=cand.title,
                            body=cand.body,
                            source_quote=cand.source_quote,
                            tags=cand.tags,
                        )
                        # 4. 写 vector store —— 必须含 embedding(ChromaVectorStore 强制)
                        #    计算 embedding 失败 → 整个 candidate 失败,MemoryStore 已写不撤回
                        #    (caller 可通过 cold_start 重新向量化,见 cold_start.py M5 重索引逻辑)
                        try:
                            text_for_emb = f"{cand.title}\n{cand.body}"
                            embedding = self.embed_fn.encode(text_for_emb)
                        except Exception as enc_err:
                            raise DualChannelError(
                                f"向量化失败(candidate={cand.title}): {enc_err}",
                                cause=enc_err,
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
                        written += 1
                    except MemoryStoreError as e:
                        # A5: 同 hash 已存在 → skip（幂等）
                        if "已存在" in str(e):
                            skipped += 1
                            continue
                        raise

                # 4. 推进 extract_cursor（不变量 #4：仅在成功后）
                #    推进到 daily_cursor + 1（下一个待处理 turn）
                self.extract_cursor = self.daily_cursor + 1
                self.meta_db.set_cursor(self.session_id, "extract", self.extract_cursor)

            # 成功 → 删 pending
            self.meta_db.remove_pending(pending_id)
            return {"extracted": len(candidates), "written": written, "skipped": skipped}

        except Exception as e:
            # 失败：保留 pending（崩溃恢复时扫描）
            raise DualChannelError(f"channel B extract 失败: {e}", cause=e)

    def _on_extract_done(self, future: Future) -> None:
        """提取完成回调（无论成功失败，释放 in_progress 标志）"""
        self._inflight.discard(future)
        with self._extraction_in_progress_lock:
            self._extraction_in_progress = False

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

    def recover_pending(self) -> dict[str, Any]:
        """
        启动时调用：扫描 pending_writes，决定是重试还是丢弃

        M2 阶段：仅 report（不重做），M3+ 加 retry 策略
        """
        pending = self.meta_db.list_pending(self.session_id)
        return {
            "session_id": self.session_id,
            "pending_count": len(pending),
            "pending": pending,
        }


__all__ = [
    "DualChannelWriter",
    "DualChannelError",
    "ExtractionInProgressError",
    "TurnMessage",
    "ExtractionCandidate",
    "VectorStoreProtocol",
]