"""
回归测试: Session Management 页面 (/Session_Management)。
"""

from __future__ import annotations

import pytest

from .pages.session_page import SessionPage


@pytest.mark.regression
def test_session_page_loads(page, app_url: str) -> None:
    """Session Management 页面应能正常打开, 标题包含 '会话'。"""
    sp = SessionPage(page, app_url)
    sp.screenshot("01_session_loaded")

    sp.assert_title_contains("会话管理")


@pytest.mark.regression
def test_session_page_has_create_button(page, app_url: str) -> None:
    """应有 '创建新会话' 按钮。"""
    sp = SessionPage(page, app_url)
    sp.screenshot("02_session_create_button")

    sp.assert_new_session_button_visible()


@pytest.mark.regression
def test_session_page_empty_state(page, app_url: str) -> None:
    """未选会话时, 主区应显示空状态提示。
    tabs 只有在选中会话后才渲染, 所以空状态下断言提示信息更稳健。"""
    sp = SessionPage(page, app_url)
    sp.screenshot("03_session_empty_state")

    # 期望看到 '请在左侧创建或选择' 之类的引导文案
    sp.find_by_text("左侧").first.wait_for(state="visible", timeout=10_000)


@pytest.mark.regression
def test_session_page_create_then_tabs(page, app_url: str) -> None:
    """新建会话后, 主区应渲染出 tabs (发送消息 / 工具调用 / 状态)。"""
    sp = SessionPage(page, app_url)

    # 创建新会话
    sp.click_new_session()
    sp.screenshot("03_session_after_create")

    # tabs 出现
    tabs = sp.tabs()
    tabs.first.wait_for(state="visible", timeout=15_000)
    tab_count = tabs.locator('[data-testid="stTab"]').count()
    assert tab_count >= 2, f"tabs 数量不足: {tab_count}"
