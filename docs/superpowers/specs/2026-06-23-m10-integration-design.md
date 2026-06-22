# M10 记忆系统全集成 — 设计 Spec

> **For agentic workers:** 本 spec 是 M10 实施的 source of truth,所有 task 围绕它展开。涉及多文件改动和跨模块协调,须通过 writing-plans → subagent-driven-development 落地。

**日期**: 2026-06-23
**状态**: Draft → 等待用户 review
**目标**: 把 agent_core/memory/ 下"已构建但未集成"的所有模块按 design doc §4-§14 真正接到自研 ReAct 流程,补齐 17 项 missing entirely + 20 项 built-not-wired

---

## 〇、范围与决策

### 范围(scope)

完整 M10:从 gap analysis 列出的 17 项 missing entirely + 20 项 built-not-wired 全部接入。约 **20+ 独立 task**,按以下 6 个 cluster 组织:

| Cluster | 范围 | Task 数 |
|---------|------|---------|
| **C1: P0 安全洞** | §14.1 路径校验 + §14.4 密钥净化 + §14.3 文件权限 | 4 |
| **C2: L3 Fast Path** | §4.3/§4.4 SessionMemoryLayer 接入 run() | 3 |
| **C3: autoDream 真跑** | §7 DistillationLoop 启动 + §13.3 status | 3 |
| **C4: 蒸馏产出可观测** | §13.4 candidate review UI + §7.4 dry_run diff | 4 |
| **C5: 写入路径强化** | §5.3.1 type 校验 + §5.3.2 Why-How + §6.9 dedup + §4.6 fork router | 4 |
| **C6: UI/可观测完整化** | §13.6 OTel + §13.7 cost + §13.8 latency + §12.3 runtime switch + §4.7 5 回退横幅 | 5 |
| **合计** | | **~23 tasks** |

### 决策(已与用户对齐)

1. **Secret 处理策略** = 净化后存(sanitize → 保留 redacted body,不丢失信息)
2. **Distill 调度频率** = 10 min 检查间隔 + 4 重门(§7.1: 24h + 5 session + 锁 + token)
3. **Candidate review UI** = Sidebar expander 提醒 + 独立 Page 详细(双入口)
4. **执行模式** = Subagent-Driven(每个 task 派 fresh subagent + 任务级 review)
5. **测试策略** = TDD(strict,每个 task 先写失败测试,再实现)
6. **commit 策略** = 每个 task 一个 commit,最终全分支 review 一次

### 非范围(out of scope)

- 真实 LLM 调用 mock 化(沿用现有 mock router 模式)
- 大规模 LLM 蒸馏实验(只验证 schema + 路径)
- Hybrid 检索的 LLM rerank 实际性能调优(只跑通通路)
- Cost budget 真实计费(只实现 budget 框架 + 关闭检查)

---

## 一、Cluster C1: P0 安全洞

### C1.1 MemoryPathValidator 接入 MemoryStore.write

**问题**: `agent_core/memory/path_validator.py:86` 实现了 4 层路径防御,但 `MemoryStore.write(memory_store.py:152)` 用裸 `Path` 拼接,完全绕过。

**目标**: 每次 write 之前调 `MemoryPathValidator.validate(rel_path)`,失败抛 `PathSecurityError`。

**影响范围**:
- `agent_core/memory/memory_store.py` — write 入口加 validator 调用
- 新增 `tests/test_path_validator_in_write.py` — 3 cases(合法路径 / `..` 穿越 / 绝对路径)

**验收**: 3 tests pass + 现有 370 行 memory_store 测试 0 回归

### C1.2 SecretScanner + MemoryEditor.sanitize 接入 Channel B

**问题**: `secret_scanner.py:139` 和 `memory_editor.py:99` 已实现,但 `DualChannelWriter._do_channel_b_extract`(`dual_channel_writer.py:372`)写盘前不调。

**目标**: 写盘前顺序: `scan_text(candidate.body)` → 命中则 `sanitize(body)` 净化 → 写盘。净化后若仍命中(罕见),整条丢弃 + 推 `SecretDetected` memory_event。

