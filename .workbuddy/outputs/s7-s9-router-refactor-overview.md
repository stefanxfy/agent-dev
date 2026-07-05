# S7-S9 LLM Router 重构完成概览

## 做了什么

将 `router.py` 从 322 行单体文件精简为 202 行，用 **ProviderRegistry 多态 dispatch** 替代硬编码的 `if/elif` 分支，完成 LLM 模块的模块化重构。

## 核心变更

### router.py (322 → 202 行, -37%)

**移除的死代码**（3 个方法）:
- `_get_anthropic_client()` → 移至 `AnthropicProvider`
- `_chat_anthropic()` → 移至 `AnthropicProvider`  
- `_get_openai_provider()` → 替换为 `ProviderRegistry.get()`

**新增的 helper**（2 个静态方法）:
- `_prepare_openai_messages()` — Fork 模式 system_prompt_override 注入
- `_log_cache_warning()` — cache_namespace 适配日志

**chat() 精简**: ~80 行 → ~35 行
```python
# Anthropic 路径：创建 Provider 并委托
provider = AnthropicProvider(config, system_prompt_override=..., cache_namespace=...)
yield from self.stream_with_retry(lambda: provider.chat(...), ...)

# OpenAI 兼容路径：ProviderRegistry 多态 dispatch
provider_cls = ProviderRegistry.get(provider_enum)
provider = provider_cls(config)
yield from self.stream_with_retry(lambda: provider.chat(...), ...)
```

### test_router.py 更新

5 处 monkey-patch `_get_openai_provider` → mock `ProviderRegistry.get()`，全部 39 个测试通过。

## 验证结果

| 指标 | 结果 |
|------|------|
| test_router.py (39) | ✅ 全部通过 |
| test_types_config.py (39) | ✅ 全部通过 |
| 消费者 import 兼容 | ✅ web.llm_options / agent_core 等零破坏 |
| 死代码清理 | ✅ 3 个旧方法移除 |
| ProviderRegistry 注册 | ✅ 4 个 provider 已注册 |
| 向后兼容 re-export | ✅ 全部 types/config/retry re-export 正常 |

## 重构总进度

```
[x] S1: types.py      (168 行) — 数据类型
[x] S2: config.py      (100 行) — 配置/枚举
[x] S3: retry.py       (133 行) — 重试 Mixin
[x] S4: base.py        (244 行) — ABC + ProviderRegistry
[x] S5-S6: providers/  (326 行) — 4 个 provider
[x] S7-S9: router.py   (202 行) — 精简 + 测试更新
─────────────────────────────────────────
全部完成: 14 个文件, 2317 行
```

## 设计收益

- **新增 provider 成本**: 从 ~80 行 → ~35 行（继承 BaseLLMProvider + 注册装饰器）
- **router.py 独立性**: 不再包含任何 provider 特定逻辑
- **ProviderRegistry 多态**: `if/elif` → `ProviderRegistry.get()`
- **零破坏性变更**: 所有 re-export 和公共 API 完全兼容
