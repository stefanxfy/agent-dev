"""
Per-file 记忆存储（v2.1 §4.5）

M2 / Day 2 — A7 + L7 + v2.1 4 类目录

设计要点：
1. 每个记忆一条 .md 文件，路径 = <type>/<hash>.md
2. frontmatter 用 YAML 序列化（含 type / created_at / item_hash / schema_version）
3. body 是 Markdown，含 **Why:** 段（feedback/project 强制）
4. 写盘原子性：tmp + os.replace（防止 partial write）
5. 读取走 MemoryPathValidator（防路径越界）
6. 提供 list_by_type / list_by_tag / search 索引
7. schema_version 在 A7 阶段用于迁移（M2 写入固定 CURRENT_SCHEMA_VERSION）

文件结构示例:
    memory_root/
    ├── user/
    │   └── 5fa7...c3b9.md   ← 一个 user 类型记忆
    ├── feedback/
    │   └── a8d2...1e4f.md
    └── ...
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, Union

import yaml

from agent_core.exceptions import StorageError, StorageReadError, StorageWriteError
from agent_core.memory.path_validator import MemoryPathValidator
from agent_core.memory.types import (
    CURRENT_SCHEMA_VERSION,
    Frontmatter,
    FrontmatterError,
    MemoryType,
    validate_body,
    validate_frontmatter,
    validate_type,
)


logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────
# 异常
# ──────────────────────────────────────────────────────────────────

class MemoryStoreError(StorageError):
    """记忆存储异常"""
    code = "MEMORY_STORE"


class MemoryExistsError(MemoryStoreError):
    """item_hash 已存在（A5 幂等）"""
    code = "MEMORY_EXISTS"


# ──────────────────────────────────────────────────────────────────
# 工具函数
# ──────────────────────────────────────────────────────────────────

def compute_item_hash(type_: MemoryType, body: str, source_quote: Optional[str] = None) -> str:
    """
    计算 item_hash（A5 幂等去重 key）

    Args:
        type_: 记忆类型
        body: 记忆正文
        source_quote: L7 必填，源引用

    Returns:
        64 字符 SHA-256 hex
    """
    payload = f"{type_}\n{body}\n{source_quote or ''}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _parse_frontmatter(content: str) -> tuple[Frontmatter, str]:
    """
    解析 --- 包裹的 YAML frontmatter

    Returns: (frontmatter dict, body markdown)

    Raises:
        MemoryStoreError: 格式错误
    """
    if not content.startswith("---\n"):
        raise MemoryStoreError("文件缺少 YAML frontmatter（以 --- 开头）")

    # 找第二个 ---
    m = re.search(r"\n---\s*\n", content[4:])
    if not m:
        raise MemoryStoreError("frontmatter 缺少结束标记 ---")

    yaml_block = content[4 : 4 + m.start()]
    body = content[4 + m.end():]

    try:
        data = yaml.safe_load(yaml_block) or {}
    except yaml.YAMLError as e:
        raise MemoryStoreError(f"frontmatter YAML 解析失败: {e}", cause=e)

    return data, body  # type: ignore[return-value]


# M11:对外暴露 parse_frontmatter(供 memory_index.scan_memory_files 复用)
parse_frontmatter = _parse_frontmatter


def _serialize_frontmatter(data: dict[str, Any]) -> str:
    """YAML 序列化（保证 key 顺序稳定，便于 diff）"""
    return yaml.safe_dump(data, allow_unicode=True, sort_keys=False, default_flow_style=False)


# ──────────────────────────────────────────────────────────────────
# MemoryStore
# ──────────────────────────────────────────────────────────────────

class MemoryStore:
    """
    per-file 记忆存储

    用法:
        store = MemoryStore(Path("~/.agent_data/memory"))

        # 写入(M11 v3: 必传 name + description)
        h = store.write(
            type="user",
            name="用户叫小明",
            description="Python 后端工程师",
            body="用户叫小明",
            source_quote="我说'我叫小明'",
        )

        # 读取
        data = store.read("user/" + h + ".md")
        # {"frontmatter": {...}, "body": "..."}

        # 列出
        items = store.list_by_type("user")
    """

    def __init__(self, memory_root: Union[str, Path]):
        self.root = Path(memory_root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.validator = MemoryPathValidator(self.root)
        self._write_lock = threading.Lock()  # 同进程并发写保护（跨进程靠 ipc_lock）

    # ── 写入（A5 幂等 + atomic） ─────────────────────────────

    def write(
        self,
        type: str,                   # 避免关键字
        body: str,
        source_quote: str,           # L7 必填
        name: Optional[str] = None,        # M11 v3 必填
        description: Optional[str] = None,  # M11 v3 必填
        title: Optional[str] = None,        # M11: 可选,默认 mirror name
        tags: Optional[list[str]] = None,
        extra: Optional[dict[str, Any]] = None,
        overwrite: bool = False,
    ) -> str:
        """
        写入一条记忆（per-file + frontmatter）

        M11 v3 schema: name / description 必填。

        Args:
            type: 4 类之一
            name: 必填(M11 v3),短名(用于 MEMORY.md manifest)
            description: 必填(M11 v3),≤200 字符,过长自动截断
            body: 记忆正文 Markdown
            source_quote: L7 必填，源引用（用户原话 / 蒸馏依据）
            title: 可选,若不传则 mirror name(兼容旧 caller)
            tags: 选填标签列表
            extra: 选填扩展字段（写入 frontmatter 顶层）
            overwrite: 同 hash 已存在时是否覆盖（默认 False，A5 幂等）

        Returns:
            item_hash (64 字符 hex)

        Raises:
            FrontmatterError: 缺 name / description (M11 v3)
            ValueError: 类型非法 / 缺 source_quote / body 校验失败
            MemoryExistsError: 同 hash 已存在（A5 幂等，overwrite=False）
            PathSecurityError: 路径越界
        """
        # 0. M11 v3: name / description 必填(向后兼容: 若 caller 用旧 title-only API,
        #    用 title 作为 name, body[:200] 作为 description)
        if not name:
            name = title  # type: ignore[assignment]
        if not name or not str(name).strip():
            raise FrontmatterError("name 必填(M11 v3 schema)")
        if not description:
            # 取 body 第一段非空(去掉 markdown 标记), 截 200
            desc_fallback = ""
            for line in body.split("\n"):
                stripped = line.strip().lstrip("# ").strip()
                if stripped:
                    desc_fallback = stripped
                    break
            description = desc_fallback[:200] or "未描述"
        if not str(description).strip():
            raise FrontmatterError("description 必填(M11 v3 schema)")

        # 1. 计算 item_hash (前置,仅依赖 type/body/source_quote 字符串,
        #    不依赖 type 校验或路径校验;M11: name/description 不参与 hash)
        item_hash = compute_item_hash(type, body, source_quote)  # type: ignore[arg-type]

        # 2. M10 C1.1: 路径校验前置(security check 先于 schema check, §14.1)
        rel_path = f"{type}/{item_hash}.md"
        abs_path = self.validator.validate(rel_path)  # 失败抛 PathSecurityError

        # 3. 校验 type
        validate_type(type)

        # 4. L7: source_quote 必填
        if not source_quote or not source_quote.strip():
            raise ValueError("source_quote 必填（v2.1 L7 不变量），防止凭空记忆")

        # 5. 校验 body（含 **Why:** 强制）
        validate_body(type, body)  # type: ignore[arg-type]

        # 6. mirror name → title(若 caller 未显式传 title)
        if title is None:
            title = name

        # 7. 构造 frontmatter(M11 v3)
        now = datetime.now(timezone.utc).isoformat()
        fm: dict[str, Any] = {
            "type": type,
            "created_at": now,
            "item_hash": item_hash,
            "schema_version": CURRENT_SCHEMA_VERSION,
            "name": name,           # M11 必填
            "description": description,  # M11 必填
            "title": title,         # 保留兼容
        }
        if tags:
            fm["tags"] = list(tags)
        if extra:
            fm.update(extra)

        # 8. frontmatter 校验(会自动截断超长 description)
        validate_frontmatter(fm)

        # 9. 构造文件内容（frontmatter + 标题 + body）
        # title 放在 frontmatter 之后第一行（Markdown H1），便于阅读
        fm_str = _serialize_frontmatter(fm)
        content = f"---\n{fm_str}---\n\n# {title}\n\n{body}\n"

        # 9. A5 幂等：已存在且不覆盖 → 抛异常
        if abs_path.exists() and not overwrite:
            raise MemoryExistsError(f"item_hash {item_hash[:12]}... 已存在（A5 幂等）")

        # 10. 原子写：tmp + os.replace
        abs_path.parent.mkdir(parents=True, exist_ok=True)
        with self._write_lock:
            fd, tmp_path = tempfile.mkstemp(
                dir=abs_path.parent, prefix=f".{item_hash[:8]}.", suffix=".tmp"
            )
            try:
                os.write(fd, content.encode("utf-8"))
                os.fsync(fd)
                os.close(fd)
                os.replace(tmp_path, abs_path)
                # 11. M10 C1.3: chmod 0o600(§14.3, 落盘权限收紧)
                os.chmod(abs_path, 0o600)
            except Exception as e:
                with __import__("contextlib").suppress(OSError):
                    os.close(fd)
                with __import__("contextlib").suppress(OSError):
                    os.unlink(tmp_path)
                raise StorageWriteError(f"写记忆文件失败: {e}", cause=e)

        return item_hash

    # ── 读取 ────────────────────────────────────────────────

    def read(self, rel_path: str) -> dict[str, Any]:
        """
        读取记忆文件

        M7 懒迁移:若 schema_version 过旧,自动调 migrate_file() 升级,
        原文件保留 .bak sidecar。

        Returns: {"frontmatter": {...}, "body": "..."}
        """
        abs_path = self.validator.validate(rel_path, must_exist=True)
        try:
            content = abs_path.read_text(encoding="utf-8")
        except OSError as e:
            raise StorageReadError(f"读记忆文件失败: {e}", cause=e)

        fm, body = _parse_frontmatter(content)

        # M7 懒迁移:若 schema_version < CURRENT → 升级
        if int(fm.get("schema_version", 0)) < CURRENT_SCHEMA_VERSION:
            from agent_core.memory.migration import migrate_file
            try:
                result = migrate_file(abs_path)
                fm = result["frontmatter"]
                body = result["body"]
            except Exception as e:
                logger.warning(f"懒迁移 {abs_path} 失败,使用原内容: {e}")
                # 失败 → 用原内容,但 validate_frontmatter 可能仍会拒
                # 让上层 caller 决定(不静默吞)

        # 防御性：再校验一次
        validate_frontmatter(fm)
        return {"frontmatter": fm, "body": body, "path": rel_path}

    def read_by_hash(self, item_hash: str, type: str) -> dict[str, Any]:
        """按 hash 直接读"""
        validate_type(type)
        return self.read(f"{type}/{item_hash}.md")

    # ── 列表 / 搜索 ────────────────────────────────────────

    def list_by_type(self, type: str) -> list[dict[str, Any]]:
        """列出某类型下所有记忆（仅 frontmatter，无 body 内容）"""
        validate_type(type)
        type_dir = self.root / type
        if not type_dir.exists():
            return []

        results = []
        for p in sorted(type_dir.glob("*.md")):
            try:
                data = self.read(str(p.relative_to(self.root)))
                results.append({
                    "hash": data["frontmatter"]["item_hash"],
                    "title": data["body"].split("\n", 1)[0].lstrip("# ").strip(),
                    "created_at": data["frontmatter"]["created_at"],
                    "tags": data["frontmatter"].get("tags", []),
                    "path": str(p.relative_to(self.root)),
                })
            except (MemoryStoreError, ValueError):
                # 损坏的文件跳过（不阻断整个列表）
                continue
        return results

    def list_by_session(
        self,
        session_id: str,
        since_turn: int = 0,
    ) -> list[dict[str, Any]]:
        """
        列出指定 session_id 且 turn_index >= since_turn 的所有记忆

        用途:门1 跑 LLM 评分时,拼"本周期已提取记忆"块

        Returns:
            [{"frontmatter": {...}, "body": "..."}, ...]
        """
        results: list[dict[str, Any]] = []
        for type_ in ("user", "feedback", "project", "reference"):
            type_dir = self.root / type_
            if not type_dir.exists():
                continue
            for md_path in type_dir.glob("*.md"):
                try:
                    data = self.read(str(md_path.relative_to(self.root)))
                except Exception:
                    continue
                fm = data.get("frontmatter", {})
                # frontmatter 需同时含 session_id 和 turn_index
                if fm.get("session_id") != session_id:
                    continue
                turn_idx = fm.get("turn_index", -1)
                if turn_idx < since_turn:
                    continue
                data["frontmatter"]["type"] = data["frontmatter"].get("type", type_)
                results.append(data)
        return results

    def list_all(self) -> dict[str, list[dict[str, Any]]]:
        """列出所有类型的记忆（按 type 分组）"""
        result: dict[str, list[dict[str, Any]]] = {}
        for t in ("user", "feedback", "project", "reference"):
            result[t] = self.list_by_type(t)
        return result

    def count_by_type(self) -> dict[str, int]:
        """统计各类型记忆数量"""
        return {t: len(self.list_by_type(t)) for t in ("user", "feedback", "project", "reference")}

    # ── 删除（谨慎） ──────────────────────────────────────

    def delete(self, rel_path: str) -> bool:
        """删除记忆文件（谨慎使用，蒸馏回滚时用）"""
        abs_path = self.validator.validate(rel_path, must_exist=True)
        try:
            abs_path.unlink()
            return True
        except OSError as e:
            raise StorageWriteError(f"删除记忆文件失败: {e}", cause=e)


__all__ = [
    "MemoryStore",
    "MemoryStoreError",
    "MemoryExistsError",
    "compute_item_hash",
]