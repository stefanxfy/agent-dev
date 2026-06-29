# LLM invoke() 重试+超时+回调 一体化设计

> 讨论：是否能把 chunk 聚合、重试、超时统一收到 `BaseLLMProvider.invoke()` 里，
> 通过回调函数注入业务差异。

---

## 1. 结论：完全可行

三处调用点的差异可以精确分解为：

| 层 | 是否共享 | 放在哪 |
|----|---------|--------|
| chunk 迭代聚合 (5行) | ✅ 3处完全相同 | invoke() 内部 |
| 重试循环 (for attempt) | ✅ 模式相同，参数不同 | invoke() 内部，参数化 |
| 超时守卫 (ThreadPoolExecutor) | ✅ extraction_gate 有，另外两处缺 | invoke() 内部，兜底保护 |
| 空响应检测 | ✅ 都应检测，且空响应必然是错 | invoke() 内部，无条件重试 |
| 失败后降级行为 | ❌ 各不相同 | on_failure 回调注入 |

---

## 2. API 设计

### 2.1 签名

```python
def invoke(
    self,
    messages: list[dict],
    *,
    max_retries: int = 2,
    timeout: float | None = 30.0,
    on_failure: Callable[[Exception], str] | None = None,
    **kwargs,  # 透传给 self.chat()（cache_namespace 等）
) -> str:
```

只有 3 个可配参数 + 1 个回调。退避策略 `0.5s × 2^attempt` 为内部常量，不需要暴露。

### 2.2 两种使用形式

**最简**（零配置）：

```python
text = router.invoke(messages=[...], cache_namespace="foo")
# 默认：2次重试（指数退避）、30s超时、空响应自动重试、失败 re-raise
```

**注入降级**（一个回调）：

```python
text = router.invoke(
    messages=[...],
    cache_namespace="...",
    max_retries=0,          # 不重试
    timeout=15.0,           # 自定义超时
    on_failure=lambda e: fallback_text,
)
```

### 2.3 设计原则

| 决策 | 理由 |
|------|------|
| 不提供 `on_retry` 回调 | 重试日志是 invoke() 的默认行为，`logger.debug()` 内置，不需要外部注入 |
| 不提供 `on_empty` 回调 | 空响应在任何场景下都是异常，无条件进重试循环，不需要"有时算错有时不算"的配置 |
| 不提供 `empty_is_error` 开关 | 同上——没有"空响应不是错误"的合理场景 |
| 不暴露 `backoff_base` 参数 | `0.5s × 2^attempt` 是适用于所有 LLM API 的稳妥值，没有场景需要改它。内部常量，调用方无感知 |
| 只保留 `on_failure` | 这是唯一有业务差异的地方（返 [] vs 返 {} vs raise），必须可注入 |

### 2.4 `on_failure` 回调契约

签名 `(Exception) -> str`，但有两个出口：

```python
def gate_on_failure(error: Exception) -> str:
    if isinstance(error, InvokeTimeoutError):
        raise LatencyTimeout(30.0)  # ← 出口1：穿透 invoke()，上层捕获
    return json.dumps({"should_extract": False})  # ← 出口2：降级文本，invoke() 正常返回
```

Python 的回调内部可以 `raise`，这不是 hack——签名 `-> str` 只表示"正常路径"，raise 走的异常路径自然穿透 invoke() 的 try/except。

### 2.5 为什么预算检查不放进 invoke()

预算检查是 pre-invoke 语义（"能不能调"），不是执行期错误处理。而且只有 extraction_gate 用 CostTracker，放进去污染其他调用点。

---

## 3. invoke() 内部实现伪代码

