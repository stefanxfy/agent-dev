#!/bin/bash
# 启用 DEBUG 日志跑 Streamlit，能清晰看到压缩过程
#
# 用法:
#   bash scripts/run_with_debug.sh              # 启用 context.compact DEBUG
#   bash scripts/run_with_debug.sh agent_core   # 启用整 agent_core DEBUG
#
# DEBUG 输出位置:
#   - 终端: 看到 INFO 级（Compact START/DONE）
#   - 终端: DEBUG 级（LLM 原始输出、Extract 路径、preserved head 内容）
#   - 终端: WARNING 级（PTL 重试）
#   - 终端: ERROR 级（Compact 失败）

set -e

# 决定要开启 DEBUG 的 logger
if [ "$1" = "agent_core" ]; then
    # 整个 agent_core 全 DEBUG
    export PYTHONUNBUFFERED=1
    exec python3 -m streamlit run web/app.py \
        --logger.level=debug \
        --browser.gatherUsageStats=false \
        2>&1 | grep -E "context\.compact|agent_core" --color=always
else
    # 只开启 context.compact（推荐）
    export PYTHONUNBUFFERED=1
    exec python3 -c "
import logging
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    datefmt='%H:%M:%S',
)
# 重点关注 context.compact
logging.getLogger('context.compact').setLevel(logging.DEBUG)
# 安静其他 logger
logging.getLogger('httpx').setLevel(logging.WARNING)
logging.getLogger('httpcore').setLevel(logging.WARNING)
logging.getLogger('urllib3').setLevel(logging.WARNING)
logging.getLogger('openai').setLevel(logging.WARNING)

# 启动 streamlit
import sys
sys.argv = ['streamlit', 'run', 'web/app.py']
from streamlit.web import cli as stcli
stcli.main()
" 2>&1 | grep --color=always -E "Compact|LLM Call|Extract|Build|preserved|PTL|🔧|🤖|🏷️|🏗️|🥝|🔍|❌|⚠️"
fi
