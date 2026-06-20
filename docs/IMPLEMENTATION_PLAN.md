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

| # | 名称 | 时长 | 涉及修复 | 验收命令 |
| --- | --- | --- | --- | --- |
| **M1** | 基础 + 配置 (Day 1) | 4h | O8 | `pytest tests/test_types_config.py -v` |
| **M2** | 写入路径 (Day 2) | 8h | A3, A4, A5, A9, A10, L7, L9 | `pytest tests/test_dual_channel_minimal.py -v` |
| **M3** | 检索 + 安全 (Day 3) | 8h | L1, L2, L4, L5, L8, L12 | `pytest tests/test_retrieval_modes.py -v` |
| **M4** | L3 压缩 (Day 4) | 4h | — | `pytest tests/test_sm_layer.py -v` |
| **M5** | 蒸馏 (Day 5) | 6h | A1, A2, A11 | `pytest tests/test_distiller.py -v` |
| **M6** | 调度 + 可观测 (Day 6) | 6h | A8, A12 (含 5/8 并发场景) | `pytest tests/test_scheduler.py -v` |
| **M7** | 集成 + UI (Day 7) | 6h | L13, A7 (schema migration) | `pytest tests/test_integration.py -v` |
| **M8** | 完整测试 + 上线 (Day 8) | 8h | A6, A12 (补 3/8 场景) | `pytest tests/ -v` + demo 全跑 |

**总人力**:50h AI 写码 + 1.5h 人验收 + 12.5h buffer(AI 修 bug / 等 LLM 响应 / 跑测试) = 64h

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
.venv/bin/python -c "
from agent_core.memory.types import validate_type
from agent_core.memory.config import MemoryConfig
from agent_core.memory.path_validator import MemoryPathValidator
import tempfile, pathlib

# 1. 类型校验
assert validate_type('user') == 'user'
try: validate_type('episodic')  # LLM 试图发明的第 5 类
except ValueError as e: print(f'✅ blocked: {e}')

# 2. 配置校验 (Pydantic)
try: MemoryConfig(retrieval={'semantic_weight': 2.0})  # 越界
except Exception as e: print(f'✅ blocked: {e}')

# 3. 路径校验 (使用临时沙箱, 避免污染 ~/)
tmp = pathlib.Path(tempfile.mkdtemp()) / 'memory'
tmp.mkdir()
v = MemoryPathValidator(tmp)
real = v.validate('user/foo.md')
print(f'✅ resolved: {real}')

try: v.validate('../../etc/passwd')  # 越界
except Exception as e: print(f'✅ blocked: {e}')

try: v.validate('admin/foo.md')  # 非法子目录
except Exception as e: print(f'✅ blocked: {e}')

try: v.validate('user/run.py')  # 非法扩展名
except Exception as e: print(f'✅ blocked: {e}')

try: v.validate('user/‮evil.md')  # Unicode trick (U+202E RLO)
except Exception as e: print(f'✅ blocked: {e}')
"
```

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
# 1. 基础写入
python -c "
from agent_core.memory import *
w = DualChannelWriter('demo_s1', MetaDB(':memory:'), MemoryStore('/tmp/demo/'))
w.channel_a_inline_write('记住我叫小明', '已记', 0)
assert w.daily_cursor == 0
print('✅ channel A wrote')
"

# 2. 重启恢复 (A3)
python -c "
from agent_core.memory import *
db = MetaDB('/tmp/demo_meta.db')
w1 = DualChannelWriter('demo_s1', db, ...)
w1.channel_a_inline_write('msg1', 'resp1', 1)
del w1
w2 = DualChannelWriter('demo_s1', db, ...)  # 重新加载
assert w2.daily_cursor == 1
print('✅ cursor persisted across restart')
"

# 3. 并发 (场景 1)
pytest tests/test_dual_channel_concurrent.py::test_concurrent_writes -v
# Expected: PASSED

# 4. 崩溃恢复 (场景 4)
pytest tests/test_dual_channel_concurrent.py::test_crash_resume_continues -v
# Expected: PASSED
```

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
# 1. 三模式切换
python -c "
import agent_core.memory as m
r = m.Retriever(mode='hybrid')
results = r.search('学习风格', top_k=3)
print(f'hybrid returned {len(results)} memories')
r2 = m.Retriever(mode='file')
print(f'file-only returned {len(r2.search(\"学习风格\", top_k=3))}')
"

# 2. 冷启动
rm -rf ~/.agent_data/memory/  # 清空
python -c "
from agent_core.memory import bootstrap
bootstrap.ensure_seeded()
import os
assert os.path.exists(os.path.expanduser('~/.agent_data/memory/user/000_default_learning_style.md'))
print('✅ seed loaded')
"

# 3. SecretScanner
python -c "
from agent_core.memory.secret_scanner import SecretScanner
s = SecretScanner()
findings = s.scan('我的 API key 是 sk-1234567890abcdefghij')
assert 'sk-1234567890abcdefghij' in findings
print('✅ secret detected')
"

