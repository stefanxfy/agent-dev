#!/usr/bin/env bash
# ────────────────────────────────────────────────────────────
# E2E 测试一键运行脚本
#
# 功能:
#   1. 校验依赖 (playwright / pytest)
#   2. 自动安装 chromium (如果还没装)
#   3. 启动 streamlit (conftest 自动管理) 并跑测试
#   4. 出 HTML 报告 + 自动打开
#
# 用法:
#   ./scripts/run_e2e.sh                 # 跑全部, 出报告
#   ./scripts/run_e2e.sh --headed        # 看浏览器
#   ./scripts/run_e2e.sh -k test_chat    # 跑名字含 test_chat 的用例
#   ./scripts/run_e2e.sh -m smoke        # 跑冒烟
#   ./scripts/run_e2e.sh --no-open       # 跑完不自动打开报告
#   ./scripts/run_e2e.sh --keep-server   # 跑完不 kill streamlit (调试用)
# ────────────────────────────────────────────────────────────

set -euo pipefail

# ─── 路径 ──────────────────────────────────────────────────
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
PROJECT_ROOT="$( cd "$SCRIPT_DIR/.." && pwd )"
E2E_DIR="$PROJECT_ROOT/tests/e2e"
REPORT_DIR="$E2E_DIR/reports"
REPORT_HTML="$REPORT_DIR/report.html"

# ─── 参数解析 ──────────────────────────────────────────────
PYTEST_ARGS=()
OPEN_REPORT=1
KEEP_SERVER=0
HELP=0

while [[ $# -gt 0 ]]; do
    case "$1" in
        --headed)        PYTEST_ARGS+=("--headed") ;;
        --slowmo)        PYTEST_ARGS+=("--slowmo=$2"); shift ;;
        --no-open)       OPEN_REPORT=0 ;;
        --keep-server)   KEEP_SERVER=1 ;;
        -h|--help)       HELP=1 ;;
        *)               PYTEST_ARGS+=("$1") ;;
    esac
    shift
done

if [[ $HELP -eq 1 ]]; then
    sed -n '2,18p' "$0"
    exit 0
fi

# ─── 依赖检查 ──────────────────────────────────────────────
echo "🔍 检查 Python 依赖..."
PY3="${PYTHON:-python3}"
if ! $PY3 -c "import playwright, pytest, pytest_html, pytest_playwright" 2>/dev/null; then
    echo "❌ 缺包, 正在安装..."
    $PY3 -m pip install --user --break-system-packages playwright pytest-playwright pytest-html
fi
echo "✅ 依赖 OK"

# ─── 浏览器检查 ─────────────────────────────────────────────
echo "🔍 检查浏览器..."
# 优先用系统 Chrome (快, 国内网络免下载), 没装再退而求其次下载 chromium
if [ -d "/Applications/Google Chrome.app" ]; then
    echo "✅ 用系统 Chrome: /Applications/Google Chrome.app"
elif $PY3 -c "from playwright.sync_api import sync_playwright
with sync_playwright() as p:
    b = p.chromium.launch(); b.close()" 2>/dev/null; then
    echo "✅ 用 Playwright 内置 Chromium"
else
    echo "❌ 无可用浏览器, 下载中 (~150MB, 国内可能慢)..."
    $PY3 -m playwright install chromium
fi

# ─── 准备报告目录 ──────────────────────────────────────────
mkdir -p "$REPORT_DIR/screenshots" "$REPORT_DIR/traces"

# ─── 跑测试 ────────────────────────────────────────────────
echo ""
echo "🚀 启动 E2E 测试..."
echo "   项目: $PROJECT_ROOT"
echo "   入口: $E2E_DIR"
echo "   参数: ${PYTEST_ARGS[*]:-默认}"
echo ""

cd "$E2E_DIR"

# 拼装 pytest 命令 (用 string 拼接避免 zsh 下空数组 unbound 问题)
PYTEST_CMD="$PY3 -m pytest"
if [[ ${#PYTEST_ARGS[@]} -gt 0 ]]; then
    for arg in "${PYTEST_ARGS[@]}"; do
        PYTEST_CMD="$PYTEST_CMD $arg"
    done
fi
PYTEST_CMD="$PYTEST_CMD --html=$REPORT_HTML --self-contained-html"

# 保留 streamlit server 用于调试
if [[ $KEEP_SERVER -eq 1 ]]; then
    STREAMLIT_SKIP_SERVER=1 \
    nohup $PY3 -m streamlit run "$PROJECT_ROOT/web/app.py" \
        --server.headless=true \
        --server.port=8501 \
        --server.address=127.0.0.1 \
        --browser.gatherUsageStats=false \
        > "$REPORT_DIR/streamlit.log" 2>&1 &
    STREAMLIT_PID=$!
    echo "   Streamlit PID=$STREAMLIT_PID (log: $REPORT_DIR/streamlit.log)"
    sleep 8  # 等待 streamlit 起来
    trap "kill $STREAMLIT_PID 2>/dev/null || true" EXIT
fi

set +e
eval "$PYTEST_CMD"
TEST_EXIT=$?
set -e

# ─── 报告汇总 ──────────────────────────────────────────────
echo ""
echo "════════════════════════════════════════════════"
echo "📊 测试报告"
echo "════════════════════════════════════════════════"
echo "  HTML 报告:   $REPORT_HTML"
echo "  截图目录:   $REPORT_DIR/screenshots/"
echo "  Trace 目录: $REPORT_DIR/traces/  (失败用例)"
echo ""

# 统计截图数量
SCREENSHOTS=$(ls "$REPORT_DIR/screenshots/" 2>/dev/null | wc -l | tr -d ' ')
TRACES=$(ls "$REPORT_DIR/traces/" 2>/dev/null | wc -l | tr -d ' ')
echo "  截图: $SCREENSHOTS 张 | Trace: $TRACES 个"
echo ""

if [[ $OPEN_REPORT -eq 1 ]] && [[ -f "$REPORT_HTML" ]]; then
    echo "🌐 打开报告..."
    open "$REPORT_HTML" 2>/dev/null || xdg-open "$REPORT_HTML" 2>/dev/null || \
        echo "   (无法自动打开, 请手动打开: $REPORT_HTML)"
fi

exit $TEST_EXIT
