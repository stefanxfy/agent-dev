"""
沙箱后端可插拔包。

设计文档:docs/tool/sandbox-pluggable-design.md

层次(整洁架构,依赖向内):
    Entity   : base.py     — SandboxRuntimeConfig(纯数据) / SandboxBackend(Protocol) / NullBackend
    Adapter  : native.py   — NativeBackend(sandbox-exec / bwrap)
             : srt.py      — SrtBackend(srt 二进制)
    Shared   : cleanup.py  — 平台无关的清理函数(bare-git scrub / tmp 过期)

SandboxManager(UseCase 层,在父目录 manager.py)持有 backend 列表 + 选定 backend,
wrap_with_sandbox 委托 backend.wrap()。新增 backend = 新文件 + 组合根注册一行。
"""

from .base import NullBackend, SandboxBackend, SandboxRuntimeConfig
from .native import NativeBackend
from .srt import SrtBackend

__all__ = [
    "SandboxRuntimeConfig",
    "SandboxBackend",
    "NullBackend",
    "NativeBackend",
    "SrtBackend",
]
