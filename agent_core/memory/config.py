"""
记忆系统配置（Pydantic v2 强校验）

M1 / Day 1 — O8 修复 + v2.1 §12.5 配置校验

设计要点：
1. 用 Pydantic v2 BaseModel —— 编译期 + 运行期双重校验
2. 嵌套结构（retrieval / distillation / paths）—— 单一职责，便于局部重载
3. 跨字段校验（weights sum = 1.0）—— Pydantic v2 model_validator(mode="after")
4. 单一 load 入口（from_dict / from_env）—— 启动时一次性 fail-fast
5. 字段全部带 description —— IDE 提示 / 自动生成文档

不使用：
- ❌ 不引入额外依赖（直接 pydantic）
- ❌ 不修改全局 agent_core.config.Config（保持向后兼容）
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)


# ──────────────────────────────────────────────────────────────────
# 1. 子配置：检索权重 / 蒸馏阈值 / 路径
# ──────────────────────────────────────────────────────────────────

class RetrievalConfig(BaseModel):
    """
    检索相关配置（§6 检索策略）

    mode:
        - vector:    仅向量检索
        - file:      仅文件名 / 元数据 grep
        - hybrid:    两者融合（默认）
    """
    model_config = ConfigDict(extra="forbid", frozen=False)

    mode: Literal["vector", "file", "hybrid"] = "hybrid"
    top_k: int = Field(default=8, ge=1, le=50, description="返回的最大记忆条数")
    semantic_weight: float = Field(
        default=0.7, ge=0.0, le=1.0,
        description="hybrid 模式下向量分权重",
    )
    lexical_weight: float = Field(
        default=0.3, ge=0.0, le=1.0,
        description="hybrid 模式下 BM25 权重",
    )
    min_score: float = Field(
        default=0.3, ge=0.0, le=1.0,
        description="低于此分的记忆丢弃",
    )
    token_budget: int = Field(
        default=2000, ge=100, le=8000,
        description="注入到 prompt 的记忆总 token 上限（v2.1 L8）",
    )

    @model_validator(mode="after")
    def _weights_sum_to_one(self) -> "RetrievalConfig":
        # v2.1 §6.4 hybrid 模式权重必须归一化
        if self.mode == "hybrid":
            total = self.semantic_weight + self.lexical_weight
            if abs(total - 1.0) > 1e-6:
                raise ValueError(
                    f"hybrid 模式下 semantic_weight ({self.semantic_weight}) "
                    f"+ lexical_weight ({self.lexical_weight}) 必须 = 1.0，"
                    f"当前 = {total}"
                )
        return self


class DistillationConfig(BaseModel):
    """
    蒸馏调度（§7 autoDream）

    时间门:距离上次成功蒸馏至少 24h 才触发
    规模门:daily log 累计 ≥ 50 行才触发
    变更门:memory 文件相对 prior_mtime 有 ≥ 10% 变更才触发
    session 门:增量 session 数 ≥ min_sessions_for_distill 才触发(M5 增)
    """
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    min_interval_hours: int = Field(default=24, ge=1, le=168)
    min_daily_log_lines: int = Field(default=50, ge=10, le=10000)
    change_threshold_pct: float = Field(default=0.10, ge=0.01, le=1.0)
    min_sessions_for_distill: int = Field(
        default=5, ge=1, le=100,
        description="增量 session 数 ≥ 此值才触发(门3,2026-06-21 M5 增)",
    )

    # 锁抢占阈值（v2.1 A1+A2+A11）
    lock_stale_pid_seconds: int = Field(default=3600, ge=60, le=86400)
    lock_stale_mtime_seconds: int = Field(default=3600, ge=60, le=86400)
    extraction_timeout_seconds: int = Field(default=60, ge=10, le=600)

    # 大小控制
    max_distill_input_tokens: int = Field(default=30000, ge=1000, le=200000)


class PathsConfig(BaseModel):
    """
    路径配置（§6 / §7 物理路径）
    """
    model_config = ConfigDict(extra="forbid")

    memory_root: Path = Field(
        default=Path("~/.agent_data/memory"),
        description="per-file 记忆根目录（user/feedback/project/reference/）",
    )
    daily_log_dir: Path = Field(
        default=Path("~/.agent_data/logs"),
        description="daily log 写入目录",
    )
    vector_index_dir: Path = Field(
        default=Path("~/.agent_data/vector_index"),
        description="ChromaDB 持久化目录",
    )
    meta_db: Path = Field(
        default=Path("~/.agent_data/meta.db"),
        description="SQLite 元数据库（cursor 持久化，§4.1 A3）",
    )
    seed_dir: Path = Field(
        default=Path("agent_core/memory/seed"),
        description="cold start 种子目录（v2.1 L5）",
    )

    @field_validator("*", mode="after")
    @classmethod
    def _expand_user(cls, v: Path) -> Path:
        """自动展开 ~ 为用户目录"""
        return v.expanduser()


class CompactConfig(BaseModel):
    """
    会话内压缩配置（§4.3 + §4.4 L3）

    L3 = SessionMemoryLayer:会话内滚动摘要(零 LLM 快路径)
    触发条件 + 5 条回退条件

    v2.1 §4.4 — 与 Claude Code `shouldUseSessionMemoryCompaction` 一一对应
    """
    model_config = ConfigDict(extra="forbid")

    # 总开关(feature gate,对应 §4.4 回退条件 1)
    enabled: bool = Field(default=True, description="L3 会话内压缩总开关")

    # 触发阈值(§4.3 + Day 4 plan)
    sm_token_threshold: int = Field(
        default=10000, ge=1000, le=200000,
        description="触发压缩的 token 数阈值(token > 此值触发)",
    )
    tool_count_threshold: int = Field(
        default=10, ge=1, le=100,
        description="触发压缩的工具调用次数阈值(tool > 此值触发)",
    )

    # SM 文件大小控制(对应 §4.4 回退条件 3)
    max_sm_tokens_for_compact: int = Field(
        default=8000, ge=500, le=50000,
        description="SM 文件过大阈值(超过走传统 LLM 压缩)",
    )
    max_per_section_chars: int = Field(
        default=8000, ge=500, le=50000,
        description="compact 时每个 section 截断字符数",
    )

    # extraction 等待超时(对应 §4.4 回退条件 4)
    extraction_wait_timeout_ms: int = Field(
        default=15000, ge=1000, le=120000,
        description="extraction 在跑时的等待超时(ms)",
    )

    # 预估压缩后仍超阈值比例(对应 §4.4 回退条件 5)
    sm_insufficient_buffer_ratio: float = Field(
        default=0.95, ge=0.5, le=1.0,
        description="SM-compact 后预估 / 阈值 > 此值 → 走传统(默认 0.95,留 5% 余量)",
    )


class CostConfig(BaseModel):
    """M10 C6.2: 成本预算配置"""
    model_config = ConfigDict(extra="forbid")

    enabled: bool = Field(default=True)
    daily_budget_usd: float = Field(default=1.0, ge=0.0)
    per_extract_budget_usd: float = Field(default=0.05, ge=0.0)


class DedupConfig(BaseModel):
    """语义去重配置(向量召回 + LLM 判定)

    写盘前用候选记忆的向量在库里召回最相似的几条:
      - 相似度 >= auto_threshold        → 直接判重复,跳过(不调 LLM,省 token)
      - judge_floor <= 相似度 < auto    → 调一次 LLM 判「重复/新增」
      - 相似度 < judge_floor            → 视为新记忆,正常写盘
    auto_threshold 默认 0.95(实测:否定/近义事实都 < 0.90,逐字改写 > 0.95,安全)。
    """
    model_config = ConfigDict(extra="forbid")

    enabled: bool = Field(default=True)
    auto_threshold: float = Field(default=0.95, ge=0.0, le=1.0)
    judge_floor: float = Field(default=0.85, ge=0.0, le=1.0)
    top_k: int = Field(default=5, ge=1)


class SafetyConfig(BaseModel):
    """
    安全策略（§14 安全模型）
    """
    model_config = ConfigDict(extra="forbid")

    enable_secret_scanner: bool = True
    enable_path_validator: bool = True
    enable_write_sandbox: bool = True

    # secret 扫描 4 种基础 pattern（v2.1 §14.4 必装）
    secret_patterns: list[str] = Field(default_factory=lambda: [
        r"(?i)api[_-]?key\s*[=:]\s*['\"]?[A-Za-z0-9_\-]{16,}",
        r"(?i)secret[_-]?key\s*[=:]\s*['\"]?[A-Za-z0-9_\-]{16,}",
        r"sk-[A-Za-z0-9]{20,}",     # OpenAI
        r"sk-ant-[A-Za-z0-9\-_]{20,}", # Anthropic
    ])


# ──────────────────────────────────────────────────────────────────
# 2. 顶层 MemoryConfig
# ──────────────────────────────────────────────────────────────────

class MemoryConfig(BaseModel):
    """
    记忆系统完整配置（v2.1 §12.5）

    用法:
        config = MemoryConfig()                    # 默认值
        config = MemoryConfig.from_env()           # 从 MEMORY_xxx env 读
        config = MemoryConfig.from_dict({...})     # 从 dict 读（测试友好）
    """
    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    # 子配置
    retrieval: RetrievalConfig = Field(default_factory=RetrievalConfig)
    distillation: DistillationConfig = Field(default_factory=DistillationConfig)
    paths: PathsConfig = Field(default_factory=PathsConfig)
    safety: SafetyConfig = Field(default_factory=SafetyConfig)
    compact: CompactConfig = Field(default_factory=CompactConfig)
    cost: CostConfig = Field(default_factory=CostConfig)
    dedup: DedupConfig = Field(default_factory=DedupConfig)

    # 全局开关
    enabled: bool = Field(default=True, description="总开关")
    embed_model: str = Field(
        default="BAAI/bge-m3",
        description="嵌入模型（v2.1 §九.1 推荐 bge-m3 多语言 1024-dim）",
    )

    # ── 入口 ──

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "MemoryConfig":
        """从 dict 构造（嵌套 dict 自动展开为子 Model）"""
        return cls.model_validate(data)

    @classmethod
    def from_env(cls, prefix: str = "MEMORY_") -> "MemoryConfig":
        """
        从环境变量构造

        约定:
            MEMORY_RETRIEVAL__MODE=vector
            MEMORY_RETRIEVAL__TOP_K=10
            MEMORY_DISTILLATION__ENABLED=false
            MEMORY_PATHS__MEMORY_ROOT=/custom/path
            MEMORY_EMBED_MODEL=all-MiniLM-L6-v2
        """
        data: dict[str, Any] = {}
        for k, v in os.environ.items():
            if not k.startswith(prefix):
                continue
            key = k[len(prefix):].lower()  # e.g. "retrieval__mode"
            parts = key.split("__")
            if not parts:
                continue
            # 顶层字段
            if len(parts) == 1:
                data[parts[0]] = _coerce_env_value(v)
                continue
            # 嵌套字段
            section, field = parts[0], "__".join(parts[1:])
            if section in ("retrieval", "distillation", "paths", "safety", "compact", "cost"):
                data.setdefault(section, {})[field] = _coerce_env_value(v)
        return cls.model_validate(data)

    def to_dict(self) -> dict[str, Any]:
        """序列化为 dict（路径转为 str，便于 JSON）"""
        return self.model_dump(mode="json")

    def set_runtime(self, key: str, value: Any) -> None:
        """M10 C6.4: 运行时修改配置(不重建 agent,字段直接 in-place 改)。

        支持 dotted path:
            config.set_runtime("cost.daily_budget_usd", 0.5)
            config.set_runtime("distillation.enabled", False)
            config.set_runtime("enabled", False)  # 顶层字段

        Args:
            key: dotted path,例如 "cost.daily_budget_usd"
            value: 新值(由 Pydantic 在 setattr 时校验类型 — validate_assignment=True)

        Raises:
            KeyError: 当 key 路径上任何一段不存在(extra='forbid')
            ValidationError: 当 value 类型不匹配字段 schema(validate_assignment=True)
        """
        parts = key.split(".")
        obj: Any = self
        for p in parts[:-1]:
            if not hasattr(obj, p):
                raise KeyError(f"Unknown config path: {key!r} (no attr {p!r})")
            obj = getattr(obj, p)
        final = parts[-1]
        if not hasattr(obj, final):
            raise KeyError(f"Unknown config path: {key!r} (no attr {final!r})")
        setattr(obj, final, value)


def _coerce_env_value(v: str) -> Any:
    """env 值类型推断（true/false/int/float/原样 str）"""
    v_lower = v.strip().lower()
    if v_lower in ("true", "yes", "1", "on"):
        return True
    if v_lower in ("false", "no", "0", "off"):
        return False
    try:
        return int(v)
    except ValueError:
        pass
    try:
        return float(v)
    except ValueError:
        pass
    return v


__all__ = [
    "RetrievalConfig",
    "DistillationConfig",
    "PathsConfig",
    "SafetyConfig",
    "CostConfig",
    "MemoryConfig",
]