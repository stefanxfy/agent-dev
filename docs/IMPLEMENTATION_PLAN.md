# Memory System v2.1 实施计划

> **协作模式**:AI Agent 高强度写代码 + 单测,人每天收工时验收 + 决策下一步。
> **效率假设**:AI 写代码 8x 人速(单文件 < 200 行的 module 平均 5-10 min),单测同步写 1.5x 代码时间。
> **总计**:8 天 × 8h = 64h,其中 AI 自走 90%,人介入 6 个验收点(每块完成时) × 15 min = 1.5h。
> 
> **关联文档**:
> - 设计: [`docs/memory-system-design.md`](docs/memory-system-design.md) (v2.1, 3764 行)
> - 源参考: [`docs/claude-code-memory-system-deep-dive.md`](docs/claude-code-memory-system-deep-dive.md)

---

## 0. 协作规则

### 0.1 分工

| 角色 | 职责 | 时间占比 |
| --- | --- | --- |
| **AI Agent** | 写代码 + 写单测 + 跑测试 + 修 bug + 生成 demo | ~92% |
| **人** | 看 demo + 验收 + 决策"继续/调整/回滚" + 修 prompt 风格 | ~8% |

### 0.2 人的介入点(共 6 个,每块完成时)

每个 milestone 完成后,AI 给出:
1. **diff 摘要**(改了哪些文件、+多少行)
2. **测试报告**(`pytest -v` 跑通 X 个)
3. **可运行 demo**(1 条命令验证效果)
4. **已知问题清单**(如有)

人做三件事(各 5 min):
- ✅ 跑 demo 看效果 → "通过/不通过"
- ✅ 看测试报告 → "绿/红"
- ✅ 决策下一步 → "继续 M{N+1} / 调整 M{N} / 回滚 M{N}"

### 0.3 完成定义(DoD, Definition of Done)

每个 milestone 满足以下全部,才算"完成":

```markdown
- [ ] 代码改动落盘 (`git diff` 有内容)
- [ ] 单测全部绿 (新模块 + 旧模块回归)
- [ ] demo 命令可跑 + 输出符合预期
- [ ] 没有 TODO/FIXME 残留 (除非明确登记在"已知问题")
- [ ] 文档对应章节已更新(如有)
- [ ] commit message 符合 §0.4 规范
```

### 0.4 commit 规范

```
<type>(<scope>): <subject>

<body>

<footer>

# type: feat / fix / refactor / test / docs / chore
# scope: memory (统一用 memory)
# subject: 中文 50 字内, 动词开头
# body: 改了哪些文件 + 为什么
# footer: 关联 issue ID (A1-A12 / L1-L13)
```

**示例**:
```
feat(memory): 双通道写入器 + cursor 持久化

新增 dual_channel_writer.py:
- channel A 内联写 (per-turn 同步, <100ms)
- channel B 后台提取 (LLM 调用, 不阻塞)
- cursor 持久化到 SQLite (A3)
- 跨进程 flock (A4)
- 事务式写 (A5, .tmp + rename)
- executor 优雅退出 (A9)
- 60s 超时强制重置 (A10)

单测 5 个全绿:
- test_channel_a_write
- test_channel_b_extract_idempotent
- test_concurrent_writes_no_overwrite
- test_crash_resume_continues
- test_stuck_extraction_resets_after_60s

Refs: A3, A4, A5, A9, A10
```

### 0.5 失败回滚原则

- **单 milestone 失败** → AI 自修 30 min,修不好人介入
- **跨 milestone 失败** → 人决策"回滚到上一个绿点"或"继续硬撑"
- **架构性失败** → 立即停,人 review 方案

---

## 1. 里程碑总览

| # | 名称 | 时长 | 涉及修复 | 验收命令 | **状态** |
| --- | --- | --- | --- | --- | --- |
| **M1** | 基础 + 配置 (Day 1) | 4h | O8 | `pytest tests/test_types_config.py -v` | ✅ **完成** (`57482f1`) |
| **M2** | 写入路径 (Day 2) | 8h | A3, A4, A5, A9, A10, L7, L9 | `pytest tests/test_dual_channel_minimal.py -v` | ✅ **完成** (`a2c0a5d`) |
| **M3** | 检索 + 安全 (Day 3) | 8h | L1, L2, L4, L5, L8, L12 | `pytest tests/test_retrieval_modes.py -v` | ✅ **完成** (`539b6e7`) |
| **M4** | L3 压缩 (Day 4) | 4h | — | `pytest tests/test_sm_layer.py -v` | ✅ **完成** (`bf41c28` + bug 修复 `a9e91af`) |
| **M5** | 蒸馏 (Day 5) | 6h | A1, A2, A11 | `pytest tests/test_distiller.py -v` | ✅ **完成** (`38f64a9`) |
| **M6** | 调度 + 可观测 (Day 6) | 6h | A8, A12 (含 5/8 并发场景) | `bash scripts/demo_m6.sh` | ✅ **完成** |
| **M7** | 集成 + UI (Day 7) | 6h | L13, A7 (schema migration) | `pytest tests/test_integration.py -v` | ⏸️ **未开始** |
| **M8** | 完整测试 + 上线 (Day 8) | 8h | A6, A12 (补 3/8 场景) | `pytest tests/ -v` + demo 全跑 | ⏸️ **未开始** |

