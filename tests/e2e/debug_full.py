"""Debug: full setup_session + send_message, dump everything."""
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
            sidebar = chat.find_by_testid("stSidebar")

            zhipu_key = _os.getenv("ZHIPU_API_KEY", "TEST_KEY")

            # Step 1: click 新建会话
            print("\n=== STEP 1: click 新建会话 ===")
            sidebar.get_by_text("新建会话").first.click()
            chat.page.wait_for_timeout(3000)
            chat._wait_no_spinner()

            def dump(tag):
                try:
                    provider = sidebar.locator('[data-testid="stSelectbox"]').first.inner_text()
                except Exception:
                    provider = "<err>"
                try:
                    model = sidebar.get_by_label("Model").first.input_value()
                except Exception:
                    model = "<err>"
                try:
                    apikey = sidebar.get_by_label("API Key").first.input_value()
                except Exception:
                    apikey = "<err>"
                print(f"  [{tag}]")
                print(f"    Provider selectbox text: {provider!r}")
                print(f"    Model.value:    {model!r}")
                print(f"    APIKey.value:   {apikey!r}")

            dump("after_new_session")

            # Step 2: select_provider zhipu
            print("\n=== STEP 2: select_provider('zhipu') ===")
            chat.select_provider("zhipu")
            dump("after_select_provider")

            # Step 3: fill_model with keyboard.type
            print("\n=== STEP 3: fill_model('GLM-5.1') ===")
            chat.fill_model("GLM-5.1")
            dump("after_fill_model")

            # Step 4: fill_api_key
            print("\n=== STEP 4: fill_api_key(zhipu_key) ===")
            chat.fill_api_key(zhipu_key)
            dump("after_fill_api_key")

            # Step 5: check chat_input
            ci = chat.chat_input()
            print(f"\n  chat_input visible: {ci.is_visible()}")

            # Step 6: now send a message and capture response
            print("\n=== STEP 6: send 'hello' and capture full response ===")
            chat.send_message("hello")
            chat.page.wait_for_timeout(15_000)  # wait for LLM response
            dump("after_send")
            try:
                last = chat.last_assistant_text()
                print(f"  Last assistant text:\n---\n{last}\n---")
            except Exception as e:
                print(f"  last assistant read failed: {e}")

            chat.screenshot("dbg_full_final")
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