**影响范围**:
- `agent_core/memory/dual_channel_writer.py:372` — 加 sanitize hook
- `web/app.py:900-913` — `MemoryEventKind` 增加 `SECRET_DETECTED` 分支
- 新增 `tests/test_channel_b_secret_sanitize.py` — 3 cases

### C1.3 chmod 0o600 文件权限

**问题**: `MemoryStore.write` 用 `open(..., "w")`,权限 OS 默认(0644),多用户机器可读。

**目标**: write 后 `os.chmod(path, 0o600)`。

**影响范围**:
- `agent_core/memory/memory_store.py` — write 后 chmod
- `tests/test_memory_store.py` — 加 1 case 验证权限

### C1.4 MemoryPathValidator 接入 DualChannelWriter

**问题**: 同 C1.1,但 Channel B 写盘路径独立。

**目标**: Channel B 写盘前同样调 validator(防止 extractor 生成的候选路径有穿越)。

**影响范围**: `dual_channel_writer.py` + 测试

---

## 二、Cluster C2: L3 Fast Path

### C2.1 SessionMemoryLayer 接入 ReactAgent.run()

**问题**: `sm_layer.py:113` 实现 SM(滚动摘要)零 LLM fast path,但 `ContextManager.check_and_compact`(`agent_core.py:382`)完全绕过。

**目标**: 改写 compact 决策:
1. 先调 `sm_layer.should_trigger_compact(ctx)` 决定是否走 L3 fast path
2. 走 L3: `sm.compact(messages)` 读 SM 文件,零 LLM,100ms 完成
3. 不走 L3: fallback `ContextManager.check_and_compact` 传统 LLM 压缩
4. decision 通过 `memory_status` chunk 上报 UI(`sm_compact: True/False`)

**影响范围**:
- `agent_core/agent_core.py:382` — compact 决策改写
- `agent_core/memory/sm_layer.py` — 可能要补 `should_trigger_compact` 接口
- `web/app.py:886-898` — `memory_status` 增加 `sm_compact` 字段
- `tests/test_sm_layer_integration.py` — 4 cases(走 L3 / fallback / decision 上报)

### C2.2 SM 文件持久化

**问题**: SM 摘要写在哪里?需定 path 约定。

**目标**: `~/.agent_data/memory/sm/<session_id>.json`(独立于 user/feedback/project 子目录,避免和 4 sealed type 冲突)。

**影响范围**: `sm_layer.py` 持久化函数

### C2.3 SM 跨会话缓存(可选,L4 蒸馏原料)

**目标**: SM 文件作为 L4 distillation 的输入候选(已经在 `distiller.py:155` 引用),保证 SM 写出时 `distiller` 能读到。

---

## 三、Cluster C3: autoDream 真跑

### C3.1 DistillationLoop 启动 + 关停

**问题**: `scheduler.py:41` 实现了 DistillationLoop,但 `get_agent()` / `agent.close()` 都不启停。

**目标**:
- `get_agent()` 末尾:`distillation_loop = DistillationLoop(scheduler, ...); loop.start()`(后台 thread)
- `agent.close()`:`loop.stop()`(graceful join)
- 10 min 检查间隔 + 4 重门(24h 上次运行时间 + 5 session 累计 + 锁可用 + token > 阈值)

**影响范围**:
- `web/app.py:526-577` — get_agent 加 loop.start
- `agent_core/agent_core.py:793` — close 加 loop.stop
- `tests/test_distillation_loop_lifecycle.py` — 3 cases(启动 / 停止 / 4 重门不通过)

### C3.2 autoDream 状态行 (Sidebar)

**问题**: 设计 §13.3 要求 "Auto-dream: last ran Xh ago" 状态行,目前 sidebar 完全没。

**目标**: sidebar expander "🌙 Auto-dream" 显:
- 状态: 待机 / 跑中 / 上次错误
- 上次运行时间(相对)
- 已生成候选数
- 锁状态

**实现**: `agent_core/memory/scheduler.py` 暴露 `get_status()` → `web/app.py` sidebar 调

**影响范围**: `web/app.py` sidebar section

### C3.3 Candidate 写盘路径(§7.4 dry_run)

**问题**: distiller.py:123 写候选到 `_candidate/` 但没看到路径。

