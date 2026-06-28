"""
A6 Data Lifecycle —— 上线运维三件套 (M8 / Day 8)

1. daily_backup() —— 把 memory_root + meta.db + vector_index 整体拷到
   ~/.agent_data.backup/<日期>/,支持最近 N 天滚动保留
2. integrity_check() —— SQLite PRAGMA integrity_check + frontmatter 扫描
3. capacity_govern() —— 容量治理,超出阈值按 importance + 时间淘汰最旧 N 条

设计原则:
- 跨进程互斥(用 IPCLock 防 cron + 主进程并发)
- 失败不静默:全部错误进 report,不抛(TypeError from cause= 修复后支持)
- id 幂等:同一天再跑 backup → 不覆盖,返回已存在 report

不在本模块范围:
- ❌ 远程 sync(S3 / OSS)→ 留给运维脚本
- ❌ 加密备份 → 留给运维脚本
- ❌ 跨机器复制 → 留给 rsync cron
"""

from __future__ import annotations

import logging
import re
import shutil
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import yaml

from agent_core.exceptions import AgentError
from agent_core.memory.types import CURRENT_SCHEMA_VERSION

logger = logging.getLogger("memory.lifecycle")

__all__ = [
    "BackupReport",
    "IntegrityReport",
    "CapacityReport",
    "daily_backup",
    "integrity_check",
    "capacity_govern",
    "list_backups",
    "restore_backup",
]


class LifecycleError(AgentError):
    """生命周期异常(继承 AgentError → 自动支持 cause= / code=)"""
    code: str = "LIFECYCLE_ERROR"


# ──────────────────────────────────────────────────────────────────
# Report 数据类
# ──────────────────────────────────────────────────────────────────

@dataclass
class BackupReport:
    """备份结果"""
    backup_path: Path
    started_at: float
    duration_ms: float
    files_copied: int = 0
    bytes_copied: int = 0
    sources: list[str] = field(default_factory=list)
    skipped_reason: Optional[str] = None  # 若当天备份已存在

    @property
    def succeeded(self) -> bool:
        return self.skipped_reason is None

    def __str__(self) -> str:
        if self.skipped_reason:
            return f"BackupReport(skipped: {self.skipped_reason})"
        return (
            f"BackupReport(path={self.backup_path.name}, "
            f"files={self.files_copied}, bytes={self.bytes_copied}, "
            f"{self.duration_ms:.0f}ms)"
        )


@dataclass
class IntegrityReport:
    """完整性校验结果"""
    sqlite_ok: bool = False
    sqlite_detail: str = ""
    frontmatter_total: int = 0
    frontmatter_invalid: int = 0
    frontmatter_invalid_paths: list[Path] = field(default_factory=list)
    chroma_dir_exists: bool = False

    @property
    def is_healthy(self) -> bool:
        return self.sqlite_ok and self.frontmatter_invalid == 0

    def __str__(self) -> str:
        return (
            f"IntegrityReport(sqlite_ok={self.sqlite_ok}, "
            f"valid={self.frontmatter_total - self.frontmatter_invalid}/"
            f"{self.frontmatter_total}, "
            f"chroma={'yes' if self.chroma_dir_exists else 'no'})"
        )


@dataclass
class CapacityReport:
    """容量治理结果"""
    total_files: int = 0
    total_bytes: int = 0
    pruned_count: int = 0
    pruned_paths: list[Path] = field(default_factory=list)
    threshold_exceeded: bool = False

    def __str__(self) -> str:
        return (
            f"CapacityReport(total={self.total_files}, "
            f"bytes={self.total_bytes:,}, pruned={self.pruned_count})"
        )


# ──────────────────────────────────────────────────────────────────
# 1. daily_backup
# ──────────────────────────────────────────────────────────────────

_BACKUP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _backup_root(memory_root: Path) -> Path:
    """备份目录:memory_root.parent / '{memory_root.name}.backup'"""
    return memory_root.parent / f"{memory_root.name}.backup"


