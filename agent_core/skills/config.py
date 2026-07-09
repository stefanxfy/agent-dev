"""
Skill 系统配置（pydantic v2）

设计要点（镜像 agent_core/memory/config.py）：
1. pydantic BaseModel，extra="forbid" + validate_assignment=True（严格契约）
2. 不污染全局 agent_core.config.Config（保持向后兼容）
3. from_env(prefix="SKILLS_")：双下划线表嵌套（如 SKILLS_PATHS__WORKSPACE_DIR）
4. Path 字段自动 expanduser()
5. 默认值取自 types.py 的 DEFAULT_* 常量（与 OpenClaw workspace.ts:124-128 对齐）

002-skill-secret-injection（T004）：
- entries: Optional[Mapping[str, SkillEntryConfig]] = None — 用户级 secret 配置
- from_yaml(path) — 纯文件读取；缺文件返 entries=None 不抛；env 覆盖由 T033 处理
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Optional

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

from agent_core.skills.env_overrides import (
    ConfigValidationError,
    SecretRef,
    SecretRefKind,
    SkillEntryConfig,
)
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
    # 002-skill-secret-injection (T004)：用户级 secret 注入配置
    # - None（默认）：legacy 模式，无 secret 注入
    # - {}：用户建了 config 文件但无 entries
    # - 非空 dict：每个 key MUST 是 loaded skill 名（T005 校验）
    entries: Optional[Any] = Field(default=None)  # Optional[Mapping[str, SkillEntryConfig]]

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

    @classmethod
    def from_yaml(cls, path: Path) -> "SkillsConfig":
        """
        从 YAML 文件构造（T004 / contract: contracts/config-file-format.md）。

        语义：
        - 文件不存在 → 返 `SkillsConfig()`（entries=None，等价 legacy mode，**不抛**）
        - 文件存在 → 读 YAML；若含 skills.entries 段 → 解析为 SkillEntryConfig 映射
          并赋给 entries 字段
        - YAML 解析错误 / schema 不符 → 抛 ConfigValidationError（fail-fast）

        不处理 env 覆盖：AGENT_CONFIG_PATH 由 T033 `load_user_config` 统一负责，
        本方法仅做"读文件 + 解析 + 构造"三件事。

        Returns:
            SkillsConfig（entries 可能为 None / {} / 完整 dict）
        """
        path = Path(path).expanduser()
        if not path.is_file():
            # 缺文件 → legacy 模式（向后兼容）
            return cls()
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f)
        except yaml.YAMLError as e:
            raise ConfigValidationError(
                f"YAML 解析失败: {path}: {e}"
            ) from e
        if not isinstance(data, dict):
            raise ConfigValidationError(
                f"YAML 顶层必须是 mapping, 实际 {type(data).__name__}: {path}"
            )

        # 提取 entries 段并解析为 SkillEntryConfig 映射
        skills_block = data.get("skills") or {}
        raw_entries = skills_block.get("entries") if isinstance(skills_block, dict) else None
        parsed_entries = _parse_entries(raw_entries)

        # 其余字段（paths/limits/load/sources）通过 model_validate 解析
        # 但 entries 已经在我们手里 → 先 validate 其他字段, 再赋值 entries
        # 否则 pydantic 会把 entries 当 dict 强校验 SkillEntryConfig schema 失败
        other = {k: v for k, v in data.items() if k != "skills"}
        cfg = cls.model_validate(other)
        if parsed_entries is not None:
            cfg.entries = parsed_entries
        return cfg


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


def _parse_secret_ref_value(raw: Any) -> SecretRef:
    """把 YAML 里的一条 secret value 转成 SecretRef。

    检测逻辑（contracts/config-file-format.md §Detection Logic）：
    - str 以前缀 "env://" 开头 → kind=ENV, value=env var 名
    - str 以前缀 "file://" 开头 → kind=FILE, value=绝对路径（去掉 file://）
    - 其它 str → kind=INLINE, value=字面值

    非 str 值 → ConfigValidationError（fail-fast at load）
    """
    if not isinstance(raw, str):
        raise ConfigValidationError(
            f"secret value 必须为字符串, 实际 {type(raw).__name__}: {raw!r}"
        )
    if raw.startswith("env://"):
        return SecretRef(kind=SecretRefKind.ENV, value=raw[len("env://"):])
    if raw.startswith("file://"):
        return SecretRef(kind=SecretRefKind.FILE, value=raw[len("file://"):])
    return SecretRef(kind=SecretRefKind.INLINE, value=raw)


def _parse_entries(raw: Any) -> Optional[dict[str, SkillEntryConfig]]:
    """YAML skills.entries 段 → dict[skill_name, SkillEntryConfig]。

    - raw=None / 空 dict → 返 None（与 SkillsConfig.entries=None legacy 模式一致）
    - raw=dict → 每 entry 解析 secrets 子字段；非 dict entry → raise
    """
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ConfigValidationError(
            f"skills.entries 必须为 mapping, 实际 {type(raw).__name__}: {raw!r}"
        )
    if not raw:
        return None  # 空 dict 等价 None（向后兼容）
    parsed: dict[str, SkillEntryConfig] = {}
    for skill_name, entry_cfg in raw.items():
        if not isinstance(skill_name, str):
            raise ConfigValidationError(
                f"skills.entries 的 key 必须为字符串, 实际 {type(skill_name).__name__}"
            )
        if entry_cfg is None:
            entry_cfg = {}
        if not isinstance(entry_cfg, dict):
            raise ConfigValidationError(
                f"skills.entries[{skill_name!r}] 必须为 mapping, "
                f"实际 {type(entry_cfg).__name__}: {entry_cfg!r}"
            )
        enabled = entry_cfg.get("enabled", True)
        if not isinstance(enabled, bool):
            raise ConfigValidationError(
                f"skills.entries[{skill_name!r}].enabled 必须为 bool, "
                f"实际 {type(enabled).__name__}: {enabled!r}"
            )
        raw_secrets = entry_cfg.get("secrets") or {}
        if not isinstance(raw_secrets, dict):
            raise ConfigValidationError(
                f"skills.entries[{skill_name!r}].secrets 必须为 mapping, "
                f"实际 {type(raw_secrets).__name__}: {raw_secrets!r}"
            )
        secrets = {
            k: _parse_secret_ref_value(v)
            for k, v in raw_secrets.items()
        }
        parsed[skill_name] = SkillEntryConfig(enabled=enabled, secrets=secrets)
    return parsed


__all__ = [
    "SkillsConfig",
    "PathsConfig",
    "LimitsConfig",
    "LoadConfig",
    "SourceConfig",
    "SourcesConfig",
]
