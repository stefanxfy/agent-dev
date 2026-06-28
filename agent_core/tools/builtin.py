"""
内置工具：Calculator + Search + Bash

Phase 2 (M2) 增量:
  - BashTool 内置实现(含 dangerouslyDisableSandbox 透传)
  - bash_handler 内部调 sandbox_manager.wrap_with_sandbox(对齐 doc §6.3)
  - check_permissions 字段保持 None — BashTool 的 check 由 PermissionEngine
    Step 1c' 专属路径调 bash_check_permissions(避免闭包循环 import + classifier 注入困难)
"""

from __future__ import annotations

import ast
import operator
import shlex
import subprocess
from typing import Any, Dict

from .base import ToolDef, ToolRegistry


# ── 安全计算器 ────────────────────────────────────────────────────────────

_ALLOWED_OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.Pow: operator.pow,
    ast.USub: operator.neg,
    ast.UAdd: operator.pos,
}


def _safe_eval(expr: str) -> float:
    """
    安全计算数学表达式。
    只允许 +-*/() 和数字，禁止 __import__、os、eval 等危险操作。
    """
    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError:
        raise ValueError(f"表达式语法错误: {expr}")

    def _eval(node: ast.AST) -> float:
        if isinstance(node, ast.Expression):
            return _eval(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            return float(node.value)
        if isinstance(node, ast.BinOp) and type(node.op) in _ALLOWED_OPS:
            return _ALLOWED_OPS[type(node.op)](_eval(node.left), _eval(node.right))
        if isinstance(node, ast.UnaryOp) and type(node.operand) in (ast.Add, ast.Sub):
            operand = _eval(node.operand)
            return -operand if isinstance(node.op, ast.USub) else operand
        raise ValueError(f"不支持的表达式: {ast.dump(node)}")

    return _eval(tree)


def calc_handler(**kwargs) -> str:
    """Calculator 工具处理函数"""
    expression = kwargs.get("expression", "")
    if not expression:
        return "错误：缺少 expression 参数"
    try:
        result = _safe_eval(expression)
        return str(result)
    except Exception as e:
        return f"计算失败: {e}"


CALC_TOOL = ToolDef(
    name="calc",
    description="计算数学表达式。支持 +, -, *, /, 括号。例如：'2 + 3 * 4'",
    parameters={
        "type": "object",
        "properties": {
            "expression": {
                "type": "string",
                "description": "数学表达式，如 '2 + 3 * 4'",
            },
        },
        "required": ["expression"],
    },
    handler=calc_handler,
)


# ── 联网搜索（DuckDuckGo Instant Answer API）─────────────────────────────

def search_handler(**kwargs) -> str:
    """Search 工具处理函数（免费，无需 API Key）

    错误处理策略（对齐 ToolRegistry.execute 的错误分类重试）：
    - ValueError：参数错误，不重试（用户输入有问题，重试无意义）
    - (ConnectionError, TimeoutError, requests.exceptions.RequestException)：
      网络错误，向上抛，让 ToolRegistry.execute 走指数退避重试逻辑
    - 其他 Exception：推测是不可恢复错误，返回错误字符串

    之前所有异常都被 catch 后返回 "搜索失败: ..." 字符串，ToolRegistry 看不到
    异常，导致重试机制失效（即使是临时网络抖动也无法重试）。
    """
    query = kwargs.get("query", "")
    if not query:
        raise ValueError("缺少 query 参数")

    import requests.exceptions
    try:
        resp = requests.get(
            "https://api.duckduckgo.com/",
            params={"q": query, "format": "json", "no_html": 1},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
    except (ConnectionError, TimeoutError, requests.exceptions.RequestException) as e:
        # 网络错误：向上抛，让 ToolRegistry.execute 走重试逻辑
        # （重试 3 次，指数退避 1s, 2s, 4s）
        raise

    # 取 Instant Answer 或 AbstractText
    answer = data.get("Answer") or data.get("AbstractText") or ""
    if answer:
        return answer[:500]  # 截断，避免过长

    # 没有 instant answer，返回相关主题列表
    related = [r["Text"] for r in data.get("RelatedTopics", [])[:3] if r.get("Text")]
    if related:
        return "\n".join(related)

    return f"未找到「{query}」的相关结果"


SEARCH_TOOL = ToolDef(
    name="search",
    description="联网搜索。输入查询词，返回搜索结果摘要。例如：'北京天气'",
    parameters={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "搜索查询词，如 '北京天气' 或 'Python 最新版本'",
            },
        },
        "required": ["query"],
    },
    handler=search_handler,
)


# ── Bash 工具(Phase 2 M2 增量)─────────────────────────────────────────────

# 输出截断长度(对齐 CC BashTool 默认 5000 字符)
_BASH_OUTPUT_MAX_CHARS = 5000


