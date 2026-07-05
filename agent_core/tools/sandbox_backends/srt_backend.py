"""
SrtBackend:调用 Anthropic 官方 srt 二进制(@anthropic-ai/sandbox-runtime)。

设计依据:docs/tool/sandbox-pluggable-design.md §5.3

CLI 接口(实测确认,非假设 —— 避免重蹈原代码 127 bug):
    srt -s <config.json> -c "<command>"

config schema(实测确认):
    {
      "filesystem": {"allowWrite": [...], "denyWrite": [...],
                      "allowRead": [...],  "denyRead": [...]},
      "network":    {"allowedDomains": [...], "deniedDomains": [...]}  ← Domains,不是 Hosts
    }

安装:srt 不一定预装。用户可选:
    - npm i -g @anthropic-ai/sandbox-runtime
    - 或从 github.com/anthropic-experimental/sandbox-runtime Releases 下预编译二进制
is_available() = which srt 或 settings 的 backends.srt.binaryPath。
"""

from __future__ import annotations

import json
import logging
import os
import shlex
import shutil
import tempfile
from typing import Optional

from ._cleanup import get_sandbox_tmp_dir, run_default_cleanup
from .base import SandboxRuntimeConfig

logger = logging.getLogger(__name__)
sandbox_logger = logging.getLogger("agent_core.sandbox")


class SrtBackend:
    """
    srt 二进制 backend(对齐 CC 同款 sandbox-runtime 语义)。

    与 NativeBackend 的区别:依赖外部 srt 二进制,但语义与 CC 完全一致(同款包),
    适合"想对齐 CC 行为"的场景。
    """

    name = "srt"

    def __init__(self, config: Optional[dict] = None):
        # backend 私有配置(来自 settings.json 的 backends.srt 段)
        self._config = config or {}
        self._binary_path: Optional[str] = self._config.get("binaryPath")
        self._cfg_file: Optional[str] = None  # lazy init(_ensure_cfg_file)
        self._available_cache: Optional[bool] = None  # is_available 缓存

    # ── is_available + initialize ──

    def is_available(self) -> bool:
        """srt 二进制存在。结果缓存(避免每次 shutil.which)。"""
        if self._available_cache is None:
            self._available_cache = self._resolve_binary() is not None
        return self._available_cache

    def initialize(self) -> None:
        # srt 无需 preflight(二进制存在即可,实际初始化在每次 -s 加载 config)
        pass

    def _resolve_binary(self) -> Optional[str]:
        """返回 srt 可执行路径,不存在返 None。"""
        if self._binary_path:
            # 用户指定路径,校验可执行
            return self._binary_path if os.access(self._binary_path, os.X_OK) else None
        return shutil.which("srt")

    # ── wrap ──

    def wrap(
        self,
        command: str,
        cfg: SandboxRuntimeConfig,
        working_dir: str,
    ) -> str:
        binary = self._resolve_binary()
        if not binary:
            sandbox_logger.warning(
                "⚠️ [srt_wrap_unavailable] srt binary not found → passthrough"
            )
            return command

        # 写 config 文件(覆盖写,进程内单例;sandbox_tmp 24h 清理兜底)
        cfg_path = self._ensure_cfg_file()
        srt_cfg = self._cfg_to_srt_json(cfg, working_dir)
        try:
            with open(cfg_path, "w") as f:
                json.dump(srt_cfg, f)
        except OSError as e:
            logger.warning("srt config 写入失败,降级 passthrough: %s", e)
            return command

        # srt -s <config> -c "<command>"
        wrapped = (
            f"{shlex.quote(binary)} "
            f"-s {shlex.quote(cfg_path)} "
            f"-c {shlex.quote(command)}"
        )
        sandbox_logger.info(
            "⚙️ [srt_wrap] cmd_preview=%s cfg_keys=%s",
            command[:60],
            list(srt_cfg.keys()),
        )
        return wrapped

    # ── translator(私有,实测 schema)──

    def _cfg_to_srt_json(
        self,
        cfg: SandboxRuntimeConfig,
        working_dir: str,
    ) -> dict:
        """
        SandboxRuntimeConfig → srt config dict(实测确认的 schema)。

        关键:network 用 allowedDomains/deniedDomains(实测,非 allowedHosts/deniedHosts)。
        """
        cwd = os.path.abspath(working_dir)
        tmp = get_sandbox_tmp_dir()
        return {
            "filesystem": {
                "allowWrite": [cwd, tmp] + list(cfg.fs_allow_write),
                "denyWrite": list(cfg.fs_deny_write),
                "allowRead": list(cfg.fs_allow_read),
                "denyRead": list(cfg.fs_deny_read),
            },
            "network": {
                # 实测:srt 用 Domains 不是 Hosts(原代码 allowedHosts 是错的)
                "allowedDomains": list(cfg.net_allowed),
                "deniedDomains": list(cfg.net_denied),
            },
        }

    # ── config 文件管理 ──

    def _ensure_cfg_file(self) -> str:
        """
        进程内单例 config 文件路径(lazy init)。

        放在 sandbox_tmp_dir(有 24h mtime 清理兜底);cleanup() 每次执行后也会删。
        并发限制:进程内单文件,多命令并发会互相覆盖 —— MVP 接受(agent 串行跑 bash,
        非高频并发),设计 §13 风险 5 已标注。
        """
        if self._cfg_file and os.path.exists(self._cfg_file):
            return self._cfg_file
        tmp_dir = get_sandbox_tmp_dir()
        fd, path = tempfile.mkstemp(prefix="srt-cfg-", suffix=".json", dir=tmp_dir)
        os.close(fd)
        self._cfg_file = path
        return path

    # ── cleanup ──

    def cleanup(self) -> None:
        # 共享清理由 SandboxManager.cleanup_after_command 统一调;
        # 本方法只清 backend 特定(config 文件,避免残留)。
        if self._cfg_file and os.path.exists(self._cfg_file):
            try:
                os.remove(self._cfg_file)
                self._cfg_file = None
            except OSError as e:
                logger.warning("srt config 文件清理失败: %s", e)
