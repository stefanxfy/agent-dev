"""
Permission Loader — settings.json 加载 + 8 source 合并

对齐 Claude Code:
- src/utils/permissions/permissions.ts(loadAllPermissionRulesFromDisk)
- src/utils/permissions/permissions.ts(loadToolPermissionContext)
- src/utils/settings/settings.ts(settings.json 解析)
- doc §4.6 settings.json schema + §4.6.1 managed-only mode

核心设计:
1. **8 source 合并顺序**(按 PermissionRuleSource.ordered_sources() 优先级):
   projectSettings → localSettings → userSettings → ... → flagSettings
2. **settings.json schema**(对齐 doc §4.6):
   ```json
   {
     "permissions": {
       "allow": ["Edit", "Read(./docs/**)"],
       "deny":  ["Bash(rm:*)"],
       "ask":   ["Bash(npm publish:*)"],
       "additionalDirectories": ["../shared-lib"]
     }
   }
   ```
3. **Managed-only mode**:env `AGENT_MANAGED_PERMISSIONS_ONLY=true` → 只读 policySettings
4. **Corrupted JSON 容忍**:解析失败返空 dict + warn,不抛异常
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Optional

from .permission_matcher import parse_all_rules_from_strings
from .permission_types import (
    AdditionalWorkingDirectory,
    PermissionBehavior,
    PermissionRule,
    PermissionRuleSource,
    ToolPermissionContext,
    ToolPermissionRulesBySource,
)


logger = logging.getLogger(__name__)


# ────────────────────────────────────────────────────────────────────
# 默认 settings 路径
# ────────────────────────────────────────────────────────────────────

def get_settings_path() -> Path:
    """
    解析 settings.json 路径(对齐 doc §4.6 + env-var fallback)

    优先级:
      1. env AGENT_SETTINGS_PATH(直接覆盖)
      2. config.agent_data_dir / "settings.json"
      3. ~/.agent_data/settings.json

    Returns:
        Path 对象(可能不存在)
    """
    # 1. env 直接覆盖
    env_override = os.environ.get("AGENT_SETTINGS_PATH", "").strip()
    if env_override:
        return Path(env_override).expanduser()

    # 2. config.agent_data_dir(支持项目级 + 用户级)
    try:
        from agent_core.config import config
        data_dir = config.agent_data_dir
        if data_dir:
            return Path(data_dir) / "settings.json"
    except ImportError:
        pass

    # 3. 默认 ~/.agent_data/settings.json
    return Path.home() / ".agent_data" / "settings.json"


def get_local_settings_path() -> Path:
    """local settings 路径(.agent_data/settings.local.json,gitignored)"""
    return get_settings_path().with_name("settings.local.json")


# ────────────────────────────────────────────────────────────────────
# settings.json 读取
# ────────────────────────────────────────────────────────────────────

def load_settings_json(settings_path: Optional[Path] = None) -> dict:
    """
    读取并解析 settings.json(容忍 corrupted JSON)

    Args:
        settings_path: 路径(默认 = get_settings_path())

    Returns:
        settings dict(解析失败或不存在 → 返空 dict)
    """
    path = settings_path or get_settings_path()
    if not path.exists():
        return {}

    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            logger.warning("settings.json 顶层不是 dict: %s", path)
            return {}
        return data
    except json.JSONDecodeError as e:
        logger.warning("settings.json 解析失败,返空 dict: %s — %s", path, e)
        return {}
    except OSError as e:
        logger.warning("settings.json 读取失败: %s — %s", path, e)
        return {}


# ────────────────────────────────────────────────────────────────────
# Managed-only mode 判定
# ────────────────────────────────────────────────────────────────────

def is_managed_only() -> bool:
    """
    检查是否启用 managed-only mode(env AGENT_MANAGED_PERMISSIONS_ONLY)

    Managed-only mode:只读取 policySettings,忽略所有其他 source。
    用于企业 / 多用户部署。
    """
    return os.environ.get("AGENT_MANAGED_PERMISSIONS_ONLY", "").strip().lower() in (
        "1", "true", "yes", "on",
    )


# ────────────────────────────────────────────────────────────────────
# 8 source 合并
# ────────────────────────────────────────────────────────────────────

# source → settings.json file 名映射
_SOURCE_TO_FILE = {
    PermissionRuleSource.PROJECT: "settings.json",
    PermissionRuleSource.LOCAL: "settings.local.json",
    PermissionRuleSource.USER: "settings.json",  # user 级也是 settings.json(在 ~/.agent_data/)
}


def _empty_source_dict() -> ToolPermissionRulesBySource:
    """构造 8 source 全空 dict"""
    return {s.value: [] for s in PermissionRuleSource}


def _parse_settings_dict(settings: dict, source: PermissionRuleSource) -> ToolPermissionRulesBySource:
    """
    把单个 settings.json dict 转成 8 source rules dict

    settings 结构:
        {
          "permissions": {
            "allow": [...],
            "deny":  [...],
            "ask":   [...]
          }
        }

    Returns:
        {source.value: [rule_strings]} — 仅填入指定 source,其他 source 留 []
    """
    result = _empty_source_dict()
    if not settings:
        return result

    perms = settings.get("permissions", {})
    if not isinstance(perms, dict):
        return result

    allow = perms.get("allow", [])
    deny = perms.get("deny", [])
    ask = perms.get("ask", [])

    if isinstance(allow, list):
        result[source.value] = [str(x) for x in allow if isinstance(x, str)]
    if isinstance(deny, list):
        # 用 "_DENY_" 占位避免冲突(实际写到 always_deny_rules)
        pass

    return result


def load_rules_by_source() -> dict[str, dict[str, list[str]]]:
    """
    从磁盘加载所有 source 的 rules(原始字符串形态)

    Returns:
        {
          "always_allow_rules": {source: [rule_strings]},
          "always_deny_rules":  {source: [rule_strings]},
          "always_ask_rules":   {source: [rule_strings]},
        }
    """
    if is_managed_only():
        # Managed-only:只读 policy 目录(env AGENT_POLICY_PATH)
        policy_path = Path(os.environ.get("AGENT_POLICY_PATH", "/etc/agent/policy/settings.json"))
        settings = load_settings_json(policy_path) if policy_path.exists() else {}
        # 整个 dict 都标为 policySettings source
        allow_rules = _parse_settings_dict(settings, PermissionRuleSource.POLICY)
        deny_rules_str = settings.get("permissions", {}).get("deny", []) if settings else []
        ask_rules_str = settings.get("permissions", {}).get("ask", []) if settings else []

        result_allow = _empty_source_dict()
        result_deny = _empty_source_dict()
        result_ask = _empty_source_dict()
        result_allow[PermissionRuleSource.POLICY.value] = allow_rules[PermissionRuleSource.POLICY.value]
        result_deny[PermissionRuleSource.POLICY.value] = [str(x) for x in deny_rules_str if isinstance(x, str)]
        result_ask[PermissionRuleSource.POLICY.value] = [str(x) for x in ask_rules_str if isinstance(x, str)]

        return {
            "always_allow_rules": result_allow,
            "always_deny_rules": result_deny,
            "always_ask_rules": result_ask,
        }

    # 常规模式:合并 projectSettings + localSettings + userSettings
    project_path = get_settings_path()
    local_path = get_local_settings_path()
    user_path = Path.home() / ".agent_data" / "settings.json"

    project_settings = load_settings_json(project_path)
    local_settings = load_settings_json(local_path) if local_path.exists() else {}
    # user_settings 用 ~/.agent_data/settings.json(如果与 project 路径不同)
    user_settings = {}
    if user_path.resolve() != project_path.resolve():
        user_settings = load_settings_json(user_path) if user_path.exists() else {}

    result_allow = _empty_source_dict()
    result_deny = _empty_source_dict()
    result_ask = _empty_source_dict()

    # 合并顺序:projectSettings < localSettings < userSettings(按优先级)
    # projectSettings 优先被读,userSettings 在它的 source dict 中覆盖(实际 CC 的优先级是 user > project,
    # 但 8 source 内已经有顺序,这里我们按文件读 + 标 source)
    for source, settings in [
        (PermissionRuleSource.PROJECT, project_settings),
        (PermissionRuleSource.LOCAL, local_settings),
        (PermissionRuleSource.USER, user_settings),
    ]:
        if not settings:
            continue
        perms = settings.get("permissions", {})
        if not isinstance(perms, dict):
            continue
        allow = perms.get("allow", [])
        deny = perms.get("deny", [])
        ask = perms.get("ask", [])

        if isinstance(allow, list):
            result_allow[source.value] = [str(x) for x in allow if isinstance(x, str)]
        if isinstance(deny, list):
            result_deny[source.value] = [str(x) for x in deny if isinstance(x, str)]
        if isinstance(ask, list):
            result_ask[source.value] = [str(x) for x in ask if isinstance(x, str)]

    return {
        "always_allow_rules": result_allow,
        "always_deny_rules": result_deny,
        "always_ask_rules": result_ask,
    }


# ────────────────────────────────────────────────────────────────────
# 解析为 PermissionRule list
# ────────────────────────────────────────────────────────────────────

def get_permission_rules_for_source(
    source: PermissionRuleSource,
    behavior: PermissionBehavior,
) -> list[PermissionRule]:
    """
    从磁盘加载指定 source + behavior 的 PermissionRule 列表
    """
    rules_by_source = load_rules_by_source()
    key = {
        PermissionBehavior.ALLOW: "always_allow_rules",
        PermissionBehavior.DENY: "always_deny_rules",
        PermissionBehavior.ASK: "always_ask_rules",
    }[behavior]

    rule_strings = rules_by_source[key].get(source.value, [])
    return parse_all_rules_from_strings(rule_strings, behavior, source)


def load_all_permission_rules_from_disk() -> list[PermissionRule]:
    """
    加载磁盘上所有 source 的所有 rule(扁平 list)

    按 PermissionRuleSource.ordered_sources() 优先级排序
    """
    if is_managed_only():
        # Managed-only:仅返 policy settings 的所有 rules
        result = []
        for behavior in [PermissionBehavior.ALLOW, PermissionBehavior.DENY, PermissionBehavior.ASK]:
            result.extend(
                get_permission_rules_for_source(PermissionRuleSource.POLICY, behavior)
            )
        return result

    result = []
    for source in PermissionRuleSource.ordered_sources():
        # session / command / cliArg / flag 都是内存级,跳过磁盘加载
        if source in (
            PermissionRuleSource.SESSION,
            PermissionRuleSource.COMMAND,
            PermissionRuleSource.CLI_ARG,
            PermissionRuleSource.FLAG,
        ):
            continue
        for behavior in [PermissionBehavior.ALLOW, PermissionBehavior.DENY, PermissionBehavior.ASK]:
            result.extend(get_permission_rules_for_source(source, behavior))
    return result


# ────────────────────────────────────────────────────────────────────
# ToolPermissionContext 构造
# ────────────────────────────────────────────────────────────────────

def load_tool_permission_context(
    mode: Optional[str] = None,
    sandbox_enabled: bool = False,
) -> ToolPermissionContext:
    """
    加载完整 ToolPermissionContext(对齐 CC loadToolPermissionContext)

    Args:
        mode: 权限模式(None = 从 env AGENT_PERMISSION_MODE 读,默认 'default')
        sandbox_enabled: 是否启用 sandbox(M2 才用)

    Returns:
        ToolPermissionContext 实例
    """
    # 1. 解析 mode
    if mode is None:
        mode = os.environ.get("AGENT_PERMISSION_MODE", "default").strip().lower() or "default"

    # 2. 加载 rules
    rules = load_rules_by_source()

    # 3. 检查是否有任何 settings 命中(no_settings_match 判定)
    has_any_settings = any(
        rules["always_allow_rules"].get(s.value)
        or rules["always_deny_rules"].get(s.value)
        or rules["always_ask_rules"].get(s.value)
        for s in (
            PermissionRuleSource.PROJECT,
            PermissionRuleSource.LOCAL,
            PermissionRuleSource.USER,
            PermissionRuleSource.POLICY,
        )
    )

    # 4. 加载 additional directories
    project_path = get_settings_path()
    project_settings = load_settings_json(project_path)
    additional_dirs: dict[str, AdditionalWorkingDirectory] = {}
    if isinstance(project_settings.get("permissions"), dict):
        for dir_str in project_settings["permissions"].get("additionalDirectories", []):
            if isinstance(dir_str, str):
                additional_dirs[dir_str] = AdditionalWorkingDirectory(
                    path=dir_str,
                    source=PermissionRuleSource.PROJECT.value,
                    reason="from settings.json additionalDirectories",
                )

    # 5. 构造 context
    return ToolPermissionContext(
        mode=mode,
        additional_working_directories=additional_dirs,
        always_allow_rules=rules["always_allow_rules"],
        always_deny_rules=rules["always_deny_rules"],
        always_ask_rules=rules["always_ask_rules"],
        no_settings_match=not has_any_settings,
        sandbox_enabled=sandbox_enabled,
        # 其他字段保持默认值
    )


# ────────────────────────────────────────────────────────────────────
# settings.json 写入(添加 / 删除 rule)
# ────────────────────────────────────────────────────────────────────

def _settings_for_destination(
    destination: PermissionRuleSource,
) -> Path:
    """destination source → settings.json 路径"""
    if destination == PermissionRuleSource.PROJECT:
        return get_settings_path()
    if destination == PermissionRuleSource.LOCAL:
        return get_local_settings_path()
    if destination == PermissionRuleSource.USER:
        return Path.home() / ".agent_data" / "settings.json"
    raise ValueError(f"不支持写入 destination: {destination} (managed-only mode 拒绝)")


def add_permission_rules_to_settings(
    rules: list[PermissionRule],
    destination: PermissionRuleSource,
) -> None:
    """
    把 rule 列表追加写入 destination 对应的 settings.json

    Args:
        rules: 要添加的 PermissionRule 列表
        destination: 目标 source(PROJECT / LOCAL / USER)

    注意:
    - 去重:已有 rule 不重复写入
    - 不支持写入 session / command / cliArg / flag / policy(managed-only)
    """
    if is_managed_only() and destination != PermissionRuleSource.POLICY:
        logger.warning("managed-only mode 不允许写入 %s", destination)
        return

    path = _settings_for_destination(destination)
    path.parent.mkdir(parents=True, exist_ok=True)

    # 读现有 settings
    settings = load_settings_json(path)
    if "permissions" not in settings or not isinstance(settings.get("permissions"), dict):
        settings["permissions"] = {}

    # 按 behavior 分组
    for behavior_key in ("allow", "deny", "ask"):
        if behavior_key not in settings["permissions"]:
            settings["permissions"][behavior_key] = []
        if not isinstance(settings["permissions"][behavior_key], list):
            settings["permissions"][behavior_key] = []

    for rule in rules:
        rule_str = str(rule)
        # 按 behavior 写入对应 list
        if rule.behavior == PermissionBehavior.ALLOW:
            target_list = settings["permissions"]["allow"]
        elif rule.behavior == PermissionBehavior.DENY:
            target_list = settings["permissions"]["deny"]
        elif rule.behavior == PermissionBehavior.ASK:
            target_list = settings["permissions"]["ask"]
        else:
            continue
        if rule_str not in target_list:
            target_list.append(rule_str)

    # 写回
    with path.open("w", encoding="utf-8") as f:
        json.dump(settings, f, indent=2, ensure_ascii=False)


def delete_permission_rule_from_settings(
    rule: PermissionRule,
    destination: PermissionRuleSource,
) -> bool:
    """
    从 destination 对应的 settings.json 删除指定 rule

    Returns:
        True 如果删除了,False 如果 rule 不存在
    """
    if is_managed_only() and destination != PermissionRuleSource.POLICY:
        logger.warning("managed-only mode 不允许删除 %s", destination)
        return False

    path = _settings_for_destination(destination)
    settings = load_settings_json(path)
    perms = settings.get("permissions", {})
    if not isinstance(perms, dict):
        return False

    rule_str = str(rule)
    if rule.behavior == PermissionBehavior.ALLOW:
        target_list = perms.get("allow", [])
    elif rule.behavior == PermissionBehavior.DENY:
        target_list = perms.get("deny", [])
    elif rule.behavior == PermissionBehavior.ASK:
        target_list = perms.get("ask", [])
    else:
        return False

    if rule_str in target_list:
        target_list.remove(rule_str)
        with path.open("w", encoding="utf-8") as f:
            json.dump(settings, f, indent=2, ensure_ascii=False)
        return True
    return False

# ────────────────────────────────────────────────────────────────────
# sandbox.excludedCommands 写入(M3 Task 4)
# ────────────────────────────────────────────────────────────────────

def save_excluded_commands(
    patterns: list[str],
    destination: PermissionRuleSource,
) -> None:
    """
    把 excluded commands 写回 settings.json 的 sandbox.excludedCommands 段(对齐 doc §5.3)

    UI 编辑区用:用户编辑 excluded commands 列表后,写回 settings.json。
    sandbox_manager 在下次 load_config 时读取(若已运行,需要 reload 或重启会话)。

    Args:
        patterns: 排除命令 pattern 列表(每条 = substring match,大小写敏感)
        destination: 目标 source(PROJECT / LOCAL / USER)

    注意:
    - 写入 sandbox.excludedCommands 字段(非 permissions 段)
    - 容忍 sandbox 段缺失(自动创建)
    - managed-only mode 拒绝写(只读 policy)
    """
    if is_managed_only() and destination != PermissionRuleSource.POLICY:
        logger.warning("managed-only mode 不允许写 excludedCommands 到 %s", destination)
        return

    path = _settings_for_destination(destination)
    path.parent.mkdir(parents=True, exist_ok=True)

    # 读现有 settings
    settings = load_settings_json(path)

    # 确保 sandbox 段存在
    if "sandbox" not in settings or not isinstance(settings.get("sandbox"), dict):
        settings["sandbox"] = {}

    # 过滤空串 + None
    clean_patterns: list[str] = []
    for p in patterns:
        if isinstance(p, str) and p.strip():
            clean_patterns.append(p.strip())

    settings["sandbox"]["excludedCommands"] = clean_patterns

    # 写回
    with path.open("w", encoding="utf-8") as f:
        json.dump(settings, f, indent=2, ensure_ascii=False)


def load_excluded_commands(destination: Optional[PermissionRuleSource] = None) -> list[str]:
    """
    从 settings.json 读 sandbox.excludedCommands 列表(对齐 doc §5.3)

    UI 编辑区用:打开页面时显示当前列表。
    注:sandbox_manager._config.excluded_commands 是 in-memory(单例),
    这里返 settings.json 里的原始字符串(可能与 in-memory 不同步)。

    Args:
        destination: 目标 source(默认 = PROJECT)

    Returns:
        pattern 列表(空列表如果 settings.json 不存在或无该字段)
    """
    if destination is None:
        destination = PermissionRuleSource.PROJECT
    path = _settings_for_destination(destination)
    settings = load_settings_json(path)
    sandbox = settings.get("sandbox", {})
    if not isinstance(sandbox, dict):
        return []
    excluded = sandbox.get("excludedCommands", [])
    if not isinstance(excluded, list):
        return []
    return [str(p) for p in excluded if isinstance(p, str) and p.strip()]