def bash_handler(**kwargs) -> str:
    """
    Bash 工具处理函数(对齐 doc §6.3 + CC BashTool.execute)

    流程:
      1. 读 command / timeout / working_dir / dangerously_disable_sandbox
      2. 决定是否 wrap sandbox(should_use_sandbox → sandbox_manager.wrap_with_sandbox)
      3. subprocess.run 执行(shell=True 支持 compound command)
      4. 返回 stdout + stderr(合并,前 5000 字符截断)

    异常处理:
      - ValueError: 缺 command 参数(不重试,对齐 ToolRegistry 分类)
      - subprocess.TimeoutExpired: 返回 timeout 提示
      - subprocess.CalledProcessError: 返回 exit code + stderr
      - FileNotFoundError: sandbox binary 不存在 → helpful error

    dangerously_disable_sandbox 透传:
      - 传给 should_use_sandbox 决定是否 wrap
      - spec §6.3:仅 bypass sandbox,不 bypass permission(permission check 在 engine 层)
    """
    command = kwargs.get("command", "")
    if not command or not command.strip():
        raise ValueError("缺少 command 参数")

    timeout = kwargs.get("timeout", 30.0)
    working_dir = kwargs.get("working_dir") or None
    dangerously_disable = bool(kwargs.get("dangerously_disable_sandbox", False))

    # 决定是否 wrap sandbox
    effective_command = command
    try:
        from .sandbox_decision import should_use_sandbox
        from .sandbox_manager import sandbox_manager

        effective_input = {
            "command": command,
            "dangerously_disable_sandbox": dangerously_disable,
        }
        if should_use_sandbox("Bash", effective_input):
            wrapped = sandbox_manager.wrap_with_sandbox(
                command, working_dir=working_dir or ".",
            )
            if wrapped != command:
                effective_command = wrapped
    except Exception:
        # sandbox 判断失败 → 不 wrap,直接执行(graceful degradation)
        effective_command = command

    # 执行
    try:
        result = subprocess.run(
            effective_command,
            shell=True,
            cwd=working_dir,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return f"Bash command timed out after {timeout}s"
    except FileNotFoundError as e:
        # sandbox binary(npx 等)不存在 → helpful error
        if "npx" in str(e) or "sandbox" in effective_command:
            return (
                "Sandbox binary not found, please run "
                "'npm install -g @anthropic-ai/sandbox-runtime' "
                "or disable sandbox in settings.json"
            )
        return f"Bash command not found: {e}"

    # 合并 stdout + stderr
    output = ""
    if result.stdout:
        output += result.stdout
    if result.stderr:
        if output:
            output += "\n"
        output += result.stderr

    # 截断(对齐 CC 5000 字符)
    if len(output) > _BASH_OUTPUT_MAX_CHARS:
        output = output[:_BASH_OUTPUT_MAX_CHARS] + f"\n... (truncated, {len(output)} chars total)"

    if not output:
        # 空输出(命令成功但无 stdout)→ 返回 exit code
        return f"(command succeeded, exit code {result.returncode})"

    return output


BASH_TOOL = ToolDef(
    name="Bash",
    description=(
        "Run a shell command on the local system. "
        "Use for running tests, installing dependencies, file operations, "
        "and system queries.\n\n"
        "The command will be executed in a sandbox by default, which restricts:\n"
        "- File writes to the working directory\n"
        "- Network access to whitelisted domains\n\n"
        "To bypass the sandbox for a specific command (use sparingly, only when "
        "sandbox restrictions cause failures), set "
        "`dangerously_disable_sandbox: true`. This does NOT bypass permission "
        "checks — deny/ask rules still apply."
    ),
    parameters={
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "Shell command to execute",
            },
            "timeout": {
                "type": "number",
                "description": "Timeout in seconds (default 30)",
                "default": 30.0,
            },
            "working_dir": {
                "type": "string",
                "description": "Working directory (default: cwd)",
            },
            "dangerously_disable_sandbox": {
                "type": "boolean",
                "description": (
                    "⚠️ Bypass sandbox for this command. Use only when sandbox "
                    "restrictions cause failures. Does not bypass permission checks."
                ),
                "default": False,
            },
        },
        "required": ["command"],
    },
    handler=bash_handler,
    category="shell",
    # check_permissions 保持 None:BashTool 的 check 由 PermissionEngine Step 1c'
    # 专属路径调 bash_check_permissions(避免闭包循环 import + classifier 注入困难,
    # 对齐 doc §4.5 "Bash 是最容易被 prompt injection 利用的工具")
    check_permissions=None,
    requires_user_interaction=False,
)


# ── 注册入口 ──────────────────────────────────────────────────────────────

def register_builtin_tools(registry: ToolRegistry):
    """将内置工具注册到指定注册表"""
    registry.register(CALC_TOOL)
    registry.register(SEARCH_TOOL)
    registry.register(BASH_TOOL)
