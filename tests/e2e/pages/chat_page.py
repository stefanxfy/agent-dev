"""
Chat 页面 (web/pages/00_Chat.py) Page Object。
URL: /Chat

支持两种模式:
1. 纯 UI 断言 (test_02_*) — 只检查元素存在, 不发消息
2. 真实 LLM 对话 (test_05_*) — 走完多轮 chat, 验证响应内容
"""

from __future__ import annotations

import json
import time
from typing import Optional

from playwright.sync_api import Locator, Page, TimeoutError as PWTimeout

from .base_page import BasePage


class ChatPage(BasePage):
    PATH = "/Chat"

    # 等待 AI 响应完成的标志 — 最后出现 "💾 已保存" caption
    RESPONSE_DONE_MARKER = "已保存到 session"
    TOOL_CALL_MARKER = "🔧 调用工具"

    def __init__(self, page: Page, base_url: str) -> None:
        super().__init__(page, base_url)
        self.goto(self.PATH)

    # ─── 元素定位 ─────────────────────────────────────────
    def chat_input(self) -> Locator:
        """聊天输入框 (Streamlit 1.38+ 的 st.chat_input textarea)。"""
        return self.page.locator('[data-testid="stChatInput"] textarea').first

    def chat_messages(self) -> Locator:
        """所有已渲染的聊天消息气泡。"""
        return self.page.locator('[data-testid="stChatMessage"]')

    def assistant_messages(self) -> Locator:
        """所有 assistant 消息 (按出现顺序)。"""
        return self.chat_messages().filter(
            has=self.page.locator('[data-testid="stChatMessageAvatarAssistant"]')
        )

    def last_assistant_message(self) -> Locator:
        """最后一条 assistant 消息。"""
        return self.assistant_messages().last

    def last_assistant_text(self) -> str:
        """最后一条 assistant 消息的纯文本 (去除 markdown 符号)。"""
        return self.last_assistant_message().inner_text().strip()

    def all_assistant_texts(self) -> list[str]:
        """所有 assistant 消息文本列表。"""
        msgs = self.assistant_messages()
        return [msgs.nth(i).inner_text().strip() for i in range(msgs.count())]

    def tool_call_captions(self) -> list[str]:
        """所有工具调用 caption 文本 (🔧 调用工具: xxx)。"""
        captions = self.page.locator('text=/🔧\\s*调用工具/').all()
        return [c.inner_text().strip() for c in captions]

    def session_done_caption(self) -> Locator:
        """💾 已保存到 session 标记 (response 完成的信号)。"""
        return self.page.locator(f'text=/{self.RESPONSE_DONE_MARKER}/').last

    # ─── Streamlit 受控输入专用 ────────────────────────────
    def _set_streamlit_value(self, locator: Locator, value: str) -> None:
        """可靠地设置 Streamlit text_input 的值 (React-style setter + 显式事件)。

        为什么不用 locator.fill():
            Streamlit text_input 用 debounce 监听 input 事件,
            Playwright 的 fill() 在快速操作 / 跨 rerun 时
            经常不触发 onChange, session_state 不更新。
            这里用 React 社区通用 trick — 走原型 setter + 手动 dispatch,
            保证 input/change/blur 三个事件都触发, 必 commit。

        Args:
            locator: 已经定位到的 <input> 元素 (sidebar.get_by_label(...).first)
            value:   要写入的值
        """
        js = """
        (el, value) => {
            const proto = window.HTMLInputElement.prototype;
            const setter = Object.getOwnPropertyDescriptor(proto, 'value').set;
            setter.call(el, value);
            el.dispatchEvent(new Event('input',  {bubbles: true}));
            el.dispatchEvent(new Event('change', {bubbles: true}));
            el.dispatchEvent(new FocusEvent('blur', {bubbles: true}));
        }
        """
        locator.evaluate(js, value)

    # ─── 基础交互 ─────────────────────────────────────────
    def send_message(self, text: str) -> None:
        """发送一条消息 (不等待响应)。"""
        textarea = self.chat_input()
        textarea.fill(text)
        textarea.press("Enter")

    def assert_chat_input_visible(self) -> None:
        self.chat_input().wait_for(state="visible", timeout=10_000)

    def assert_history_empty(self) -> None:
        self.page.wait_for_timeout(500)
        count = self.chat_messages().count()
        assert count == 0, f"预期空聊天, 实际有 {count} 条消息"

    # ─── 真实对话 (LLM 模式) ──────────────────────────────
    def setup_session(
        self,
        api_key: str = "test-key-not-real",
        provider: str = "anthropic",  # 默认 anthropic (页面默认, 无需切换)
        model: str = "claude-3-7-sonnet-20250219",
        debug: bool = False,  # True 时每步截图, 排查 setup 流程用
    ) -> None:
        """创建新会话, 填入 API Key, 进入可对话状态。

        ⚠️ 关键: 必须 **两遍** 新建会话:
        - 第一遍让 sidebar widget 出现
        - 第二遍在所有 widget 稳定后强制 agent=None + rerun,
          让 agent 用最新 session_state (Provider/Model/APIKey) 重新初始化。

        为什么需要: chat 页面的 agent 初始化逻辑是
            if st.session_state.agent is None: ...
        任何 rerun 期间, 只要 api_key 非空 (含环境变量兜底),
        就会触发 agent init。如果 fill_model 还没来得及把 session_state["chat_model"]
        设成目标值, agent 就用旧的 anthropic 默认值初始化了。
        """
        sidebar = self.find_by_testid("stSidebar")

        # 1. 第一遍新建: 让 sidebar widget 出现
        new_btn = sidebar.get_by_text("新建会话").first
        new_btn.wait_for(state="visible", timeout=10_000)
        new_btn.click()
        self.page.wait_for_timeout(3_000)
        self._wait_no_spinner()
        if debug:
            self.screenshot("debug_01_after_new_session")

        # 2. (可选) 切 provider
        if provider != "anthropic":
            self.select_provider(provider)
            if debug:
                self.screenshot("debug_02_after_select_provider")
            self.fill_model(model)
            if debug:
                self.screenshot("debug_03_after_fill_model")

        # 3. 填 API Key
        self.fill_api_key(api_key)
        if debug:
            self.screenshot("debug_04_after_fill_api_key")

        # 4. 强制 agent 重新初始化
        # 此时 session_state 里的 Provider/Model/APIKey 都已稳定,
        # 再点一次 新建会话 → agent=None → rerun → 用最新 session_state 重新 init
        new_btn2 = sidebar.get_by_text("新建会话").first
        if new_btn2.is_visible():
            new_btn2.click()
            self.page.wait_for_timeout(3_000)
            self._wait_no_spinner()
        if debug:
            self.screenshot("debug_05_after_second_new_session")

        # 5. 等 chat_input 出现
        try:
            self.assert_chat_input_visible()
        except PWTimeout:
            if debug:
                self.screenshot("debug_06_chat_input_timeout")
            raise

    def select_provider(self, provider: str) -> None:
        """切到指定 provider (anthropic/openai/zhipu)。
        Streamlit selectbox 1.38+ 用 BaseWeb 组件, 点击展开后 option 会出现在 portal 里。"""
        sidebar = self.find_by_testid("stSidebar")
        selectbox = sidebar.locator('[data-testid="stSelectbox"]').first
        selectbox.scroll_into_view_if_needed()
        selectbox.click()
        # 等选项出现 (option role, 不依赖 listbox 容器)
        option = self.page.get_by_role("option", name=provider, exact=True)
        option.wait_for(state="visible", timeout=5_000)
        option.click()
        # 等 streamlit re-render (切 provider 会清掉 Model 字段, 需要重新填)
        self.page.wait_for_timeout(2_500)
        self._wait_no_spinner()

    def _set_input_via_react_setter(self, locator, value: str) -> None:
        """Streamlit text_input 的 commit 触发模式:
        走 React 原生 value setter + 派发 input 事件 + change 事件。
        这是 React 社区对 controlled input 的标准 trick,
        Playwright 的 fill() / keyboard.type() 在 Streamlit 1.38+ 不可靠。
        """
        js = """
        (el, value) => {
            const proto = window.HTMLInputElement.prototype;
            const desc = Object.getOwnPropertyDescriptor(proto, 'value');
            desc.set.call(el, value);
            el.dispatchEvent(new Event('input',  {bubbles: true}));
            el.dispatchEvent(new Event('change', {bubbles: true}));
        }
        """
        locator.evaluate(js, value)

    def fill_model(self, model: str = "") -> None:
        """填 Model 字段。空字符串时填 glm-4 (zhipu 默认)。"""
        sidebar = self.find_by_testid("stSidebar")
        model_input = sidebar.get_by_label("Model").first
        model_input.wait_for(state="visible", timeout=10_000)
        model_input.click()  # focus (重要: Streamlit 在 focused 时才处理事件)
        self._set_input_via_react_setter(model_input, model or "glm-4")
        self.page.wait_for_timeout(2_000)  # 等 rerun + debounce commit
        self._wait_no_spinner()

    def fill_api_key(self, api_key: str) -> None:
        """填 API Key。"""
        sidebar = self.find_by_testid("stSidebar")
        api_input = sidebar.get_by_label("API Key").first
        api_input.wait_for(state="visible", timeout=10_000)
        api_input.click()
        self._set_input_via_react_setter(api_input, api_key)
        self.page.wait_for_timeout(2_500)  # 等 rerun + Agent 初始化
        self._wait_no_spinner()

    def send_and_wait(
        self,
        text: str,
        timeout: int = 60,
    ) -> str:
        """发送消息, 等待 AI 完整响应, 返回最后一条 assistant 文本。

        等待策略:
        1. 等 '💾 已保存到 session' 出现 (response 完全 done)
        2. 兜底: 超时后取当前最后一条 assistant 消息
        """
        n_before = self.assistant_messages().count()

        # 1. 发送
        self.send_message(text)

        # 2. 等待 "💾 已保存到 session" caption 出现 — 表示 response 完全结束
        try:
            self.session_done_caption().wait_for(
                state="visible",
                timeout=timeout * 1000,
            )
        except PWTimeout:
            # 兜底: 多等一会再读
            self.page.wait_for_timeout(3_000)

        # 3. 多等 1s 让 markdown 完全 render
        self.page.wait_for_timeout(1_000)

        # 4. 读最后一条 assistant 消息
        n_after = self.assistant_messages().count()
        if n_after <= n_before:
            # 没新增 assistant 消息, 返回空 (可能是出错)
            return ""
        return self.last_assistant_text()

    def run_scenario(
        self,
        messages: list[str],
        timeout: int = 60,
    ) -> list[str]:
        """跑一轮多轮对话, 返回每一轮 assistant 响应列表。"""
        replies: list[str] = []
        for msg in messages:
            reply = self.send_and_wait(msg, timeout=timeout)
            replies.append(reply)
        return replies

    def assert_tool_called(self, tool_name: str) -> None:
        """断言在最近的响应中调用了指定工具。
        工具调用会在聊天区出现 '🔧 调用工具: xxx' caption。"""
        captions = self.tool_call_captions()
        assert any(tool_name in c for c in captions), (
            f"未检测到工具调用 '{tool_name}', 实际 caption: {captions}"
        )
