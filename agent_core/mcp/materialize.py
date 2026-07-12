"""
MCP 物化层: 把 server 返回的 Tool 转成 agent_core 的 ToolDef。

职责:
  - materialize_tools(server_name, mcp_tools, manager) → list[ToolDef]
    每个工具: 命名 mcp__server__tool / category="mcp" / parameters 透传 inputSchema
    / handler 是同步闭包，调 manager.call_tool
  - _normalize_call_result(CallToolResult) → str（manager.call_tool 内部也调用）
    text 取 .text；image/audio/resource → 占位符；isError → 抛异常（被 execute 捕获返 error）；
    structuredContent → 追加 JSON
  - _sanitize_unicode: 控制字符（除 \\t\\n\\r）替换为 \\xNN，防 LLM token 解析炸 / prompt injection
    （对齐 CC 封装前消毒）

对齐决策 #9: 同步 handler，base.py 零改动（C-浅 不做）。
对齐决策 #4: 命名 mcp__server__tool（调用 names.make_tool_name）。
"""

from __future__ import annotations

import json
import logging
import re
from typing import TYPE_CHECKING, Any

from agent_core.tools.base import ToolDef

from agent_core.mcp.names import make_tool_name

if TYPE_CHECKING:
    from agent_core.mcp.manager import McpManager

logger = logging.getLogger("agent_core.mcp")  # 🔌

# 控制字符（除 \t \n \r）→ 替换为 \xNN
_CONTROL_CHAR_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def materialize_tools(
    server_name: str,
    mcp_tools: Any,
    manager: "McpManager",
) -> list[ToolDef]:
    """MCP Tool 列表 → ToolDef 列表（同步 handler，category=mcp，命名 mcp__server__tool）。"""
    out: list[ToolDef] = []
    seen_names: set[str] = set()
    for t in mcp_tools or []:
        raw_name = getattr(t, "name", None) or "unnamed"
        safe_name = make_tool_name(server_name, raw_name)

        # 同 server 内清洗后重名 → 加后缀 _2/_3（跨 server 冲突由 ToolRegistry.register 覆盖）
        if safe_name in seen_names:
            i = 2
            while f"{safe_name}_{i}" in seen_names:
                i += 1
            safe_name = f"{safe_name}_{i}"
            logger.warning("🔌 mcp tool name 冲突(server=%s raw=%s)，重命名为 %s", server_name, raw_name, safe_name)
        seen_names.add(safe_name)

        out.append(ToolDef(
            name=safe_name,
            description=_build_description(server_name, t),
            parameters=_extract_schema(t),
            handler=_make_handler(manager, server_name, raw_name),
            category="mcp",
        ))
    return out


def _extract_schema(t: Any) -> dict:
    """透传 MCP tool 的 inputSchema；无/非法 → 兜底空 object schema（R5）。"""
    schema = getattr(t, "inputSchema", None)
    if not isinstance(schema, dict) or not schema:
        return {"type": "object", "properties": {}}
    return schema


def _build_description(server_name: str, t: Any) -> str:
    """[mcp:server] 原描述 —— 帮 LLM 区分来源 + 权限 UI 可读。"""
    desc = getattr(t, "description", None) or "(no description)"
    return f"[mcp:{server_name}] {desc}"


def _make_handler(manager: "McpManager", server: str, tool_name: str):
    """同步 handler 闭包: 调 manager.call_tool（决策 #9: 同步，不改 base.py）。

    接收 base.py 注入的 _cancel_event kwarg 但 MCP 一次性 RPC 不响应取消，吞掉（R8）。
    """
    def handler(**kwargs):
        kwargs.pop("_cancel_event", None)
        return manager.call_tool(server, tool_name, kwargs)
    return handler


def _normalize_call_result(result: Any) -> str:
    """CallToolResult → str。

    isError=True → raise RuntimeError（被 ToolRegistry.execute 捕获，返 error result）。
    content 遍历: text 取 .text；image/audio/resource → 安全占位符（MVP 不内联二进制）。
    structuredContent → 追加 JSON。最后 _sanitize_unicode 消毒。
    """
    if getattr(result, "isError", False):
        texts = [c.text for c in (result.content or []) if getattr(c, "type", None) == "text"]
        raise RuntimeError(f"MCP tool error: {' '.join(texts) or 'unknown'}")

    parts: list[str] = []
    for c in (result.content or []):
        ctype = getattr(c, "type", None)
        if ctype == "text":
            parts.append(c.text)
        elif ctype == "image":
            data = getattr(c, "data", "") or ""
            mime = getattr(c, "mimeType", "?")
            parts.append(f"[image: {mime}, {len(data)} chars base64]")
        elif ctype == "audio":
            mime = getattr(c, "mimeType", "?")
            parts.append(f"[audio: {mime}]")
        elif ctype == "resource":
            parts.append(f"[resource: {getattr(c, 'uri', '?')}]")
        else:
            parts.append(f"[{ctype or 'unknown'} content]")

    structured = getattr(result, "structuredContent", None)
    if structured:
        try:
            parts.append(f"[structured] {json.dumps(structured, ensure_ascii=False)}")
        except (TypeError, ValueError):
            parts.append("[structured: <unserializable>]")

    text = "\n".join(parts) if parts else "(empty result)"
    return _sanitize_unicode(text)


def _sanitize_unicode(text: str) -> str:
    """控制字符（除 \\t\\n\\r）→ \\xNN，防 LLM token 解析炸 / prompt injection。"""
    return _CONTROL_CHAR_RE.sub(lambda m: f"\\x{ord(m.group()):02x}", text)


