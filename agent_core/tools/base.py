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
from typing import Any, Callable, Iterable, Optional


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
        # 🆕 T-C2:on_tool_call 回调(让 UI / audit / 监控订阅工具执行事件)。
        # 默认 None,无侵入;订阅方 set_on_tool_call(cb) 后,execute 各状态点
        # 会 fire 一次。callback 异常被吞掉,不阻断主流程。
        self._on_tool_call: Optional[Callable] = None

    def set_on_tool_call(self, callback: Optional[Callable]) -> None:
        """
        注册工具执行回调(订阅模式)。

        callback 签名:
            def cb(category: str, tool_name: str, status: str,
                   duration_ms: float, error: Optional[str] = None) -> None

        status 取值:
            - "running"      进入 execute,schema 校验通过(尚未执行)
            - "success"      handler 正常返回
            - "timeout"      handler 超时
            - "param_error"  jsonschema 校验失败 / ValueError
            - "network_error" ConnectionError 等网络异常(已重试 max_retries 次)
            - "error"        其他未捕获异常(已重试 max_retries 次)
            - "not_found"    工具名不在 registry

        category: ToolDef.category(builtin / shell / read / mcp / general …)

        异常处理:cb 内部异常被 _fire_on_tool_call 吞掉,不阻断 execute 主流程。
        注销回调:set_on_tool_call(None)
        """
        self._on_tool_call = callback

    def _fire_on_tool_call(
        self,
        category: str,
        tool_name: str,
        status: str,
        duration_ms: float,
        error: Optional[str] = None,
    ) -> None:
        """触发 on_tool_call 回调(异常被吞掉,不阻断 execute)"""
        if self._on_tool_call is None:
            return
        try:
            self._on_tool_call(
                category=category,
                tool_name=tool_name,
                status=status,
                duration_ms=duration_ms,
                error=error,
            )
        except Exception as e:
            logger.warning(
                "🧩 on_tool_call callback 异常(吞掉,不阻断 execute): tool=%s status=%s err=%s",
                tool_name, status, e,
            )

    # ── 注册 / 获取 ─────────────────────────────────────────────────────

    def register(self, tool: ToolDef):
        """注册一个工具"""
        self._tools[tool.name] = tool
        # 🧩 debug:log 新注册 + 当前已注册 tool 名单(便于测试追溯与 bug 定位;
        # Core path MUST 打 debug 日志,复用 sub-logger 机制)
        logger.debug(
            "🧩 tool registered: name=%s category=%s version=%s total=%d all=%s",
            tool.name,
            getattr(tool, "category", "?"),
            getattr(tool, "version", "?"),
            len(self._tools),
            sorted(self._tools.keys()),
        )
        # 弃用警告
        if tool.deprecated_since is not None:
            logger.warning(
                "tool %s 自版本 %s 起被弃用(deprecated_since=%s)",
                tool.name, tool.deprecated_since, tool.deprecated_since,
            )

    def register_many(self, tools: "Iterable[ToolDef]") -> int:
        """
        批量注册工具(对称于 `unregister_by_prefix`,MCP server 工具集刷新用)。

        Args:
            tools: 可迭代的 ToolDef 序列

        Returns:
            新注册数量(同名覆盖不计)。
            例:tools=[A, B, A] 且 A 已存在 → 返回 1(只新增 B)

        实现:逐个调 `register`(统一日志 + deprecation warning 行为)。
        非 list 类型(如 generator)也接受 — 内部 `for tool in tools:` 自然支持。
        """
        count = 0
        for tool in tools:
            existed = tool.name in self._tools
            self.register(tool)
            if not existed:
                count += 1
        logger.debug(
            "🧩 register_many 完成: 新注册 %d 个, 当前总数 %d",
            count, len(self._tools),
        )
        return count

    def unregister(self, name: str) -> bool:
        """注销单个工具。返回是否曾存在。

        Phase 2 Step 4：list_changed 动态刷新工具集用。
        """
        existed = self._tools.pop(name, None) is not None
        if existed:
            logger.debug("🧩 tool unregistered: name=%s remaining=%d", name, len(self._tools))
        return existed

    def unregister_by_prefix(self, prefix: str) -> int:
        """按前缀批量注销（给 mcp__<server>__ 用），返回注销数量。

        Phase 2 Step 4/5：list_changed 时整 server 工具集刷新用。
        注意：调用方须同步失效 run_state.tool_schemas（turn_chain.ToolsSchemaPrepareHandler
        缓存），否则当前 run 的后续 turn 还用老 schema。
        """
        names_to_remove = [n for n in self._tools if n.startswith(prefix)]
        for n in names_to_remove:
            del self._tools[n]
        if names_to_remove:
            logger.debug(
                "🧩 tools unregistered by prefix=%s count=%d remaining=%d",
                prefix, len(names_to_remove), len(self._tools),
            )
        return len(names_to_remove)

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

    def execute(self, tool_name: str, tool_input: dict, max_retries: int = 3, timeout: float = 10.0, *, cancel_event: Any = None) -> dict:
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
        # 🆕 T-C2:track duration + category for on_tool_call hook
        _t0 = time.monotonic()
        _category = getattr(tool_def, "category", "general") if tool_def else "unknown"

        if not tool_def:
            # 🆕 T-C2:not_found 状态 hook(让 UI 能区分"未注册" vs "执行失败")
            self._fire_on_tool_call(_category, tool_name, "not_found", 0.0,
                                    error=f"未找到工具: {tool_name}")
            return {"status": "error", "error": f"未找到工具: {tool_name}"}

        # ── Phase 1: jsonschema 校验(对齐 doc §7.2)────────
        if self._enable_jsonschema_validation:
            validation_error = self._validate_tool_input(tool_def, tool_input)
            if validation_error is not None:
                # 校验失败 → 立即 error,不重试
                # 🆕 T-C2:param_error hook
                self._fire_on_tool_call(
                    _category, tool_name, "param_error",
                    (time.monotonic() - _t0) * 1000,
                    error=f"参数校验失败: {validation_error}",
                )
                return {"status": "error", "error": f"参数校验失败: {validation_error}"}

        # 把 cancel_event 注入 kwargs(handler 通过 _current_cancel_event
        # ContextVar 读;review R1:ContextVar 在 worker thread 中不继承,
        # 所以通过 kwarg 显式传递,handler 直接读)。
        tool_input_with_cancel = dict(tool_input)
        if cancel_event is not None:
            tool_input_with_cancel["_cancel_event"] = cancel_event

        def _run_handler():
            """封装 handler 执行，用于超时控制"""
            return tool_def.handler(**tool_input_with_cancel)

        last_error = None
        for attempt in range(1, max_retries + 1):
            try:
                # 使用 ThreadPoolExecutor + future 实现超时控制
                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                    future = executor.submit(_run_handler)
                    try:
                        result = future.result(timeout=timeout)
                        # 🆕 T-C2:success hook
                        self._fire_on_tool_call(
                            _category, tool_name, "success",
                            (time.monotonic() - _t0) * 1000,
                        )
                        return {"status": "success", "output": str(result)}
                    except concurrent.futures.TimeoutError:
                        # 超时，不重试，立即返回（防止阻塞 Agent）
                        # 🆕 T-C2:timeout hook
                        self._fire_on_tool_call(
                            _category, tool_name, "timeout",
                            (time.monotonic() - _t0) * 1000,
                            error=f"执行超时({timeout}s)",
                        )
                        return {"status": "error", "error": f"执行超时（{timeout}s），工具 `{tool_name}` 未响应，请检查工具实现或增加 timeout"}

            except ValueError as e:
                # 参数错误，不重试，立即返回
                # 🆕 T-C2:param_error hook(ValueError 也归类为参数错误)
                self._fire_on_tool_call(
                    _category, tool_name, "param_error",
                    (time.monotonic() - _t0) * 1000,
                    error=str(e),
                )
                return {"status": "error", "error": f"参数错误: {e}"}

            except (TimeoutError, ConnectionError) as e:
                # 网络错误，重试
                last_error = e
                if attempt < max_retries:
                    time.sleep(2 ** (attempt - 1))  # 指数退避：1s, 2s, 4s
                    continue
                # 🆕 T-C2:network_error hook(重试耗尽)
                self._fire_on_tool_call(
                    _category, tool_name, "network_error",
                    (time.monotonic() - _t0) * 1000,
                    error=f"网络错误(已重试 {max_retries} 次): {e}",
                )
                return {"status": "error", "error": f"网络错误（已重试 {max_retries} 次）: {e}"}

            except Exception as e:
                last_error = e
                if attempt < max_retries:
                    time.sleep(1)  # 简单重试延迟
                    continue
                # 🆕 T-C2:error hook(重试耗尽)
                self._fire_on_tool_call(
                    _category, tool_name, "error",
                    (time.monotonic() - _t0) * 1000,
                    error=f"工具执行失败(已重试 {max_retries} 次): {type(e).__name__}: {e}",
                )
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
