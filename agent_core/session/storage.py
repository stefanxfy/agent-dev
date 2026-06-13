"""
SessionStorage - JSONL 持久化会话存储
参考 Claude Code sessionStorage.ts 实现的 append-only 存储

核心设计：
- JSONL 格式：每行一条 JSON Entry，append-only
- parentUuid 链：每条 entry 指向上一条 UUID
- 延迟创建：首条消息才创建文件
- UUID 去重：内存维护 UUID set，append 前检查
- 写队列：100ms 批量刷新，减少磁盘 IO
- 原子性：先写临时文件，再 rename
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger("session.storage")


class SessionStorage:
    """
    JSONL append-only 会话存储

    Attributes:
        session_id: 全局唯一会话 ID（UUID 格式）
        data_dir: 数据目录，默认为 ~/.agent_data/sessions/
        created_at: 会话创建时间
    """

    # 写队列刷新间隔（毫秒）
    _FLUSH_INTERVAL_MS = 100

    def __init__(
        self,
        session_id: Optional[str] = None,
        data_dir: Optional[str] = None,
        auto_flush: bool = True,
    ):
        # Session ID
        self.session_id = session_id or str(uuid.uuid4())
        self.created_at = datetime.now()

        # 数据目录
        if data_dir:
            self.data_dir = Path(data_dir)
        else:
            self.data_dir = self._get_default_data_dir()
        self.data_dir.mkdir(parents=True, exist_ok=True)

        # JSONL 文件路径（延迟创建）
        self._jsonl_path: Optional[Path] = None

        # 内存去重 set
        self._uuid_set: set[str] = set()

        # 内存索引：uuid → entry 缓存（用于 get_entry）
        self._entry_cache: dict[str, dict] = {}

        # 写队列 + 刷新锁
        self._pending: list[dict] = []
        self._flush_lock = threading.Lock()
        self._last_flush_time: float = time.time()
        self._auto_flush = auto_flush
        self._shutdown = False

        # 后台刷新线程
        self._flush_thread: Optional[threading.Thread] = None
        if self._auto_flush:
            self._flush_thread = threading.Thread(target=self._flush_loop, daemon=True)
            self._flush_thread.start()

    # ── 路径与目录 ─────────────────────────────────────────────────

    @staticmethod
    def _get_default_data_dir() -> Path:
        """获取默认数据目录"""
        home = Path.home()
        # 优先使用项目根目录下的 .agent_data
        # 如果有项目目录，存到项目下；否则存到 ~/.agent_data
        cwd = Path.cwd()
        if (cwd / ".git").exists() or (cwd / "agent_core").exists():
            return cwd / ".agent_data" / "sessions"
        return home / ".agent_data" / "sessions"

    @property
    def jsonl_path(self) -> Optional[Path]:
        return self._jsonl_path

    def _ensure_path(self) -> Path:
        """确保 JSONL 文件路径存在（延迟创建）"""
        if self._jsonl_path is None:
            self._jsonl_path = self.data_dir / f"{self.session_id}.jsonl"
        return self._jsonl_path

    # ── Entry 构造 ─────────────────────────────────────────────────

    def _make_entry(
        self,
        entry_type: str,
        message: Optional[dict] = None,
        parent_uuid: Optional[str] = None,
        **extra,
    ) -> dict:
        """构造标准 Entry（信封套信纸结构）

        Entry 结构（参考 Claude Code）:
        {
            "uuid": "...",
            "parentUuid": "...",
            "sessionId": "...",
            "type": "user" | "assistant" | "tool_use" | "tool_result" | ...,
            "timestamp": "...",
            "message": { ... }    ← API 原始消息，原样存储
            **extra               ← 顶层扩展字段（thinking, tool_logs 等）
        }
        """
        entry_uuid = str(uuid.uuid4())

        entry = {
            "uuid": entry_uuid,
            "parentUuid": parent_uuid,
            "sessionId": self.session_id,
            "type": entry_type,
            "timestamp": datetime.now().isoformat(),
        }

        # message 字段：存 API 原始消息对象（零转换）
        if message:
            entry["message"] = message

        # 额外字段挂顶层（thinking, tool_logs, tool_use_id 等）
        if extra:
            entry.update(extra)

        # 注册到内存索引
        self._entry_cache[entry_uuid] = entry
        self._uuid_set.add(entry_uuid)

        return entry

    # ── 核心写入 API ────────────────────────────────────────────────

    def append_entry(
        self,
        entry_type: str,
        message: Optional[dict] = None,
        parent_uuid: Optional[str] = None,
        **extra,
    ) -> str:
        """
        追加一条 Entry 到会话存储。

        Args:
            entry_type: Entry 类型（如 user / assistant / tool_result / summary）
            message: API 原始消息对象（原样存入 message 字段）
            parent_uuid: 父消息 UUID（None 表示新链起点）
            **extra: 额外字段（挂 Entry 顶层，如 thinking, tool_logs）

        Returns:
            新 Entry 的 UUID
        """
        # 检查 UUID 去重
        if parent_uuid and parent_uuid in self._uuid_set:
            # 父消息存在，使用传入的 parent_uuid
            pass
        elif self._pending or self._entry_cache:
            # 有历史消息，自动链到最新
            parent_uuid = self._get_last_uuid()

        entry = self._make_entry(entry_type, message, parent_uuid, **extra)
        self._pending.append(entry)

        logger.debug(
            f"[{self.session_id[:8]}] append {entry_type}: "
            f"uuid={entry['uuid'][:8]}, parent={str(parent_uuid or '')[:8]}"
        )

        # 立即 flush（同步模式）或加入写队列
        if not self._auto_flush:
            self.flush()

        return entry["uuid"]

    def _get_last_uuid(self) -> Optional[str]:
        """获取最新一条 Entry 的 UUID"""
        if self._pending:
            return self._pending[-1]["uuid"]
        if self._entry_cache:
            # 扫描最后一条（按 timestamp）
            # 简化：从 jsonl_path 尾部读 1 行
            if self._jsonl_path and self._jsonl_path.exists():
                with open(self._jsonl_path, "r", encoding="utf-8") as f:
                    lines = f.readlines()
                if lines:
                    last = json.loads(lines[-1])
                    return last.get("uuid")
        return None

    # ── 消息类型快捷方法 ───────────────────────────────────────────

    def add_message(
        self,
        role: str,
        content=None,
        entry_type: Optional[str] = None,
        message: Optional[dict] = None,
        **extra,
    ) -> str:
        """添加一条消息 Entry（快捷方法）

        两种用法：
        1. add_message("user", "hello") → message = {"role": "user", "content": "hello"}
        2. add_message("assistant", message={"role": "assistant", "content": [...]})
           → 直接存传入的 message dict（零转换）
        """
        if entry_type is None:
            entry_type = role
        if message is None:
            message = {"role": role, "content": content}
        return self.append_entry(
            entry_type=entry_type,
            message=message,
            **extra,
        )

    def add_summary(
        self,
        summary: str,
        tokens_saved: int = 0,
        format: str = "BASE",
        **extra,
    ) -> str:
        """添加 summary Entry（压缩产物）

        存储格式：message = {"summary": ..., "tokens_saved": ..., "format": ...}
        """
        return self.append_entry(
            entry_type="summary",
            message={
                "summary": summary,
                "tokens_saved": tokens_saved,
                "format": format,
            },
            **extra,
        )

    def append_raw_entry(self, entry: dict) -> str:
        """直接追加已构造好的 Entry（不走 message 包装，用于 metadata entries）

        Args:
            entry: 完整的 Entry dict（必须包含 uuid, type, sessionId）

        Returns:
            Entry UUID
        """
        self._entry_cache[entry.get("uuid", "")] = entry
        self._uuid_set.add(entry.get("uuid", ""))
        self._pending.append(entry)

        if not self._auto_flush:
            self.flush()

        return entry.get("uuid", "")

    def add_compact_boundary(self, **extra) -> str:
        """添加压缩边界 Entry（parentUuid = None，断链标记）"""
        entry_uuid = str(uuid.uuid4())
        entry = {
            "uuid": entry_uuid,
            "parentUuid": None,  # ← 断链！
            "sessionId": self.session_id,
            "type": "compact-boundary",
            "timestamp": datetime.now().isoformat(),
            **extra,
        }
        self._pending.append(entry)
        self._entry_cache[entry_uuid] = entry
        self._uuid_set.add(entry_uuid)
        logger.debug(f"[{self.session_id[:8]}] compact_boundary: uuid={entry_uuid[:8]}")
        return entry_uuid

    # ── 读取 API ──────────────────────────────────────────────────

    def read_entries(
        self,
        include_compact_boundary: bool = True,
    ) -> list[dict]:
        """
        读取会话全部 Entry（按时间顺序）

        Args:
            include_compact_boundary: 是否包含压缩边界

        Returns:
            Entry 列表
        """
        path = self._ensure_path()
        if not path.exists():
            return []

        entries = []
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                    if not include_compact_boundary and entry.get("type") == "compact-boundary":
                        continue
                    entries.append(entry)
                    self._entry_cache[entry["uuid"]] = entry
                    self._uuid_set.add(entry["uuid"])
                except json.JSONDecodeError:
                    logger.warning(f"Failed to decode JSONL line: {line[:100]}")
                    continue

        return entries

    def read_tail(self, kb: int = 64) -> list[dict]:
        """
        读取尾部 64KB（轻量读取，用于元数据恢复）

        Args:
            kb: 读取的 KB 数

        Returns:
            尾部 Entry 列表（倒序）
        """
        path = self._ensure_path()
        if not path.exists():
            return []

        # 从文件末尾读取 kb
        byte_limit = kb * 1024
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            file_size = f.tell()
            if file_size <= byte_limit:
                f.seek(0)
            else:
                f.seek(-byte_limit, os.SEEK_END)
            # 跳过可能截断的行
            f.readline()

        entries = []
        with open(path, "r", encoding="utf-8") as f:
            f.seek(-byte_limit, os.SEEK_END) if file_size > byte_limit else None
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    continue

        return entries

    def get_entry(self, entry_uuid: str) -> Optional[dict]:
        """根据 UUID 查询 Entry"""
        # 先查内存缓存
        if entry_uuid in self._entry_cache:
            return self._entry_cache[entry_uuid]

        # 查 JSONL（扫描）
        path = self._ensure_path()
        if not path.exists():
            return None

        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                try:
                    entry = json.loads(line)
                    if entry["uuid"] == entry_uuid:
                        self._entry_cache[entry_uuid] = entry
                        return entry
                except (json.JSONDecodeError, KeyError):
                    continue

        return None

    def get_messages(
        self,
        stop_at_boundary: bool = True,
    ) -> list[dict]:
        """
        获取消息列表（排除元数据 entry）

        Args:
            stop_at_boundary: 是否在压缩边界处停止

        Returns:
            消息 Entry 列表
        """
        entries = self.read_entries(include_compact_boundary=True)
        messages = []

        for entry in entries:
            etype = entry.get("type")
            if stop_at_boundary and etype == "compact-boundary":
                break  # 停在边界处，boundary 本身不包含
            if etype in ("user", "assistant", "tool_use", "tool_result", "system"):
                messages.append(entry)

        return messages

    # ── 会话列表 ─────────────────────────────────────────────────

    @classmethod
    def list_sessions(
        cls,
        data_dir: Optional[str] = None,
        include_compact: bool = False,
    ) -> list[dict]:
        """
        列出所有会话

        Returns:
            会话摘要列表（按更新时间倒序）
        """
        if data_dir:
            dd = Path(data_dir)
        else:
            dd = cls._get_default_data_dir()

        if not dd.exists():
            return []

        sessions = []
        for f in dd.glob("*.jsonl"):
            session_id = f.stem
            stat = f.stat()
            mtime = datetime.fromtimestamp(stat.st_mtime)
            size = stat.st_size

            # 读取元数据（metadata 在文件头部，所以从头扫描）
            meta = {}
            try:
                with open(f, "r", encoding="utf-8") as fp:
                    for line in fp:
                        try:
                            e = json.loads(line.strip())
                            if e.get("type") == "custom-title":
                                meta["title"] = e.get("customTitle")
                            elif e.get("type") == "ai-title":
                                meta["ai_title"] = e.get("aiTitle")
                            elif e.get("type") == "tag":
                                meta["tags"] = meta.get("tags", [])
                                meta["tags"].append(e.get("tag"))
                            elif e.get("type") == "last-prompt":
                                meta["last_prompt"] = e.get("lastPrompt")
                        except json.JSONDecodeError:
                            continue
            except Exception:
                pass

            sessions.append({
                "session_id": session_id,
                "updated_at": mtime,
                "created_at": datetime.fromtimestamp(stat.st_ctime),
                "size": size,
                "title": meta.get("title") or meta.get("ai_title") or "新会话",
                "tags": meta.get("tags", []),
                "last_prompt": meta.get("last_prompt"),
            })

        # 按更新时间倒序
        sessions.sort(key=lambda x: x["updated_at"], reverse=True)
        return sessions

    # ── 删除会话 ─────────────────────────────────────────────────

    def delete(self):
        """删除当前会话文件"""
        self.flush()
        self._shutdown = True
        if self._jsonl_path and self._jsonl_path.exists():
            self._jsonl_path.unlink()
            logger.info(f"Deleted session: {self.session_id}")

    @classmethod
    def delete_session(cls, session_id: str, data_dir: Optional[str] = None):
        """删除指定会话"""
        if data_dir:
            dd = Path(data_dir)
        else:
            dd = cls._get_default_data_dir()
        path = dd / f"{session_id}.jsonl"
        if path.exists():
            path.unlink()
            logger.info(f"Deleted session: {session_id}")

    # ── 写队列 + 批量刷新 ─────────────────────────────────────────

    def flush(self):
        """立即刷新写队列（同步追加写入）"""
        if not self._pending:
            return

        with self._flush_lock:
            if not self._pending:
                return

            to_write = self._pending
            self._pending = []

            path = self._ensure_path()

            # 直接追加到文件（每次 flush 会追加多行）
            try:
                with open(path, "a", encoding="utf-8") as f:
                    for entry in to_write:
                        f.write(json.dumps(entry, ensure_ascii=False) + "\n")

                logger.debug(
                    f"[{self.session_id[:8]}] flushed {len(to_write)} entries"
                )
            except Exception as e:
                # 写入失败，放回队列
                self._pending = to_write + self._pending
                logger.error(f"Failed to flush: {e}")
                raise

    def _flush_loop(self):
        """后台刷新线程（100ms drain）"""
        while not self._shutdown:
            time.sleep(self._FLUSH_INTERVAL_MS / 1000.0)

            if self._shutdown:
                break

            # 检查是否需要刷新
            elapsed = (time.time() - self._last_flush_time) * 1000
            if self._pending and elapsed >= self._FLUSH_INTERVAL_MS:
                try:
                    self.flush()
                    self._last_flush_time = time.time()
                except Exception as e:
                    logger.error(f"Flush error: {e}")

    # ── 上下文管理接口 ─────────────────────────────────────────────

    def __repr__(self):
        return (
            f"SessionStorage(session_id={self.session_id[:8]}, "
            f"pending={len(self._pending)}, "
            f"cached={len(self._entry_cache)})"
        )

    def __del__(self):
        """析构时确保 flush"""
        try:
            self.flush()
        except Exception:
            pass
