"""
NativeBackend:直接调 OS 原生沙箱(macOS sandbox-exec / Linux bwrap)。

设计依据:docs/tool/sandbox-pluggable-design.md §5.3

特点:
- 不依赖任何外部 npm/二进制包(除 OS 自带),契合 Python 架构
- 平台检测 + 依赖检测从 SandboxManager 下沉至此(设计 §10,UseCase 层不再知
  "macOS Seatbelt 内建"这种细节)
- 可真集成测试(设计 §9):直接调 sandbox-exec/bwrap 能在 CI 真跑

MVP 限制(设计 §13 风险 1/2):
- macOS 网络限制:Seatbelt host 级限制需 socat,本 MVP 网络全开,留 TODO
- Linux deny_write:bwrap 基于 bind mount,无"deny"概念;deny_write 路径不 bind 即拒
"""

from __future__ import annotations

import logging
import os
import shlex
import shutil
import sys
from typing import Optional

from ._cleanup import get_sandbox_tmp_dir, run_default_cleanup
from .base import SandboxRuntimeConfig

logger = logging.getLogger(__name__)
sandbox_logger = logging.getLogger("agent_core.sandbox")


class NativeBackend:
    """
    原生 OS 沙箱 backend。

    macOS → sandbox-exec(Seatbelt,内建)
    Linux → bwrap(bubblewrap,需 apt install bubblewrap)

    平台/依赖检测(_is_supported_platform / _check_dependencies)从 SandboxManager 下沉。
    """

    name = "native"

    def __init__(self, config: Optional[dict] = None):
        # backend 私有配置(来自 settings.json 的 backends.native 段,预留扩展)
        self._config = config or {}
        self._initialized = False
        self._available_cache: Optional[bool] = None  # is_available 缓存(避免每次 shutil.which)

    # ── is_available + initialize(平台/依赖检测,从 SandboxManager 下沉)──

    def is_available(self) -> bool:
        """
        平台支持 + 二进制存在。结果缓存(首次 shutil.which 后不再重复查 PATH,
        避免 1000+ 次 engine 调用反复查 PATH 的性能问题)。

        macOS:sandbox-exec(/usr/bin/sandbox-exec,Seatbelt 内建)
        Linux-WSL2:bwrap
        其他:False
        """
        if self._available_cache is None:
            self._available_cache = (
                self._is_supported_platform() and self._check_dependencies()
            )
        return self._available_cache

    def initialize(self) -> None:
        """preflight;errors 非空则 raise(由 SandboxManager 决定 fail-open/closed)。"""
        if self._initialized:
            return
        deps = self._check_dependencies_detailed()
        if deps["errors"]:
            raise RuntimeError(f"NativeBackend 依赖缺失: {deps['errors']}")
        for w in deps["warnings"]:
            logger.warning("NativeBackend warning: %s", w)
        self._initialized = True

    def _is_supported_platform(self) -> bool:
        """对齐 CC isSupportedPlatform。"""
        if sys.platform == "darwin":
            return True  # macOS Seatbelt 内建
        if sys.platform.startswith("linux"):
            try:
                with open("/proc/version") as f:
                    return "microsoft" in f.read().lower()  # WSL2
            except (FileNotFoundError, OSError):
                return False
        return False

    def _check_dependencies(self) -> bool:
        """
        快速路径:只判二进制存在。

        偏差说明:原 sandbox_manager.py macOS 直接 return True(假设 Seatbelt 内建)。
        本实现改用 shutil.which("sandbox-exec") 更稳妥(deprecated 不代表一定在),
        /usr/bin/sandbox-exec 在所有现行 macOS 都存在,which 能找到。
        """
        if sys.platform == "darwin":
            return shutil.which("sandbox-exec") is not None
        return shutil.which("bwrap") is not None

    def _check_dependencies_detailed(self) -> dict:
        """对齐 CC checkDependencies — 返回 {errors, warnings}。"""
        errors: list[str] = []
        warnings: list[str] = []
        if sys.platform == "darwin":
            if not shutil.which("sandbox-exec"):
                errors.append("sandbox-exec 未找到(macOS 应内建于 /usr/bin/sandbox-exec)")
        elif sys.platform.startswith("linux"):
            if not shutil.which("bwrap"):
                errors.append("bubblewrap (bwrap) 未安装:apt install bubblewrap")
            if not shutil.which("socat"):
                warnings.append("socat 未安装:网络隔离 host 级限制需要(MVP 未用)")
        else:
            errors.append(f"不支持的平台: {sys.platform}")
        return {"errors": errors, "warnings": warnings}

    # ── wrap ──

    def wrap(
        self,
        command: str,
        cfg: SandboxRuntimeConfig,
        working_dir: str,
    ) -> str:
        """平台分发:macOS → Seatbelt,Linux → bwrap。返回给 subprocess(shell=True) 的命令字符串。"""
        if not self.is_available():
            # 防御性:契约保证 is_available()=False 时不被调,但若被调则安全降级
            sandbox_logger.warning(
                "⚠️ [native_wrap_unavailable] platform/deps missing → passthrough"
            )
            return command

        if sys.platform == "darwin":
            return self._wrap_seatbelt(command, cfg, working_dir)
        return self._wrap_bwrap(command, cfg, working_dir)

    def _wrap_seatbelt(
        self,
        command: str,
        cfg: SandboxRuntimeConfig,
        working_dir: str,
    ) -> str:
        profile = self._cfg_to_seatbelt(cfg, working_dir)
        # sandbox-exec -p '<profile>' /bin/sh -c '<command>'
        # /bin/sh -c 让用户命令(含管道/重定向)在 profile 约束内由 sh 解释执行
        wrapped = (
            f"sandbox-exec -p {shlex.quote(profile)} "
            f"/bin/sh -c {shlex.quote(command)}"
        )
        sandbox_logger.info(
            "⚙️ [native_seatbelt_wrap] cmd_preview=%s profile_lines=%d",
            command[:60],
            len(profile.splitlines()),
        )
        return wrapped

    def _wrap_bwrap(
        self,
        command: str,
        cfg: SandboxRuntimeConfig,
        working_dir: str,
    ) -> str:
        argv = self._cfg_to_bwrap_argv(command, cfg, working_dir)
        wrapped = shlex.join(argv)
        sandbox_logger.info(
            "⚙️ [native_bwrap_wrap] cmd_preview=%s argv_len=%d",
            command[:60],
            len(argv),
        )
        return wrapped

    # ── translators(私有,设计 §5.1 决策 B:翻译属于 backend,不属于 config)──

    def _cfg_to_seatbelt(
        self,
        cfg: SandboxRuntimeConfig,
        working_dir: str,
    ) -> str:
        """
        生成 macOS Seatbelt .sb profile(MVP 基础版)。

        策略(对齐设计 §13 风险 1):
        - (deny default) 兜底拒绝
        - 允许进程/信号/sysctl 读
        - 读:默认放开 file-read-data/metadata(CC 也基本放开读,收紧成本高);
          再叠加 fs_allow_read / fs_deny_read 显式规则
        - 写:默认拒(deny default),显式 allow cwd + sandbox_tmp + fs_allow_write;
          fs_deny_write 叠加显式 deny(Seatbelt 中更具体的 subpath 规则优先)
        - 网络:全开(MVP;host 级限制需 socat,留 TODO)
        """
        cwd = os.path.abspath(working_dir)
        tmp = get_sandbox_tmp_dir()
        # 注:只保留确定可用的 Seatbelt operation(process/file-read*/file-write*/network)。
        # 早期尝试的 signal*/sysctl-read/file-read-data 等在现代 sandbox-exec 报
        # "unbound variable"(exit 65),故用 file-read* 通配替代细分读操作。
        lines = [
            "(version 1)",
            "(deny default)",
            "(allow process*)",
            "(allow file-read*)",  # 通配所有读(CC 也基本放开读)
            # 常见 device 例外(命令输出 sink / 随机源)。subpath 对单文件亦匹配。
            '(allow file-write* (subpath "/dev/null"))',
            '(allow file-read* (subpath "/dev/urandom"))',
            '(allow file-read* (subpath "/dev/random"))',
        ]
        # 显式 allow write(更具体的 subpath 优先于 deny default)
        # 注:额外加白 TMPDIR 父目录 + /tmp(/private/tmp):mktemp / 多数 CLI 默认写 tmp,
        # 不加白会 Operation not permitted(2026-07-04 用户反馈)。这是 profile 完备性
        # 持续工程的一部分(设计 §13 风险 1);MVP 安全权衡:网络全开已"穿透",tmp 写放宽
        # 风险可控(MVP 无文件逃逸到沙箱外的路径,deny default 仍兜底)。
        tmpdir_parent = os.path.dirname(tmp)
        for p in (
            [cwd, tmp, tmpdir_parent, "/tmp", "/private/tmp"]
            + list(cfg.fs_allow_write)
        ):
            if p:
                rp = os.path.realpath(p)
                lines.append(f'(allow file-write* (subpath "{rp}"))')
        # 显式 deny write(更具体,优先于 allow)
        for p in cfg.fs_deny_write:
            if p:
                rp = os.path.realpath(p)
                lines.append(f'(deny file-write* (subpath "{rp}"))')
        # 读规则(fs_allow_read / fs_deny_read 叠加)
        for p in cfg.fs_allow_read:
            if p:
                lines.append(f'(allow file-read* (subpath "{os.path.realpath(p)}"))')
        for p in cfg.fs_deny_read:
            if p:
                lines.append(f'(deny file-read* (subpath "{os.path.realpath(p)}"))')
        # 网络(MVP: 全开)
        lines.append("(allow network*)")
        return "\n".join(lines)

    def _cfg_to_bwrap_argv(
        self,
        command: str,
        cfg: SandboxRuntimeConfig,
        working_dir: str,
    ) -> list[str]:
        """
        生成 Linux bwrap argv(MVP 基础版)。

        策略(对齐设计 §13 风险 2):
        - --unshare-net:默认隔离网络(host 白名单需 socat,超出 MVP)
        - --ro-bind 基础系统目录(/usr /lib /bin /etc 等)
        - --bind cwd + sandbox_tmp + fs_allow_write(可写)
        - --ro-bind fs_allow_read(只读)
        - deny_write:bwrap 无 deny 概念;不 bind 即拒访问(MVP 简化,不处理 deny_write 覆盖)
        - glob 限制:bwrap 不支持 * / ? / [,启动时由 SandboxManager 警告(对齐 CC)
        """
        cwd = os.path.abspath(working_dir)
        tmp = get_sandbox_tmp_dir()
        argv = ["bwrap", "--unshare-net", "--die-with-parent"]
        for sysdir in ["/usr", "/lib", "/lib64", "/bin", "/sbin", "/etc", "/run"]:
            if os.path.exists(sysdir):
                argv += ["--ro-bind", sysdir, sysdir]
        argv += ["--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp"]
        # 可写路径
        for p in [cwd, tmp] + list(cfg.fs_allow_write):
            if p and os.path.exists(p):
                argv += ["--bind", p, p]
        # 只读路径
        for p in cfg.fs_allow_read:
            if p and os.path.exists(p):
                argv += ["--ro-bind", p, p]
        argv += ["--", "sh", "-c", command]
        return argv

    # ── cleanup(共享实现,设计 §10)──

    def cleanup(self) -> None:
        # 共享清理(bare-git scrub + tmp 过期)由 SandboxManager.cleanup_after_command
        # 统一调 run_default_cleanup,backend.cleanup 只做 backend 特定清理。
        # NativeBackend 无特定清理。
        pass
