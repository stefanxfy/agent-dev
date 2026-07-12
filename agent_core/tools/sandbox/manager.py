"""
OS 层沙箱管理器(UseCase 层)— 编排可插拔 backend。

设计:docs/tool/sandbox-pluggable-design.md

架构(整洁架构,依赖向内):
- SandboxManager(本文件,UseCase 层)持有 backend 列表 + 选定 backend,
  wrap_with_sandbox 委托 self._backend.wrap()。不 import 任何具体 backend 类
  (依赖 SandboxBackend Protocol,组合根注入实例)。
- backend 实现(Adapter 层):NativeBackend(sandbox-exec/bwrap)、SrtBackend(srt 二进制)
  在 sandbox_backends/ 子包,各自负责 translator + subprocess。
- SandboxRuntimeConfig(Entity 层,纯数据)由 _build_runtime_config 产出,backend 消费。

关键变更(相对旧实现):
- 删除硬编码 npx 调用(CLI 接口错,exit 127),改为委托可插拔 backend
- 平台检测(_is_supported_platform / _check_dependencies)下沉到 NativeBackend
- 共享清理(_scrub_bare_git 等)移到 sandbox_backends/_cleanup.py
- SandboxManager 不再强制单例(__new__),但模块级 sandbox_manager 仍作为默认实例
  (组合根 web/app.py 用它;非 web 入口的兜底)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

from .backends import NullBackend, SandboxBackend, SandboxRuntimeConfig
from .backends.cleanup import get_sandbox_tmp_dir

logger = logging.getLogger(__name__)

# ⚙️ sandbox 子系统 logger
sandbox_logger = logging.getLogger("agent_core.sandbox")


# ────────────────────────────────────────────────────────────────────
# SandboxConfig — 沙箱配置(对齐 CC SandboxSettingsSchema + 设计 §7)
# ────────────────────────────────────────────────────────────────────

@dataclass
class SandboxConfig:
    """
    沙箱配置(对齐 CC SandboxSettingsSchema + 设计 §7 新增 backend 字段)。

    新增字段(决策 D):
    - backend: "auto" | "srt" | "native"(默认 auto)
    - backend_priority: auto 模式探测顺序(默认 ["native", "srt"],决策 A)

    backends.{srt,native} 嵌套私有配置不在此处(由组合根从 settings.json 直接读取,
    传给各 backend 构造函数)。
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
    # 设计 §7 新增
    backend: str = "auto"
    backend_priority: list[str] = field(default_factory=lambda: ["native", "srt"])


# ────────────────────────────────────────────────────────────────────
# SandboxManager — 编排可插拔 backend
# ────────────────────────────────────────────────────────────────────

