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

# M1 已有但属 v1 历史实现（保留调用方兼容，不在 M1 验收范围）
from .daily import DailyLogger

# M2+ 待实现 —— 暂不导入，避免 M1 测试 fail
# from .memory_store import MemoryStore
# from .dual_channel_writer import DualChannelWriter
# from .retriever import MemoryRetriever
# from .distiller import MemoryDistiller
# from .scheduler import DistillationScheduler

__all__ = [
    # types
    "MemoryType",
    "validate_type",
    "validate_frontmatter",
    "validate_body",
    "CURRENT_SCHEMA_VERSION",
    # config
    "MemoryConfig",
    # path_validator
    "MemoryPathValidator",
    "PathSecurityError",
    # v1 legacy
    "DailyLogger",
]