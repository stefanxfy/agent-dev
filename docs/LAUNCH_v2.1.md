# Memory System v2.1 上线手册

> **状态**:M1–M8 全部完成(2026-06-21)
> **目标读者**:运维 / SRE / 想接记忆系统的开发者
> **覆盖范围**:配置 / 监控 / 回滚 / 跨进程安全 / 数据迁移

---

## 0. 30 秒总览

| 维度 | 现状 |
|------|------|
| 总模块数 | 12 个(agent_core/memory/*.py) |
| 公开 API | ~50 个(见 `agent_core/memory/__init__.py:__all__`) |
| 测试覆盖 | M1:types/config/path · M2:meta_db/lock/store · M3:embed/chroma/scanner/coldstart/retriever/extractor · M4:sm_layer · M5:distiller · M6:scheduler/loop · M7:migration · M8:lifecycle |
| 已知并发场景 | 8/8(场景 3 跨进程归 M8,见 test_dual_channel_concurrent.py) |
| 端到端 demo | `scripts/demo_v2.1.py`(9 步) |
| Schema 当前版本 | **2**(`CURRENT_SCHEMA_VERSION = types.py:124`) |

---

## 1. 目录与文件结构

```
~/.agent_data/
├── memory/                          # 记忆主体(per-file markdown)
│   ├── user/      *.md              # 用户偏好/事实
│   ├── project/   *.md              # 项目背景
│   ├── feedback/  *.md              # 用户反馈
│   ├── tool/      *.md              # 工具使用经验
│   ├── *.md.bak                    # 迁移 sidecar(可清理,见 §5.3)
│   └── .consolidate-lock           # 蒸馏锁(临时,运行时存在)
├── meta.db                          # SQLite:cursors + pending
├── chroma/                          # 向量索引(chromadb)
│   └── .../
├── logs/                            # 会话日志(JSONL)
│   └── <session_id>.jsonl
└── memory.backup/                   # 备份根(由 daily_backup 创建)
    └── YYYY-MM-DD/                  # 一天一目录
        ├── memory/
        ├── meta.db
        └── chroma/
```

> **重要**:迁移 sidecar(`.bak`)和运行锁(`.consolidate-lock`)是隐藏文件,运维工具看不见。`rglob("*.md")` 会自动跳过 `.bak`。

---

## 2. 关键配置(全部有默认值,生产可不动)

### 2.1 `DistillationConfig`(蒸馏)

```python
from agent_core.memory.config import DistillationConfig

DistillationConfig(
    # ─── 触发频率 ───
    min_hours_between_runs=24,        # 两次蒸馏最小间隔(小时)
    stale_session_hours=24,           # 多少小时前的 session 才参与
    min_session_files=3,              # 至少 N 个 session 才启动

    # ─── 锁参数 ───
    lock_stale_pid_seconds=3600,      # PID 死亡 N 秒后可强占
    lock_stale_mtime_seconds=3600,    # 锁文件 mtime 超过 N 秒可强占

    # ─── 蒸馏上限 ───
    max_candidates_per_run=50,        # 单次蒸馏候选数
    max_prompt_tokens=8000,           # LLM prompt 预算

    # ─── LLM 提取 ───
    extract_max_attempts=3,           # LLM 失败重试次数
    extract_timeout_seconds=120,      # 单次 LLM 调用超时
)
```

### 2.2 `MemoryConfig`(记忆)

```python
from agent_core.memory.config import MemoryConfig

MemoryConfig(
    # 检索
    retriever_top_k=5,                # search() 默认 top_k
    retriever_min_score=0.3,          # 召回最低相似度(0-1)

    # 注入
    injection_token_budget=2000,      # 注入到 LLM 的记忆 token 上限
    injection_dedup_threshold=0.85,   # 注入去重的相似度阈值

    # 向量
    chroma_collection="agent_memory", # chromadb collection 名
)
```

### 2.3 推荐生产配置

| 场景 | min_hours_between_runs | max_candidates_per_run | retriever_top_k |
|------|------------------------|------------------------|-----------------|
| 个人开发 | 12 | 30 | 5 |
| 小团队(2-10 人) | 24 | 50 | 8 |
| 中等规模(10-100) | 24 | 100 | 10 |
| 高频自动化 | 6 | 200 | 15 |

---

## 3. 监控指标

### 3.1 必看(每次发布前验证)

| 指标 | 阈值 | 检测方式 |
|------|------|----------|
| **distillation success rate** | ≥ 95% | `distiller.py:DistillationResult.success` |
| **integrity_check.is_healthy** | True | `lifecycle.integrity_check()` |
| **frontmatter_invalid** | 0 | 同上 |
| **sqlite_ok** | True | 同上 |
| **迁移 .bak 残留数** | 0(M7 完成为前提) | `find ~/.agent_data -name "*.bak"` |

### 3.2 健康检查(每日 cron)

```python
from agent_core.memory import integrity_check, capacity_govern, list_backups
from pathlib import Path

root = Path("~/.agent_data/memory").expanduser()
meta_db = Path("~/.agent_data/meta.db")

# 1. 完整性
ir = integrity_check(root, meta_db=meta_db)
if not ir.is_healthy:
    alert(f"memory 不健康: sqlite={ir.sqlite_detail}, "
          f"frontmatter_invalid={ir.frontmatter_invalid}")

# 2. 容量
cr = capacity_govern(root, max_files=10000, max_bytes=500*1024*1024)
if cr.threshold_exceeded:
    log(f"memory 容量告警: total={cr.total_files}, pruned={cr.pruned_count}")

# 3. 备份连续性
backups = list_backups(root)
if not backups or backups[0].name < "2026-06-20":  # 距今 > 1 天
    alert("备份缺失或过期")
```

### 3.3 OTel tracing(M6 已就绪)

```python
from agent_core.memory import configure_tracing
configure_tracing(endpoint="http://otel-collector:4317")

# 之后所有 distillation / migration / search 操作会发 span
# span 名称:
#   - memory.distill.run
#   - memory.migration.migrate_file
#   - memory.retriever.search
```

### 3.4 实时状态(写到日志)

DistillationResult / BackupReport / IntegrityReport 都返回 dataclass,直接 `__str__` / 序列化:

```python
print(result)  # DistillationResult(success=True, candidates=12, written=10, ...)
```

---

## 4. 上线步骤(从 0 到 1)

### 4.1 全新部署(无历史数据)

```bash
# 1. 安装
pip install -e .  # agent-dev 项目

# 2. 初始化目录
mkdir -p ~/.agent_data/memory/{user,project,feedback,tool}
mkdir -p ~/.agent_data/{logs,chroma,memory.backup}

# 3. 跑 demo 验证
.venv/bin/python scripts/demo_v2.1.py  # 应 9/9 步骤通过

# 4. (可选)装 cron
crontab -e
# 0 3 * * * /path/to/scripts/daily_backup.sh   # 每日凌晨 3 点
# 0 4 * * * /path/to/scripts/distill.sh         # 每日凌晨 4 点
```

### 4.2 从 v1.x 升级(有旧 .md 文件)

```bash
# 1. 备份旧数据(防意外)
rsync -a ~/.agent_data/memory/ ~/.agent_data/memory.pre-migration/

# 2. 跑批量迁移
.venv/bin/python -c "
from agent_core.memory.migration import migrate_all
from pathlib import Path
r = migrate_all(Path.home() / '.agent_data' / 'memory')
print(f'migrated={r.migrated}, already={r.already_current}, skipped={r.skipped}')
if r.has_errors:
    print('ERRORS:', r.errors)
"

# 3. 验证
.venv/bin/python -c "
from agent_core.memory import integrity_check
from pathlib import Path
r = integrity_check(Path.home() / '.agent_data' / 'memory')
print(r)
"

# 4. (可选)清理 .bak(确认无问题后)
find ~/.agent_data/memory -name "*.bak" -delete
```

### 4.3 跨进程部署(多 worker / 主机)

**已内置保护**:
- `migrate_all()` 用 IPCLock 跨进程互斥(非阻塞,锁占用返回空 report)
- `DistillationScheduler._acquire_lock()` 自动强占死锁(stale PID / mtime)
- `MetaDB` 用 SQLite WAL 模式(允许多读单写)
- `channel_a_inline_write` 双重锁(进程内 + 跨进程)

**注意事项**:
- ❌ **不要**在 NFS / 跨主机文件系统中使用(flock 不可靠)
- ✅ 适合:单机多 worker、单机 + 定时 cron
- ⚠️ 多主机:用共享存储(EFS 不行,要用 EFS + Redis 分布式锁替代)

---

## 5. 运维 SOP

### 5.1 日常检查(每日)

```bash
# 健康检查
.venv/bin/python -c "
from agent_core.memory import integrity_check, list_backups
from pathlib import Path
ir = integrity_check(Path.home() / '.agent_data' / 'memory')
print(f'healthy={ir.is_healthy}, fm_invalid={ir.frontmatter_invalid}')
backups = list_backups(Path.home() / '.agent_data' / 'memory')
print(f'latest backup: {backups[0].name if backups else None}')
"
```

### 5.2 容量治理(每周)

```python
from agent_core.memory import capacity_govern

r = capacity_govern(
    memory_root,
    max_files=10000,          # 1 万文件上限
    max_bytes=500 * 1024**2,  # 500 MB
    importance_min=3,         # importance >= 3 不淘汰
)
# r.threshold_exceeded / r.pruned_count / r.pruned_paths
```

### 5.3 清理 .bak(迁移完 1 周后)

```bash
# .bak 是迁移前的原文件,迁移完确认无误后可以删
find ~/.agent_data/memory -name "*.bak" -mtime +7 -delete
```

### 5.4 回滚到指定日期

```python
from agent_core.memory import restore_backup
from pathlib import Path

# 把 memory_root 替换为 2026-06-20 的备份
restore_backup(
    backup_date="2026-06-20",
    memory_root=Path.home() / ".agent_data" / "memory",
    meta_db=Path.home() / ".agent_data" / "meta.db",
    vector_index=Path.home() / ".agent_data" / "chroma",
)
# ⚠️ 危险:会覆盖当前数据。建议先 cp 一份出来。
```

### 5.5 强制重置(灾难恢复)

```bash
# 删掉一切,重新初始化
rm -rf ~/.agent_data/memory ~/.agent_data/meta.db ~/.agent_data/chroma
mkdir -p ~/.agent_data/memory/{user,project,feedback,tool}
mkdir -p ~/.agent_data/{logs,chroma}

# 重新跑 cold start(从 seed 列表)
.venv/bin/python -c "
from agent_core.memory import ColdStartLoader
loader = ColdStartLoader(Path.home() / '.agent_data' / 'memory')
loader.load_seeds([...])  # 见 test_coldstart.py
"
```

---

## 6. 故障排查

### 6.1 "integrity_check 不健康"

| 症状 | 原因 | 修复 |
|------|------|------|
| `frontmatter_invalid > 0` | .md 文件损坏 / schema_version 旧 | 看 `ir.frontmatter_invalid_paths`,手动 `validate_frontmatter` 单文件,或跑 `migrate_all()` |
| `sqlite_ok = False` | meta.db 损坏 | `sqlite3 meta.db "PRAGMA integrity_check"`,坏了就 `restore_backup()` |
| `sqlite_detail` 是 "no such table" | meta.db 未初始化 | 重启 agent(自动建表) |

### 6.2 "蒸馏一直失败"

```bash
# 看日志
tail -n 100 ~/.agent_data/logs/*.log | grep -i "distill\|exception"

# 常见原因:
# 1. LLM API key 过期 → 更新 env
# 2. session 文件 < 3 个 → 调 min_session_files=1(临时)
# 3. 锁被死进程占 → 删 .consolidate-lock + 检查 PID
```

### 6.3 "检索召回为空"

| 症状 | 原因 | 修复 |
|------|------|------|
| `retriever_min_score` 太高 | 0.3 → 0.2 试试 | 调 `MemoryConfig.retriever_min_score` |
| embedding 模型不一致 | bge 切换到 minilm | 重建 chroma 索引 |
| chromadb 损坏 | 删 `chroma/` 重建 | 重新 `migrate_all` 触发重建 |

### 6.4 "M8 lifecycle 报错"

| 错误 | 原因 | 修复 |
|------|------|------|
| `LifecycleError: 备份不存在` | backup_date 写错 | 查 `list_backups()` |
| `LifecycleError: 日期格式错` | 用 "2026/06/21" 而不是 "2026-06-21" | 改格式 |
| `CapacityReport.threshold_exceeded` 但 pruned=0 | 全部 `importance >= importance_min` | 调低 importance_min 或调高 max_files |

---

## 7. 性能预算

| 操作 | 预期耗时 | 触发频率 |
|------|----------|----------|
| `channel_a_inline_write` | < 10ms | 每次 turn |
| `channel_b_background_extract`(6 turns) | < 1s | 异步,不阻塞 |
| `retriever.search` | < 200ms(1000 文档) | 每次 LLM 调用 |
| `cold_start.load_seeds` | < 500ms(50 种子) | 一次性 |
| `distillation.run`(10 sessions) | < 30s | 每日 1 次 |
| `migrate_all`(1000 文件) | < 10s | 升级时一次性 |
| `daily_backup`(500 MB) | < 60s(本地 SSD) | 每日 1 次 |
| `integrity_check` | < 2s(1000 文件) | 每日 1 次 |
| `capacity_govern` | < 1s(1000 文件) | 每周 1 次 |

> 全部指标基于 1024 维向量 + chromadb 默认配置 + 本地 SSD。慢盘 / 跨主机场景需重新 benchmark。

---

## 8. 升级路径(后续 milestone)

| 计划 | 模块 | 优先级 |
|------|------|--------|
| 远程同步(S3 / 共享盘) | `agent_core/memory/sync.py` | P1 |
| 多租户隔离 | `agent_core/memory/tenant.py` | P1 |
| 加密静态存储 | `agent_core/memory/crypto.py` | P2 |
| 跨主机分布式锁 | 替换 IPCLock → Redis | P2 |
| 检索排序学习 | reranker 集成 | P3 |
| 自动摘要压缩 | 蒸馏前预压缩 | P3 |

---

## 9. 变更日志

| 日期 | 版本 | 变更 |
|------|------|------|
| 2026-06-21 | v2.1 | M1-M8 全部完成(lifecycle + migration + 8 场景并发) |
| 2026-06-18 | v2.0 | 双通道写入 + 检索 + 蒸馏 |
| 2026-05-XX | v1.x | 早期 .md 文件(无 schema_version,M7 已支持懒迁移) |

---

## 10. 联系 & 反馈

- 测试:`.venv/bin/python -m pytest tests/ -v` 应 ~70+ passed
- demo:`.venv/bin/python scripts/demo_v2.1.py` 应 9/9 通过
- 文档不全 → 提 issue,标注 milestone + 模块名
