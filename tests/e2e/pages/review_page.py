"""
Candidate Review 页面 (web/pages/02_Candidate_Review.py) Page Object。
URL: /Candidate_Review
"""

from __future__ import annotations

from playwright.sync_api import Locator, Page

from .base_page import BasePage


class ReviewPage(BasePage):
    PATH = "/Candidate_Review"

    def __init__(self, page: Page, app_url: str) -> None:
        super().__init__(page, app_url)
        self.goto(self.PATH)

    def title_heading(self) -> Locator:
        return self.page.locator(
            '[data-testid="stAppViewContainer"] h1'
        ).first

    def title_text(self) -> str:
        return self.title_heading().inner_text().strip()

    def assert_title_is_review(self) -> None:
        """标题应包含 'Candidate' 或 '审核' / 'Review' 关键词。"""
        text = self.title_text()
        assert any(kw in text for kw in ("Candidate", "Review", "审核", "候选")), (
            f"页面标题不像 Candidate Review 页面: {text!r}"
        )

    def has_pending_badge(self) -> bool:
        """检查侧边栏是否显示 '待审' 提醒。"""
        sidebar = self.page.locator('[data-testid="stSidebar"]')
        return sidebar.get_by_text("待审").count() > 0
