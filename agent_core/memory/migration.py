"""
Schema 迁移 —— v0/v1 旧记忆文件 → CURRENT_SCHEMA_VERSION (M7 / Day 7)

设计要点：
1. 懒迁移:MemoryStore.read() 在解析 frontmatter 时,若 schema_version 过旧,
   自动调 migrate_file(),原文件保留 .bak sidecar
2. 批量迁移:migrate_all(root) 一次扫所有 .md,返回迁移数
3. MigrationRegistry:register(from_v, fn) 注册转换函数,migrate(from_v, data)
   链式调用到 CURRENT_SCHEMA_VERSION
4. 回滚:sidecar .bak 文件保留,出错时可手动 cp 回原路径

不在本模块范围:
- ❌ backup/cron(A7)→ M8
- ❌ 远程 sync → 暂时只支持本地 fs
- ❌ 跨版本同时迁移(只支持 from_v < CURRENT 的链式 +1)

Public API:
- `MigrationRegistry.register(from_v, fn)` —— 注册转换函数
- `MigrationRegistry.migrate(from_v, data, body="") -> (fm, body)` —— 链式迁移(M11 支持 body)
- `MigrationRegistry.migrate_fm_only(from_v, data) -> fm` —— 仅迁移 frontmatter
- `migrate_file(path) -> dict` —— 单文件懒迁移(返回 migrated bool)
- `migrate_all(root) -> MigrationReport` —— 批量,返回报告(迁移/跳过/已是最新 三种计数)
- `MigrationError` —— 迁移异常基类(继承 AgentError)
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import yaml

from agent_core.exceptions import AgentError
from agent_core.memory.types import CURRENT_SCHEMA_VERSION

logger = logging.getLogger("memory.migration")

__all__ = [
    "MigrationRegistry",
    "MigrationError",
    "MigrationReport",
    "migrate_file",
    "migrate_all",
]


class MigrationError(AgentError):
    """迁移异常(继承 AgentError → 自动支持 cause= / code=)"""
    code: str = "MIGRATION_ERROR"


@dataclass
class MigrationReport:
    """
    migrate_all() 批量迁移结果(取代旧版返回 int)

    字段语义:
    - migrated: 成功从旧版本升级到 CURRENT_SCHEMA_VERSION 的文件数
    - already_current: 本来就是 >= CURRENT(无 .bak 写入、文件未动)
    - skipped: 失败跳过的文件数(frontmatter 损坏 / OSError 等)
    - errors: (path, error_msg) 元组列表,长度应 == skipped

    使用:
        r = migrate_all(root)
        if r.has_errors:
            log.warning(f"{r.skipped} 个文件跳过,首个:{r.errors[0]}")
        print(f"migrated={r.migrated}, already_current={r.already_current}")
    """
    migrated: int = 0
    already_current: int = 0
    skipped: int = 0
    errors: list[tuple[Path, str]] = field(default_factory=list)

    @property
    def total(self) -> int:
        """扫到的 .md 总数(不含 .bak)"""
        return self.migrated + self.already_current + self.skipped

    @property
    def has_errors(self) -> bool:
        return self.skipped > 0

    def __str__(self) -> str:
        return (
            f"MigrationReport(migrated={self.migrated}, "
            f"already_current={self.already_current}, "
            f"skipped={self.skipped})"
        )


# ──────────────────────────────────────────────────────────────────
# MigrationRegistry —— 链式迁移 from_v → CURRENT
# ──────────────────────────────────────────────────────────────────

class MigrationRegistry:
    """
    迁移函数注册表

    链式迁移规则:
    - register(from_v, fn):注册 from_v → from_v+1 的转换函数
    - migrate(from_v, data):从 from_v 一直迁移到 CURRENT_SCHEMA_VERSION

    示例:
        def v0_to_v1(d): d["schema_version"] = 1; return d
        MigrationRegistry.register(0, v0_to_v1)
        MigrationRegistry.migrate(0, {"foo": "bar"})
        # → {"foo": "bar", "schema_version": 1} (假设 CURRENT=1)
    """

    _migrations: dict[int, Callable[[dict], dict]] = {}

    @classmethod
    def register(cls, from_v: int, fn: Callable[[dict], dict]) -> None:
        """注册 from_v → from_v+1 的转换函数"""
        if from_v in cls._migrations:
            logger.warning(f"覆盖已有 v{from_v} 迁移函数")
        cls._migrations[from_v] = fn

    @classmethod
    def get(cls, from_v: int) -> Callable[[dict], dict] | None:
        return cls._migrations.get(from_v)

    @classmethod
    def migrate(cls, from_v: int, data: dict, body: str = "") -> tuple[dict, str]:
        """
        链式迁移 from_v → CURRENT_SCHEMA_VERSION(M11 支持 body 透传)

        Returns:
            (new_fm, new_body) 元组

        Raises:
            MigrationError: 缺中间版本迁移函数 / from_v > CURRENT
        """
        if from_v > CURRENT_SCHEMA_VERSION:
            raise MigrationError(
                f"from_v={from_v} > CURRENT_SCHEMA_VERSION={CURRENT_SCHEMA_VERSION},"
                "目标版本过低,无法降级"
            )
        v = from_v
        while v < CURRENT_SCHEMA_VERSION:
            fn = cls._migrations.get(v)
            if fn is None:
                raise MigrationError(
                    f"缺 v{v} → v{v+1} 迁移函数,已注册: {sorted(cls._migrations.keys())}"
                )
            try:
                data, body = _call_migration_fn(fn, data, body)
            except Exception as e:
                raise MigrationError(f"v{v} 迁移函数异常: {e}", cause=e) from e
            v += 1
        return data, body

    @classmethod
    def migrate_fm_only(cls, from_v: int, data: dict) -> dict:
        """仅迁移 frontmatter(忽略 body),向后兼容旧 caller"""
        new_fm, _ = cls.migrate(from_v, data, "")
        return new_fm

    @classmethod
    def registered_versions(cls) -> list[int]:
        return sorted(cls._migrations.keys())


# ──────────────────────────────────────────────────────────────────
# 单文件 / 批量迁移
# ──────────────────────────────────────────────────────────────────

# --- frontmatter 解析/渲染(与 memory_store.py 同款,避免跨文件依赖) ---

_FM_PATTERN = re.compile(r"\A---\n(.*?)\n---\s*\n?(.*)\Z", re.DOTALL)


def _split_frontmatter(content: str) -> tuple[dict, str]:
    """拆 frontmatter + body(返回 dict, body str)"""
    m = _FM_PATTERN.match(content)
    if not m:
        raise MigrationError("文件无 --- 包裹的 YAML frontmatter")
    fm = yaml.safe_load(m.group(1)) or {}
    return fm, m.group(2)


def _render(text_fm: dict, body: str) -> str:
    """渲染 frontmatter + body 到完整文件内容"""
    yaml_str = yaml.safe_dump(text_fm, allow_unicode=True, sort_keys=False, default_flow_style=False)
    return f"---\n{yaml_str}---\n{body}"


def migrate_file(path: Path) -> dict:
    """
    懒迁移单文件

    流程:
    1. 读 .md
    2. 解析 frontmatter
    3. 若 schema_version >= CURRENT → no-op,返回 migrated=False
    4. 否则:复制原内容到 .bak → migrate(from_v, fm) → 写回 → 返回 migrated=True

    Returns:
        {"frontmatter": {...}, "body": "...", "path": path, "migrated": bool, "from_v": int}

    Raises:
        MigrationError: 解析失败 / 迁移函数异常
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        raise MigrationError(f"读 {path} 失败: {e}", cause=e) from e

    try:
        fm, body = _split_frontmatter(text)
    except MigrationError:
        raise  # 重新抛出

    from_v = int(fm.get("schema_version", 0))
    if from_v >= CURRENT_SCHEMA_VERSION:
        return {
            "frontmatter": fm, "body": body, "path": path,
            "migrated": False, "from_v": from_v,
        }

    # 备份原文件(.bak sidecar)
    bak_path = path.with_suffix(path.suffix + ".bak")
    try:
        bak_path.write_text(text, encoding="utf-8")
    except OSError as e:
        raise MigrationError(f"写 .bak 失败: {e}", cause=e) from e

    # 链式迁移(支持 body 透传给 v2→v3)
    new_fm, new_body = MigrationRegistry.migrate(from_v, fm, body)
    new_text = _render(new_fm, new_body)

    # 写回原路径
    try:
        path.write_text(new_text, encoding="utf-8")
    except OSError as e:
        raise MigrationError(f"写回 {path} 失败: {e}", cause=e) from e

    logger.info(f"migrated {path}: v{from_v} → v{CURRENT_SCHEMA_VERSION} (bak={bak_path})")
    return {
        "frontmatter": new_fm, "body": body, "path": path,
        "migrated": True, "from_v": from_v,
    }