```python
import concurrent.futures
import time
import logging

logger = logging.getLogger(__name__)


class EmptyResponseError(RuntimeError):
    """LLM 返回空文本"""


class InvokeTimeoutError(TimeoutError):
    """invoke() 超时"""


class BaseLLMProvider(ABC):

    # ── 内部常量 ──────────────────────────────────────────────

    _BACKOFF_BASE: float = 0.5  # 指数退避基准秒数

    # ── 公开 API ──────────────────────────────────────────────

    def invoke(
        self,
        messages: list[dict],
        *,
        max_retries: int = 2,
        timeout: float | None = 30.0,
        on_failure: Callable[[Exception], str] | None = None,
        **kwargs,  # 透传给 self.chat()（cache_namespace 等）
    ) -> str:
        """同步 LLM 调用，内置重试 + 超时 + 空响应检测。

        重试和超时日志默认开启 (logger.debug)。
        业务差异仅通过 on_failure 回调注入。
        """
        last_err: Exception | None = None

        for attempt in range(max_retries + 1):
            try:
                # ── 超时守卫 ──
                if timeout is not None:
                    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
                        future = ex.submit(self._aggregate_chunks, messages, **kwargs)
                        text = future.result(timeout=timeout)
                else:
                    text = self._aggregate_chunks(messages, **kwargs)

                # ── 空响应检测（无条件，空一定是错）──
                if not text.strip():
                    raise EmptyResponseError(
                        f"LLM 返回空响应 attempt={attempt}"
                    )

                return text

            except concurrent.futures.TimeoutError as e:
                last_err = InvokeTimeoutError(
                    f"LLM invoke timeout ({timeout}s) attempt={attempt}"
                ) from e
            except EmptyResponseError as e:
                last_err = e
            except Exception as e:
                last_err = e

            # ── 退避 + 日志（内置，不需要回调）──
            if attempt < max_retries:
                delay = self._BACKOFF_BASE * (2 ** attempt)
                logger.debug(
                    f"LLM invoke retry {attempt + 1}/{max_retries} "
                    f"after {delay:.1f}s: {type(last_err).__name__}"
                )
                time.sleep(delay)

        # ── 所有重试耗尽 ──
        assert last_err is not None
        if on_failure is not None:
            return on_failure(last_err)  # 回调内部可 raise 或 return
        raise last_err

    # ── 内部方法 ──────────────────────────────────────────────

    def _aggregate_chunks(self, messages: list[dict], **kwargs) -> str:
        """chunk 迭代聚合：收归那 5 行重复代码。"""
        parts: list[str] = []
        for chunk in self.chat(messages, **kwargs):
            td = getattr(chunk, "text_delta", None)
            if td is not None:
                t = getattr(td, "text", None)
                if t:
                    parts.append(t)
        return "".join(parts)

    # ── 子类必须实现 ──────────────────────────────────────────

    @abstractmethod
    def chat(self, messages: list[dict], **kwargs) -> Iterator[StreamChunk]:
        """流式 LLM 调用（子类实现）"""
        ...
```

---

## 4. 三处调用点迁移对照

### 4.1 extraction_gate（最复杂）

**现状**（~30 行，两方法）：

```python
def _call_llm(self, prompt: str) -> str:
    if self._cost_tracker:               # [1] 预算检查 ← 保留在外部
        budget_err = self._cost_tracker.check_budget()
        if budget_err: raise budget_err

    text = ""
    try:                                   # [2] 超时守卫 ← invoke() 内置
        with ThreadPoolExecutor(max_workers=1) as ex:
            future = ex.submit(self._do_llm_call, prompt)
            text = future.result(timeout=self._latency_timeout)
    except TimeoutError:                   # [3] 超时处理 ← 回调
        raise LatencyTimeout(self._latency_timeout)

    if self._cost_tracker:                 # [4] cost 统计 ← 保留在外部
        self._cost_tracker.add(len(prompt)//4, len(text)//4)
    return text

def _do_llm_call(self, prompt: str) -> str:  # [5] ThreadPoolExecutor 的 target
    text = ""                                # [6] chunk 聚合 ← invoke() 内置
    for chunk in self.llm_router.chat(...):
        if chunk.text_delta:
            text += chunk.text_delta.text
    return text
```

**迁移后**（~20 行，单方法）：

