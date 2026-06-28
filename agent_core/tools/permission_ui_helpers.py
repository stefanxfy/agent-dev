"""
权限规则编辑 UI 的纯函数 helper(对齐 spec §9 Phase 3 Task 1)

为什么独立成模块(不放在 web/pages/03_Permissions.py):
- Streamlit 页面文件顶层会执行 st.set_page_config,直接 import 会触发 streamlit context
- 把可测的纯函数抽出来,test 能直接测,不碰 streamlit 渲染
- 文件单一职责:本模块只做 rule 解析/构造/格式化,无 UI 依赖

复用 M1 资产:
- permission_loader.add_permission_rules_to_settings / delete_permission_rule_from_settings
- permission_loader.load_rules_by_source / get_permission_rules_for_source
- permission_matcher.parse_permission_rule(UI 预览用)
- permission_types.PermissionRule / PermissionRuleValue / PermissionBehavior / PermissionRuleSource
"""

from __future__ import annotations

from typing import Optional

from .permission_matcher import parse_permission_rule
from .permission_types import (
    PermissionBehavior,
    PermissionRule,
    PermissionRuleSource,
    PermissionRuleValue,
)


# ────────────────────────────────────────────────────────────────────
# render_rule_preview — 解析预览
# ────────────────────────────────────────────────────────────────────

def render_rule_preview(tool_name: str, content: Optional[str]) -> str:
    """
    把 rule_content 解析为人类可读的预览字符串(UI 实时显示用)

    对齐 doc §4.2:调 parse_permission_rule 拿 ShellPermissionRule,str() 化

    Args:
        tool_name: 工具名("Bash" / "Read" / ...)
        content: rule_content(None = 整个 tool 命中)

    Returns:
        预览字符串,如:
        - "exact: ''  (整个 Bash 命中)"
        - "prefix: 'rm '  (以 rm 开头)"
        - "exact: 'npm run build'  (完全匹配)"
        - "wildcard: '*echo*'  (含 echo)"

    Examples:
        >>> render_rule_preview("Bash", "rm:*")
        "prefix(rm :*)"
        >>> render_rule_preview("Bash", None)
        "exact()"
    """
    if not tool_name:
        return "(空 tool_name)"
    rule = parse_permission_rule(tool_name, content)
    return str(rule)


# ────────────────────────────────────────────────────────────────────
# build_permission_rule — 构造 PermissionRule(UI add 用)
# ────────────────────────────────────────────────────────────────────

# behavior 字符串 → PermissionBehavior 映射(UI selectbox 用)
_BEHAVIOR_MAP = {
    "allow": PermissionBehavior.ALLOW,
    "deny": PermissionBehavior.DENY,
    "ask": PermissionBehavior.ASK,
}

# destination 字符串 → PermissionRuleSource(UI selectbox 用,只允许可写的 3 个 source)
_DESTINATION_MAP = {
    "projectSettings": PermissionRuleSource.PROJECT,
    "localSettings": PermissionRuleSource.LOCAL,
    "userSettings": PermissionRuleSource.USER,
}


def build_permission_rule(
    behavior: str,
    tool_name: str,
    content: Optional[str],
    destination: str,
) -> PermissionRule:
    """
    从 UI 表单输入构造 PermissionRule(对齐 doc §4.6 add rule)

    Args:
        behavior: "allow" / "deny" / "ask"
        tool_name: 工具名(非空)
        content: rule_content(None / 空串 = 整个 tool 命中)
        destination: "projectSettings" / "localSettings" / "userSettings"

    Returns:
        PermissionRule

    Raises:
        ValueError: behavior 非法 / destination 非法(managed-only source)/ tool_name 空
    """
    if behavior not in _BEHAVIOR_MAP:
        raise ValueError(
            f"非法 behavior: {behavior}(允许: {list(_BEHAVIOR_MAP.keys())})"
        )
    if destination not in _DESTINATION_MAP:
        raise ValueError(
            f"非法 destination: {destination}"
            f"(允许: {list(_DESTINATION_MAP.keys())};session/command/cliArg/flag/policy 不可写)"
        )
    if not tool_name or not tool_name.strip():
        raise ValueError("tool_name 不能为空")

    # content 空串 → None(整个 tool 命中)
    normalized_content = content.strip() if content else None
    if normalized_content == "":
        normalized_content = None

    return PermissionRule(
        source=_DESTINATION_MAP[destination],
        behavior=_BEHAVIOR_MAP[behavior],
        value=PermissionRuleValue(
            tool_name=tool_name.strip(),
            rule_content=normalized_content,
        ),
    )


# ────────────────────────────────────────────────────────────────────
# format_rules_by_source — 扁平化(UI 列表渲染用)
# ────────────────────────────────────────────────────────────────────

def format_rules_by_source(rules_by_source: dict) -> list[dict]:
    """
    把 load_rules_by_source() 的嵌套 dict 扁平化为 UI 友好的 list

    load_rules_by_source() 返:
        {
            "always_allow_rules": {source: [rule_str, ...], ...},
            "always_deny_rules": {...},
            "always_ask_rules": {...},
        }

    本函数返:
        [
            {"source": "projectSettings", "behavior": "deny", "rule_str": "Bash(rm:*)",
             "tool_name": "Bash", "content": "rm:*"},
            ...
        ]

    Args:
        rules_by_source: load_rules_by_source() 的返回

    Returns:
        扁平化的 rule dict 列表(按 behavior 分组:deny → ask → allow)
    """
    if not rules_by_source:
        return []

    result: list[dict] = []
    # 按 behavior 顺序(deny 优先显示,最显眼)
    behavior_order = [
        ("always_deny_rules", "deny"),
        ("always_ask_rules", "ask"),
        ("always_allow_rules", "allow"),
    ]
    for rules_key, behavior in behavior_order:
        source_dict = rules_by_source.get(rules_key, {})
        if not isinstance(source_dict, dict):
            continue
        for source, rule_list in source_dict.items():
            if not isinstance(rule_list, list):
                continue
            for rule_str in rule_list:
                if not isinstance(rule_str, str) or not rule_str.strip():
                    continue
                tool_name, content = _split_rule_str(rule_str)
                result.append({
                    "source": source,
                    "behavior": behavior,
                    "rule_str": rule_str,
                    "tool_name": tool_name,
                    "content": content,
                })
    return result


def _split_rule_str(rule_str: str) -> tuple[str, Optional[str]]:
    """
    "Bash(rm:*)" → ("Bash", "rm:*")  /  "Read" → ("Read", None)

    UI 列表渲染用(显示 tool_name + content 分列)
    """
    rule_str = rule_str.strip()
    if "(" in rule_str:
        name, rest = rule_str.split("(", 1)
        content = rest.rstrip(")").strip()
        return name.strip(), (content or None)
    return rule_str, None