def migrate_all(root: Path) -> MigrationReport:
    """
    批量迁移 root 下所有 .md 文件

    Args:
        root: memory_root 路径(如 ~/.agent_data/memory)

    Returns:
        MigrationReport:含 migrated / already_current / skipped / errors

    Note:
        - 返回值从旧版 `int`(迁移数)升级为 `MigrationReport`,
          调用方需相应调整。旧版 0 = 全部已是最新 的语义不可靠
          (跳过 N 个也返回 0),新报告可区分 skipped 与 already_current。
        - 失败的文件不会中断遍历,会被记入 report.errors,供运维后续处理。
        - M8:加 IPCLock 跨进程互斥,防止 cron 触发的批量迁移与
          MemoryStore.read() 的 lazy migration 撞车(.bak 丢失)。
          若锁被其他进程占用 → 立即返回空 report(非阻塞)。
    """
    from agent_core.memory.ipc_lock import IPCLock, LockBusy

    root = Path(root)
    report = MigrationReport()
    if not root.exists():
        return report

    # 跨进程互斥:防止 .bak 被另一进程覆盖
    lock_path = root.parent / f".{root.name}.migrate.lock"
    lock = IPCLock(lock_path, stale_pid_seconds=3600, stale_mtime_seconds=3600)
    try:
        acquired = lock.acquire(blocking=False)
    except LockBusy:
        logger.info(f"另一进程正在迁移 {root},跳过(返回空 report)")
        return report
    if not acquired:
        logger.info(f"另一进程正在迁移 {root},跳过(返回空 report)")
        return report
    try:
        for path in root.rglob("*.md"):
            if path.suffix == ".bak":
                continue
            try:
                result = migrate_file(path)
                if result["migrated"]:
                    report.migrated += 1
                else:
                    report.already_current += 1
            except MigrationError as e:
                report.skipped += 1
                report.errors.append((path, str(e)))
                logger.warning(f"跳过 {path}: {e}")
                continue
    finally:
        lock.release()
    return report


