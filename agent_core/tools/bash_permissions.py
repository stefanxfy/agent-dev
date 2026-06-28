"""
BashTool.check_permissions 实现
完整对齐 Claude Code src/tools/BashTool/bashPermissions.ts:1663 bashToolHasPermission

子命令解析双路径:
- TREE_SITTER_BASH=true (默认 false) → tree-sitter AST,支持嵌套引号/命令替换/重定向
- TREE_SITTER_BASH=false            → shlex + regex(legacy path,够用 80% 场景)

classifier 集成:
- Bash 是最容易被 prompt injection 利用的工具,所有 Bash 调用走 classifier 兜底
- M2 同步实现(spec 的 async 在 M3+ 真接 classifier 时再开,避免引入 pytest-asyncio)

关键不变量(对齐 doc §6.3):
- sandbox auto-allow 路径下,subcommand-level deny 仍触发整体 deny
- dangerously_disable_sandbox 仅 bypass sandbox,不 bypass permission rule check
- cd + git 组合 → ASK(防 #29316 bare-git scrub)
"""

from __future__ import annotations

import logging
import os
import re
import shlex
from dataclasses import dataclass, field
from typing import Any, Optional

from .permission_matcher import match_permission_rule, parse_permission_rule
from .permission_types import (
    ClassifierReason,
    OtherReason,
    PermissionBehavior,
    PermissionDecision,
    PermissionMode,
    PermissionRuleSource,
    SafetyCheckReason,
    SubcommandResultsReason,
    ToolPermissionContext,
)

logger = logging.getLogger(__name__)


# ── 配置:tree-sitter 是否启用(对齐 CC env TREE_SITTER_BASH) ──

def _tree_sitter_enabled() -> bool:
    """读 env TREE_SITTER_BASH(每次调用读,便于测试 monkeypatch env)"""
    return os.environ.get("TREE_SITTER_BASH", "false").lower() == "true"


# 对齐 CC MAX_SUBCOMMANDS_FOR_SECURITY_CHECK (bashPermissions.ts:103)
MAX_SUBCOMMANDS = 50

# 对齐 CC MAX_SUGGESTED_RULES_FOR_COMPOUND (bashPermissions.ts:110)
MAX_SUGGESTED_RULES = 5

# 安全 wrapper 前缀(对齐 CC stripSafeWrappers)
SAFE_WRAPPERS = ["timeout", "time", "nice", "env", "command", "nohup"]

# 带数字参数的 wrapper(timeout 30 / nice -5 等)— 剥 wrapper 后再剥其参数
# env / command 不带强制的数字参数(只剥 wrapper 本身 + 后续 env var)
_NUMERIC_ARG_WRAPPERS = {"timeout", "time", "nice", "nohup"}

# 只读命令白名单(acceptEdits 模式下自动 allow)
# 注意:不含 git(git push/git commit 是写操作,只 git status/diff/log 只读)
_READ_ONLY_COMMANDS = frozenset({
    "cat", "ls", "echo", "pwd", "head", "tail", "wc", "grep", "find",
    "which", "whoami", "date", "uname", "df", "du", "ps", "top",
    "file", "stat", "tree", "sort", "uniq", "cut", "tr",
})

# 高优先级 source(对齐 doc §4.5.1 简化,只查这 4 个高层 source)
_HIGH_PRIORITY_SOURCES = [
    PermissionRuleSource.FLAG,
    PermissionRuleSource.POLICY,
    PermissionRuleSource.PROJECT,
    PermissionRuleSource.LOCAL,
    PermissionRuleSource.USER,
]


# ────────────────────────────────────────────────────────────────────
# Subcommand dataclass
# ────────────────────────────────────────────────────────────────────

@dataclass
class Subcommand:
    """
    AST / regex 解析后的单条子命令(对齐 CC Subcommand)

    字段:
    - command: 原始命令文本 "git push origin main"
    - name: 命令名(第一个 token)"git"
    - args: 参数列表 ["push", "origin", "main"]
    - operator: 连接符 ";" / "&&" / "||" / "|" / "&"
    - is_redirect: 是否含 > < >> <<
    - is_subshell: 是否在 $(...) / `...` 内
    """
    command: str
    name: str = ""
    args: list[str] = field(default_factory=list)
    operator: str = ";"
    is_redirect: bool = False
    is_subshell: bool = False


