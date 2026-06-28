"""
工具系统基础类（纯 dict 定义，无 Pydantic 依赖）

Phase 1 (M1) 增量:
  - ToolDef 加 category / version / deprecated_since / check_permissions / requires_user_interaction 字段
  - ToolRegistry.execute 加 jsonschema 校验
  - deprecation warning 日志

对齐 docs/tool/tool-security-architecture.md §7.2(jsonschema 校验) + §4.2(check_permissions / requires_user_interaction)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Optional


logger = logging.getLogger(__name__)


@dataclass
class ToolDef:
    """
    工具定义。
    用纯 dict 定义参数（JSON Schema），handler 接收 **kwargs。

    Phase 1 新增字段(对齐 doc §4.2 + §7.2):
      - category: 工具分类(read / write / shell / agent / general)用于 UI 展示 + rule 过滤
      - version: 工具版本号(用于 deprecation tracking)
      - deprecated_since: 弃用起始版本(非 None 时打印 warning log)
      - check_permissions: 可选 tool-level 权限预检
        签名 (input_dict, context) -> PermissionDecision
        返 DENY 直接终止 pipeline;ALLOW/PASSTHROUGH 继续
      - requires_user_interaction: True 时强制 ASK(对齐 CC agent tool 行为)
    """
    name: str
    description: str
    parameters: dict           # JSON Schema dict，如 {"type": "object", "properties": {...}}
    handler: Callable          # 签名：(**kwargs) -> str

    # ── Phase 1 增量字段(都有 default,保持向后兼容)───────
    category: str = "general"
    version: str = "1.0"
    deprecated_since: Optional[str] = None
    check_permissions: Optional[Callable] = None
    requires_user_interaction: bool = False


class ToolRegistry:
    """
    工具注册表。
    不用 LangChain，自己管理工具定义和调用。
    """

    def __init__(self, enable_jsonschema_validation: bool = True):
        """
        Args:
            enable_jsonschema_validation: 是否对 execute() 输入做 JSON Schema 校验
                                          (Phase 1 引入,M1 默认开启;可关闭以兼容老 behavior)
        """
        self._tools: dict[str, ToolDef] = {}
        self._enable_jsonschema_validation = enable_jsonschema_validation

    # ── 注册 / 获取 ─────────────────────────────────────────────────────

    def register(self, tool: ToolDef):
        """注册一个工具"""
        self._tools[tool.name] = tool
        # 弃用警告
        if tool.deprecated_since is not None:
            logger.warning(
                "tool %s 自版本 %s 起被弃用(deprecated_since=%s)",
                tool.name, tool.deprecated_since, tool.deprecated_since,
            )

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

        Phase 1 增量:
          - 在 _run_handler 之前对 tool_input 做 jsonschema 校验(对齐 doc §7.2)
          - 校验失败立即返回 error,不重试

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

        # ── Phase 1: jsonschema 校验(对齐 doc §7.2)────────
        if self._enable_jsonschema_validation:
            validation_error = self._validate_tool_input(tool_def, tool_input)
            if validation_error is not None:
                # 校验失败 → 立即 error,不重试
                return {"status": "error", "error": f"参数校验失败: {validation_error}"}

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

    # ── jsonschema 校验 helper(Phase 1 增量)───────────────────────

    def _validate_tool_input(self, tool_def: ToolDef, tool_input: Any) -> Optional[str]:
        """
        对 tool_input 做 JSON Schema 校验(对齐 doc §7.2)

        Returns:
            错误信息字符串,None 表示通过
        """
        import jsonschema

        schema = tool_def.parameters
        if not schema:
            # 无 schema 不校验
            return None

        try:
            jsonschema.validate(instance=tool_input, schema=schema)
            return None
        except jsonschema.ValidationError as e:
            # 简化错误信息
            path = ".".join(str(p) for p in e.absolute_path) if e.absolute_path else "<root>"
            return f"{path}: {e.message}"
        except jsonschema.SchemaError as e:
            # schema 本身有问题
            logger.error("tool %s schema invalid: %s", tool_def.name, e.message)
            return f"tool schema invalid: {e.message}"
