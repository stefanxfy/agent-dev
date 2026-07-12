# 代码优化报告：`agent_core/mcp` + `agent_core/tools` 联合审查

> 由 `/code-super-advisor` skill 生成（审查代码模式 + 增量影响分析）。本报告是 [tools-code-review-2026-07-09.md](tools-code-review-2026-07-09.md) 的 **MCP-aware 增量版**：tool 模块单独发现 **引用原报告不重复**（如 T-M1/P0 等），MCP 模块做完整三层审查，MCP↔tools 交叉点单独分析。

## 审查信息

| 项目 | 内容 |
|------|------|
| 审查范围 | `agent_core/mcp/`（6 文件,1,363 LOC）+ `agent_core/tools/` 因 MCP 改动的位置 + `web/app.py` MCP 注入段（70 行）+ `agent_core/turn_chain.py` McpPromptsHandler + `agent_core/builder.py` |
| 审查日期 | 2026-07-09 |
| 关联报告 | [tools-code-review-2026-07-09.md](tools-code-review-2026-07-09.md) — 本报告不重复其 P0/P1 全部发现，仅引用 |
| 测试覆盖 | MCP 子系统测试 15 个文件（`test_mcp_*.py`），全部覆盖 primitives / resources / prompts / reconnect / list_changed / roots / e2e |

## 总体评价

- **综合评级**：⭐⭐⭐☆☆（3/5，持平）
- **MCP 子系统健康度**：B
- **Tool 模块受 MCP 冲击面**：集中但尖锐（1 个 P0+ 升级，1 个新 P2，1 个新 P3）
- **一句话总结**：MCP 实现是 **正面样板**（决策驱动 + 架构清晰 + 测试充分），但其接入暴露了 tool 模块 **权限引擎对 MCP 工具命名空间的盲区**——M1 P0 由"内容级规则对 builtin 工具失效"升级为"对所有 MCP 工具 100% 失效"，需立即修。

## 问题统计

| 严重度 | MCP 新增 | Tool 侧受影响 | 跨系统 | 合计 |
|--------|---------|---------------|--------|------|
| P0 | 0 | 1（T-M1 升级） | 0 | **1** |
| P1 | 2 | 1（T-C2 hook point） | 0 | **3** |
| P2 | 4 | 1（T-New1 category 死字段） | 0 | **5** |
| P3 | 2 | 1（T-New2 lifecycle API） | 0 | **3** |

---

## 模块拓扑与集成路径

```
                  ┌──────────────────────────────┐
                  │ web/app.py:800-872           │  组合根
                  │ McpManager 注入 + callback  │
                  └──────────┬───────────────────┘
                             │ construct + register
              ┌──────────────┼───────────────────────────┐
              ▼              ▼                           ▼
   ┌────────────────────┐  ┌──────────────────────┐  ┌─────────────────┐
   │ agent_core/mcp/    │  │ agent_core/tools/    │  │ turn_chain.py   │
   │ manager.py (585)   │  │ base.py:97           │  │ McpPromptsHdl   │
   │ client.py (193)    │  │  unregister_by_prefix│  │  (914-949)      │
   │ config.py (221)    │  │  ← MCP 加的唯一方法   │  └─────────────────┘
   │ materialize.py(300)│  └──────────────────────┘
   │ names.py (65)      │           ▲
   │ __init__.py (38)   │           │ uses ToolDef + category="mcp"
   └────────────────────┘           │
              │                     │
              │ uses load_settings_json + load_rules_by_source
              ▼                     │
   ┌────────────────────────────────┴───────┐
   │ agent_core/tools/permission_loader.py    │
   │ load_settings_json (line 96)             │
   │ load_rules_by_source (line 200)          │
   └──────────────────────────────────────────┘
```

**关键观察**：MCP→tools 是 **单向依赖**（tool 模块对 MCP **零感知**，无任何 `import` 或分支）。tools 模块因 MCP 改动的位置仅 1 处——`base.py` 新增 `unregister_by_prefix`。

---

## 一、Tool 模块侧影响审计
> 只看 MCP 接入导致的改动点 + MCP 反向暴露的工具侧缺陷。tool 模块其他发现引用原报告 `[T-M1]`/`[T-A2]` 等编号。

### 1.1 Tool 模块因 MCP 改动位置（共 1 处新增方法）

