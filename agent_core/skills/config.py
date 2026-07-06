"""
Skill 系统配置（pydantic v2）

设计要点（镜像 agent_core/memory/config.py）：
1. pydantic BaseModel，extra="forbid" + validate_assignment=True（严格契约）
2. 不污染全局 agent_core.config.Config（保持向后兼容）
3. from_env(prefix="SKILLS_")：双下划线表嵌套（如 SKILLS_PATHS__WORKSPACE_DIR）
4. Path 字段自动 expanduser()
5. 默认值取自 types.py 的 DEFAULT_* 常量（与 OpenClaw workspace.ts:124-128 对齐）
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from agent_core.skills.types import (
    DEFAULT_MAX_CANDIDATES_PER_ROOT,
    DEFAULT_MAX_SKILL_FILE_BYTES,
    DEFAULT_MAX_SKILLS_IN_PROMPT,
    DEFAULT_MAX_SKILLS_PROMPT_CHARS,
    SkillSource,
)


def _default_workspace_dir() -> Path:
    """
    workspace 默认目录（plan.md:23 + data-model §2，镜像 session/storage.py:88-93）。

    - 项目根（cwd 含 .git / agent_core）→ cwd/skills（项目级 skill）
    - 否则回退 ~/.agent_data/skills（用户级 skill）
    - AGENT_DATA_DIR env 覆盖家目录基（→ $AGENT_DATA_DIR/skills）

    项目根判定用标记文件而非 skills/ 是否存在，故新项目（尚未建 skills/）仍解析到
    项目路径（scan_source 遇空目录返回 []）。
    """
    cwd = Path.cwd()
    if (cwd / ".git").exists() or (cwd / "agent_core").exists():
        return cwd / "skills"
    base = os.environ.get("AGENT_DATA_DIR")
    if base:
        return Path(base) / "skills"
    # Path.home() 已展开 ~（尊重 $HOME）；default_factory 产出不经 _expand validator
    # （pydantic v2 validate_default=False），故在此直接展开
    return Path.home() / ".agent_data" / "skills"


# ──────────────────────────────────────────────────────────────────
# 嵌套配置
# ──────────────────────────────────────────────────────────────────

class PathsConfig(BaseModel):
    """skill 来源目录解析"""
    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    # bundled：包内 builtin 目录（__file__-relative，location-independent）
    # 注：memory/ 用 CWD-relative（memory/config.py:118），但 bundled asset
    # 必须随包定位，故此处用 __file__（有意偏离，记录在 plan §E）。
    bundled_dir: Path = Field(
        default_factory=lambda: Path(__file__).parent / "builtin"
    )
    # workspace：项目 ./skills 优先；非项目 cwd 回退 ~/.agent_data/skills
    # （plan.md:23 + data-model §2，镜像 session/storage.py:88-93）
    workspace_dir: Path = Field(default_factory=_default_workspace_dir)

    @field_validator("bundled_dir", "workspace_dir")
    @classmethod
    def _expand(cls, v: Path) -> Path:
        return v.expanduser()


class LimitsConfig(BaseModel):
    """预算与上限（FR-004/017，默认值对齐 OpenClaw）"""
    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    max_skill_file_bytes: int = DEFAULT_MAX_SKILL_FILE_BYTES
    max_skills_in_prompt: int = DEFAULT_MAX_SKILLS_IN_PROMPT
    max_skills_prompt_chars: int = DEFAULT_MAX_SKILLS_PROMPT_CHARS
    max_candidates_per_root: int = DEFAULT_MAX_CANDIDATES_PER_ROOT


class LoadConfig(BaseModel):
    """加载行为开关"""
    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    enabled: bool = True  # 全局开关（false → 不注入 skills 段）


# ──────────────────────────────────────────────────────────────────
# US5 (T048)：来源优先级（配置数据 > 对象模式，架构 §C）
# ──────────────────────────────────────────────────────────────────

class SourceConfig(BaseModel):
    """单个 skill 来源（source 名 + 目录 + 优先级；高 priority 覆盖低）"""
    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    source: str           # "bundled" | "workspace"（SkillSource.value）
    dir: Path
    priority: int = 0     # 数字大者覆盖小者（data-model §2）

    @field_validator("dir")
    @classmethod
    def _expand(cls, v: Path) -> Path:
        return v.expanduser()

    @field_validator("source")
    @classmethod
    def _lower(cls, v: str) -> str:
        return v.strip().lower()


class SourcesConfig(BaseModel):
    """有序来源列表（加源 = 加一行配置，不动合并纯函数；架构 §C）"""
    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    items: list[SourceConfig] = Field(default_factory=list)

    @property
    def ordered(self) -> list[SourceConfig]:
        """按 priority 升序（低先扫、高覆盖）"""
        return sorted(self.items, key=lambda s: s.priority)


# ──────────────────────────────────────────────────────────────────
# 顶层配置
# ──────────────────────────────────────────────────────────────────

class SkillsConfig(BaseModel):
    """Skill 系统配置入口"""
    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    paths: PathsConfig = Field(default_factory=PathsConfig)
    limits: LimitsConfig = Field(default_factory=LimitsConfig)
    load: LoadConfig = Field(default_factory=LoadConfig)
    # US5 (T048)：显式来源列表（可选；None → 从 paths 派生 bundled+workspace 双层）
    sources: Optional[SourcesConfig] = None

    def sources_list(self) -> list[SourceConfig]:
        """
        返回有序来源（priority 升序）。

        - 显式配置 sources → 用之（支持 >2 层自定义来源）
        - 否则从 paths 派生默认双层：bundled(1) < workspace(2)（data-model §2）
        """
        if self.sources is not None and self.sources.items:
            return self.sources.ordered
        return [
            SourceConfig(
                source=SkillSource.BUNDLED.value,
                dir=self.paths.bundled_dir,
                priority=1,
            ),
            SourceConfig(
                source=SkillSource.WORKSPACE.value,
                dir=self.paths.workspace_dir,
                priority=2,
            ),
        ]

    # ── loaders ────────────────────────────────────────────────

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SkillsConfig":
        """从 dict 构造（如 YAML/JSON 配置文件）"""
        return cls.model_validate(data)

    @classmethod
    def from_env(cls, prefix: str = "SKILLS_") -> "SkillsConfig":
        """
        从环境变量构造（双下划线表嵌套）。

        示例：
            SKILLS_PATHS__WORKSPACE_DIR=/x/skills
            SKILLS_LIMITS__MAX_SKILLS_PROMPT_CHARS=10000
            SKILLS_LOAD__ENABLED=false

        未设置的项走默认值。
        """
        nested: dict[str, Any] = {}
        for key, value in os.environ.items():
            if not key.startswith(prefix):
                continue
            remaining = key[len(prefix):]  # e.g. PATHS__WORKSPACE_DIR
            if not remaining:
                continue
            # 双下划线拆嵌套
            parts = remaining.lower().split("__")
            cursor = nested
            for part in parts[:-1]:
                cursor = cursor.setdefault(part, {})
            cursor[parts[-1]] = _coerce(value)
        if not nested:
            return cls()
        return cls.model_validate(nested)


def _coerce(raw: str) -> Any:
    """把 env 字符串尽量转成合适的 Python 类型"""
    s = raw.strip()
    if s.lower() in ("true", "yes", "1"):
        return True
    if s.lower() in ("false", "no", "0"):
        return False
    try:
        return int(s)
    except ValueError:
        pass
    return s


__all__ = [
    "SkillsConfig",
    "PathsConfig",
    "LimitsConfig",
    "LoadConfig",
    "SourceConfig",
    "SourcesConfig",
]
