"""
MCP 配置面（纯数据，无连接、无副作用）。

职责:
  - McpServerConfig dataclass: 单个 server 配置（kind=stdio/http 判别）
  - parse_mcp_servers(settings): 从 settings["mcp"]["servers"] 解析（容错）
  - load_mcp_config_from_settings(): 复用 permission_loader.load_settings_json

对齐决策 #7: 配置走 settings.json 的 mcp.servers 段（结构化配置不走 .env）。
解析风格对齐 load_settings_json 的容错: 缺字段/类型错 → 跳过 + warn。

settings.json schema:
  {
    "mcp": {
      "servers": {
        "<name>": {
          "type": "stdio" | "http" | "sse",        # 或 "transport"; sse 归一为 http
          "command": "...", "args": [...], "env": {...}, "cwd": "...",   # stdio
          "url": "...", "headers": {...}, "timeout": 30, "sseReadTimeout": 300,  # http
          "enabled": true, "connectTimeout": 30
        }
      }
    }
  }
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Literal, Optional

logger = logging.getLogger("agent_core.mcp")  # 🔌


@dataclass
class McpServerConfig:
    """单个 MCP server 配置（判别联合用 kind 字段）。"""

    name: str
    kind: Literal["stdio", "http"]
    # stdio 字段
    command: Optional[str] = None
    args: list[str] = field(default_factory=list)
    env: Optional[dict[str, str]] = None
    cwd: Optional[str] = None
    # http 字段
    url: Optional[str] = None
    headers: Optional[dict[str, str]] = None
    timeout: Optional[float] = None
    sse_read_timeout: Optional[float] = None
    # 通用
    enabled: bool = True
    connect_timeout: float = 30.0


def parse_mcp_servers(settings: Any) -> list[McpServerConfig]:
    """
    从 settings dict 的 mcp.servers 段解析出 McpServerConfig 列表（容错）。

    缺段/类型错 → 返空；单 server 解析失败 → 跳过该 server + warn，不阻断其余。
    """
    if not isinstance(settings, dict):
        return []
    mcp_section = settings.get("mcp")
    if not isinstance(mcp_section, dict):
        return []
    servers = mcp_section.get("servers")
    if not isinstance(servers, dict):
        return []

    out: list[McpServerConfig] = []
    for name, raw in servers.items():
        try:
            cfg = _parse_one(str(name), raw)
        except Exception as e:
            logger.warning("🔌 mcp server '%s' 配置解析失败，跳过: %s", name, e)
            continue
        if cfg is not None:
            out.append(cfg)
    return out


def _parse_one(name: str, raw: Any) -> Optional[McpServerConfig]:
    """解析单个 server 配置。enabled=False 仍返回（上层决定是否跳过连接）。"""
    if not isinstance(raw, dict):
        raise ValueError(f"server 配置不是 dict: {type(raw).__name__}")

    kind_raw = raw.get("type") or raw.get("transport")
    if kind_raw == "stdio":
        kind = "stdio"
    elif kind_raw in ("http", "streamable-http", "sse"):
        kind = "http"  # sse 走 streamablehttp_client（SDK 内部处理）
    elif kind_raw is None:
        # 无 type：按 command/url 推断
        if raw.get("command"):
            kind = "stdio"
        elif raw.get("url"):
            kind = "http"
        else:
            raise ValueError("无法判定 transport：缺 type/command/url")
    else:
        raise ValueError(f"未知 transport type: {kind_raw!r}（MVP 仅支持 stdio/http/sse）")

    cfg = McpServerConfig(
        name=name,
        kind=kind,
        command=_opt_str(raw.get("command")),
        args=_opt_list_str(raw.get("args")),
        env=_opt_dict_str(raw.get("env")),
        cwd=_opt_str(raw.get("cwd")),
        url=_opt_str(raw.get("url")),
        headers=_opt_dict_str(raw.get("headers")),
        timeout=_opt_float(raw.get("timeout")),
        sse_read_timeout=_opt_float(raw.get("sse_read_timeout") or raw.get("sseReadTimeout")),
        enabled=bool(raw.get("enabled", True)),
        connect_timeout=_opt_float(raw.get("connectTimeout")) or 30.0,
    )

    if kind == "stdio" and not cfg.command:
        raise ValueError("stdio server 缺 command")
    if kind == "http" and not cfg.url:
        raise ValueError("http server 缺 url")

    return cfg


def load_mcp_config_from_settings() -> list[McpServerConfig]:
    """复用 permission_loader.load_settings_json，取 mcp.servers 段解析。"""
    from agent_core.tools.permission_loader import load_settings_json

    data = load_settings_json()
    configs = parse_mcp_servers(data)
    logger.debug(
        "🔌 mcp config loaded: %d servers %s",
        len(configs), [c.name for c in configs],
    )
    return configs


# ── roots（client 级，声明给 server 的可访问根目录）─────────────────
def parse_mcp_roots(settings: Any) -> list[dict]:
    """
    解析 mcp.roots（client 级，声明给 server 的可访问根目录；非 per-server）。

    settings 结构: {"mcp": {"roots": [{"uri": "file:///path", "name": "..."}, ...]}}
    或简写 ["file:///path", ...]。

    缺省（未配）→ 返当前工作目录，让 client 总声明 roots capability
    （学习目的：让 server 能问 roots，对齐 P3 决策）。
    Root uri 必须是 file://（MCP Root.uri 是 FileUrl）。
    """
    if not isinstance(settings, dict):
        return _default_roots()
    mcp_section = settings.get("mcp")
    if not isinstance(mcp_section, dict):
        return _default_roots()
    roots_raw = mcp_section.get("roots")
    if roots_raw is None:
        return _default_roots()

    out: list[dict] = []
    if isinstance(roots_raw, list):
        for r in roots_raw:
            if isinstance(r, dict) and r.get("uri"):
                out.append({"uri": _to_file_uri(str(r["uri"])), "name": str(r.get("name", "")) or ""})
            elif isinstance(r, str):
                out.append({"uri": _to_file_uri(r), "name": ""})
    return out or _default_roots()


def _to_file_uri(p: str) -> str:
    """归一为 file:// URI（绝对路径 /x → file:///x）。"""
    if p.startswith("file://"):
        return p
    if p.startswith("/"):
        return "file://" + p          # /abs → file:///abs（// + / = ///）
    return "file:///" + p              # 相对路径兜底


def _default_roots() -> list[dict]:
    import os
    cwd = os.getcwd()                  # /Users/... → file:///Users/...
    return [{"uri": "file://" + cwd, "name": "cwd"}]


def load_mcp_roots() -> list[dict]:
    """复用 load_settings_json，取 mcp.roots 段（缺省 cwd）。"""
    from agent_core.tools.permission_loader import load_settings_json
    return parse_mcp_roots(load_settings_json())


# ── 类型安全的取值 helper ────────────────────────────────────────────
def _opt_str(v: Any) -> Optional[str]:
    return str(v) if v is not None else None


def _opt_list_str(v: Any) -> list[str]:
    if v is None:
        return []
    if not isinstance(v, list):
        raise ValueError(f"args 不是 list: {type(v).__name__}")
    return [str(x) for x in v]


def _opt_dict_str(v: Any) -> Optional[dict[str, str]]:
    if v is None:
        return None
    if not isinstance(v, dict):
        raise ValueError(f"期望 dict，得到 {type(v).__name__}")
    return {str(k): str(val) for k, val in v.items()}


def _opt_float(v: Any) -> Optional[float]:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        raise ValueError(f"期望 number，得到 {v!r}")