# ────────────────────────────────────────────────────────────────────
# parse_subcommands — 双路径
# ────────────────────────────────────────────────────────────────────

def parse_subcommands(command: str) -> list[Subcommand]:
    """
    解析 bash 命令为子命令列表
    对齐 CC bashPermissions.ts:parseSubcommands

    路径选择:
    - TREE_SITTER_BASH=true → _parse_via_tree_sitter(AST)
    - else → _parse_via_regex(legacy)
    """
    if not command or not command.strip():
        return []
    if _tree_sitter_enabled():
        return _parse_via_tree_sitter(command)
    return _parse_via_regex(command)


def _parse_via_regex(command: str) -> list[Subcommand]:
    """
    Legacy path:用 shlex + regex 拆分子命令
    对齐 CC legacy path:不处理嵌套引号/命令替换(TREE_SITTER_BASH=false 默认走此路径)

    选型说明(对齐 doc §4.5.1):
    - regex 路径无法 100% 处理嵌套引号 / $(...) / 反引号(shell AST 复杂度)
    - CC 同等情况下也用 regex fallback + tree-sitter 兜底
    - 够覆盖 80% 常见 bash 命令,其余 20% 走 tree-sitter AST 路径
    """
    # 按顶层 && || ; | 拆(注意:这会错误拆分引号内的这些符号 → AST path 才对)
    parts = re.split(r"\s*(?:&&|\|\||;|\|)\s*", command)
    subs: list[Subcommand] = []
    n = len(parts)
    for i, p in enumerate(parts):
        p = p.strip()
        if not p:
            continue
        # shlex.split 处理引号
        try:
            tokens = shlex.split(p)
        except ValueError:
            # 引号不匹配等 → 退化为 split
            tokens = p.split()
        name = tokens[0] if tokens else ""
        # operator:用 re.finditer 找原 command 里这些 part 之间的分隔符
        op = ";" if i < n - 1 else ""
        subs.append(Subcommand(
            command=p,
            name=name,
            args=tokens[1:],
            operator=op,
            is_redirect=bool(re.search(r"[<>]", p)),
            is_subshell="$(" in p or "`" in p,
        ))
    return subs


def _parse_via_tree_sitter(command: str) -> list[Subcommand]:
    """
    Tree-sitter AST path:解析完整 bash grammar
    处理嵌套引号/命令替换/heredoc/重定向 等复杂场景

    依赖:`pip install tree-sitter tree-sitter-bash`
    启用:`TREE_SITTER_BASH=true`

    对齐 doc §4.5.1:AST 正确识别引号是 string literal,只在最外层 && 拆;
    $(...) 是 subshell,内部不拆。
    """
    try:
        import tree_sitter_bash  # type: ignore
        from tree_sitter import Language, Parser  # type: ignore

        BASH_LANG = Language(tree_sitter_bash.language())
        parser = Parser(BASH_LANG)
        tree = parser.parse(bytes(command, "utf8"))

        subs: list[Subcommand] = []

        def walk(node, operator=";"):
            """递归遍历 AST 节点"""
            if node is None:
                return
            node_type = node.type
            if node_type == "command":
                # 提取 command name + arguments
                name_node = node.child_by_field_name("name")
                name = name_node.text.decode("utf8") if name_node and name_node.text else ""
                args: list[str] = []
                for child in node.children:
                    if child.type in ("command_name", "variable_assignment", "redirect"):
                        continue
                    if child.text:
                        args.append(child.text.decode("utf8"))
                cmd_text = node.text.decode("utf8") if node.text else ""
                subs.append(Subcommand(
                    command=cmd_text,
                    name=name,
                    args=args,
                    operator=operator,
                    is_redirect=any(c.type == "redirect" for c in node.children),
                    is_subshell=False,
                ))
            elif node_type == "list":
                # cmd1 && cmd2 || cmd3
                children = [c for c in node.children if c is not None]
                current_op = ";"
                for c in children:
                    if c.is_named:
                        walk(c, current_op)
                    elif c.type in ("&&", "||", "|", "&", ";"):
                        current_op = c.type
            elif node_type == "subshell":
                # $(...) / `...`
                for child in node.children:
                    if child is not None and child.is_named:
                        walk(child, "$")
                        if subs:
                            subs[-1].is_subshell = True
            elif node_type == "pipeline":
                # cmd1 | cmd2 | cmd3
                for child in node.children:
                    if child is not None and child.is_named:
                        walk(child, "|")
            elif node_type == "command_substitution":
                # $(...) 内嵌
                for child in node.children:
                    if child is not None and child.is_named:
                        walk(child, "$")
                        if subs:
                            subs[-1].is_subshell = True
            else:
                # 其他节点递归子节点
                for child in node.children:
                    if child is not None:
                        walk(child, operator)

        walk(tree.root_node)
        return subs if subs else _parse_via_regex(command)

    except ImportError:
        # 装了 TREE_SITTER_BASH=true 但没装依赖 → 降级到 regex
        logger.debug("tree-sitter-bash 未安装,降级到 regex path")
        return _parse_via_regex(command)
    except Exception as e:
        # AST 解析失败 → 降级到 regex
        logger.warning("tree-sitter 解析失败,降级到 regex: %s", e)
        return _parse_via_regex(command)


