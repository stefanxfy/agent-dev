# E2E 测试 (Playwright + Streamlit)

针对 Streamlit Web UI 的端到端回归测试, 基于 [Playwright](https://playwright.dev/python/) Python SDK。

## 测试范围

| 文件 | 覆盖 |
|---|---|
| `test_01_home_loads.py` | 主页加载、侧边栏、多页导航 |
| `test_02_chat_page.py` | Chat 页面 UI (输入框、Provider 选择、会话控制) |
| `test_03_session_management.py` | Session 管理页面 (创建按钮、tabs) |
| `test_04_candidate_review.py` | Candidate Review 页面 (标题、侧边栏导航) |

## 安装

```bash
# 一次性安装 (3 步)
python3 -m pip install --user --break-system-packages playwright pytest-playwright pytest-html
python3 -m playwright install chromium

# 验证
python3 -c "from playwright.sync_api import sync_playwright; print('ok')"
```

> 你的 Python 3.11 由 uv 管理, 必须加 `--break-system-packages --user`。

## 运行

```bash
cd /Users/fanyunxu/Desktop/myproject/agent-dev/tests/e2e

# 跑全部 (headless, 约 60s)
pytest

# 跑单个文件
pytest test_02_chat_page.py

# 跑单个用例
pytest test_02_chat_page.py::test_chat_input_visible

# 看浏览器 (本地调试)
pytest --headed

# 慢动作 + 显示浏览器 (录制用)
pytest --headed --slowmo=500

# 用本机 Chrome 替代 chromium
pytest --browser-channel=chrome

# 只跑冒烟
pytest -m smoke

# 只跑回归
pytest -m regression

# 详细输出 + 打印 print()
pytest -s

# 出 HTML 报告
pytest --html=reports/report.html --self-contained-html
```

## 报告

- **HTML 报告**: `reports/report.html` (用浏览器打开)
- **截图**: 每个用例截一张全屏到 `reports/screenshots/`
- **失败 trace**: 失败用例自动录 trace, 在 HTML 报告里可点击 "Trace" 按钮回放每一步

## 调试技巧

```bash
# 1) 让 streamlit 不被 fixture 自动起, 手动起便于 attach
STREAMLIT_SKIP_SERVER=1 streamlit run web/app.py &
pytest --headed --slowmo=300

# 2) 用 Playwright Inspector 录操作
PWDEBUG=1 pytest test_02_chat_page.py::test_chat_input_visible

# 3) 录制 codegen 生成新脚本
python3 -m playwright codegen http://localhost:8501

# 4) 单独跑某个用例并保留浏览器
pytest --headed --slowmo=500 -k test_chat_input_visible
```

## 添加新用例

1. 在 `pages/` 下新增 Page Object (继承 `BasePage`)
2. 写 `test_XX_*.py` 文件, 用 `@pytest.mark.regression` 标注
3. 复杂场景加 `@pytest.mark.slow` 标记
4. 关键步骤加 `screenshot("name")` 留证据

## 与现有 unit test 的关系

- `tests/` 根目录: 30+ 单元/集成测试 (pytest), 跑 Python 函数, 毫秒级
- `tests/e2e/`: 浏览器端到端测试, 跑 Streamlit UI, 秒级, 需要 streamlit 进程

两者不冲突, 互为补充:
```bash
# 跑全部测试
cd /Users/fanyunxu/Desktop/myproject/agent-dev
pytest tests/ -q                 # 单元测试
pytest tests/e2e/ -q             # E2E 测试
```
