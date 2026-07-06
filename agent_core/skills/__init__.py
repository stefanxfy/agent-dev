"""
Skill 系统（v1 — 完整核心）

按需加载的领域指令包：每个 skill 是含 SKILL.md（YAML frontmatter + markdown body）
的目录；系统把"可用 skill 目录表"（name + description + 文件 location）注入 system
prompt，由 LLM 在任务匹配时通过 Read 工具按需读取 SKILL.md 并遵循其专门指令。
同时支持用户在 UI 中 `/skill-name <args>` 显式触发。

设计来源：specs/001-skill-system/（spec / plan / data-model / contracts）。
镜像 agent_core/memory/ 子系统约定（barrel + types + pydantic config + 文件后端 store）。

公开 API 见 ``__all__``；设计文档见 docs/agent_core-skill-system-design.md。
"""

# ── Entity 层（数据契约 + 纯函数）──────────────────────────────────
from agent_core.skills.types import (
    CURRENT_SCHEMA_VERSION,
    Skill,
    SkillEligibilityState,
    SkillEntry,
    SkillFrontmatter,
    SkillMetadata,
    SkillRequires,
    SkillRenderMode,
    SkillSnapshot,
    SkillSource,
    SkillSummary,
    SkillVisibility,
    derive_visibility,
    parse_metadata,
    validate_frontmatter,
)

# ── Framework/Adapter/UseCase 层 ──────────────────────────────────
from agent_core.skills.commands import resolve_skill_command, sanitize_skill_command_name
from agent_core.skills.config import (
    LimitsConfig,
    LoadConfig,
    PathsConfig,
    SkillsConfig,
    SourceConfig,
    SourcesConfig,
)
from agent_core.skills.eligibility import evaluate_eligibility
from agent_core.skills.frontmatter import ParsedSkillMd, parse_skill_md
from agent_core.skills.prompt import escape_xml, render_skills_section
from agent_core.skills.registry import SkillsRegistry
from agent_core.skills.skill_index import (
    load_all,
    merge_by_priority,
    scan_source,
    sort_by_name,
)
from agent_core.skills.skill_store import load_single_skill, resolve_body_references
from agent_core.skills.snapshot import build_snapshot
from agent_core.skills.status import format_skill_status

__all__ = [
    # types
    "CURRENT_SCHEMA_VERSION",
    "Skill",
    "SkillEntry",
    "SkillSummary",
    "SkillSnapshot",
    "SkillSource",
    "SkillVisibility",
    "SkillEligibilityState",
    "SkillRenderMode",
    "SkillFrontmatter",
    "SkillMetadata",
    "SkillRequires",
    "validate_frontmatter",
    "parse_metadata",
    "derive_visibility",
    # config
    "SkillsConfig",
    "PathsConfig",
    "LimitsConfig",
    "LoadConfig",
    "SourceConfig",
    "SourcesConfig",
    # frontmatter / store / index
    "parse_skill_md",
    "ParsedSkillMd",
    "load_single_skill",
    "resolve_body_references",
    "scan_source",
    "sort_by_name",
    "merge_by_priority",
    "load_all",
    # use case
    "evaluate_eligibility",
    "build_snapshot",
    "render_skills_section",
    "escape_xml",
    "resolve_skill_command",
    "sanitize_skill_command_name",
    "format_skill_status",
    # adapter
    "SkillsRegistry",
]
