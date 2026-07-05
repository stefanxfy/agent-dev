# S4 完成：base.py — BaseLLMProvider ABC + invoke()

## 产出文件

`agent_core/llm/base.py`（155 行）

## 新增组件

| 组件 | 类型 | 说明 |
|------|------|------|
| `LLMResponse` | dataclass | 非流式聚合结果（text/thinking/tool_calls/usage/stop_reason） |
| `BaseLLMProvider` | ABC + RetryMixin | 所有 provider 的抽象基类 |
| `ProviderRegistry` | 类级注册表 | register/get/list_providers/is_registered |
| `register_provider` | 装饰器 | 一行注册 provider 类 |

## 核心方法

- **`chat()`** — 抽象方法，子类实现流式对话
- **`invoke()`** — 非流式封装，内置 `stream_with_retry()` 重试 + `on_chunk` 回调
- **`_aggregate_chunks()`** — 聚合流式 chunks → LLMResponse（text/thinking/tool_calls/usage/stop_reason）

## 验证结果

- MRO: `BaseLLMProvider → ABC → RetryMixin → object` ✅
- 注册/查找/覆盖 全部通过 ✅
- invoke + _aggregate_chunks 文本/thinking/tool_calls 聚合正确 ✅
- 5 个现有模块导入零破坏 ✅

## 当前进度

- [x] S1: types.py
- [x] S2: config.py
- [x] S3: retry.py
- [x] **S4: base.py** ← 刚完成
- [ ] S5-S6: providers/ 子包
- [ ] S7-S9: 清理 + 重构 router.py + 更新测试
