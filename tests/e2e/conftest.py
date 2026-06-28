"""
E2E 测试 fixtures + hooks — 启动 Streamlit server、注入 Playwright、附加截图到报告。

用法 (在 tests/e2e/ 目录里):
    pytest -v
    pytest -v --headed
    pytest -v test_02_chat_page.py

Streamlit server 在 session 级别启动, 整个测试套件共享一个进程, 跑完自动 kill。
每个测试函数拿到的 page 已经是新页面 (独立 cookie/storage)。
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Generator

import pytest
from playwright.sync_api import Page


# ────────────────────────────────────────────────────────────
# 配置常量
# ────────────────────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).resolve().parents[2]  # agent-dev/
APP_ENTRY = PROJECT_ROOT / "web" / "app.py"
STREAMLIT_HOST = "127.0.0.1"
STREAMLIT_PORT = 8501
STREAMLIT_BASE_URL = f"http://{STREAMLIT_HOST}:{STREAMLIT_PORT}"
SERVER_STARTUP_TIMEOUT = 60  # Streamlit 首次冷启动较慢


# ────────────────────────────────────────────────────────────
# Streamlit server 生命周期
# ────────────────────────────────────────────────────────────
def _wait_for_port(host: str, port: int, timeout: int) -> bool:
    """轮询端口直到可连, 或超时。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(1.0)
            try:
                sock.connect((host, port))
                return True
            except OSError:
                time.sleep(0.5)
    return False


def _wait_for_streamlit_ready(base_url: str, timeout: int) -> bool:
    """Streamlit 端口可达 ≠ HTTP 就绪。再 GET 一次确认服务没在加载中。"""
    import urllib.request
    import urllib.error

    deadline = time.time() + timeout
    last_err = None
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(base_url, timeout=3) as resp:
                if resp.status == 200:
                    return True
        except (urllib.error.URLError, ConnectionError, OSError) as e:
            last_err = e
        time.sleep(1.0)
    print(f"[conftest] Streamlit 未就绪: {last_err}", file=sys.stderr)
    return False


@pytest.fixture(scope="session")
def streamlit_server() -> Generator[str, None, None]:
    """Session 级别 fixture: 启动 streamlit run web/app.py, 结束自动 kill。

    跳过条件: 环境变量 STREAMLIT_SKIP_SERVER=1 (CI 上手动起服务时用)。

    副作用: 会把项目根 .env 加载进 os.environ (再透传给 streamlit 子进程),
    让 chat 页面能用 ZHIPU_API_KEY / OPENAI_API_KEY 等作为兜底。
    """
    if os.environ.get("STREAMLIT_SKIP_SERVER") == "1":
        yield STREAMLIT_BASE_URL
        return

    if not APP_ENTRY.exists():
        pytest.fail(f"找不到 Streamlit 入口: {APP_ENTRY}")

    # 把 .env 加载进 os.environ, streamlit 子进程能继承到 API key
    env_file = PROJECT_ROOT / ".env"
    if env_file.exists():
        from dotenv import load_dotenv
        load_dotenv(env_file, override=False)  # 已有变量不覆盖

    env = os.environ.copy()
    env.setdefault("STREAMLIT_SERVER_HEADLESS", "true")
    env.setdefault("STREAMLIT_BROWSER_GATHER_USAGE_STATS", "false")
    env.setdefault("STREAMLIT_GLOBAL_DEVELOPMENT_MODE", "false")

    cmd = [
        sys.executable,
        "-m", "streamlit", "run", str(APP_ENTRY),
        "--server.headless=true",
        f"--server.port={STREAMLIT_PORT}",
        f"--server.address={STREAMLIT_HOST}",
        "--browser.gatherUsageStats=false",
        "--server.runOnSave=false",
        "--server.fileWatcherType=none",
    ]

    print(f"\n[conftest] 启动 Streamlit: {' '.join(cmd)}")
    proc = subprocess.Popen(
        cmd,
        cwd=str(PROJECT_ROOT),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )

    try:
        if not _wait_for_port(STREAMLIT_HOST, STREAMLIT_PORT, SERVER_STARTUP_TIMEOUT):
            try:
                proc.terminate()
                out, _ = proc.communicate(timeout=3)
                print("[conftest] Streamlit 启动失败, 日志:\n" + (out or ""), file=sys.stderr)
            except Exception:
                pass
            pytest.fail(f"Streamlit 端口 {STREAMLIT_PORT} 在 {SERVER_STARTUP_TIMEOUT}s 内未监听")

        if not _wait_for_streamlit_ready(STREAMLIT_BASE_URL, 30):
            pytest.fail(f"Streamlit HTTP 在 30s 内未返回 200")

        print(f"[conftest] Streamlit 已就绪: {STREAMLIT_BASE_URL}")
        yield STREAMLIT_BASE_URL
    finally:
        print(f"[conftest] 关闭 Streamlit (PID={proc.pid})")
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)


