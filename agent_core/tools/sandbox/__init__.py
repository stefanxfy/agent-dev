"""agent_core.tools.sandbox 子包 facade(D-5 / Phase E)。

设计契约:
  - 新代码应 `from agent_core.tools.sandbox import X`(本 facade)或显式子模块路径
  - 旧扁平路径 `agent_core.tools.sandbox_X` 已全部迁移完毕(E.4 完成),
    不再保留兼容 shim;`tools/__init__.py` 不 re-export sandbox 符号

层次(整洁架构,依赖向内):
    backends/ — SandboxBackend(Protocol) / NativeBackend / SrtBackend / NullBackend / cleanup
    manager.py   — SandboxManager(UseCase 层)+ 模块级 sandbox_manager 单例
    decision.py  — should_use_sandbox / excluded command 检查
    prompt.py    — get_sandbox_prompt_section(系统 prompt 注入)

设计文档:docs/tool/sandbox-pluggable-design.md
"""

# ── manager ──
from agent_core.tools.sandbox.manager import (
    SandboxConfig,
    SandboxManager,
    sandbox_manager,   # 进程级单例(D-4:对齐 CC,保留 __new__ 强制)
)

# ── decision ──
from agent_core.tools.sandbox.decision import (
    should_use_sandbox,
    get_excluded_command_match,
    get_excluded_command_message,
)

# ── prompt ──
from agent_core.tools.sandbox.prompt import get_sandbox_prompt_section

# ── backends(可插拔后端)──
from agent_core.tools.sandbox.backends import (
    NullBackend,
    SandboxBackend,
    SandboxRuntimeConfig,
    NativeBackend,
    SrtBackend,
)

__all__ = [
    # manager
    "SandboxConfig",
    "SandboxManager",
    "sandbox_manager",
    # decision
    "should_use_sandbox",
    "get_excluded_command_match",
    "get_excluded_command_message",
    # prompt
    "get_sandbox_prompt_section",
    # backends
    "NullBackend",
    "SandboxBackend",
    "SandboxRuntimeConfig",
    "NativeBackend",
    "SrtBackend",
]
