"""所有 LLM provider 的代码根包。

导入本包会触发 `from .anthropic import ...` / `from .openai import ...`
等子模块加载,进而触发 `@ProviderRegistry.register` 副作用,完成自动注册。

⚠️ Bug 防御:必须显式 import 子包。Python 不会自动加载子包,只 import
`agent_core.llm.providers` 这个空包不会触发任何 @register_provider 装饰器,
会出现 "Provider ... 未注册" 错误。

历史:2026-06-29 LLM Router 重构 Stage 1 创建(参见
`docs/llm-router-architecture-redesign.md` §3.1)。
"""
# 触发 @register_provider 副作用(每个 provider 文件顶层有装饰器)
from . import anthropic, openai  # noqa: F401

__all__ = []
