"""
回归测试: Candidate Review 页面 (/Candidate_Review)。
"""

from __future__ import annotations

import pytest

from .pages.review_page import ReviewPage


@pytest.mark.regression
def test_review_page_loads(page, app_url: str) -> None:
    """Candidate Review 页面应能正常打开, 标题包含 'Candidate'/'Review'/'审核'/'候选' 之一。"""
    rp = ReviewPage(page, app_url)
    rp.screenshot("01_review_loaded")

    rp.assert_title_is_review()


@pytest.mark.regression
def test_review_page_no_console_errors(page, app_url: str) -> None:
    """页面加载过程不应出现 JS console error。
    这是检测 Streamlit 组件渲染失败、版本不兼容等问题的快速指标。"""
    errors: list[str] = []
    page.on("pageerror", lambda exc: errors.append(f"pageerror: {exc}"))
    page.on(
        "console",
        lambda msg: errors.append(f"console.error: {msg.text}")
        if msg.type == "error" else None,
    )

    ReviewPage(page, app_url)
    page.wait_for_timeout(2_000)  # 给 websocket 推完的时间

    # 过滤已知无害的 404 (Streamlit 静态资源, dev 模式可能找不到)
    real_errors = [
        e for e in errors
        if "favicon" not in e.lower()
        and "third-party cookie" not in e.lower()
        # Streamlit 在 dev mode 下, 一些 static 资源返回 404, 属于环境噪声
        and not ("404" in e and "Failed to load resource" in e)
    ]
    assert not real_errors, f"页面加载出现错误:\n" + "\n".join(real_errors)


@pytest.mark.regression
def test_review_page_navigates_via_sidebar(page, app_url: str) -> None:
    """从主页通过侧边栏导航到 Candidate Review 页面。"""
    # 先到主页
    page.goto(f"{app_url}/", wait_until="domcontentloaded")
    page.wait_for_selector('[data-testid="stApp"]', state="visible", timeout=20_000)
    page.wait_for_timeout(1_500)

    # 找侧边栏里包含 'Candidate' 的链接
    sidebar = page.locator('[data-testid="stSidebar"]')
    link = sidebar.get_by_role("link").filter(has_text="Candidate").first
    link.wait_for(state="visible", timeout=10_000)
    link.click()

    # 等待跳转
    page.wait_for_url("**/Candidate_Review", timeout=10_000)
    page.wait_for_timeout(2_000)

    # 验证
    heading = page.locator('[data-testid="stAppViewContainer"] h1').first
    heading.wait_for(state="visible", timeout=10_000)
    text = heading.inner_text().strip()
    assert any(kw in text for kw in ("Candidate", "Review", "审核", "候选")), text
