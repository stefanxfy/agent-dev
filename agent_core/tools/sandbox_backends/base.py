"""
沙箱后端可插拔抽象(Entity 层,最内层)

设计依据:docs/tool/sandbox-pluggable-design.md §5

依赖铁律(整洁架构):本模块是最内层 Entity,不 import subprocess / 平台模块 / 任何具体
backend。只定义抽象与纯数据。所有依赖箭头从外层(Adapter)指向这里,这里不知外层任何东西。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

# ⚙️ sandbox 子系统 logger(与 sandbox_manager 共用,便于统一过滤)
sandbox_logger = logging.getLogger("agent_core.sandbox")


# ────────────────────────────────────────────────────────────────────
# SandboxRuntimeConfig — 纯数据(Entity 层)
# ────────────────────────────────────────────────────────────────────

@dataclass
class SandboxRuntimeConfig:
    """
    backend 无关的沙箱规则语义(纯数据,零翻译方法)。

    设计 §5.1 / 决策 B(修正):翻译方法(_cfg_to_srt_json / _cfg_to_seatbelt_profile /
    _cfg_to_bwrap_argv)属于各 backend 的私有方法,**不**挂在本 dataclass 上。
    这样加新 backend 不动 config(满足 OCP,内层不依赖外层细节)。

    由 SandboxManager._build_runtime_config() 构造(从应用层 permission 规则 +
    sandbox settings → 本 dataclass),各 backend.wrap() 消费。
    """

    fs_allow_write: list[str] = field(default_factory=list)
    fs_deny_write: list[str] = field(default_factory=list)
    fs_allow_read: list[str] = field(default_factory=list)
    fs_deny_read: list[str] = field(default_factory=list)
    net_allowed: list[str] = field(default_factory=list)
    net_denied: list[str] = field(default_factory=list)


# ────────────────────────────────────────────────────────────────────
# SandboxBackend — Protocol(UseCase↔Adapter 边界抽象)
# ────────────────────────────────────────────────────────────────────

@runtime_checkable
class SandboxBackend(Protocol):
    """
    沙箱后端抽象(对齐设计 §5.2)。

    契约:
    - is_available()=False 时,wrap() 不被调用(由 SandboxManager._select_backend
      在选定阶段保证,不在运行时反复判)。
    - wrap() 返回给 subprocess.run(shell=True) 的命令字符串。
    - initialize() 在 is_available() 返回 True 后、首次 wrap 前调一次(可 lazy)。
    - cleanup() 每次 wrap 执行后被调用(由 BashTool 接线,设计 §10)。
    """

    name: str

    def is_available(self) -> bool:
        """依赖是否满足(二进制存在 / 平台支持)。只判"能否调起",不判隔离是否生效。"""
        ...

    def initialize(self) -> None:
        """preflight + 缓存初始化状态。失败应让后续 is_available() 返 False。"""
        ...

    def wrap(
        self,
        command: str,
        cfg: SandboxRuntimeConfig,
        working_dir: str,
    ) -> str:
        """把用户命令包装成沙箱内可执行命令字符串。"""
        ...

    def cleanup(self) -> None:
        """每次命令执行后的清理(bare-git scrub / tmp 过期 / 自身资源)。"""
        ...


# ────────────────────────────────────────────────────────────────────
# NullBackend — 兜底(设计 §5.4)
# ────────────────────────────────────────────────────────────────────

class NullBackend:
    """
    兜底 backend。

    使用场景(设计 §6 切换矩阵):
    - auto 模式:全部 backend 不可用 + failIfUnavailable=false
    - 显式模式:指定 backend 不可用 + failIfUnavailable=false

    行为:wrap() 返回原命令(不沙箱化)+ WARNING。**不静默**(修正现状 fail-open
    静默降级问题 —— 必须让用户知道"沙箱没生效")。
    """

    name: str = "null"

    def is_available(self) -> bool:
        return True

    def initialize(self) -> None:
        pass

    def wrap(
        self,
        command: str,
        cfg: SandboxRuntimeConfig,
        working_dir: str,
    ) -> str:
        sandbox_logger.warning(
            "⚠️ [backend_null_wrap] command passthrough — no sandbox isolation active "
            "(backend=null, command_preview=%s)",
            command[:60],
        )
        return command

    def cleanup(self) -> None:
        # 共享清理由 SandboxManager.cleanup_after_command 统一调。NullBackend 无特定清理。
        pass
