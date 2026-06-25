"""
记忆系统元数据库（SQLite WAL）

M2 / Day 2 — A3 修复 + v2.1 §4.1 设计

设计要点：
1. SQLite WAL 模式 —— 支持并发读 + 单写者（适合 cursor 持久化高频写场景）
2. 3 张核心表：
   - cursors: (session_id, cursor_kind, value, updated_at)
     用于 A3 持久化 daily_cursor / extract_cursor，重启恢复
   - pending_writes: (id, session_id, payload, attempts, created_at)
     用于 A10 transactional write（先记入 pending，成功后删）
   - candidates: (id, session_id, item_hash, type, status, payload, created_at)
     用于 LLM 提取的候选记忆（A5 幂等去重 / L6 status 区分）
3. connection per thread（Python SQLite 默认行为）
4. 支持 `:memory:`（测试）与磁盘路径（生产）
5. 全部 UPSERT（ON CONFLICT REPLACE），避免竞态
"""

from __future__ import annotations

import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Optional

from agent_core.exceptions import StorageError


# ──────────────────────────────────────────────────────────────────
# 异常
# ──────────────────────────────────────────────────────────────────

class MetaDBError(StorageError):
    """元数据库异常"""
    code = "META_DB"


# ──────────────────────────────────────────────────────────────────
# 表结构（DDL）
# ──────────────────────────────────────────────────────────────────

_DDL = """
CREATE TABLE IF NOT EXISTS cursors (
    session_id   TEXT NOT NULL,
    cursor_kind  TEXT NOT NULL,
    value        INTEGER NOT NULL,
    updated_at   REAL NOT NULL,
    PRIMARY KEY (session_id, cursor_kind)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS pending_writes (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id   TEXT NOT NULL,
    payload      TEXT NOT NULL,           -- JSON 序列化
    attempts     INTEGER NOT NULL DEFAULT 0,
    created_at   REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_pending_session
    ON pending_writes (session_id, created_at);

CREATE TABLE IF NOT EXISTS candidates (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id   TEXT NOT NULL,
    item_hash    TEXT NOT NULL,           -- SHA-256 hex (A5 幂等去重)
    type         TEXT NOT NULL,           -- 4 类之一
    status       TEXT NOT NULL,           -- L6: pending|accepted|rejected|written
    payload      TEXT NOT NULL,           -- JSON: {title, body, source_quote, tags}
    score        REAL,                    -- LLM 自评 (0-1)
    created_at   REAL NOT NULL,
    UNIQUE (session_id, item_hash)        -- A5: 同 session 同 hash 不重复
);

CREATE INDEX IF NOT EXISTS idx_candidates_status
    ON candidates (session_id, status, created_at);

CREATE TABLE IF NOT EXISTS candidate_decisions (
    cand_key    TEXT NOT NULL,           -- compute_candidate_key(type, body) (M10 C4.4)
    decision    TEXT NOT NULL,           -- accepted | rejected
    decided_at  REAL NOT NULL,
    PRIMARY KEY (cand_key)
);

-- M11: memory_tasks 表 —— 单表收编 turn 原文 + 状态机 + candidates + 重试信息
-- 替代旧 cursors / pending_writes / JSONL 三源架构
-- 五态:NONE / PENDING / INFLIGHT / DONE / FAILED
CREATE TABLE IF NOT EXISTS memory_tasks (
    task_id              INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id           TEXT NOT NULL,
    turn_index           INTEGER NOT NULL,
    state                TEXT NOT NULL DEFAULT 'NONE'
                          CHECK(state IN ('NONE','PENDING','INFLIGHT','DONE','FAILED')),
    attempts             INTEGER NOT NULL DEFAULT 0,
    max_attempts         INTEGER NOT NULL DEFAULT 3,
    next_at              REAL,
    inflight_at          REAL,
    user_msg             TEXT NOT NULL,
    assistant_resp       TEXT NOT NULL,
    turn_metadata        TEXT,
    candidates_payload   TEXT,
    extraction_error     TEXT,
    created_at           REAL NOT NULL,
    updated_at           REAL NOT NULL,
    UNIQUE (session_id, turn_index)
);

CREATE INDEX IF NOT EXISTS idx_tasks_state
    ON memory_tasks (state, next_at);

CREATE INDEX IF NOT EXISTS idx_tasks_session_turn
    ON memory_tasks (session_id, turn_index);

CREATE INDEX IF NOT EXISTS idx_tasks_compaction
    ON memory_tasks (state, updated_at);
"""


