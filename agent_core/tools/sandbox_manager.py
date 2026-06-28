"""
OS 层沙箱管理器(对齐 Claude Code src/utils/sandbox/sandbox-adapter.ts + doc §5.2)

设计:
- 单例 SandboxManager(对齐 CC)— 全进程一份配置 + 初始化状态
- is_sandbox_enabled() 四段短路:config.enabled / 平台支持 / 依赖存在 / 初始化成功
- wrap_with_sandbox(command) 返回 sandbox-runtime wrap-cli 命令字符串
  (ToolRegistry.execute 时通过 subprocess.run(['npx', ...]) 调用,对齐 CC Node.js 子进程路径)
- cleanup_after_command():bare-git scrub(防 #29316)+ sandbox_tmp_dir mtime 过期

底层复用 @anthropic-ai/sandbox-runtime(CC 同款 npm 包),通过 subprocess 调 npx,
不引入 npm 依赖到 requirements.txt。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shlex
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


# ────────────────────────────────────────────────────────────────────
# SandboxConfig — 沙箱配置(对齐 CC SandboxSettingsSchema)
# ────────────────────────────────────────────────────────────────────

@dataclass
class SandboxConfig:
    """
    沙箱配置(对齐 CC SandboxSettingsSchema + doc §5.2)

    字段:
    - enabled: 总开关(False 默认,需 settings.json 显式 opt-in)
    - fail_if_unavailable: 依赖缺失时是否硬退出(对齐 CC failIfUnavailable)
    - auto_allow_bash_if_sandboxed: 沙箱内 bash 自动 allow(对齐 doc §5.4)
    - allow_unsandboxed_commands: 是否允许 dangerously_disable_sandbox 透传
    - network_allowed_domains / network_denied_domains: 网络白/黑名单
    - fs_allow_write / fs_deny_write: 文件系统写白/黑名单
    - fs_allow_read / fs_deny_read: 文件系统读白/黑名单
    - excluded_commands: 排除命令(UX 而非安全,对齐 doc §5.3 注释)
    """
    enabled: bool = False
    fail_if_unavailable: bool = False
    auto_allow_bash_if_sandboxed: bool = True
    allow_unsandboxed_commands: bool = True
    network_allowed_domains: list[str] = field(default_factory=list)
    network_denied_domains: list[str] = field(default_factory=list)
    fs_allow_write: list[str] = field(default_factory=list)
    fs_deny_write: list[str] = field(default_factory=list)
    fs_allow_read: list[str] = field(default_factory=list)
    fs_deny_read: list[str] = field(default_factory=list)
    excluded_commands: list[str] = field(default_factory=list)


# ────────────────────────────────────────────────────────────────────
# SandboxManager — 单例(对齐 CC)
# ────────────────────────────────────────────────────────────────────

class SandboxManager:
    """
    沙箱管理器单例(对齐 CC BaseSandboxManager / sandbox-adapter.ts)

    生命周期:
      1. 进程启动 → __new__ 创建单例,_config=SandboxConfig(),_initialized=False
      2. load_config(settings) → 从 settings.json 更新 _config(可选)
      3. initialize() → 检查依赖,设 _initialized
      4. is_sandbox_enabled() → 4 段短路判断
      5. wrap_with_sandbox(cmd) → 返 wrap-cli 命令字符串
      6. cleanup_after_command() → bare-git scrub + tmp dir 过期
    """

    _instance: Optional["SandboxManager"] = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
            cls._instance._config = SandboxConfig()
        return cls._instance

    # ── 配置加载 ──────────────────────────────────────────────

    def load_config(self, config_dict: Optional[dict]) -> None:
        """
        从 settings.json 的 sandbox 段加载配置(对齐 CC loadSandboxSettings)

        Args:
            config_dict: settings.json 里的 "sandbox" 子 dict,None 则不动
        """
        if config_dict is None:
            return
        # 容忍解析失败 + 部分字段缺失
        try:
            self._config = SandboxConfig(
                enabled=bool(config_dict.get("enabled", self._config.enabled)),
                fail_if_unavailable=bool(
                    config_dict.get("failIfUnavailable", self._config.fail_if_unavailable)
                ),
                auto_allow_bash_if_sandboxed=bool(
                    config_dict.get("autoAllowBashIfSandboxed", self._config.auto_allow_bash_if_sandboxed)
                ),
                allow_unsandboxed_commands=bool(
                    config_dict.get("allowUnsandboxedCommands", self._config.allow_unsandboxed_commands)
                ),
                network_allowed_domains=list(
                    config_dict.get("networkAllowedDomains", self._config.network_allowed_domains)
                ),
                network_denied_domains=list(
                    config_dict.get("networkDeniedDomains", self._config.network_denied_domains)
                ),
                fs_allow_write=list(config_dict.get("fsAllowWrite", self._config.fs_allow_write)),
                fs_deny_write=list(config_dict.get("fsDenyWrite", self._config.fs_deny_write)),
                fs_allow_read=list(config_dict.get("fsAllowRead", self._config.fs_allow_read)),
                fs_deny_read=list(config_dict.get("fsDenyRead", self._config.fs_deny_read)),
                excluded_commands=list(
                    config_dict.get("excludedCommands", self._config.excluded_commands)
                ),
            )
        except Exception as e:
            logger.warning("sandbox config 加载失败,保持默认: %s", e)

    # ── 4 段短路判断 ──────────────────────────────────────────

    def is_sandbox_enabled(self) -> bool:
        """
        沙箱是否启用(对齐 CC isSandboxingEnabled)

        四段短路(全部为 True 才启用):
          1. self._config.enabled
          2. 平台支持(macOS Seatbelt / Linux-WSL2)
          3. 依赖存在(bwrap 或 macOS 内建)
          4. 初始化成功
        """
        if not self._config.enabled:
            return False
        if not self._is_supported_platform():
            logger.warning("当前平台不支持沙箱(macOS/Linux-WSL2 才支持)")
            return False
        if not self._check_dependencies():
            logger.warning("沙箱依赖缺失(bwrap/ripgrep/socat)")
            return False
        if not self._initialized:
            # 未初始化 → 触发一次 initialize(对齐 CC lazy init)
            self.initialize()
        return self._initialized

    # ── 初始化 ────────────────────────────────────────────────

    def initialize(self) -> None:
        """
        初始化沙箱(对齐 CC initialize — 失败 graceful)

        - 检查依赖详细报告
        - errors 非空 → 不初始化;若 fail_if_unavailable → SystemExit(1)
        - warnings 仅 log
        """
        if self._initialized:
            return
        try:
            deps = self._check_dependencies_detailed()
            if deps["errors"]:
                raise RuntimeError(f"沙箱依赖缺失: {deps['errors']}")
            if deps["warnings"]:
                for w in deps["warnings"]:
                    logger.warning("沙箱 warning: %s", w)
            logger.info("✅ 沙箱初始化成功: %s", deps)
            self._initialized = True
        except SystemExit:
            raise
        except Exception as e:
            logger.error("❌ 沙箱初始化失败: %s", e)
            if self._config.fail_if_unavailable:
                raise SystemExit(1)  # 对齐 CC failIfUnavailable
            self._initialized = False

    # ── 命令包装 ──────────────────────────────────────────────

    def wrap_with_sandbox(
        self,
        command: str,
        shell_path: str = "/bin/bash",
        working_dir: str = ".",
    ) -> str:
        """
        把 `rm -rf /tmp/foo` 包装成沙箱内可执行命令
        对齐 CC BaseSandboxManager.wrapWithSandbox

        实现:返回 sandbox-runtime wrap-cli 命令字符串(对齐 CC 行为)。
             ToolRegistry.execute 时通过 subprocess.run 调用:
               subprocess.run(
                 ['npx', '-y', '@anthropic-ai/sandbox-runtime@latest',
                  'wrap', '--config', config_json, '--', command],
                 shell=True
               )
             对齐 CC 实际路径(CC 也拼 wrap-cli + Node.js 子进程)。

        禁用时返回原 command(不沙箱化,对齐 CC 不沙箱化时原样返回)。
        """
        if not self.is_sandbox_enabled():
            return command  # 不沙箱化,原样返回

        runtime_config = self._build_runtime_config(working_dir)
        config_json = json.dumps(runtime_config)

        # 拼成 shell 命令(用 shlex.quote 防 injection)
        return (
            f"npx -y @anthropic-ai/sandbox-runtime@latest wrap "
            f"--config {shlex.quote(config_json)} -- "
            f"{shlex.quote(command)}"
        )

    def wrap_with_sandbox_argv(
        self,
        command: str,
        shell_path: str = "/bin/bash",
        working_dir: str = ".",
    ) -> Optional[list[str]]:
        """
        返回 argv 列表形式(供 subprocess.run(argv, shell=False) 调用)

        比 wrap_with_sandbox 更安全(不经 shell 解析),但要求 caller 用 argv 调用。
        禁用或失败时返 None(caller 应 fallback 到原 command)。

        M2 主路径用 wrap_with_sandbox(shell=True),
        本方法留给 M3+ 想避免 shell 注入的高级场景。
        """
        if not self.is_sandbox_enabled():
            return None
        runtime_config = self._build_runtime_config(working_dir)
        config_json = json.dumps(runtime_config)
        return [
            "npx", "-y", "@anthropic-ai/sandbox-runtime@latest",
            "wrap",
            "--config", config_json,
            "--", command,
        ]

    # ── 清理 ──────────────────────────────────────────────────

    def cleanup_after_command(self) -> None:
        """
        对齐 CC cleanupAfterCommand — 同步清理

        完整实现内容(对齐 CC cleanupAfterCommand):
        1. bare-git scrub:扫描 working_dir + sandbox_tmp_dir 下的 .git 残留
           (防 #29316 bare-git scrub 攻击向量逃逸沙箱)
        2. 临时文件清理:sandbox_tmp_dir 里的 run-* 子目录按 mtime 过期(默认 24h)
        """
        if not self.is_sandbox_enabled():
            return
        try:
            self._scrub_bare_git()           # 防 #29316
            self._cleanup_sandbox_tmp_dir()  # mtime 过期
        except Exception as e:
            logger.warning("沙箱 cleanup 部分失败: %s", e)

    # ── 内部:runtime config ──────────────────────────────────

    def _build_runtime_config(self, working_dir: str) -> dict:
        """对齐 CC convertToSandboxRuntimeConfig"""
        return {
            "filesystem": {
                "allowWrite": [
                    ".",
                    self._get_sandbox_tmp_dir(),
                ] + list(self._config.fs_allow_write),
                "denyWrite": list(self._config.fs_deny_write),
                "allowRead": list(self._config.fs_allow_read),
                "denyRead": list(self._config.fs_deny_read),
            },
            "network": {
                "allowedHosts": list(self._config.network_allowed_domains),
                "deniedHosts": list(self._config.network_denied_domains),
            },
        }

    # ── 内部:平台检测 ────────────────────────────────────────

    def _is_supported_platform(self) -> bool:
        """
        对齐 CC isSupportedPlatform

        - macOS → True(Seatbelt 内建)
        - Linux → True 仅当 WSL2(/proc/version 含 microsoft)
        - 其他(Windows 等)→ False
        """
        if sys.platform == "darwin":
            return True  # macOS Seatbelt 内建
        if sys.platform.startswith("linux"):
            # 检查是否在 WSL
            try:
                with open("/proc/version") as f:
                    return "microsoft" in f.read().lower()  # WSL2
            except (FileNotFoundError, OSError):
                return False
        return False

    # ── 内部:依赖检测 ────────────────────────────────────────

    def _check_dependencies(self) -> bool:
        """
        对齐 CC checkDependencies 快速路径:只判断 bwrap / macOS 内建

        Returns:
            True 如果依赖满足(macOS 内建 / Linux 有 bwrap)
        """
        if sys.platform == "darwin":
            return True  # macOS Seatbelt 内建,无需 bwrap
        return shutil.which("bwrap") is not None

    def _check_dependencies_detailed(self) -> dict:
        """
        对齐 CC checkDependencies — 返回 {errors, warnings}

        - errors: bwrap 缺失(linux 必需)→ 致命
        - warnings: socat 缺失(linux 网络隔离可能不完整)→ 非致命
        """
        errors: list[str] = []
        warnings: list[str] = []
        if sys.platform == "darwin":
            # macOS Seatbelt 内建,无额外依赖
            pass
        elif sys.platform.startswith("linux"):
            if not shutil.which("bwrap"):
                errors.append("bubblewrap (bwrap) 未安装:apt install bubblewrap")
            if not shutil.which("socat"):
                warnings.append("socat 未安装:网络隔离可能不完整")
        else:
            errors.append(f"不支持的平台: {sys.platform}")
        return {"errors": errors, "warnings": warnings}

    # ── 内部:sandbox tmp dir ─────────────────────────────────

    def _get_sandbox_tmp_dir(self) -> str:
        """
        对齐 CC sandboxTmpDir — /tmp/claude-<uid> mode 0o700

        每个用户独立 tmp dir(防跨用户读写),权限 0o700(仅 owner)。
        """
        import tempfile
        uid = os.getuid() if hasattr(os, "getuid") else 0
        tmp = Path(tempfile.gettempdir()) / f"claude-{uid}"
        try:
            tmp.mkdir(mode=0o700, exist_ok=True)
        except OSError as e:
            logger.warning("sandbox tmp dir 创建失败: %s", e)
        return str(tmp)

    # ── 内部:bare-git scrub(防 #29316)──────────────────────

    def _scrub_bare_git(self, search_dirs: Optional[list[str]] = None) -> int:
        """
        对齐 CC scrubBareGit — 扫描 .git 残留并删除裸 git 目录

        防 CC #29316 bare-git scrub 攻击:恶意仓库在 .git/config 里塞
        alias / hook,用户 `cd repo && git status` 时触发 RCE。
        沙箱执行后扫一遍,删掉异常的裸 .git 目录。

        Args:
            search_dirs: 扫描目录列表(None → [cwd, sandbox_tmp_dir])

        Returns:
            删除的 bare-git 目录数
        """
        if search_dirs is None:
            search_dirs = [os.getcwd(), self._get_sandbox_tmp_dir()]

        removed = 0
        for search_dir in search_dirs:
            search_path = Path(search_dir)
            if not search_path.exists():
                continue
            try:
                for entry in search_path.iterdir():
                    if not entry.is_dir():
                        continue
                    name = entry.name
                    # bare-git 目录特征:以 .git 结尾,或本身就是 .git 但非标准仓库
                    # 标准 .git 目录在项目根是正常的,这里只清 sandbox tmp + 异常位置
                    # 对齐 CC:只清 sandbox_tmp_dir 里的 .git 残留 + cwd 下的 *.git 目录
                    if search_dir == self._get_sandbox_tmp_dir() and name == ".git":
                        # sandbox tmp 里的 .git 一定是异常的(沙箱内不该有 git 仓库)
                        self._safe_rmtree(entry)
                        removed += 1
                    elif name.endswith(".git") and name != ".git":
                        # xxx.git 形式的 bare repo(cwd 下)
                        self._safe_rmtree(entry)
                        removed += 1
            except OSError as e:
                logger.warning("bare-git scrub 扫描 %s 失败: %s", search_dir, e)
        if removed:
            logger.info("bare-git scrub: 删除 %d 个异常 .git 目录", removed)
        return removed

    def _safe_rmtree(self, path: Path) -> None:
        """安全删除目录(异常不抛)"""
        import shutil as _shutil
        try:
            _shutil.rmtree(path)
        except OSError as e:
            logger.warning("删除 %s 失败: %s", path, e)

    # ── 内部:sandbox tmp dir mtime 过期 ──────────────────────

    def _cleanup_sandbox_tmp_dir(self, max_age_hours: float = 24.0) -> int:
        """
        对齐 CC cleanupSandboxTmpDir — mtime 过期清理

        sandbox_tmp_dir 里的 run-* 子目录按 mtime 过期(默认 24h)。
        防止沙箱临时文件无限堆积。

        Args:
            max_age_hours: 最大保留小时数(默认 24)

        Returns:
            删除的子目录数
        """
        tmp_dir = Path(self._get_sandbox_tmp_dir())
        if not tmp_dir.exists():
            return 0

        now = time.time()
        max_age_seconds = max_age_hours * 3600
        removed = 0
        try:
            for entry in tmp_dir.iterdir():
                if not entry.is_dir():
                    continue
                try:
                    mtime = entry.stat().st_mtime
                except OSError:
                    continue
                if (now - mtime) > max_age_seconds:
                    self._safe_rmtree(entry)
                    removed += 1
        except OSError as e:
            logger.warning("sandbox tmp dir 清理失败: %s", e)
        if removed:
            logger.info("sandbox tmp cleanup: 删除 %d 个过期子目录", removed)
        return removed

    # ── 测试 helper(不影响 production 逻辑)─────────────────

    def _reset_for_testing(self, config: Optional[SandboxConfig] = None) -> None:
        """
        测试专用:重置单例状态(production 不调)

        单例模式让测试间状态泄漏,这里显式重置以便隔离测试。
        """
        self._initialized = False
        self._config = config or SandboxConfig()


# ────────────────────────────────────────────────────────────────────
# 全局单例(对齐 CC — 模块级导出)
# ────────────────────────────────────────────────────────────────────

sandbox_manager = SandboxManager()
