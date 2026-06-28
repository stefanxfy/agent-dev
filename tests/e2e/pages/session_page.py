"""
Session Management 页面 (web/pages/01_Session_Management.py) Page Object。
URL: /Session_Management
"""

from __future__ import annotations

from playwright.sync_api import Locator, Page

from .base_page import BasePage


class SessionPage(BasePage):
    PATH = "/Session_Management"

    def __init__(self, page: Page, app_url: str) -> None:
        super().__init__(page, app_url)
        self.goto(self.PATH)

    def sidebar(self) -> Locator:
        return self.page.locator('[data-testid="stSidebar"]')

    def new_session_button(self) -> Locator:
        return self.sidebar().get_by_role("button", name="创建新会话")

    def session_list_section(self) -> Locator:
        """侧边栏 '已有会话' 区域。"""
        return self.sidebar().locator(
            'text=已有会话'
        ).first

    def tabs(self) -> Locator:
        """主区 tabs (发送消息 / 工具调用 / 等)。"""
        return self.page.locator('[data-testid="stTabs"]')

    def assert_new_session_button_visible(self) -> None:
        self.new_session_button().wait_for(state="visible", timeout=10_000)

    def click_new_session(self) -> None:
        self.new_session_button().click()
        # 新建后会刷新, 等待 sidebar 更新
        self.page.wait_for_timeout(1_000)
        self._wait_no_spinner()