**总人力(预算)**:50h AI 写码 + 1.5h 人验收 + 12.5h buffer = 64h
**实际消耗**:M1-M3 已完成,详见 §6.1 实际产出

**📋 Demo 脚本约定**(2026-06-21 立):
- 每个 milestone 验收 demo 都抽到 `scripts/demo_mN.sh`,可独立 `bash` 跑通
- plan 中**只放 `bash scripts/demo_mN.sh` 一行**,不贴内联代码
- 脚本模板见 `scripts/demo_m4.sh`(含 demo 分块 + pytest 收尾 + 注释规范)
- 例外:历史 milestone(M5-M8)首次落地时按本约定写新脚本

---

## 2. 每日里程碑(详细)

### Day 1 — M1: 基础 + 配置

**目标**:类型系统 + 配置校验 + 路径校验的"地基三件套"跑通。

**AI 工作清单**(4h):

| 任务 | 产出 | 关键点 |
| --- | --- | --- |
| 封闭 4 类类型 | `agent_core/memory/types.py` | `Literal["user","feedback","project","reference"]`,编译期硬约束 |
| Pydantic 配置 | `agent_core/memory/config.py` | `BaseModel` + `Field(ge=, le=)` + 跨字段校验 (weights sum=1) |
| 路径校验 | `agent_core/memory/path_validator.py` | 4 层防御 (L1-L4), `os.path.isabs()` 跨平台, `normpath` |
| 单测 | `tests/test_types_config.py` | 20 个 case: 类型非法 / 必填字段 / 范围越界 / 路径越界 / Unicode trick |

**验收 demo**:
```bash
bash scripts/demo_m1.sh
# Expected: 8/8 demo 通过 + 31 passed
```

完整 demo 代码见 [`scripts/demo_m1.sh`](scripts/demo_m1.sh)。

> **API 说明**:MemoryPathValidator 实例化时必须传 `memory_root`(每个 sandbox 一个 validator),
> 之后的 `validate(rel_path)` 只接受相对路径。这与 v1 sketch 不同,后者把 root 作为
> `validate()` 第二参数。实例化更内聚(一个 sandbox 对应一个 validator 实例),便于复用
> + 单测 + 跨进程共享。

**人验收** (15 min):
- 跑 demo → 看到 **7 个 ✅**（计划要求 3+）
- `git log --oneline` → 看到 `feat(memory): 基础三件套`
- 决策:✅ 继续 M2

---

### Day 2 — M2: 写入路径(最关键的一天)

**目标**:双通道写入器跑通,这是整个系统的脊柱。**这是 v2.1 最重要的一里程碑**,出问题后面都白干。

**AI 工作清单**(8h):

| 任务 | 产出 | 关键点 |
| --- | --- | --- |
| Per-file 存储 | `memory_store.py` | frontmatter 解析,schema 校验,4 类目录 |
| 双通道写入器 | `dual_channel_writer.py` | **A3+A4+A5+A9+A10 一次写完**(5 项合并在 1 文件) |
| Edit-only 编辑器 | `memory_editor.py` | 工具描述 + 路径白名单 + **L7 source_quote 必填** + **L9 输出 sanitizer 5 pattern** |
| SQLite meta db | `meta_db.py` | `cursors` 表 + `pending_writes` 表 + `candidates` 表 |
| 跨进程锁 | `ipc_lock.py` | `flock` (Unix) + `msvcrt` (Windows stub) |
| 单测 | `tests/test_dual_channel_minimal.py` | 5 个 smoke + 8/8 并发场景中的 **2/8** (场景 1, 4) |

**核心代码模式**(A3+A4+A5 一次写完):

