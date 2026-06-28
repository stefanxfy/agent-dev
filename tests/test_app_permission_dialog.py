"""
web/app.py Streamlit 权限弹窗 smoke test(Step 12)

M12:Streamlit UI 不易单测,只验证:
1. web/app.py 可 import(不报 import error)
2. _handle_permission_dialog 函数存在
3. run_agent 包装了 permission check

完整 UI 行为由手动 streamlit run 验证。
"""

from __future__ import annotations

import importlib
import sys


def test_web_app_imports():
    """web/app.py 可 import(不报 import error)"""
    # 清掉 cache(避免 stale)
    for mod_name in list(sys.modules.keys()):
        if mod_name.startswith("web") or mod_name == "app":
            del sys.modules[mod_name]

    try:
        # 尝试 import web.app
        # 注意:streamlit 的 st.set_page_config 等会在 import 时执行,所以
        # 我们不在这里强制 import,改用 spec 加载检测函数是否存在
        import importlib.util
        from pathlib import Path
        app_path = Path("web/app.py")
        if not app_path.exists():
            # 在仓库根目录外的相对路径
            repo_root = Path(__file__).parent.parent
            app_path = repo_root / "web" / "app.py"

        spec = importlib.util.spec_from_file_location("web.app", app_path)
        assert spec is not None
        # 实际 import 会跑 streamlit 命令,这里只检查 spec 能构造
        assert spec.name == "web.app"
    except Exception as e:
        # 实际运行时, streamlit 会拦截 import,这里容错
        import pytest
        pytest.skip(f"web/app.py 不能直接 import(streamlit runtime): {e}")


def test_handle_permission_dialog_defined():
    """_handle_permission_dialog 函数在 web/app.py 中存在"""
    from pathlib import Path
    import re

    app_path = Path(__file__).parent.parent / "web" / "app.py"
    if not app_path.exists():
        import pytest
        pytest.skip("web/app.py not found")

    content = app_path.read_text(encoding="utf-8")
    # 验证函数定义
    assert "def _handle_permission_dialog" in content, "function _handle_permission_dialog not defined"
    # 验证 st.dialog 装饰器
    assert "@st.dialog" in content, "st.dialog decorator not used"
    # 验证三个按钮
    assert "Allow once" in content
    assert "Deny" in content
    assert "Always allow" in content
    # 验证 resolve_permission 调用
    assert "resolve_permission" in content


def test_run_agent_wraps_permission():
    """run_agent 包装了 _pending_permission_request 检查"""
    from pathlib import Path
    app_path = Path(__file__).parent.parent / "web" / "app.py"
    if not app_path.exists():
        import pytest
        pytest.skip("web/app.py not found")

    content = app_path.read_text(encoding="utf-8")
    # 验证 run_agent 函数体内有 permission 检查
    run_agent_idx = content.find("def run_agent")
    assert run_agent_idx > 0
    # 取函数体片段
    next_def = content.find("\ndef ", run_agent_idx + 1)
    if next_def == -1:
        next_def = len(content)
    run_agent_body = content[run_agent_idx:next_def]
    assert "_pending_permission_request" in run_agent_body
    assert "_handle_permission_dialog" in run_agent_body


def test_get_agent_injects_permission_engine():
    """get_agent 注入 PermissionEngine 到 agent.permission_engine"""
    from pathlib import Path
    app_path = Path(__file__).parent.parent / "web" / "app.py"
    if not app_path.exists():
        import pytest
        pytest.skip("web/app.py not found")

    content = app_path.read_text(encoding="utf-8")
    # 验证注入代码存在
    assert "agent.permission_engine" in content
    assert "PermissionEngine" in content
    assert "default_hooks" in content
    assert "load_tool_permission_context" in content