# ── resources 物化（Phase 2 Step 2：两个固定全局工具）──────────────
def materialize_resource_tools(manager) -> list:
    """两个固定全局 ToolDef：list_mcp_resources / read_mcp_resource（参数路由，对齐 Claude Code）。

    不为每个 resource 生成工具（资源可能海量）——LLM 先 list 再 read。
    组合根须 guard 只注册一次（多 server 共享同一对工具）。
    """
    return [
        ToolDef(
            name="list_mcp_resources",
            description=(
                "[mcp] 列出所有已连接 MCP server 暴露的 resources（可选 server 过滤）。"
                "返回每行 '[server] uri — description'。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "server": {"type": "string", "description": "可选：只列指定 server 的 resources"},
                },
            },
            handler=_make_list_resources_handler(manager),
            category="mcp",
        ),
        ToolDef(
            name="read_mcp_resource",
            description=(
                "[mcp] 读取指定 server 的 resource 内容（按 uri）。"
                "uri 来自 list_mcp_resources 的输出。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "server": {"type": "string"},
                    "uri": {"type": "string"},
                },
                "required": ["server", "uri"],
            },
            handler=_make_read_resource_handler(manager),
            category="mcp",
        ),
    ]


def _make_list_resources_handler(manager):
    def handler(**kwargs):
        kwargs.pop("_cancel_event", None)
        return manager.list_resources(kwargs.get("server"))
    return handler


def _make_read_resource_handler(manager):
    def handler(**kwargs):
        kwargs.pop("_cancel_event", None)
        return manager.read_resource(kwargs["server"], kwargs["uri"])
    return handler


def _normalize_resource_result(result: Any) -> str:
    """ReadResourceResult → str。contents 是 list[TextResourceContents|BlobResourceContents]。

    text 取 .text；blob（base64 str）→ 占位符；复用 _sanitize_unicode。
    """
    parts: list[str] = []
    for c in (result.contents or []):
        text = getattr(c, "text", None)
        if text is not None:
            parts.append(text)
        else:
            blob = getattr(c, "blob", None)
            if blob is not None:
                mime = getattr(c, "mimeType", "?")
                parts.append(f"[blob: {mime}, {len(blob)} chars base64]")
            else:
                parts.append(f"[{getattr(c, 'uri', '?')} content]")
    text = "\n".join(parts) if parts else "(empty resource)"
    return _sanitize_unicode(text)


# ── prompts 物化（Phase 2 Step 3：get_mcp_prompt 工具 + 段渲染）─────
def materialize_prompt_tools(manager) -> list:
    """一个固定全局 ToolDef：get_mcp_prompt（取 server 的 prompt 模板渲染后内容）。

    名单经 McpPromptsHandler 段注入 system prompt，LLM 据此用本工具取内容（对齐 skill 模式）。
    组合根 guard 只注册一次。
    """
    return [ToolDef(
        name="get_mcp_prompt",
        description=(
            "[mcp] 获取指定 server 的 prompt 模板渲染后内容。"
            "name / 参数来自 system prompt 的 <available_mcp_prompts> 名单。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "server": {"type": "string"},
                "name": {"type": "string"},
                "arguments": {"type": "object", "description": "prompt 参数（可选，key→value）"},
            },
            "required": ["server", "name"],
        },
        handler=_make_get_prompt_handler(manager),
        category="mcp",
    )]


def _make_get_prompt_handler(manager):
    def handler(**kwargs):
        kwargs.pop("_cancel_event", None)
        return manager.get_prompt(kwargs["server"], kwargs["name"], kwargs.get("arguments"))
    return handler


def _normalize_prompt_result(result: Any) -> str:
    """GetPromptResult → str。messages 是 list[PromptMessage(role, content: ContentBlock)]。"""
    parts: list[str] = []
    for m in (result.messages or []):
        role = getattr(m, "role", "user")
        parts.append(f"[{role}] {_content_block_to_text(getattr(m, 'content', None))}")
    text = "\n".join(parts) if parts else "(empty prompt)"
    return _sanitize_unicode(text)


def _content_block_to_text(content: Any) -> str:
    """单个 ContentBlock → str（text/image/audio/resource 占位）。"""
    ctype = getattr(content, "type", None)
    if ctype == "text":
        return getattr(content, "text", "")
    if ctype == "image":
        return f"[image: {getattr(content, 'mimeType', '?')}]"
    if ctype == "audio":
        return f"[audio: {getattr(content, 'mimeType', '?')}]"
    if ctype == "resource":
        return f"[resource: {getattr(content, 'uri', '?')}]"
    return f"[{ctype or 'unknown'} content]"


def render_mcp_prompts_section(prompts_by_server: dict) -> str:
    """渲染 MCP prompts 名单段（仿 skills.render_skills_section），供 McpPromptsHandler 注入。

    prompts_by_server: {server_name: list[types.Prompt]}。空 → 返 ""（不注入）。
    """
    if not prompts_by_server or not any(prompts_by_server.values()):
        return ""
    lines = [
        "",
        "## MCP Prompts (optional)",
        "可用 MCP prompt 模板（用 get_mcp_prompt 工具按 server+name 取渲染内容）:",
        "<available_mcp_prompts>",
    ]
    for server, prompts in prompts_by_server.items():
        for p in prompts or []:
            pname = getattr(p, "name", "?")
            desc = getattr(p, "description", "") or ""
            args = getattr(p, "arguments", None) or []
            arg_str = ", ".join(getattr(a, "name", "?") for a in args) if args else ""
            entry = f"- server={server} name={pname}"
            if arg_str:
                entry += f" args=[{arg_str}]"
            if desc:
                entry += f" — {desc}"
            lines.append(entry)
    lines.append("</available_mcp_prompts>")
    return "\n".join(lines)