# ────────────────────────────────────────────────────────────────────
# 安全 wrapper 剥离 + 命令类型检测
# ────────────────────────────────────────────────────────────────────

def strip_safe_wrappers(cmd: str) -> str:
    """
    timeout 30 FOO=bar bazel run → bazel run
    对齐 CC bashPermissions.ts:stripSafeWrappers

    剥离逻辑(循环):
    - 安全 wrapper 前缀(timeout/time/nice/env/command/nohup)
    - 数字参数(timeout 30 / nice -5 等 wrapper 的参数)
    - 环境变量赋值前缀(FOO=bar)
    """
    if not cmd:
        return cmd
    parts = cmd.split()
    while parts:
        first = parts[0]
        # 安全 wrapper
        if first in SAFE_WRAPPERS:
            parts = parts[1:]
            # timeout/time/nice/nohup 带可选数字参数 → 也剥
            if (
                first in _NUMERIC_ARG_WRAPPERS
                and parts
                and _is_numeric_arg(parts[0])
            ):
                parts = parts[1:]
            continue
        # 环境变量赋值(FOO=bar)
        if re.match(r"^[A-Z_][A-Z0-9_]*=", first):
            parts = parts[1:]
            continue
        break
    return " ".join(parts) if parts else cmd


def _is_numeric_arg(token: str) -> bool:
    """判断 token 是否是 wrapper 的数字参数(timeout 30 / nice -5 / nice 10)"""
    if not token:
        return False
    # 纯数字 / 带符号的数字 / 带时间单位(30s / 5m 等)
    return bool(re.match(r"^[+-]?\d+(\.\d+)?[smhd]?$", token))


def is_cd_command(cmd: str) -> bool:
    """cd 命令检测(对齐 CC isCdCommand)"""
    if not cmd:
        return False
    return cmd.startswith("cd ") or cmd.strip() == "cd"


def is_read_only(cmd: str) -> bool:
    """
    只读命令检测(acceptEdits 模式下自动 allow)
    对齐 CC isReadOnly

    只读 = 不修改文件系统 / 不联网 / 无副作用
    """
    if not cmd:
        return False
    stripped = strip_safe_wrappers(cmd).strip()
    first_token = stripped.split()[0] if stripped.split() else ""
    if not first_token:
        return False
    # 只读命令白名单
    if first_token in _READ_ONLY_COMMANDS:
        return True
    # git 子命令:status/diff/log 是只读
    if first_token == "git":
        parts = stripped.split()
        if len(parts) > 1 and parts[1] in {"status", "diff", "log", "show", "blame"}:
            return True
    return False


# ────────────────────────────────────────────────────────────────────
# bash_check_permissions — 主入口(M2 同步版本)
# ────────────────────────────────────────────────────────────────────