```python
def _call_llm(self, prompt: str) -> str:
    # [1] 预算检查 — pre-invoke，保留
    if self._cost_tracker:
        budget_err = self._cost_tracker.check_budget()
        if budget_err:
            raise budget_err

    try:
        text = self.llm_router.invoke(
            messages=[
                {"role": "system", "content": EXTRACT_SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            cache_namespace=self.cache_namespace,
            max_retries=0,          # gate 保延迟，不重试 LLM 调用
            timeout=30.0,           # 30s 超时
            on_failure=_gate_on_failure,
        )
    except (BudgetExceeded, LatencyTimeout):
        raise  # 让上层 bridge 转 MemoryEvent

    # [4] cost 统计 — post-invoke，保留
    if self._cost_tracker:
        self._cost_tracker.add(len(prompt) // 4, len(text) // 4)

    return text


def _gate_on_failure(error: Exception) -> str:
    """gate 专属降级：Timeout → 抛；其他 → JSON degrade"""
    if isinstance(error, InvokeTimeoutError):
        raise LatencyTimeout(30.0)
    logger.warning(f"LLM gate 降级: {error}")
    return json.dumps({"should_extract": False, "reason": "llm_call_error"})
```

**变化**：
- `_do_llm_call()` 整个方法删除（chunk 聚合被 invoke() 收归）
- `ThreadPoolExecutor` 手动管理删除（invoke() 内置）
- 预算检查 / cost 统计保留在外部（pre/post-invoke，不是 invoke 的职责）
- `_gate_on_failure` 改为模块级纯函数（不依赖 self）

### 4.2 retriever（最简）

**现状**（~15 行 chunk 聚合 + JSON parse）：

```python
text = ""
t0 = time.time()
try:
    for chunk in self.llm_router.chat(
        messages=[...], cache_namespace="memory_side_query",
    ):
        if getattr(chunk, "text_delta", None):
            text += chunk.text_delta.text
    data = json.loads(_strip_code_fence(text))
    return data.get("selected_paths", [])[:max_select]
except Exception as e:
    logger.warning(f"sideQuery 失败,降级返空: {e}")
    return []
```

**迁移后**（~10 行）：

```python
t0 = time.time()
try:
    text = self.llm_router.invoke(
        messages=[
            {"role": "system", "content": SIDE_QUERY_SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        cache_namespace="memory_side_query",
        # max_retries=2  (默认)
        # timeout=30.0   (默认 — 之前没有超时保护，现在有了！)
        on_failure=lambda e: _retriever_degrade(e),
    )
    data = json.loads(_strip_code_fence(text))
    return data.get("selected_paths", [])[:max_select]
except Exception:
    # on_failure 已经处理了降级，这里理论上不会走到
    # 但保留作为兜底（callback 内部可能 raise）
    return []


def _retriever_degrade(error: Exception) -> str:
    logger.warning(f"sideQuery LLM 降级: {type(error).__name__}: {error}")
    return "[]"
```

**变化**：
- 去掉了 4 行 chunk 聚合
- **获得了之前缺失的超时保护**（默认 30s）
- **获得了之前缺失的重试能力**（默认 2 次）
- 回调 `_retriever_degrade` 返回 `"[]"`，外部 JSON parse 后得到空列表

### 4.3 sm_callback（有自带重试循环）

**现状**（~25 行，含自己的重试循环）：

```python
def _callback(prompt: str) -> str:
    messages = [...]
    last_err = None
    for attempt in range(1, max_retries + 1):
        try:
            chunks = []
            for chunk in router.chat(messages=messages, cache_namespace=...):
                text_delta = getattr(chunk, "text_delta", None)
                if text_delta is not None:
                    text = getattr(text_delta, "text", None)
                    if text:
                        chunks.append(text)
            response = "".join(chunks)
            if not response.strip():
                raise RuntimeError("LLM 返回空响应")
            return response
        except Exception as e:
            last_err = e
            logger.warning(f"LLM 调用失败 attempt={attempt}/{max_retries}")
            if attempt < max_retries:
                backoff = backoff_base * (2 ** (attempt - 1))
                time.sleep(backoff)

    if on_failure == "return_empty":
        return ""
    raise RuntimeError(f"重试{max_retries}次仍失败: {last_err}")
```

**迁移后**（~12 行）：

```python
def _callback(prompt: str) -> str:
    messages = [
        {"role": "system", "content": SM_EDIT_SYSTEM_PROMPT},
        {"role": "user", "content": prompt},
    ]

    try:
        return router.invoke(
            messages=messages,
            cache_namespace=cache_namespace,
            max_retries=max_retries,
            timeout=30.0,  # 之前缺超时保护，现在有了
        )
    except Exception as e:
        if on_failure == "return_empty":
            logger.warning(f"[L3 SM callback] 重试{max_retries}次仍失败,返空")
            return ""
        raise RuntimeError(
            f"SM extract LLM callback 失败 重试{max_retries}次仍无法获取响应: {e}"
        ) from e
```

