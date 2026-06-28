"""
Permission Matcher — 规则匹配 + compound rule 解析

对齐 Claude Code:
- src/utils/permissions/permissionRuleParser.ts(parsePermissionRule / ShellPermissionRule)
- src/utils/permissions/shellRuleMatching.ts(matchingRulesForInput)
- src/utils/permissions/pathValidation.ts(matchingRulesForInput 路径约束)
- doc §4.2 规则匹配 + §4.2.2 拆分算法详解

核心设计:
1. **ShellPermissionRule 5 种类型**:exact / prefix / wildcard / compound / unsupported
2. **Compound rule 语义**:first-occurrence separator split(CC 实际行为)
   - 例: "rm:foo && echo bar" → "rm:foo" + " " + "echo bar"(&& 是 first separator)
   - 例: "cmd1; cmd2 | cmd3" → "cmd1" + "; " + "cmd2 | cmd3"(; 是 first separator)
3. **8 source 优先级聚合**:matching 时按 command < flag 顺序找
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

from .permission_types import (
    PermissionBehavior,
    PermissionRule,
    PermissionRuleSource,
    PermissionRuleValue,
    ToolPermissionContext,
)


# ────────────────────────────────────────────────────────────────────
# ShellPermissionRule — 5 种规则类型(dataclass)
# ────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class ShellPermissionRule:
    """
    Bash shell 规则解析后的形态(对齐 CC ShellPermissionRule)

    5 种类型:
    - exact: 完全匹配 "Bash(npm run build)"
    - prefix: 前缀匹配 "Bash(rm:*)" → 命令以 "rm " 开头
    - wildcard: 通配符匹配 "Bash(*echo*)" → 命令字符串包含 echo
    - compound: 复合规则 "Bash(rm:*) && (echo:*)" → 拆分后 AND 语义
    - unsupported: 解析失败 / 不支持(留作 M3 扩展)
    """
    type: str  # 'exact' | 'prefix' | 'wildcard' | 'compound' | 'unsupported'
    command: Optional[str] = None          # exact 形态
    prefix: Optional[str] = None           # prefix 形态
    pattern: Optional[str] = None          # wildcard 形态
    parts: list["ShellPermissionRule"] = field(default_factory=list)  # compound 形态

    def __str__(self) -> str:
        if self.type == "exact":
            return f"exact({self.command})"
        elif self.type == "prefix":
            return f"prefix({self.prefix}:*)"
        elif self.type == "wildcard":
            return f"wildcard(*{self.pattern}*)"
        elif self.type == "compound":
            return "compound(" + " && ".join(str(p) for p in self.parts) + ")"
        else:
            return "unsupported"


# ────────────────────────────────────────────────────────────────────
# Compound rule parsing — first-occurrence separator split
# ────────────────────────────────────────────────────────────────────

# separator 优先级(CC 实际行为:first occurrence 即可,不需要优先级)
# 这里只用作"识别 + 顺序"参考,真正决定 split 的"first occurrence"
_COMPOUND_SEPARATORS = ["&&", "||", ";", "|"]
"""对齐 doc §4.2.2 + CC 实际行为:从左到右找第一个出现的 separator"""


def _try_parse_compound(content: str) -> Optional[ShellPermissionRule]:
    """
    尝试把 content 解析为 compound rule(对齐 CC + doc §4.2.2)

    算法:
      1. 从左到右扫描,在每个位置检查是否是 separator(2 字符 && / || 优先,再 ; / |)
      2. 找到 first-occurrence 后切分为 [left, separator, right]
      3. 递归 parse 左右两侧
      4. 如果任何一侧无法 parse 成有效规则,整体返 None
      5. 如果没有 separator,返 None(让 caller 走 exact/prefix/wildcard)

    注意:
    - **quoted string 中的 separator 不拆分**(对齐 CC)——M1 简化:暂不处理 quote(M2 BashTool 集成时再加)
    - 简化版本:从字面字符串 first occurrence 切
    """
    if not content:
        return None

    # 找 first-occurrence separator
    first_idx = -1
    first_sep = None
    for sep in _COMPOUND_SEPARATORS:
        idx = content.find(sep)
        if idx != -1 and (first_idx == -1 or idx < first_idx):
            first_idx = idx
            first_sep = sep

    if first_sep is None:
        # 没有 separator,无法构成 compound
        return None

    # 切分
    left = content[:first_idx].strip()
    right = content[first_idx + len(first_sep):].strip()

    if not left or not right:
        return None

    # 递归 parse 左右(走同一个 parse_permission_rule)
    left_rule = parse_permission_rule("Bash", left)
    right_rule = parse_permission_rule("Bash", right)

    # 任一解析失败,compound 整体返 None
    if left_rule.type == "unsupported" or right_rule.type == "unsupported":
        return None

    return ShellPermissionRule(type="compound", parts=[left_rule, right_rule])


# ────────────────────────────────────────────────────────────────────
# parse_permission_rule — 顶层解析入口
# ────────────────────────────────────────────────────────────────────

def parse_permission_rule(tool_name: str, content: Optional[str]) -> ShellPermissionRule:
    """
    把 rule_content 字符串解析为 ShellPermissionRule

    Args:
        tool_name: 工具名(用于 compound 内部递归)
        content: rule_content(None 表示整个 tool 命中)

    Returns:
        ShellPermissionRule 实例

    Examples:
        >>> parse_permission_rule("Bash", None)
        ShellPermissionRule(type='exact', command='')  # 整个 tool 命中
        >>> parse_permission_rule("Bash", "rm:*")
        ShellPermissionRule(type='prefix', prefix='rm ')
        >>> parse_permission_rule("Bash", "npm run build")
        ShellPermissionRule(type='exact', command='npm run build')
        >>> parse_permission_rule("Bash", "*echo*")
        ShellPermissionRule(type='wildcard', pattern='echo')
        >>> parse_permission_rule("Bash", "rm:foo && echo:bar")
        ShellPermissionRule(type='compound', parts=[...])
    """
    # 1. None content → exact 空字符串(整个 tool 命中)
    if content is None:
        return ShellPermissionRule(type="exact", command="")

    content = content.strip()
    if not content:
        return ShellPermissionRule(type="exact", command="")

    # 2. compound 形态:含 separator(优先于 prefix 检查,
    #    因为 "rm:* && echo:*" 也以 ":*" 结尾,但其实是 compound)
    if any(sep in content for sep in _COMPOUND_SEPARATORS):
        compound = _try_parse_compound(content)
        if compound is not None:
            return compound
        # compound 解析失败,继续走 prefix / exact(降级)

    # 3. prefix:* 形态
    if content.endswith(":*"):
        # "rm:*" → 切掉 ":*" 后剩 "rm:" → 去 ":" + 去尾空格 + 加尾空格 → "rm "
        # "git commit:*" → 同理 → "git commit "
        prefix_part = content[:-2].rstrip(":").rstrip()
        if prefix_part and not prefix_part.endswith(" "):
            prefix_part = prefix_part + " "
        return ShellPermissionRule(type="prefix", prefix=prefix_part)

    # 4. wildcard 形态:*xxx* 或 *xxx
    if content.startswith("*") and content.endswith("*") and len(content) >= 3:
        # *echo*
        pattern = content[1:-1]
        return ShellPermissionRule(type="wildcard", pattern=pattern)
    if content.startswith("*") and not content.endswith("*") and len(content) >= 2:
        # *echo(只有前缀 * 没有后缀 *)
        pattern = content[1:]
        return ShellPermissionRule(type="wildcard", pattern=pattern)

    # 5. exact 形态
    return ShellPermissionRule(type="exact", command=content)


# ────────────────────────────────────────────────────────────────────
# match_permission_rule — 单条规则是否匹配某条命令
# ────────────────────────────────────────────────────────────────────

def match_permission_rule(rule: ShellPermissionRule, input_str: str) -> bool:
    """
    判断规则是否匹配 input_str(对齐 CC matchingRulesForInput)

    Args:
        rule: 解析后的 ShellPermissionRule
        input_str: 待匹配的输入字符串(Bash command / file path 等)

    Returns:
        True 如果匹配
    """
    if rule.type == "exact":
        # exact 空 = 整个 tool 命中
        if rule.command == "":
            return True
        return input_str == rule.command

    elif rule.type == "prefix":
        return input_str.startswith(rule.prefix or "")

    elif rule.type == "wildcard":
        # Bash rule wildcard(*xxx* / *xxx)语义:字符串包含 pattern
        # 不做 glob 通配(* 在 Bash rule 里只是"含"的标记)
        pattern = rule.pattern or ""
        return pattern in input_str

    elif rule.type == "compound":
        # compound AND 语义:所有 part 都匹配才算
        return all(match_permission_rule(part, input_str) for part in rule.parts)

    else:  # unsupported
        return False


# ────────────────────────────────────────────────────────────────────
# match_wildcard_pattern — glob pattern 匹配
# ────────────────────────────────────────────────────────────────────

def _glob_to_regex(pattern: str) -> str:
    """
    把 glob pattern 转换为 regex 字符串(对齐 CC + 标准 glob 语义)

    转换规则:
    - `**/` → `(?:.*/)?`(0+ 目录层级,可空)
    - `**` → `.*`(任意字符含 /)
    - `*` → `[^/]*`(任意字符不含 /)
    - `?` → `[^/]`(单字符不含 /)
    - 其他字面字符 → re.escape

    用 placeholder 机制确保 glob 元字符不被误 escape。
    """
    # 用 sentinel 替换 glob 元字符,避免后续 escape 误伤
    PLACEHOLDER_GLOB = "\x00GLOB\x00"
    placeholders: list[str] = []

    def _stash(replacement: str) -> str:
        """把 replacement 暂存到 placeholders,返回唯一 sentinel"""
        placeholders.append(replacement)
        return f"{PLACEHOLDER_GLOB}{len(placeholders) - 1}{PLACEHOLDER_GLOB}"

    # 顺序很重要:**/ 先于 **
    result = ""
    i = 0
    while i < len(pattern):
        if pattern[i:i + 3] == "**/":
            result += _stash("(?:.*/)?")
            i += 3
        elif pattern[i:i + 2] == "**":
            result += _stash(".*")
            i += 2
        elif pattern[i] == "*":
            result += _stash("[^/]*")
            i += 1
        elif pattern[i] == "?":
            result += _stash("[^/]")
            i += 1
        else:
            result += pattern[i]
            i += 1

    # 现在 result 中除了 placeholder,都是字面字符 — escape 它们
    # 先把 placeholder 换成 regex(占位,不被 escape)
    parts = result.split(PLACEHOLDER_GLOB)
    final = ""
    for j, part in enumerate(parts):
        if j % 2 == 0:
            # 偶数索引 = 字面字符段
            final += re.escape(part)
        else:
            # 奇数索引 = placeholder 索引
            idx = int(part)
            final += placeholders[idx]

    return "^" + final + "$"


def match_wildcard_pattern(text: str, pattern: str) -> bool:
    """
    glob 风格通配符匹配(对齐 CC matchWildcardPattern)

    用于 file path 匹配(Read/Write/Edit tool 的 path pattern),
    区别于 Bash rule wildcard(那个是"字符串包含"语义)。

    支持:
    - `*` 匹配 0+ 任意字符(不含 /)
    - `**` 匹配 0+ 任意字符(含 /),跨目录层级
    - `?` 匹配单个字符(不含 /)

    Args:
        text: 待匹配文本(典型为文件路径)
        pattern: glob pattern

    Returns:
        True 如果匹配

    Examples:
        >>> match_wildcard_pattern("docs/README.md", "*.md")
        True
        >>> match_wildcard_pattern("docs/sub/foo.md", "**/*.md")
        True
        >>> match_wildcard_pattern("docs/sub/deep/foo.md", "docs/**/*.md")
        True
        >>> match_wildcard_pattern("docs/x.txt", "*.md")
        False
    """
    regex = _glob_to_regex(pattern)
    return bool(re.match(regex, text))


# ────────────────────────────────────────────────────────────────────
# permission_rule_extract_prefix — 从 prefix rule 提取 prefix 字符串
# ────────────────────────────────────────────────────────────────────

def permission_rule_extract_prefix(rule_content: str) -> str:
    """
    从 prefix rule 提取 prefix 字符串(对齐 CC permissionRuleExtractPrefix)

    Args:
        rule_content: 原始 rule_content,如 "rm:*" 或 "git commit:*"

    Returns:
        prefix 字符串,如 "rm " 或 "git commit "
    """
    if not rule_content:
        return ""
    rule = parse_permission_rule("Bash", rule_content)
    if rule.type == "prefix":
        return rule.prefix or ""
    if rule.type == "exact":
        return rule.command or ""
    return ""


# ────────────────────────────────────────────────────────────────────
# _parse_rule_strings — 把字符串规则解析为 PermissionRule
# ────────────────────────────────────────────────────────────────────

def _parse_rule_strings(
    source: PermissionRuleSource,
    behavior: PermissionBehavior,
    rule_strings: list[str],
) -> list[PermissionRule]:
    """
    把 string 形态的规则(从 settings.json 解析)转成 PermissionRule 实例

    字符串形态例: "Bash(rm:*)" / "Read" / "Edit"
    解析:
    - "Bash(rm:*)" → (tool_name="Bash", rule_content="rm:*")
    - "Read" → (tool_name="Read", rule_content=None)
    - "" → skip
    """
    result = []
    for rs in rule_strings:
        rs = rs.strip()
        if not rs:
            continue
        # 解析 "ToolName(content)" 形态
        match = re.match(r"^([A-Za-z][A-Za-z0-9_]*)\s*\((.*)\)\s*$", rs, re.DOTALL)
        if match:
            tool_name = match.group(1)
            rule_content = match.group(2).strip()
        else:
            # 无括号: 整个 tool 命中
            tool_name = rs
            rule_content = None
        result.append(
            PermissionRule(
                source=source,
                behavior=behavior,
                value=PermissionRuleValue(tool_name=tool_name, rule_content=rule_content),
            )
        )
    return result


# ────────────────────────────────────────────────────────────────────
# matching_rules_for_input — 按 source 优先级聚合匹配的规则
# ────────────────────────────────────────────────────────────────────

def matching_rules_for_input(
    tool_name: str,
    input_str: str,
    context: ToolPermissionContext,
) -> dict[str, list[PermissionRule]]:
    """
    找出所有 source × behavior 中匹配 (tool_name, input_str) 的规则
    按 source 优先级排序(command < flag)

    Args:
        tool_name: 工具名
        input_str: 待匹配的输入(Bash command / file path 等)
        context: ToolPermissionContext

    Returns:
        {behavior: list[PermissionRule]} — 按 behavior 分组

    Examples:
        >>> ctx = ToolPermissionContext(...)
        >>> matching_rules_for_input("Bash", "rm -rf /", ctx)
        {
            "deny": [PermissionRule(Bash(rm:*), source=projectSettings)],
            "ask": [],
            "allow": [PermissionRule(Bash(*echo*), source=userSettings)],
        }
    """
    result = {
        PermissionBehavior.ALLOW.value: [],
        PermissionBehavior.DENY.value: [],
        PermissionBehavior.ASK.value: [],
    }

    # 按 source 优先级从低到高(command < flag),第一个匹配的 source 决定优先级
    for source in PermissionRuleSource.ordered_sources():
        for behavior in [PermissionBehavior.DENY, PermissionBehavior.ASK, PermissionBehavior.ALLOW]:
            rule_strings = []
            if behavior == PermissionBehavior.DENY:
                rule_strings = context.always_deny_rules.get(source.value, [])
            elif behavior == PermissionBehavior.ASK:
                rule_strings = context.always_ask_rules.get(source.value, [])
            elif behavior == PermissionBehavior.ALLOW:
                rule_strings = context.always_allow_rules.get(source.value, [])

            # 把字符串解析为 PermissionRule,过滤掉非 tool_name 匹配的
            for rule in _parse_rule_strings(source, behavior, rule_strings):
                if rule.tool_name != tool_name:
                    continue
                # 检查 rule_content 是否匹配 input_str
                if rule.rule_content is None:
                    # 整个 tool 命中
                    result[behavior.value].append(rule)
                else:
                    parsed = parse_permission_rule(tool_name, rule.rule_content)
                    if match_permission_rule(parsed, input_str):
                        result[behavior.value].append(rule)

    return result


# ────────────────────────────────────────────────────────────────────
# parse_all_rules_from_strings — 解析整个 settings.json 字符串列表
# ────────────────────────────────────────────────────────────────────

def parse_all_rules_from_strings(
    rule_strings: list[str],
    behavior: PermissionBehavior,
    source: PermissionRuleSource = PermissionRuleSource.PROJECT,
) -> list[PermissionRule]:
    """
    把 settings.json 的 allow/deny/ask 字符串列表解析为 PermissionRule 列表

    公开 API,permission_loader 用
    """
    return _parse_rule_strings(source, behavior, rule_strings)