def bash_check_permissions(
    tool_input: dict,
    context: ToolPermissionContext,
    classifier: Optional[Any] = None,
    messages: Optional[list[dict]] = None,
) -> PermissionDecision:
    """
    对齐 CC bashToolHasPermission 完整流水线(M2 同步版)

    流程(对齐 doc §6.3):
      Step 0:  sandbox auto-allow 提前检查
      Step 1:  parse_subcommands
      Step 1.5: MAX_SUBCOMMANDS cap
      Step 2:  cd + git 检测(#29316 bare-git scrub)
      Step 3:  classifier speculative check(M2 同步)
      Step 4:  per-subcommand rule check
      Step 5:  整合 classifier 决策
      Step 6:  全 allow → ALLOW

    Args:
        tool_input: {"command": "...", ...}
        context: 权限上下文
        classifier: HaikuClassifier 实例(可空 → 跳过 classifier)
        messages: 对话历史(classifier 用,可空)

    Returns:
        PermissionDecision
    """
    command = tool_input.get("command", "") or ""

    # Step 0: empty command
    if not command.strip():
        return PermissionDecision(
            behavior=PermissionBehavior.ASK.value,
            decision_reason=OtherReason(reason="empty command"),
        )

    # ── Step 0: sandbox auto-allow 提前检查(对齐 doc §5.4 + §6.3) ──
    # 三段条件:is_sandbox_enabled + auto_allow_bash_if_sandboxed + should_use_sandbox
    sandbox_auto = _check_sandbox_auto_allow_conditions(tool_input)
    if sandbox_auto:
        auto = check_sandbox_auto_allow(tool_input, context)
        if auto.behavior in (PermissionBehavior.DENY.value, PermissionBehavior.ASK.value):
            return auto  # deny/ask rule 在沙箱内仍生效,不绕过
        # 否则 fall through(沙箱兜底)—— 但仍跑 subcommand check
        # (对齐 CC §6.3:即便 auto-allow,subcommand-level deny 仍触发)

    # ── Step 1: 拆分 subcommand ──
    subcommands = parse_subcommands(command)
    if not subcommands:
        return PermissionDecision(
            behavior=PermissionBehavior.ASK.value,
            decision_reason=OtherReason(reason="无法解析 command"),
        )

    # ── Step 1.5: MAX_SUBCOMMANDS cap ──
    if len(subcommands) > MAX_SUBCOMMANDS:
        return PermissionDecision(
            behavior=PermissionBehavior.ASK.value,
            decision_reason=OtherReason(
                reason=f"subcommand 数 {len(subcommands)} 超过 {MAX_SUBCOMMANDS}",
            ),
        )

    # ── Step 2: cd + git 检测(对齐 CC bare-git scrub attack #29316) ──
    has_cd = any(is_cd_command(sc.command) for sc in subcommands)
    has_git = any(sc.name == "git" for sc in subcommands)
    if has_cd and has_git:
        return PermissionDecision(
            behavior=PermissionBehavior.ASK.value,
            decision_reason=SafetyCheckReason(
                reason="cd + git 组合可能加载恶意 .git/config (CC #29316 bare-git scrub)",
                classifier_approvable=False,
            ),
        )

    # ── Step 3: classifier speculative check(M2 同步) ──
    # 对齐 doc §4.5:classifier 并发跑,但 M2 同步实现
    classifier_result = None
    classifier_should_block = False
    classifier_reason = ""
    if classifier is not None and getattr(context, "is_anthropic_provider", False):
        try:
            from .classifier import is_classifier_enabled
            provider = "anthropic"  # classifier 仅 ANT 启用
            if is_classifier_enabled(
                provider=provider,
                mode=PermissionMode(context.mode),
                no_settings_match=context.no_settings_match,
            ):
                result = classifier.classify(
                    messages or [],
                    "Bash",
                    tool_input,
                    context,
                )
                classifier_result = result
                if not result.unavailable:
                    classifier_should_block = result.should_block
                    classifier_reason = result.reason
        except Exception as e:
            logger.warning("classifier speculative check 失败,降级: %s", e)

    # ── Step 4: 对每个 subcommand 跑规则匹配 ──
    deny_count = 0
    ask_count = 0
    allow_count = 0
    first_blocking: Optional[PermissionDecision] = None
    for sc in subcommands:
        sc_stripped = strip_safe_wrappers(sc.command)
        result = _check_single_command(sc_stripped, tool_input, context)
        if result.behavior == PermissionBehavior.DENY.value:
            deny_count += 1
            if first_blocking is None:
                first_blocking = result
        elif result.behavior == PermissionBehavior.ASK.value:
            ask_count += 1
            if first_blocking is None:
                first_blocking = result
        elif result.behavior == PermissionBehavior.ALLOW.value:
            allow_count += 1

    # 任一 subcommand deny/ask → 立即返回(不等 classifier,对齐 doc §4.5 Step 5)
    if first_blocking is not None:
        return PermissionDecision(
            behavior=first_blocking.behavior,
            decision_reason=SubcommandResultsReason(
                allow_count=allow_count,
                ask_count=ask_count,
                deny_count=deny_count,
                reason=first_blocking.message or f"子命令命中规则 ({first_blocking.behavior})",
            ),
            updated_input=tool_input,
        )

    # ── Step 5: 等 classifier 结果(M2 同步,结果已在 Step 3 拿到) ──
    if classifier_should_block:
        return PermissionDecision(
            behavior=PermissionBehavior.DENY.value,
            decision_reason=ClassifierReason(
                classifier="bash_deny",
                reason=classifier_reason or "classifier denied bash command",
            ),
            message=f"Classifier denied: {classifier_reason}",
        )

    # ── Step 6: 全部 subcommand 通过 → ALLOW ──
    # sandbox auto-allow 路径 → ALLOW(sandbox 兜底)
    if sandbox_auto:
        return PermissionDecision(
            behavior=PermissionBehavior.ALLOW.value,
            decision_reason=OtherReason(
                reason="Auto-allowed in sandbox; deny rules already checked",
            ),
        )

    # 非 sandbox → 走 fast-path 后处理:acceptEdits + read-only → ALLOW
    if context.mode == PermissionMode.ACCEPT_EDITS.value:
        # 所有 subcommand 都是只读 → ALLOW
        all_read_only = all(is_read_only(sc.command) for sc in subcommands)
        if all_read_only:
            return PermissionDecision(
                behavior=PermissionBehavior.ALLOW.value,
                decision_reason=OtherReason(reason="acceptEdits + read-only"),
            )

    # 默认 PASSTHROUGH(让 PermissionEngine 上层 decide)
    return PermissionDecision(
        behavior=PermissionBehavior.PASSTHROUGH.value,
        decision_reason=OtherReason(reason="所有 subcommand 通过,passthrough"),
    )


