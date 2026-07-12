"""agent_core.tools 子包 facade。

D.2 / Phase E 完成后,权限代码已迁到 `agent_core.tools.permission/`,
沙箱代码已迁到 `agent_core.tools.sandbox/`。本文件 re-export 常用顶层模块;
新代码应直接用 `from agent_core.tools.permission import X` 或
`from agent_core.tools.sandbox import X`。
"""

from agent_core.tools import audit_logger  # noqa: F401
from agent_core.tools import base  # noqa: F401
from agent_core.tools import builtin  # noqa: F401