| 位置 | 改动 | 影响 |
|------|------|------|
| `base.py:97-108` `ToolRegistry.unregister_by_prefix` | 新增方法（13 行） | 给 MCP list_changed / 重连 / 断连场景用；**是 MCP 反向迫使 tool 注册中心暴露生命周期 API**（T-New2 P3） |

代码段:
```python
def unregister_by_prefix(self, prefix: str) -> int:
    """按前缀批量注销（给 mcp__<server>__ 用），返回注销数量。"""
```

无其他新增方法/字段。`ToolDef` 的 `category="mcp"` 复用现有字段。

### 1.2 Tool 模块对 MCP 反向暴露的缺陷

#### T-M1 P0 升级（原 T-M1 P0 → P0+，因 MCP）
**位置**: `agent_core/tools/permission_engine.py:605-633` `_check_global_*_rule` 三方法 + `manager.py:153-175` connect_all 路径

**问题**: 原报告 T-M1 指出 global rule 只匹配 `tool_name`，内容级规则失效。MCP 接入后影响面**显著升级**:

- builtin 工具的 `Edit/Bash/Read/Write` 与 mcp 工具命名空间隔离（`mcp__fs__write` 完全不像 `Edit`），用户**不可能靠 typo 绕过**——但也**不可能靠内容级规则约束 MCP 工具**
- 所有 builtin 写的"Edit(/tmp/*)"、`Bash(npm:*)` 内容级规则，**对任何 mcp__* 工具 0% 匹配**
- Bash 因有 `bash_permissions.py:443-476` 独立旁路幸免；**MCP tools 无任何专用 content-aware 旁路**
- 用户体验：用户在 settings.json 写 "Edit(/tmp/*)" 看到该规则"生效"，但实际 mcp__fs 工具完全不受约束 → **误以为安全**

**示例攻击路径**:
```
用户配置: "Edit(/tmp/**)" always_deny
LLM 决定用 mcp__fs__write(path="/tmp/secrets.txt")   ← 不命中 deny
PermissionEngine._check_global_deny_rule("mcp__fs__write")   ← tool_name 不匹配
→ 实际进入 ASK/ALLOW，可能被 LLM 自决或用户误批
```

**修法建议**:
1. engine 接入 `permission_matcher.matching_rules_for_input`（原 T-M1 修法），并扩展 `_parse_rule_str` 支持 `mcp__<server>__<tool>(...)` 语法
2. 或在 MCP 工具命名层做 alias（让 `mcp__fs__write` 也注册成 builtin `Write` 的 alias 走原 path）——但语义会丢
3. 短期缓解：在 `web/app.py:824` 解析 `_deny` 时，把 `Edit/Bash/Read/Write` 类规则复制一份加 `mcp__*:` 前缀（lossy 但能堵住常见情况）

#### T-C2 P1（原 T-A2/T-A3 强化，因 MCP 接入）
**位置**: `base.execute`（`base.py:126-208`，80 行，无 category-aware 分支）

**问题**: 原报告 T-A2 指出"base.execute 0 hook point"。MCP 接入后这一短板更明显——MCP 工具调用产生大量**新事件类型**需要 hook:
- `mcp tool call start/end`（vs builtin 工具的 start/end）
- `mcp server reconnected` / `mcp server disconnected`
- `mcp tools list_changed`
- `mcp call timeout` vs `mcp call error`

目前这些事件全靠 `logger.info/warning` 输出 + `_on_tools_changed` 回调（仅做 register/unregister）。**没有可观测性 hook point**——用户 UI 看不到"mcp__fs__write 调用失败 30s 超时"这类信息；审计日志（`audit_logger`）也**零 MCP 字段**（`grep mcp audit_logger.py` 无命中）。

**修法建议**:
1. 在 `ToolDef` 加 `category` 已被消费的位置（如 audit_logger）读取 `category="mcp"` 并在 audit 记录里加 `tool_category` 字段
2. 给 `ToolRegistry.execute` 加 `on_tool_call(category, name, duration_ms, status)` 回调，由 web/app.py 注入 Streamlit session_state 更新逻辑
3. McpManager 的 `_safe_on_tools_changed`（已存在）也可 emit 到统一事件总线

#### T-New1 P2（新增发现）
**位置**: `ToolDef.category` 字段（`base.py:43`）+ MCP `materialize.py:64` `category="mcp"`

**问题**: 字段已存在但 **没有任何消费方**:
- `audit_logger.py` 无 MCP 字段
- `permission_engine.py` 不读 category
- `safety_check.py` 不读 category
- `builtin.py` 设了 `category="shell"/"read"` 但下游 0 消费