def _check_sandbox_auto_allow_conditions(tool_input: dict) -> bool:
    """
    检查 sandbox auto-allow 三段条件(对齐 doc §5.4 + §6.3):
      1. is_sandbox_enabled() == True
      2. config.auto_allow_bash_if_sandboxed == True
      3. should_use_sandbox("Bash", tool_input) == True

    注:should_use_sandbox 内部已检查 is_sandbox_enabled,但这里显式拆开
    便于独立测试 + 对齐 CC 入口条件判断。
    """
    from .sandbox_decision import should_use_sandbox
    from .sandbox_manager import sandbox_manager

    if not sandbox_manager.is_sandbox_enabled():
        return False
    if not sandbox_manager._config.auto_allow_bash_if_sandboxed:
        return False
    if not should_use_sandbox("Bash", tool_input):
        return False
    return True


def _check_single_command(
    cmd: str, tool_input: dict, context: ToolPermissionContext,
) -> PermissionDecision:
    """
    单条命令的规则匹配(对齐 CC bashToolCheckPermission)

    按 source 优先级遍历:deny → ask → allow(对齐 doc §4.5.1)
    任一 source 命中即返回该 behavior。

    Returns:
        DENY / ASK / ALLOW / PASSTHROUGH
    """
    # deny rules(按 source 优先级)
    for source in _HIGH_PRIORITY_SOURCES:
        for rule_str in context.always_deny_rules.get(source.value, []):
            if _rule_matches(rule_str, cmd):
                return PermissionDecision(
                    behavior=PermissionBehavior.DENY.value,
                    decision_reason=OtherReason(reason=f"deny rule: {rule_str}"),
                    message=f"Denied by rule: {rule_str}",
                )
    # ask rules
    for source in _HIGH_PRIORITY_SOURCES:
        for rule_str in context.always_ask_rules.get(source.value, []):
            if _rule_matches(rule_str, cmd):
                return PermissionDecision(
                    behavior=PermissionBehavior.ASK.value,
                    decision_reason=OtherReason(reason=f"ask rule: {rule_str}"),
                    message=f"Asked by rule: {rule_str}",
                )
    # allow rules
    for source in _HIGH_PRIORITY_SOURCES:
        for rule_str in context.always_allow_rules.get(source.value, []):
            if _rule_matches(rule_str, cmd):
                return PermissionDecision(
                    behavior=PermissionBehavior.ALLOW.value,
                    decision_reason=OtherReason(reason=f"allow rule: {rule_str}"),
                )

    # 默认 passthrough(让上层 decide)
    return PermissionDecision(
        behavior=PermissionBehavior.PASSTHROUGH.value,
        decision_reason=OtherReason(reason="无匹配规则,passthrough"),
    )


