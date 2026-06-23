"""M10 C6.4: 运行时切换 config 不重建 agent — MemoryConfig.set_runtime 的最小契约。

3 个 case:
1. 嵌套字段(cost.daily_budget_usd)in-place 改
2. 顶层字段(enabled)in-place 改
3. 未知路径抛 KeyError(明确信号,不要吞成 AttributeError)
"""
from __future__ import annotations

import pytest

from agent_core.memory.config import MemoryConfig


def test_set_runtime_updates_nested_field():
    """cost.daily_budget_usd 路径 in-place 修改(M10 C6.4 Step A)。"""
    config = MemoryConfig()
    original = config.cost.daily_budget_usd
    # 默认值不应等于 0.5,否则测试无意义
    assert original != 0.5
    config.set_runtime("cost.daily_budget_usd", 0.5)
    assert config.cost.daily_budget_usd == 0.5


def test_set_runtime_updates_top_level_field():
    """顶层字段 enabled 直接改(M10 C6.4 Step A)。"""
    config = MemoryConfig()
    assert config.enabled is True
    config.set_runtime("enabled", False)
    assert config.enabled is False


def test_set_runtime_raises_keyerror_on_unknown_path():
    """unknown path 抛 KeyError — 不要吞成 AttributeError。"""
    config = MemoryConfig()
    with pytest.raises(KeyError) as excinfo:
        config.set_runtime("nonexistent.field", 5.0)
    # 错误信息里应该提到未知字段名,便于排查
    assert "nonexistent" in str(excinfo.value)


def test_set_runtime_raises_validation_error_on_type_mismatch():
    """type mismatch → ValidationError (MemoryConfig 启用了 validate_assignment=True)"""
    from pydantic import ValidationError
    from agent_core.memory.config import MemoryConfig
    config = MemoryConfig()
    # enabled 是 bool — 传 object() 应被 Pydantic 拦下
    with pytest.raises(ValidationError):
        config.set_runtime("enabled", object())


def test_react_agent_accepts_memory_config_param():
    """ReactAgent.__init__ 接受 memory_config 参数并存为 self.memory_config"""
    from agent_core.agent_core import ReactAgent
    from agent_core.memory.config import MemoryConfig
    from unittest.mock import MagicMock
    config = MemoryConfig()
    agent = ReactAgent(
        llm_router=MagicMock(),
        tool_registry=MagicMock(),
        memory_config=config,
    )
    assert agent.memory_config is config


def test_get_agent_auto_constructs_cost_tracker():
    """M10 C7.1 final review fix: get_agent() 默认构造 CostTracker 并传给 ExtractionGate

    source-level check(streamlit 不在 .venv,跑不了真 get_agent)
    改用 AST 解析 web/app.py 源码,避免 import streamlit
    """
    import ast
    from pathlib import Path

    src_path = Path("web/app.py")
    tree = ast.parse(src_path.read_text(encoding="utf-8"))

    # 找到 get_agent 函数
    get_agent_func = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "get_agent":
            get_agent_func = node
            break
    assert get_agent_func is not None, "get_agent 函数未在 web/app.py 中找到"

    func_src = ast.unparse(get_agent_func)
    # CostTracker 类被实例化
    assert "CostTracker" in func_src, "CostTracker 未在 get_agent 中实例化"
    # cost_tracker= kwarg 传给 gate / ExtractionGate
    assert "cost_tracker=" in func_src, "cost_tracker 未作为 kwarg 传给 gate"
    # memory_config 被 hoist 到 get_agent 顶部作为单一共享实例
    assert "memory_config = MemoryConfig()" in func_src, (
        "memory_config 未在 get_agent 顶部 hoist 为单一实例"
    )


def test_get_agent_uses_shared_memory_config_instance():
    """M10 C7.1: get_agent() 内 ReactAgent 收到的是 gate 用的同一 memory_config

    AST-level:ReactAgent 调用使用变量 'memory_config'(共享)而不是 'MemoryConfig()'(新实例)
    """
    import ast
    from pathlib import Path

    tree = ast.parse(Path("web/app.py").read_text(encoding="utf-8"))
    get_agent_func = next(
        (n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "get_agent"),
        None,
    )
    assert get_agent_func is not None, "get_agent 函数未找到"

    # 找 ReactAgent(...) 调用,验它的 memory_config kwarg 是 Name(id='memory_config')
    func_src = ast.unparse(get_agent_func)
    react_idx = func_src.find("ReactAgent(")
    assert react_idx != -1, "ReactAgent 未在 get_agent 中调用"

    # AST 检查 keyword args
    call_node = None
    for node in ast.walk(get_agent_func):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "ReactAgent":
            call_node = node
            break
    assert call_node is not None, "ReactAgent Call 节点未找到"

    memory_config_kwarg = next(
        (kw for kw in call_node.keywords if kw.arg == "memory_config"),
        None,
    )
    assert memory_config_kwarg is not None, "ReactAgent 没收到 memory_config kwarg"
    assert isinstance(memory_config_kwarg.value, ast.Name), (
        f"ReactAgent.memory_config 应是变量引用 (Name), 实际 {type(memory_config_kwarg.value).__name__} — "
        "可能存在两实例 bug(gate 用一个,ReactAgent 又 new 一个)"
    )
    assert memory_config_kwarg.value.id == "memory_config", (
        f"ReactAgent.memory_config 变量名应为 'memory_config',实际 '{memory_config_kwarg.value.id}'"
    )


def test_memory_config_hoisted_outside_memory_enabled_block():
    """M10 C7.1 fix: memory_config = MemoryConfig() 必须在 `if memory_enabled` 块外

    否则 memory_enabled=False 时 ReactAgent 收到 UnboundLocalError。
    这个测试是真实运行报错后的回归守卫。
    """
    import ast
    from pathlib import Path

    tree = ast.parse(Path("web/app.py").read_text(encoding="utf-8"))
    get_agent_func = next(
        (n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "get_agent"),
        None,
    )
    assert get_agent_func is not None

    # 找 memory_config = MemoryConfig() 这个赋值节点,验它不在 if 块内
    found_assignment = None
    for node in ast.walk(get_agent_func):
        # Skip nested function definitions
        if isinstance(node, ast.FunctionDef) and node is not get_agent_func:
            continue
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            if isinstance(target, ast.Name) and target.id == "memory_config":
                if isinstance(node.value, ast.Call) and isinstance(node.value.func, ast.Name) \
                        and node.value.func.id == "MemoryConfig":
                    found_assignment = node
                    break
    assert found_assignment is not None, "memory_config = MemoryConfig() 未在 get_agent 中找到"

    # 关键检查:这个赋值节点的祖先链不能有 If(test=<memory_enabled test>)
    # 简化:它的直接父节点不能是 If(用 ast.NodeVisitor 遍历父链)
    # 我们手动递归检查祖先链
    def has_if_ancestor(node, target_node, root):
        """BFS 找 target_node 的祖先链,看是否有 If"""
        for child in ast.iter_child_nodes(root):
            if child is target_node:
                return False  # 找到了 target,但 parent 不是 If
            if isinstance(child, ast.If):
                # 检查 target 是否在 child 的 body/orelse 中
                for sub in ast.walk(child):
                    if sub is target_node:
                        return True
                # 检查 child 子树外
                if has_if_ancestor(node, target_node, child):
                    return True
            elif has_if_ancestor(node, target_node, child):
                return True
        return False

    assert not has_if_ancestor(None, found_assignment, get_agent_func), (
        "memory_config = MemoryConfig() 在 `if memory_enabled` 块内 — "
        "memory_enabled=False 时会触发 UnboundLocalError"
    )