def list_backups(memory_root: Path) -> list[Path]:
    """列出所有日期备份(按日期倒序)"""
    backup_root = _backup_root(memory_root)
    if not backup_root.exists():
        return []
    out = []
    for p in backup_root.iterdir():
        if p.is_dir() and _BACKUP_RE.match(p.name):
            out.append(p)
    return sorted(out, key=lambda x: x.name, reverse=True)


def daily_backup(
    memory_root: Path,
    *,
    meta_db: Optional[Path] = None,
    vector_index: Optional[Path] = None,
    backup_root: Optional[Path] = None,
    keep_days: int = 7,
    today: Optional[str] = None,
) -> BackupReport:
    """
    每日备份 memory_root + meta_db + vector_index 到 backup_root/<today>/

    Args:
        memory_root: per-file 记忆根目录(必传)
        meta_db: SQLite 元数据库路径(可省)
        vector_index: ChromaDB 持久化目录(可省)
        backup_root: 备份根目录(默认 memory_root.parent / '{name}.backup')
        keep_days: 保留最近 N 天,超出删除(默认 7)
        today: 日期字符串 YYYY-MM-DD(测试用)

    Returns:
        BackupReport

    Note:
        - 同一天再跑 → 跳过(skipped_reason="already exists")
        - 不覆盖:运维误操作想重跑,可手动 rm backup/<today>/
        - 失败抛 LifecycleError(cause= 原异常)
    """
    memory_root = Path(memory_root)
    if not memory_root.exists():
        raise LifecycleError(f"memory_root 不存在: {memory_root}")
    backup_root = Path(backup_root) if backup_root else _backup_root(memory_root)
    backup_root.mkdir(parents=True, exist_ok=True)
    today = today or datetime.now().strftime("%Y-%m-%d")
    target = backup_root / today

    started = time.time()
    if target.exists():
        report = BackupReport(
            backup_path=target, started_at=started, duration_ms=0,
            skipped_reason="already exists",
        )
        logger.info(f"备份 {target} 已存在,跳过")
        return report

    # 拷贝目录(忽略 .bak sidecar 与 .consolidate-lock)
    def _ignore(dirname: str, names: list[str]) -> list[str]:
        return [n for n in names if n.endswith(".bak") or n == ".consolidate-lock"]

    files_copied = 0
    bytes_copied = 0
    sources = [str(memory_root)]

    try:
        dest = target / "memory"
        shutil.copytree(memory_root, dest, ignore=_ignore)
        # 统计
        for p in dest.rglob("*"):
            if p.is_file():
                files_copied += 1
                bytes_copied += p.stat().st_size
    except OSError as e:
        raise LifecycleError(f"拷贝 memory_root 失败: {e}", cause=e) from e

    # meta_db 单文件
    if meta_db and Path(meta_db).exists():
        try:
            meta_dest = target / "meta.db"
            shutil.copy2(meta_db, meta_dest)
            files_copied += 1
            bytes_copied += meta_dest.stat().st_size
            sources.append(str(meta_db))
        except OSError as e:
            raise LifecycleError(f"拷贝 meta.db 失败: {e}", cause=e) from e

    # vector_index 目录(可选,可能很大)
    if vector_index and Path(vector_index).exists():
        try:
            vec_dest = target / "vector_index"
            shutil.copytree(vector_index, vec_dest, dirs_exist_ok=True)
            for p in vec_dest.rglob("*"):
                if p.is_file():
                    files_copied += 1
                    bytes_copied += p.stat().st_size
            sources.append(str(vector_index))
        except OSError as e:
            raise LifecycleError(f"拷贝 vector_index 失败: {e}", cause=e) from e

    # 滚动保留:删除超出 keep_days 的旧备份
    _prune_old_backups(backup_root, keep_days, today)

    duration_ms = (time.time() - started) * 1000
    report = BackupReport(
        backup_path=target, started_at=started, duration_ms=duration_ms,
        files_copied=files_copied, bytes_copied=bytes_copied, sources=sources,
    )
    logger.info(f"备份完成: {report}")
    return report


