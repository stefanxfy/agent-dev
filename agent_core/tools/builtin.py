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
import contextvars
import logging
import operator
import shlex
import subprocess
import threading
import time
from typing import Any, Dict, Optional

from .base import ToolDef, ToolRegistry


# ⚙️ sandbox 子系统 logger(也覆盖 builtin 工具链路)
sandbox_logger = logging.getLogger("agent_core.sandbox")


# ── D15-b: in-flight cancel 通道 ─────────────────────────────────────
# 线程安全的 ContextVar:agent.run() 在执行 tool 前 set 当前 cancel_event,
# bash_handler 读它启动 watcher。ContextVar 在 thread 间共享同一 var。
_current_cancel_event: contextvars.ContextVar[Optional[threading.Event]] = (
    contextvars.ContextVar("current_cancel_event", default=None)
)


def set_current_cancel_event(event: Optional[threading.Event]) -> contextvars.Token:
    """agent.run() 调用:设当前 cancel_event 给 sub-process handler 读。

    返回 token,调用方负责 reset (用 reset_current_cancel_event)。
    """
    return _current_cancel_event.set(event)


def reset_current_cancel_event(token: contextvars.Token) -> None:
    """配合 set_current_cancel_event — finally 块中复原。"""
    _current_cancel_event.reset(token)


