# LLM Router 架构重构设计

> **模块**:`agent_core/llm/`
> **当前痛点**:重复代码多、职责混杂、新增 provider 需改核心文件
> **目标**:解耦 · 可扩展 · 抽象 · 继承 · 多态
> **文档版本**:v1.0 · 2026-06-28

---

## 1. 当前架构问题诊断

### 1.1 现状概览

```
agent_core/llm/
├── __init__.py                 (空)
├── router.py                   29 KB  ← 一切都在这里
├── openai_compatible.py        14 KB  ← 半个抽象 + 3 个子类
└── thinking_splitter.py        4.5 KB
```

### 1.2 核心问题清单

| # | 问题 | 体现位置 | 违反的原则 |
|---|---|---|---|
| **P1** | `router.py` 是「上帝类」 | 29 KB 一个文件,塞了 enum / config / dataclass / 错误分类 / 重试 / Anthropic 私有实现 / OpenAI 调度 / system prompt 注入 / cache 注入 | **SRP**(单一职责) |
| **P2** | `Anthropic` 不是「类」,是「方法」 | `_chat_anthropic` 是 `LLMRouter` 的私有方法(行 556–658),与 `OpenAICompatibleProvider` 类体系**严重不对称** | **OCP**(开闭原则) |
| **P3** | 调度硬编码 `if/elif` | `chat()` 中 `if provider == "anthropic": ... elif provider in ("openai","zhipu","minimax"): ...`(行 419–461),新增 provider 必须改此处 | **OCP / DIP** |
| **P4** | Provider 特有变换泄露到 Router | `tool_choice` 映射为 Anthropic 的 `{"type":"none"}`(行 576–579)、`system_message` 顶层 vs 消息内位置(row 587–593)、`cache_namespace` 分 provider 警告日志(行 435–454)——这些都该在 Provider 内部 | **信息专家模式 / 封装** |
| **P5** | 工厂 `create_openai_compatible_provider` 手写映射 | `openai_compatible.py:323-341` 一个 dict 硬编码 provider→class 关系,新增 provider 还得改这里 | **OCP** |
| **P6** | 错误分类与重试与 Router 耦合 | `_classify_http_error` / `_is_stream_interruption_error` / `_stream_with_retry` 全在 router.py(行 174–552),不能独立测试/复用 | **SRP** |
| **P7** | 领域类型混入 Router | `StreamChunk` / `TextDelta` / `ThinkingDelta` / `ToolCallDelta` / `UsageStats` 都在 router.py(行 116–343),Provider 想 import 这些数据类反而要绕一圈 | **关注点分离** |
| **P8** | `_ThinkTagSplitter` 下划线「私有」但被外部使用 | `router.py:18` re-export、`openai_compatible.py:35` 直接 import,下划线误导读者以为不可碰 | **命名一致性** |
| **P9** | Provider 元数据散落多地 | `PROVIDER_ENV_KEY`(router.py:87) + `default_base_url`(openai_compatible.py 类属性) + 各自的 `_resolve_api_key`,无法一眼看清「这个 provider 的所有元数据」 | **内聚性** |
| **P10** | 系统消息处理逻辑双套 | Anthropic 走 `kwargs["system"]` 顶层参数;OpenAI 兼容走 `messages` 内的 system 消息;且 `system_prompt_override` 在两套逻辑里**分别写了一遍**(router.py:407-417 + 431-434) | **DRY** |
| **P11** | 测试耦合过深 | `test_router.py` 同时 import 7 个不同抽象层次的类(行 8),测一个东西要造一堆 mock | **可测性** |

### 1.3 一个直观的对比

**现状:Anthropic 走「方法」,OpenAI 兼容走「类」**

```python
# router.py:419 (Anthropic)
if provider == "anthropic":
    yield from self._stream_with_retry(
        lambda: self._chat_anthropic(...),  # ← 方法
        provider=provider,
    )
# router.py:424 (OpenAI 兼容)
elif provider in ("openai", "zhipu", "minimax"):
    openai_provider = self._get_openai_provider()
    yield from self._stream_with_retry(
        lambda: openai_provider.chat(...),  # ← 类的实例
        provider=provider,
    )
```

**目标:全部走「类」,Router 不知道具体 provider 是什么,也不管重试**

```python
# router.py(目标)
def chat(self, messages, ...):
    system_prompt, messages = self._resolve_system(messages, ...)
    yield from self.provider.chat(            # 多态分发 + Provider 内部重试
        messages=messages, tools=..., system_prompt=system_prompt, ...
    )
```

---

## 2. 设计目标与原则

### 2.1 设计原则(SOLID + DIP + 多用组合少用继承)

| 原则 | 在 LLM Router 中的具体落实 |
|---|---|
| **SRP** | Router 只做「调度」(分发到 Provider);Provider 自己负责「协议适配 + 重试」;Strategy 只做「单一变换」;DataType 不含逻辑 |
| **OCP** | 新增 provider = 新建一个文件 + 一个 `@register_provider` 装饰器;Router 一行不改 |
| **LSP** | 所有 Provider 实现同一 `BaseProvider` 抽象类,Router 永远只调用 `BaseProvider` 的接口(不需要知道重试策略) |
| **ISP** | `BaseProvider` 接口保持极简(就一个 `chat()`),可选能力用 mixin 拆开(Thinking、CacheControl、ToolChoice) |
| **DIP** | Router 依赖 `BaseProvider` 抽象,不依赖 `RetryPolicy` / 任何具体 provider 类;Provider 通过「注册中心」注入,重试策略作为 Provider 内部属性 |
| **多用 Template Method,慎用 Strategy** | thinking 提取用 `BaseProvider` 子类的 `_extract_thinking()` 钩子(Template Method);`MiniMaxProvider._ThinkTagSplitter` 用嵌套类承载,而不抽到独立 `ThinkingExtractor` 策略类(参见 §8.3 YAGNI 决策) |

### 2.2 5 个设计目标

1. **对称性**:Anthropic、OpenAI、Google、DeepSeek、豆包……**全部走同一类继承体系**,没有「方法 vs 类」的混搭
2. **零侵入扩展**:加第 N 个 OpenAI 兼容 provider,**只需要 1 个新文件 + ~15 行**,Router / 工厂 / 调度都不动
3. **可测性**:每个 Provider 可独立 mock 一个 `BaseProvider` 测;重试可单独测;Strategy 可单独测
4. **Provider 自洽**:重试、错误分类、thinking 提取——都是「Provider 自己协议的实现细节」,封装在 Provider 内部;Router 不感知。usage 统计由 Provider 在流结束时产出,Router 只透传
5. **模块主入口向后兼容**:`from agent_core.llm.router import LLMRouter, LLMConfig, StreamChunk, ...` 等**主入口**符号仍可用(`router.py` re-export);**内部细节**路径(`_ThinkTagSplitter` / `thinking_splitter` 模块 / `llm.base`)会按 §6.1 兼容性矩阵迁移(见下文)

---

## 3. 目标架构

### 3.1 目录结构

```
agent_core/llm/
├── __init__.py                      # 公共 API(对老代码屏蔽迁移细节)
│
├── types.py                         # 纯数据类,无业务逻辑
│   ├── StreamChunk
│   ├── TextDelta / ThinkingDelta / ToolCallDelta
│   └── UsageStats
│
├── config.py                        # 配置相关
│   ├── LLMProvider (enum)           # 只保留 enum,无 model 列表
│   ├── ThinkingConfig
│   └── LLMConfig (Pydantic)
│       ├── provider / model / api_key / base_url
│       ├── max_tokens / temperature
│       ├── thinking: Optional[ThinkingConfig]
│       └── retry_policy: Optional[RetryPolicy]   # None = 用 Provider 默认
│                                                # (BaseProvider.__init__ 读取后
│                                                # 覆盖 self.retry_policy)
│
├── registry.py                      # Provider 注册中心(单例)
│   ├── ProviderRegistry
│   └── register_provider 装饰器
│
├── providers/                       # 所有 provider 的代码(抽象 + 实现)
│   ├── __init__.py                  # import 此包 → 触发自动注册
│   │
│   ├── _retry.py                    # 重试机制(Provider 内部工具,Router 不感知)
│   │   ├── RetryPolicy              # 配置(最大次数、退避、哪些状态码)
│   │   ├── classify_http_error()
│   │   └── retry_stream() 生成器(被 BaseProvider._with_retry 调用)
│   │
│   ├── base.py                      # BaseProvider(ABC) — 所有 provider 的根抽象
│   │                                # (放在 providers/ 下,与其他 base.py 对称)
│   │                                # 内置 Template Method:chat() 包装 _do_chat() + _with_retry()
│   │
│   ├── anthropic/                   # Anthropic 协议族(目前 1 个实现)
│   │   ├── __init__.py              # from .base import AnthropicProvider
│   │   └── base.py                  # AnthropicProvider(BaseProvider)
│   │
│   └── openai/                      # OpenAI 协议族(3 个实现)
│       ├── __init__.py
│       ├── base.py                  # OpenAICompatibleProvider(BaseProvider)
│       ├── openai.py                # OpenAIProvider  (官方 OpenAI)
│       ├── zhipu.py                 # ZhipuProvider
│       └── minimax.py               # MiniMaxProvider
│
├── router.py                        # 瘦身后的 Router(只剩 50~60 行,只做调度)
│
└── (已删除) thinking_splitter.py    # 状态机已内嵌到 MiniMaxProvider._ThinkTagSplitter,旧 import 路径全部迁移
```