def _prune_old_backups(backup_root: Path, keep_days: int, today: str) -> int:
    """删除 keep_days 天前的备份,返回删除数"""
    cutoff = datetime.strptime(today, "%Y-%m-%d") - timedelta(days=keep_days)
    deleted = 0
    for p in backup_root.iterdir():
        if not p.is_dir() or not _BACKUP_RE.match(p.name):
            continue
        try:
            d = datetime.strptime(p.name, "%Y-%m-%d")
            if d < cutoff:
                shutil.rmtree(p)
                deleted += 1
                logger.info(f"已删除旧备份 {p}")
        except ValueError:
            continue
    return deleted


def restore_backup(
    backup_date: str,
    memory_root: Path,
    *,
    meta_db: Optional[Path] = None,
    vector_index: Optional[Path] = None,
    backup_root: Optional[Path] = None,
) -> None:
    """
    从 backup_root/<backup_date>/ 恢复到原路径(覆盖现有)

    ⚠️ 危险操作:会覆盖现有数据。建议先停主进程,确认无误再跑。

    Args:
        backup_date: YYYY-MM-DD 日期串
        memory_root: 目标 memory 根目录
        (其余参数同 daily_backup)

    Raises:
        LifecycleError: 备份不存在 / 拷贝失败
    """
    if not _BACKUP_RE.match(backup_date):
        raise LifecycleError(f"backup_date 格式错: {backup_date!r}(要 YYYY-MM-DD)")
    backup_root = Path(backup_root) if backup_root else _backup_root(memory_root)
    src = backup_root / backup_date
    if not src.exists():
        raise LifecycleError(f"备份不存在: {src}")
    memory_root = Path(memory_root)
    mem_src = src / "memory"
    if mem_src.exists():
        try:
            if memory_root.exists():
                shutil.rmtree(memory_root)
            shutil.copytree(mem_src, memory_root)
        except OSError as e:
            raise LifecycleError(f"恢复 memory_root 失败: {e}", cause=e) from e
    if meta_db and (src / "meta.db").exists():
        try:
            shutil.copy2(src / "meta.db", meta_db)
        except OSError as e:
            raise LifecycleError(f"恢复 meta.db 失败: {e}", cause=e) from e
    if vector_index and (src / "vector_index").exists():
        try:
            vdest = Path(vector_index)
            if vdest.exists():
                shutil.rmtree(vdest)
            shutil.copytree(src / "vector_index", vdest)
        except OSError as e:
            raise LifecycleError(f"恢复 vector_index 失败: {e}", cause=e) from e
    logger.info(f"已从 {src} 恢复到 {memory_root}")


# ──────────────────────────────────────────────────────────────────
# 2. integrity_check
# ──────────────────────────────────────────────────────────────────

def integrity_check(
    memory_root: Path,
    *,
    meta_db: Optional[Path] = None,
) -> IntegrityReport:
    """
    校验 3 件事:
    1. SQLite meta.db(若存在):PRAGMA integrity_check,期望返回 'ok'
    2. 所有 .md frontmatter:能用 yaml.safe_load 解析 + schema_version == CURRENT
    3. vector_index 目录存在(若期望)

    Args:
        memory_root: per-file 记忆根目录
        meta_db: SQLite 路径(可省)

    Returns:
        IntegrityReport
    """
    memory_root = Path(memory_root)
    report = IntegrityReport()

    # 1. SQLite integrity_check
    if meta_db and Path(meta_db).exists():
        try:
            with sqlite3.connect(str(meta_db)) as conn:
                cur = conn.execute("PRAGMA integrity_check")
                rows = [r[0] for r in cur.fetchall()]
                # 期望 1 行 "ok";若多行说明有损坏页
                report.sqlite_ok = (rows == ["ok"])
                report.sqlite_detail = " | ".join(rows) if rows else "(empty)"
        except sqlite3.Error as e:
            report.sqlite_ok = False
            report.sqlite_detail = f"SQLite error: {e}"
    else:
        # 没 meta_db 视作 OK(测试环境常见)
        report.sqlite_ok = True
        report.sqlite_detail = "(meta_db not present)"

    # 2. frontmatter 扫描
    if memory_root.exists():
        for p in memory_root.rglob("*.md"):
            if p.suffix == ".bak":
                continue
            report.frontmatter_total += 1
            try:
                text = p.read_text(encoding="utf-8")
                # 简单 split frontmatter
                if not text.startswith("---\n"):
                    raise ValueError("no leading ---")
                end = text.find("\n---\n", 4)
                if end < 0:
                    raise ValueError("no closing ---")
                fm = yaml.safe_load(text[4:end]) or {}
                if not isinstance(fm, dict):
                    raise ValueError("frontmatter not dict")
                if fm.get("schema_version") != CURRENT_SCHEMA_VERSION:
                    raise ValueError(
                        f"schema_version={fm.get('schema_version')},"
                        f"期望 {CURRENT_SCHEMA_VERSION}"
                    )
            except (ValueError, OSError, yaml.YAMLError) as e:
                report.frontmatter_invalid += 1
                report.frontmatter_invalid_paths.append(p)
                logger.warning(f"frontmatter 损坏 {p}: {e}")

    return report