**目标**: 候选写到 `~/.agent_data/memory/_candidate/<distill_run_id>/<candidate_hash>.md`(不与 4 sealed type 混)。

**影响范围**: `distiller.py` + 新增 `tests/test_candidate_layout.py`

---

## 四、Cluster C4: 蒸馏产出可观测

### C4.1 Sidebar "📥 待审记忆 N 条" 提醒

**目标**: sidebar 顶部 expander,展开显示 5 条最近候选的 title + type + 来源 session + 时间。点 "查看" 跳到独立 page。

**影响范围**: `web/app.py` 新 sidebar block + `pages/2_Candidate_Review.py` 新建

### C4.2 Candidate Review 独立 Page

**目标**: `pages/2_Candidate_Review.py`(或 `pages/Candidate_Review.py`):
- 表格列出 `~/.agent_data/memory/_candidate/` 全部候选
- 每行:title / type / session_id / 长度 / Accept / Edit / Reject / Skip 按钮
- 选中后 diff 视图(候选 vs 已存在的同名 memory)
- Accept → 移到 `user|feedback|project|reference/` + `meta.db` 注册
- Edit → 弹 streamlit text_area,改后保存
- Reject → 删除候选 + `distiller.record_rejection(hash, reason)`
- Skip → 仅关闭,不删

**影响范围**: 新建 `pages/candidate_review.py` + `tests/test_candidate_review_actions.py` 6 cases

### C4.3 dry_run 模式(§7.4)

**目标**: distiller 跑两遍:第一遍 dry_run 只生成候选,等用户审完再正式合并。

**影响范围**: `distiller.py` 改 `run(mode='dry_run' | 'merge')`

### C4.4 候选 review 后状态回灌 distiller

**目标**: Accept/Reject 决策写入 `meta.db.candidate_decisions` 表,distiller 下次运行时 skip 已 review 的。

**影响范围**: `meta_db.py` 加表 + `distiller.py` 读表

---

## 五、Cluster C5: 写入路径强化

### C5.1 4 sealed type 校验

**问题**: `validate_type` 在 `types.py:39` 但 `MemoryStore.write` 不调。

**目标**: write 前 `validate_type(memory_type)`,失败抛 `ValueError`。

**影响范围**: `memory_store.py` + 测试 2 cases

### C5.2 Why-How 模板校验

**问题**: `validate_body` 检查 `feedback/project` 需 `**Why:**` 字段,但 write 不调。

**目标**: write 前调 validate_body,失败标 `invalid: missing Why` 但**仍写入**(只是 `meta.db.invalid_memories` 表记录),不阻塞提取。

**影响范围**: `memory_store.py` + `meta_db.py` 加表

### C5.3 Gate-1 周期内去重 prompt

**问题**: `prompt_templates.build_extract_prompt` 实现 `<existing_memories_in_this_period>` 块但 `react_memory_bridge._call_llm` 不消费。

**目标**: bridge 调 LLM 前 `existing = memory_store.list_by_session(session_id, since_turn=gate1_period_start_turn)`,把 `existing` 喂给 `build_extract_prompt`。

**影响范围**: `react_memory_bridge.py:103-184` + 测试 3 cases

### C5.4 Fork 独立 Router(§4.6)

**问题**: 主 agent + 提取 agent 共享 router → cache 互相污染(主 turn 的 system 跟 extract 评分混)。

**目标**: `get_agent()` 创建独立 `extract_router = LLMRouter(config, cache_namespace="memory_extractor")`,`ExtractionGate(llm_router=extract_router, ...)`,与主 router 物理隔离。

**影响范围**: `web/app.py:561-565` + 测试 2 cases(确认 cache_namespace 独立)

---

## 六、Cluster C6: UI / 可观测完整化

### C6.1 OTel tracer 包裹(§13.6)

**问题**: `tracing.py:29` 暴露 `tracer`,NoOp 默认,production code 路径不包。

**目标**:
- `get_agent()` 末尾调 `configure_tracing(service_name="agent_dev", exporter="console")` (dev) 或 `"otlp"` (prod,env 控制)
- 4 个关键路径包 span:
  - `MemoryRetriever.search` → span "memory.search"
  - `ExtractionGate.should_extract` → span "memory.extract"
  - `DistillationLoop.tick` → span "memory.dream"
  - `SessionMemoryLayer.compact` → span "memory.sm_compact"