def _rule_matches(rule_str: str, cmd: str) -> bool:
    """
    判断 rule_str 是否匹配 cmd
    对齐 CC bashToolCheckPermission 的 rule match

    rule_str 形态:
    - "Bash(rm:*)" → tool=Bash, content="rm:*"
    - "Bash" → tool=Bash, content=None(整个 tool 命中)
    - "Edit(...)" → tool≠Bash → False

    Args:
        rule_str: 规则字符串
        cmd: 待匹配的命令(已 strip safe wrappers)

    Returns:
        True 如果规则匹配且是 Bash 工具
    """
    parsed_tool, content = _parse_rule_string(rule_str)
    if parsed_tool != "Bash":
        return False
    rule = parse_permission_rule("Bash", content)
    return match_permission_rule(rule, cmd)


def _parse_rule_string(rule_str: str) -> tuple[str, Optional[str]]:
    """
    Bash(rm:*) → ('Bash', 'rm:*')  /  Bash → ('Bash', None)

    对齐 doc §4.5 _parse_rule_string
    """
    rule_str = rule_str.strip()
    if "(" in rule_str:
        name, rest = rule_str.split("(", 1)
        content = rest.rstrip(")")
        return name.strip(), content.strip() or None
    return rule_str, None


# ────────────────────────────────────────────────────────────────────
# check_sandbox_auto_allow — 沙箱 auto-allow 路径
# ────────────────────────────────────────────────────────────────────

def check_sandbox_auto_allow(
    tool_input: dict, context: ToolPermissionContext,
) -> PermissionDecision:
    """
    对齐 CC bashPermissions.ts:1829-1843 checkSandboxAutoAllow

    仅在 sandbox auto-allow 路径下调用:
    - 用与正常 bash_check_permissions 相同的 subcommand 级 rule 检查
    - 但不弹窗(沙箱兜底);deny rule 仍生效
    - ask rule 在沙箱路径下也透传(对齐 doc §6.3 "deny rules already checked")
    - 都通过 → ALLOW(reason: 'Auto-allowed in sandbox; deny rules already checked')

    实现:复刻 bash_check_permissions Step 1-4 但去掉 ask fallback
    """
    command = tool_input.get("command", "") or ""
    subcommands = parse_subcommands(command)

    if not subcommands:
        return PermissionDecision(
            behavior=PermissionBehavior.ASK.value,
            decision_reason=OtherReason(reason="无法解析 command (sandbox auto-allow)"),
        )

    # 拆分 + cap(与 bash_check_permissions 一致)
    if len(subcommands) > MAX_SUBCOMMANDS:
        return PermissionDecision(
            behavior=PermissionBehavior.ASK.value,
            decision_reason=OtherReason(
                reason=f"subcommand 数超 {MAX_SUBCOMMANDS}",
            ),
        )

    # 对每个 subcommand 跑 rule check(查 deny + ask)
    for sc in subcommands:
        sc_stripped = strip_safe_wrappers(sc.command)
        result = _check_single_command(sc_stripped, tool_input, context)
        if result.behavior == PermissionBehavior.DENY.value:
            return PermissionDecision(
                behavior=PermissionBehavior.DENY.value,
                decision_reason=OtherReason(
                    reason=f"沙箱 auto-allow 路径下 subcommand deny: {sc.command}",
                ),
                message=f"Denied by rule in sandbox: {sc.command}",
            )
        if result.behavior == PermissionBehavior.ASK.value:
            # ask rule 在沙箱内也透传(不绕过)
            return PermissionDecision(
                behavior=PermissionBehavior.ASK.value,
                decision_reason=OtherReason(
                    reason=f"沙箱 auto-allow 路径下 subcommand ask: {sc.command}",
                ),
                message=f"Asked by rule in sandbox: {sc.command}",
            )

    # 全 allow → ALLOW(沙箱兜底,不再弹窗)
    return PermissionDecision(
        behavior=PermissionBehavior.ALLOW.value,
        decision_reason=OtherReason(
            reason="Auto-allowed in sandbox; deny rules already checked",
        ),
    )
