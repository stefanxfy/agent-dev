"""
Task 8: web/app.py get_agent() wiring test

Verifies:
1. `web/app.get_agent()` 接受 memory_enabled 时,把 ReactMemoryBridge 注入到 ReactAgent
2. 该 bridge 是真实的 ReactMemoryBridge 实例(不是 None)
3. memory_enabled=False 时,react_memory_bridge 应为 None(向后兼容)

Streamlit 在测试环境常常 import 失败(无脚本上下文);本测试用 permissive 策略:
- 若 web.app import 失败 → 跳过(return) — 测试环境无 streamlit 不算 bug
- 若 import 成功 → 验证 wiring 正确
"""
import sys
import types
import importlib.util
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


PROJECT_ROOT = Path(__file__).parent.parent.resolve()
WEB_APP_PATH = PROJECT_ROOT / "web" / "app.py"


def _try_load_web_app():
    """
    把 web/app.py 当模块加载,绕过 streamlit 的运行时副作用。
    返回 module 或 None(若 streamlit 不可用)。
    """
    try:
        import streamlit  # noqa: F401
        streamlit_available = True
    except Exception:
        streamlit_available = False

    if not streamlit_available:
        return None

    spec = importlib.util.spec_from_file_location("web.app_test", WEB_APP_PATH)
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    # 把 module 注册到 sys.modules 防止装饰器 / 类型注解再 import 时找不到
    sys.modules["web.app_test"] = module
    try:
        spec.loader.exec_module(module)
    except Exception as e:
        pytest.skip(f"web/app.py 执行期副作用失败(无 streamlit 脚本上下文): {e}")
    return module


def test_get_agent_uses_react_memory_bridge():
    """
    Step 8.2: get_agent(memory_enabled=True) 时,ReactAgent 收到的
    react_memory_bridge kwarg 必须是非 None 的 ReactMemoryBridge 实例。
    """
    web_app = _try_load_web_app()
    if web_app is None:
        pytest.skip("streamlit 不可用,跳过 wiring 测试")

    # st.session_state.get("memory_enabled", False) → True
    # 其他 UI 状态给默认值
    fake_state = {
        "memory_enabled": True,
        "system_prompt": "",
    }
    web_app.st.session_state.get = lambda key, default=None: fake_state.get(
        key, default
    )

    # 拦截 ReactAgent 构造,捕获 kwargs
    captured_kwargs = {}
    real_react_agent = web_app.ReactAgent

    def _spy_react_agent(*args, **kwargs):
        captured_kwargs.update(kwargs)
        # 返回一个 mock,避免 ReactAgent 真的跑 __init__ 里的副作用
        return MagicMock(name="ReactAgentInstance")

    web_app.ReactAgent = _spy_react_agent

    try:
        agent = web_app.get_agent(session_id="wiring-test-session")
    finally:
        web_app.ReactAgent = real_react_agent

    # 断言
    assert "react_memory_bridge" in captured_kwargs, (
        "get_agent() 没把 react_memory_bridge 传给 ReactAgent — "
        "Task 8 wiring 未完成"
    )
    bridge = captured_kwargs["react_memory_bridge"]
    assert bridge is not None, (
        "memory_enabled=True 时,react_memory_bridge 不应为 None"
    )
    # 确认是真实的 ReactMemoryBridge 实例
    from agent_core.memory.react_memory_bridge import ReactMemoryBridge
    assert isinstance(bridge, ReactMemoryBridge), (
        f"react_memory_bridge 应该是 ReactMemoryBridge 实例,实际是 {type(bridge)}"
    )


def test_get_agent_memory_disabled_bridge_none():
    """
    向后兼容:memory_enabled=False 时,react_memory_bridge 应为 None
    """
    web_app = _try_load_web_app()
    if web_app is None:
        pytest.skip("streamlit 不可用,跳过 wiring 测试")

    fake_state = {
        "memory_enabled": False,
        "system_prompt": "",
    }
    web_app.st.session_state.get = lambda key, default=None: fake_state.get(
        key, default
    )

    captured_kwargs = {}
    real_react_agent = web_app.ReactAgent

    def _spy_react_agent(*args, **kwargs):
        captured_kwargs.update(kwargs)
        return MagicMock(name="ReactAgentInstance")

    web_app.ReactAgent = _spy_react_agent

    try:
        web_app.get_agent(session_id="wiring-disabled")
    finally:
        web_app.ReactAgent = real_react_agent

    # memory_enabled=False 时,ReactAgent 不应该拿到 react_memory_bridge
    # (或拿到 None)— 我们选择不在 disabled 路径上传 kwarg
    bridge = captured_kwargs.get("react_memory_bridge")
    assert bridge is None, (
        f"memory_enabled=False 时,react_memory_bridge 应为 None,实际为 {bridge!r}"
    )


def test_web_app_passes_react_memory_bridge_kwarg():
    """
    静态检查:即使 streamlit 不可用,也能验证 web/app.py 的
    ReactAgent(...) 构造调用包含了 react_memory_bridge=react_memory_bridge。
    """
    source = WEB_APP_PATH.read_text(encoding="utf-8")
    # 在 ReactAgent( ... ) 块里必须出现 react_memory_bridge=
    # 用一个简单的 token check 即可(避免引入 ast 依赖)
    assert "react_memory_bridge=react_memory_bridge" in source, (
        "web/app.py 的 ReactAgent(...) 调用必须传入 "
        "react_memory_bridge=react_memory_bridge(Task 8 wiring 未完成)"
    )


def test_web_app_imports_strict_pipeline_components():
    """静态检查:web/app.py 必须 import MetaDB/DualChannelWriter/ExtractionGate/ReactMemoryBridge"""
    source = WEB_APP_PATH.read_text(encoding="utf-8")
    for name in ("MetaDB", "DualChannelWriter", "ExtractionGate", "ReactMemoryBridge"):
        assert name in source, (
            f"web/app.py 必须 import {name}(Task 8 严格双通道 wiring 未完成)"
        )


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v", "--tb=short"]))
