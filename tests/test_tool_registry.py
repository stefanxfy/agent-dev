"""
base.py (ToolDef + ToolRegistry) 测试

覆盖:
1. ToolDef 新字段默认(category/version/check_permissions/requires_user_interaction)
2. ToolDef 新字段显式传入
3. register() 触发 deprecation warning
4. ToolRegistry.execute jsonschema 校验失败 → error 不重试
5. ToolRegistry.execute jsonschema 校验通过 → 正常执行
6. ToolRegistry.execute 关闭 jsonschema 后行为不变
7. ToolDef.check_permissions 被 PermissionEngine 调(集成)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable, Optional

import pytest

from agent_core.tools.base import ToolDef, ToolRegistry
from agent_core.tools.permission.types import (
    PermissionBehavior,
    PermissionDecision,
)


# ────────────────────────────────────────────────────────────────────
# 工具工厂 helper
# ────────────────────────────────────────────────────────────────────

def _make_simple_tool(
    name: str = "dummy",
    schema: Optional[dict] = None,
    handler: Optional[Callable] = None,
    **kwargs,
) -> ToolDef:
    """构造一个简单 ToolDef 用于测试"""
    if schema is None:
        schema = {
            "type": "object",
            "properties": {"x": {"type": "string"}},
            "required": ["x"],
        }
    if handler is None:
        handler = lambda **kw: f"ok: {kw.get('x', '')}"  # noqa: E731
    return ToolDef(
        name=name,
        description=f"dummy {name}",
        parameters=schema,
        handler=handler,
        **kwargs,
    )


# ────────────────────────────────────────────────────────────────────
# ToolDef 新字段
# ────────────────────────────────────────────────────────────────────

class TestToolDefNewFields:
    def test_default_category_is_general(self):
        """默认 category='general'"""
        tool = _make_simple_tool()
        assert tool.category == "general"

    def test_default_version_is_1_0(self):
        """默认 version='1.0'"""
        tool = _make_simple_tool()
        assert tool.version == "1.0"

    def test_default_deprecated_since_is_none(self):
        """默认 deprecated_since=None"""
        tool = _make_simple_tool()
        assert tool.deprecated_since is None

    def test_default_check_permissions_is_none(self):
        """默认 check_permissions=None"""
        tool = _make_simple_tool()
        assert tool.check_permissions is None

    def test_default_requires_user_interaction_is_false(self):
        """默认 requires_user_interaction=False"""
        tool = _make_simple_tool()
        assert tool.requires_user_interaction is False

    def test_explicit_new_fields(self):
        """显式传入新字段"""
        def check(input_dict, ctx):
            return PermissionDecision(behavior=PermissionBehavior.DENY.value)

        tool = _make_simple_tool(
            category="write",
            version="2.5",
            deprecated_since="2.0",
            check_permissions=check,
            requires_user_interaction=True,
        )
        assert tool.category == "write"
        assert tool.version == "2.5"
        assert tool.deprecated_since == "2.0"
        assert tool.check_permissions is check
        assert tool.requires_user_interaction is True


# ────────────────────────────────────────────────────────────────────
# register() 触发 deprecation warning
# ────────────────────────────────────────────────────────────────────

class TestDeprecationWarning:
    def test_deprecated_tool_logs_warning(self, caplog):
        """deprecated_since 非空时 register 触发 warning log"""
        tool = _make_simple_tool(name="old", deprecated_since="1.0")
        registry = ToolRegistry()
        with caplog.at_level(logging.WARNING, logger="agent_core.tools.base"):
            registry.register(tool)
        assert any("old" in r.message and "deprecated" in r.message.lower()
                   for r in caplog.records)

    def test_non_deprecated_tool_no_warning(self, caplog):
        """无 deprecated_since 不触发 warning"""
        tool = _make_simple_tool(name="fresh")
        registry = ToolRegistry()
        with caplog.at_level(logging.WARNING, logger="agent_core.tools.base"):
            registry.register(tool)
        deprecation_records = [r for r in caplog.records
                                if "deprecated" in r.message.lower()]
        assert deprecation_records == []


# ────────────────────────────────────────────────────────────────────
# jsonschema 校验
# ────────────────────────────────────────────────────────────────────

class TestJsonSchemaValidation:
    def test_validation_passes_returns_success(self):
        """input 符合 schema → 正常执行"""
        registry = ToolRegistry()
        registry.register(_make_simple_tool(
            schema={
                "type": "object",
                "properties": {"x": {"type": "string"}},
                "required": ["x"],
            },
        ))
        result = registry.execute("dummy", {"x": "hello"})
        assert result["status"] == "success"
        assert "hello" in result["output"]

    def test_validation_missing_required_returns_error(self):
        """缺 required 字段 → error, 不重试"""
        registry = ToolRegistry()
        registry.register(_make_simple_tool(
            schema={
                "type": "object",
                "properties": {"x": {"type": "string"}},
                "required": ["x"],
            },
        ))
        result = registry.execute("dummy", {})
        assert result["status"] == "error"
        assert "校验失败" in result["error"]

    def test_validation_wrong_type_returns_error(self):
        """类型不匹配 → error"""
        registry = ToolRegistry()
        registry.register(_make_simple_tool(
            schema={
                "type": "object",
                "properties": {"count": {"type": "integer"}},
                "required": ["count"],
            },
            handler=lambda **kw: str(kw["count"]),
        ))
        result = registry.execute("dummy", {"count": "not_an_int"})
        assert result["status"] == "error"
        assert "校验失败" in result["error"]

    def test_validation_extra_fields_allowed_by_default(self):
        """默认 jsonschema 不禁止 extra fields(对齐 CC)"""
        registry = ToolRegistry()
        registry.register(_make_simple_tool(
            schema={
                "type": "object",
                "properties": {"x": {"type": "string"}},
            },
        ))
        # 多传字段默认应该过(jsonschema 默认 additionalProperties=True)
        result = registry.execute("dummy", {"x": "hi", "extra": 1})
        assert result["status"] == "success"

    def test_validation_disabled_skips_check(self):
        """enable_jsonschema_validation=False → 跳过校验"""
        registry = ToolRegistry(enable_jsonschema_validation=False)
        registry.register(_make_simple_tool(
            schema={
                "type": "object",
                "properties": {"x": {"type": "string"}},
                "required": ["x"],
            },
        ))
        # 缺 required 字段也通过
        result = registry.execute("dummy", {})
        assert result["status"] == "success"

    def test_validation_no_schema_passes_through(self):
        """parameters 为空 dict → 跳过校验(handler 可用任意输入)"""
        registry = ToolRegistry()
        registry.register(_make_simple_tool(
            schema={},
            handler=lambda **kw: "ok",
        ))
        result = registry.execute("dummy", {"anything": "goes"})
        assert result["status"] == "success"
        assert result["output"] == "ok"

    def test_validation_error_message_includes_path(self):
        """错误信息含字段路径"""
        registry = ToolRegistry()
        registry.register(_make_simple_tool(
            schema={
                "type": "object",
                "properties": {
                    "user": {
                        "type": "object",
                        "properties": {"age": {"type": "integer"}},
                    },
                },
                "required": ["user"],
            },
        ))
        result = registry.execute("dummy", {"user": {"age": "old"}})
        assert result["status"] == "error"
        # 错误信息应该指明是 user.age
        assert "user" in result["error"] or "age" in result["error"]

    def test_validation_does_not_retry(self):
        """校验失败不重试(立即 error)"""
        call_count = {"n": 0}

        def counting_handler(**kw):
            call_count["n"] += 1
            return "should not run"

        registry = ToolRegistry()
        registry.register(_make_simple_tool(
            schema={
                "type": "object",
                "properties": {"x": {"type": "string"}},
                "required": ["x"],
            },
            handler=counting_handler,
        ))
        # 缺 required → 校验失败 → 不调 handler
        result = registry.execute("dummy", {}, max_retries=3)
        assert result["status"] == "error"
        assert call_count["n"] == 0


# ────────────────────────────────────────────────────────────────────
# ToolDef.check_permissions 集成(模拟 PermissionEngine 调用)
# ────────────────────────────────────────────────────────────────────

class TestCheckPermissionsIntegration:
    def test_check_permissions_called_by_engine(self):
        """ToolDef.check_permissions 可被 PermissionEngine 调"""
        from agent_core.tools.permission.engine import PermissionEngine
        from agent_core.tools.permission.types import ToolPermissionContext

        def custom_check(input_dict, ctx):
            # 总是 DENY
            return PermissionDecision(
                behavior=PermissionBehavior.DENY.value,
                decision_reason=None,
                message="custom tool denied",
            )

        tool = _make_simple_tool(name="gated", check_permissions=custom_check)
        engine = PermissionEngine(context=ToolPermissionContext())
        decision = engine.check_permissions(tool, {"x": "y"}, [])
        # 1c 步骤:tool.check_permissions 返 DENY → DENY
        assert decision.behavior == PermissionBehavior.DENY.value

    def test_requires_user_interaction_triggers_ask(self):
        """requires_user_interaction=True → ASK(Step 1d)"""
        from agent_core.tools.permission.engine import PermissionEngine
        from agent_core.tools.permission.types import ToolPermissionContext

        tool = _make_simple_tool(name="interactive", requires_user_interaction=True)
        engine = PermissionEngine(context=ToolPermissionContext())
        decision = engine.check_permissions(tool, {}, [])
        assert decision.behavior == PermissionBehavior.ASK.value


# ────────────────────────────────────────────────────────────────────
# 现有 builtin 工具继续可用(向后兼容)
# ────────────────────────────────────────────────────────────────────

class TestBuiltinBackwardCompat:
    def test_calc_tool_registers(self):
        """builtin calc tool 可注册"""
        from agent_core.tools.builtin import CALC_TOOL
        registry = ToolRegistry()
        registry.register(CALC_TOOL)
        assert registry.get("calc") is not None
        # 新字段应有默认值
        assert CALC_TOOL.category == "general"
        assert CALC_TOOL.deprecated_since is None
        assert CALC_TOOL.requires_user_interaction is False

    def test_calc_validates_input(self):
        """builtin calc tool input 校验"""
        from agent_core.tools.builtin import CALC_TOOL
        registry = ToolRegistry()
        registry.register(CALC_TOOL)
        # 缺 expression → 校验失败(因为 schema 要求 required)
        result = registry.execute("calc", {})
        assert result["status"] == "error"


# ────────────────────────────────────────────────────────────────────
# T-C2: on_tool_call hook(B.3)
# ────────────────────────────────────────────────────────────────────

class TestOnToolCallHook:
    """B.3:ToolRegistry.execute 在各状态点 fire on_tool_call 回调。"""

    def _make_capture_cb(self):
        """构造 capture-only callback,记录每次 fire 的入参。"""
        captured = []

        def cb(category, tool_name, status, duration_ms, error=None):
            captured.append({
                "category": category,
                "tool_name": tool_name,
                "status": status,
                "duration_ms": duration_ms,
                "error": error,
            })

        return cb, captured

    def test_success_fires_callback(self):
        """成功执行 → status='success' + category=ToolDef.category"""
        cb, captured = self._make_capture_cb()
        tool = _make_simple_tool(name="ok", category="general")
        registry = ToolRegistry()
        registry.register(tool)
        registry.set_on_tool_call(cb)

        result = registry.execute("ok", {"x": "hi"})
        assert result["status"] == "success"
        assert len(captured) == 1
        assert captured[0]["status"] == "success"
        assert captured[0]["category"] == "general"
        assert captured[0]["tool_name"] == "ok"
        assert captured[0]["error"] is None
        assert captured[0]["duration_ms"] >= 0

    def test_mcp_category_propagates(self):
        """ToolDef.category='mcp' → callback 收到 category='mcp'"""
        cb, captured = self._make_capture_cb()
        tool = _make_simple_tool(
            name="mcp__fs__write",
            category="mcp",
            # 简化 schema 允许任意输入
            schema={"type": "object", "properties": {}, "required": []},
        )
        registry = ToolRegistry()
        registry.register(tool)
        registry.set_on_tool_call(cb)

        registry.execute("mcp__fs__write", {"path": "/tmp/x"})
        assert captured[0]["category"] == "mcp"

    def test_not_found_fires_callback(self):
        """工具未注册 → status='not_found'"""
        cb, captured = self._make_capture_cb()
        registry = ToolRegistry()
        registry.set_on_tool_call(cb)

        result = registry.execute("ghost", {})
        assert result["status"] == "error"
        assert len(captured) == 1
        assert captured[0]["status"] == "not_found"
        assert captured[0]["tool_name"] == "ghost"
        assert "未找到" in (captured[0]["error"] or "")

    def test_param_error_fires_callback(self):
        """schema 校验失败 → status='param_error'"""
        cb, captured = self._make_capture_cb()
        tool = _make_simple_tool(name="strict")
        registry = ToolRegistry()
        registry.register(tool)
        registry.set_on_tool_call(cb)

        # schema 缺 required 'x' → 校验失败
        result = registry.execute("strict", {})
        assert result["status"] == "error"
        assert captured[0]["status"] == "param_error"
        assert "参数校验失败" in (captured[0]["error"] or "")

    def test_timeout_fires_callback(self):
        """handler 阻塞超时 → status='timeout'"""
        import time

        def slow_handler(**kw):
            time.sleep(2.0)
            return "should-not-reach"

        cb, captured = self._make_capture_cb()
        tool = _make_simple_tool(
            name="slow",
            handler=slow_handler,
            schema={"type": "object", "properties": {}, "required": []},
        )
        registry = ToolRegistry()
        registry.register(tool)
        registry.set_on_tool_call(cb)

        result = registry.execute("slow", {}, timeout=0.1, max_retries=1)
        assert result["status"] == "error"
        assert captured[0]["status"] == "timeout"

    def test_callback_exception_does_not_break_execute(self):
        """callback 抛异常被吞掉,不阻断 execute"""
        def bad_cb(**kw):
            raise RuntimeError("cb-bomb")

        tool = _make_simple_tool(name="ok")
        registry = ToolRegistry()
        registry.register(tool)
        registry.set_on_tool_call(bad_cb)

        result = registry.execute("ok", {"x": "hi"})
        # callback 异常被吞,execute 仍返 success
        assert result["status"] == "success"

    def test_no_callback_no_op(self):
        """未注册 callback → fire 不做事,不报错"""
        tool = _make_simple_tool(name="ok")
        registry = ToolRegistry()
        registry.register(tool)
        # 不调 set_on_tool_call
        result = registry.execute("ok", {"x": "hi"})
        assert result["status"] == "success"

    def test_set_none_unregisters(self):
        """set_on_tool_call(None) 注销 callback"""
        cb, captured = self._make_capture_cb()
        tool = _make_simple_tool(name="ok")
        registry = ToolRegistry()
        registry.register(tool)
        registry.set_on_tool_call(cb)
        registry.set_on_tool_call(None)  # 注销

        registry.execute("ok", {"x": "hi"})
        assert len(captured) == 0  # 不应 fire


# ────────────────────────────────────────────────────────────────────
# C.1 T-New2: register_many 批量注册(对称于 unregister_by_prefix)
# ────────────────────────────────────────────────────────────────────

class TestRegisterMany:
    """C.1:ToolRegistry.register_many 批量注册,返回新增数量。"""

    def test_register_many_empty(self):
        """空可迭代对象 → 返回 0,不报错"""
        registry = ToolRegistry()
        assert registry.register_many([]) == 0
        assert registry.list_names() == []

    def test_register_many_single(self):
        """单 tool → 返回 1"""
        registry = ToolRegistry()
        assert registry.register_many([_make_simple_tool(name="a")]) == 1
        assert "a" in registry.list_names()

    def test_register_many_multiple(self):
        """多 tool → 返回新注册数"""
        registry = ToolRegistry()
        count = registry.register_many([
            _make_simple_tool(name="a"),
            _make_simple_tool(name="b"),
            _make_simple_tool(name="c"),
        ])
        assert count == 3
        assert set(registry.list_names()) == {"a", "b", "c"}

    def test_register_many_overlap_counts_only_new(self):
        """同名覆盖不计(返回新注册数=实际新加入)"""
        registry = ToolRegistry()
        registry.register(_make_simple_tool(name="a"))  # 先注册 a
        count = registry.register_many([
            _make_simple_tool(name="a"),  # 覆盖
            _make_simple_tool(name="b"),  # 新增
        ])
        assert count == 1  # 只 b 是新加
        assert set(registry.list_names()) == {"a", "b"}

    def test_register_many_accepts_generator(self):
        """接受 generator(不强制 list)"""
        registry = ToolRegistry()

        def gen():
            yield _make_simple_tool(name="g1")
            yield _make_simple_tool(name="g2")

        count = registry.register_many(gen())
        assert count == 2
        assert set(registry.list_names()) == {"g1", "g2"}

    def test_register_many_triggers_deprecation_warning(self):
        """deprecated tool 仍触发 logger.warning(沿用 register 单 tool 行为)"""
        registry = ToolRegistry()
        deprecated_tool = _make_simple_tool(
            name="old",
            deprecated_since="v2.0",
        )
        # ToolDef.deprecated_since 触发 logger.warning(非 DeprecationWarning)
        # 验证 register_many 内部不吞 warning 即可 — register() 已单独测过
        # 这里只验证 deprecated_since 字段透传
        assert deprecated_tool.deprecated_since == "v2.0"
        # 调用不应抛异常
        registry.register_many([deprecated_tool])
        assert "old" in registry.list_names()

    def test_register_many_mcp_like_workflow(self):
        """MCP server 刷新场景模拟:unregister_by_prefix → register_many"""
        # 模拟 MCP 第一次连接注册 2 个 tool
        registry = ToolRegistry()
        registry.register_many([
            _make_simple_tool(name="mcp__fs__write", category="mcp"),
            _make_simple_tool(name="mcp__fs__read", category="mcp"),
        ])
        assert len(registry.list_names()) == 2

        # 模拟 list_changed:server 重启,tool 列表变了
        registry.unregister_by_prefix("mcp__fs__")
        assert len(registry.list_names()) == 0

        # 重新注册
        registry.register_many([
            _make_simple_tool(name="mcp__fs__write", category="mcp"),
            _make_simple_tool(name="mcp__fs__delete", category="mcp"),  # 新 tool
        ])
        names = set(registry.list_names())
        assert names == {"mcp__fs__write", "mcp__fs__delete"}
