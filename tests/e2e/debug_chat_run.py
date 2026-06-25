"""Same flow as test, but reads debug expander at end."""
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
            try:
                chat.setup_session(api_key=zhipu_key, provider="zhipu", model="glm-4")
            except Exception as e:
                print(f"\n!!! setup_session failed: {e}")
                # Read debug expander
                try:
                    chat.find_by_text("🔧 调试信息").first.click()
                    chat.page.wait_for_timeout(1500)
                    debug_text = page.locator('[data-testid="stExpanderDetails"]').last.inner_text()
                    print("=== DEBUG EXPANDER (after failure) ===")
                    print(debug_text)
                    print("===")
                except Exception:
                    pass
                # Check error message
                err = page.get_by_text("请先在侧边栏输入 API Key")
                print(f"  API Key error visible: {err.is_visible() if err.count() else False}")
                chat.screenshot("dbg_setup_failed")
                raise

            # Open debug expander
            chat.find_by_text("🔧 调试信息").first.click()
            chat.page.wait_for_timeout(2_000)

            debug_text = page.locator('[data-testid="stExpanderDetails"]').last.inner_text()
            print("=== BEFORE SEND ===")
            print(debug_text)
            print("===\n")

            # Send a message
            chat.send_message("你好")
            chat.page.wait_for_timeout(20_000)

            # Read debug again
            debug_text = page.locator('[data-testid="stExpanderDetails"]').last.inner_text()
            print("=== AFTER SEND ===")
            print(debug_text)
            print("===\n")

            # Dump chat messages
            msgs = chat.assistant_messages()
            for i in range(msgs.count()):
                print(f"  assistant[{i}]: {msgs.nth(i).inner_text()[:200]!r}")

            chat.screenshot("dbg_after_send")
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