# ──────────────────────────────────────────────────────────────────
# MetaDB 类
# ──────────────────────────────────────────────────────────────────

class MetaDB:
    """
    记忆系统元数据库（SQLite WAL 模式）

    用法:
        db = MetaDB(":memory:")                  # 测试
        db = MetaDB("~/.agent_data/meta.db")     # 生产

        # Cursor 操作（A3）
        db.set_cursor("s1", "daily", 5)
        v = db.get_cursor("s1", "daily")  # 5

        # Pending writes（A10）
        pid = db.add_pending("s1", {"action": "write", "path": "user/foo.md"})
        db.remove_pending(pid)

        # Candidates（L6 + A5）
        cid = db.add_candidate("s1", "abc123", "user", "pending",
                               {"title": "...", "body": "...", "source_quote": "..."})
        db.update_candidate_status(cid, "accepted")
    """

    def __init__(self, path: str | Path):
        self.path = str(path)
        self._is_memory = self.path == ":memory:"
        self._local = threading.local()  # connection per thread

        # 仅非 :memory: 模式启用 WAL + 目录创建
        if not self._is_memory:
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
            # 首次连接前打 PRAGMA（WAL 模式需要）

    # ── 连接管理（per-thread） ────────────────────────────────

    def _conn(self) -> sqlite3.Connection:
        """获取当前线程的连接（懒创建）"""
        if not hasattr(self._local, "conn") or self._local.conn is None:
            conn = sqlite3.connect(
                self.path,
                timeout=30.0,
                isolation_level=None,  # autocommit，我们手动 BEGIN/COMMIT
                check_same_thread=False,
            )
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")  # WAL 模式
            conn.execute("PRAGMA synchronous=NORMAL")  # 性能/安全折中
            conn.execute("PRAGMA foreign_keys=ON")
            # 创建表
            conn.executescript(_DDL)
            self._local.conn = conn
        return self._local.conn

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """
        事务上下文（A10 关键）

        用法:
            with db.transaction() as conn:
                conn.execute("UPDATE ...")
                conn.execute("INSERT ...")
            # 自动 COMMIT；异常时自动 ROLLBACK
        """
        conn = self._conn()
        try:
            conn.execute("BEGIN IMMEDIATE")  # IMMEDIATE 立刻获取写锁，避免 SQLITE_BUSY
            yield conn
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise

    def close(self) -> None:
        """关闭当前线程的连接（其他线程的连接依然保留直到线程退出）"""
        if hasattr(self._local, "conn") and self._local.conn is not None:
            self._local.conn.close()
            self._local.conn = None

    # ── Cursor（A3 持久化） ───────────────────────────────────

    def set_cursor(self, session_id: str, kind: str, value: int) -> None:
        """UPSERT cursor（原子，覆盖式）"""
        try:
            with self.transaction() as conn:
                conn.execute(
                    """
                    INSERT INTO cursors (session_id, cursor_kind, value, updated_at)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT (session_id, cursor_kind) DO UPDATE SET
                        value = excluded.value,
                        updated_at = excluded.updated_at
                    """,
                    (session_id, kind, value, time.time()),
                )
        except sqlite3.Error as e:
            raise MetaDBError(f"set_cursor 失败: {e}", cause=e)

    def get_cursor(self, session_id: str, kind: str, default: int = 0) -> int:
        """读取 cursor，不存在返回 default"""
        try:
            row = self._conn().execute(
                "SELECT value FROM cursors WHERE session_id = ? AND cursor_kind = ?",
                (session_id, kind),
            ).fetchone()
            return int(row["value"]) if row else default
        except sqlite3.Error as e:
            raise MetaDBError(f"get_cursor 失败: {e}", cause=e)

    # ── Pending writes（A10 transactional） ─────────────────

    def add_pending(self, session_id: str, payload: dict[str, Any]) -> int:
        """
        记录一次 pending write（A10：先记 pending → 实际操作 → 成功后删 pending）

        Returns: pending row id
        """
        import json
        try:
            with self.transaction() as conn:
                cur = conn.execute(
                    """
                    INSERT INTO pending_writes (session_id, payload, attempts, created_at)
                    VALUES (?, ?, 0, ?)
                    """,
                    (session_id, json.dumps(payload, ensure_ascii=False), time.time()),
                )
                return int(cur.lastrowid)
        except sqlite3.Error as e:
            raise MetaDBError(f"add_pending 失败: {e}", cause=e)

    def remove_pending(self, pending_id: int) -> None:
        """成功后删除 pending 行"""
        try:
            with self.transaction() as conn:
                conn.execute("DELETE FROM pending_writes WHERE id = ?", (pending_id,))
        except sqlite3.Error as e:
            raise MetaDBError(f"remove_pending 失败: {e}", cause=e)

    def bump_pending_attempts(self, pending_id: int) -> int:
        """C 修复:失败后递增 attempts 计数(原代码只设 0,从不递增)

        Returns: 递增后的 attempts 值
        """
        try:
            with self.transaction() as conn:
                conn.execute(
                    "UPDATE pending_writes SET attempts = attempts + 1 WHERE id = ?",
                    (pending_id,),
                )
                row = conn.execute(
                    "SELECT attempts FROM pending_writes WHERE id = ?",
                    (pending_id,),
                ).fetchone()
                return int(row["attempts"]) if row else 0
        except sqlite3.Error as e:
            raise MetaDBError(f"bump_pending_attempts 失败: {e}", cause=e)

    def update_pending_payload(self, pending_id: int, payload: dict[str, Any]) -> None:
        """重试时更新 payload(如 attempts 用尽后改为 drop 标记)"""
        import json
        try:
            with self.transaction() as conn:
                conn.execute(
                    "UPDATE pending_writes SET payload = ? WHERE id = ?",
                    (json.dumps(payload, ensure_ascii=False), pending_id),
                )
        except sqlite3.Error as e:
            raise MetaDBError(f"update_pending_payload 失败: {e}", cause=e)

    def list_pending(self, session_id: Optional[str] = None) -> list[dict]:
        """列出 pending writes（用于崩溃恢复 / 调试）"""
        import json
        try:
            if session_id is not None:
                rows = self._conn().execute(
                    "SELECT id, session_id, payload, attempts, created_at FROM pending_writes "
                    "WHERE session_id = ? ORDER BY created_at",
                    (session_id,),
                ).fetchall()
            else:
                rows = self._conn().execute(
                    "SELECT id, session_id, payload, attempts, created_at FROM pending_writes "
                    "ORDER BY created_at"
                ).fetchall()
            return [
                {
                    "id": r["id"],
                    "session_id": r["session_id"],
                    "payload": json.loads(r["payload"]),
                    "attempts": r["attempts"],
                    "created_at": r["created_at"],
                }
                for r in rows
            ]
        except sqlite3.Error as e:
            raise MetaDBError(f"list_pending 失败: {e}", cause=e)

    # ── memory_tasks（M11 单表 WAL 状态机） ───────────

    def insert_task(
        self,
        session_id: str,
        turn_index: int,
        user_msg: str,
        assistant_resp: str,
        state: str = "NONE",
        max_attempts: int = 3,
        turn_metadata: Optional[str] = None,
    ) -> Optional[int]:
        """插入一行 memory_tasks,返回 task_id。

        UNIQUE(session_id, turn_index) 约束:重复插入返回 None(幂等)。
        A 通道写盘时使用,同 session 同 turn 二次写 = 幂等,不算错。
        """
        now = time.time()
        try:
            with self.transaction() as conn:
                try:
                    cur = conn.execute(
                        """
                        INSERT INTO memory_tasks
                            (session_id, turn_index, user_msg, assistant_resp,
                             state, max_attempts, turn_metadata,
                             created_at, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            session_id, turn_index, user_msg, assistant_resp,
                            state, max_attempts, turn_metadata,
                            now, now,
                        ),
                    )
                    return int(cur.lastrowid)
                except sqlite3.IntegrityError:
                    # UNIQUE 冲突,幂等返回 None
                    return None
        except sqlite3.Error as e:
            raise MetaDBError(f"insert_task 失败: {e}", cause=e)

    def get_task(self, task_id: int) -> Optional[dict]:
        """按 task_id 读一行,返回 dict 或 None(不存在)。"""
        try:
            row = self._conn().execute(
                """
                SELECT task_id, session_id, turn_index, state,
                       attempts, max_attempts, next_at, inflight_at,
                       user_msg, assistant_resp, turn_metadata,
                       candidates_payload, extraction_error,
                       created_at, updated_at
                FROM memory_tasks WHERE task_id = ?
                """,
                (task_id,),
            ).fetchone()
            if not row:
                return None
            return dict(row)
        except sqlite3.Error as e:
            raise MetaDBError(f"get_task 失败: {e}", cause=e)

    def cas_grab_task(
        self,
        task_id: int,
        from_states: list[str],
        to_state: str,
    ) -> bool:
        """CAS 抢占:state ∈ from_states → 转到 to_state,自动写 inflight_at=now()。

        Returns:
            True  抢到(rowcount==1)
            False 别人抢到(state 已不在 from_states 中)
        """
        now = time.time()
        placeholders = ",".join("?" * len(from_states))
        try:
            with self.transaction() as conn:
                cur = conn.execute(
                    f"""
                    UPDATE memory_tasks
                    SET state = ?,
                        inflight_at = ?,
                        updated_at = ?
                    WHERE task_id = ?
                      AND state IN ({placeholders})
                    """,
                    (to_state, now, now, task_id, *from_states),
                )
                return cur.rowcount == 1
        except sqlite3.Error as e:
            raise MetaDBError(f"cas_grab_task 失败: {e}", cause=e)

    def update_task_state(
        self,
        task_id: int,
        new_state: str,
    ) -> None:
        """通用 state 字段更新,自动刷 updated_at。"""
        try:
            with self.transaction() as conn:
                conn.execute(
                    "UPDATE memory_tasks SET state = ?, updated_at = ? WHERE task_id = ?",
                    (new_state, time.time(), task_id),
                )
        except sqlite3.Error as e:
            raise MetaDBError(f"update_task_state 失败: {e}", cause=e)

    def mark_done_with_candidates(
        self,
        task_id: int,
        candidates_payload: str,
    ) -> None:
        """state → DONE,candidates_payload 落盘(JSON 字符串)。

        Phase 2 关键:此方法在 LLM 算完之后、写 memory_store 之前调用,
        确保 candidates 持久化,崩了不丢。
        """
        try:
            with self.transaction() as conn:
                conn.execute(
                    """
                    UPDATE memory_tasks
                    SET state = 'DONE',
                        candidates_payload = ?,
                        updated_at = ?
                    WHERE task_id = ?
                    """,
                    (candidates_payload, time.time(), task_id),
                )
        except sqlite3.Error as e:
            raise MetaDBError(f"mark_done_with_candidates 失败: {e}", cause=e)

    def mark_failed(
        self,
        task_id: int,
        attempts: int,
        next_at: Optional[float],
        error: str,
    ) -> None:
        """state → FAILED,记录 attempts / next_at / extraction_error。

        Args:
            attempts: 本次失败后的累计次数
            next_at: 下次可重试时间(指数退避算出);终态(>=max)时传 None
            error: 错误描述
        """
        try:
            with self.transaction() as conn:
                conn.execute(
                    """
                    UPDATE memory_tasks
                    SET state = 'FAILED',
                        attempts = ?,
                        next_at = ?,
                        extraction_error = ?,
                        updated_at = ?
                    WHERE task_id = ?
                    """,
                    (attempts, next_at, error, time.time(), task_id),
                )
        except sqlite3.Error as e:
            raise MetaDBError(f"mark_failed 失败: {e}", cause=e)

    # ── M11 startup_scan helpers ──────────────────────

    def delete_done_tasks(self, before_timestamp: float) -> int:
        """清理 state='DONE' AND updated_at < before_timestamp 的行。

        Phase 2 / Step 2.2.5:startup_scan 步骤 1a 调用,根据
        task_wal_config.done_retention_seconds 计算 before_timestamp。
        Returns:删除行数。
        """
        try:
            with self.transaction() as conn:
                cur = conn.execute(
                    "DELETE FROM memory_tasks "
                    "WHERE state = 'DONE' AND updated_at < ?",
                    (before_timestamp,),
                )
                return cur.rowcount
        except sqlite3.Error as e:
            raise MetaDBError(f"delete_done_tasks 失败: {e}", cause=e)

    def delete_failed_tasks(self, before_timestamp: float) -> int:
        """清理 FAILED 终态(attempts >= max_attempts)AND updated_at < before_timestamp。

        Phase 2 / Step 2.2.6:startup_scan 步骤 1b,只删终态 FAILED,
        退避中 FAILED(attempts < max_attempts)保留等下次重试。
        """
        try:
            with self.transaction() as conn:
                cur = conn.execute(
                    "DELETE FROM memory_tasks "
                    "WHERE state = 'FAILED' AND attempts >= max_attempts "
                    "AND updated_at < ?",
                    (before_timestamp,),
                )
                return cur.rowcount
        except sqlite3.Error as e:
            raise MetaDBError(f"delete_failed_tasks 失败: {e}", cause=e)

    def melt_stuck_inflight(self, max_age_seconds: float) -> int:
        """熔断卡死 INFLIGHT:state='INFLIGHT' AND inflight_at < now - max_age_seconds → FAILED。

        Phase 2 / Step 2.2.7:startup_scan 步骤 2,处理进程崩溃导致 INFLIGHT
        永远不释放的死锁。attempts+1 + 写 next_at(用退避公式) + extraction_error
        标记 "inflight timeout"。
        """
        now = time.time()
        cutoff = now - max_age_seconds
        try:
            with self.transaction() as conn:
                # 找出 stuck 行,先读再算 next_at
                cur = conn.execute(
                    "SELECT task_id, attempts, max_attempts FROM memory_tasks "
                    "WHERE state = 'INFLIGHT' AND inflight_at < ?",
                    (cutoff,),
                )
                rows = cur.fetchall()
                if not rows:
                    return 0
                for task_id, attempts, max_attempts in rows:
                    new_attempts = (attempts or 0) + 1
                    # 退避公式:now + 60 × 2^(new_attempts-1)
                    backoff = 60 * (2 ** (new_attempts - 1))
                    next_at = now + backoff
                    error_msg = f"inflight timeout (>{max_age_seconds}s)"
                    conn.execute(
                        "UPDATE memory_tasks "
                        "SET state = 'FAILED', "
                        "    attempts = ?, "
                        "    next_at = ?, "
                        "    extraction_error = ?, "
                        "    updated_at = ? "
                        "WHERE task_id = ? AND state = 'INFLIGHT'",
                        (new_attempts, next_at, error_msg, now, task_id),
                    )
                return len(rows)
        except sqlite3.Error as e:
            raise MetaDBError(f"melt_stuck_inflight 失败: {e}", cause=e)

    def list_dispatchable_tasks(
        self,
        session_id: Optional[str] = None,
        limit: int = 100,
    ) -> list[dict]:
        """派工列表:state IN ('NONE', 'PENDING') ORDER BY turn_index ASC。

        Phase 2 / Step 2.2.9:startup_scan 步骤 4 返回所有待处理任务,
        Channel B 后台循环 dispatch 时按 turn_index 顺序处理。
        """
        try:
            with self.transaction() as conn:
                if session_id is None:
                    cur = conn.execute(
                        "SELECT task_id, session_id, turn_index, state, attempts, "
                        "       max_attempts, next_at, inflight_at "
                        "FROM memory_tasks "
                        "WHERE state IN ('NONE', 'PENDING') "
                        "ORDER BY turn_index ASC LIMIT ?",
                        (limit,),
                    )
                else:
                    cur = conn.execute(
                        "SELECT task_id, session_id, turn_index, state, attempts, "
                        "       max_attempts, next_at, inflight_at "
                        "FROM memory_tasks "
                        "WHERE session_id = ? AND state IN ('NONE', 'PENDING') "
                        "ORDER BY turn_index ASC LIMIT ?",
                        (session_id, limit),
                    )
                cols = [d[0] for d in cur.description]
                return [dict(zip(cols, row)) for row in cur.fetchall()]
        except sqlite3.Error as e:
            raise MetaDBError(f"list_dispatchable_tasks 失败: {e}", cause=e)

    # ── Candidates（L6 + A5 幂等去重） ──────────────────────

    def add_candidate(
        self,
        session_id: str,
        item_hash: str,
        type_: str,
        status: str,
        payload: dict[str, Any],
        score: Optional[float] = None,
    ) -> Optional[int]:
        """
        添加一条 candidate 记忆

        A5 幂等:UNIQUE (session_id, item_hash) 约束 → 重复插入返回 None
        Returns: candidate id，新增成功；重复则返回 None
        """
        import json
        try:
            with self.transaction() as conn:
                try:
                    cur = conn.execute(
                        """
                        INSERT INTO candidates
                            (session_id, item_hash, type, status, payload, score, created_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            session_id, item_hash, type_, status,
                            json.dumps(payload, ensure_ascii=False),
                            score, time.time(),
                        ),
                    )
                    return int(cur.lastrowid)
                except sqlite3.IntegrityError:
                    # A5: 同 hash 已存在，幂等返回 None
                    return None
        except sqlite3.Error as e:
            raise MetaDBError(f"add_candidate 失败: {e}", cause=e)

    def update_candidate_status(self, candidate_id: int, status: str) -> None:
        """L6: 推进 status（pending → accepted/rejected/written）"""
        try:
            with self.transaction() as conn:
                conn.execute(
                    "UPDATE candidates SET status = ? WHERE id = ?",
                    (status, candidate_id),
                )
        except sqlite3.Error as e:
            raise MetaDBError(f"update_candidate_status 失败: {e}", cause=e)

    def list_candidates(
        self,
        session_id: str,
        status: Optional[str] = None,
        type_: Optional[str] = None,
    ) -> list[dict]:
        """列出 candidates（支持 status / type 过滤）"""
        import json
        sql = "SELECT id, session_id, item_hash, type, status, payload, score, created_at FROM candidates WHERE session_id = ?"
        params: list[Any] = [session_id]
        if status is not None:
            sql += " AND status = ?"
            params.append(status)
        if type_ is not None:
            sql += " AND type = ?"
            params.append(type_)
        sql += " ORDER BY created_at"
        try:
            rows = self._conn().execute(sql, params).fetchall()
            return [
                {
                    "id": r["id"],
                    "session_id": r["session_id"],
                    "item_hash": r["item_hash"],
                    "type": r["type"],
                    "status": r["status"],
                    "payload": json.loads(r["payload"]),
                    "score": r["score"],
                    "created_at": r["created_at"],
                }
                for r in rows
            ]
        except sqlite3.Error as e:
            raise MetaDBError(f"list_candidates 失败: {e}", cause=e)

    # ── Candidate decisions（M10 C4.4: review 决策回灌） ────

    def record_candidate_decision(self, cand_key: str, decision: str) -> None:
        """M10 C4.4: 记一条 review 决策（UPSERT，同 key 覆盖）

        Args:
            cand_key: compute_candidate_key(type, body)
            decision: "accepted" 或 "rejected"
        """
        try:
            with self.transaction() as conn:
                conn.execute(
                    """
                    INSERT INTO candidate_decisions (cand_key, decision, decided_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(cand_key) DO UPDATE SET
                        decision = excluded.decision,
                        decided_at = excluded.decided_at
                    """,
                    (cand_key, decision, time.time()),
                )
        except sqlite3.Error as e:
            raise MetaDBError(f"record_candidate_decision 失败: {e}", cause=e)

    def list_decided_candidates(self) -> set[str]:
        """M10 C4.4: 返回所有已审候选 key 集合（accepted + rejected 都算已审）

        返回 set[str] 便于 O(1) 查询；候选数不大，一次性全读可接受。
        """
        try:
            rows = self._conn().execute(
                "SELECT cand_key FROM candidate_decisions"
            ).fetchall()
            return {r["cand_key"] for r in rows}
        except sqlite3.Error as e:
            raise MetaDBError(f"list_decided_candidates 失败: {e}", cause=e)


__all__ = ["MetaDB", "MetaDBError"]