# ──────────────────────────────────────────────────────────────────
# 内置迁移函数 (v0 → v1 → v2 → v3)
# ──────────────────────────────────────────────────────────────────

def _v0_to_v1(data: dict) -> dict:
    """
    v0 无 schema_version 字段 → 补全 frontmatter 必填字段

    必填补全(让迁移后能过 validate_frontmatter):
    - type: 默认 "user"(v0 绝大多数是用户笔记)
    - created_at: 默认今天(YYYY-MM-DD,ISO 8601 子集,fromisoformat 可解);
      若已有值但是 datetime.date(YAML 把无引号日期解析成 date 对象),
      转成 ISO string 再赋回去
    - item_hash: 占位 64 字符 hex("0"*64);v0 时代无幂等概念,占位不影响后续
      (MemoryStore 重写时会用真实 SHA256 替换)
    - confidence: 默认 0.5

    不补 source_quote:validate 仅在 write() 时必填,read() 不强制
    """
    import datetime

    data["schema_version"] = 1
    data.setdefault("type", "user")
    # created_at:可能是 string / datetime.date(YAML 解析) / 缺失
    if "created_at" not in data:
        data["created_at"] = time.strftime("%Y-%m-%d")
    elif isinstance(data["created_at"], datetime.date):
        data["created_at"] = data["created_at"].isoformat()
    data.setdefault("item_hash", "0" * 64)
    data.setdefault("confidence", 0.5)
    return data


def _v1_to_v2(data: dict) -> dict:
    """v2 加 importance 字段(M3 cold_start 用),1-10 整数"""
    data["schema_version"] = 2
    data.setdefault("importance", 5)
    return data


def _first_meaningful_line(body: str) -> str:
    """取 body 第一段非空非标题的行"""
    for line in body.split("\n"):
        s = line.strip().lstrip("# ").strip()
        if s:
            return s
    return ""


def _v2_to_v3(data: dict, body: str = "") -> tuple[dict, str]:
    """
    M11 v2 → v3 迁移:补 name / description,schema_version 升 3

    - name:从 title mirror(若 title 也缺,取 body 第一段)
    - description:从 body 第一段取(≤200 字符,fallback 到 title 或 '未描述')
    - 保留 body 不变

    返回 (new_fm, body) 以兼容"含 body 的链式迁移"调用方式
    """
    fm = dict(data)  # copy, 避免 mutate

    # 1. name:从 title mirror
    if "name" not in fm or not str(fm["name"]).strip():
        fm["name"] = fm.get("title") or _first_meaningful_line(body)[:50] or "未命名"

    # 2. description:从 body 摘要
    if "description" not in fm or not str(fm["description"]).strip():
        first_para = _first_meaningful_line(body)
        fallback = fm.get("title", "未描述")
        fm["description"] = (first_para or fallback)[:200] or "未描述"

    # 3. schema_version 升 3
    fm["schema_version"] = 3

    return fm, body


# 注册内置迁移(import 时自动生效)
MigrationRegistry.register(0, _v0_to_v1)
MigrationRegistry.register(1, _v1_to_v2)
# M11: v2 → v3 注册(与 v0/v1 不同,接受 (fm, body) 元组返回)
#      兼容两种调用:迁移函数可接受 1 或 2 参数
MigrationRegistry.register(2, _v2_to_v3)


def _call_migration_fn(fn: Callable, data: dict, body: str) -> tuple[dict, str]:
    """调用迁移函数,根据签名自动适配 1-arg / 2-arg

    v0/v1 迁移: fn(data) -> data
    v2 迁移:    fn(data, body) -> (data, body)

    返回 (new_fm, new_body)
    """
    import inspect
    try:
        sig = inspect.signature(fn)
        nparams = len([p for p in sig.parameters.values()
                       if p.kind in (p.POSITIONAL_OR_KEYWORD, p.POSITIONAL_ONLY)])
    except (ValueError, TypeError):
        nparams = 1  # 默认按 1-arg 调用

    if nparams >= 2:
        return fn(data, body)
    return fn(data), body