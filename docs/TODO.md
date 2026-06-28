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

---

## 3. M10 C5.4 双通道 wiring(补 web/app.py 实现,清 3 个契约测试)

**提出日期**: 2026-06-28
**提出背景**: M3 阶段跑全量回归时发现 `tests/test_app_wiring.py` 3 个 pre-existing failure,都是 M10 C5.4 时代的"测试契约已加、web/app.py 实现未补"。

**契约来源**(commit `6e07a6b` + `4e4b0e5`):
- `test_get_agent_uses_react_memory_bridge` — `get_agent(memory_enabled=True)` 必须传非 None `ReactMemoryBridge`
- `test_web_app_imports_strict_pipeline_components` — `web/app.py` 必须 import 4 个名字:`MetaDB` / `DualChannelWriter` / `ExtractionGate` / `ReactMemoryBridge`
- `test_get_agent_constructs_independent_extractor_router` — 必须构造 `extractor_router`(独立 LLMRouter 实例),并 `gate = ExtractionGate(llm_router=extractor_router, ...)`

**为什么 fork 独立 extractor_router**(§4.6):
1. 共享 prompt cache → 提取任务污染主对话 context
2. 共享 rate limit / token budget → 用户响应被 background 提取挤占
3. 共享 model selection → 提取可能用便宜模型但 cache 命中主对话的贵模型 slot

fork 后两条路径**完全隔离**,各自管 cache namespace + budget。

**待补的实现**(在 `web/app.py:get_agent()` 内):
```python
# 1. 独立 extractor_router(防 cache 污染)
extractor_router = LLMRouter(config)

# 2. Channel B 持久化层
meta_db = MetaDB(...)

# 3. 双通道写盘
dual_writer = DualChannelWriter(meta_db=meta_db, ...)

# 4. 门控(门 3 用 extractor_router 评分)
gate = ExtractionGate(llm_router=extractor_router, ...)

# 5. 同步→异步桥
bridge = ReactMemoryBridge(gate=gate, dual_writer=dual_writer)

# 6. 注入 ReactAgent
ReactAgent(..., react_memory_bridge=bridge)
```

**涉及文件**:
- `web/app.py` — `get_agent()` 加 6 步 wiring(主路径)
- `tests/test_app_wiring.py` — 现有 3 个契约测试,补实现后自然绿
- 旁路:`web/memory_wiring.py` 可能需要微调,把 `extractor_router` 暴露出来

**风险**:
1. 改动 `web/app.py` 高频入口,影响所有用户 → 需 streamlit smoke
2. DualChannelWriter 构造可能依赖 MetaDB schema(M11 已就绪)→ 需先确认 schema
3. `memory_enabled=True` 路径用户主流程引入 background 提取 → 性能回归测

**预估工作量**: 1-1.5 天
- 0.5 天:在 `web/app.py:get_agent()` 加 6 步 wiring
- 0.5 天:streamlit smoke + agent_core run 集成测
- 0.3 天:3 个契约测试跑绿验证

**优先级**: 中(契约测试在 fail,但功能未上线 → 不阻塞 M3 交付;M10/M11 后续阶段会补)

**不阻塞 M3 验收**: 这 3 个 failure 与 M3(权限 UI + hook 三阶段)无关,`web/app.py` 的 memory wiring 是 M10 时代遗留任务,放后续阶段完成。

---