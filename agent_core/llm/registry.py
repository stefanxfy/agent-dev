"""Provider Registry — OCP 友好的 provider 注册表(2026-06-29 LLM Router 重构 Stage 2)。

设计动机:
- 原 `create_openai_compatible_provider` 走 `LLMProvider → ProviderClass` 硬编码 mapping,
  加第 4 个 OpenAI 兼容 provider 需同时改 2 个文件(router.py + openai_compatible.py)。
- ProviderRegistry 用装饰器自注册:加 provider 只需新建文件 + 装饰器,不动 router.py。

使用示例:
    @ProviderRegistry.register(LLMProvider.ANTHROPIC)
    class AnthropicProvider(BaseProvider):
        ...

    # 创建实例
    provider = ProviderRegistry.create(LLMConfig(provider='anthropic', ...))

外部测试 patch 目标:ProviderRegistry.create (classmethod)
- 比 patch LLMRouter._provider 私有属性更稳定(类方法不会随实例化重置)
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Type

from .config import LLMProvider

if TYPE_CHECKING:
    from .providers.base import BaseProvider


class ProviderRegistry:
    """Provider 自注册表 — key: LLMProvider enum, value: BaseProvider 子类"""

    _mapping: dict[LLMProvider, Type["BaseProvider"]] = {}

    @classmethod
    def register(cls, key: LLMProvider):
        """装饰器:把 BaseProvider 子类注册到指定 key。

        重复注册会抛 ValueError,防止覆盖导致难以诊断的 bug。
        """
        def decorator(klass: Type["BaseProvider"]) -> Type["BaseProvider"]:
            if key in cls._mapping:
                existing = cls._mapping[key]
                # 幂等性检查:模块重复 import / importlib.reload 时
                # class 对象 identity 不同,但 __qualname__ + __module__ 一致
                if (
                    existing.__qualname__ == klass.__qualname__
                    and existing.__module__ == klass.__module__
                ):
                    return klass
                raise ValueError(
                    f"Provider {key!r} 已被 {existing.__name__} 注册,不能重复注册 {klass.__name__}"
                )
            cls._mapping[key] = klass
            return klass
        return decorator

    @classmethod
    def create(cls, config) -> "BaseProvider":
        """根据 LLMConfig.provider 创建对应的 Provider 实例。

        未注册时抛 ValueError,提示用户 import agent_core.llm.providers 触发自动注册。
        """
        klass = cls._mapping.get(config.provider)
        if klass is None:
            raise ValueError(
                f"Provider {config.provider!r} 未注册。"
                f"已注册: {[k.value for k in cls._mapping.keys()]}。"
                f"提示: import agent_core.llm.providers 触发自动注册。"
            )
        return klass(config)


def register_provider(key: LLMProvider):
    """模块级便捷入口(等价于 ProviderRegistry.register(key))"""
    return ProviderRegistry.register(key)


__all__ = ["ProviderRegistry", "register_provider"]
