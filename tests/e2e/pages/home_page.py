"""
主页面 (web/app.py) Page Object。
URL: /
"""

from __future__ import annotations

from playwright.sync_api import Page

from .base_page import BasePage


class HomePage(BasePage):
    PATH = "/"

    def __init__(self, page: Page, app_url: str) -> None:
        super().__init__(page, app_url)
        self.goto(self.PATH)

    def get_title_text(self) -> str:
        """读取页面顶部 stApp 里的标题文本。"""
        # stTitle 的 testid = "stHeading", 取第一个 H1
        return self.page.locator(
            '[data-testid="stAppViewContainer"] h1'
        ).first.inner_text().strip()
