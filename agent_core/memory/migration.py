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
- `MigrationRegistry.migrate(from_v, data) -> dict` —— 链式迁移
- `migrate_file(path) -> dict` —— 单文件懒迁移(返回 migrated bool)
- `migrate_all(root) -> int` —— 批量,返回迁移数
- `MigrationError` —— 迁移异常基类
"""

from __future__ import annotations

import logging
import re
import time
from pathlib import Path
from typing import Any, Callable

import yaml

from agent_core.exceptions import AgentError
from agent_core.memory.types import CURRENT_SCHEMA_VERSION

logger = logging.getLogger("memory.migration")

__all__ = [
    "MigrationRegistry",
    "MigrationError",
    "migrate_file",
    "migrate_all",
]


class MigrationError(AgentError):
    """迁移异常(继承 AgentError → 自动支持 cause= / code=)"""
    code: str = "MIGRATION_ERROR"


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
    def migrate(cls, from_v: int, data: dict) -> dict:
        """
        链式迁移 from_v → CURRENT_SCHEMA_VERSION

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
                data = fn(data)
            except Exception as e:
                raise MigrationError(f"v{v} 迁移函数异常: {e}", cause=e) from e
            v += 1
        return data

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

    # 链式迁移
    new_fm = MigrationRegistry.migrate(from_v, fm)
    new_text = _render(new_fm, body)

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


def migrate_all(root: Path) -> int:
    """
    批量迁移 root 下所有 .md 文件

    Args:
        root: memory_root 路径(如 ~/.agent_data/memory)

    Returns:
        int: 迁移文件数(0 = 全部已是最新)
    """
    root = Path(root)
    if not root.exists():
        return 0
    count = 0
    for path in root.rglob("*.md"):
        # 跳过 .bak sidecar
        if path.suffix == ".bak":
            continue
        try:
            result = migrate_file(path)
            if result["migrated"]:
                count += 1
        except MigrationError as e:
            logger.warning(f"跳过 {path}: {e}")
            continue
    return count


# ──────────────────────────────────────────────────────────────────
# 内置迁移函数 (v0 → v1 → v2)
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


# 注册内置迁移(import 时自动生效)
MigrationRegistry.register(0, _v0_to_v1)
MigrationRegistry.register(1, _v1_to_v2)