"""agent_core.tools.permission.rule_checker — 内容级 rule 匹配(D.3b-1)。

从 PermissionEngine 抽出的纯逻辑 helper:
  - is_path_tool / derive_input_str / parse_rule_str(工具函数,无状态)
  - RuleChecker 类(持有 ToolPermissionContext,提供 check_deny/ask/allow)

设计契约:
  - RuleChecker 不依赖 PermissionEngine(step 解耦)
  - engine 保留 _check_global_*_rule / _is_path_tool 等 delegate 方法(向后兼容,
    部分 step / 测试可能直接调);内部委托给 RuleChecker
  - step 通过注入的 ctx.rule_checker 访问
"""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Optional

from .matcher import (
    match_wildcard_pattern,
    matching_rules_for_input,
)
from .types import (
    PermissionBehavior,
    PermissionRule,
    PermissionRuleSource,
    PermissionRuleValue,
)

if TYPE_CHECKING:
    from .types import ToolPermissionContext


# path 类工具集合(MCP 工具命名 mcp__<server>__<tool>,在此统一走 glob)
# Bash 走 matching_rules_for_input 的 shell 语义(prefix:/exact/wildcard)
PATH_TOOLS: frozenset[str] = frozenset({
    "Read", "Write", "Edit", "MultiEdit",
    "Glob", "Grep", "NotebookEdit",
})


def is_path_tool(tool_name: str) -> bool:
    """判断是否走 glob 路径匹配(Edit/Read/Write 类 + MCP)。"""
    return tool_name in PATH_TOOLS or tool_name.startswith("mcp__")


def derive_input_str(tool_name: str, tool_input: dict) -> str:
    """把 tool_input 序列化为 matcher 期望的 input_str。

    分支:
      - Bash              → tool_input["command"]
      - path 类 + 有 path → 第一个非空 path 字段(file_path / path / notebook_path / directory)
      - 其他 / 无 path    → JSON dump(sort_keys,default=str)
    """
    if tool_name == "Bash":
        return tool_input.get("command", "") or ""
    if is_path_tool(tool_name):
        path = (
            tool_input.get("file_path")
            or tool_input.get("path")
            or tool_input.get("notebook_path")
            or tool_input.get("directory")
        )
        if path:
            return str(path)
    return json.dumps(tool_input, sort_keys=True, default=str)


def parse_rule_str(
    rule_str: str,
    source: PermissionRuleSource,
    behavior: PermissionBehavior,
) -> Optional[PermissionRule]:
    """从 "Bash(rm:*)" / "Edit" 字符串解析 PermissionRule。

    注:完整 parse 在 permission_matcher.parse_all_rules_from_strings 里;
    这里简化版只切 (tool_name, rule_content) 形态。
    """
    rule_str = rule_str.strip()
    if not rule_str:
        return None
    match = re.match(r"^([A-Za-z][A-Za-z0-9_]*)\s*\((.*)\)\s*$", rule_str, re.DOTALL)
    if match:
        tool_name = match.group(1)
        rule_content = match.group(2).strip()
    else:
        tool_name = rule_str
        rule_content = None
    return PermissionRule(
        source=source,
        behavior=behavior,
        value=PermissionRuleValue(
            tool_name=tool_name,
            rule_content=rule_content,
        ),
    )


class RuleChecker:
    """内容级 rule 匹配器(持有 ToolPermissionContext)。

    提供 check_deny / check_ask / check_allow 三方法,
    分别对应 PermissionEngine 原 Step 1a / 1b / 2b。

    匹配语义:
      - path 工具(Read/Write/Edit/MCP)走 glob(match_wildcard_pattern)
      - Bash / 其他走 matching_rules_for_input(shell 语义)
      - 按 PermissionRuleSource 优先级,首个命中胜出
    """

    def __init__(self, context: "ToolPermissionContext"):
        self.context = context

    # ── 公开 API(check_permissions pipeline 用)──

    def check_deny(self, tool_name: str, tool_input: dict) -> Optional[PermissionRule]:
        """Step 1a: 内容级 deny rule 匹配(按 source 优先级,首个命中胜出)。"""
        return self._check_global_rule_with_input(
            tool_name, tool_input,
            self.context.always_deny_rules, PermissionBehavior.DENY,
        )

    def check_ask(self, tool_name: str, tool_input: dict) -> Optional[PermissionRule]:
        """Step 1b: 内容级 ask rule 匹配。"""
        return self._check_global_rule_with_input(
            tool_name, tool_input,
            self.context.always_ask_rules, PermissionBehavior.ASK,
        )

    def check_allow(self, tool_name: str, tool_input: dict) -> Optional[PermissionRule]:
        """Step 2b: 内容级 allow rule 匹配。"""
        return self._check_global_rule_with_input(
            tool_name, tool_input,
            self.context.always_allow_rules, PermissionBehavior.ALLOW,
        )

    # ── 内部 helper ──

    def _check_global_rule_with_input(
        self,
        tool_name: str,
        tool_input: dict,
        rules_dict: dict,
        behavior: PermissionBehavior,
    ) -> Optional[PermissionRule]:
        """统一的内容级 rule 查找入口:path 工具走 glob,Bash 走 matching_rules_for_input。"""
        if is_path_tool(tool_name):
            return self._check_path_tool_rule(tool_name, tool_input, rules_dict, behavior)
        input_str = derive_input_str(tool_name, tool_input)
        matched = matching_rules_for_input(tool_name, input_str, self.context)
        rules = matched.get(behavior.value, [])
        return rules[0] if rules else None

    def _check_path_tool_rule(
        self,
        tool_name: str,
        tool_input: dict,
        rules_dict: dict,
        behavior: PermissionBehavior,
    ) -> Optional[PermissionRule]:
        """path 类工具走 glob 语义(`/tmp/*` → match_wildcard_pattern)。

        用 parse_rule_str 按 source 优先级遍历,首个命中胜出。
        不复用 matching_rules_for_input,因其内部用 shell 语义无法处理 glob。
        """
        target = derive_input_str(tool_name, tool_input)
        for source in PermissionRuleSource.ordered_sources():
            for rule_str in rules_dict.get(source.value, []):
                rule = parse_rule_str(rule_str, source, behavior)
                if rule is None or rule.tool_name != tool_name:
                    continue
                if rule.rule_content is None:
                    # 整个 tool 命中(如 "Edit" / "Read")
                    return rule
                if match_wildcard_pattern(target, rule.rule_content):
                    return rule
        return None
