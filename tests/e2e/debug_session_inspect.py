"""Read session_state values via Streamlit's websocket message protocol."""
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

            # Try to read session_state via window.streamlitDebug (Streamlit 1.30+ exposes this)
            state_info = page.evaluate("""
                () => {
                    // Streamlit exposes some internal objects in dev mode
                    // Look for any way to access session_state
                    const result = {};
                    result.hasStreamlitDebug = !!window.streamlitDebug;
                    if (window.streamlitDebug) {
                        try {
                            result.streamlitDebugKeys = Object.keys(window.streamlitDebug).slice(0, 30);
                        } catch(e) { result.err = String(e); }
                    }
                    // Try various globals
                    const globals = ['Streamlit', 'streamlit', '__streamlit'];
                    result.globals = {};
                    for (const g of globals) {
                        if (window[g]) result.globals[g] = typeof window[g];
                    }
                    return result;
                }
            """)
            print("Globals check:", state_info)

            # Click 新建会话
            print("\n=== 新建会话 ===")
            sidebar.get_by_text("新建会话").first.click()
            chat.page.wait_for_timeout(3000)
            chat._wait_no_spinner()

            # Try select_provider via my method
            print("\n=== select_provider zhipu ===")
            chat.select_provider("zhipu")

            # fill_model
            print("\n=== fill_model ===")
            chat.fill_model("glm-4")

            # Check DOM value
            model_dom = sidebar.get_by_label("Model").first.input_value()
            print(f"  Model DOM: {model_dom!r}")

            # Try to find the React fiber or actual input event listeners
            event_info = page.evaluate("""
                () => {
                    const inputs = document.querySelectorAll('input[type="text"]');
                    const result = [];
                    for (const inp of inputs) {
                        result.push({
                            value: inp.value,
                            ariaLabel: inp.getAttribute('aria-label'),
                            name: inp.name,
                            id: inp.id,
                        });
                    }
                    return result;
                }
            """)
            print(f"  All text inputs: {event_info}")

            # fill_api_key
            print("\n=== fill_api_key ===")
            chat.fill_api_key("TEST_KEY_VALUE")

            # Check values
            model_dom = sidebar.get_by_label("Model").first.input_value()
            key_dom = sidebar.get_by_label("API Key").first.input_value()
            print(f"  After fill_api_key - Model DOM: {model_dom!r}, APIKey DOM len: {len(key_dom)}")

            # Open debug expander and read it
            chat.find_by_text("🔧 调试信息").first.click()
            chat.page.wait_for_timeout(1500)
            debug_text = page.locator('[data-testid="stExpanderDetails"]').last.inner_text()
            print("\n=== DEBUG EXPANDER ===")
            print(debug_text)
            print("===")

            chat.screenshot("dbg_inspect")
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