- NoOp 模式下 `tracer.start_as_current_span` 不做任何事(已实现)

**影响范围**: `web/app.py:526-577` + 4 个 memory 模块顶部 + 测试 2 cases

### C6.2 Cost budget guard(§13.7)

**问题**: `daily_budget_usd` / `per_extract_budget_usd` 没接。

**目标**:
- `agent_core/memory/config.py:81` 新增 `CostConfig(daily_budget_usd, per_extract_budget_usd, ...)`
- `LLMRouter.chat()` 调用后累计 `cost`,到达预算则 raise `BudgetExceeded` → bridge 推 `extract_error: budget_exceeded` 事件
- 默认配置从 `.agent_config.json` 读,fallback `daily_budget_usd=1.0`, `per_extract_budget_usd=0.05`

**影响范围**: `config.py` + `llm/router.py` + `react_memory_bridge.py` + 测试 4 cases

### C6.3 Latency budget drop(§13.8)

**问题**: 提取 LLM 超时无动作,主流程被拖死。

**目标**:
- `CostConfig.latency_budget_p95_ms = 8000`
- bridge 调 LLM 用 `concurrent.futures` + 8s timeout,超时则 drop 候选 + 推 `extract_error: timeout`
- 主 turn 流照常,不卡

**影响范围**: `react_memory_bridge.py` + 测试 3 cases

### C6.4 Runtime config switch(§12.3)

**问题**: 改 mode(retrieval_mode / cost budget)需重建 agent。

**目标**:
- `agent_core/memory/config.py` 加 `set_runtime(key, value)` 改 `.agent_config.json` 中 runtime 区
- `web/app.py` 监听改动 → 不重建 agent,只调对应组件 reload(若 cost 改 → bridge 重新读 budget)

**影响范围**: `config.py` + `web/app.py` + 测试 2 cases

### C6.5 5 回退条件 UI 横幅(§4.7)

**问题**: 提取错误只 `extract_error` 静默,用户看不到。

**目标**: sidebar 顶部 "⚠️ 记忆系统降级中" 红色 banner:
- 显示最近 1 个 extract_error
- 5 种回退原因图标:
  - `lock_busy` → 锁忙
  - `rate_limited` → 速率限制
  - `budget_exceeded` → 预算超
  - `timeout` → 超时
  - `secret_detected` → 检测到 secret
- 点击 "🔄 重置" 按钮清空 banner

**影响范围**: `web/app.py` + `MemoryEventKind` 增 5 个 enum + 测试 2 cases

---

## 七、验证 / 测试策略

### TDD 顺序(每个 task)

1. **Step 1**: 写失败测试
2. **Step 2**: 跑测试,确认 fail
3. **Step 3**: 最小实现让测试 pass
4. **Step 4**: 跑全 M10 套件确认 0 回归
5. **Step 5**: commit

### 总测试数预估

| Cluster | 新 case | 修改 case | 合计 |
|---------|---------|----------|------|
| C1 安全 | 8 | 1 | 9 |
| C2 L3 | 4 | 0 | 4 |
| C3 autoDream | 3 | 0 | 3 |
| C4 UI | 6 | 0 | 6 |
| C5 写入 | 10 | 0 | 10 |
| C6 可观测 | 13 | 0 | 13 |
| **合计** | **44** | **1** | **45** |

加上现有 112 = **总 157 个 test case**。

### 端到端验证(全分支 review 后)

- 启动 web UI → 启用 memory → 发 5 句含 "记住" / "我讨厌" 等偏好 → 检查 sidebar memory_stats 累计 + `~/.agent_data/memory/user/` 出现 5 个 md
- 启 autoDream → 等 10 min → 检查 `~/.agent_data/memory/_candidate/` 出现候选 → sidebar "🌙 Auto-dream" 显示 "last ran 0h ago"
- candidate review page → Accept 一个 → 该 md 移到 `user/`
- 写含 `sk-xxx` 的"记忆" → 验证 sanitize 净化后存储
- 写含 `../../etc/passwd` 的路径 → 验证 `PathSecurityError`
- 关 budget 到 $0 → 跑提取 → 验证 `BudgetExceeded` + 红色 banner

