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
import os
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

# Streamlit rerun 时用来识别"已经加过文件 handler",避免重复堆叠
_FILE_HANDLER_TAG = "_agent_app_file_handler"

# 第三方库在 DEBUG 模式下刷屏,统一降到 WARNING
_NOISY_LIBS = ("httpx", "httpcore", "urllib3", "openai", "anthropic",
               "watchdog", "git")

# ────────────────────────────────────────────────────────────────────
# 子 logger 注册表 — 与 AGENT_LOG_<NAME> 环境变量一一对应
# 默认 DEBUG;设了可降噪(INFO/WARNING/ERROR)
# 作用:把"工具权限 + 安全沙箱"的 6 个子系统拆成独立 logger,
#      `grep -E "🛡️|⚙️|🪝|📋|🧪|🤖" logs/app/agent.log` 可还原一次工具调用全链路
# ────────────────────────────────────────────────────────────────────
_SUB_LOGGER_ENV = (
    ("agent_core.permission", "AGENT_LOG_PERMISSION"),   # 🛡️ permission engine / matcher / bash / loader / denial / fast-path
    ("agent_core.sandbox",    "AGENT_LOG_SANDBOX"),      # ⚙️ sandbox manager / decision / prompt / builtin wrap
    ("agent_core.hook",       "AGENT_LOG_HOOK"),         # 🪝 PreToolUse / PermissionRequest / PermissionDenied
    ("agent_core.audit",      "AGENT_LOG_AUDIT"),        # 📋 audit.jsonl 写盘
    ("agent_core.classifier", "AGENT_LOG_CLASSIFIER"),   # 🤖 Haiku classifier
    ("agent_core.safety",     "AGENT_LOG_SAFETY"),       # 🧪 safety_check / sensitive_path / secret regex
    ("agent_core.skills",     "AGENT_LOG_SKILLS"),       # 🧩 skill discovery / load / snapshot / handler（specs/001-skill-system）
    ("agent_core.mcp",        "AGENT_LOG_MCP"),          # 🔌 mcp client 连接/协商/调用/故障隔离/物化
)

_LEVEL_NAMES = {
    "DEBUG":    logging.DEBUG,
    "INFO":     logging.INFO,
    "WARNING":  logging.WARNING,
    "WARN":     logging.WARNING,
    "ERROR":    logging.ERROR,
    "CRITICAL": logging.CRITICAL,
}


def _apply_env_level(logger_name: str, env_var: str, default: int = logging.DEBUG) -> None:
    """按 AGENT_LOG_<NAME> env var 调整子 logger 级别。默认 DEBUG,设了可降噪。

    子 logger 继承 root 的 handlers,本函数只 setLevel,不重复 addHandler。
    未识别 / 未设置的 env 值 → 默认 DEBUG(对齐"学习项目,日志越详尽越好"的开发规则)。
    """
    raw = os.environ.get(env_var, "").strip().upper()
    level = _LEVEL_NAMES.get(raw, default)
    logging.getLogger(logger_name).setLevel(level)

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

    # 应用 6 个子 logger 的 env 级别覆盖(继承 root 的 handlers,只改 level)
    for sub_name, env_var in _SUB_LOGGER_ENV:
        _apply_env_level(sub_name, env_var)

    return root
