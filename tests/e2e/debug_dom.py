"""Inspect the Streamlit input DOM structure thoroughly."""
import time
import sys
import os as _os
from pathlib import Path

from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))


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
            page.goto("http://127.0.0.1:8501/Chat", wait_until="domcontentloaded")
            page.wait_for_selector('[data-testid="stApp"]', timeout=20_000)

            sidebar = page.locator('[data-testid="stSidebar"]')

            # Find all input elements in sidebar
            info = page.evaluate("""
                () => {
                    const sidebar = document.querySelector('[data-testid="stSidebar"]');
                    if (!sidebar) return {err: 'no sidebar'};
                    const allInputs = sidebar.querySelectorAll('input');
                    const result = [];
                    for (const inp of allInputs) {
                        result.push({
                            type: inp.type,
                            value: inp.value,
                            ariaLabel: inp.getAttribute('aria-label'),
                            placeholder: inp.placeholder,
                            id: inp.id,
                            name: inp.name,
                            className: inp.className,
                            visible: inp.offsetParent !== null,
                            readOnly: inp.readOnly,
                            disabled: inp.disabled,
                        });
                    }
                    return result;
                }
            """)
            print("Sidebar inputs:", info)

            # Click 新建会话
            sidebar.get_by_text("新建会话").first.click()
            page.wait_for_timeout(3000)

            # Find all inputs again
            info = page.evaluate("""
                () => {
                    const sidebar = document.querySelector('[data-testid="stSidebar"]');
                    const allInputs = sidebar.querySelectorAll('input');
                    const result = [];
                    for (const inp of allInputs) {
                        result.push({
                            type: inp.type,
                            value: inp.value,
                            ariaLabel: inp.getAttribute('aria-label'),
                            placeholder: inp.placeholder,
                            id: inp.id,
                            visible: inp.offsetParent !== null,
                        });
                    }
                    return result;
                }
            """)
            print("\nAfter 新建会话:", info)

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