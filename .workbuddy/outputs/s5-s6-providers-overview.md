# S5-S6 完成：providers/ 子包

## 新增文件

```
agent_core/llm/providers/
├── __init__.py          (12 行)  包入口，触发注册
├── anthropic.py         (155 行) AnthropicProvider
├── openai_provider.py   (38 行)  OpenAIProvider
├── zhipu.py             (34 行)  ZhipuProvider
└── minimax.py           (37 行)  MiniMaxProvider
```

## 核心设计

### AnthropicProvider（全新实现）
- 从 `router.py._chat_anthropic()` 提取完整 Anthropic 流式逻辑
- 支持：system message 提取、prompt cache (ephemeral)、thinking、tool_choice
- Fork 模式：`AnthropicProvider(config, system_prompt_override="...", cache_namespace="fork-xxx")`

### OpenAI/Zhipu/MiniMax（薄包装，双继承）
- 继承链：`XxxProvider → BaseLLMProvider + openai_compatible.XxxProvider`
- `BaseLLMProvider` 提供：`invoke()` / `stream_with_retry()` 重试 / ProviderRegistry 注册
- `openai_compatible.XxxProvider` 提供：流式循环 / 消息转换 / tool_calls 缓冲 / thinking 提取
- 每文件 ~35 行，零重复代码

## 验证结果

| 验证项 | 状态 |
|--------|------|
| 4 个 provider 注册到 ProviderRegistry | ✅ |
| ProviderRegistry.get() → 实例化 | ✅ |
| Anthropic fork 参数 | ✅ |
| provider_name 属性 | ✅ |
| 11 个模块导入零破坏 | ✅ |

## 重构进度

```
[x] S1: types.py       [x] S2: config.py
[x] S3: retry.py       [x] S4: base.py
[x] S5-S6: providers/  ← 刚完成
[ ] S7-S9: 清理 + 重构 router.py + 更新测试
```
