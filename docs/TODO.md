# TODO — 待办事项 / 未来优化

> 记录"想到了但现在不做"的改进点,等有空或需要时回来实现。
> 创建日期: 2026-06-27

---

## 1. 抽取 LLM 公共调用函数 `_chat_sync()`

**提出日期**: 2026-06-27
**提出背景**: 接 L3 SM callback 时,发现项目里至少 3 处非流式 LLM 调用都长一个样:
- `agent_core/agent_core.py` ReAct 主对话(流式,这次不算)
- `agent_core/memory/dual_channel_writer.py` 候选提取(后台线程,非流式)
- `agent_core/memory/retriever.py` side_query(同步,非流式)
- `agent_core/memory/sm_callback.py` SM extract(即将实现,同步,非流式)

**重复 pattern**:
```
[messages 拼装] → [extractor_router.chat] → [流式 chunks] → [聚合 str]
```

**为什么不抽**:
1. 每处的错误处理不一样(ReAct yield 给 UI,后台 retry 写 WAL,sm callback 重试抛异常)
2. 流式/非流式混用(ReAct 必须流式,其他 3 处可聚合)
3. cache_namespace / retry / timeout 配置每处不同

**建议抽取的接口**(等以后有空):
```python
def _chat_sync(
    messages: list[dict],
    *,
    cache_namespace: str,
    max_retries: int = 1,
    timeout_s: float = 30.0,
    on_chunk: Callable[[str], None] | None = None,  # 可选回调
) -> str:
    """同步调用 extractor_router,聚合流式 chunks 返回 str。

    on_chunk: 如果传了,每收到一个 chunk 就调一次(ReAct 流式场景)。
             不传则一次性聚合返回(后台 / callback 场景)。
    """
```

**涉及文件**(重构时改):
- `agent_core/agent_core.py` — ReAct 主对话改用 `_chat_sync(..., on_chunk=yield_chunk)`
- `agent_core/memory/dual_channel_writer.py` — 候选提取改用 `_chat_sync()`
- `agent_core/memory/retriever.py` — side_query 改用 `_chat_sync()`
- `agent_core/memory/sm_callback.py` — SM callback 改用 `_chat_sync()`

**放哪里**: `agent_core/llm/sync_helper.py` (新文件)

**预估工作量**: 0.5 天(写公共函数 + 改 4 个调用点 + 4 个测试文件回归)

**优先级**: 低(目前 3-4 处独立写还能接受,真正痛了再抽)

---

## 2. (占位)未来其他 TODO

(以后想到再追加,保持这个文件)