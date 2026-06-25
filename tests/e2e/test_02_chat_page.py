"""
回归测试: Chat 页面 (/Chat) 关键元素渲染 + 基础交互。

注: 本项目 chat 依赖 LLM API, 没有 API key 时发送消息会报错。
    本测试只验证 UI 层 — 页面加载、关键组件可见、布局正确。
    真实 chat 流程属于集成测试范畴, 应该在 e2e_chat_with_api.py 里独立跑。
"""

from __future__ import annotations

import pytest

from .pages.chat_page import ChatPage


@pytest.mark.regression
def test_chat_page_loads(page, app_url: str) -> None:
    """Chat 页面应能正常打开, 显示 'Agent 聊天' 标题。"""
    chat = ChatPage(page, app_url)
    chat.screenshot("01_chat_loaded")

    chat.assert_title_contains("Agent 聊天")


@pytest.mark.regression
def test_chat_input_visible_after_session(page, app_url: str) -> None:
    """创建会话后, 主区应进入 '等待 API Key' 或 '聊天' 状态。
    注: 没有 API Key 时, chat_input 被门控, 所以这里只断言页面已从空状态切换出来。"""
    chat = ChatPage(page, app_url)

    # 创建新会话 (sidebar 第一个 ➕ 按钮)
    new_btn = chat.find_by_testid("stSidebar").get_by_text("新建会话").first
    new_btn.wait_for(state="visible", timeout=10_000)
    new_btn.click()
    # 等 streamlit 重新 render
    chat.page.wait_for_timeout(2_500)
    chat._wait_no_spinner()

    chat.screenshot("02_chat_input_visible")

    # 空状态已消失 (出现 '请先在侧边栏输入 API Key' 或 chat_input)
    main_text = chat.page.locator('[data-testid="stAppViewContainer"]').inner_text()
    # '请在侧边栏新建会话' 是空状态, 此时已不在
    assert "请在侧边栏新建会话" not in main_text, \
        "点击新建会话后, 页面仍显示空状态提示"

    # 至少有其中一个: chat_input 出现, 或 API Key 提示出现
    has_chat = chat.chat_input().count() > 0
    has_api_key_warning = "API Key" in main_text
    assert has_chat or has_api_key_warning, \
        f"页面应进入 API Key 提示 或 chat_input 状态, 实际: {main_text[:200]}"


@pytest.mark.regression
def test_chat_sidebar_has_session_controls(page, app_url: str) -> None:
    """Chat 页面侧边栏应包含 '新建会话' 按钮。"""
    chat = ChatPage(page, app_url)
    chat.screenshot("03_chat_sidebar")

    sidebar = chat.find_by_testid("stSidebar")
    # '新建会话' 或 '➕ 新建会话'
    new_btn = sidebar.get_by_text("新建会话")
    new_btn.first.wait_for(state="visible", timeout=10_000)


@pytest.mark.regression
def test_chat_page_has_provider_selector(page, app_url: str) -> None:
    """Chat 页面应有 LLM Provider 下拉选择 (anthropic/openai/zhipu)。"""
    chat = ChatPage(page, app_url)
    chat.screenshot("04_chat_provider_selector")

    # selectbox 渲染为 [data-testid="stSelectbox"]
    selectbox = chat.find_by_testid("stSelectbox")
    selectbox.first.wait_for(state="visible", timeout=10_000)