---

## 八、风险与边界

### 已知风险

1. **distiller.py 670 行 + scheduler.py 177 行同时改**: C3/C4 task 间可能 merge conflict → plan 拆细粒度
2. **sm_layer + agent_core.py 同时改 compact 路径**: C2 task 风险高,需 strong 集成测试
3. **OTel + 现有 NoOp tracer 同时存在**: 双重 import 容易混淆 → 严格用 `configure_tracing(service_name)` 模式
4. **streamlit 异步 task 不友好**: DistillationLoop 后台 thread 与 streamlit session_state 线程不安全 → 用 threading.Lock 包裹共享状态

### 边界

- **不做** LLM 蒸馏真实数据实验(只验证 schema + 路径)
- **不做** hybrid 检索的 LLM rerank 真实性能 benchmark(只跑通通路)
- **不做** CostConfig 真实计费(只实现 budget 框架 + 关闭检查)
- **不做** 大规模 cold start seed 内容(只验证 loader 跑通)
- **不做** OTel exporter 真实 OTLP 上报(只 console, dev 模式)

---

## 九、commit 计划

预计 **23 个 commit**,按 cluster 顺序:

```
C1.1: feat(memory): MemoryPathValidator 接入 MemoryStore.write
C1.2: feat(memory): SecretScanner + sanitize 接入 Channel B 写盘
C1.3: feat(memory): chmod 0o600 写记忆文件
C1.4: feat(memory): PathValidator 接入 DualChannelWriter
C2.1: feat(sm): SessionMemoryLayer 接入 run() compact 决策
C2.2: feat(sm): SM 文件持久化到 sm/<session>.json
C2.3: feat(distill): SM 跨会话缓存作为 L4 输入
C3.1: feat(distill): DistillationLoop 启动/关停接入
C3.2: feat(ui): sidebar Auto-dream 状态行
C3.3: feat(distill): candidate 写盘路径 _candidate/<run>/
C4.1: feat(ui): sidebar 待审记忆提醒 expander
C4.2: feat(ui): Candidate Review 独立 page(Accept/Edit/Reject/Skip)
C4.3: feat(distill): dry_run 模式
C4.4: feat(distill): review 决策回灌(跳过已审)
C5.1: feat(memory): validate_type 接入 write
C5.2: feat(memory): validate_body + invalid_memories 表
C5.3: feat(memory): Gate-1 周期内去重 prompt
C5.4: feat(memory): Fork 独立 extract_router(cache 隔离)
C6.1: feat(otel): tracer 包裹 4 个 memory 路径
C6.2: feat(memory): Cost budget guard
C6.3: feat(memory): Latency budget drop
C6.4: feat(config): runtime switch 不重建 agent
C6.5: feat(ui): 5 回退条件 banner
```

最后 **1 个全分支 review commit**(如果需要)+ **1 个 docs commit**(IMPLEMENTATION_PLAN.md 加 M10 Day 10 section)。

---

## 十、依赖顺序(DAG)

```
C1.1 ─┬─→ C1.2 ─→ C1.3
      │              │
      ↓              ↓
    C1.4         C2.1 ─→ C2.2 ─→ C2.3
                                │
                                ↓
                          C3.1 ─→ C3.2 ─→ C3.3
                                              │
                                              ↓
                                        C4.1 ─→ C4.2
                                                │
                                                ↓
                                          C4.3 ─→ C4.4

C5.1 ─┬─→ C5.2
      │
      ↓
    C5.3 ─→ C5.4

C6.1 ─→ C6.2 ─→ C6.3
                │
                ↓
              C6.4
                │
                ↓
              C6.5
```

**关键依赖**:
- C1.1 必须先做(MemoryStore 是其他改动的基础)
- C2.1 / C3.1 / C5.1 是不同 cluster 的入口,互相独立
- C6 链最后做(汇总所有 memory 路径的可观测)
- C4.2 依赖 C4.1 / C4.3 / C3.3

---

**Spec 完。等待用户 review,review 通过后进入 writing-plans 阶段。**
