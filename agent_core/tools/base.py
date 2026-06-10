"""
工具系统基础类（纯 dict 定义，无 Pydantic 依赖）
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Optional


@dataclass
class ToolDef:
    """
    工具定义。
    用纯 dict 定义参数（JSON Schema），handler 接收 **kwargs。
    """
    name: str
    description: str
    parameters: dict           # JSON Schema dict，如 {"type": "object", "properties": {...}}
    handler: Callable          # 签名：(**kwargs) -> str


class ToolRegistry:
    """
    工具注册表。
    不用 LangChain，自己管理工具定义和调用。
    """

    def __init__(self):
        self._tools: dict[str, ToolDef] = {}

    # ── 注册 / 获取 ─────────────────────────────────────────────────────

    def register(self, tool: ToolDef):
        """注册一个工具"""
        self._tools[tool.name] = tool

    def get(self, name: str) -> Optional[ToolDef]:
        """按名称获取工具定义"""
        return self._tools.get(name)

    def list_names(self) -> list[str]:
        """返回所有已注册工具的名称列表"""
        return list(self._tools.keys())

    # ── 生成 LLM 需要的 tool schema ────────────────────────────────────

    def list_schemas(self, provider: str = "anthropic") -> list[dict]:
        """
        返回 LLM 需要的 tool schema 列表。
        provider: "anthropic" | "openai"
        """
        if provider == "openai":
            return [
                {
                    "type": "function",
                    "function": {
                        "name": t.name,
                        "description": t.description,
                        "parameters": t.parameters,
                    },
                }
                for t in self._tools.values()
            ]
        else:  # anthropic
            return [
                {
                    "name": t.name,
                    "description": t.description,
                    "input_schema": t.parameters,
                }
                for t in self._tools.values()
            ]

    # ── 执行工具 ────────────────────────────────────────────────────────

    def execute(self, tool_name: str, tool_input: dict, max_retries: int = 3, timeout: float = 10.0) -> dict:
        """
        执行工具，带超时控制、重试和详细错误处理。
        
        返回标准格式的 tool_result dict：
            {"status": "success", "output": ...}
            {"status": "error", "error": ...}
        
        错误类型：
            - ValueError: 参数错误（不重试，立即返回）
            - TimeoutError: 超时（不重试，立即返回，防止阻塞 Agent）
            - ConnectionError: 网络错误（重试，指数退避）
            - Exception: 其他错误（重试）
        
        Args:
            tool_name: 工具名称
            tool_input: 工具参数 dict
            max_retries: 最大重试次数（仅对网络/其他错误生效，参数错误和超时不重试）
            timeout: 单次执行超时（秒），超时后立即返回错误，不阻塞 Agent
        """
        import time
        import concurrent.futures
        
        tool_def = self._tools.get(tool_name)
        if not tool_def:
            return {"status": "error", "error": f"未找到工具: {tool_name}"}

        def _run_handler():
            """封装 handler 执行，用于超时控制"""
            return tool_def.handler(**tool_input)

        last_error = None
        for attempt in range(1, max_retries + 1):
            try:
                # 使用 ThreadPoolExecutor + future 实现超时控制
                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                    future = executor.submit(_run_handler)
                    try:
                        result = future.result(timeout=timeout)
                        return {"status": "success", "output": str(result)}
                    except concurrent.futures.TimeoutError:
                        # 超时，不重试，立即返回（防止阻塞 Agent）
                        return {"status": "error", "error": f"执行超时（{timeout}s），工具 `{tool_name}` 未响应，请检查工具实现或增加 timeout"}
            
            except ValueError as e:
                # 参数错误，不重试，立即返回
                return {"status": "error", "error": f"参数错误: {e}"}
            
            except (TimeoutError, ConnectionError) as e:
                # 网络错误，重试
                last_error = e
                if attempt < max_retries:
                    time.sleep(2 ** (attempt - 1))  # 指数退避：1s, 2s, 4s
                    continue
                return {"status": "error", "error": f"网络错误（已重试 {max_retries} 次）: {e}"}
            
            except Exception as e:
                last_error = e
                if attempt < max_retries:
                    time.sleep(1)  # 简单重试延迟
                    continue
                return {"status": "error", "error": f"工具执行失败（已重试 {max_retries} 次）: {type(e).__name__}: {e}"}
        
        # 理论上不会到这里
        return {"status": "error", "error": f"未知错误: {last_error}"}