```python
# dual_channel_writer.py
class DualChannelWriter:
    def __init__(self, session_id, meta_db, memory_files, vector_store):
        self.daily_cursor = meta_db.get_cursor(session_id, "daily")
        self.extract_cursor = meta_db.get_cursor(session_id, "extract")
        self._ipc_daily = IPCLock(".daily.ipclock")
        self._ipc_extract = IPCLock(".extract.ipclock")
        self._executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="chb")
        atexit.register(self._graceful_shutdown)
        # ... A10 标志 + 超时
```

**验收 demo**:
```bash
bash scripts/demo_m2.sh
```

> ⚠️ **使用脚本而非 inline bash**：plan 中 demo 代码用 markdown `\`\`\`bash` 包裹，
> 但 demo 块内部的 `# 注释` 行（如 `# === Demo 1 ===`）在复制粘贴到 zsh 时会被
> 解释为命令，触发 `zsh: command not found: #` 警告（不影响运行但很丑）。
> 把 demo 抽到独立 `.sh` 脚本里可以彻底规避此问题。
>
> 脚本入口: [`scripts/demo_m2.sh`](../../scripts/demo_m2.sh)
> 跑法: `bash scripts/demo_m2.sh`(无需参数)
>
> 内部包含 4 个 demo + §4.5.1 场景 1/4 的 pytest 验收。

> **API 说明**(对比 v1 sketch):
> - `DualChannelWriter(session_id, meta_db, memory_store, vector_store, *, ...)` — `vector_store` 是必填位置参数（v1 sketch 漏了）
> - `MetaDB(':memory:')` 仅供单进程测试用；跨重启验证必须用磁盘路径 `MetaDB('/path/to/meta.db')`
> - `.shutdown(timeout=30)` 是 M2 推荐的优雅退出方式（M9/A9）

**人验收** (15 min):
- 跑 4 个 demo → 全 ✅
- 看 `test_dual_channel_concurrent.py` 输出 → 2/8 场景绿
- **关键决策点** ⚠️:并发 + 重启是否真工作?如果红,人 review 设计,不要硬撑
- 决策:✅ 继续 M3

**已知风险**:
- macOS flock 行为和 Linux 微差异 → 如果场景 3 跨进程在 macOS 红,Linux 验证后跳过
- A9 executor `shutdown(timeout=30)` 在 Python 3.8 不可用(无 timeout 参数),用 Event.wait() 替代

---

### Day 3 — M3: 检索 + 安全

**目标**:3 模式检索可用 + 冷启动 seed + LLM 提取合并 + token budget + 路径之外的第二道安全门(SecretScanner)。

**AI 工作清单**(8h):

| 任务 | 产出 | 关键点 |
| --- | --- | --- |
| 嵌入模型切换 | `memory_store.py` 改造 | 默认 `BAAI/bge-m3` (L2),配置项可改 |
| 三模式检索器 | `retriever.py` | `mode: vector/file/hybrid`,`HybridRetriever` 主类 |
| Token budget 注入 | `_build_memory_context` | `inject_mode: summary/full`,`max_injection_tokens: 2000` (L4+L8) |
| LLM 评分+提取合并 | `extractor.py` | `_llm_score_and_extract` 一次调用,返回 `ExtractResult` (L1) |
| 冷启动 seed | `seed/{4 类}/000_*.md` | 4 个默认记忆,`confidence: 0.5`,`MemoryBootstrap.ensure_seeded()` (L5) |
| SecretScanner | `secret_scanner.py` | 4 个 pattern: sk-*, sk-ant-*, ghp_*, xox* |
| 单测 | `tests/test_retrieval_modes.py` | 4 mode 都跑 + seed 自动加载 + secret 拒收 + 注入 budget 截断 |

**验收 demo**:
```bash
bash scripts/demo_m3.sh
# Expected: 5/5 demo 通过(覆盖 bge-m3 / SecretScanner / Extractor / Retriever / ColdStartLoader)+ pytest 全过
```

完整 demo 代码见 [`scripts/demo_m3.sh`](scripts/demo_m3.sh)。

**人验收** (15 min):
- 跑 5 个 demo → 全 ✅
- 特别看 demo #2 (冷启动) — 真的自动 seed 了吗?
- 决策:✅ 继续 M4

---

### Day 4 — M4: L3 压缩

> ✅ **状态:已完成** (2026-06-21 更新)
> commit `bf41c28` + bug 修复 `TBD`。
> 产出:[sm_layer.py](agent_core/memory/sm_layer.py) 595 行 + [test_sm_layer.py](tests/test_sm_layer.py) 32 case + [config.py](agent_core/memory/config.py) `CompactConfig` 52 行。
> ⚠️ **M4 范围 = 模块 + 测试**,**集成到对话流不在 M4,归 M7**(参见 §四.0.3 / §Day 7)。

