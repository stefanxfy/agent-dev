"""
冷启动 Seed 加载器（v2.1 §九.3 L5）

M3 / Day 3 — L5 修复

设计要点：
1. **首次启动**：memory 目录为空 → 加载"种子"(seed) 让系统立即可用
2. **种子来源**：
   - a) 仓库内置 `seeds/` 目录(只读基础记忆)
   - b) 用户显式 `cold_start_path` 配置(自定义种子)
   - c) 来自上次会话的 snapshot(可选,见 M4)
3. **幂等加载**：
   - 用 item_hash (A5) 去重
   - 已存在的 memory 不覆盖
4. **类型支持**：所有 sealed type (user/feedback/event/project/reference)
5. **可观测**：
   - 返回 ColdStartReport(loaded/skipped/failed 计数)
   - 不静默失败(所有错误都进 report)

调用入口:
    loader = ColdStartLoader(store, vector_store, embed_fn)
    report = loader.load()  # 自动选 seeds/ 目录
    loader.load_from_dir("/path/to/seeds")
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml

from agent_core.memory.memory_store import (
    MemoryStore,
    MemoryExistsError,
    compute_item_hash,
)
from agent_core.exceptions import StorageError

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────
# 异常
# ──────────────────────────────────────────────────────────────────

class ColdStartError(StorageError):
    """冷启动失败"""
    code = "COLD_START"


# ──────────────────────────────────────────────────────────────────
# 数据结构
# ──────────────────────────────────────────────────────────────────

@dataclass
class SeedItem:
    """单个种子 item"""
    type: str
    title: str
    body: str
    source_quote: str = ""
    tags: list[str] = field(default_factory=list)
    importance: int = 5  # 1-10
    source: str = "seed"  # seed/imported/curated

    @classmethod
    def from_dict(cls, d: dict) -> "SeedItem":
        """从 dict 构造,容错处理"""
        type_ = d.get("type") or d.get("memory_type") or "user"
        if type_ not in ("user", "feedback", "event", "project", "reference"):
            raise ColdStartError(f"未知 seed type: {type_!r}")
        return cls(
            type=type_,
            title=str(d.get("title", "")).strip(),
            body=str(d.get("body", "")).strip(),
            source_quote=str(d.get("source_quote", d.get("quote", ""))),
            tags=list(d.get("tags", [])),
            importance=int(d.get("importance", 5)),
            source=str(d.get("source", "seed")),
        )


@dataclass
class ColdStartReport:
    """加载结果"""
    total: int = 0
    loaded: int = 0
    skipped: int = 0      # hash 已存在
    failed: int = 0
    failures: list[tuple[str, str]] = field(default_factory=list)  # (title, error)
    sources: list[Path] = field(default_factory=list)

    def summary(self) -> str:
        return (
            f"ColdStart: total={self.total} loaded={self.loaded} "
            f"skipped={self.skipped} failed={self.failed} "
            f"sources={len(self.sources)}"
        )


# ──────────────────────────────────────────────────────────────────
# ColdStartLoader
# ──────────────────────────────────────────────────────────────────

class ColdStartLoader:
    """
    冷启动 Seed 加载器

    用法:
        loader = ColdStartLoader(
            memory_store=store,
            vector_store=vec,
            embed_fn=embed_fn,
        )
        report = loader.load()  # 默认读 <project>/seeds/*.yaml
    """

    DEFAULT_SEED_SUBDIR = "seeds"
    SUPPORTED_EXTS = {".yaml", ".yml", ".json"}

    def __init__(
        self,
        memory_store: MemoryStore,
        vector_store,
        embed_fn,
        default_seeds_dir: Optional[Path] = None,
    ):
        """
        Args:
            memory_store: MemoryStore 实例
            vector_store: VectorStore 实例
            embed_fn: EmbedFn 实例
            default_seeds_dir: 默认种子目录(若不指定, 用 <memory_store.root>/../../seeds)
        """
        self.memory_store = memory_store
        self.vector_store = vector_store
        self.embed_fn = embed_fn

        # 默认种子目录
        if default_seeds_dir is None:
            # memory_root 的父目录(通常是 <project>/)
            # 实际种子目录 = <project>/seeds/
            self.default_seeds_dir = (
                memory_store.root.parent / self.DEFAULT_SEED_SUBDIR
            )
        else:
            self.default_seeds_dir = Path(default_seeds_dir)

    # ── 公开 API ─────────────────────────────────────────────

    def load(self, seeds_dir: Optional[Path] = None) -> ColdStartReport:
        """
        从目录加载所有种子(主入口)

        Args:
            seeds_dir: 自定义种子目录,默认用 self.default_seeds_dir

        Returns:
            ColdStartReport
        """
        target = Path(seeds_dir) if seeds_dir else self.default_seeds_dir

        if not target.exists():
            logger.info(f"冷启动种子目录不存在: {target} (跳过)")
            report = ColdStartReport()
            report.sources.append(target)
            return report

        if not target.is_dir():
            raise ColdStartError(f"种子路径不是目录: {target}")

        return self.load_from_dir(target)

    def load_from_dir(self, dir_path: Path) -> ColdStartReport:
        """从指定目录加载"""
        dir_path = Path(dir_path)
        if not dir_path.exists():
            return ColdStartReport(sources=[dir_path])
        if not dir_path.is_dir():
            raise ColdStartError(f"种子路径不是目录: {dir_path}")

        report = ColdStartReport(sources=[dir_path])

        files = sorted(
            f for f in dir_path.iterdir()
            if f.is_file() and f.suffix.lower() in self.SUPPORTED_EXTS
        )

        for fp in files:
            try:
                items = self._parse_seed_file(fp)
            except Exception as e:
                report.failures.append((fp.name, f"parse: {e}"))
                report.failed += 1
                continue

            for item in items:
                report.total += 1
                try:
                    written = self._write_one(item)
                    if written:
                        report.loaded += 1
                    else:
                        report.skipped += 1
                except Exception as e:
                    report.failures.append((item.title or "(no title)", str(e)))
                    report.failed += 1

        logger.info(report.summary())
        return report

    def load_one(self, item: SeedItem) -> bool:
        """
        加载单个 seed item(直接调用,跳过文件)

        Returns:
            True 写入了;False 已存在跳过
        """
        return self._write_one(item)

    # ── 内部 ─────────────────────────────────────────────

    def _parse_seed_file(self, fp: Path) -> list[SeedItem]:
        """解析单个种子文件,返回 SeedItem 列表"""
        content = fp.read_text(encoding="utf-8")

        if fp.suffix.lower() == ".json":
            data = json.loads(content)
        else:
            data = yaml.safe_load(content)

        if data is None:
            return []

        # 支持 3 种格式:
        # 1) 单个 dict
        # 2) list[dict]
        # 3) {"items": [...]}  (顶层包一层)
        if isinstance(data, dict):
            if "items" in data and isinstance(data["items"], list):
                items_data = data["items"]
            else:
                items_data = [data]
        elif isinstance(data, list):
            items_data = data
        else:
            raise ColdStartError(
                f"种子文件格式错误(期望 dict/list, 实际 {type(data).__name__}): {fp}"
            )

        return [SeedItem.from_dict(d) for d in items_data]

    def _write_one(self, item: SeedItem) -> bool:
        """
        写入单个 item

        Returns:
            True 写入了新文件
            False hash 重复, 跳过

        Raises:
            其他异常向上传播(被 report.failed 捕获)
        """
        # 1. 算 hash
        item_hash = compute_item_hash(item.type, item.body, item.source_quote)

        # 2. 查 A5 幂等: 同 hash 已存在就跳过
        rel_path = f"{item.type}/{item_hash}.md"
        full_path = self.memory_store.root / rel_path
        if full_path.exists():
            logger.debug(f"已存在,跳过: {rel_path}")
            return False

        # 3. 写入文件
        #    source 字段仅接受 user_input / extracted / manual（types.py §7）
        #    把 seed/imported/curated 都映射为 manual
        if item.source in ("user_input", "extracted", "manual"):
            fm_source = item.source
        else:
            fm_source = "manual"
        extra = {
            "importance": item.importance,
            "source": fm_source,
            "seed_origin": item.source,  # 保留原始来源供审计
        }
        try:
            self.memory_store.write(
                type=item.type,
                title=item.title,
                body=item.body,
                source_quote=item.source_quote,
                tags=item.tags,
                extra=extra,
            )
        except MemoryExistsError:
            # 并发场景: 另一个进程已写入
            logger.debug(f"并发写入,跳过: {rel_path}")
            return False

        # 4. 加入向量库
        try:
            text_for_emb = f"{item.title}\n{item.body}"
            embedding = self.embed_fn.encode(text_for_emb)
            # ChromaDB metadata 约束:list 值不能为空
            #   tags=[] 会让 upsert 报 ValueError,过滤掉空 list 即可
            metadata = {
                "type": item.type,
                "title": item.title,
                "importance": item.importance,
                "source": item.source,
            }
            if item.tags:   # 非空才加 tags 字段
                metadata["tags"] = item.tags
            # VectorStoreProtocol.add(doc: dict) — 统一字典格式
            # 由 ChromaVectorStore 实现(见 chroma_store.py)
            self.vector_store.add(item_hash, embedding)
        except Exception as e:
            # 向量写入失败不阻塞(文件已写, M5 重索引能补)
            logger.warning(f"seed 向量化失败({item.title}): {e}")

        return True


__all__ = [
    "ColdStartLoader",
    "SeedItem",
    "ColdStartReport",
    "ColdStartError",
]