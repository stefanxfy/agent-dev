"""
冒烟测试: 主页 (web/app.py) 能正常加载。

这是最基础的回归 — 如果这个挂了, 整个应用都不可用。
"""

from __future__ import annotations

import pytest

from .pages.home_page import HomePage


@pytest.mark.smoke
def test_home_page_loads(page, app_url: str) -> None:
    """主页应能打开, 标题包含 'Agent' 关键词。"""
    home = HomePage(page, app_url)

    # 截屏作为冒烟通过的证据
    home.screenshot("01_home_loaded")

    title = home.get_title_text()
    assert "Agent" in title or "🤖" in title, (
        f"主页标题异常: {title!r}"
    )


@pytest.mark.smoke
def test_home_page_has_sidebar(page, app_url: str) -> None:
    """主页应渲染侧边栏 (多页导航 + 状态展示)。"""
    home = HomePage(page, app_url)
    home.screenshot("02_home_sidebar")

    # Streamlit 默认会渲染侧边栏区域 (即使内容为空)
    sidebar = home.find_by_testid("stSidebar")
    sidebar.wait_for(state="visible", timeout=10_000)


@pytest.mark.smoke
def test_home_page_multipage_nav_visible(page, app_url: str) -> None:
    """侧边栏应显示多页导航链接 (Chat / Session_Management / Candidate_Review)。"""
    home = HomePage(page, app_url)
    home.screenshot("03_home_multipage_nav")

    sidebar = home.find_by_testid("stSidebar")
    # 至少有一个页面链接存在
    links = sidebar.locator("a[href]")
    assert links.count() >= 1, "侧边栏未渲染多页导航链接"
