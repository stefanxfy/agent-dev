"""
记忆系统模块（v2.1）

M1 / Day 1 交付：types / config / path_validator 三件套
M2+ 后续模块（dual_channel_writer / memory_store / distiller / scheduler / retriever）随 milestone 增量导入

设计原则：
- 子模块按需导入，避免 M2+ 未完成的模块拖累 M1 测试
- 对外暴露 `__all__` 明确列出可用 API
"""

from __future__ import annotations

# M1 已就绪
from .types import (
    CURRENT_SCHEMA_VERSION,
    MemoryType,
    validate_body,
    validate_frontmatter,
    validate_type,
)
from .config import MemoryConfig
from .path_validator import MemoryPathValidator, PathSecurityError

# M2 已就绪（双通道脊柱）
from .meta_db import MetaDB, MetaDBError
from .ipc_lock import IPCLock, LockBusy, LockStale, make_daily_lock, make_extract_lock
from .memory_store import (
    MemoryStore,
    MemoryStoreError,
    MemoryExistsError,
    compute_item_hash,
)
from .dual_channel_writer import (
    DualChannelWriter,
    DualChannelError,
    ExtractionInProgressError,
    ExtractionCandidate,
    MockVectorStore,
    TurnMessage,
)
from .memory_editor import (
    MemoryEditor,
    MemoryEditError,
    SecretDetectedError,
    InjectionDetectedError,
    EditPreconditionError,
    sanitize,
    scan_secrets,
)

# v1 历史实现（保留兼容）
from .daily import DailyLogger

# M3+ 待实现 —— 暂不导入，避免 M2 测试 fail
# from .retriever import MemoryRetriever
# from .distiller import MemoryDistiller
# from .scheduler import DistillationScheduler

__all__ = [
    # M1: types
    "MemoryType",
    "validate_type",
    "validate_frontmatter",
    "validate_body",
    "CURRENT_SCHEMA_VERSION",
    # M1: config
    "MemoryConfig",
    # M1: path
    "MemoryPathValidator",
    "PathSecurityError",
    # M2: meta_db
    "MetaDB",
    "MetaDBError",
    # M2: ipc_lock
    "IPCLock",
    "LockBusy",
    "LockStale",
    "make_daily_lock",
    "make_extract_lock",
    # M2: memory_store
    "MemoryStore",
    "MemoryStoreError",
    "MemoryExistsError",
    "compute_item_hash",
    # M2: dual_channel_writer
    "DualChannelWriter",
    "DualChannelError",
    "ExtractionInProgressError",
    "ExtractionCandidate",
    "MockVectorStore",
    "TurnMessage",
    # M2: memory_editor
    "MemoryEditor",
    "MemoryEditError",
    "SecretDetectedError",
    "InjectionDetectedError",
    "EditPreconditionError",
    "sanitize",
    "scan_secrets",
    # v1 legacy
    "DailyLogger",
]