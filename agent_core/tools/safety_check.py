"""
Safety check — 敏感路径 + secret 正则检测

对齐 Claude Code src/utils/permissions/bashSecurity.ts + pathValidation.ts:
- .claude/ / .agent_data/ / .git/ / .ssh/ 等敏感目录 → ask(强制 user 确认)
- API key / SSH key / PEM 私钥等 secret pattern → ask
- Read / Write / Edit tool 的 path 才检查;其他 tool 不检查
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Optional


# ────────────────────────────────────────────────────────────────────
# 🧪 safety 子系统 logger — 可被 AGENT_LOG_SAFETY 单独调级别
# 这是本文件从 0 引入的 logger;safety_check 在主 permission 流程中地位关键
# (Step 1e),但之前完全静默,无任何可观测性
# ────────────────────────────────────────────────────────────────────
safety_logger = logging.getLogger("agent_core.safety")


# ────────────────────────────────────────────────────────────────────
# SENSITIVE_PATH_PATTERNS — 敏感路径前缀
# ────────────────────────────────────────────────────────────────────

SENSITIVE_PATH_PATTERNS: list[str] = [
    ".agent_data/",            # 本项目 settings / sessions / audit.jsonl — 防止 LLM 改自己权限
    ".git/",                   # git 配置 / hooks(防 bare-git scrub attack)
    ".gitconfig",              # git 全局配置
    ".ssh/",                   # SSH private key
    ".ssh/id_rsa",
    ".ssh/id_ed25519",
    ".ssh/known_hosts",
    ".aws/",                   # AWS credentials
    ".aws/credentials",
    ".gcloud/",                # GCP credentials
    ".kube/",                  # K8s config(集群接管)
    ".docker/",                # Docker config(registry token)
    ".netrc",                  # FTP/HTTP credentials
    ".npmrc",                  # npm token
    ".pypirc",                 # pypi token
    ".env",                    # 环境变量文件
    ".env.local",
    ".env.production",
    "id_rsa",                  # 通用 SSH key 文件名
    "id_dsa",
    "id_ecdsa",
    "id_ed25519",
    # 注意:Claude Code 自身的 .claude/ 也算敏感;本项目用 .agent_data/ 替代
    # ".claude/" 已不在列表中,因本项目没这个目录
]

# 让 prefix 匹配兼容绝对路径与相对路径
_SENSITIVE_NORMALIZED = [p.lstrip("/") for p in SENSITIVE_PATH_PATTERNS]


# ────────────────────────────────────────────────────────────────────
# SECRET_PATTERNS — secret 正则(对齐 CC bashSecurity.ts)
# ────────────────────────────────────────────────────────────────────

SECRET_PATTERNS: list[re.Pattern] = [
    # Anthropic API key:sk-ant-xxx
    re.compile(r"sk-ant-(?:api\d+-)?[A-Za-z0-9_\-]{32,}"),
    # OpenAI API key:sk-xxx(proj-xxx, svck-xxx 都算)
    re.compile(r"sk-(?:proj-|svck-)?[A-Za-z0-9_\-]{32,}"),
    # GitHub Personal Access Token:ghp_xxx / gho_xxx / ghu_xxx / ghs_xxx / ghr_xxx
    re.compile(r"gh[pousr]_[A-Za-z0-9]{36,}"),
    # GitHub Fine-Grained PAT:github_pat_xxx
    re.compile(r"github_pat_[A-Za-z0-9_]{82}"),
    # GitHub OAuth / App tokens:gh[f]_[A-Za-z0-9]{36,}
    re.compile(r"gh[f]_[A-Za-z0-9]{36,}"),
    # AWS Access Key:AKIA / ASIA 开头
    re.compile(r"(?:AKIA|ASIA)[A-Z0-9]{16}"),
    # AWS Secret Access Key:40 字符 base64(粗略匹配)
    re.compile(r"(?i)aws.{0,20}['\"][0-9a-zA-Z/+]{40}['\"]"),
    # Google API Key:AIza 开头
    re.compile(r"AIza[0-9A-Za-z_\-]{35}"),
    # Stripe Live Key:sk_live_xxx
    re.compile(r"sk_live_[0-9a-zA-Z]{24,}"),
    # Stripe Test Key:sk_test_xxx
    re.compile(r"sk_test_[0-9a-zA-Z]{24,}"),
    # Slack Bot Token:xoxb-xxx
    re.compile(r"xox[baprs]-[0-9A-Za-z\-]+"),
    # PEM 私钥
    re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |PGP )?PRIVATE KEY-----"),
    # 通用 JWT(eyJ 开头,三段 base64)
    re.compile(r"eyJ[A-Za-z0-9_\-]+\.eyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+"),
    # 邮箱 + 密码(粗糙匹配,避免 FPs 较多所以限定 "password=" 形式)
    re.compile(r"(?i)(?:password|passwd|pwd)\s*[=:]\s*['\"]?[^\s'\"]{8,}"),
]


# ────────────────────────────────────────────────────────────────────
# is_sensitive_path — 路径敏感检测
# ────────────────────────────────────────────────────────────────────

def is_sensitive_path(path: str) -> bool:
    """
    检测路径是否命中敏感前缀(对齐 CC pathValidation.ts + doc §4.7)

    Args:
        path: 文件路径(相对或绝对)

    Returns:
        True 如果路径以 SENSITIVE_PATH_PATTERNS 中任一前缀开头

    Examples:
        >>> is_sensitive_path(".agent_data/settings.json")
        True
        >>> is_sensitive_path("/Users/alice/.ssh/id_rsa")
        True
        >>> is_sensitive_path("./docs/README.md")
        False
    """
    if not path:
        return False

    # 标准化:去掉前导 "./" 和 "/",转为相对路径形式比较
    normalized = path.strip()
    while normalized.startswith("./"):
        normalized = normalized[2:]
    normalized = normalized.lstrip("/")

    safety_logger.debug(
        "🧪 [sensitive_path_check] path=%s normalized=%s prefixes=%d",
        path, normalized, len(_SENSITIVE_NORMALIZED),
    )

    for prefix in _SENSITIVE_NORMALIZED:
        # 1. 完全等于(防止 "./id_rsa" 撞到 "id_rsa" 单独匹配)
        if normalized == prefix.rstrip("/"):
            safety_logger.info(
                "🧪 [sensitive_path_match] path=%s matched_prefix=%s mode=exact",
                path[:120], prefix,
            )
            return True
        # 2. 前缀匹配(目录或文件)
        if normalized.startswith(prefix):
            safety_logger.info(
                "🧪 [sensitive_path_match] path=%s matched_prefix=%s mode=prefix",
                path[:120], prefix,
            )
            return True

    return False


# ────────────────────────────────────────────────────────────────────
# contains_secret — secret 正则检测
# ────────────────────────────────────────────────────────────────────

def contains_secret(text: str) -> bool:
    """
    检测文本是否包含 secret(API key / SSH key / PEM / JWT 等)

    Args:
        text: 待检测文本

    Returns:
        True 如果命中 SECRET_PATTERNS 中任一 pattern
    """
    if not text:
        return False
    safety_logger.debug(
        "🧪 [secret_check] text_len=%d patterns=%d",
        len(text), len(SECRET_PATTERNS),
    )
    for i, pattern in enumerate(SECRET_PATTERNS):
        match = pattern.search(text)
        if match:
            safety_logger.info(
                "🧪 [secret_match] pattern_idx=%d matched=%r text_preview=%s",
                i, match.group(0)[:20], text[:80],
            )
            return True
    return False


# ────────────────────────────────────────────────────────────────────
# safety_check — 顶层检查函数(对齐 doc §4.7)
# ────────────────────────────────────────────────────────────────────

# 哪些 tool 才检查 path(Bash 不在此处,沙箱层处理)
_PATH_CHECK_TOOLS = frozenset({"Read", "Write", "Edit", "MultiEdit", "NotebookEdit"})

# 哪些 tool 检查 tool_input 中所有 string 字段的 secret
_SECRET_CHECK_TOOLS = frozenset({
    "Read", "Write", "Edit", "MultiEdit",  # 文件读写可能含 secret
    "Bash",                                # 命令字符串可能含 secret
    "WebFetch",                            # URL + body 可能含 secret
})


def safety_check(tool_name: str, tool_input: dict) -> bool:
    """
    Safety check 顶层函数(对齐 doc §4.7)

    返回 True 表示需要拦截(进 ask 流程)。
    用于 PermissionEngine 的 step 1e:`safety_check(...) → ASK(SafetyCheckReason)`

    Args:
        tool_name: 工具名
        tool_input: 工具输入参数 dict

    Returns:
        True 如果需要拦截(敏感路径或含 secret)

    Examples:
        >>> safety_check("Read", {"path": ".agent_data/settings.json"})
        True
        >>> safety_check("Read", {"path": "./docs/README.md"})
        False
        >>> safety_check("Write", {"path": "x.py", "content": "sk-ant-xxxxx"})
        True
    """
    if not tool_input:
        safety_logger.debug("🧪 [safety_check_skip] tool=%s empty input", tool_name)
        return False

    safety_logger.debug(
        "🧪 [safety_check_entry] tool=%s input_keys=%s path_check=%s secret_check=%s",
        tool_name, list(tool_input.keys()),
        tool_name in _PATH_CHECK_TOOLS,
        tool_name in _SECRET_CHECK_TOOLS,
    )

    # 1. Path 检查(仅 Read/Write/Edit/MultiEdit/NotebookEdit)
    if tool_name in _PATH_CHECK_TOOLS:
        path = tool_input.get("path") or tool_input.get("file_path") or ""
        if path and is_sensitive_path(path):
            safety_logger.info(
                "🧪 [safety_path_block] tool=%s path=%s",
                tool_name, path[:120],
            )
            return True

    # 2. Secret 检查(在所有 string 字段上跑 regex)
    if tool_name in _SECRET_CHECK_TOOLS:
        for key, value in tool_input.items():
            if isinstance(value, str) and contains_secret(value):
                safety_logger.info(
                    "🧪 [safety_secret_block] tool=%s key=%s value_preview=%s",
                    tool_name, key, value[:80],
                )
                return True
            elif isinstance(value, list):
                # 处理 content list(Anthropic format) — 检查每项
                for item in value:
                    if isinstance(item, dict):
                        # content block dict
                        for v in item.values():
                            if isinstance(v, str) and contains_secret(v):
                                safety_logger.info(
                                    "🧪 [safety_secret_block] tool=%s list_dict value_preview=%s",
                                    tool_name, str(v)[:80],
                                )
                                return True
                    elif isinstance(item, str) and contains_secret(item):
                        safety_logger.info(
                            "🧪 [safety_secret_block] tool=%s list_str value_preview=%s",
                            tool_name, item[:80],
                        )
                        return True
            elif isinstance(value, dict):
                # 嵌套 dict,递归检查 string value
                for v in value.values():
                    if isinstance(v, str) and contains_secret(v):
                        safety_logger.info(
                            "🧪 [safety_secret_block] tool=%s dict value_preview=%s",
                            tool_name, str(v)[:80],
                        )
                        return True

    return False


# ────────────────────────────────────────────────────────────────────
# 工具函数:resolve_path(测试 + 调试用)
# ────────────────────────────────────────────────────────────────────

def normalize_path_for_check(path: str) -> str:
    """
    规范化路径用于敏感检测(去掉 home / cwd 前缀,统一相对路径形式)

    测试和 audit_logger 用 — production 仍走 raw path
    """
    if not path:
        return ""

    normalized = path

    # 把 ~/ 转成相对路径
    home = str(Path.home())
    if normalized.startswith(home):
        normalized = normalized[len(home):].lstrip("/")
    # 把 cwd 转成相对路径
    try:
        cwd = os.getcwd()
        if normalized.startswith(cwd):
            normalized = normalized[len(cwd):].lstrip("/")
    except OSError:
        pass
    # 去掉前导 "./"
    while normalized.startswith("./"):
        normalized = normalized[2:]
    normalized = normalized.lstrip("/")

    return normalized