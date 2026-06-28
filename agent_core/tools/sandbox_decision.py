"""
每条 tool_use 是否走沙箱(对齐 CC shouldUseSandbox + doc §5.3)

判定逻辑(四段短路):
1. sandbox 未启用 → False
2. dangerously_disable_sandbox=True 且 allow_unsandboxed_commands=True → False(模型主动绕过 + 用户允许)
3. tool_name not in (Bash/Read/Write/Edit) → False(calc/search 不需要)
4. _is_excluded_command 命中 → False(用户排除,UX 而非安全)

注意:excluded_commands 是 UX 而非安全(spec §5.3 注释)—— 被排除的命令
仍走应用层 permission check,只是不在 OS 沙箱里跑。
"""

from __future__ import annotations

from typing import Any

from .sandbox_manager import sandbox_manager


# 哪些工具需要走沙箱(对齐 doc §5.3 表)
SANDBOXED_TOOLS = frozenset({"Bash", "Read", "Write", "Edit"})


def should_use_sandbox(tool_name: str, tool_input: dict) -> bool:
    """
    判定一次 tool_use 是否走 OS 沙箱(对齐 CC shouldUseSandbox + doc §5.3)

    Args:
        tool_name: 工具名("Bash" / "Read" / "calc" / ...)
        tool_input: 工具输入(可能含 dangerously_disable_sandbox)

    Returns:
        True 如果该 tool_use 应在 OS 沙箱内执行

    四段短路(任一为 True 即 False):
      1. sandbox 未启用
      2. 模型主动 dangerously_disable_sandbox + 用户允许绕过
      3. 工具不在沙箱化名单(calc/search 等不需要)
      4. 命令被 excluded_commands 排除(UX)
    """
    # 1. sandbox 未启用
    if not sandbox_manager.is_sandbox_enabled():
        return False

    # 2. 模型主动 dangerously_disable_sandbox + 用户允许绕过
    if tool_input.get("dangerously_disable_sandbox"):
        if sandbox_manager._config.allow_unsandboxed_commands:
            return False  # 模型主动绕过 + 用户允许
        # allow_unsandboxed_commands=False(strict mode)→ 仍沙箱化
        # 继续 fall through(不 return),最终落到 SANDBOXED_TOOLS 判断

    # 3. 工具不在沙箱化名单
    if tool_name not in SANDBOXED_TOOLS:
        return False

    # 4. 命令被 excluded_commands 排除(只对 Bash 检查)
    if _is_excluded_command(tool_name, tool_input):
        return False

    return True


def _is_excluded_command(tool_name: str, tool_input: dict) -> bool:
    """
    对齐 CC containsExcludedCommand — UX 而非安全

    只对 Bash 工具检查(spec §5.3 注释)。
    excluded_commands 是 substring match(大小写敏感)。

    Args:
        tool_name: 工具名
        tool_input: 工具输入(从 "command" 字段取命令)

    Returns:
        True 如果命令匹配任一 excluded pattern
    """
    if tool_name != "Bash":
        return False
    cmd = tool_input.get("command", "") or ""
    for pattern in sandbox_manager._config.excluded_commands:
        if pattern and pattern in cmd:
            return True
    return False