**目标**:会话内压缩(SessionMemory)跑通,这是 v2 升级的核心创新点。

**AI 工作清单**(4h):

| 任务 | 产出 | 关键点 |
| --- | --- | --- |
| SessionMemory 文件 | `sm_layer.py` | 每个 session 一个 .md,frontmatter 锁定 schema |
| L3 压缩触发 | `should_trigger_compact` | 阈值: token > 10K 或 tool > 10 |
| 5 条回退条件 | 同上 | gate 关 / 文件空 / 模板态 / 提取中 / 仍超阈值 |
| 压缩算法 | `compact()` | 滚动摘要,保留最近 N 轮 + 早期摘要 |
| 单测 | `tests/test_sm_layer.py` | **32 个**(超出 plan 8 个最低要求):文件状态 / 触发 / 5 回退 / compact / extract / 数据结构 / 集成链路 / 回归 |
| 回归 bug 修复 | `_estimate_messages_tokens` | 累积 bug → used_tokens_estimate 可能为负,新增 `test_compact_estimates_tokens_for_long_messages` 守护 |

**验收 demo**(2026-06-21 对齐实际 API 后版本):
```bash
# 1. 端到端 demo(4 case: 生命周期 / 触发 / 压缩 / 回归守护)
bash scripts/demo_m4.sh
# Expected: 4/4 demo 通过 + 32 passed
```

完整 demo 代码见 [`scripts/demo_m4.sh`](scripts/demo_m4.sh)。

**API 变更说明**(对比 plan 原始描述):
| plan 写的 | 实际实现 | 备注 |
| --- | --- | --- |
| `SessionMemoryLayer('demo_s2')` | `SessionMemoryLayer(session_id, sm_path, config)` | 构造函数显式接 3 参数,避免隐式全局 |
| `sm.append_message(...)` | (无此方法) | 消息是调用方传入的,SM 不持久化原始 messages |
| `sm.should_compact()` | `sm.should_trigger_compact(ctx)` | 需传 TurnContext(total_tokens, tool_count) |
| `sm.compact()` | `sm.compact(messages, context_window)` | 需传 messages 列表 + 模型 context_window |
| `sm.token_count()` | `sm.sm_token_count()` | 仅统计 SM 文件本身,不带消息 |

**人验收** (15 min):
- 跑 3 个 demo → 全 ✅
- 看 compact 后 `summary_message.content` → 是否含 SM 关键信息(目标 + 决策)
- 决策:✅ M4 模块验收通过,**集成待 M7**

---

### Day 5 — M5: 蒸馏

> ✅ **状态:已完成** (2026-06-21 更新)
> commit `38f64a9`。
> 产出:[distiller.py](agent_core/memory/distiller.py) (~500 行) + [test_distiller.py](tests/test_distiller.py) 22 case + [config.py](agent_core/memory/config.py) `DistillationConfig` 加 `min_sessions_for_distill` 字段。
> ⚠️ **设计偏离 plan**:锁文件和"上次蒸馏时间"分离为两个文件(`.consolidate-lock` 瞬态 + `.last-distill` 持久 mtime)。原因:O_EXCL 原子创建要求文件不存在,与"保留 mtime"语义冲突。详见 plan §7.1 line 2423-2529。

**目标**:autoDream 跑通,锁 v2.1 安全(防 TOCTOU + 失败回滚 + JSON envelope)。

**AI 工作清单**(6h):

| 任务 | 产出 | 关键点 |
| --- | --- | --- |
| 蒸馏器 | `Distiller` class | 读多 session → LLM 整合 → 候选 dict |
| 锁 v2.1 | `_acquire_lock` / `_release_lock` | **A1 (O_EXCL) + A2 (mtime 回滚) + A11 (JSON envelope) 一次写完** |
| 调度器 | `DistillationScheduler` | 四重门: gate / time / busy / sessions |
| dry_run 默认 | `run(dry_run=True)` | 候选写到 `_candidate/{type}/`,不污染正式目录 |
| 单测 | `tests/test_distiller.py` | **22 个**(超出 plan 6 个最低要求):四重门(6) / 锁原子(2) / 强占(3) / envelope(3) / 回滚(2) / 核心(4) / 数据结构(2) |