class SandboxManager:
    """
    沙箱管理器(UseCase 层)— 进程级单例。

    设计契约:
      - 单例:__new__ 强制返回同一实例。Streamlit 单进程下,各 session 共享
        settings.json 的 sandbox 配置是 feature(对齐 CC 的"进程级单例"
        模式),而非 bug。OpenClaw 风格的"session 级 DI"对本项目是过度设计。
      - session 级状态(deny 计数 / 审计聚合 / backend 选型)由 SessionContext
        持有,本类保持 stateless 配置门面 + backend 委托。
      - 多用户并发隔离是独立任务,届时拆 SessionSandboxContext(per-session DI),
        本类改为 stateless config provider。已确认 Out of Scope(2026-07)。

    生命周期:
      1. __new__:进程级单例初始化(_config / _backends / _backend)
      2. load_config(settings):从 settings.json 的 sandbox 段更新 _config
      3. configure_backends(backends):组合根注入 backend 列表 + _select_backend 选定
      4. is_sandbox_enabled():config.enabled + _backend.is_available()
      5. wrap_with_sandbox(cmd):委托 _backend.wrap(cmd, runtime_config, wd)
      6. cleanup_after_command():委托 _backend.cleanup()
    """

    _instance: Optional["SandboxManager"] = None

    def __new__(cls):
        # 进程级单例(对齐 CC + 设计 §10"保留形态"):
        #   SandboxManager() 始终返同一实例,让测试 fixture 的 mock 与
        #   production 读模块级 sandbox_manager 一致(见模块底部实例化)。
        # 多 session 隔离(SessionSandboxContext)留作独立任务,本 plan Out of Scope。
        if cls._instance is None:
            inst = super().__new__(cls)
            inst._config = SandboxConfig()
            inst._backends = []
            inst._backend = None  # 选中的 backend
            cls._instance = inst
        return cls._instance

    # ── 配置加载 ──────────────────────────────────────────────

    def load_config(self, config_dict: Optional[dict]) -> None:
        """
        从 settings.json 的 sandbox 段加载配置(对齐 CC loadSandboxSettings + 设计 §7)。

        Args:
            config_dict: settings.json 里的 "sandbox" 子 dict,None 则不动
        """
        if config_dict is None:
            return
        try:
            self._config = SandboxConfig(
                enabled=bool(config_dict.get("enabled", self._config.enabled)),
                fail_if_unavailable=bool(
                    config_dict.get("failIfUnavailable", self._config.fail_if_unavailable)
                ),
                auto_allow_bash_if_sandboxed=bool(
                    config_dict.get(
                        "autoAllowBashIfSandboxed", self._config.auto_allow_bash_if_sandboxed
                    )
                ),
                allow_unsandboxed_commands=bool(
                    config_dict.get(
                        "allowUnsandboxedCommands", self._config.allow_unsandboxed_commands
                    )
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
                backend=str(config_dict.get("backend", self._config.backend)),
                backend_priority=list(
                    config_dict.get("backendPriority", self._config.backend_priority)
                ),
            )
        except Exception as e:
            logger.warning("sandbox config 加载失败,保持默认: %s", e)
            return

        # config 变更后,若 backends 已注入,重新选定(backend/backendPriority 可能变了)
        if self._backends:
            self._select_backend()

    # ── backend 注入 + 选定(组合根调用)──────────────────────

    def configure_backends(self, backends: list[SandboxBackend]) -> None:
        """
        组合根(web/app.py)注入 backend 列表 + 选定。

        静态显式注入(设计 §3 决策):顺序不隐含优先级,优先级由 _config.backend_priority
        决定。注入后立即 _select_backend。
        """
        self._backends = list(backends)
        self._select_backend()

    def _select_backend(self) -> None:
        """
        按设计 §6 切换矩阵选定 _backend。

        - auto:按 backend_priority 顺序找首个 is_available() 的 backend
        - 显式:只在 backends 里找 name 匹配的,**不 cross-fallback**(违反显式意图)
        - 不可用兜底:fail_if_unavailable → SystemExit(1);否则 NullBackend + 警告
        """
        if not self._backends:
            self._backend = None
            return

        by_name: dict[str, SandboxBackend] = {b.name: b for b in self._backends}

        if self._config.backend == "auto":
            for name in self._config.backend_priority:
                b = by_name.get(name)
                if b is None:
                    continue
                if b.is_available():
                    self._backend = b
                    sandbox_logger.info(
                        "⚙️ [backend_selected] name=%s reason=auto_priority_available",
                        b.name,
                    )
                    return
                sandbox_logger.warning(
                    "⚠️ [backend_unavailable] name=%s reason=is_available_false",
                    b.name,
                )
            # 全不可用
            if self._config.fail_if_unavailable:
                sandbox_logger.error("❌ [backend_all_unavailable] fail_if_unavailable → exit")
                raise SystemExit(1)
            self._backend = NullBackend()
            sandbox_logger.warning(
                "⚠️ [backend_null_fallback] reason=auto_all_unavailable"
            )
            return

        # 显式模式
        b = by_name.get(self._config.backend)
        if b is not None and b.is_available():
            self._backend = b
            sandbox_logger.info(
                "⚙️ [backend_selected] name=%s reason=explicit", b.name
            )
            return
        sandbox_logger.warning(
            "⚠️ [backend_unavailable] name=%s reason=explicit_not_available",
            self._config.backend,
        )
        if self._config.fail_if_unavailable:
            raise SystemExit(1)
        self._backend = NullBackend()
        sandbox_logger.warning(
            "⚠️ [backend_null_fallback] reason=explicit_unavailable original=%s",
            self._config.backend,
        )

    # ── 启用判定 ──────────────────────────────────────────────

    def is_sandbox_enabled(self) -> bool:
        """
        沙箱是否启用。

        两段:config.enabled + 选中的 _backend.is_available()。
        平台/依赖检测已下沉到 backend,本层不再判"macOS Seatbelt 内建"。
        """
        if not self._config.enabled:
            return False
        if self._backend is None:
            return False
        return self._backend.is_available()

    def initialize(self) -> None:
        """委托 backend.initialize()(lazy preflight)。"""
        if self._backend is None:
            return
        try:
            self._backend.initialize()
        except Exception as e:
            logger.error("❌ backend initialize 失败: %s", e)
            if self._config.fail_if_unavailable:
                raise SystemExit(1)

    # ── 命令包装(委托 backend)────────────────────────────────

    def wrap_with_sandbox(
        self,
        command: str,
        shell_path: str = "/bin/bash",
        working_dir: str = ".",
    ) -> str:
        """
        把用户命令包装成沙箱内可执行命令。委托 self._backend.wrap()。

        禁用或无 backend → 返原命令(passthrough,不沙箱化)。
        """
        if not self.is_sandbox_enabled():
            sandbox_logger.debug(
                "⚙️ [wrap_with_sandbox] sandbox not enabled → passthrough"
            )
            return command

        cfg = self._build_runtime_config(working_dir)
        return self._backend.wrap(command, cfg, working_dir)  # type: ignore[union-attr]

    def _build_runtime_config(self, working_dir: str) -> SandboxRuntimeConfig:
        """对齐 CC convertToSandboxRuntimeConfig —— 产 backend 无关的纯数据。"""
        return SandboxRuntimeConfig(
            fs_allow_write=[".", get_sandbox_tmp_dir()] + list(self._config.fs_allow_write),
            fs_deny_write=list(self._config.fs_deny_write),
            fs_allow_read=list(self._config.fs_allow_read),
            fs_deny_read=list(self._config.fs_deny_read),
            net_allowed=list(self._config.network_allowed_domains),
            net_denied=list(self._config.network_denied_domains),
        )

    # ── 清理(委托 backend)────────────────────────────────────

    def cleanup_after_command(self) -> None:
        """共享清理(bare-git scrub + tmp 过期,总跑)+ backend 特定清理。

        设计:共享清理(防 CC #29316)对 host 上所有 bash 命令都有意义,不依赖沙箱
        是否启用或 backend 是否选中,故总跑;backend 特定清理(如 SrtBackend config
        文件)委托 _backend.cleanup()。
        """
        from .backends.cleanup import run_default_cleanup

        backend_name = self._backend.name if self._backend else "none"
        run_default_cleanup(backend_name)
        if self._backend is not None:
            self._backend.cleanup()

    # ── 测试 helper(不影响 production 逻辑)─────────────────

    def _reset_for_testing(self, config: Optional[SandboxConfig] = None) -> None:
        """测试专用:重置状态(production 不调)。"""
        self._config = config or SandboxConfig()
        self._backends = []
        self._backend = None


# ────────────────────────────────────────────────────────────────────
# 全局默认实例(组合根 web/app.py 用它;非 web 入口兜底)
#
# 这是进程级单例的代表实例 — 与 SandboxManager.__new__ 强制单例语义一致。
# Streamlit 单进程下,所有 session 共享此实例的 sandbox 配置(sandbox_manager.json
# 等),符合 CC 的"进程级单例"模式。多 session 隔离留作独立任务。
# ────────────────────────────────────────────────────────────────────

sandbox_manager = SandboxManager()