# 4. Token budget
python -c "
from agent_core.memory.retriever import Retriever
r = Retriever(mode='file', max_injection_tokens=2000)
ctx = r.build_context(['x' * 8000] * 5)  # 40KB 输入
assert len(ctx) < 2000 * 4  # 8000 字符 ~2000 tokens
print(f'✅ truncated to {len(ctx)} chars')
"

# 5. LLM 合并调用
pytest tests/test_extractor.py::test_merged_call -v
# Expected: 1 次 LLM 调用而非 2 次
```

**人验收** (15 min):
- 跑 5 个 demo → 全 ✅
- 特别看 demo #2 (冷启动) — 真的自动 seed 了吗?
- 决策:✅ 继续 M4

---

### Day 4 — M4: L3 压缩

**目标**:会话内压缩(SessionMemory)跑通,这是 v2 升级的核心创新点。

**AI 工作清单**(4h):

| 任务 | 产出 | 关键点 |
| --- | --- | --- |
| SessionMemory 文件 | `sm_layer.py` | 每个 session 一个 .md,frontmatter 锁定 schema |
| L3 压缩触发 | `should_trigger_compact` | 阈值: token > 10K 或 tool > 10 |
| 5 条回退条件 | 同上 | gate 关 / 文件空 / 模板态 / 提取中 / 仍超阈值 |
| 压缩算法 | `_compact_to_sm` | 滚动摘要,保留最近 N 轮 + 早期摘要 |
| 单测 | `tests/test_sm_layer.py` | 8 个: 触发 / 5 回退 / 摘要正确性 / 不丢消息 |

**验收 demo**:
```bash
# 1. 触发压缩
python -c "
from agent_core.memory.sm_layer import SessionMemoryLayer
sm = SessionMemoryLayer('demo_s2')
# 模拟 10K tokens 的对话
for i in range(100):
    sm.append_message(f'user msg {i}', f'resp {i}')
assert sm.should_compact()
sm.compact()
assert sm.token_count() < 10000
print('✅ compacted')
"

