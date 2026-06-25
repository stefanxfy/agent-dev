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
from .config import DistillationConfig, MemoryConfig
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

# M3: 检索 + 安全
from .embeddings import (
    EmbedFn,
    BGEM3EmbedFn,
    MiniLMEmbedFn,
    make_embed_fn,
    EmbeddingError,
)
from .chroma_store import (
    ChromaVectorStore,
    ChromaStoreError,
    make_chroma_store,
)
from .secret_scanner import (
    SecretScanner,
    SecretHit,
    ScanResult,
    get_default_scanner,
    scan_text,
    assert_clean,
)
from .cold_start import (
    ColdStartLoader,
    SeedItem,
    ColdStartReport,
    ColdStartError,
)
from .retriever import (
    MemoryRetriever,
    MemoryHit,
    RetrievalReport,
    RetrievalMode,
    RetrievalError,
)
from .extractor import (
    MemoryExtractor,
    ExtractStats,
    ExtractorError,
    CandidateRejected,
)

# M4: L3 会话内压缩
from .sm_layer import (
    SessionMemoryLayer,
    SessionMemoryError,
    CompactDecision,
    CompactResult,
    TurnContext,
)

# M5: L5 蒸馏 (autoDream)
from .distiller import (
    Distiller,
    DistillationScheduler,
    DistillationError,
    DistillationResult,
)

# M6: 调度外层 + OTel 可观测
from .tracing import tracer, configure_tracing, TRACER_NAME
from .scheduler import DistillationLoop

# M9: 严格双通道(ReAct 决策树 + 桥接器)
from .extraction_gate import ExtractionGate, TurnContext, Decision
from .react_memory_bridge import ReactMemoryBridge, MemoryEvent, MemoryEventKind
from .prompt_templates import build_extract_prompt, EXTRACT_SYSTEM_PROMPT

# M7: Schema 迁移
from .migration import (
    MigrationRegistry,
    MigrationError,
    MigrationReport,
    migrate_file,
    migrate_all,
)

# M8: A6 Data Lifecycle (上线运维)
from .lifecycle import (
    BackupReport,
    CapacityReport,
    IntegrityReport,
    LifecycleError,
    capacity_govern,
    daily_backup,
    integrity_check,
    list_backups,
    restore_backup,
)

# v1 DailyLogger 已删除(2026-06-25 dead config cleanup)
# 历史 JSONL daily log 已被 memory_tasks WAL 表 + memory_store 收编

__all__ = [
    # M1: types
    "MemoryType",
    "validate_type",
    "validate_frontmatter",
    "validate_body",
    "CURRENT_SCHEMA_VERSION",
    # M1: config
    "MemoryConfig",
    "DistillationConfig",
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
    "TurnMessage",
    # M2: memory_editor
    "MemoryEditor",
    "MemoryEditError",
    "SecretDetectedError",
    "InjectionDetectedError",
    "EditPreconditionError",
    "sanitize",
    "scan_secrets",
    # M3: embeddings
    "EmbedFn",
    "BGEM3EmbedFn",
    "MiniLMEmbedFn",
    "make_embed_fn",
    "EmbeddingError",
    # M3: vector store
    "ChromaVectorStore",
    "ChromaStoreError",
    "make_chroma_store",
    # M3: secret_scanner
    "SecretScanner",
    "SecretHit",
    "ScanResult",
    "get_default_scanner",
    "scan_text",
    "assert_clean",
    # M3: cold_start
    "ColdStartLoader",
    "SeedItem",
    "ColdStartReport",
    "ColdStartError",
    # M3: retriever
    "MemoryRetriever",
    "MemoryHit",
    "RetrievalReport",
    "RetrievalMode",
    "RetrievalError",
    # M3: extractor
    "MemoryExtractor",
    "ExtractStats",
    "ExtractorError",
    "CandidateRejected",
    # M4: L3 会话内压缩
    "SessionMemoryLayer",
    "SessionMemoryError",
    "CompactDecision",
    "CompactResult",
    "TurnContext",
    # M5: L5 蒸馏
    "Distiller",
    "DistillationScheduler",
    "DistillationError",
    "DistillationResult",
    # M6: 调度 + 可观测
    "DistillationLoop",
    "tracer",
    "configure_tracing",
    "TRACER_NAME",
    # M9: 严格双通道(ReAct)
    "ExtractionGate",
    "TurnContext",
    "Decision",
    "ReactMemoryBridge",
    "MemoryEvent",
    "MemoryEventKind",
    "build_extract_prompt",
    "EXTRACT_SYSTEM_PROMPT",
    # M7: Schema 迁移
    "MigrationRegistry",
    "MigrationError",
    "MigrationReport",
    "migrate_file",
    "migrate_all",
    # M8: Lifecycle
    "BackupReport",
    "CapacityReport",
    "IntegrityReport",
    "LifecycleError",
    "capacity_govern",
    "daily_backup",
    "integrity_check",
    "list_backups",
    "restore_backup",
]