字段存在即承诺——承诺失败让代码读者困惑。建议要么补消费者（推荐，配合 T-C2），要么从 dataclass 删字段（YAGNI）。

#### T-New2 P3（新增发现）
**位置**: `base.py:97-108` `unregister_by_prefix`

**问题**: 这是 **MCP 反向推动 tool 注册中心暴露生命周期 API 的产物**，但只支持"按前缀删除"，未配对支持:
- `register_many(tool_defs)`（批量注册，目前只能 for 循环）
- `list_changed` 信号（应由 Registry emit，外部监听而非组合根用 callback）
- `get_categories()`（UI 渲染时按类别折叠）

属于"开了头没收尾"，建议补 `register_many` 至少与 `unregister_by_prefix` 对称。

### 1.3 原报告发现与 MCP 兼容性

| 原报告 # | 标题 | MCP 影响 |
|---------|------|---------|
| T-A1 | tools 65% 是权限，包名失真 | **强化**：现在 `agent_core/mcp/` 独立子包专门做外部工具集成，`tools/` 包名歧义更明显 |
| T-A2 | 执行管线无 owner，3 层 smear | **部分削弱**：MCP 设计决策明确"base.py 零改动"，意味着建议加 hook point 是 known trade-off |
| T-A3 | base.execute 无 hook point | **强化**：见 T-C2 |
| T-M1 P0 | global rule 只匹配 tool_name | **升级 P0+**：见 T-M1（影响面扩展到所有 MCP 工具） |
| T-M2 P0 | PreToolUse 重复执行 | 不变（MCP 工具同样受影响） |
| T-M3 | check_permissions 460 行 monolith | 不变（MCP 工具同样走这条管线） |
| T-C1 P1 | Seatbelt 路径注入 | 不变 |
| T-C2-C5 | 重复/长方法/矛盾注释 | 不变 |

---

## 二、MCP 模块独立审查
> 6 文件 / 1,363 LOC,架构层 → 模式层 → 代码层全量精读。

### 2.1 架构层（基于《架构整洁之道》）

#### 拓扑合理性

**正面**:
- 4 模块职责清晰：`config`（数据）/`client`（连接）/`materialize`（适配）/`manager`（编排）/`names`（安全）——边界干净
- **零循环依赖**：manager → {client, materialize, names}；materialize → names → ToolDef（叶）
- `_LiveServer` 是清晰 bounded context；`_ServerHealth` 是 frozen dataclass 提供**跨线程读安全语义**（docstring 显式声明 GIL 假设）——是模块最值得学习的样板
- 配置面（config.py）零副作用，纯数据——可独立单元测试
- `__init__.py` 主动压制 SDK 噪音 log（`logging.getLogger(_name).setLevel(WARNING)`）——是细致的运维意识

**架构问题**:
1. **M2-1 P2：`McpManager` 3 大职责混合**（585 行，单 class）——编排 + health 调度 + 跨线程 callback 路由。`_LiveServer` 概念清晰但 `_ServerHealth`/`_pending_refresh`/`_active_agent`/`_active_registry` 都直接挂在 manager 上。建议抽 `McpHealthScheduler` 子类或独立类。
2. **M2-2 P2：`McpManager._run` 是同步门面，但协程超时与线程结果超时有双重超时机制**（`asyncio.wait_for` + `fut.result(timeout+2)`）——架构上正确，但 `_wrap` 协程嵌套了一层且没抽工具方法（manager.py:141-150）。抽 `_run_async(coro, timeout)` 私有 helper 即可（与 `_open_transport` 同级）。
3. **M2-3 P2：回调路径（`_on_tools_changed`）的 GIL 安全声明仅在 docstring**，没有 invariant test 守住——若将来 manager 跨多进程或换 GIL-less runtime 会 silent 失败。建议加 unit test: "callback 跑在 mcp loop 线程时访问 self._active_registry 不爆"。

### 2.2 模式层（GoF + SOLID）