# ── D15-b subprocess 调度 helper ─────────────────────────────────────
# 把 subprocess 实际调用包成 helper,便于测试 patch + 保持 cancel 能力。
# 有 cancel_event → Popen + watcher(支持 in-flight interrupt)
# 无 cancel_event → subprocess.run(timeout=...) 简单路径(向后兼容,
#   测试可以 patch 这个名字,不影响 production)。
def _run_subprocess_with_cancel(
    cmd: str,
    *,
    cwd: Optional[str] = None,
    timeout: float,
    cancel_event: Optional[threading.Event],
) -> "tuple[str, str, int]":
    """执行 shell 命令,可选监听 cancel_event。

    Returns: (stdout, stderr, returncode)
    Returns signal-handled error string appended to stderr if not found.
    """
    if cancel_event is None:
        # 简单路径:测试 / CLI / direct 调用都走这里,保持旧 API
        result = subprocess.run(
            cmd,
            shell=True,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return result.stdout or "", result.stderr or "", result.returncode

    # D15-b: Popen + cancel watcher 路径
    process = subprocess.Popen(
        cmd,
        shell=True,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    def _watcher():
        while process.poll() is None:
            if cancel_event.is_set():
                sandbox_logger.warning(
                    "⚙️ [bash_subprocess_cancelled] cmd=%s — terminate SIGTERM",
                    cmd[:80],
                )
                process.terminate()
                try:
                    process.wait(timeout=0.5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    try:
                        process.wait(timeout=1.0)
                    except Exception:
                        pass
                break
            time.sleep(0.1)

    watcher_thread = threading.Thread(
        target=_watcher, name="bash-cancel-watcher", daemon=True
    )
    watcher_thread.start()
    try:
        stdout, stderr = process.communicate(timeout=timeout)
        return stdout or "", stderr or "", process.returncode
    finally:
        watcher_thread.join(timeout=1.0)


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

    # R1 (review): ContextVar 不跨 thread boundary,所以 worker thread
    # (Tools.execute 用 ThreadPoolExecutor 调 handler)看不到父 thread 设
    # 的 cancel_event。Tools.execute 现在把 cancel_event 注入 _cancel_event kwarg
    # (闭包捕获走,跨 thread 安全),优先用;ContextVar 留作 legacy fallback。
    cancel_event = kwargs.get("_cancel_event") or _current_cancel_event.get(None)

    sandbox_logger.info(
        "⚙️ [bash_handler_entry] command=%s timeout=%s working_dir=%s "
        "dangerously_disable=%s",
        command[:200], timeout, working_dir, dangerously_disable,
    )

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
            _wrap_t0 = time.time()
            wrapped = sandbox_manager.wrap_with_sandbox(
                command, working_dir=working_dir or ".",
            )
            sandbox_logger.info(
                "⚙️ [bash_sandbox_wrapped] duration_ms=%.1f wrapped=%s",
                (time.time() - _wrap_t0) * 1000, wrapped != command,
            )
            if wrapped != command:
                effective_command = wrapped
    except Exception as e:
        # sandbox 判断失败 → 不 wrap,直接执行(graceful degradation)
        sandbox_logger.warning("⚙️ [bash_sandbox_decide_failed] err=%s — passthrough", e)
        effective_command = command

    # 执行
    _sub_t0 = time.time()
    sandbox_logger.debug(
        "⚙️ [bash_subprocess_start] cmd_preview=%s timeout=%s",
        effective_command[:120], timeout,
    )
    # ── 执行:经 _run_subprocess_with_cancel helper────────────
    # cancel_event 已在函数顶部优先从 _cancel_event kwarg 取(review R1 修复:
    # ContextVar 跨 thread 不传播)

    try:
        stdout, stderr, return_code = _run_subprocess_with_cancel(
            effective_command,
            cwd=working_dir,
            timeout=timeout,
            cancel_event=cancel_event,
        )
    except subprocess.TimeoutExpired:
        sandbox_logger.warning(
            "⚙️ [bash_subprocess_timeout] cmd=%s timeout=%s duration_ms=%.1f",
            command[:80], timeout, (time.time() - _sub_t0) * 1000,
        )
        return f"Bash command timed out after {timeout}s"
    except FileNotFoundError as e:
        sandbox_logger.warning(
            "⚙️ [bash_subprocess_not_found] cmd=%s err=%s",
            command[:80], e,
        )
        # 沙箱二进制缺失:不同 backend 依赖不同(sandbox-exec / bwrap / srt)
        if "sandbox-exec" in effective_command or "bwrap" in effective_command or "srt" in effective_command:
            return (
                "Sandbox binary not found. 请确认对应 backend 的依赖可用:"
                "NativeBackend 需 sandbox-exec(macOS 内建)/ bwrap(Linux `apt install bubblewrap`);"
                "SrtBackend 需 srt 二进制(`npm i -g @anthropic-ai/sandbox-runtime`)。"
                "或在 settings.json 设 sandbox.enabled=false 禁用沙箱。"
            )
        return f"Bash command not found: {e}"

    # ── 中断检测:cancel 触发 → 返 marker 给 LLM ─────────────────
    # cancel 触发后 Popen 已把 stdout 写入,我们把 marker 拼到 stdout 头部
    # (这样 LLM 收到的 tool_result 知道是被中断的)
    if cancel_event is not None and cancel_event.is_set():
        sandbox_logger.info(
            "⚙️ [bash_cancelled] cmd=%s — append cancel marker",
            command[:80],
        )
        cancelled_text = (
            "[Bash command cancelled by user interrupt]\n"
            f"(partial stdout: {(stdout or '')[:200]})"
        )
        stdout = cancelled_text

    # 合并 stdout + stderr(类 subprocess.CompletedProcess 行为)
    output = ""
    if stdout:
        output += stdout
    if stderr:
        if output:
            output += "\n"
        output += stderr

    # 截断(对齐 CC 5000 字符)
    if len(output) > _BASH_OUTPUT_MAX_CHARS:
        output = output[:_BASH_OUTPUT_MAX_CHARS] + f"\n... (truncated, {len(output)} chars total)"

    sandbox_logger.info(
        "⚙️ [bash_subprocess_done] exit_code=%s duration_ms=%.1f "
        "stdout_len=%d stderr_len=%d",
        return_code, (time.time() - _sub_t0) * 1000,
        len(stdout or ""), len(stderr or ""),
    )

    # P1 修复(设计 §10):每次 bash 执行后接 cleanup_after_command。
    # 防 CC #29316 bare-git scrub + sandbox_tmp_dir mtime 过期。
    # 三条 return 路径(空输出 / 正常 / cancel 已 return)都过此 cleanup。
    try:
        from .sandbox_manager import sandbox_manager
        sandbox_manager.cleanup_after_command()
    except Exception as cleanup_err:
        sandbox_logger.warning("⚙️ [sandbox_cleanup_failed] err=%s", cleanup_err)

    if not output:
        # 空输出(命令成功但无 stdout)→ 返回 exit code
        return f"(command succeeded, exit code {return_code})"

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


# ── 文件读取（skill 系统依赖，FR-011）──────────────────────────────────────

READ_MAX_BYTES = 256_000  # 与 SKILL.md 单文件上限一致（types.DEFAULT_MAX_SKILL_FILE_BYTES）


def read_file_handler(**kwargs) -> str:
    """Read 工具处理函数：按绝对路径读文件内容。

    用途：让 LLM 在匹配 skill 后按 `<location>` 加载 SKILL.md（research Decision 1）。
    填补 permission/safety 子系统早已硬编码 "Read" 名字但未注册的缺口。

    错误处理：所有失败返回错误字符串（不抛），与 calc/bash 一致。
    安全：拒绝 symlink；大小上限 READ_MAX_BYTES；路径遍历由 permission_engine
    在 PermissionCheckHandler 层把关（本 handler 不重复实现）。
    """
    from pathlib import Path

    path = kwargs.get("path", "")
    if not path:
        return "错误：缺少 path 参数"
    try:
        p = Path(path).expanduser()
        if not p.is_file():
            return f"错误：文件不存在或不是普通文件: {path}"
        if p.is_symlink():
            return f"错误：拒绝读取 symlink: {path}"
        size = p.stat().st_size
        if size > READ_MAX_BYTES:
            return f"错误：文件过大（{size} 字节，上限 {READ_MAX_BYTES}）"
        return p.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        return f"读取失败: {e}"


READ_TOOL = ToolDef(
    name="Read",
    description=(
        "读取本地文件内容（按绝对路径）。当任务匹配某个 skill 时，用此工具按 "
        "<available_skills> 中宣告的 <location> 加载该 skill 的 SKILL.md。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "要读取的文件绝对路径（来自 <available_skills> 的 location）",
            },
        },
        "required": ["path"],
    },
    handler=read_file_handler,
    category="read",
)


# ── 注册入口 ──────────────────────────────────────────────────────────────

def register_builtin_tools(registry: ToolRegistry):
    """将内置工具注册到指定注册表"""
    registry.register(CALC_TOOL)
    registry.register(SEARCH_TOOL)
    registry.register(BASH_TOOL)
    registry.register(READ_TOOL)
