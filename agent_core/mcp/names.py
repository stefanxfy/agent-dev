"""
MCP 工具命名安全化（纯函数）。

职责:
  - make_tool_name(server, tool) → mcp__<server>__<tool>（清洗非法字符）
  - server_prefix(server) → mcp__<server>（deny strip 匹配用）
  - is_server_denied(server, deny_rules) → bool（server 级 deny 判定）

对齐决策 #4: mcp__server__tool 前缀（与 builtin 单池混用，前缀防冒充）。
tool name 须满足 LLM API 约束 ^[a-zA-Z0-9_-]{1,64}$，mcp__s__t 合规。
跨 server 同名工具的去重由 ToolRegistry.register 天然保证（同名覆盖 + debug log）。
"""

import logging
import re

logger = logging.getLogger("agent_core.mcp")  # 🔌

# server/tool 名字段只保留 [A-Za-z0-9_]（- 和 . 等都归一为 _）
_SAFE_SEGMENT_RE = re.compile(r"[^A-Za-z0-9_]")
_FULL_NAME_RE = re.compile(r"^mcp__[A-Za-z0-9_]+__[A-Za-z0-9_]+$")


def _safe_segment(seg: str) -> str:
    """清洗名字段：非 [A-Za-z0-9_] → _，去首尾 _。空 → 'unnamed'。"""
    cleaned = _SAFE_SEGMENT_RE.sub("_", str(seg)).strip("_")
    return cleaned or "unnamed"


def make_tool_name(server: str, tool: str) -> str:
    """server + tool → mcp__<server>__<tool>（对齐决策 #4，前缀防冒充 builtin）。"""
    name = f"mcp__{_safe_segment(server)}__{_safe_segment(tool)}"
    if not _FULL_NAME_RE.match(name):
        logger.warning("🔌 mcp tool name 清洗后仍异常: server=%s tool=%s -> %s", server, tool, name)
    if len(name) > 64:
        # LLM tool name 上限 64 字符；超长截断 tool 段
        name = name[:64]
        logger.warning("🔌 mcp tool name 超 64 字符，已截断: %s", name)
    return name


def server_prefix(server: str) -> str:
    """server 级前缀，用于 deny strip 匹配：mcp__<server>。"""
    return f"mcp__{_safe_segment(server)}"


def is_server_denied(server: str, deny_rule_strings) -> bool:
    """
    判断某 server 是否被 server 级 deny 命中（第一/二道 strip 用）。

    匹配规则（对齐 PermissionRule 字符串语义）:
      - rule == server_prefix(server)              → 整 server deny（mcp__fs）
      - rule startswith server_prefix + "*"        → 通配（mcp__fs*）
      - rule startswith server_prefix + "("        → 参数形态（mcp__fs(...)）

    防前缀误匹配: mcp__fs 不命中 mcp__fs2（因为 fs2 的 prefix 是 mcp__fs2）。
    """
    prefix = server_prefix(server)
    for r in deny_rule_strings or []:
        r = str(r).strip()
        if not r:
            continue
        if r == prefix or r.startswith(prefix + "*") or r.startswith(prefix + "("):
            return True
    return False
