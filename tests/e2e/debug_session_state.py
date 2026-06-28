"""Inspect session_state values via the chat page's debug expander."""
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
            zhipu_key = _os.getenv("ZHIPU_API_KEY", "TEST_KEY")
            chat.setup_session(api_key=zhipu_key, provider="zhipu", model="glm-4")

            # Open the debug expander and read its text
            debug_exp = chat.find_by_text("🔧 调试信息").first
            debug_exp.click()
            chat.page.wait_for_timeout(2_000)

            # Read the debug content
            debug_text = page.locator('[data-testid="stExpanderDetails"]').last.inner_text()
            print("=== Debug expander content ===")
            print(debug_text)
            print("=== End ===")

            # Also dump input values
            sidebar = chat.find_by_testid("stSidebar")
            provider = sidebar.locator('[data-testid="stSelectbox"]').first.inner_text()
            model = sidebar.get_by_label("Model").first.input_value()
            apikey = sidebar.get_by_label("API Key").first.input_value()
            print(f"\n[final state] Provider: {provider!r}")
            print(f"[final state] Model: {model!r}")
            print(f"[final state] APIKey len: {len(apikey)}")

            chat.screenshot("dbg_session_state")
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