### 3.2 核心抽象

#### 抽象 1:`BaseProvider`(抽象基类,放在 `providers/base.py`)

**位置选择**:`BaseProvider` 放在 `providers/base.py`,**不**放在 `llm/base.py`。
理由:让「每个 `base.py` 都对应『本目录的抽象』」形成统一模式:

| 路径 | 抽象级别 | 说明 |
|---|---|---|
| `providers/base.py` | 协议族无关 | 跨所有协议族的最小契约(`chat()`) |
| `providers/anthropic/base.py` | Anthropic 协议 | Anthropic 协议的默认实现 |
| `providers/openai/base.py` | OpenAI 协议 | OpenAI 协议族共享 80% 的中间抽象(`OpenAICompatibleProvider`) |

**`BaseProvider` 放在 `providers/` 下的好处**:
1. **目录结构对称** — `base.py` ↔ `base.py` ↔ `base.py` 形成清晰的「抽象层级」链条
2. **路径更短** — `providers/anthropic/base.py` 继承时只需 `from ..base import BaseProvider`(2 个点),不用 3 个点
3. **`providers/` 自包含** — 所有 provider 相关代码(含根抽象)都在 `providers/` 下,`llm/` 只剩 types/config/registry/retry/router 等「基础设施」
4. **未来拆分友好** — 如果 `providers/` 未来要拆成独立子包(`llm_providers/`),已经自包含,不需要再调整 `llm/base.py` 的位置

**循环导入分析**:把 `BaseProvider` 放进 `providers/` 后,`registry.py` 需要 `from .providers.base import BaseProvider`。
导入顺序是 `llm/__init__.py` 先 `from . import providers`(触发 `providers/base.py` 加载),
再 `from .registry import ...`(此时 `providers.base` 已在 `sys.modules`,无循环)。

```python
# agent_core/llm/providers/base.py
from abc import ABC, abstractmethod
from typing import Generator, Optional
from ..types import StreamChunk              # ← .. 是 providers/ 上一级 = llm/
from ..config import LLMConfig
from ._retry import RetryPolicy, retry_stream


class BaseProvider(ABC):
    """所有 LLM Provider 必须实现的接口(根抽象)。

    设计原则(极简 + 自洽):
    - 子类只需实现 _do_chat():实际的协议调用(不含重试)
    - chat() 是 Template Method 公开入口:用 self.retry_policy 包装 _do_chat()
    - Router 只调 chat(),不需要知道有重试这回事
    - Provider 自己感知 RetryPolicy(类属性,可被 LLMConfig 覆盖)

    为什么 BaseProvider.chat() 是 Template Method 而不是直接 abstract:
      - 90% 的 Provider 共享同一份重试逻辑(HTTP 5xx / 429 重试 + 退避)
      - 强制每个 Provider 写自己的 chat() 会让简单 Provider 也得 copy-paste 重试代码
      - Template Method 让「默认行为统一,override 仍可行」——需要自定义重试的
        Provider 只需 override chat() 即可(Anthropic vs OpenAI 错误码不同就
        可能用得上)
    """

    # 元数据(子类可覆盖,Registry 据此自动填充 UI/配置)
    provider_name: str = ""              # 唯一标识
    default_base_url: Optional[str] = None
    env_key: str = ""                    # 默认 API key 环境变量名
    retry_policy: RetryPolicy = RetryPolicy.default()  # 默认重试策略(子类可覆盖)

    def __init__(self, config: LLMConfig):
        self.config = config
        # LLMConfig 可覆盖默认 retry_policy(允许外部按调用配置重试)
        if getattr(config, "retry_policy", None) is not None:
            self.retry_policy = config.retry_policy

    @abstractmethod
    def _do_chat(
        self,
        messages: list[dict],
        tools: Optional[list[dict]] = None,
        tool_choice: Optional[str] = None,
        system_prompt: Optional[str] = None,
        cache_namespace: Optional[str] = None,
    ) -> Generator[StreamChunk, None, None]:
        """子类必须实现:实际协议调用(不含重试包装)。

        抛出的任何异常视为「协议层错误」——由 BaseProvider.chat() 决定是否重试。
        """
        ...

    def chat(
        self,
        messages: list[dict],
        tools: Optional[list[dict]] = None,
        tool_choice: Optional[str] = None,
        system_prompt: Optional[str] = None,
        cache_namespace: Optional[str] = None,
    ) -> Generator[StreamChunk, None, None]:
        """Template Method 公开入口:用 self.retry_policy 包装 _do_chat()。

        Router 和外部调用方只调本方法,不需要知道重试存在。
        Provider 自己决定如何重试(用默认策略 or override 本方法)。
        """
        stream_fn = lambda: self._do_chat(
            messages, tools, tool_choice, system_prompt, cache_namespace,
        )
        yield from self._with_retry(stream_fn)

    def _with_retry(self, stream_fn):
        """默认重试包装(子类可 override 使用不同策略)。"""
        yield from retry_stream(stream_fn, self.provider_name, self.retry_policy)
```

#### 抽象 2:`ProviderRegistry`(注册中心 + 工厂)

```python
# agent_core/llm/registry.py
from typing import Type, Callable
from .providers.base import BaseProvider         # ← 改:.base → .providers.base
from .config import LLMConfig, LLMProvider


class ProviderRegistry:
    """全局单例:provider 类 → 枚举值的注册表。

    用法:
        @ProviderRegistry.register(LLMProvider.DEEPSEEK)
        class DeepSeekProvider(OpenAICompatibleProvider):
            ...

        provider = ProviderRegistry.create(LLMConfig(provider=LLMProvider.DEEPSEEK, ...))
    """

    _mapping: dict[LLMProvider, Type[BaseProvider]] = {}

    @classmethod
    def register(cls, key: LLMProvider) -> Callable[[Type[BaseProvider]], Type[BaseProvider]]:
        """装饰器:把 provider 类注册到枚举值。"""
        def decorator(klass: Type[BaseProvider]) -> Type[BaseProvider]:
            if key in cls._mapping:
                raise ValueError(f"Provider {key} 已被 {cls._mapping[key]} 注册,不能重复注册 {klass}")
            cls._mapping[key] = klass
            return klass
        return decorator

    @classmethod
    def create(cls, config: LLMConfig) -> BaseProvider:
        """工厂:根据 LLMConfig.provider 创建对应 provider 实例。"""
        klass = cls._mapping.get(config.provider)
        if klass is None:
            raise ValueError(
                f"Provider {config.provider} 未注册。"
                f"已注册:{list(cls._mapping.keys())}。"
                f"提示:import agent_core.llm.providers 触发自动注册"
            )
        return klass(config)
```

#### 抽象 3:`_extract_thinking()` 钩子(下沉到 `OpenAICompatibleProvider`)

**位置**:`agent_core/llm/providers/openai/base.py`(不是 `llm/providers/base.py`,也不是 `llm/base.py`)

**为什么放这里不放 BaseProvider**:

| 候选位置 | 评价 |
|---|---|
| ❌ `BaseProvider` | `AnthropicProvider` 不需要这个钩子(thinking 来自 final message)。在根抽象定义会让所有 provider 都"继承"一个用不上的钩子,违反 ISP |
| ✅ `OpenAICompatibleProvider` | OpenAI 协议族共享 80% 流式代码,`_process_delta()` 在这里调钩子。`ZhipuProvider` / `MiniMaxProvider` / 未来 `DeepSeekProvider` 都能 override。**钩子在它真正被调用的地方** |
| ❌ 抽到独立 `strategies/` 子包 | 过度设计,只有 1 处用(参见上文 YAGNI 决策) |

