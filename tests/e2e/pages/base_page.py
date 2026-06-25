"""
Streamlit Page Object 基类。

封装所有 Streamlit 通用交互, 避免用例直接写 selector:
- 等待页面加载完成 (stApp 渲染 + websocket 连上)
- 通用元素查找 (按 testid / 文本)
- 截图辅助
- 路由切换 (通过侧边栏的多页导航, 或直接 goto)

Streamlit DOM 关键 testid (1.38+):
- stApp           主容器
- stSidebar       侧边栏
- stChatInput     聊天输入框
- stChatMessage   聊天消息气泡
- stTextInput     文本输入
- stButton        按钮
- stSelectbox     下拉选择
- stHeader / stSubheader / stTitle / stCaption
- stDataFrame / stTable
- stTabs / stTab
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Optional

from playwright.sync_api import Locator, Page, expect


# Streamlit 启动时 websocket + 首次 render 通常要 2-3s, 冷启动可能 5-8s
DEFAULT_LOAD_TIMEOUT_MS = 20_000
STREAMLIT_WS_SETTLE_MS = 1_500  # render 完成后等 ws 稳定


class BasePage:
    """所有 Page Object 的基类。子类在 __init__ 里调 super().goto() 进入对应页。"""

    def __init__(self, page: Page, app_url: str) -> None:
        self.page = page
        self.base_url = app_url.rstrip("/")
        # Streamlit DOM 变化频繁 (websocket 推送), 默认超时拉到 20s
        self.page.set_default_timeout(DEFAULT_LOAD_TIMEOUT_MS)
        self.page.set_default_navigation_timeout(DEFAULT_LOAD_TIMEOUT_MS)

    # ─── 导航 ─────────────────────────────────────────────
    def goto(self, path: str = "/") -> "BasePage":
        """直接 goto URL。Streamlit 首次加载要等 stApp 出现 + ws 稳定。"""
        url = f"{self.base_url}{path}"
        self.page.goto(url, wait_until="domcontentloaded")
        self.wait_loaded()
        return self

    def wait_loaded(self) -> None:
        """等待 Streamlit 渲染完成。判断条件:
        1. [data-testid=stApp] 出现
        2. websocket 状态稳定 (页面无 spinner)
        """
        self.page.wait_for_selector(
            '[data-testid="stApp"]',
            state="visible",
            timeout=DEFAULT_LOAD_TIMEOUT_MS,
        )
        self._wait_no_spinner()
        # 给 websocket 一点时间把首次 render 推完
        self.page.wait_for_timeout(STREAMLIT_WS_SETTLE_MS)

    def _wait_no_spinner(self, timeout_ms: int = 15_000) -> None:
        """Streamlit 加载时会有 [data-testid=stStatusWidget] 或 Running... 文本。
        等待它消失, 表明 server 端已 render 完毕。"""
        # 优先等 spinner 出现再消失; 如果一直不出现 (极快的情况) 也直接返回
        try:
            self.page.wait_for_selector(
                '[data-testid="stStatusWidget"]',
                state="detached",
                timeout=timeout_ms,
            )
        except Exception:
            pass
        # 兜底: 兜一份等待时间, 防止 websocket 静默重连
        self.page.wait_for_timeout(300)

    # ─── 通用元素查找 ─────────────────────────────────────
    def find_by_testid(self, testid: str) -> Locator:
        return self.page.locator(f'[data-testid="{testid}"]')

    def find_by_text(self, text: str, exact: bool = False) -> Locator:
        return self.page.get_by_text(text, exact=exact)

    def find_by_role(self, role: str, name: str | None = None) -> Locator:
        loc = self.page.get_by_role(role)
        if name:
            loc = loc.filter(has_text=name)
        return loc

    # ─── 通用断言 ─────────────────────────────────────────
    def assert_title_contains(self, text: str) -> None:
        """断言 stApp 内出现某段文本 (作为标题/正文存在)。"""
        expect(self.find_by_text(text).first).to_be_visible(timeout=10_000)

    def assert_text_appears(self, text: str) -> None:
        """断言文本出现在页面任意位置。"""
        expect(self.find_by_text(text).first).to_be_visible(timeout=10_000)

    def assert_text_not_appears(self, text: str) -> None:
        """断言文本不应出现。"""
        expect(self.find_by_text(text).first).to_be_hidden(timeout=5_000)

    # ─── 截图 ─────────────────────────────────────────────
    def screenshot(
        self,
        name: str,
        subdir: str = "screenshots",
        full_page: bool = True,
    ) -> Path:
        """截屏并保存到 reports/<subdir>/<name>.png。
        pytest 会自动把 reports/ 加进 html 报告附件。"""
        out_dir = Path("reports") / subdir
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / f"{name}.png"
        self.page.screenshot(path=str(path), full_page=full_page)
        return path

    # ─── 侧边栏 / 多页导航 ───────────────────────────────
    def navigate_via_sidebar(self, page_label: str) -> None:
        """通过侧边栏的多页链接跳转到指定子页面。"""
        # Streamlit 多页导航在侧边栏, 链接文本通常就是页面文件名去掉前缀
        with self.page.expect_navigation(wait_until="domcontentloaded"):
            self.page.get_by_role("link", name=page_label).first.click()
        self.wait_loaded()
