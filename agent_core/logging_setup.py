"""
统一日志配置 —— 控制台 + 按小时切分的本地文件。

从 web/app.py 抽出,便于单测。用法:

    from agent_core.logging_setup import setup_logging
    setup_logging(level=logging.INFO, log_dir=PROJECT_ROOT / "logs" / "app")

行为:
- 控制台 StreamHandler(短时间戳)
- 可选 TimedRotatingFileHandler(when="H"):活动文件 logs/app/agent.log,
  跨小时自动切走,旧文件后缀 agent.log.YYYY-MM-DD_HH;保留 backupCount 小时。
- 幂等:Streamlit 每次 rerun 都会重跑模块顶层代码,靠 handler tag 去重,
  不会重复 addHandler。
"""
from __future__ import annotations

import logging
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

# Streamlit rerun 时用来识别"已经加过文件 handler",避免重复堆叠
_FILE_HANDLER_TAG = "_agent_app_file_handler"

# 第三方库在 DEBUG 模式下刷屏,统一降到 WARNING
_NOISY_LIBS = ("httpx", "httpcore", "urllib3", "openai", "anthropic",
               "watchdog", "git")

_FILE_FMT = logging.Formatter(
    "[%(asctime)s] [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
_CONSOLE_FMT = logging.Formatter(
    "[%(asctime)s] [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)


def setup_logging(
    level: int = logging.INFO,
    log_dir=None,
    *,
    console: bool = True,
    backup_count: int = 168,
) -> logging.Logger:
    """配置 root logger:控制台 + 可选 TimedRotating 文件(按小时切分)。

    Args:
        level: root 日志级别。
        log_dir: 文件日志目录;None 则不写文件(仅控制台)。
        console: 是否加控制台 handler。
        backup_count: 保留多少个历史小时文件(默认 168 = 7 天)。

    Returns:
        配置好的 root logger。
    """
    root = logging.getLogger()
    root.setLevel(level)

    # 控制台 handler(幂等:已有非文件的 StreamHandler 则不重复加)
    if console and not any(
        isinstance(h, logging.StreamHandler)
        and not isinstance(h, logging.FileHandler)
        for h in root.handlers
    ):
        sh = logging.StreamHandler()
        sh.setFormatter(_CONSOLE_FMT)
        root.addHandler(sh)

    # 文件 handler(幂等:靠 tag 识别,Streamlit rerun 不重复加)
    if log_dir is not None and not any(
        getattr(h, _FILE_HANDLER_TAG, False) for h in root.handlers
    ):
        log_dir = Path(log_dir)
        log_dir.mkdir(parents=True, exist_ok=True)
        fh = TimedRotatingFileHandler(
            log_dir / "agent.log",
            when="H",
            interval=1,
            backupCount=backup_count,
            encoding="utf-8",
        )
        fh.suffix = "%Y-%m-%d_%H"
        fh.setFormatter(_FILE_FMT)
        setattr(fh, _FILE_HANDLER_TAG, True)
        root.addHandler(fh)

    # DEBUG 模式静音第三方库
    if level <= logging.DEBUG:
        for name in _NOISY_LIBS:
            logging.getLogger(name).setLevel(logging.WARNING)

    return root