```python
# agent_core/llm/providers/openai/base.py
from typing import Generator
from ..base import BaseProvider                  # ← `..` 是 providers/,`base` 是 providers/base.py
from ...types import StreamChunk, TextDelta


class OpenAICompatibleProvider(BaseProvider):
    """OpenAI chat.completions 协议族的中间抽象类。

    完成 80% 工作(message 转换、tool 转换、流循环、tool_calls 缓冲)。
    子类差异:
        - provider_name / default_base_url / env_key
        - _extract_thinking(): thinking 提取(钩子,默认无)
        - _on_stream_end(): 流结束钩子(可选,如 splitter flush)
    """

    def _extract_thinking(self, delta) -> Generator[StreamChunk, None, None]:
        """钩子:从 OpenAI 协议 stream delta 提取 thinking。

        不同 OpenAI 兼容 provider 的 thinking 表达方式:
        - OpenAI 官方:无 thinking(默认实现)
        - GLM / DeepSeek: `delta.reasoning_content` 字段
        - MiniMax:        `<think>...</think>` 标签,嵌在 text_delta 内

        Args:
            delta: OpenAI 协议 stream 的单个 delta(chunk.choices[0].delta)

        默认无 thinking(对应 OpenAI 官方)。子类按需 override。
        """
        if False:                                # 让本函数成为 generator(子类可用 yield from 调用)
            yield  # type: ignore[unreachable]

    def _process_delta(self, delta, tool_calls_buffer) -> Generator[StreamChunk, None, None]:
        """处理单个 delta 的标准流程(钩子的调用点就在这里):

        1. 提取 thinking(钩子)
        2. text
        3. tool_calls 缓冲
        """
        yield from self._extract_thinking(delta)  # ← 钩子点
        if delta.content:
            yield StreamChunk(text_delta=TextDelta(text=delta.content))
        if delta.tool_calls:
            for tc in delta.tool_calls:
                self._accumulate_tool_call(tc, tool_calls_buffer)
```

#### 抽象 4:`RetryPolicy` + 重试机制(Provider 内部工具,**不归 Router 管**)

**位置**:`providers/_retry.py`(前缀 `_` 表明这是 providers 包的内部工具,Router 永远不 import)

**为什么不放在 `llm/retry.py`**:
- Router 已经不 import 它了——Router 完全不感知重试存在
- 留着 `_` 前缀是为了明确「这是 providers 内部细节,不是模块公共 API」
- 未来如果想升级为公共 API(比如其他模块也想用),把 `_` 去掉 + 重新评估

**为什么不让 Router 调**:
- 不同 Provider 的错误语义不同(Anthropic 429 触发策略可能和 OpenAI 不一样)
- RetryPolicy 是 Provider 自己的配置(类属性,可覆盖),Router 不该决定
- 简化 Router 职责——它只做「找到 provider → 调 chat() → 透传 chunks」

```python
# agent_core/llm/providers/_retry.py
from dataclasses import dataclass
from typing import Generator, TypeVar, Callable
import time as _time
import logging

logger = logging.getLogger("llm.providers.retry")

T = TypeVar("T")


@dataclass(frozen=True)
class RetryPolicy:
    """Provider 自己的重试策略配置(纯数据)。"""
    max_request_retry: int = 3
    max_stream_retry: int = 2
    backoff_base: float = 0.5
    retryable_status_codes: frozenset[int] = frozenset({408, 429, 500, 502, 503, 504})
    soft_retryable_status_codes: frozenset[int] = frozenset({400})
    non_retryable_status_codes: frozenset[int] = frozenset({401, 403, 404, 422})

    @classmethod
    def default(cls) -> "RetryPolicy":
        """全局默认策略(被 BaseProvider.retry_policy 引用)。"""
        return cls()


def classify_http_error(status: int, policy: RetryPolicy) -> str:
    """纯函数:HTTP 状态码 → 'retry' / 'soft_retry' / 'fail'。"""
    if status in policy.retryable_status_codes:
        return "retry"
    if status in policy.soft_retryable_status_codes:
        return "soft_retry"
    return "fail"


def retry_stream(
    stream_fn: Callable[[], Generator],
    provider_name: str,
    policy: RetryPolicy,
) -> Generator:
    """生成器:对 stream_fn() 的产出做两层重试(请求级 + 流级)。

    调用方:BaseProvider._with_retry()。
    外部不直接调用——都走 chat()。
    """
    for req_attempt in range(policy.max_request_retry + 1):
        try:
            for stream_attempt in range(policy.max_stream_retry + 1):
                try:
                    for chunk in stream_fn():
                        yield chunk
                    return
                except Exception as e:
                    if _is_stream_interruption(e) and stream_attempt < policy.max_stream_retry:
                        backoff = policy.backoff_base * (2 ** stream_attempt)
                        logger.warning(f"🔄 [{provider_name}] stream 重试 {stream_attempt + 1}/{policy.max_stream_retry}")
                        _time.sleep(backoff)
                        continue
                    raise
        except Exception as e:
            status = getattr(e, "status_code", None) or getattr(getattr(e, "response", None), "status_code", None)
            if status is not None:
                verdict = classify_http_error(int(status), policy)
                if verdict == "fail":
                    raise
                if verdict == "soft_retry" and req_attempt >= 1:
                    raise
                if req_attempt < policy.max_request_retry:
                    backoff = policy.backoff_base * (2 ** req_attempt)
                    _time.sleep(backoff)
                    continue
            raise
```

### 3.3 Provider 实现(对称!)

#### AnthropicProvider(独立成类,放在 `anthropic/base.py`)

**结构选择理由**:`AnthropicProvider` 模仿 `openai/` 的**包结构**(不是单文件)。
`anthropic/` 目录目前**只有 `base.py` 一个文件**——目前 Anthropic 协议只有 1 个
实现(`AnthropicProvider`),不需要分文件。但**目录结构本身是「可扩展架构」**:
- 未来如果加 AWS Bedrock Claude / Claude on Vertex AI 等其他 Anthropic 协议
  provider,只需要 `providers/anthropic/bedrock.py` 新增一个文件继承 `AnthropicProvider`
  或重写差异部分,**目录结构不必再次重构**
- 与 `openai/` 形成**对称**:两个协议族都是「包」,都是 `__init__.py` 暴露
  公共 API,都是 `base.py` 放主类
- 文件数与协议族的实现数解耦——「目前有 1 个实现」不决定「必须用单文件」

