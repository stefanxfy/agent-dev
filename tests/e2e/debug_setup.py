"""Quick debug: trace what happens during setup_session.

跑法: cd tests/e2e && python3 debug_setup.py
会按顺序打印每个步骤后 Model/API Key 输入框的真实 value 属性。
"""
import time
import sys
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

        # Start streamlit manually first
        import subprocess
        import os as _os
        env = _os.environ.copy()
        env["STREAMLIT_SERVER_HEADLESS"] = "true"
        cmd = [
            sys.executable, "-m", "streamlit", "run",
            str(project_root / "web" / "app.py"),
            "--server.headless=true", "--server.port=8501",
            "--server.address=127.0.0.1",
            "--browser.gatherUsageStats=false",
            "--server.runOnSave=false",
            "--server.fileWatcherType=none",
        ]
        proc = subprocess.Popen(cmd, cwd=str(project_root), env=env)
        time.sleep(8)

        try:
            chat = ChatPage(page, "http://127.0.0.1:8501")
            sidebar = chat.find_by_testid("stSidebar")

            # Step 1: click 新建会话
            print("\n=== STEP 1: 点击 新建会话 ===")
            new_btn = sidebar.get_by_text("新建会话").first
            new_btn.click()
            chat.page.wait_for_timeout(3000)
            chat._wait_no_spinner()
            chat.screenshot("dbg_01_after_new")

            def read_inputs(tag: str) -> None:
                model = sidebar.get_by_label("Model").first
                apikey = sidebar.get_by_label("API Key").first
                mv = model.input_value() if model.count() else "<missing>"
                av = apikey.input_value() if apikey.count() else "<missing>"
                print(f"  [{tag}] Model.value={mv!r}, APIKey.value={av!r}")
                print(f"  [{tag}] Model.count={model.count()}, APIKey.count={apikey.count()}")

            read_inputs("after_new_session")

            # Step 2: select_provider zhipu
            print("\n=== STEP 2: select_provider('zhipu') ===")
            chat.select_provider("zhipu")
            chat.screenshot("dbg_02_after_select")
            read_inputs("after_select_provider")

            # Step 3: fill_model via _set_streamlit_value
            print("\n=== STEP 3: fill_model('GLM-5.1') via _set_streamlit_value ===")
            model_input = sidebar.get_by_label("Model").first
            chat._set_streamlit_value(model_input, "GLM-5.1")
            chat.page.wait_for_timeout(2000)
            chat._wait_no_spinner()
            chat.screenshot("dbg_03_after_model")
            read_inputs("after_fill_model")

            # Step 4: fill_api_key
            print("\n=== STEP 4: fill_api_key(zhipu_key) ===")
            api_input = sidebar.get_by_label("API Key").first
            zhipu_key = _os.getenv("ZHIPU_API_KEY", "TEST_KEY")
            chat._set_streamlit_value(api_input, zhipu_key)
            chat.page.wait_for_timeout(3000)
            chat._wait_no_spinner()
            chat.screenshot("dbg_04_after_key")
            read_inputs("after_fill_api_key")

            # Step 5: check chat_input visibility
            print("\n=== STEP 5: check chat_input ===")
            ci = chat.chat_input()
            print(f"  chat_input count: {ci.count()}, visible: {ci.is_visible() if ci.count() else False}")
            chat.screenshot("dbg_05_final")

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