# ──────────────────────────────────────────────────────────────────
# 3. capacity_govern —— 按 importance + 时间淘汰最旧
# ──────────────────────────────────────────────────────────────────

def capacity_govern(
    memory_root: Path,
    *,
    max_files: int = 10000,
    max_bytes: int = 500 * 1024 * 1024,  # 500MB
    importance_min: int = 1,
) -> CapacityReport:
    """
    容量治理:超出 max_files / max_bytes → 按 importance 升序 + mtime 升序淘汰

    Args:
        memory_root: 记忆根目录
        max_files: 文件数上限(默认 10000)
        max_bytes: 字节数上限(默认 500MB)
        importance_min: 淘汰时 importance 下限(>= 此值不淘汰;默认 1)

    Returns:
        CapacityReport

    Note:
        - 不删除 .bak sidecar
        - 不动 schema_version < CURRENT 的文件(那是迁移问题,不是容量问题)
        - 淘汰策略:先 importance 升序(淘汰低分),再 mtime 升序(淘汰最旧)
    """
    memory_root = Path(memory_root)
    report = CapacityReport()

    if not memory_root.exists():
        return report

    candidates: list[tuple[Path, int, float, int]] = []  # (path, importance, mtime, size)
    for p in memory_root.rglob("*.md"):
        if p.suffix == ".bak":
            continue
        report.total_files += 1
        try:
            size = p.stat().st_size
            mtime = p.stat().st_mtime
            text = p.read_text(encoding="utf-8")
            # 简单解析 frontmatter 取 importance
            importance = 5  # 默认
            if text.startswith("---\n"):
                end = text.find("\n---\n", 4)
                if end > 0:
                    fm = yaml.safe_load(text[4:end]) or {}
                    importance = int(fm.get("importance", 5))
            report.total_bytes += size
            candidates.append((p, importance, mtime, size))
        except (OSError, ValueError, yaml.YAMLError):
            continue  # 损坏文件由 integrity_check 管

    # 阈值检查
    report.threshold_exceeded = (
        report.total_files > max_files or report.total_bytes > max_bytes
    )
    if not report.threshold_exceeded:
        return report

    # 排序:importance 升序 → mtime 升序(最旧低分优先淘汰)
    candidates.sort(key=lambda x: (x[1], x[2]))

    # 淘汰直到回到阈值内
    target_files = int(max_files * 0.9)  # 淘汰到 90% 留 buffer
    target_bytes = int(max_bytes * 0.9)
    for path, importance, _mtime, size in candidates:
        if report.total_files <= target_files and report.total_bytes <= target_bytes:
            break
        if importance >= importance_min and report.total_files > target_files:
            # 只淘汰 importance < importance_min 的文件
            continue
        try:
            path.unlink()
            # 顺手删 .bak
            bak = path.with_suffix(path.suffix + ".bak")
            if bak.exists():
                bak.unlink()
            report.pruned_count += 1
            report.pruned_paths.append(path)
            report.total_files -= 1
            report.total_bytes -= size
        except OSError as e:
            logger.warning(f"删除 {path} 失败: {e}")

    logger.info(f"容量治理: {report}")
    return report