```python
# agent_core/llm/providers/anthropic/base.py
from typing import Generator, Optional
from ..base import BaseProvider                  # ← 改:`..` 是 providers/,`base` 是 providers/base.py
from ...config import LLMConfig
from ...types import StreamChunk, TextDelta, ThinkingDelta, ToolCallDelta, UsageStats
from ...registry import ProviderRegistry
from ...config import LLMProvider

import anthropic  # 第三方


@ProviderRegistry.register(LLMProvider.ANTHROPIC)
class AnthropicProvider(BaseProvider):
    """Anthropic Claude 流式 Provider。

    关注点:
    - system 走 kwargs["system"] 顶层参数
    - thinking 走 content block(在 final message.content 中,**不在 stream delta 中**)
    - tools 走 Anthropic 原生格式
    - cache_control 走 cache_namespace

    关于 thinking 提取(本类不通过 _extract_thinking 钩子):
      Anthropic 协议的 thinking 表达方式和 OpenAI 兼容族**根本不同**——
      它是 final message.content 里的独立 block(`block.type == "thinking"`),
      不在 stream delta 中增量到达。
      所以本类**直接**在 chat() 内部遍历 final.content 提取 thinking,
      **不**通过 _extract_thinking 钩子——因为钩子是 OpenAI 协议族的实现细节,
      接收的参数是 stream delta(本类根本没有这种数据),不应继承。

      BaseProvider 不预置 _extract_thinking,正是为了让这种协议差异显式化:
      每个 provider 自己处理自己协议的 thinking 表达方式,不共享虚假抽象。
    """

    provider_name = "anthropic"
    default_base_url = None
    env_key = "ANTHROPIC_API_KEY"

    def __init__(self, config: LLMConfig):
        super().__init__(config)
        self._client = None  # lazy

    @property
    def client(self) -> "anthropic.Anthropic":
        if self._client is None:
            from ...config import config as _config      # ← 3 个点:anthropic/ → providers/ → llm/ → agent_core/
            api_key = self.config.api_key or _config.anthropic_api_key
            self._client = anthropic.Anthropic(api_key=api_key)
        return self._client

    def _do_chat(
        self,
        messages: list[dict],
        tools: Optional[list[dict]] = None,
        tool_choice: Optional[str] = None,
        system_prompt: Optional[str] = None,
        cache_namespace: Optional[str] = None,
    ) -> Generator[StreamChunk, None, None]:
        """实现 _do_chat:实际协议调用。chat() 继承自 BaseProvider,自动用
        self.retry_policy 包装本方法(见 BaseProvider.chat() Template Method)。"""
        kwargs = self._build_kwargs(messages, tools, tool_choice, system_prompt, cache_namespace)
        yield from self._stream(kwargs)
    # chat() 不 override,继承自 BaseProvider(自动 retry 包装 _do_chat)

    def _build_kwargs(self, messages, tools, tool_choice, system_prompt, cache_namespace) -> dict:
        kwargs = {
            "model": self.config.model,
            "max_tokens": self.config.max_tokens,
            "messages": messages,
        }
        if tools:
            kwargs["tools"] = tools
            if tool_choice is not None:
                # Provider 内部处理 tool_choice 映射(以前在 router 里)
                kwargs["tool_choice"] = {"type": tool_choice if tool_choice in ("auto", "any") else tool_choice}
        if self.config.temperature > 0:
            kwargs["temperature"] = self.config.temperature

        # system prompt 注入(Anthropic 走顶层参数)
        if system_prompt:
            if cache_namespace:
                kwargs["system"] = [{
                    "type": "text",
                    "text": system_prompt,
                    "cache_control": {"type": "ephemeral"},
                }]
            else:
                kwargs["system"] = system_prompt

        # tools 上打 cache_control 锚点
        # 锚点放最后一个 tool 的原因:Anthropic protocol 限制每个请求最多 4 个
        # cache_control block,且通常 tools 列表变更频率高于 system —— 锚在末尾
        # 可保证「前面的 tools 全被 cache」(前面的 tool 在 prompt 中位于锚点之前,
        # Anthropic 会把锚点前所有内容作为 cache prefix)。如未来有更细粒度
        # 缓存需求(每个 tool 独立 cache),应改成下推到每 tool 上,但当前 1 个
        # 锚点已能满足「重新编译 tools 列表」场景。
        if cache_namespace and tools:
            tools = [dict(t) for t in tools]
            tools[-1] = {**tools[-1], "cache_control": {"type": "ephemeral"}}
            kwargs["tools"] = tools

        # thinking blocks
        if self.config.thinking and self.config.thinking.enabled:
            kwargs["thinking"] = {
                "type": "enabled",
                "budget_tokens": self.config.thinking.budget_tokens,
            }
        return kwargs

    def _stream(self, kwargs) -> Generator[StreamChunk, None, None]:
        with self.client.messages.stream(**kwargs) as stream:
            for text_delta in stream.text_stream:
                if text_delta:
                    yield StreamChunk(text_delta=TextDelta(text=text_delta))

            final = stream.get_final_message()
            for block in final.content:
                if block.type == "thinking":
                    yield StreamChunk(thinking_delta=ThinkingDelta(thinking=block.thinking, is_final=True))
                elif block.type == "tool_use":
                    yield StreamChunk(tool_call=ToolCallDelta(
                        tool_name=block.name, tool_input=dict(block.input),
                        tool_use_id=block.id, is_final=True,
                    ))

            yield StreamChunk(usage=UsageStats(
                input_tokens=final.usage.input_tokens,
                output_tokens=final.usage.output_tokens,
                thinking_tokens=getattr(final.usage, "thinking_tokens", 0),
                cached_tokens=getattr(final.usage, "cache_read_input_tokens", 0),
            ))
            if final.stop_reason:
                yield StreamChunk(stop_reason=final.stop_reason)
```

#### OpenAICompatibleProvider(继续作为子抽象)

```python
# agent_core/llm/providers/openai/base.py
from typing import Generator, Optional
import json
from ..base import BaseProvider                  # ← `..` 是 providers/,`base` 是 providers/base.py
from ...config import LLMConfig
from ...types import StreamChunk, TextDelta, ToolCallDelta, UsageStats


class OpenAICompatibleProvider(BaseProvider):
    """所有 OpenAI 协议族的 provider 的中间抽象层。

    完成 80% 的工作(message 转换、tool 转换、流循环、tool_calls 缓冲)。
    子类差异:
        - provider_name / default_base_url / env_key
        - _extract_thinking(): thinking 提取钩子(默认无,见下)
        - _on_stream_end(): 流结束钩子(可选,如 splitter flush)
    """

    provider_name = ""                # 占位符,所有具体子类必须 override
    default_base_url: Optional[str] = None
    env_key: str = ""

    def __init__(self, config: LLMConfig):
        super().__init__(config)
        self._client = None

    # ── thinking 提取钩子(本类的实现细节,不下沉到 BaseProvider) ──
    def _extract_thinking(self, delta) -> Generator[StreamChunk, None, None]:
        """钩子:从 OpenAI 协议 stream delta 提取 thinking。

        不同 OpenAI 兼容 provider 的 thinking 表达方式:
        - OpenAI 官方:无 thinking(默认实现)
        - GLM / DeepSeek: `delta.reasoning_content` 字段
        - MiniMax:        `<think>...</think>` 标签,嵌在 text_delta 内

        Args:
            delta: OpenAI 协议 stream 的单个 delta(chunk.choices[0].delta)

        默认无 thinking(对应 OpenAI 官方)。子类按需 override。
        """
        if False:                                # 让本函数成为 generator(子类可用 yield from 调用)
            yield  # type: ignore[unreachable]

    def _resolve_api_key(self) -> str:
        from ...config import config as _config          # ← 3 个点:openai/ → providers/ → llm/ → agent_core/
        return self.config.api_key or getattr(_config, self.env_key.lower(), "")

    @property
    def client(self):
        if self._client is None:
            import openai
            self._client = openai.OpenAI(
                api_key=self._resolve_api_key(),
                base_url=self.config.base_url or self.default_base_url,
            )
        return self._client

    def _do_chat(
        self,
        messages: list[dict],
        tools: Optional[list[dict]] = None,
        tool_choice: Optional[str] = None,
        system_prompt: Optional[str] = None,
        cache_namespace: Optional[str] = None,
    ) -> Generator[StreamChunk, None, None]:
        # 1. 构造请求(Provider 内完成 system 注入到 messages)
        final_messages = self._inject_system(messages, system_prompt)
        kwargs = self._build_kwargs(final_messages, tools, tool_choice)
        # 2. 启动流
        yield from self._stream_with_buffer(kwargs)
    # chat() 不 override,继承自 BaseProvider(自动 retry 包装 _do_chat)

    def _inject_system(self, messages, system_prompt):
        if not system_prompt:
            return messages
        return [{"role": "system", "content": system_prompt}] + [m for m in messages if m["role"] != "system"]

    def _build_kwargs(self, messages, tools, tool_choice) -> dict:
        kwargs = {
            "model": self.config.model,
            "messages": self._convert_messages(messages),
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if tools:
            kwargs["tools"] = self._convert_tools(tools)
            if tool_choice is not None:
                kwargs["tool_choice"] = tool_choice
        if self.config.temperature > 0:
            kwargs["temperature"] = self.config.temperature
        return kwargs

    def _process_delta(self, delta, tool_calls_buffer) -> Generator[StreamChunk, None, None]:
        """处理单个 delta 的标准流程(钩子的实际调用点):

        1. 提取 thinking(钩子调用点)
        2. text
        3. tool_calls 缓冲

        子类通过 override _extract_thinking() 影响第 1 步。
        """
        yield from self._extract_thinking(delta)            # ← 钩子点
        if delta.content:
            yield StreamChunk(text_delta=TextDelta(text=delta.content))
        if delta.tool_calls:
            for tc in delta.tool_calls:
                self._accumulate_tool_call(tc, tool_calls_buffer)

    def _stream_with_buffer(self, kwargs) -> Generator[StreamChunk, None, None]:
        """启动 OpenAI 流,把每个 chunk 拆成 delta 走 _process_delta 处理。

        注意:这是钩子链的「末端」——所有 OpenAI 兼容 provider 的 stream chunk
        都会经过这里,再分发到 _process_delta(里面再调 _extract_thinking 钩子)。
        MiniMaxProvider 可以 override 本方法在流开始/结束时插入 splitter 行为。
        """
        tool_calls_buffer: list = []
        for chunk in self.client.chat.completions.create(**kwargs):
            for choice in chunk.choices:
                delta = choice.delta
                yield from self._process_delta(delta, tool_calls_buffer)
            # 某些 chunk 不带 choices(纯 usage chunk),把 usage 透传
            if getattr(chunk, "usage", None):
                yield StreamChunk(usage=UsageStats(
                    input_tokens=chunk.usage.prompt_tokens,
                    output_tokens=chunk.usage.completion_tokens,
                ))
        # 流结束:flush 缓冲的 tool_calls(走 on_stream_end 钩子)
        yield from self._finalize_tool_calls(tool_calls_buffer)
        yield from self._on_stream_end()

    def _on_stream_end(self) -> Generator[StreamChunk, None, None]:
        """流结束钩子:默认 no-op。MiniMaxProvider override 此方法 flush
        _ThinkTagSplitter 残留 buffer(不丢任何字符)。"""
        if False:
            yield  # 让它成为 generator(子类可用 yield from 调用)

    # _convert_messages / _convert_tools / _accumulate_tool_call /
    # _finalize_tool_calls 与现状相同(略)
    ...
```

