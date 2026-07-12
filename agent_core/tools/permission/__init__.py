"""agent_core.tools.permission 子包 facade(D.2d)。

设计契约:
  - 新代码应 `from agent_core.tools.permission import X`(本 facade)或显式子模块路径
  - 旧扁平路径 `agent_core.tools.permission_X` 已全部迁移完毕(D.2e/f 完成),
    不再保留兼容 shim;`tools/__init__.py` 不 re-export 权限符号

完成内容:
  - re-export 所有 stable public API(11 个子模块)
  - 让 `from agent_core.tools.permission import PermissionEngine` 等直接可用
"""

# ── types ──
from agent_core.tools.permission.types import (
    PermissionBehavior,
    PermissionMode,
    PermissionRuleSource,
    PermissionRule,
    PermissionRuleValue,
    PermissionRuleData,
    PermissionDecision,
    AdditionalWorkingDirectory,
    ToolPermissionContext,
    RuleReason,
    ModeReason,
    ClassifierReason,
    SafetyCheckReason,
    AsyncAgentReason,
    OtherReason,
    SubcommandResultsReason,
    PermissionPromptReason,
    HookReason,
    SandboxOverrideReason,
    WorkingDirReason,
)

# ── engine ──
from agent_core.tools.permission.engine import PermissionEngine

# ── matcher ──
from agent_core.tools.permission.matcher import (
    ShellPermissionRule,
    parse_permission_rule,
    match_permission_rule,
    match_wildcard_pattern,
    permission_rule_extract_prefix,
    matching_rules_for_input,
    parse_all_rules_from_strings,
)

# ── denial ──
from agent_core.tools.permission.denial import (
    DENIAL_LIMITS,
    DenialTrackingState,
    record_denial,
    record_success,
    handle_denial_limit_exceeded,
    check_denial_limit,
    record_denial_for_session,
    record_success_for_session,
    get_denial_state,
    set_denial_state,
    reset_denial_state,
    clear_all_denial_states,
)

# ── classifier ──
from agent_core.tools.permission.classifier import (
    ClassifierResult,
    HaikuClassifier,
    is_classifier_enabled,
    start_speculative_classifier_check,
    SpeculativeClassifierHandle,
)

# ── fast_path ──
from agent_core.tools.permission.fast_path import (
    FastPathResult,
    check_classifier_fast_path,
    is_auto_mode_allowlisted_tool,
    is_fast_path_disabled_tool,
)

# ── loader ──
from agent_core.tools.permission.loader import (
    get_settings_path,
    get_local_settings_path,
    load_settings_json,
    is_managed_only,
    load_rules_by_source,
    get_permission_rules_for_source,
    load_all_permission_rules_from_disk,
    load_tool_permission_context,
    add_permission_rules_to_settings,
    delete_permission_rule_from_settings,
    save_excluded_commands,
    load_excluded_commands,
)

# ── ui ──
from agent_core.tools.permission.ui import (
    render_rule_preview,
    build_permission_rule,
    format_rules_by_source,
)

# ── bash ──
from agent_core.tools.permission.bash import (
    Subcommand,
    parse_subcommands,
    strip_safe_wrappers,
    is_cd_command,
    is_read_only,
    bash_check_permissions,
    check_sandbox_auto_allow,
)

# ── hook ──
from agent_core.tools.permission.hook import (
    PreToolUseResult,
    HookRegistry,
    PermissionRequestResult,
    PermissionDeniedResult,
    default_hooks,
    default_secret_hook,
    default_path_validation_hook,
    make_webhook_permission_request_hook,
    make_retry_hint_denied_hook,
)

# ── safety ──
from agent_core.tools.permission.safety import (
    is_sensitive_path,
    contains_secret,
    safety_check,
    normalize_path_for_check,
)