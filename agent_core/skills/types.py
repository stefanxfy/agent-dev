"""
Skill 系统类型定义（v1 — US1 阶段）

设计要点（镜像 agent_core/memory/types.py）：
1. Literal/Enum 表达封闭分类（SkillSource / SkillVisibility / SkillEligibilityState）
2. TypedDict 表达 frontmatter 结构（与 YAML 解析结果对接）
3. @dataclass(frozen=True) 表达 Value Object（按值比较、可哈希、支撑 INV-2 字节稳定性）
4. 所有校验为纯函数（不依赖外部状态），便于测试 + 跨进程复用
5. CURRENT_SCHEMA_VERSION 用于未来 frontmatter schema 演进

US1 阶段仅定义核心契约；US3 (T035) 扩展 SkillMetadata + visibility/eligibility 字段。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional, TypedDict


logger = logging.getLogger("agent_core.skills")


# ──────────────────────────────────────────────────────────────────
# 0. 错误类型（子类化 ValueError，兼容 caller 的简单 except）
# ──────────────────────────────────────────────────────────────────

class SkillFrontmatterError(ValueError):
    """SKILL.md frontmatter 校验失败"""


# ──────────────────────────────────────────────────────────────────
# 1. 枚举
# ──────────────────────────────────────────────────────────────────

class SkillSource(str, Enum):
    """skill 来源（v1 双层：BUNDLED < WORKSPACE）"""
    BUNDLED = "bundled"
    WORKSPACE = "workspace"

    @property
    def priority(self) -> int:
        """合并优先级：数字大者覆盖小者（WORKSPACE 覆盖 BUNDLED）"""
        return _SOURCE_PRIORITY[self]


_SOURCE_PRIORITY: dict[SkillSource, int] = {
    SkillSource.BUNDLED: 1,
    SkillSource.WORKSPACE: 2,
}


class SkillRenderMode(str, Enum):
    """available-skills 段的渲染降级模式（FR-017）"""
    FULL = "full"        # name + description + location
    COMPACT = "compact"  # name + location（去 description）
    TRUNCATE = "truncate"  # compact + 二分截断


class SkillVisibility(str, Enum):
    """skill 对模型的可见性（frontmatter `disable-model-invocation` 推导，data-model §5）"""
    MODEL_VISIBLE = "model_visible"            # 进 <available_skills> 目录表
    HIDDEN_FROM_MODEL = "hidden_from_model"    # 不进目录表，但仍登记（可 /invoke）


class SkillEligibilityState(str, Enum):
    """skill 资格状态机（即时计算，data-model §6）"""
    ELIGIBLE = "eligible"                          # 满足所有 requires（或 always）
    DISABLED = "disabled"                          # config 显式禁用（v1 预留：无 per-skill config）
    MISSING_REQUIREMENTS = "missing_requirements"  # requires 未满足
    ERRORED = "errored"                            # 加载失败（load_error 非 None）


# ──────────────────────────────────────────────────────────────────
# 2. frontmatter TypedDict（与 YAML 解析结果对接）
# ──────────────────────────────────────────────────────────────────

class SkillRequiresDict(TypedDict, total=False):
    """metadata.requires 子结构（YAML anyBins → any_bins）"""
    bins: list[str]
    anyBins: list[str]
    env: list[str]
    config: list[str]


class SkillMetadataDict(TypedDict, total=False):
    """metadata 子结构（contracts/skill-md-format.md §4）"""
    always: bool
    os: list[str]
    requires: SkillRequiresDict


class SkillFrontmatter(TypedDict, total=False):
    """SKILL.md 顶部 YAML frontmatter 结构（v1 子集，对齐 contracts/skill-md-format.md）"""
    name: str
    description: str
    homepage: Optional[str]
    # US3 (T036) 扩展：
    disable_model_invocation: bool   # YAML: disable-model-invocation
    user_invocable: bool             # YAML: user-invocable
    metadata: SkillMetadataDict


# ──────────────────────────────────────────────────────────────────
# 2b. metadata Value Object（纯函数从 dict 构造）
# ──────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class SkillRequires:
    """requires 五维（contracts §4）：bins/anyBins/env/config（os 在 SkillMetadata 顶层）"""
    bins: tuple[str, ...] = ()
    any_bins: tuple[str, ...] = ()
    env: tuple[str, ...] = ()
    config: tuple[str, ...] = ()


@dataclass(frozen=True)
class SkillMetadata:
    """skill metadata（always / os 白名单 / requires）"""
    always: bool = False
    os: tuple[str, ...] = ()
    requires: SkillRequires = field(default_factory=SkillRequires)


def _as_str_tuple(v: Any) -> tuple[str, ...]:
    """容错把 YAML 值转为 str 元组（str / list / None）"""
    if v is None:
        return ()
    if isinstance(v, str):
        return (v,) if v else ()
    if isinstance(v, (list, tuple)):
        return tuple(str(x) for x in v if x is not None)
    return ()


def parse_metadata(raw: Any) -> SkillMetadata:
    """
    从 frontmatter `metadata` dict 构造 SkillMetadata（纯函数，容错）。

    未知字段忽略（前向兼容）；类型不符走默认值（不抛）。
    """
    if not isinstance(raw, dict):
        return SkillMetadata()
    always = bool(raw.get("always", False))
    os_tuple = _as_str_tuple(raw.get("os"))
    req_raw = raw.get("requires")
    if not isinstance(req_raw, dict):
        req_raw = {}
    requires = SkillRequires(
        bins=_as_str_tuple(req_raw.get("bins")),
        any_bins=_as_str_tuple(req_raw.get("anyBins")),
        env=_as_str_tuple(req_raw.get("env")),
        config=_as_str_tuple(req_raw.get("config")),
    )
    return SkillMetadata(always=always, os=os_tuple, requires=requires)


def derive_visibility(disable_model_invocation: bool) -> SkillVisibility:
    """从 frontmatter `disable-model-invocation` 推导 visibility（data-model §5）"""
    if disable_model_invocation:
        return SkillVisibility.HIDDEN_FROM_MODEL
    return SkillVisibility.MODEL_VISIBLE


# ──────────────────────────────────────────────────────────────────
# 3. Value Object（frozen dataclass）
# ──────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Skill:
    """单个 skill 的核心契约（不可变 Value Object）"""
    name: str
    description: str
    file_path: str          # SKILL.md 绝对路径（prompt 中 <location>）
    base_dir: str           # skill 目录（file_path 父目录），解析 body 相对路径用
    source: SkillSource


@dataclass(frozen=True)
class SkillEntry:
    """已发现 skill 的运行时记录（加载 + 元数据 + US3 的 eligibility/visibility）"""
    skill: Skill
    load_error: Optional[str] = None  # 非 None 表示加载失败（malformed/超限/缺 desc）
    # US2 (T029)：body 中 references/scripts/assets/ 相对引用解析后的绝对路径元组
    # （确定性：去重 + 字典序；contracts/skill-md-format.md §5.2）
    body_references: tuple[str, ...] = ()
    # US3 (T035)：触发控制 + 资格（frontmatter 推导 / snapshot 即时求值）
    metadata: Optional[SkillMetadata] = None
    disable_model_invocation: bool = False
    user_invocable: bool = True
    visibility: SkillVisibility = SkillVisibility.MODEL_VISIBLE
    # eligibility：load 时默认 ELIGIBLE（占位）；snapshot 经 evaluate_eligibility 即时重算
    # （data-model §6：eligibility 不持久化，每次 snapshot 基于当前 env/config 求值）
    eligibility: SkillEligibilityState = SkillEligibilityState.ELIGIBLE


@dataclass(frozen=True)
class SkillSummary:
    """snapshot 摘要项（供 status/调试）"""
    name: str
    source: SkillSource
    eligible: bool = True
    missing: tuple[str, ...] = ()
    # Polish (T053)：供 format_skill_status / slash 派发的触发控制信息
    visibility: SkillVisibility = SkillVisibility.MODEL_VISIBLE
    user_invocable: bool = True
    eligibility_state: SkillEligibilityState = SkillEligibilityState.ELIGIBLE
    load_error: Optional[str] = None
    # Convergence (T064)：body 相对引用解析后的绝对路径（供 format_skill_status 作者诊断）
    body_references: tuple[str, ...] = ()


@dataclass
class SkillSnapshot:
    """一次 snapshot 构建的完整结果（注入 handler 消费）"""
    prompt: str                              # 已渲染的 ## Skills 段（含 ⚠️ 警告，若有）
    skills: list[SkillSummary] = field(default_factory=list)
    version: int = 0                         # 单调递增（mtime 失效时 bump）
    render_mode: SkillRenderMode = SkillRenderMode.FULL
    truncated_count: int = 0                 # truncate 模式下被截掉的 skill 数


# ──────────────────────────────────────────────────────────────────
# 4. 常量
# ──────────────────────────────────────────────────────────────────

CURRENT_SCHEMA_VERSION: int = 1

# 默认预算上限（取自 OpenClaw workspace.ts:124-128，可在 SkillsConfig 覆盖）
DEFAULT_MAX_SKILL_FILE_BYTES = 256_000        # 256 KB
DEFAULT_MAX_SKILLS_IN_PROMPT = 150
DEFAULT_MAX_SKILLS_PROMPT_CHARS = 18_000
DEFAULT_MAX_CANDIDATES_PER_ROOT = 300


# ──────────────────────────────────────────────────────────────────
# 5. 纯校验函数
# ──────────────────────────────────────────────────────────────────

def validate_frontmatter(data: Any, *, fallback_name: Optional[str] = None) -> SkillFrontmatter:
    """
    校验 SKILL.md frontmatter dict。

    规则（contracts/skill-md-format.md）：
    - description MUST 非空字符串（缺则抛 SkillFrontmatterError）
    - name 缺时回退 fallback_name（目录名）；非空字符串
    - homepage 若给须是 str
    - 未知字段前向兼容忽略（US2 T031 测试）

    Args:
        data: YAML 解析结果（dict）
        fallback_name: name 缺失时的回退（通常是目录名）

    Returns:
        合法的 SkillFrontmatter
    """
    if not isinstance(data, dict):
        raise SkillFrontmatterError(
            f"frontmatter 必须是 dict，实际为 {type(data).__name__}"
        )

    # description 必填（FR-003）
    desc = data.get("description")
    if not isinstance(desc, str) or not desc.strip():
        raise SkillFrontmatterError(
            "description 必填且为非空字符串（FR-003）；"
            "它是模型唯一的触发依据"
        )
    data["description"] = desc.strip()

    # name 缺失回退目录名（FR-003）
    name = data.get("name")
    if not isinstance(name, str) or not name.strip():
        if not fallback_name:
            raise SkillFrontmatterError(
                "name 缺失且无 fallback_name 可用"
            )
        name = fallback_name
        logger.debug("🧩 frontmatter name fallback: skill=%s", name)
    data["name"] = name.strip()

    # homepage 可选
    if "homepage" in data and data["homepage"] is not None:
        if not isinstance(data["homepage"], str):
            raise SkillFrontmatterError(
                f"homepage 必须是 str，实际为 {type(data['homepage']).__name__}"
            )

    # US3 (T036)：disable-model-invocation / user-invocable（kebab → snake + 容错默认）
    # 契约 §3：非法值 → disable 按 false、user-invocable 按 true
    dmi = data.get("disable-model-invocation", data.get("disable_model_invocation", False))
    data["disable_model_invocation"] = _as_bool(dmi, default=False)
    ui = data.get("user-invocable", data.get("user_invocable", True))
    data["user_invocable"] = _as_bool(ui, default=True)

    # US3 (T036)：metadata（dict；非 dict → 视为空，parse_metadata 容错）
    raw_meta = data.get("metadata")
    if raw_meta is not None and not isinstance(raw_meta, dict):
        logger.debug("🧩 frontmatter metadata 非 dict，忽略: %s", type(raw_meta).__name__)
        data["metadata"] = {}
    # 预解析为 SkillMetadata 并塞回（loader 直接取用）
    data["metadata"] = parse_metadata(data.get("metadata"))

    return data  # type: ignore[return-value]


def _as_bool(v: Any, *, default: bool) -> bool:
    """容错把 YAML 值转为 bool（非法 → default；契约 §3）"""
    if isinstance(v, bool):
        return v
    if v is None:
        return default
    if isinstance(v, str):
        if v.lower() in ("true", "yes", "1"):
            return True
        if v.lower() in ("false", "no", "0"):
            return False
    return default


__all__ = [
    "SkillFrontmatterError",
    "SkillSource",
    "SkillRenderMode",
    "SkillVisibility",
    "SkillEligibilityState",
    "SkillFrontmatter",
    "SkillRequiresDict",
    "SkillMetadataDict",
    "SkillRequires",
    "SkillMetadata",
    "parse_metadata",
    "derive_visibility",
    "Skill",
    "SkillEntry",
    "SkillSummary",
    "SkillSnapshot",
    "CURRENT_SCHEMA_VERSION",
    "DEFAULT_MAX_SKILL_FILE_BYTES",
    "DEFAULT_MAX_SKILLS_IN_PROMPT",
    "DEFAULT_MAX_SKILLS_PROMPT_CHARS",
    "DEFAULT_MAX_CANDIDATES_PER_ROOT",
    "validate_frontmatter",
]