**用得好的模式**:
- **Strategy + Abstract Factory**: `connect_server` 是 transport Strategy（stdio/http 2-tuple/3-tuple），`_open_transport` 是 Factory——新增 transport 只需加分支
- **Null Object + 工厂**: `_default_roots()` 是 Null Object 兜底
- **观察者 / 回调总线**: `_on_tools_changed` 是显式 callback，`message_handler` 嵌套 async→spawn task 解 receive_loop 自死锁——典型反应式回调链
- **Discriminated Union**: `McpServerConfig.kind: Literal["stdio", "http"]` 干净
- **Bounded Context**: `_LiveServer` 持有 session+tool_defs+resources+prompts，跨子系统共享的最小数据
- **Specification**（潜在）: `is_server_denied` 是 server-level spec；`make_tool_name` 是 tool name validator

**违反 SOLID 的位置**:

| 原则 | 位置 | 问题 | 修法 |
|------|------|------|------|
| SRP | manager.py (585行) | 编排 + health 调度 + 跨线程 callback 路由 + atexit 钩子 + 进程退出清理 5 职责 | 抽 `McpHealthScheduler` / `McpLifecycle` |
| OCP | client.py `_open_transport` | 加新 transport 需改 `if/elif` 分支（stdio/http） | 提 Transport 抽象类 + Registry |
| DIP | client.py → `mcp` SDK | 强依赖 mcp Python SDK，无 Protocol 抽象 → 难替换为 rust-mcp-sdk | 加 `McpClient` Protocol 接口（成本高，可标 P3） |
| LoD | manager.py `_make_message_handler` | 内部 try/except + 多层 isinstance 检查深嵌套 | 提 `_handle_server_notification(name, notif)` 顶层方法 |

**模式层问题清单**:

| # | 严重度 | 问题 | 位置 | 建议 |
|---|--------|------|------|------|
| M-M1 | P2 | `McpManager` SRP 违反 | manager.py 全文件 | 抽 `McpHealthScheduler` + `McpLifecycle` |
| M-M2 | P2 | `_open_transport` OCP 违反 | client.py:57-100 | 提 `Transport` ABC + registry（即使当前 2 种也划算，便于测试） |
| M-M3 | P2 | `manager._run` 超时双层嵌套（asyncio.wait_for + fut.result），可读性下降 | manager.py:131-150 | 提 `_run_async(coro, timeout)` helper |
| M-M4 | P2 | `manager.dispose` 7 步骤在单方法，try/except `RuntimeError: pass` 出现 4 次——**Signal Loss 模式** | manager.py:534-585 | 抽 `_safe_loop_call(coro_or_callable)` helper |
| M-M5 | P3 | manager 有显式 GIL 假设但无 invariant test 守住 | manager.py 全文件 | 加测试：callback 跑在 mcp loop 访问 _active_registry 不爆 |
| M-M6 | P3 | `McpServerConfig` 字段注释以 `# stdio 字段` / `# http 字段` 区分，但**LSP 风险**：传 `kind="http"` 的 config 给 stdio-only 函数会 silently None | config.py:42-54 | 拆成 `StdioServerConfig` / `HttpServerConfig` 子类（避免 if-cfg.kind 散落） |

### 2.3 代码层（基于《重构》）

**坏味道诊断**:

| # | 坏味道 | 位置 | 严重度 | 推荐重构手法 |
|---|--------|------|--------|-------------|
| 1 | `try/except RuntimeError: pass` 重复 4 次 | manager.py:545-559, 575-577 | P2 | Extract Method `_safe_loop_call_soon_threadsafe` |
| 2 | `manager.dispose` 7 步骤单方法 50 行 | manager.py:534-585 | P2 | Extract Method `_stop_health_task` / `_signal_dispose_events` / `_stop_loop_thread` |
| 3 | Materialize 三个 `_make_*_handler` 闭包重复 `_cancel_event` 弹出模式 | materialize.py:83-91, 181-185, 188-192, 243-247 | P2 | Extract `handler_factory(manager, fn, **kwargs)` 统一工厂 |
| 4 | `_normalize_*_result` 三函数形态相似（content 遍历 + 占位符 + sanitize） | materialize.py:94-130, 195-213, 250-271 | P2 | Extract `_content_blocks_to_text(contents)` 共享 |
| 5 | `manager._run` 嵌套 `_wrap` 闭包 | manager.py:131-150 | P2 | Inline `_wrap` → 把 `task.cancel()` 直接放进 except |
| 6 | 魔法数字: `timeout+2` / `timeout+5` / `timeout: int=12.0` | manager.py:150, 481, 510, 569 | P3 | 命名常量 `_FUT_TIMEOUT_BUFFER_S = 2.0` |
| 7 | `_check_global_*_rule` 三个方法形态重复 | permission_engine.py:605-633（已记录于原报告 T-M1） | 引用 | 见 T-M1 |
| 8 | `parse_mcp_servers` / `parse_mcp_roots` 防御性 `isinstance` 链重复 | config.py:63-69, 153-160 | P3 | 提 `_get_mcp_section(settings)` helper |

