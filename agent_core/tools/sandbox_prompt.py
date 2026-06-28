"""
注入沙箱规则到 system prompt
对齐 Claude Code src/tools/BashTool/prompt.ts getSimpleSandboxSection (172-273)

$TMPDIR 处理:CC 把它字面量化(替换成实际 sandbox tmp dir 路径)注入 prompt,
不是保留字面量 —— 目的:让 prompt 在不同用户间可缓存(全局 prompt cache 命中率)。

禁用沙箱时返回空字符串(不注入)。
"""

from __future__ import annotations

from .sandbox_manager import sandbox_manager


def get_sandbox_prompt_section() -> str:
    """
    注入沙箱规则到 system prompt
    对齐 Claude Code src/tools/BashTool/prompt.ts getSimpleSandboxSection

    Returns:
        沙箱规则 prompt 段(禁用时返 "")

    关键约束(对齐 doc §5.4):
    - tmpdir 字面化(非 $TMPDIR),便于跨用户 prompt cache
    - 告诉 LLM 默认沙箱化,不要主动 dangerously_disable_sandbox
    - 失败时才用 disable,要解释原因
    - 警告不要建议加敏感路径到 allowlist
    - strict mode(allow_unsandboxed_commands=False)追加 STRICT MODE 块
    """
    if not sandbox_manager.is_sandbox_enabled():
        return ""

    cfg = sandbox_manager._config
    tmpdir_literal = sandbox_manager._get_sandbox_tmp_dir()  # 字面化,非 $TMPDIR
    strict_mode = not cfg.allow_unsandboxed_commands

    fs_read = cfg.fs_allow_read or []
    fs_write_allowed = cfg.fs_allow_write or [".", tmpdir_literal]
    network_allowed = cfg.network_allowed_domains or []
    fs_deny_read = cfg.fs_deny_read or []
    fs_deny_write = cfg.fs_deny_write or []
    network_denied = cfg.network_denied_domains or []

    prompt = f"""## Command sandbox
By default, your command will be run in a sandbox. This sandbox controls
which directories and network hosts commands may access or modify without
an explicit override.

The sandbox has the following restrictions:
Filesystem: {{ "read": {{"allow": {fs_read}, "deny": {fs_deny_read}}}, "write": {{"allow": {fs_write_allowed}, "deny": {fs_deny_write}}} }}
Network: {{ "allowedHosts": {network_allowed}, "deniedHosts": {network_denied} }}

- You should always default to running commands within the sandbox.
  Do NOT attempt to set `dangerously_disable_sandbox: true` unless:
  - The user *explicitly* asks you to bypass sandbox
  - A specific command just failed and you see evidence of sandbox restrictions causing the failure.
  Evidence of sandbox-caused failures includes:
    - "Operation not permitted" errors for file/network operations
    - Access denied to specific paths outside allowed directories
    - Network connection failures to non-whitelisted hosts
    - Unix socket connection errors
  When you see evidence of sandbox-caused failure:
    - Immediately retry with `dangerously_disable_sandbox: true` (don't ask, just do it)
    - Briefly explain what sandbox restriction likely caused the failure.
- Treat each command you execute with `dangerously_disable_sandbox: true` individually.
- Do not suggest adding sensitive paths like ~/.bashrc, ~/.zshrc, ~/.ssh/*,
  or credential files to the sandbox allowlist.

- For temporary files, always use the `{tmpdir_literal}` directory
  (literal path, NOT $TMPDIR). This path is sandbox-writable.
  Do NOT use `/tmp` directly - use this path instead.
"""

    if strict_mode:
        # 对齐 CC 严格模式变体(prompt.ts:292)
        prompt += """
**STRICT MODE**: `dangerously_disable_sandbox` is **disabled** by your
organization's policy. You cannot bypass the sandbox. If a command
fails inside the sandbox, you must work around the restriction by:
  - using a different command that achieves the same goal
  - asking the user to add the path/host to the sandbox allowlist
  - or asking the user to explicitly run the command outside this session
"""

    return prompt
