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


__all__ = ["MetaDB", "MetaDBError"]