#### 具体子类(每个只 8-15 行)

```python
# agent_core/llm/providers/openai/openai.py
from .base import OpenAICompatibleProvider
from ...registry import ProviderRegistry
from ...config import LLMProvider


@ProviderRegistry.register(LLMProvider.OPENAI)
class OpenAIProvider(OpenAICompatibleProvider):
    """OpenAI 官方 GPT(无 thinking)。"""
    provider_name = "openai"
    default_base_url = None
    env_key = "OPENAI_API_KEY"
    # _extract_thinking 用基类默认(无 thinking)


# agent_core/llm/providers/openai/zhipu.py
@ProviderRegistry.register(LLMProvider.ZHIPU)
class ZhipuProvider(OpenAICompatibleProvider):
    """智谱 GLM:thinking 走 reasoning_content 字段(流式逐块到达)。"""
    provider_name = "zhipu"
    default_base_url = "https://open.bigmodel.cn/api/coding/paas/v4"
    env_key = "ZHIPU_API_KEY"

    def _extract_thinking(self, delta):
        # GLM 把 thinking 放在独立 reasoning_content 字段,与 text 并行流式到达
        if hasattr(delta, "reasoning_content") and delta.reasoning_content:
            yield StreamChunk(thinking_delta=ThinkingDelta(thinking=delta.reasoning_content))


# agent_core/llm/providers/openai/minimax.py
@ProviderRegistry.register(LLMProvider.MINIMAX)
class MiniMaxProvider(OpenAICompatibleProvider):
    """MiniMax (M3):thinking 可能在 reasoning_content 或 <think> 标签里。

    MiniMax 协议把 thinking 包在 `<think>...</think>` 标签里,嵌在普通 text_delta
    内输出。本类内嵌一个 _ThinkTagSplitter 状态机把这种"伪装成文本的 thinking"
    实时切出来。

    设计选择:状态机内嵌为 nested class,而不是抽到独立 thinking_splitter.py。
    理由:YAGNI —— 只有 MiniMax 走标签,Anthropic/GLM/OpenAI 都用各自协议的
    thinking 表达方式,没有横向复用场景。嵌套类仍然可以独立单测
    (MiniMaxProvider._ThinkTagSplitter() 直接 new)。
    """

    provider_name = "minimax"
    default_base_url = "https://api.minimaxi.com/v1"
    env_key = "MINIMAX_API_KEY"

    # ── 嵌套状态机:MiniMax 协议特有 ─────────────────────────────────
    class _ThinkTagSplitter:
        """`<think>...</think>` 标签流式切分状态机。

        状态机:
            NORMAL ──(见 <think>)──▶ THINKING
            THINKING ──(见 </think>)──▶ NORMAL

        关键能力:
        1. 标签跨 chunk 切片:`<thi` + `nk>...` + `</thin` + `king>`
           都能正确解析(用 _buf 缓冲未完成的部分)
        2. 多对标签:`<think>a</think> hello <think>b</think> world`
        3. 流末尾收尾:flush() 兜底未完成的缓冲区
        4. 失败降级:任何异常路径都保留原文(不丢内容)
        """
        OPEN_TAG = "<think>"
        CLOSE_TAG = "</think>"
        STATE_NORMAL = "normal"
        STATE_THINKING = "thinking"

        def __init__(self) -> None:
            self._state = self.STATE_NORMAL
            self._buf = ""

        def feed(self, text: str) -> list[StreamChunk]:
            if not text:
                return []
            s = self._buf + text
            self._buf = ""
            out: list = []
            i = 0
            while i < len(s):
                tag = self.OPEN_TAG if self._state == self.STATE_NORMAL else self.CLOSE_TAG
                idx = s.find(tag, i)
                if idx == -1:
                    # 没找到完整标签 — 保留最后 6 字符在 _buf(可能是不完整标签)
                    tail = s[i:]
                    keep = len(tag) - 1
                    if len(tail) > keep:
                        out.append(self._emit(tail[:-keep]))
                        self._buf = tail[-keep:]
                    else:
                        self._buf = tail
                    break
                else:
                    if idx > i:
                        out.append(self._emit(s[i:idx]))
                    i = idx + len(tag)
                    self._state = (
                        self.STATE_THINKING
                        if self._state == self.STATE_NORMAL
                        else self.STATE_NORMAL
                    )
                    # </think> 后跳过一个换行(MiniMax 实测紧跟 \n)
                    if (self._state == self.STATE_NORMAL
                            and i < len(s)
                            and s[i] == "\n"):
                        i += 1
            return out

        def flush(self) -> list[StreamChunk]:
            """流结束时调用,把残留 buffer 兜底输出(不丢内容)"""
            if not self._buf:
                return []
            chunk = self._emit(self._buf)
            self._buf = ""
            return [chunk]

        def _emit(self, text: str) -> StreamChunk:
            if self._state == self.STATE_THINKING:
                return StreamChunk(thinking_delta=ThinkingDelta(thinking=text))
            return StreamChunk(text_delta=TextDelta(text=text))

    # ── Provider 主逻辑 ───────────────────────────────────────────────
    def __init__(self, config: LLMConfig):
        super().__init__(config)
        # splitter 状态机:每 chat() 重置一次,避免上次流残留
        self._think_splitter = self._ThinkTagSplitter()

    def _extract_thinking(self, delta):
        # 优先 reasoning_content(若 model 支持,GLM 风格)
        if hasattr(delta, "reasoning_content") and delta.reasoning_content:
            yield StreamChunk(thinking_delta=ThinkingDelta(thinking=delta.reasoning_content))
        # 否则 text_delta 走 splitter 切 <think> 标签
        elif delta.content:
            yield from self._think_splitter.feed(delta.content)

    def _stream_with_buffer(self, kwargs):
        """Override:每 chat() 重置 splitter,避免上次流残留。"""
        self._think_splitter = self._ThinkTagSplitter()
        yield from super()._stream_with_buffer(kwargs)

    def _on_stream_end(self):
        """流结束 flush splitter 残留 buffer(不丢任何字符)。"""
        yield from self._think_splitter.flush()
```

### 3.4 瘦身后的 Router(只做调度,不管重试)

```python
# agent_core/llm/router.py (目标版本,~50 行)
from typing import Generator, Optional
from .config import LLMConfig
from .types import StreamChunk
from .providers.base import BaseProvider        # 类型提示用
from .registry import ProviderRegistry


class LLMRouter:
    """多厂商 LLM 统一路由——瘦身后只做 2 件事:

    1. 根据 LLMConfig.provider 创建具体 Provider(委托给 Registry)
    2. 把 system_prompt_override 规范化后,直接调 Provider.chat()

    **不**管重试:重试是 Provider 自己的事(由 BaseProvider.chat() 包装 _do_chat())。
    **不**管错误分类:Provider 自己知道哪些错误该重试。
    **不**管 thinking 提取:Provider 自己的协议细节。
    """

    def __init__(self, config: LLMConfig):
        self.config = config
        self._provider = None

    @property
    def provider(self) -> BaseProvider:
        if self._provider is None:
            self._provider = ProviderRegistry.create(self.config)
        return self._provider

    def chat(
        self,
        messages: list[dict],
        tools: Optional[list[dict]] = None,
        system_prompt_override: Optional[str] = None,
        tool_choice: Optional[str] = None,
        cache_namespace: Optional[str] = None,
    ) -> Generator[StreamChunk, None, None]:
        # 1. 提取/规范化 system 消息(跨 provider 通用逻辑,放在 Router)
        system_prompt, filtered_messages = self._resolve_system(messages, system_prompt_override)
        # 2. 多态分发(完全不知道具体是哪个 provider,也不管重试)
        yield from self.provider.chat(
            messages=filtered_messages,
            tools=tools,
            tool_choice=tool_choice,
            system_prompt=system_prompt,
            cache_namespace=cache_namespace,
        )

    def _resolve_system(self, messages, override):
        """单一权威的 system 消息解析逻辑(以前在 router 里写了 2 遍)。

        这一步留在 Router 是因为 system 提取是「调用方语义」(Fork 模式 override
        vs 普通提取),不是「provider 协议语义」。每个 provider 拿到 system 后
        怎么注入到自己的 kwargs 里(Anthropic 顶层 vs OpenAI 消息内),那是
        Provider 自己的事——见 OpenAICompatibleProvider._inject_system /
        AnthropicProvider._build_kwargs。
        """
        if override is not None:
            # 仿照 Claude Code createCacheSafeParams:Fork 模式 override 优先
            filtered = [m for m in messages if m["role"] != "system"]
            return override, filtered
        # 非 Fork 模式:从 messages 提取第一条 system
        for m in messages:
            if m["role"] == "system":
                return m["content"], [x for x in messages if x["role"] != "system"]
        return None, list(messages)
```

