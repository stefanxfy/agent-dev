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

## 3. M10 C5.4 双通道 wiring — **测试 fixture 错位**,不是实现缺口 ⚠️

**提出日期**: 2026-06-28
**更正日期**: 2026-06-28(用户质疑后核实)
**状态**: 代码已完整实现,测试断言位置错误

### 事实

M10 C5.4 wiring **已经全部实现**,只是抽到 helper 函数里([web/memory_wiring.py:build_memory_system()](web/memory_wiring.py#L60-L160)):

| 步骤 | 实现位置 | 行号 |
|---|---|---|
| `extractor_router = LLMRouter(llm_config)` | `web/memory_wiring.py` | L104 |
| `meta_db = MetaDB(meta_db_path)` | `web/memory_wiring.py` | L122 |
| `gate = ExtractionGate(llm_router=extractor_router, ...)` | `web/memory_wiring.py` | L130-135 |
| `dual_channel = DualChannelWriter(...)` | `web/memory_wiring.py` | L146-155 |
| `react_memory_bridge = ReactMemoryBridge(...)` | `web/memory_wiring.py` | L158+ |
| `ReactAgent(react_memory_bridge=react_memory_bridge)` | `web/app.py` | L697 |

`web/app.py:get_agent()`(L617-640)委托给 `build_memory_system()`,把 bundle 解包后注入 ReactAgent。

### 失败原因分两类

**类 A — 测试假设错误(2 个静态字符串测试)**:
- `test_web_app_imports_strict_pipeline_components` — 在 `web/app.py` 源码里 grep `MetaDB` 字面
- `test_get_agent_constructs_independent_extractor_router` — grep `extractor_router` + `gate = ExtractionGate(...)`

实现把组件藏在 helper 里,源码扫描找不到。这是测试设计问题,**不是实现缺口**。

**类 B — 环境缺依赖(1 个端到端测试)**:
- `test_get_agent_uses_react_memory_bridge` — 跑真 `get_agent()`,依赖 chromadb

当前 env 没装 `chromadb`(`bash scripts/setup_embeddings.sh` 未跑),`build_memory_system` 返 None → bridge=None → 测试 fail。装依赖后自然绿。

### 修复路径(三选一)

**A. 改测试 fixture**(推荐,工作最小):
- 测试 1 改用 `unittest.mock.patch("web.memory_wiring.build_memory_system", return_value=(...mock bundle...))`,只验证 bridge flow-through
- 测试 2/3 改扫描 `web/memory_wiring.py` 而非 `web/app.py`(或同时扫两个文件)

**B. 重构 web/app.py 内联 wiring**(不推荐):
- 把 helper 拆开 inline 回 `get_agent()`(更耦合,但匹配原契约)
- 风险:破坏 `web/memory_wiring.py` 作为单一职责模块的封装

**C. 装 chromadb**(`scripts/setup_embeddings.sh`):
- 让类 B 的端到端测试有真实依赖可调
- 类 A 仍 fail(测试 fixture 问题,与依赖无关)

### 涉及文件

- `tests/test_app_wiring.py` — 改 3 个契约测试的 fixture(L75-95 / L120-160 / L196-235)
- `web/memory_wiring.py` — 不动(实现正确)
- `web/app.py` — 不动(委托正确)

### 风险

1. 改测试时需保证仍能 catch 真实的"未接桥"回归(避免过度 mock 失去意义)
2. chromadb 装好后端到端测试可能引入 flakiness(初始化耗时)

### 预估工作量

- 0.2 天:改 3 个测试 fixture + 跑绿验证

### 优先级

**低** — 功能已可用(streamlit UI 启动时 `memory_enabled=True` 路径走通,只是测试 fixture 与实现路径不匹配)。不阻塞 M3 验收,也不阻塞用户使用。

### 不阻塞 M3 验收

**澄清**:我(M3 助手)初版 TODO 误判为"代码未补",实为"测试 fixture 错位"。感谢用户质疑后核实。M3 工作 0 影响。

---