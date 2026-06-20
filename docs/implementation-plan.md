# agent-dev 实现计划

> 会话管理（已完成）+ 上下文管理（待开发）+ 记忆系统（远期）
>
> 版本：v3.0 | 日期：2026-06-14
> 变更：v2.0 → v3.0 会话管理已完成，删除不适用模块，修正时间估算

---

## 一、当前状态

### 会话管理 — ✅ 已完成

| 模块 | 功能 | 状态 |
|------|------|------|
| 生命周期 | 创建/切换/关闭/分叉 | ✅ |
| 消息存储 | JSONL 持久化/tail+head 双窗口/去重 | ✅ |
| 元数据 | 标题生成（AI+用户）/标签/Agent类型/模式 | ✅ |
| 状态恢复 | Resume/Continue/Fork 语义 | ✅ |
| 进度追踪 | 实时消息数/元数据查看 | ✅ |
| 清理归档 | TTL/归档 | ✅ |

**交付物**：10 个文件，~4500 行，22 个测试通过，已推送 GitHub。

### 上下文管理 — 📋 待开发

| 功能 | 状态 | 说明 |
|------|------|------|
| Token 预算监控 | 📋 | ContextBudgetManager，GLM 适配参数 |
| 自动压缩触发 | 📋 | 剩余 ~6.5% 时触发 |
| 两阶段摘要 | 📋 | analysis + summary 格式 |
| PTL 防御 | 📋 | 压缩请求超限时剥洋葱重试 |
| 熔断保护 | 📋 | 连续 3 次失败停止压缩 |
| verbatim quotes 防漂移 | 📋 | 用户消息逐字引用 |

**不实现**：PromptCacheManager（GLM 不支持 `cache_control`）、StateKeeper（无 FileRead/Plan/MCP）、MessageStore（SessionStorage 已有）。

### 记忆系统 — 🔮 远期

| 功能 | 状态 |
|------|------|
| 偏好提取 | 🔜 |
| MEMORY.md 索引 | 🔜 |
| System Prompt 注入 | 🔜 |

---

## 二、文件清单（上下文管理）

```
agent_core/
├── context/                        # 上下文管理（新建）
│   ├── __init__.py
│   ├── budget.py                  # 预算管理 + 触发决策 + 熔断
│   ├── tokenizer.py               # Token 估算（中英文比例）
│   ├── compact.py                 # 压缩编排 + PTL 防御
│   └── manager.py                 # 统一入口 ContextManager
│
├── session/                        # 会话管理（已完成）
│   └── ... (已完成)
│
├── agent_core.py                  # 修改：集成 ContextManager
└── web/app.py                     # 修改：UI 显示 token 用量
```

**合计：4 个新建 + 2 个改造**

---

## 三、时间估算（修正后）

### v2.0 的问题

v2.0 估算"2 天完成三大系统"，实际会话管理花了 3 天。原因是低估了 Streamlit 集成、Bug 修复、标题生成系统的复杂度。

### v3.0 估算

上下文管理按实际复杂度拆分：

```
Phase 1: 基础框架（~4 小时）
├── budget.py + tokenizer.py
├── 单元测试（should_compact、熔断器、token 估算）
└── 不集成到 Agent，独立验证

Phase 2: 压缩功能（~6 小时）
├── compact.py
├── 压缩 prompt 调试
├── PTL 防御测试
└── 端到端：构造超长对话 → 触发压缩 → 验证摘要

Phase 3: 集成（~4 小时）
├── 替换 agent_core.py 的 _trim_history
├── Streamlit UI 显示 token 用量 + 压缩状态
└── 集成测试
```

**总计：~14 小时（约 2 天有效工作时间）**

记忆系统远期再做，不纳入当前计划。

---

## 四、验收标准

### 上下文管理完成标准

- [ ] `ContextBudgetManager.should_compact()` 正确触发
- [ ] `SimpleTokenCounter` 中英文混合估算误差 < 20%
- [ ] `CompactOrchestrator.compact()` 生成有效摘要
- [ ] PTL 防御：构造超长对话触发至少 1 次剥洋葱重试
- [ ] 熔断器：连续 3 次失败后停止压缩
- [ ] 替换 `_trim_history`，ReAct 循环正常工作
- [ ] Streamlit UI 显示 token 用量和压缩状态
- [ ] 会话持久化正常（压缩后的消息能保存到 JSONL）

---

## 五、技术约定

### Token 估算

```python
def estimate_tokens(text: str) -> int:
    chinese = sum(1 for c in text if '\u4e00' <= c <= '\u9fff')
    english = len(text) - chinese
    return int(chinese * 1.4 + english * 0.25 + 10)  # overhead
```

### GLM 适配参数

```python
GLM_CONTEXT_WINDOW = 128_000
AUTOCOMPACT_BUFFER_TOKENS = 8_000   # ~6.25%
MAX_OUTPUT_TOKENS_FOR_SUMMARY = 4_096
MAX_CONSECUTIVE_AUTOCOMPACT_FAILURES = 3
MAX_PTL_RETRIES = 3
TRUNCATE_RATIO = 0.2
```

### JSONL Entry 规范

继承会话管理的格式（Claude Code 风格 4 条 Entry）：

```json
{
    "uuid": "str",
    "parentUuid": "str | null",
    "sessionId": "str",
    "timestamp": "ISO 8601",
    "type": "user | assistant | summary",
    "message": { "role": "...", "content": "..." }
}
```

### 压缩触发阈值

```python
COMPACT_THRESHOLD_RATIO = 0.0625  # 剩余 ≤ 6.25% 时触发
PRESERVED_HEAD_MESSAGES = 6       # 压缩后保留最近 6 条原始消息
```

---

> 文档版本：v3.0
> 更新日期：2026-06-14
> 适用项目：agent-dev