**变化**：
- 整个重试循环删除（13行 → 0行）
- chunk 聚合删除（5行 → 0行）
- **获得了之前缺失的超时保护**
- 重试日志由 invoke() 内置的 `logger.debug()` 自动输出，不再需要手动写
- `on_failure` 语义留在外部 try/except（`"raise"/"return_empty"` 两态行为用外层的 `if on_failure ==` 处理比塞进回调更清晰）

---

## 5. 边界条件分析

### 5.1 `on_failure` 返回 `str`，但有时需要 raise？

Python 的回调函数内部可以 `raise`，这是语言行为不是签名约束。`-> str` 只表示"正常路径"，回调内部 raise 的异常会穿透 invoke() 的 try/except，直接到达调用方。

```python
def gate_on_failure(error: Exception) -> str:
    if isinstance(error, InvokeTimeoutError):
        raise LatencyTimeout(30.0)  # ← 穿透 invoke()，到达 gate 的 try/except
    return json.dumps({"should_extract": False})  # ← invoke() 正常返回
```

这是设计意图，不是 hack。

### 5.2 ThreadPoolExecutor 的开销

`invoke()` 内部为每次调用创建 `ThreadPoolExecutor(max_workers=1)`。对于高频调用点（如 extraction_gate 每轮都触发），这个开销需要考虑：

- macOS/Linux 上 `ThreadPoolExecutor` 创建成本 ≈ 0.5-1ms
- 相比 LLM 调用本身（500ms-30s），可忽略不计
- 如果未来有极端高频场景，可以加 `_timeout_executor` 复用池，但目前不需要

### 5.3 超时后的 chunk 流清理

`future.result(timeout=...)` 抛出 `TimeoutError` 后，底层 generator 不会自动关闭。但在 Python 中，`future.cancel()` 后线程中运行的 generator 会在下一次 yield 时收到 `GeneratorExit`（如果线程还活着）。对于 HTTP 请求级的 generator，这通常意味着底层连接被关闭。

当前 extraction_gate 的代码也没有显式清理，所以这个行为不变。

---

## 6. 收益总结

### 代码量变化

| 文件 | 删除行数 | 新增行数 | 净变化 |
|------|---------|---------|--------|
| `base.py` (invoke + _aggregate_chunks) | — | +45 | — |
| extraction_gate.py (`_call_llm` + `_do_llm_call`) | -30 | ~20 | -10 |
| retriever.py (`_call_side_query`) | -4 | +1 | -3 |
| sm_callback.py (`_callback`) | -18 | ~15 | -3 |
| **合计** | **-52** | **+81** | **+29** |

数字上 +29 行，但：
- `base.py` 的 +55 行是**一次性基础设施**，未来所有新调用点零成本
- 三个调用方各减 3-10 行，且去掉了最难维护的重复代码
- 两个调用方（retriever + sm_callback）**获得了之前缺失的超时保护**

### 健壮性提升

| 调用点 | 之前缺什么 | 现在有了 |
|--------|-----------|---------|
| retriever | 超时保护 | 30s 超时，2 次重试 |
| sm_callback | 超时保护 | 30s 超时 |
| extraction_gate | 重试（有意不重试） | max_retries=0 保持现状，但 chunk 聚合统一了 |

### 新增调用点成本

之前新增一个 LLM 调用点需要：
1. 写 chunk 聚合 5 行
2. 写重试循环 10 行（如果怕网络抖动）
3. 写超时守卫 8 行（如果怕挂住）

现在 → 一行 `router.invoke(messages=[...])`。

---

## 7. 实施路径

这个设计与 LLM Router 重构（`docs/llm-router-refactor-design.md`）完全融合——`invoke()` + `_aggregate_chunks()` 加在 Router 重构的 **S4 步骤**（`BaseLLMProvider` 实现）上，零额外步骤。

同步调用优化（`docs/llm-sync-call-refactor-design.md`）的方案也因此升级——从"只收 chunk 聚合"升级为"收 chunk 聚合 + 重试 + 超时 + 回调"。