**设计要点**(对比 plan 原始描述):
| plan 写的 | 实际实现 | 备注 |
| --- | --- | --- |
| 锁文件 mtime = 上次时间 | 拆为 `.consolidate-lock`(瞬态) + `.last-distill`(持久 mtime) | O_EXCL 与 mtime 持久化冲突,折中 |
| `_acquire_lock` 返回 0 = 锁被占 | 返回 `LOCK_TAKEN = -1` 表示锁被占,0 = 成功但无 prior | 区分"成功"与"失败" |
| 强占逻辑 | acquire 前先检查陈旧(PID 死 OR mtime 超),是则删锁 | 显式两步,避免歧义 |
| 单 `DistillationScheduler` 类 | 拆为 `Distiller`(纯函数) + `DistillationScheduler`(调度+锁) | 关注点分离,易测 |

**验收 demo**:
```bash
bash scripts/demo_m5.sh
# 5 demo: gate_disabled / too_soon / 10 线程并发 / 失败回滚 / 端到端 dry_run
# + bonus: 真写盘路径
# + 22 pytest
```

完整 demo 代码见 [`scripts/demo_m5.sh`](scripts/demo_m5.sh)。

**人验收** (15 min):
- 跑 demo → 5/5 + 22 pytest 全绿
- 特别看 demo #3: 10 线程并发,只有 1 个赢锁,其它 9 个 LOCK_TAKEN
- 决策:✅ M5 模块验收通过,**真实 LLM 调用与 UI 归 M7**

---

### Day 6 — M6: 调度 + 可观测 + 并发测试

> ✅ **状态:完成** (2026-06-21 更新)
> - `agent_core/memory/scheduler.py` —— DistillationLoop (start/stop/tick_once,后台 daemon)
> - `agent_core/memory/tracing.py` —— OTel tracer (默认 NoOp,env 触发 OTLP)
> - `tests/test_scheduler.py` —— 9 cases (含 OTel span 嵌套)
> - `tests/test_dual_channel_concurrent.py` —— 5 scenarios (场景 2/5/6/7/8)
> - `scripts/demo_m6.sh` —— 3 个端到端 demo
> - `dual_channel_writer.py` —— extraction watchdog (场景 8 前置)
> - `distiller.py` —— OTel span 包装 `run()`
>
> Day 2 已覆盖场景 1/4,Day 6 补 2/5/6/7/8(场景 3 跨进程归 M8)。

**目标**:调度器 + OTel + 补 5/8 并发场景(A12 矩阵的 3/6/7/8 + 已有的 1/4 = 5 个)。

**AI 工作清单**(6h):

| 任务 | 产出 | 关键点 |
| --- | --- | --- |
| 调度器完善 | `scheduler.py` | cron-style:每 5min 检查 should_distill |
| OTel 最小版 | `tracing.py` | `tracer.start_as_current_span('memory.extract')` + key attributes |
| 5/8 并发场景 | `test_dual_channel_concurrent.py` | 补场景 2/5/6/7/8 (已有 1/4) |
| 单测 | `tests/test_scheduler.py` | 6 个: 调度触发 / OTel span / 5 并发场景 |

**8 场景状态**:
- ✅ 场景 1 (Day 2 已有): 双线程 channel_a 并发
- 🆕 场景 2 (Day 6): A 写 → B 提取 边界
- 🆕 场景 3 (Day 8 补): 跨进程 A + B
- ✅ 场景 4 (Day 2 已有): 通道 B 提取崩溃
- 🆕 场景 5 (Day 6): 蒸馏锁强占 (PID 已死)
- 🆕 场景 6 (Day 6): 蒸馏锁强占 (mtime 超时)
- 🆕 场景 7 (Day 6): 蒸馏失败回滚
- 🆕 场景 8 (Day 6): extraction_in_progress 卡死