**代码层 P1+ 列表**:

| # | 严重度 | 问题 | 位置 | 建议 |
|---|--------|------|------|------|
| M-C1 | P1 | `manager._safe_on_tools_changed` 与 `_do_refresh` 内嵌 on_tools_changed 块重复 | manager.py:249-261, 458-462 | Extract `notify_tools_changed(name)` 内部 helper |
| M-C2 | P1 | `manager.connect_all` 20+ 行内混 enabled 过滤 + deny strip + connect + health init + 启调度器 5 件事 | manager.py:153-190 | Extract Method `_should_connect(name, cfg)` + `_connect_one_sync` |
| M-C3 | P2 | Materialize 三 handler 闭包 + 三 normalize 函数形态重复 | materialize.py:83-91, 181-192, 243-247 + 94-130, 195-213, 250-271 | 提 2 个 helper（handler factory + content block→text） |

**正面代码细节**:
- 子 logger 命名规范（`agent_core.mcp` + 🔌 emoji 前缀）——延续项目偏好
- docstring 普遍含"为什么"+ "对齐决策 #X"引用——决策可追溯
- `try/except RuntimeError: pass` 处虽多，但**注释明确说"loop 已停"**——知情吞错而非 silent
- `make_tool_name` 安全清洗 + 64 字符截断 + warning 日志——防御到位
- `is_server_denied` 防前缀误匹配（`mcp__fs` 不命中 `mcp__fs2`）——细节正确

---

## 三、问题归因分析

```
因果链:

A. 架构层 ──────────────────────────────────────────
   T-A1 (tools 包名失真)
      + M2-1 (McpManager SRP 违反, 585行)
      → 工具相关代码分散在 agent_core.tools 和 agent_core.mcp 两包
      → 组合根 web/app.py 80 行手写胶水

B. 模式层 ──────────────────────────────────────────
   T-M1 (engine 全局规则只匹配 tool_name)
      + M-M2 (transport OCP 违反)
      → MCP 工具命名 mcp__<server>__<tool> 与 builtin 命名空间隔离
      → 用户 settings.json 的内容级规则 100% 失效（T-M1 升级 P0+）
      → Bash 有专用旁路幸免, MCP 无专用旁路, 完全裸奔

C. 代码层 ──────────────────────────────────────────
   M-C1 + M-C2 (manager SRP 违反在代码层表征为长方法+重复)
      + T-C2 (base.execute 无 hook point)
      → MCP 工具调用无 category-aware 可观测性 hook
      → audit_logger / UI 都看不到 mcp__fs__write 调用细节
      → T-New1 (category 字段死代码)
```

**最关键因果链**: `T-M1 工具命名空间隔离 + 缺专用旁路 → MCP 工具完全裸奔 → 内容级 deny/ask 失效 → 用户误以为安全`。

---

## 四、改进路线图（优先级合并）

### P0 — 立即修复（1 周内）

| # | 改动 | 工作量 | 影响 |
|---|------|--------|------|
| **T-M1** | engine 接入 `matching_rules_for_input` + 扩展 `_parse_rule_str` 支持 `mcp__<server>__<tool>(...)` 语法 | 1-2 天 | 修全局 P0 漏洞，MCP 工具走内容级规则 |
| **T-C2** | `ToolDef.category` 字段补消费者：`audit_logger` 加 `tool_category` 字段 + `ToolRegistry.execute` 加 `on_tool_call` 回调 | 1 天 | MCP 工具调用可观测 |
| **T-New1** | 决策 category 字段是补消费（配合 T-C2）还是删字段 | 0.5 天 | 选边 |

### P1 — 短期改进（2 周内）

| # | 改动 | 工作量 |
|---|------|--------|
| **M-C1** | 提 `notify_tools_changed` helper | 0.5 天 |
| **M-C2** | `connect_all` 拆 3 个 helper | 0.5 天 |
| **M-M1** | 抽 `McpHealthScheduler`（可选） | 1 天 |
| **M-C3** | Materialize 提 handler factory + content block→text helper | 0.5 天 |
| **T-New2** | 补 `register_many` 与 `unregister_by_prefix` 对称 | 0.5 天 |