### 3.5 `__init__.py` 触发自动注册 + 向后兼容

```python
# agent_core/llm/__init__.py
"""Public API for LLM module.

迁移说明:历史代码仍可继续 from agent_core.llm.router import ...
所有类型/类已从 router.py 抽到 types.py / base.py,但 router.py 仍
re-export 它们,保持 100% 向后兼容。
"""
# 触发自动注册:import 此包即注册所有内置 provider
from . import providers  # noqa: F401  # ← 这一步会加载 providers/base.py(BaseProvider)

# 公共 API
from .types import StreamChunk, TextDelta, ThinkingDelta, ToolCallDelta, UsageStats
from .config import LLMConfig, LLMProvider, LLMModel, ThinkingConfig
from .providers.base import BaseProvider          # ← 改:.base → .providers.base
from .router import LLMRouter
from .registry import ProviderRegistry

# RetryPolicy 不再是公共 API——它是 Provider 内部细节
# 想看:from agent_core.llm.providers._retry import RetryPolicy  (下划线 = 内部)
# thinking_splitter 状态机已内嵌到 MiniMaxProvider,不再 import

__all__ = [
    "StreamChunk", "TextDelta", "ThinkingDelta", "ToolCallDelta", "UsageStats",
    "LLMConfig", "LLMProvider", "LLMModel", "ThinkingConfig",
    "BaseProvider", "LLMRouter", "ProviderRegistry",
]
```

```python
# agent_core/llm/providers/anthropic/__init__.py
"""Anthropic 协议族 provider 集合。

目前只有 AnthropicProvider 一家(`base.py`)。如果未来加 AWS Bedrock
Claude / Vertex Claude 等,只需在本包新增 bedrock.py / vertex.py,
再在 __all__ 列出,无需改动外部 import 路径。
"""
from .base import AnthropicProvider

__all__ = ["AnthropicProvider"]
```

```python
# agent_core/llm/providers/openai/__init__.py
"""OpenAI chat.completions 协议族 provider 集合。

目前包含 OpenAIProvider / ZhipuProvider / MiniMaxProvider 三家。

类名说明:本包叫 `openai/`,但中间抽象类仍叫 `OpenAICompatibleProvider`
(不是 `OpenAIProvider`)——因为 "compatible" 明确表达「OpenAI chat.completions
协议兼容」语义,涵盖 Zhipu / MiniMax / DeepSeek 等非 OpenAI 但协议相同的
provider;而 `OpenAIProvider` 是 OpenAI 官方这家具体 provider,语义不同。
两个名字不能合并——合并后 `OpenAIProvider` 既能指抽象又能指官方,歧义更大。
"""
from .base import OpenAICompatibleProvider
from .openai import OpenAIProvider
from .zhipu import ZhipuProvider
from .minimax import MiniMaxProvider

__all__ = [
    "OpenAICompatibleProvider",
    "OpenAIProvider",
    "ZhipuProvider",
    "MiniMaxProvider",
]
```

> 注:`router.py` 完整内容见 §3.4。`router.py` 不再 re-export `_ThinkTagSplitter`(已内嵌到 `MiniMaxProvider`),但**主入口**符号(`LLMRouter` / `LLMConfig` / `StreamChunk` / 等)仍 re-export,以满足 §2.2 目标 5。

---

## 4. 对照表:重构前 → 重构后

### 4.1 加第 4 个 OpenAI 兼容 provider(以 DeepSeek 为例)

| 步骤 | 重构前 | 重构后 |
|---|---|---|
| 1. 写新 provider 类 | 改 `openai_compatible.py:323-341` 工厂 mapping + 新建类 | **新建 `providers/openai/deepseek.py` 一个文件** |
| 2. 写代码量 | ~20 行(provider 类)+ 修改工厂 dict + 改 router.py 的 `elif provider in (...)` | **~10 行,只写类** |
| 3. 改动的文件 | 2-3 个 | **1 个** |
| 4. 风险 | 改 `router.py` 的 `if/elif` 容易引入 if-else 链错 | **新文件独立,Router 一行不动** |

### 4.2 「system 消息应该放哪」——一类典型问题

| 视角 | 重构前 | 重构后 |
|---|---|---|
| 代码位置 | router.py:407-417(Anthropic) + 431-434(OpenAI)——**两处分别写** | router.py `_resolve_system()` 一处;Provider 自己决定怎么注入到 kwargs |
| 改一处影响另一处 | 是 | 否(Provider 内部行为) |
| 单测 | 测 router 的 system 提取必须 mock 整个 anthropic client | 测 `_resolve_system` 是纯函数,无依赖 |

### 4.3 重试策略调整(由 Provider 内部负责)

| 场景 | 重构前 | 重构后 |
|---|---|---|
| 改「全局重试 3 次 → 5 次」 | 改 router.py 顶部常量,影响所有 provider | 改 `BaseProvider.retry_policy` 默认值,所有 Provider 一起改 |
| 给 GLM 单独配 5 次 | 改 router.py 加 if-elif,污染主路径 | `class ZhipuProvider(OpenAICompatibleProvider): retry_policy = RetryPolicy(max_request_retry=5, ...)` 一行覆盖 |
| 给 Anthropic 不同的 401 处理 | 改 router.py 的错误分类 if-elif | `class AnthropicProvider(BaseProvider):` override `chat()` 自定义重试逻辑 |
| 测试重试 | 必须 import 整个 router + mock 完整 LLM 调用 | `retry_stream()` 是纯生成器,给个 mock `stream_fn` 即可单测;或直接测 `provider._with_retry()` |
| 临时关闭某个 provider 的重试 | 改 router.py 全局配置,影响所有 provider | 构造时 `LLMConfig(retry_policy=RetryPolicy(max_request_retry=0))` 覆盖 |

---

## 5. SOLID 原则自检

| 原则 | 落实情况 |
|---|---|
| **SRP** | ✅ `types.py`(纯数据)/ `providers/_retry.py`(Provider 内部重试工具)/ `providers/`(协议适配)/ `router.py`(只做调度) 各管一件事 |
| **OCP** | ✅ 新增 provider = 新建文件 + `@register_provider` 装饰器;Router、Registry、retry 都不改 |
| **LSP** | ✅ 所有 `BaseProvider` 子类可替换:`router.provider` 永远只调 `BaseProvider.chat()`,不看具体类型 |
| **ISP** | ✅ `BaseProvider` **只暴露 `chat()`** —— 不预置任何协议特定的钩子(thinking 提取等)。`_extract_thinking()` 钩子下沉到 `OpenAICompatibleProvider`(它真正被调用的地方),AnthropicProvider 完全不继承,免得"继承一个用不上的钩子" |
| **DIP** | ✅ Router 依赖 `BaseProvider` 抽象 + `ProviderRegistry` 注册中心,完全不 import 具体 provider 类 / `RetryPolicy` / `retry_stream`(这些是 Provider 内部细节) |

### 5.1 多态性自检

- **运行时分发**:`router.provider` 调用时由 Registry 决定具体类——**真多态**
- **Template Method 多态(下沉到正确层级)**:`_extract_thinking()` 钩子**只**在 `OpenAICompatibleProvider` 定义(默认 no-op),`ZhipuProvider` / `MiniMaxProvider` / 未来 `DeepSeekProvider` override。`AnthropicProvider` **不继承**这个钩子 —— 它的 thinking 处理是另一个完全不同的机制(final message content blocks),**强行共享抽象反而是错误的**
- **Provider 内多态**:`RetryPolicy` 是 dataclass,`BaseProvider.retry_policy` 类属性提供默认值,各 Provider 可一行覆盖(`class ZhipuProvider(OpenAICompatibleProvider): retry_policy = RetryPolicy(max_request_retry=5, ...)`)

