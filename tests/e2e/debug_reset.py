"""Debug: trace what happens AFTER _reset_session clicks 新建会话.

Hypothesis: setup_session succeeds (chat_input visible), then _reset_session
clicks 新建会话 → page rerun → agent reset → chat_input hidden.
"""
import time
import sys
import os as _os
from pathlib import Path

from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from pages.chat_page import ChatPage  # noqa: E402


def main() -> int:
    from dotenv import load_dotenv
    project_root = Path(__file__).resolve().parents[2]
    load_dotenv(project_root / ".env", override=False)

    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome", headless=True)
        page = browser.new_context().new_page()
        page.set_viewport_size({"width": 1440, "height": 900})

        import subprocess
        env = _os.environ.copy()
        cmd = [
            sys.executable, "-m", "streamlit", "run",
            str(project_root / "web" / "app.py"),
            "--server.headless=true", "--server.port=8501",
            "--server.address=127.0.0.1",
        ]
        proc = subprocess.Popen(cmd, cwd=str(project_root), env=env)
        time.sleep(8)

        try:
            chat = ChatPage(page, "http://127.0.0.1:8501")

            # === setup_session ===
            print("\n=== setup_session ===")
            zhipu_key = _os.getenv("ZHIPU_API_KEY", "TEST_KEY")
            chat.setup_session(api_key=zhipu_key, provider="zhipu")
            ci = chat.chat_input()
            print(f"  After setup_session: chat_input visible = {ci.is_visible()}")

            # === _reset_session ===
            print("\n=== _reset_session (click 新建会话) ===")
            new_btn = chat.find_by_testid("stSidebar").get_by_text("新建会话").first
            new_btn.click()
            chat.page.wait_for_timeout(3000)
            chat._wait_no_spinner()
            ci = chat.chat_input()
            print(f"  After _reset_session: chat_input count = {ci.count()}, visible = {ci.is_visible() if ci.count() else 'N/A'}")

            # Check error message
            err = page.get_by_text("请先在侧边栏输入 API Key")
            print(f"  Error visible: {err.is_visible() if err.count() else False}")

            sidebar = chat.find_by_testid("stSidebar")
            try:
                model_val = sidebar.get_by_label("Model").first.input_value()
                key_val = sidebar.get_by_label("API Key").first.input_value()
                print(f"  Model.value={model_val!r}, APIKey.value={key_val!r}")
            except Exception as e:
                print(f"  Read inputs failed: {e}")

            chat.screenshot("dbg_reset_state")
            browser.close()
            return 0
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()


if __name__ == "__main__":
    sys.exit(main())