### P2 — 中期优化（1 个月内）

| # | 改动 | 工作量 |
|---|------|--------|
| **M-M2** | `Transport` ABC + registry | 1 天 |
| **M-M3** | `manager._run` 提 `_run_async` helper | 0.5 天 |
| **M-M4** | `manager.dispose` 抽 `_safe_loop_call_soon_threadsafe` | 0.5 天 |
| **T-A1** | 文档化 `agent_core/tools/` 仅承担"本地内置工具"职责，与 `agent_core/mcp/` 边界 | 0.5 天（文档） |

### P3 — 长期改善

| # | 改动 | 工作量 |
|---|------|--------|
| **M-M5** | 加 GIL 安全 invariant test | 0.5 天 |
| **M-M6** | `McpServerConfig` LSP 改造（拆 Stdio/Http 子类） | 1 天 |
| **T-A2/A3** | `base.execute` 加 Pre/PostExecute hook point（与 MCP 设计决策"零改动"权衡） | 2 天 — 需先与决策者对齐是否升级决策 |

---

## 附录 A：未在本报告展开的原报告发现

以下原报告发现（[tools-code-review-2026-07-09.md](tools-code-review-2026-07-09.md)）与 MCP 接入无直接交叉，本报告不重复:

- T-M2（P0 PreToolUse 重复执行）— MCP 工具同样走 check_permissions 路径，行为一致
- T-M3（P1 check_permissions 460 行）— 同上
- T-A4/T-A5/T-A6（P1/P2 sandbox 配置访问 / facade 缺失 / 后端策略不一致）— 与 MCP 无关
- T-M4-M8（SRP/OCP/NullBackend LSP/Template Method/YAGNI）— 同上
- T-C1（C1 Seatbelt 注入）— MCP 工具默认不在 sandbox 内执行（handler 走 asyncio run_coroutine_threadsafe，绕 sandbox）
- T-C3-C5（长方法 / Data Clump / 注释矛盾）— 同上
- 全部 P2/P3 代码层细节

## 附录 B：MCP 测试矩阵（实测覆盖）

| 测试文件 | 覆盖范围 |
|---------|---------|
| test_mcp_manager.py | manager 同步门面 / dispose 幂等 / health 快照 |
| test_mcp_hello_e2e.py | e2e 连接 + tool call |
| test_mcp_primitives_e2e.py | tools / resources / prompts 三种物化 |
| test_mcp_resources.py | materialize_resource_tools / _normalize_resource_result |
| test_mcp_prompts.py | materialize_prompt_tools / render_mcp_prompts_section |
| test_mcp_list_changed.py | 动态刷新 + on_tools_changed 回调 |
| test_mcp_reconnect.py | 断连 → 调度器 → 重连 |
| test_mcp_unregister.py | ToolRegistry.unregister / unregister_by_prefix 边界 |
| test_mcp_roots.py | client roots capability + 解析 |
| test_mcp_e2e.py | 端到端（组合根层） |

**测试覆盖评级**: A-（覆盖 primitives / lifecycle / reconnect / roots，**唯一缺口**：`_safe_on_tools_changed` 异常吞掉的 invariant test；MCP 工具被 PermissionEngine 拒绝的端到端测试）

## 附录 C：审查依据

- 《架构整洁之道》— 组件划分、依赖规则、边界、SRP/OCP/SRP 检验
- 《设计模式》+ SOLID — Strategy、观察者、Abstract Factory、Specification、Null Object、SRP/OCP/DIP/LoD
- 《重构：改善既有代码的设计》— Long Method、Duplicated Code、Feature Envy、Data Clump、Magic Numbers、Try-catch-pass Signal Loss

## 备注

- 本报告为只读审查，未改动任何代码。
- MCP 子系统的实现质量是项目内子系统的**新基准**（决策驱动 + 边界清晰 + 测试充分 + 文档注释充分），建议作为其他子系统（sandbox / permission / audit）的对照样板。
- 原报告所有 P0/P1 发现仍然成立，本报告唯一新增/升级的是 **T-M1 P0 → P0+**（MCP 工具绕内容级规则的攻击面扩展）。
- 决策锚点：MCP 决策 #9 明确"base.py 零改动（C-浅 不做）"——T-A2/A3 的 hook point 建议需先与决策者确认是否升级该决策，否则属于 known trade-off。