# 2. 5 条回退
pytest tests/test_sm_layer.py -v -k fallback
# Expected: 5 passed
```

**人验收** (15 min):
- 跑 2 个 demo → 全 ✅
- 看压缩后文件 → 内容是否合理(关键决策点)
- 决策:✅ 继续 M5

---

### Day 5 — M5: 蒸馏

**目标**:autoDream 跑通,锁 v2.1 安全(防 TOCTOU + 失败回滚 + JSON envelope)。

**AI 工作清单**(6h):

| 任务 | 产出 | 关键点 |
| --- | --- | --- |
| 蒸馏器 | `distiller.py` | 读多 session → LLM 整合 → 候选文件 |
| 锁 v2.1 | `_acquire_lock` / `_release_lock` | **A1 (O_EXCL) + A2 (mtime 回滚) + A11 (JSON envelope) 一次写完** |
| 调度器 | `scheduler.py` | 四重门: gate / 时间 / 节流 / session 数 |
| diff/merge review | `dry_run=True` 生成候选 | 用户用 git diff 工具看 |
| 单测 | `tests/test_distiller.py` | 6 个: 触发 / 锁竞争 / 失败回滚 / 锁强占 / envelope 校验 |

**核心代码模式**(A1+A2+A11 一次写完):

```python
# distiller.py
class DistillationScheduler:
    LOCK_FILE = Path(".agent_data/memory/.consolidate-lock")
    
    def _acquire_lock(self) -> int:
        """A1: 原子创建 / A11: JSON envelope"""
        prior_mtime = self.LOCK_FILE.stat().st_mtime if self.LOCK_FILE.exists() else 0
        envelope = {"pid": os.getpid(), "host": socket.gethostname(),
                    "started_at": time.time(), "schema_version": 1}
        try:
            fd = os.open(self.LOCK_FILE, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.write(fd, json.dumps(envelope).encode())
        except FileExistsError:
            return 0  # A1: 别人持锁
        return int(prior_mtime * 1000)
    
    def _release_lock(self, prior_mtime_ms: int) -> None:
        """A2: 失败回滚 mtime"""
        if self._last_failed and prior_mtime_ms > 0:
            self._write_last_distill_at(prior_mtime_ms / 1000)
        self.LOCK_FILE.unlink(missing_ok=True)
```

**验收 demo**:
```bash
# 1. 锁原子创建 (场景 1: 并发锁)
pytest tests/test_distiller.py::test_lock_atomic -v
# 起 10 个进程并发 acquire, 只有 1 个赢

# 2. 失败回滚 (场景 7)
pytest tests/test_distiller.py::test_failure_rolls_back_mtime -v

# 3. JSON envelope 校验
python -c "
from agent_core.memory.distiller import DistillationScheduler
s = DistillationScheduler()
# 写一个 fake 锁
import json
s.LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
s.LOCK_FILE.write_text(json.dumps({'pid': 99999, 'host': 'fake', 'started_at': 0, 'schema_version': 1}))
# 读 envelope
env = s._read_lock_envelope()
assert env.pid == 99999
print('✅ envelope parsed')
# 写垃圾应该返回 empty
s.LOCK_FILE.write_text('garbage')
env = s._read_lock_envelope()
assert env.pid == 0  # 强占允许
print('✅ garbage rejected')
"
```

**人验收** (15 min):
- 跑 3 个 demo → 全 ✅
- 特别看 demo #1:10 进程并发,**只有 1 个**赢锁,其它全 0
- 决策:✅ 继续 M6

---

### Day 6 — M6: 调度 + 可观测 + 并发测试

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
# 1. 调度器触发
python -c "
from agent_core.memory.scheduler import DistillationScheduler
s = DistillationScheduler()
# 模拟 5 个 session
for i in range(5): s.record_session(f's{i}')
# 模拟 24h 前
s._last_distill_at = time.time() - 25*3600
assert s.should_distill()
print('✅ should_distill returned True')
"

# 2. OTel span
python -c "
from agent_core.memory.tracing import tracer
with tracer.start_as_current_span('memory.extract') as span:
    span.set_attribute('memory.candidates', 3)
    print('✅ span created')
"
# 检查输出有 'memory.extract' span

# 3. 5 个新并发场景
pytest tests/test_dual_channel_concurrent.py -v
# Expected: 7 passed (1, 2, 4, 5, 6, 7, 8) - 场景 3 仍 skip (跨进程)
```

**人验收** (15 min):
- 跑 3 个 demo → 全 ✅
- 看 5/8 并发场景全绿
- 决策:✅ 继续 M7

---

### Day 7 — M7: 集成 + UI + Schema 迁移

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
python -c "
from langgraph_agent.agent import Agent
a = Agent(memory_enabled=True)
r = a.run('记住我叫小明')
assert '已记' in r.content
r2 = a.run('你记得我吗?')
assert '小明' in r2.content  # 应从 memory 召回
print('✅ end-to-end works')
"

# 2. Schema migration
python -c "
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
- 决策:✅ 继续 M8

---

### Day 8 — M8: 完整测试 + 上线准备

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

- [x] M1-M8 全部绿
- [x] `pytest tests/ -v` 输出 50+ passed
- [x] `python scripts/demo_v2.1.py` 输出 10/10 ✅
- [x] `docs/memory-system-design.md` 反映实际实现(如有差异)
- [x] `docs/LAUNCH_v2.1.md` 上线 checklist 完整
- [x] CHANGELOG 写 v2.1.0 条目
- [x] git tag `v2.1.0`

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

```
agent_core/memory/                    (新增 ~3500 行)
├── types.py                          (M1, 80 行)
├── config.py                         (M1, 150 行)
├── path_validator.py                 (M1, 100 行)
├── meta_db.py                        (M2, 200 行)
├── ipc_lock.py                       (M2, 80 行)
├── memory_store.py                   (M2, 400 行)
├── dual_channel_writer.py            (M2, 500 行)
├── memory_editor.py                  (M2, 250 行)
├── retriever.py                      (M3, 600 行)
├── extractor.py                      (M3, 300 行)
├── memory_verifier.py                (M3, 200 行)
├── secret_scanner.py                 (M3, 80 行)
├── bootstrap.py                      (M3, 60 行)
├── seed/                             (M3, 4 个 .md)
├── sm_layer.py                       (M4, 350 行)
├── distiller.py                      (M5, 400 行)
├── scheduler.py                      (M5+M6, 300 行)
├── tracing.py                        (M6, 60 行)
├── migration.py                      (M7, 150 行)
└── lifecycle.py                      (M8, 200 行)

agent_core/langgraph_agent/
└── agent.py                          (M7 patch, +150 行)

app_langgraph.py                      (M7 patch, +80 行 UI)

tests/                                (新增 ~2500 行)
├── test_types_config.py              (M1, 200 行, 20 cases)
├── test_path_validator.py            (M1, 100 行, 10 cases)
├── test_dual_channel_minimal.py      (M2, 200 行, 5 cases)
├── test_dual_channel_concurrent.py   (M2+M6+M8, 400 行, 8 cases)
├── test_retrieval_modes.py           (M3, 250 行, 6 cases)
├── test_extractor.py                 (M3, 200 行, 5 cases)
├── test_sm_layer.py                  (M4, 200 行, 8 cases)
├── test_distiller.py                 (M5, 300 行, 6 cases)
├── test_scheduler.py                 (M6, 150 行, 6 cases)
├── test_integration.py               (M7, 300 行, 5 cases)
└── test_lifecycle.py                 (M8, 150 行, 4 cases)

scripts/
└── demo_v2.1.py                      (M8, 150 行)

docs/
├── memory-system-design.md           (已有, 3764 行)
├── IMPLEMENTATION_PLAN.md            (本文件)
└── LAUNCH_v2.1.md                    (M8, 200 行)

总计: 8 天, ~6500 行新代码, ~3500 行测试, 79 个测试 case
```

---

**最后更新**:2026-06-20
**状态**:待执行
**负责**:AI Agent 写码 + 人验收