---

## 6. 兼容性 / 迁移计划

### 6.1 兼容性矩阵

| 老代码 | 现状 | 重构后 |
|---|---|---|
| `from agent_core.llm.router import LLMRouter, LLMConfig, LLMProvider` | ✅ 可用 | ✅ 仍可用(router.py re-export) |
| `from agent_core.llm.router import StreamChunk, TextDelta, ThinkingDelta, ToolCallDelta, UsageStats` | ✅ 可用 | ✅ 仍可用 |
| `from agent_core.llm.router import _ThinkTagSplitter` | ✅ 可用 | ❌ **不再 re-export**(老代码需改为 `from agent_core.llm.providers.openai.minimax import MiniMaxProvider` 然后用 `MiniMaxProvider._ThinkTagSplitter()`) |
| `from agent_core.llm.openai_compatible import OpenAICompatibleProvider, OpenAIProvider, ...` | ✅ 可用 | ⚠️ 需保留 shim,或新代码改用 `from agent_core.llm.providers.openai import ...`(`openai_compatible.py` 已被拆为 `providers/openai/` 子包) |
| `from agent_core.llm.thinking_splitter import _ThinkTagSplitter` | ✅ 可用 | ❌ **`thinking_splitter.py` 彻底删除**(全项目只有 3 处引用,全部直接迁移到 `MiniMaxProvider._ThinkTagSplitter`) |
| `from agent_core.llm.base import BaseProvider` | ✅ 可用 | ❌ **`base.py` 已迁到 `providers/base.py`**,新代码用 `from agent_core.llm.providers.base import BaseProvider` 或 `from agent_core.llm import BaseProvider`(后者 re-export) |

### 6.2 迁移步骤(分阶段,每阶段独立可发布)

**阶段 1:抽离 types + retry(零行为变化)**
- 新建 `types.py`,把 `StreamChunk` / `TextDelta` / `ThinkingDelta` / `ToolCallDelta` / `UsageStats` 搬过去
- 新建 `providers/_retry.py`,把 `_classify_http_error` / `_is_stream_interruption_error` / `_stream_with_retry` 搬过去(Router **不** import 这个文件,只 Provider 用)
- `router.py` 用 `from .types import ...` 引入,**不再** import retry
- **验证**:跑现有测试,行为应完全不变

**阶段 2:抽出 `BaseProvider` + 抽 AnthropicProvider 成类(包结构)**
- 新建 `providers/base.py` 定义 `BaseProvider`(**放在 `providers/` 下,而不是 `llm/base.py`**,与其他 `base.py` 对称)
- 更新所有 `BaseProvider` 引用方:
  - `llm/__init__.py`: `from .base import BaseProvider` → `from .providers.base import BaseProvider`
  - `llm/router.py`: `from .base import BaseProvider` → `from .providers.base import BaseProvider`
  - `llm/registry.py`: `from .base import BaseProvider` → `from .providers.base import BaseProvider`
  - `llm/providers/anthropic/base.py`: `from ...base import BaseProvider` → `from ..base import BaseProvider`(少 1 级)
  - `llm/providers/openai/base.py`: 同上
- 删除原 `llm/base.py`(不留 shim,与 `thinking_splitter.py` 处理一致)
- 新建 `providers/anthropic/` **包**(不是单文件)
  - `providers/anthropic/__init__.py` 触发 `from .base import AnthropicProvider`,re-export 给外部
  - `providers/anthropic/base.py` 把 `_chat_anthropic` 搬过去,定义 `AnthropicProvider`
- `LLMRouter.chat()` 中 `if provider == "anthropic"` 分支改为 `provider=self._get_provider().chat(...)`
- `self._get_anthropic_client()` 移到 `AnthropicProvider` 内部
- **结构说明**:虽然目前 `anthropic/` 包下**只有 `base.py` 一个文件**,但用「包」而非「单文件」,是为了和 `openai/` 对称,并为未来扩展(AWS Bedrock Claude / Vertex Claude 等)预留目录结构,不必再重构
- **验证**:`grep -r "from .*base import BaseProvider" agent_core/llm/` 检查所有 import 路径都更新;Anthropic 流式调用 smoke test,thinking 块、tool_use、cache_control 行为不变

**阶段 3:`ProviderRegistry` + 迁移 OpenAI 兼容 provider**
- 新建 `registry.py`
- 改 `openai_compatible.py` 的 3 个类用 `@ProviderRegistry.register(...)` 装饰
- `create_openai_compatible_provider` 工厂改为 `ProviderRegistry.create()`
- `LLMRouter._get_openai_provider` 改为 `ProviderRegistry.create(self.config)`
- **验证**:3 个 OpenAI 兼容 provider 行为不变

**阶段 4:thinking 提取改为钩子方法 + 状态机内嵌**
- `OpenAICompatibleProvider._process_delta` 改为 `yield from self._extract_thinking(delta)`(在 `_stream_with_buffer` 里调用,见 §3.3 `_stream_with_buffer` 实现)
- `ZhipuProvider` / `MiniMaxProvider` override `_extract_thinking()`
- **状态机内嵌到 `MiniMaxProvider` 作为 nested class `MiniMaxProvider._ThinkTagSplitter`**
- **彻底删除 `thinking_splitter.py` 文件**(不留 shim)
- **更新所有引用方**:
  - `agent_core/llm/router.py` 删掉 `_ThinkTagSplitter` re-export
  - `agent_core/llm/openai_compatible.py:35, 291, 299` 该文件整体被 `providers/openai/minimax.py` 替换
  - `agent_core/llm/test_router.py:8` import 路径改为:
    ```python
    from agent_core.llm.providers.openai.minimax import MiniMaxProvider
    ```
    所有 `_ThinkTagSplitter()` 调用改为 `MiniMaxProvider._ThinkTagSplitter()`
- **验证**:thinking 流式顺序、跨 chunk 切片、flush() 行为不变 + `grep -r "_ThinkTagSplitter\|thinking_splitter"` 0 hit(除新位置)

**阶段 5:`providers/` 子包化 + 触发自动注册 + `openai_compatible/` 改名 `openai/`**
- 把 `openai_compatible.py` 拆成 `providers/openai/` 子包(包名从 `openai_compatible` 改为 `openai`,与 `anthropic/` 风格对称)
  - 旧: `agent_core/llm/openai_compatible.py`(单文件,3 个类)
  - 新: `agent_core/llm/providers/openai/{base.py, openai.py, zhipu.py, minimax.py, __init__.py}`
- **类名保留 `OpenAICompatibleProvider`**:虽然包名从 `openai_compatible` 缩短为 `openai`,但中间抽象类仍叫 `OpenAICompatibleProvider`——"compatible" 表达「OpenAI chat.completions 协议兼容」语义,涵盖 Zhipu / MiniMax / DeepSeek 等非 OpenAI 协议相同 provider;`OpenAIProvider` 是 OpenAI 官方这家具体 provider。两者语义不同不能合并
- `providers/__init__.py` 触发自动注册
- `LLMRouter.__init__` 不再需要 `_get_anthropic_client` / `_get_openai_provider`,统一靠 `ProviderRegistry`
- **验证**:全部测试通过 + 5 个 provider(Anthropic / OpenAI / Zhipu / MiniMax / 新加的 DeepSeek demo)全跑通 + `grep -r "_ThinkTagSplitter\|thinking_splitter\|openai_compatible" agent_core/llm/` 0 hit(除文档本身)

### 6.3 风险与回滚

| 风险 | 缓解 |
|---|---|
| `from .llm.openai_compatible import X` 外部引用 | 保留 shim:在 `llm/openai_compatible.py` 留一行 `from .providers.openai import *` 转发(仅这一个 shim 是必要的) |
| `from .llm.thinking_splitter import _ThinkTagSplitter` 外部引用 | **不留 shim**,全项目只有 3 处引用(router.py / openai_compatible.py / test_router.py),全部直接更新到新位置;验证用 `grep -r` 确认 0 hit |
| Anthropic cache_control / thinking 块行为偏差 | 阶段 2 单独跑 Anthropic 集成测试,对比 thinking token 数、cache hit 率 |
| 重试策略行为变化 | 阶段 1 保留完全相同的 `RETRYABLE_STATUS_CODES` / `MAX_STREAM_RETRY` 常量,只是搬家 |
| 循环 import | `types.py` 不 import 任何其他 llm 子模块 |

