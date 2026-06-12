"""
SessionCleanup - 会话清理与归档
参考 Claude Code session cleanup 逻辑

功能：
- TTL 清理：自动删除超过指定天数的会话
- 归档：压缩旧会话到 .gz 文件
- 磁盘使用统计
- 批量清理接口
"""

from __future__ import annotations

import gzip
import logging
import shutil
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from .storage import SessionStorage

logger = logging.getLogger("session.cleanup")


class SessionCleanup:
    """
    会话清理与归档器

    功能：
    - 按 TTL 删除旧会话
    - 归档会话到 gzip 压缩文件
    - 统计磁盘使用
    - 清理孤立文件（无引用的 worktree 状态等）
    """

    def __init__(self, data_dir: Optional[str] = None):
        if data_dir:
            self.data_dir = Path(data_dir)
        else:
            self.data_dir = SessionStorage._get_default_data_dir()

        # 默认 TTL
        self.default_ttl_days = 30
        self.archive_dir = self.data_dir / "archives"

    # ── TTL 清理 ─────────────────────────────────────────────────

    def cleanup_by_ttl(
        self,
        ttl_days: Optional[int] = None,
        dry_run: bool = False,
    ) -> list[str]:
        """
        删除超过 TTL 的会话

        Args:
            ttl_days: TTL 天数（默认 30 天）
            dry_run: True 则只返回要删除的列表，不实际删除

        Returns:
            被删除/将删除的 session_id 列表
        """
        ttl_days = ttl_days or self.default_ttl_days
        cutoff = datetime.now() - timedelta(days=ttl_days)
        deleted = []

        if not self.data_dir.exists():
            return deleted

        for f in self.data_dir.glob("*.jsonl"):
            # 跳过归档目录
            if "archives" in str(f):
                continue

            try:
                mtime = datetime.fromtimestamp(f.stat().st_mtime)
                if mtime < cutoff:
                    session_id = f.stem
                    if dry_run:
                        logger.info(f"[DRY RUN] Would delete: {session_id}")
                    else:
                        f.unlink()
                        logger.info(f"Deleted old session: {session_id}")
                    deleted.append(session_id)
            except Exception as e:
                logger.warning(f"Failed to process {f}: {e}")

        logger.info(f"TTL cleanup: deleted {len(deleted)} sessions (older than {ttl_days} days)")
        return deleted

    def cleanup_by_size(
        self,
        max_sessions: int = 100,
        keep_recent: int = 10,
        dry_run: bool = False,
    ) -> list[str]:
        """
        按会话数量清理（保留最近的 N 个）

        Args:
            max_sessions: 最多保留的会话数
            keep_recent: 必须保留的最近会话数（不会被删除）
            dry_run: 只打印不删除

        Returns:
            被删除的 session_id 列表
        """
        sessions = SessionStorage.list_sessions(str(self.data_dir))
        if len(sessions) <= max_sessions:
            logger.info(f"Session count {len(sessions)} <= limit {max_sessions}, no cleanup needed")
            return []

        # 保留最近的 keep_recent 个
        keep_ids = {s["session_id"] for s in sessions[:keep_recent]}
        to_delete = sessions[max_sessions:]
        deleted = []

        for s in to_delete:
            if s["session_id"] in keep_ids:
                continue
            if dry_run:
                logger.info(f"[DRY RUN] Would delete: {s['session_id']}")
            else:
                SessionStorage.delete_session(s["session_id"], str(self.data_dir))
                logger.info(f"Deleted by size: {s['session_id']}")
            deleted.append(s["session_id"])

        logger.info(f"Size cleanup: deleted {len(deleted)} sessions")
        return deleted

    def cleanup_empty_sessions(self, dry_run: bool = False) -> list[str]:
        """
        删除空会话（小于 100 字节的会话文件）

        Returns:
            被删除的空 session_id 列表
        """
        min_size = 100
        deleted = []

        if not self.data_dir.exists():
            return deleted

        for f in self.data_dir.glob("*.jsonl"):
            try:
                if f.stat().st_size < min_size:
                    session_id = f.stem
                    if dry_run:
                        logger.info(f"[DRY RUN] Would delete empty: {session_id}")
                    else:
                        f.unlink()
                        logger.info(f"Deleted empty session: {session_id}")
                    deleted.append(session_id)
            except Exception as e:
                logger.warning(f"Failed to process {f}: {e}")

        return deleted

    # ── 归档 ─────────────────────────────────────────────────────

    def archive_session(
        self,
        session_id: str,
        compress: bool = True,
    ) -> Optional[Path]:
        """
        归档指定会话

        Args:
            session_id: 会话 ID
            compress: 是否 gzip 压缩

        Returns:
            归档文件路径，失败返回 None
        """
        src = self.data_dir / f"{session_id}.jsonl"
        if not src.exists():
            logger.warning(f"Session not found for archive: {session_id}")
            return None

        self.archive_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        ext = ".jsonl.gz" if compress else ".jsonl"
        dst = self.archive_dir / f"{session_id}_{timestamp}{ext}"

        try:
            if compress:
                with open(src, "rb") as fi, gzip.open(dst, "wb") as fo:
                    shutil.copyfileobj(fi, fo)
            else:
                shutil.copy2(src, dst)

            # 删除原文件
            src.unlink()
            logger.info(f"Archived session: {session_id} -> {dst}")
            return dst

        except Exception as e:
            logger.error(f"Failed to archive {session_id}: {e}")
            return None

    def archive_old_sessions(
        self,
        older_than_days: int = 7,
        compress: bool = True,
        dry_run: bool = False,
    ) -> list[str]:
        """
        归档超过指定天数的会话

        Args:
            older_than_days: 超过多少天归档
            compress: 是否压缩
            dry_run: 只打印不执行

        Returns:
            归档的 session_id 列表
        """
        cutoff = datetime.now() - timedelta(days=older_than_days)
        archived = []

        sessions = SessionStorage.list_sessions(str(self.data_dir))
        for s in sessions:
            if s["updated_at"] < cutoff:
                if dry_run:
                    logger.info(f"[DRY RUN] Would archive: {s['session_id']}")
                else:
                    result = self.archive_session(s["session_id"], compress=compress)
                    if result:
                        archived.append(s["session_id"])

        return archived

    def list_archives(self) -> list[dict]:
        """列出所有归档文件"""
        if not self.archive_dir.exists():
            return []

        archives = []
        for f in self.archive_dir.glob("*.jsonl*"):
            stat = f.stat()
            archives.append({
                "path": str(f),
                "session_id": f.stem.split("_")[0],
                "archived_at": datetime.fromtimestamp(stat.st_mtime).isoformat(),
                "size": stat.st_size,
                "compressed": f.suffix == ".gz",
            })
        return sorted(archives, key=lambda x: x["archived_at"], reverse=True)

    def restore_from_archive(self, archive_path: Path) -> Optional[str]:
        """
        从归档恢复会话

        Args:
            archive_path: 归档文件路径

        Returns:
            恢复后的 session_id
        """
        import uuid

        if not archive_path.exists():
            logger.error(f"Archive not found: {archive_path}")
            return None

        # 生成新的 session_id（避免与现有冲突）
        new_session_id = str(uuid.uuid4())
        dst = self.data_dir / f"{new_session_id}.jsonl"

        try:
            if archive_path.suffix == ".gz":
                with gzip.open(archive_path, "rb") as fi, open(dst, "wb") as fo:
                    shutil.copyfileobj(fi, fo)
            else:
                shutil.copy2(archive_path, dst)

            logger.info(f"Restored from archive: {archive_path.name} -> {new_session_id}")
            return new_session_id

        except Exception as e:
            logger.error(f"Failed to restore from {archive_path}: {e}")
            return None

    # ── 磁盘统计 ─────────────────────────────────────────────────

    def disk_usage(self) -> dict:
        """
        返回磁盘使用统计

        Returns:
            {
                "session_count": N,
                "total_bytes": N,
                "archives_count": N,
                "archives_bytes": N,
                "largest_session": (session_id, bytes),
            }
        """
        sessions = SessionStorage.list_sessions(str(self.data_dir))

        total_bytes = sum(s["size"] for s in sessions)
        archives = self.list_archives()
        archives_bytes = sum(a["size"] for a in archives)

        largest = max(sessions, key=lambda s: s["size"]) if sessions else None

        return {
            "session_count": len(sessions),
            "total_bytes": total_bytes,
            "total_mb": round(total_bytes / 1024 / 1024, 2),
            "archives_count": len(archives),
            "archives_bytes": archives_bytes,
            "largest_session": (largest["session_id"], largest["size"])
                if largest else (None, 0),
        }

    # ── 一键清理 ─────────────────────────────────────────────────

    def full_cleanup(
        self,
        ttl_days: int = 30,
        max_sessions: int = 100,
        archive_before_delete: bool = True,
        compress: bool = True,
        dry_run: bool = False,
    ) -> dict:
        """
        执行完整清理流程

        顺序：
        1. 归档超过 7 天的会话（可选）
        2. 删除超过 TTL 的会话
        3. 按数量清理（保留最近的 max_sessions）
        4. 删除空会话

        Returns:
            清理报告
        """
        report = {
            "archived": [],
            "deleted_by_ttl": [],
            "deleted_by_size": [],
            "deleted_empty": [],
            "disk_usage_before": self.disk_usage(),
            "errors": [],
        }

        try:
            # Step 1: 归档
            archived = self.archive_old_sessions(older_than_days=7, compress=compress, dry_run=dry_run)
            report["archived"] = archived

            # Step 2: TTL 清理
            deleted_ttl = self.cleanup_by_ttl(ttl_days=ttl_days, dry_run=dry_run)
            report["deleted_by_ttl"] = deleted_ttl

            # Step 3: 数量清理
            deleted_size = self.cleanup_by_size(max_sessions=max_sessions, dry_run=dry_run)
            report["deleted_by_size"] = deleted_size

            # Step 4: 空会话
            deleted_empty = self.cleanup_empty_sessions(dry_run=dry_run)
            report["deleted_empty"] = deleted_empty

        except Exception as e:
            logger.error(f"Cleanup error: {e}")
            report["errors"].append(str(e))

        report["disk_usage_after"] = self.disk_usage()
        logger.info(f"Full cleanup done: {report}")
        return report