# ────────────────────────────────────────────────────────────
# 浏览器 / 页面 fixture
# ────────────────────────────────────────────────────────────
@pytest.fixture(autouse=True)
def _configure_browser_context(page: Page) -> Generator[Page, None, None]:
    """每个测试开始前: 设置视口。"""
    page.set_viewport_size({"width": 1440, "height": 900})
    yield page


@pytest.fixture
def app_url(streamlit_server: str) -> str:
    """Streamlit server 的 base URL, 暴露给测试用。"""
    return streamlit_server


# ────────────────────────────────────────────────────────────
# pytest_runtest_makereport hook: 截图 + 标记 rep_call
# ────────────────────────────────────────────────────────────
def _safe_test_id(nodeid: str) -> str:
    return nodeid.replace("/", "_").replace("::", "__").replace(".py", "")


@pytest.hookimpl(tryfirst=True, hookwrapper=True)
def pytest_runtest_makereport(item, call):
    """合并实现:
    1) 标记 item.rep_<when> 让 _trace_on_failure fixture 能读到成败
    2) 用例执行后 (call 阶段) 截屏, 附加到 pytest-html 报告
    """
    outcome = yield
    report = outcome.get_result()
    setattr(item, f"rep_{report.when}", report)

    if report.when != "call":
        return

    page = item.funcargs.get("page")
    if page is None:
        return

    test_id = _safe_test_id(item.nodeid)
    status = "passed" if report.passed else "failed"
    screenshot_dir = PROJECT_ROOT / "tests" / "e2e" / "reports" / "screenshots"
    screenshot_dir.mkdir(parents=True, exist_ok=True)
    shot_path = screenshot_dir / f"{test_id}__{status}.png"

    try:
        page.screenshot(path=str(shot_path), full_page=True)
    except Exception as e:
        print(f"[conftest] 截图失败: {e}", file=sys.stderr)
        return

    if hasattr(report, "extra"):
        from pytest_html import extras
        report.extra.append(extras.png(str(shot_path.absolute())))


# ────────────────────────────────────────────────────────────
# trace 录制: 失败用例自动留 trace
# ────────────────────────────────────────────────────────────
@pytest.fixture(autouse=True)
def _trace_on_failure(request, page: Page):
    """对每个用例开启 trace, 失败时保存到 reports/traces/。
    成功用例不保留, 节省空间。"""
    trace_dir = PROJECT_ROOT / "tests" / "e2e" / "reports" / "traces"
    trace_dir.mkdir(parents=True, exist_ok=True)

    context = page.context
    context.tracing.start(screenshots=True, snapshots=True, sources=False)
    yield
    rep_call = getattr(request.node, "rep_call", None)
    if rep_call and rep_call.failed:
        test_id = _safe_test_id(request.node.nodeid)
        trace_path = trace_dir / f"{test_id}.zip"
        context.tracing.stop(path=str(trace_path))
    else:
        context.tracing.stop()