---

## 7. 收益总结

### 7.1 代码量

| 指标 | 重构前 | 重构后 | 变化 |
|---|---|---|---|
| `router.py` 行数 | 694 | **~50**(只调度) | **-93%** |
| 加第 N 个 OpenAI 兼容 provider | ~20 行 + 改 2 处工厂 + 改 router | **~10 行 + 0 改** | -75% |
| 重复代码(message 转换、tool 转换、流循环) | 2 套(Anthropic vs OpenAI) | 1 套(`OpenAICompatibleProvider`)+ 1 套(Anthropic 协议天然不同) | -50% |
| 重试逻辑内聚度 | 散落 router 顶部,污染主路径 | **Provider 内部**(`_retry.py` 是 providers 包内部工具,Rouoter 完全不 import) | ↑↑↑ |

### 7.2 可维护性

- **新增 provider** 只触达 1 个新文件
- **改全局重试策略** 只触达 `BaseProvider.retry_policy` 默认值(或 `_retry.py:RetryPolicy.default()`)
- **改单个 provider 的重试** 只触达该 provider 子类覆盖 `retry_policy` 或 override `chat()`
- **改某个 provider 的 thinking 提取逻辑** 只触达该 provider 子类的 `_extract_thinking()` 覆盖
- **改 system 消息处理** 只触达 `OpenAICompatibleProvider._inject_system()`(或个别 override)
- **改 usage 统计** 只触达 `types.py:UsageStats`

### 7.3 可测性

| 测试目标 | 重构前需要 mock 什么 | 重构后需要 mock 什么 |
|---|---|---|
| 重试逻辑 | `LLMRouter` + 一个 `chat()` 生成器 | 一个 `stream_fn` lambda |
| Thinking 提取(MiniMax) | `MiniMaxProvider` 完整对象 + `_ThinkTagSplitter` 状态 | 子类化 `MiniMaxProvider` 后只测 `_extract_thinking()` 一个方法 |
| Think 标签切分状态机(独立) | import `_ThinkTagSplitter` + 构造 | `MiniMaxProvider._ThinkTagSplitter()` 直接 new(嵌套类仍可独立单测) |
| system 提取 | 整个 `LLMRouter.chat()` + 模拟 provider 返回 | `_resolve_system()` 纯函数 |
| 新 provider 集成 | 改 router.py + 改测试 | 写一个 Provider 类 + 一个测试 |

### 7.4 扩展能力

未来可低成本新增的 provider,例如:

```python
# 加 DeepSeek(完整 5 行,OpenAI 协议 + reasoning_content 字段)
# 文件:agent_core/llm/providers/openai/deepseek.py
from .base import OpenAICompatibleProvider
from ...registry import ProviderRegistry
from ...config import LLMProvider


@ProviderRegistry.register(LLMProvider.DEEPSEEK)
class DeepSeekProvider(OpenAICompatibleProvider):
    provider_name = "deepseek"
    default_base_url = "https://api.deepseek.com/v1"
    env_key = "DEEPSEEK_API_KEY"

    def _extract_thinking(self, delta):
        # DeepSeek 走 reasoning_content 字段,与 GLM 风格一致
        if hasattr(delta, "reasoning_content") and delta.reasoning_content:
            yield StreamChunk(thinking_delta=ThinkingDelta(thinking=delta.reasoning_content))
```

```python
# 加 Google Gemini(走完全不同的协议,需自己实现 chat())
@ProviderRegistry.register(LLMProvider.GEMINI)
class GeminiProvider(BaseProvider):  # 走根抽象,不经过 OpenAICompatibleProvider
    provider_name = "gemini"
    default_base_url = "..."
    env_key = "GEMINI_API_KEY"

    def chat(self, messages, tools, ...):
        # 自己实现 Gemini 协议的 message 转换、stream 处理、thinking 提取
        ...
```

```python
# 加 AWS Bedrock Claude(Anthropic 协议但走 AWS SigV4)—
# 复用 anthropic/base.py 的所有协议逻辑,只覆盖请求构造/鉴权
# 不需要改 AnthropicProvider 一行代码
# agent_core/llm/providers/anthropic/bedrock.py
from .base import AnthropicProvider
from ....registry import ProviderRegistry
from ....config import LLMProvider


@ProviderRegistry.register(LLMProvider.BEDROCK_CLAUDE)
class BedrockClaudeProvider(AnthropicProvider):
    """Anthropic 协议但底层走 AWS Bedrock(替换 _build_kwargs / client 即可)。

    继承 AnthropicProvider 默认实现,只需覆盖差异:
    - _build_kwargs(): Bedrock 用 invoke_model API,字段命名略不同
    - client property: boto3 客户端 + SigV4 签名,不是 anthropic SDK
    - thinking 块、流循环、tool_use、cache_control 全部继承基类
    """
    provider_name = "bedrock-claude"
    default_base_url = None
    env_key = "AWS_ACCESS_KEY_ID"

    @property
    def client(self):
        # boto3 + SigV4,而不是 anthropic.Anthropic
        if self._client is None:
            import boto3
            self._client = boto3.client("bedrock-runtime", region_name="us-east-1")
        return self._client

    def _build_kwargs(self, messages, tools, tool_choice, system_prompt, cache_namespace):
        # Bedrock 的 model_id、body 结构略不同,override 即可
        ...
```

> 上例演示:**`anthropic/` 包结构**让「加第二个 Anthropic 协议 provider」变成「
> 新增 1 个文件 + 继承」,而不是「重构目录结构」。这是「可扩展架构」的实际价值——
> 不在于当下省了多少代码,在于**未来加 provider 时不必再经历一次重构**。

---

## 8. 附录:从 80/20 视角看「必须做」与「可选做」

### 8.1 必须做(本次重构核心)

1. ✅ 抽 `types.py`——所有数据类集中(行 116–343 → 1 个文件)
2. ✅ 抽 `providers/_retry.py`——错误分类 + 重试生成器(行 174–552 → 1 个 Provider 内部工具)
3. ✅ 抽 `BaseProvider` + 抽 `AnthropicProvider` 成类(消除方法 vs 类不对称)
4. ✅ 引入 `ProviderRegistry` + `@register_provider` 装饰器(消除手写 factory)
5. ✅ `LLMRouter` 瘦到 ~50 行,只做调度(重试已下沉到 `BaseProvider._with_retry`,Router 不再感知)
6. ✅ `thinking` 提取改为 Provider 的 `_extract_thinking()` 钩子(Template Method,不是独立策略类)

### 8.2 建议做(但本期可不做)

1. ⏳ `Provider` 元数据(model 列表 / env_key)集中描述(目前 `MODELS_BY_PROVIDER` 仍在 router.py:62-84)

> 说明:`providers/openai/` 子包化、`providers/anthropic/` 包结构、`providers/_retry.py` 等已经在 §3.1 目标架构中作为**必做项**纳入,不再列于此处。

### 8.3 显式不做(避免过度设计)

1. ❌ **不抽 `ThinkingExtractor` 独立策略类**——thinking 是 Provider 的协议特征,用钩子方法(Template Method)足矣,单独的策略类层次反而增加间接层(`thinking_extractor` 字段、构造函数传参、`_default_extractor()` 工厂方法)
2. ❌ **不抽 `ThinkTagSplitter` 到独立 `thinking_splitter.py` 文件**——YAGNI,目前只有 MiniMax 协议走 `<think>` 标签,内嵌到 `MiniMaxProvider._ThinkTagSplitter` 嵌套类即可。嵌套类仍可独立 `new`、独立单测(`MiniMaxProvider._ThinkTagSplitter()`),保留 100% 可测性,但不增加跨文件依赖。`thinking_splitter.py` **彻底删除**,全项目只有 3 处引用(router.py / openai_compatible.py / test_router.py)全部直接迁移,不留兼容 shim(`grep -r "_ThinkTagSplitter\|thinking_splitter" agent_core/` 必须 0 hit,除新位置的 `MiniMaxProvider._ThinkTagSplitter` 定义外)
3. ❌ 不引入 DI 容器(dependency-injector 等)——用 `__init__.py` 的 import side-effect 触发注册,够用且零依赖
4. ❌ 不引入 Protocol 抽象类 vs ABC 的选择之争——直接用 `abc.ABC`,简单清晰
5. ❌ 不引入 OpenTelemetry 等可观测性框架——`logger.warning`/`logger.info` 已够用

---

**End of document.**
