"""agent_core.tools.permission._logging — permission 子系统共享 logger(D.3b)。

permission_logger 是 engine + step 共享的统一 logger(emoji 前缀 🛡️/🧪/🤖),
可被 AGENT_LOG_PERMISSION 单独调级别。

从 engine.py 抽出,让 step 类不必反向 import engine(解耦)。
"""

import logging

# 🛡️ permission 子系统统一 logger — 可被 AGENT_LOG_PERMISSION 单独调级别
permission_logger = logging.getLogger("agent_core.permission")
