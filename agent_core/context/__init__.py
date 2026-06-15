# agent_core/context — 上下文管理系统
#
# 模块：
#   budget.py    — Token 预算监控 + 自动压缩触发 + 熔断保护
#   tokenizer.py — Token 估算（中英文比例）
#   compact.py   — 压缩编排 + PTL 防御
#   manager.py   — 统一入口 ContextManager
#
# 设计参考：Claude Code src/services/compact/
# 适配：GLM 模型参数，删除 Claude 专有逻辑（cache_control/Forked Agent/StateKeeper）