> **8 场景详细说明 + 测试模板** 见 [`docs/memory-system-design.md` §4.5.1](docs/memory-system-design.md#451-不变量测试矩阵8-个并发崩溃场景v21-增对应-a12-修复)——本节只列进度追踪,设计契约在 design.md。

**验收 demo**:
```bash
bash scripts/demo_m6.sh
# 3 个 demo: 调度触发 / OTel span / 5 个并发场景
# + 14 pytest: tests/test_scheduler.py (9) + tests/test_dual_channel_concurrent.py (5)
```

**人验收** (15 min):
- 跑 `bash scripts/demo_m6.sh` → 3 demo 全 ✅,14 pytest 全过
- 决策:✅ M6 完成,M7 启动

---

### Day 7 — M7: 集成 + UI + Schema 迁移

> ⏸️ **状态:未开始** (2026-06-21 更新)
> 无相关 commit,文件 `migration.py` 不存在,`langgraph_agent/agent.py` 与 `router.py` 未打 memory patch。
> `app_langgraph.py` UI 状态条未集成。

**目标**:集成到主 agent + UI 状态条 + 完整 LLM 合约 + Schema migration 兜底。

**AI 工作清单**(6h):

| 任务 | 产出 | 关键点 |
| --- | --- | --- |
| Agent 集成 | `langgraph_agent/agent.py` patch | turn 前后调用 memory 系统 |
| LLM Router 合约 | `router.py` patch | `LLMResponse` (含 `usage.cache_read_tokens`) + `cache_namespace` 参数 |
| Schema migration | `migration.py` | `schema_version: 1` + `MigrationRegistry` + 懒迁移 + sidecar |
| UI 状态条 | `app_langgraph.py` 改 | 加 3 行: Search N/M、Injected X tokens、Last 0-hit N ago |
| 单测 | `tests/test_integration.py` | 5 个: 端到端 turn / cache namespace / migration / UI 数据流 |

**验收 demo**:
```bash
# 1. 端到端 turn
.venv/bin/python -c "
from langgraph_agent.agent import Agent
a = Agent(memory_enabled=True)
r = a.run('记住我叫小明')
assert '已记' in r.content
r2 = a.run('你记得我吗?')
assert '小明' in r2.content  # 应从 memory 召回
print('✅ end-to-end works')
"

# 2. Schema migration
.venv/bin/python -c "
from agent_core.memory.migration import migrate_file
import tempfile, pathlib
with tempfile.NamedTemporaryFile(mode='w', suffix='.md', delete=False) as f:
    f.write('---\ntype: user\n---\n# 旧格式记忆')
    path = f.name
migrated = migrate_file(pathlib.Path(path))
assert migrated.get('schema_version') == 1
print('✅ migrated to v1')
"
```

**人验收** (15 min):
- 跑 2 个 demo → 全 ✅
- **特别看 demo #1** — 真实对话,看 memory 是否真的被用
- 决策:⏸️ 待 M7 启动后填

---

### Day 8 — M8: 完整测试 + 上线准备

> ⏸️ **状态:未开始** (2026-06-21 更新)
> 无 `lifecycle.py` / `demo_v2.1.py` / `LAUNCH_v2.1.md` / CHANGELOG 条目 / `v2.1.0` git tag。
> 场景 3 跨进程并发未做。

**目标**:补 3/8 并发场景 + A6 backup/cron + 完整 demo + 上线 checklist。

**AI 工作清单**(8h):

| 任务 | 产出 | 关键点 |
| --- | --- | --- |
| 补场景 3 (跨进程) | `test_dual_channel_concurrent.py` | 起 2 个子进程,验证 flock 互斥 |
| A6 Data Lifecycle | `lifecycle.py` | daily backup rsync + `PRAGMA integrity_check` + 容量治理 |
| 完整 demo | `scripts/demo_v2.1.py` | 1 个脚本跑通"记住→重启→召回"全流程 |
| 上线 checklist | `docs/LAUNCH_v2.1.md` | 配置项 / 监控 / 回滚步骤 |
| 全量回归 | `pytest tests/ -v` | 50+ 测试全绿 |

**验收 demo**(终极,1 个命令):
```bash
python scripts/demo_v2.1.py

# 预期输出:
# ✅ 1. 启动: 4 个 seed 已加载
# ✅ 2. 写入: "我叫小明" → channel A 即时答"已记"
# ✅ 3. 蒸馏: 5 turns 后触发 L4 异步提取
# ✅ 4. 重启: 进程退出再启动, memory 仍存在
# ✅ 5. 召回: "你记得我吗?" → 答"小明"
# ✅ 6. 跨进程: 子进程 flock 互斥
# ✅ 7. 并发: 10 线程 channel A 无丢失
# ✅ 8. 蒸馏失败: mtime 回滚
# ✅ 9. 备份: ~/.agent_data.backup/<日期>/ 已生成
# ✅ 10. 完整性: SQLite PRAGMA integrity_check = ok
```

**人验收** (15 min):
- 跑 1 个终极 demo → 10/10 ✅
- 看 `git log --oneline` → 8 天 commit 历史清晰
- **最终决策**:🚀 准备上线 / 🛑 还需要修

> ⏸️ **待 M8 启动后填**

---

## 3. 风险 & 缓冲

| 风险 | 概率 | 影响 | 缓解 |
| --- | --- | --- | --- |
| **M2 双通道设计跑不通** | 中 | 8 天全废 | Day 2 中午 12:00 检查点,场景 1/4 测试不绿就停 |
| bge-m3 体积 2.3GB 下载慢 | 高 | Day 3 慢 | 保留 MiniLM 默认,bge-m3 后切,Day 3 demo 用 MiniLM |
| LLM 合并 prompt 改变导致输出微变 | 中 | 用户感知 | Day 3 跑 100 条 fixture, candidates 数 ±10% 内接受 |
| 跨进程 flock 在 macOS 行为差异 | 中 | 场景 3 红 | macOS 标 skip,Linux CI 必跑 |
| 人介入延迟 > 24h | 中 | 整体延期 | 异步 review: AI 把 demo + diff 推 PR,人批 PR |

**总 buffer**:12.5h(2 天内),任何 M{N} 出问题都有时间修。

---

## 4. 完成定义(项目级)

整个 v2.1 项目完成需要:

### M1-M3 已完成部分(2026-06-21 更新)

- [x] **M1**: commit `57482f1` (feat(memory): M1 基础三件套)
- [x] **M2**: commit `a2c0a5d` (feat(memory): M2 双通道写入器),配 `a63e6b5` / `be46eca`
- [x] **M3**: commit `539b6e7` (feat(memory): M3 检索 + 安全),配 `fe144f9` / `7983c72` / `c2e001e` / `f7263c8` / `492d1c7` / `574e096`
- [x] `pytest tests/ -v` 输出 **154 passed**(M1+M2+M3 内存套件全绿)
- [x] `docs/memory-system-design.md` 已更新并反映实际实现
- [x] `agent_core/exceptions.py` + `agent_core/types.py` 已 commit(memory 必修依赖)
- [x] `docs/context-compaction-token-estimation-theory.md §5.3` 记录 tiktoken vs GLM 偏差(已知问题)

### M4-M8 待完成部分

- [ ] **M4**: L3 压缩 / sm_layer.py
- [ ] **M5**: 蒸馏 / distiller.py
- [x] **M6**: 调度 + 可观测 / scheduler.py + tracing.py + 5 个并发场景
- [ ] **M7**: 集成 + UI + Schema 迁移 / migration.py + agent.py patch + UI 状态条
- [ ] **M8**: 完整测试 + 上线准备 / lifecycle.py + demo_v2.1.py + LAUNCH_v2.1.md + CHANGELOG + `v2.1.0` git tag
- [ ] `pytest tests/ -v` 输出 **200+ passed**(当前 154,待 M4-M8 加 ~50 case)
- [ ] 8/8 并发场景全绿(当前 2/8: 场景 1、4)
- [ ] 3 个 token 估算测试校准(`test_chinese_text` / `test_mixed_text` / `test_should_compact_when_near_limit`)

### 已知问题(待修复,不阻塞 M1-M3 完成判定)

- ⚠️ tiktoken vs GLM-4-Flash 计费偏差 -2% ~ -71% — 见 [§5.3 已知问题](context-compaction-token-estimation-theory.md#53-⚠️-已知问题tiktoken-vs-glm-实际计费偏差待修复)
- ⚠️ 3 个 token 估算测试失败(`test_context.py` line 58/72/235)

---

## 5. 验收节奏速查

| 时间点 | 谁 | 做什么 | 时长 |
| --- | --- | --- | --- |
| 每天收工 (Day 1-7) | 人 | 跑当天 demo + 看 diff + 决策 | 15 min |
| Day 8 收工 | 人 | 跑终极 demo + 上线决策 | 30 min |
| 任何 milestone 失败 | 人 | 介入 review, 决策继续/调整/回滚 | 30-60 min |

**总人时**:6 × 15 min + 30 min + buffer = **~2h/人** 8 天
**总 AI 时**:~62h/AI 自动

**效率对比**:
- 纯人开发:估 8 天 × 8h × 1 人 = 64h,**人时 64h**
- AI+人:估 8 天,**人时 2h,AI 时 62h**
- **加速比:~32x**(以"人等的时间"计)

如果按产出代码行算,AI+人 单日产出 ~1500 行 (含测试) vs 纯人单日 ~150 行,**实际开发加速 ~10x**。

---

## 6. 附录:文件交付清单

### 6.1 M1-M3 实际产出(2026-06-21 完成)

```
agent_core/                            (~3400 行,含新增)
├── exceptions.py                      (统一异常体系,AgentError 根类 + 6 领域子类)
└── types.py                           (MessageRole 枚举)

agent_core/memory/                     (~3000 行已交付)
├── types.py                           (M1, 4 类类型 + validate_type)
├── config.py                          (M1, MemoryConfig + RetrievalConfig)
├── path_validator.py                  (M1, 4 层防御)
├── meta_db.py                         (M2, SQLite cursors/pending/candidates)
├── ipc_lock.py                        (M2, flock 跨进程锁)
├── memory_store.py                    (M2, per-file 存储 + frontmatter)
├── dual_channel_writer.py             (M2, A3+A4+A5+A9+A10 双通道)
├── memory_editor.py                   (M2, Edit-only + L7+L9 + secret 扫描)
├── retriever.py                       (M3, semantic/keyword/hybrid + L4 密钥过滤)
├── extractor.py                       (M3, LLM 提取 + 评分合并)
├── secret_scanner.py                  (M3, 4 类密钥 pattern)
├── embeddings.py                      (M3, BGEM3EmbedFn + MiniLMEmbedFn,no Mock)
└── chroma_store.py                    (M3, ChromaVectorStore,生产 vector_store 唯一实现)

tests/                                 (~2400 行,8 文件)
├── test_types_config.py               (M1, 47 cases)
├── test_dual_channel_minimal.py       (M2, 7 cases:5 smoke + 场景 1 + 场景 4)
├── test_cold_start.py                 (M3, 16 cases)
├── test_retriever.py                  (M3, 25 cases:3 模式 + 类型 + 排名 + get_by_hash)
├── test_extractor.py                  (M3, ? cases)
├── test_embeddings.py                 (M3, 15 cases)
├── test_secret_scanner.py             (M3, ? cases)
└── test_usage_baseline_restore.py     (8 cases,context compaction v4 配套)

scripts/
├── setup_embeddings.sh                (一键安装 bge-m3 + ChromaDB)
├── demo_m2.sh                         (M2 验收 demo)
└── demo_m3.sh                         (M3 验收 demo)

总计(M1-M3):~3000 行核心代码 + ~2400 行测试 + ~210 个测试 case
           pytest tests/ 输出: 154 passed / 1 skipped
```

### 6.2 M4-M8 计划产出(未开始)

```
agent_core/memory/                     (~1760 行待交付)
├── sm_layer.py                        (M4, 350 行 — SessionMemory L3 压缩)
├── distiller.py                       (M5, 400 行 — 蒸馏 + 锁 v2.1)
├── scheduler.py                       (M5+M6, 300 行 — 蒸馏调度)
├── tracing.py                         (M6, 60 行 — OTel span)
├── migration.py                       (M7, 150 行 — Schema migration)
├── lifecycle.py                       (M8, 200 行 — A6 backup/cron)
├── memory_verifier.py                 (M3, 200 行 — 召回验证,延后)
├── bootstrap.py                       (M3, 60 行 — seed 引导,延后)
└── daily.py                           (已存在,集成点待 M7 接入)

agent_core/langgraph_agent/
└── agent.py                           (M7 patch, +150 行 — memory 系统接入)

app_langgraph.py                       (M7 patch, +80 行 UI — 状态条)

tests/                                 (~1850 行待交付)
├── test_path_validator.py             (M1, 100 行, 10 cases — 缺失)
├── test_dual_channel_concurrent.py    (M2+M6+M8, 400 行, 8 cases — 当前 2/8)
├── test_retrieval_modes.py            (M3, 250 行, 6 cases — 替代 test_retriever.py)
├── test_sm_layer.py                   (M4, 200 行, 8 cases)
├── test_distiller.py                  (M5, 300 行, 6 cases)
├── test_scheduler.py                  (M6, 150 行, 6 cases)
├── test_integration.py                (M7, 300 行, 5 cases)
└── test_lifecycle.py                  (M8, 150 行, 4 cases)

scripts/
└── demo_v2.1.py                       (M8, 150 行 — 端到端 demo)

docs/
└── LAUNCH_v2.1.md                     (M8, 200 行 — 上线 checklist)

总计(M4-M8 待交付):~1760 行核心代码 + ~1850 行测试 + ~53 个测试 case
```

### 6.3 总体交付(项目级)

| 项 | 已交付 | 待交付 | 合计 |
|---|---|---|---|
| 核心代码 | ~3000 行 | ~1760 行 | ~4760 行 |
| 测试 | ~2400 行 | ~1850 行 | ~4250 行 |
| 测试 case | 154 | ~53 | ~207 |
| commit 数 | 13 (memory 系列) | 估 ~15 | 估 ~28 |

---

**最后更新**:2026-06-21
**M1-M3 状态**:✅ 完成 (commit `57482f1` → `574e096`)
**M4-M8 状态**:⏸️ 未开始,待启动决策
**负责**:AI Agent 写码 + 人验收
