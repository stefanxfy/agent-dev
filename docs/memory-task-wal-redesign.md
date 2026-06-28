# 记忆持久化任务状态机重构方案

> 项目:agent-dev(自研 Agent 框架)
> 日期:2026-06-25
> 状态:设计阶段,待评审
> 依赖: [`memory-system-design.md`](memory-system-design.md)(v2.2)、[`dual_channel_writer.py`](../agent_core/memory/dual_channel_writer.py)、[`meta_db.py`](../agent_core/memory/meta_db.py)

---

## 〇、问题陈述

### 〇.1 现状痛点

当前 v2.2 实现里,Channel B 的崩溃恢复存在**三个结构性缺陷**:

| # | 缺陷 | 表现 |
|---|------|------|
| 1 | **candidates 不持久化** | LLM 评分结果(`candidates_snapshot`)只在内存,进程崩溃即丢,被 stuck 区间的 turns **永久失去 LLM 评分** |
| 2 | **多源真相分散** | 崩溃恢复要同时读 JSONL(turn 原文) + MetaDB `cursors`(水位线) + `pending_writes`(任务队列),逻辑分散在 `recover_pending` + `_load_messages_for_retry` 两个函数 |
| 3 | **静默失效** | 失败/丢数据没有 UI 反馈,用户感知不到"有些 turn 没被记住" |

### 〇.2 改造目标

1. **candidates 落盘**:LLM 评分结果持久化,崩了不丢
2. **单源真相**:把 turn 原文 + 状态机 + 重试信息 + candidates 收进**一张表**
3. **Channel A 改造**:不再写 JSONL 文件,改为写 SQLite
4. **启动清理**:已完成(DONE)的任务在启动时批量清理,避免表无限增长
5. **失败可见**:FAILED 任务有记录,UI 可查

### 〇.3 范围

| 范围内 | 范围外 |
|--------|--------|
| 替换 Channel A 写盘介质 | Channel A 的 gate 决策逻辑 |
| 替换 `pending_writes` + `cursors` 表 | Channel B 的 LLM 评分 prompt |
| 替换 `recover_pending` 启动逻辑 | 记忆数据本身的 schema |
| 加启动清理逻辑 | 跨进程并发(本期不实装,SQLite 锁免费但本机单进程够用) |
| 加 candidates 落盘 | Channel B 的 LLM 调用重试策略(熔断) |

---

## 一、核心设计理念

| 设计原则 | 说明 |
|---------|------|
| **单表收编** | 一次写入 = 一次状态变化 = 一次 commit,避免跨表事务 |
| **显式状态机** | `state` 字段(NONE / PENDING / INFLIGHT / DONE / FAILED)替代隐式"行有无" |
| **candidates 早落盘** | LLM 评分结果在写 `memory_store` **之前**先写表,崩了不丢 |
| **CAS 抢占** | `UPDATE ... WHERE state IN (...)` 拿任务,rowcount=0 表示别人抢到 |
| **退避有据** | 失败按指数退避(10s / 20s / 40s),`max_attempts=3` 后终态 FAILED |
| **熔断防卡** | INFLIGHT 状态带 `inflight_at` 时间戳,启动时扫超时(> 30min)转 FAILED |
| **启动清理** | DONE 任务在启动时按 `updated_at` 批量删,避免表无限增长 |

---

## 二、目标架构

### 2.1 数据流

```
┌──────────────────────────────────────────────────────────────┐
│                  Turn 结束 (on_turn_end)                       │
└─────────────┬────────────────────────────────────────────────┘
              ↓
   ┌──────────────────┐
   │   Channel A      │ ← 同步, ~10ms(1 次 SQL)
   │  (Inline Write)  │
   └────────┬─────────┘
            ↓ INSERT INTO memory_tasks
            ↓   (state='NONE', turn 原文就位)
   ┌────────┴─────────┐
   │   Gate (LLM)     │ ← 同步, <1s
   │  决定要不要提取   │
   └────────┬─────────┘
            ↓ 通过 → UPDATE state='PENDING'
            ↓
   ┌────────┴─────────┐
   │  Channel B       │ ← 后台线程池
   │  (Extractor)     │
   └────────┬─────────┘
            ↓ 1. CAS 抢占 → UPDATE state='INFLIGHT', inflight_at=now
            ↓ 2. 调 LLM(可能 10-30s)
            ↓ 3. ★ UPDATE state='DONE', candidates_payload=...  ← 关键
            ↓ 4. 写 memory_store + vector_store
            ↓ 5. 提交完成

  ───────────── 启动扫描(startup_scan)─────────────

  启动时:
    [1] 清理:DELETE FROM memory_tasks WHERE state='DONE' AND updated_at < ?
    [2] 熔断:UPDATE state='INFLIGHT' → FAILED  (inflight_at < now-30min)
    [3] 重排:UPDATE state='FAILED' → PENDING   (next_at <= now)
    [4] 派工:SELECT * WHERE state IN ('NONE','PENDING') → 提交 worker
```

### 2.2 与现状的对比

| 维度 | 现状(v2.2) | 重构后 |
|------|------------|--------|
| 写盘介质 | JSONL + SQLite 双源 | 单 SQLite 表 |
| 状态机 | 隐式(行有无) | 显式 `state` 字段 |
| candidates 落盘 | ❌ 内存 | ✅ `candidates_payload` 字段 |
| 崩溃恢复 | 读 JSONL + 查 cursors + 扫 pending | 单 SQL 查询 |
| 启动清理 | ❌ 无 | ✅ DONE 行定期删 |
| 失败可见 | ❌ 静默 | ✅ FAILED 行可查 |
| Channel A 延迟 | ~10ms(JSONL fsync) | ~10ms(SQLite commit) |
| Channel B 写盘原子性 | 半步:candidates 不落盘 | 全步:candidates 必落盘 |
| 跨进程并发 | IPCLock 休眠 | SQLite 锁免费(本机单进程够) |

---

## 三、Schema 设计

### 3.1 单表 `memory_tasks`

```sql
CREATE TABLE IF NOT EXISTS memory_tasks (
    -- ★ 主键
    task_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id     TEXT NOT NULL,
    turn_index     INTEGER NOT NULL,
    
    -- ★ 状态机(显式五态)
    state          TEXT NOT NULL DEFAULT 'NONE',
    --   NONE      : Channel A 刚写完,gate 还没决
    --   PENDING   : gate 通过,等 Channel B worker 拿
    --   INFLIGHT  : Channel B 正在调 LLM
    --   DONE      : candidates 已落盘 + memory_store 已写
    --   FAILED    : 重试用尽(终态,需人工)
    
    -- ★ 重试 / 调度
    attempts       INTEGER NOT NULL DEFAULT 0,
    max_attempts   INTEGER NOT NULL DEFAULT 3,
    next_at        REAL,                -- 下次可执行时间(指数退避)
    inflight_at    REAL,                -- INFLIGHT 开始时间(熔断用)
    
    -- ★ turn 原文(替代 JSONL)
    user_msg       TEXT NOT NULL,
    assistant_resp TEXT NOT NULL,
    turn_metadata  TEXT,                -- JSON: ts / tokens / tool_calls 等扩展
    
    -- ★ 提取结果(candidates 落盘,崩了不丢)
    candidates_payload  TEXT,           -- JSON: [ExtractionCandidate, ...]
    extraction_error    TEXT,           -- 失败时的错误信息
    
    -- ★ 时间戳
    created_at     REAL NOT NULL,
    updated_at     REAL NOT NULL,
    
    UNIQUE (session_id, turn_index)     -- 一个 turn 只能有一行
);

-- 高频查询索引
CREATE INDEX IF NOT EXISTS idx_tasks_state 
    ON memory_tasks(state, next_at);

CREATE INDEX IF NOT EXISTS idx_tasks_session_turn 
    ON memory_tasks(session_id, turn_index);

CREATE INDEX IF NOT EXISTS idx_tasks_compaction 
    ON memory_tasks(state, updated_at);
```

### 3.2 索引设计依据

| 索引 | 服务于 | 查询样例 |
|------|--------|---------|
| `idx_tasks_state` | 启动扫描 | `WHERE state IN ('NONE','PENDING','FAILED') AND next_at <= ?` |
| `idx_tasks_session_turn` | 单 session 扫 | `WHERE session_id=? ORDER BY turn_index` |
| `idx_tasks_compaction` | 启动清理 | `WHERE state='DONE' AND updated_at < ?` |

### 3.3 状态机五态定义

```
                ┌──────────┐
                │   NONE   │ ← Channel A 写完 turn
                └────┬─────┘
                     ↓ gate 通过
                ┌──────────┐
                │ PENDING  │ ← 进入提取队列
                └────┬─────┘
                     ↓ worker CAS 抢占成功
                ┌──────────┐
                │ INFLIGHT │ ← 正在调 LLM
                └────┬─────┘
              成功 ↓ / 失败 ↓
         ┌──────────┐  ┌──────────┐
         │   DONE   │  │  FAILED  │
         └──────────┘  └────┬─────┘
              ↓ 启动清理     ↓ attempts < max_attempts
         (DELETE)        转 PENDING(next_at = now + 退避)
                           ↓ attempts >= max_attempts
                       终态(人工介入)
```

### 3.4 字段精度

| 字段 | 类型 | 选型理由 |
|------|------|---------|
| `state` | TEXT | SQLite 字符串字面量,可读性 > 整型编码 |
| `turn_index` | INTEGER | turn 编号天然整数;UNIQUE(session_id, turn_index) 防重 |
| `candidates_payload` | TEXT(JSON) | LLM 输出本身是 JSON,直接序列化;**避免与 memory_store 双写** |
| `next_at` / `inflight_at` / `updated_at` / `created_at` | REAL | `time.time()` 返回 float,避免 datetime 解析开销 |
| `attempts` / `max_attempts` | INTEGER | 重试计数 |

---

## 四、Channel A 改造

### 4.1 新实现(伪代码)

```python
# 旧(写 JSONL + 调 add_pending + 推 daily_cursor)
def channel_a_inline_write(self, turn_index, user_msg, assistant_resp, ...):
    # 1. JSONL fsync
    self._do_channel_a_write(turn_index, user_msg, assistant_resp)
    # 2. SQLite: add_pending
    self.meta_db.add_pending(self.session_id, {
        "action": "channel_b_extract",
        "turn_range": [turn_index, turn_index],
    })
    # 3. SQLite: 推 daily_cursor
    self.meta_db.set_cursor(self.session_id, "daily", turn_index)

# 新(单 SQL,无 JSONL)
def channel_a_inline_write(self, turn_index, user_msg, assistant_resp, ...):
    self.meta_db.execute("""
        INSERT INTO memory_tasks (
            session_id, turn_index, user_msg, assistant_resp,
            turn_metadata, state, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, 'NONE', ?, ?)
        ON CONFLICT (session_id, turn_index) DO NOTHING
    """, (
        self.session_id, turn_index, user_msg, assistant_resp,
        json.dumps({"ts": time.time(), "tokens": ..., "tool_calls": ...}),
        time.time(), time.time()
    ))
    self.meta_db.commit()
```

### 4.2 收益

| 指标 | 旧 | 新 |
|------|----|----|
| 写盘次数/turn | 1 fsync(JSONL) + 1 add_pending + 1 set_cursor = 3 IO | 1 commit = 1 IO |
| 文件维护 | JSONL 文件要 compaction | 零文件 |
| 跨进程读 | 读 JSONL + 查 MetaDB | 查 MetaDB 一处 |

### 4.3 不变量

- **Channel A 仍是同步**——延迟从 3 IO 降到 1 IO,更短
- **幂等不变**——`UNIQUE(session_id, turn_index)` + `ON CONFLICT DO NOTHING` 保证重入安全
- **崩溃语义不变**——`INSERT ... COMMIT` 要么全成功要么全失败,半行不存在

---

## 五、Channel B 改造

### 5.1 新实现(伪代码)

```python
def channel_b_extract_task(self, task_id: int):
    # ─── 阶段 1:CAS 抢占 ─────────────────────────────
    with self.meta_db.transaction() as conn:
        cur = conn.execute("""
            UPDATE memory_tasks 
            SET state='INFLIGHT', 
                inflight_at=?,
                attempts=attempts+1
            WHERE task_id=? 
              AND state IN ('NONE', 'PENDING', 'FAILED')
        """, (time.time(), task_id))
        if cur.rowcount == 0:
            return  # 别人抢到了,本 worker 退出
        task = conn.execute(
            "SELECT * FROM memory_tasks WHERE task_id=?", (task_id,)
        ).fetchone()
    
    # ─── 阶段 2:调 LLM ──────────────────────────────
    try:
        turn_msg = TurnMessage(
            turn_index=task["turn_index"],
            user_msg=task["user_msg"],
            assistant_resp=task["assistant_resp"]
        )
        candidates = self.llm_extractor([turn_msg])
        
        # ─── 阶段 3:★ candidates 落盘(关键)──────────
        with self.meta_db.transaction() as conn:
            conn.execute("""
                UPDATE memory_tasks 
                SET state='DONE',
                    candidates_payload=?,
                    updated_at=?
                WHERE task_id=?
            """, (
                json.dumps([c.__dict__ for c in candidates], ensure_ascii=False),
                time.time(), task_id
            ))
        
        # ─── 阶段 4:写 memory_store(可重入)──────────
        self.memory_store.write_batch(candidates)
        # ↑ 崩在这:candidates 已落盘,下次 startup_scan 看 state=DONE 
        #   + candidates_payload 非空 → 重写 memory_store(幂等)
        
    except Exception as e:
        # ─── 阶段 5:失败处理 ─────────────────────────
        attempts = task["attempts"] + 1
        max_attempts = task["max_attempts"]
        error_msg = f"{type(e).__name__}: {e}"
        
        with self.meta_db.transaction() as conn:
            if attempts >= max_attempts:
                # 终态:重试用尽
                conn.execute("""
                    UPDATE memory_tasks 
                    SET state='FAILED',
                        extraction_error=?,
                        updated_at=?
                    WHERE task_id=?
                """, (error_msg, time.time(), task_id))
            else:
                # 可重试:指数退避 — 间隔来自 env 配置 (MEMORY_WAL_RETRY_BACKOFF_SECONDS, 默认 60s)
                backoff = self.config.retry_backoff_seconds * (2 ** (attempts - 1))
                # attempts=1 → 60s, attempts=2 → 120s, attempts=3 → 240s
                conn.execute("""
                    UPDATE memory_tasks
                    SET state='FAILED',
                        attempts=?,
                        next_at=?,
                        extraction_error=?,
                        updated_at=?
                    WHERE task_id=?
                """, (attempts, time.time() + backoff, error_msg,
                      time.time(), task_id))
```

### 5.2 阶段 3 的关键性

**旧实现的 bug**:candidates 在内存,LLM 算完直接写 `memory_store`,崩在 `write_batch` 中间 → candidates 永久丢。

**新实现的修复**:LLM 算完先写 `candidates_payload` 到表里,再写 `memory_store`。
- 崩在阶段 3 之前 → 重做整个 LLM(可接受,本来就要做)
- 崩在阶段 3-4 之间 → candidates 落盘了,startup_scan 看 `state=DONE + candidates_payload 非空`,重写 `memory_store`(幂等)
- 崩在阶段 4 之后 → 已 DONE,启动清理会删

### 5.3 `write_batch` 幂等性

`memory_store` 写入是 `INSERT ... ON CONFLICT DO NOTHING`(`UNIQUE(session_id, item_hash)`),所以重复调用安全。

---

## 六、启动扫描(startup_scan)

### 6.1 实现(伪代码)

```python
def startup_scan(self):
    """进程启动时调用:清理 → 熔断 → 重排 → 派工"""

    # ─── 步骤 1:启动清理(详见 §七)────────────
    # 留存期来自 env 配置:done_retention_days / failed_retention_days
    cleanup_done_tasks(self.config.done_retention_seconds)
    cleanup_failed_tasks(self.config.failed_retention_seconds)
    
    # ─── 步骤 2:INFLIGHT 熔断(> 30min 转 FAILED)─
    stuck_cutoff = time.time() - 1800  # 30 分钟
    self.meta_db.execute("""
        UPDATE memory_tasks
        SET state='FAILED',
            next_at=?,
            extraction_error='INFLIGHT timeout (>30min)',
            updated_at=?
        WHERE state='INFLIGHT' AND inflight_at < ?
    """, (time.time() + 10, time.time(), stuck_cutoff))
    self.meta_db.commit()
    
    # ─── 步骤 3:FAILED 到时间 → PENDING────────
    self.meta_db.execute("""
        UPDATE memory_tasks
        SET state='PENDING', updated_at=?
        WHERE state='FAILED' AND next_at <= ?
    """, (time.time(), time.time()))
    self.meta_db.commit()
    
    # ─── 步骤 4:派工(NONE/PENDING → worker 池)──
    pending = self.meta_db.execute("""
        SELECT task_id, session_id, turn_index, user_msg, assistant_resp
        FROM memory_tasks
        WHERE state IN ('NONE', 'PENDING')
        ORDER BY turn_index
    """).fetchall()
    for task in pending:
        self.worker_pool.submit(self.channel_b_extract_task, task["task_id"])
```

### 6.2 步骤 2 的熔断设计

| 场景 | inflight_at | 启动时处理 |
|------|------------|-----------|
| LLM 调用 hang 死 | 30 分钟前 | 转 FAILED + 10s 后重试 |
| 正常完成 | 10 秒前 | 早该转 DONE,不会出现 |
| 进程被 kill -9 | 任意时间 | 超过 30 分钟转 FAILED |

**为什么是 30 分钟**:
- 正常 LLM 调用 10-30s
- 30 分钟 = 正常 60-180 倍,远大于合理上限
- 太短(如 5 分钟):正常慢调用会被误判
- 太长(如 2 小时):hang 死后长时间无响应

### 6.3 步骤 4 的派工顺序

按 `turn_index` 升序处理——保证同一个 session 的提取按 turn 顺序进行,避免乱序写入向量库。

---

## 七、启动清理策略(🆕 关键变更)

### 7.1 目标

避免 `memory_tasks` 表无限增长——DONE 任务在留存期(默认 7 天)后批量删除。

### 7.2 实现

```python
def cleanup_done_tasks(self, retention_seconds: int):
    """启动时调用:删除已留存超过 retention_seconds 的 DONE 任务

    保留依据:
      - DONE 行的 candidates 已落盘 + memory_store 已写,信息已搬走
      - 留存期来自 env:MEMORY_WAL_DONE_RETENTION_DAYS,默认 1 天
    """
    cutoff = time.time() - retention_seconds

    deleted = self.meta_db.execute("""
        DELETE FROM memory_tasks
        WHERE state='DONE' AND updated_at < ?
    """, (cutoff,))
    self.meta_db.commit()

    _log.info(
        f"启动清理(DONE):删除 {deleted.rowcount} 条 "
        f"(留存 > {retention_seconds}s, 早于 {time.strftime('%Y-%m-%d %H:%M', time.localtime(cutoff))})"
    )
    return deleted.rowcount


def cleanup_failed_tasks(self, retention_seconds: int):
    """启动时调用:删除终态 FAILED 留存超期的任务

    保留依据:
      - FAILED 终态(attempts >= max_attempts)已不可重试,留着是给人复盘
      - 留存期来自 env:MEMORY_WAL_FAILED_RETENTION_DAYS,默认 1 天
      - 注意:退避中 FAILED(state='FAILED' 但 next_at 未来)不在清理范围内
    """
    cutoff = time.time() - retention_seconds

    deleted = self.meta_db.execute("""
        DELETE FROM memory_tasks
        WHERE state='FAILED'
          AND attempts >= max_attempts
          AND updated_at < ?
    """, (cutoff,))
    self.meta_db.commit()

    _log.info(
        f"启动清理(FAILED):删除 {deleted.rowcount} 条 "
        f"(留存 > {retention_seconds}s, 早于 {time.strftime('%Y-%m-%d %H:%M', time.localtime(cutoff))})"
    )
    return deleted.rowcount
```

### 7.3 调用时机

**只在启动时调用一次**,不在定时任务里反复跑。

**理由**:
- DONE 行的 `candidates_payload` 仍占空间(几 KB/行),保留默认 1 天足够排错
- 进程运行时清理风险高:可能删了正在被读的 row
- 启动时清理是"已知安全窗口"——无 worker 在跑

### 7.4 配置项(env → Pydantic → 代码)

#### 7.4.1 .env 字段(用户友好)

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `MEMORY_WAL_MAX_RETRY` | int | `3` | 单任务最大重试次数,达到后转 FAILED 终态 |
| `MEMORY_WAL_RETRY_BACKOFF_SECONDS` | int | `60` | 失败退避基础间隔(秒);实际下次时间 = `backoff × 2^(attempts-1)` → 60s / 120s / 240s |
| `MEMORY_WAL_DONE_RETENTION_DAYS` | int | `1` | DONE 行留存天数,启动时超期批量删 |
| `MEMORY_WAL_FAILED_RETENTION_DAYS` | int | `1` | FAILED 终态行留存天数,启动时超期批量删 |

**示例 .env 段**:

```bash
# ─── 记忆任务 WAL 配置(双通道重启改造)────────────────────
MEMORY_WAL_MAX_RETRY=3
MEMORY_WAL_RETRY_BACKOFF_SECONDS=60
MEMORY_WAL_DONE_RETENTION_DAYS=1
MEMORY_WAL_FAILED_RETENTION_DAYS=1
```

#### 7.4.2 Pydantic 模型(校验 + 单位换算)

**不引 pydantic-settings**——项目历史决议(见 [agent_core/memory/config.py:13-14](agent_core/memory/config.py))。沿用 `BaseModel + 手写 from_env + MEMORY_<SECTION>__<FIELD>` 双下划线约定(参考 [config.py:274-302](agent_core/memory/config.py#L274-L302))。

环境变量用"天"和"秒"两种单位,在 Pydantic 模型里**统一换算成秒**,下游代码不感知单位差异。

```python
from pydantic import BaseModel, Field, ConfigDict


class TaskWALConfig(BaseModel):
    """记忆任务 WAL 配置 — 代码内统一使用秒为单位

    env 命名约定(沿用项目风格):
        MEMORY_WAL__MAX_RETRY=3
        MEMORY_WAL__RETRY_BACKOFF_SECONDS=60
        MEMORY_WAL__DONE_RETENTION_DAYS=1
        MEMORY_WAL__FAILED_RETENTION_DAYS=1

    单位换算(仅在 model_validator 里做一次):
        *_RETENTION_DAYS (int) → *_RETENTION_SECONDS (int = days × 86400)
    """
    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    # env 直读字段(单位:秒 / 次数)
    max_retry: int = Field(default=3, ge=1, le=10)
    retry_backoff_seconds: int = Field(default=60, ge=1)

    # 换算后字段(单位:秒,供 cleanup 使用)
    done_retention_seconds: int = Field(default=86400, ge=1)    # 默认 1 天
    failed_retention_seconds: int = Field(default=86400, ge=1)  # 默认 1 天

    @model_validator(mode="before")
    @classmethod
    def _convert_days_to_seconds(cls, data: Any) -> Any:
        """env 里读 *_RETENTION_DAYS(天),换算成 *_RETENTION_SECONDS(秒)。

        允许用户两种写法:
          1. 直接传 seconds: done_retention_seconds=3600
          2. 传 days: done_retention_days=1  →  done_retention_seconds=86400

        days 优先,seconds 兜底(如果都给了,days 赢)。
        """
        if not isinstance(data, dict):
            return data
        for prefix in ("done", "failed"):
            days_key = f"{prefix}_retention_days"
            sec_key = f"{prefix}_retention_seconds"
            if days_key in data and data[days_key] is not None:
                data[sec_key] = int(data[days_key]) * 86400
            data.pop(days_key, None)  # 移除中间字段,不进 model
        return data
```

**关键点**:
- env 字段是 `MEMORY_WAL__DONE_RETENTION_DAYS=1`(双下划线段名,跟项目其他配置一致)
- 字段名带 `__DAYS` 后缀,不是 `_DAYS`(因为 `__` 是项目分段符)
- model_validator 一次性把 days 换算成 seconds,下游代码全用 seconds
- 缺字段走 Pydantic 默认值,不报错

#### 7.4.3 字段映射总表

| .env 字段 | Pydantic 字段 | 单位 | 消费方 |
|----------|--------------|------|--------|
| `MEMORY_WAL__MAX_RETRY` | `max_retry: int=3` | 次 | Channel B 阶段 5 |
| `MEMORY_WAL__RETRY_BACKOFF_SECONDS` | `retry_backoff_seconds: int=60` | 秒 | Channel B 阶段 5 退避公式 |
| `MEMORY_WAL__DONE_RETENTION_DAYS` | `done_retention_seconds: int=86400` | 天 → 秒 | `cleanup_done_tasks` |
| `MEMORY_WAL__FAILED_RETENTION_DAYS` | `failed_retention_seconds: int=86400` | 天 → 秒 | `cleanup_failed_tasks` |

**注意**:`MEMORY_WAL__DONE_RETENTION_DAYS` 和 `MEMORY_WAL__DONE_RETENTION_SECONDS` **互斥**,同给时 days 赢(见 `_convert_days_to_seconds`)。默认走 days 写法,跟 .env.example 对齐。

#### 7.4.4 退避公式

```
next_at = now + retry_backoff_seconds × 2^(attempts - 1)

attempts=1 → 60s
attempts=2 → 120s
attempts=3 → 240s(若 max_retry > 3,继续翻倍)
```

修改 `MEMORY_WAL__RETRY_BACKOFF_SECONDS` 不需要改代码,**重载进程即生效**。

#### 7.4.5 加载顺序

1. `python-dotenv` 加载 `.env` 到 `os.environ`(项目已有,在 `web/app.py:40` / `agent_core/config.py:29, 83`)
2. `MemoryConfig.from_env()` 读 `MEMORY_*` 前缀,自动调用 `_coerce_env_value` 推断类型
3. `MEMORY_WAL__*` 字段进入 `MemoryConfig.wal: TaskWALConfig`
4. `TaskWALConfig.model_validator` 把 `_DAYS` 换算成 `_SECONDS`
5. `MemoryConfig.wal` 注入到 `DualChannelWriter.__init__(task_wal_config=...)`

**挂载点**:`MemoryConfig` 加子字段 `wal: TaskWALConfig = Field(default_factory=TaskWALConfig)`,跟现有 `dedup: DedupConfig` 同级。需要同步修 `MemoryConfig.from_env` 第 300 行的白名单,把 `"wal"` 加进去。

### 7.5 不删除的行

| 状态 | 启动清理 | 备注 |
|------|---------|------|
| `DONE` | ✅ `done_retention_days` 天后删(默认 1 天) | 任务已闭环 |
| `FAILED` 终态(`attempts >= max_retry`) | ✅ `failed_retention_days` 天后删(默认 1 天) | 留存期已过人无需要 |
| `FAILED` 退避中(`attempts < max_retry`) | ❌ 不删 | 还会被 startup_scan 重新激活 |
| `PENDING` / `NONE` / `INFLIGHT` | ❌ 不删 | 任务未闭环 |

### 7.6 边界场景

| 场景 | 行为 |
|------|------|
| 7 天前 DONE,今启动 | 删除 |
| 1 天前 DONE,今启动 | 保留 |
| 8 天前 FAILED(终态) | 保留(FAILED 走独立 30 天规则,本期不实装,留 TODO) |
| `updated_at` 缺失的脏行 | 视为老,删除(`< cutoff` 用 NULL-safe 比较;或预迁移时补齐) |

### 7.7 性能开销

| 数据量 | 启动清理耗时 |
|-------|------------|
| 1000 行 DONE | < 10ms |
| 10000 行 | 50-100ms |
| 100000 行 | 500ms-1s |

**索引 `idx_tasks_compaction (state, updated_at)` 让 DELETE 走索引扫描,O(对数)**。

---

## 八、Channel B 派工与并发

### 8.1 Worker 池

| 配置 | 默认 | 说明 |
|------|-----|------|
| `max_workers` | 2 | 线程池大小 |
| 任务队列 | 无界 | SQLite 行数 < 10000 时足够 |

### 8.2 CAS 抢占的并发安全

```sql
-- Worker A 和 B 同时调这句,只有一个会拿到 rowcount=1
UPDATE memory_tasks 
SET state='INFLIGHT' 
WHERE task_id=? AND state IN ('NONE','PENDING','FAILED')
```

SQLite 写锁串行化,无需额外同步原语。

### 8.3 跨进程并发(本期不实装)

`IPCLock` 已在 `dual_channel_writer.py` 存在但未使用。本期单进程够用,跨进程并发留 TODO:

```python
# TODO(跨进程并发):多进程同 session 启动时,需要
# 1. flock 抢 start_scan 权
# 2. 抢不到的进程跳过派工,只做读
```

---

## 九、错误处理与可观测性

### 9.1 错误分类

| 错误类型 | 捕获位置 | 处理 |
|---------|---------|------|
| LLM 调用失败 | `channel_b_extract_task` 阶段 2 | 退避重试 |
| SQLite 写盘失败 | `meta_db.transaction()` | 自动 ROLLBACK + 上抛 |
| `memory_store.write_batch` 失败 | 阶段 4 | 上抛 → 阶段 5 退避 |
| JSON 序列化失败 | `json.dumps(candidates)` | 上抛 → 阶段 5 退避 |

### 9.2 日志

| 事件 | 级别 | 字段 |
|------|-----|------|
| Channel A 写盘 | DEBUG | `task_id, turn_index` |
| CAS 抢占成功 | DEBUG | `task_id, attempts` |
| CAS 抢占失败 | DEBUG | `task_id`(别人抢到) |
| LLM 调用开始 | INFO | `task_id, turn_index` |
| LLM 算完,落盘 candidates | INFO | `task_id, count(candidates)` |
| memory_store 写完 | INFO | `task_id` |
| 失败退避 | WARNING | `task_id, attempts, next_at, error` |
| INFLIGHT 熔断 | WARNING | `task_id, inflight_at, 持续时长` |
| 启动清理 | INFO | `deleted_count, cutoff_time` |
| 终态 FAILED | ERROR | `task_id, attempts, error`(需人工) |

### 9.3 UI 暴露

(本期不实装,留 TODO) `FAILED` 终态任务应在 UI 列出,带"重试"按钮。

---

## 十、迁移路径

### 10.1 现状盘点

```python
# meta_db.py 当前 3 张表
cursors              # 整体水位线(daily/extract)        — 弃用
pending_writes       # 任务队列(action+payload+attempts) — 弃用
candidates           # 候选记忆(item_hash+status)        — 保留
candidate_decisions  # 决策回灌(M10 C4.4)               — 保留

# dual_channel_writer.py 当前写盘
# 1. Channel A 写 JSONL  ← 弃用
# 2. Channel A 写 add_pending  ← 弃用
# 3. Channel A 写 set_cursor  ← 弃用
# 4. Channel B 写 memory_store  ← 保留
# 5. recover_pending 读 cursors + pending_writes  ← 弃用,改 startup_scan
```

### 10.2 迁移步骤

| 阶段 | 内容 | 风险 |
|------|------|------|
| **1. 加表 + 写** | 新建 `memory_tasks`,Channel A/B 双写(旧表 + 新表) | 低,旧路径仍主导 |
| **2. 切读** | `recover_pending` 优先读 `memory_tasks`,旧表兜底 | 中,需跑回归 |
| **3. 切写** | 旧表停写,只写 `memory_tasks` | 高,需全量测试 |
| **4. 清理** | DROP `cursors` / `pending_writes` 表 | 低,已无写入 |

### 10.3 兼容期

阶段 1-2 期间,**两套表同时存在**。验证期 1-2 周,期间保留旧表只读,出问题可回滚。

---

## 十一、测试矩阵

### 11.1 单元测试

| 测试 | 验证点 |
|------|--------|
| `test_channel_a_insert_idempotent` | 同一 turn_index 二次插入,rowcount=0 |
| `test_cas_grab_succeeds_once` | 两个并发 CAS,只有一个 rowcount=1 |
| `test_candidates_payload_persists` | LLM 算完前 kill -9,重启后能反序列化 candidates |
| `test_failure_backoff_schedule` | 第 1/2/3 次失败 next_at = now+10/20/40 |
| `test_max_attempts_terminal` | 第 3 次失败 state=FAILED,attempts=3 |
| `test_inflight_熔断` | inflight_at = now-31min,启动时转 FAILED |

### 11.2 集成测试

| 测试 | 验证点 |
|------|--------|
| `test_startup_scan_drops_done` | 8 天前 DONE 行被删,1 天前保留 |
| `test_startup_scan_drops_failed_terminal` | 终态 FAILED(`attempts >= max_retry`)超期被删,退避中 FAILED 保留 |
| `test_cleanup_uses_env_config` | 改 env `MEMORY_WAL_DONE_RETENTION_DAYS=7`,验证 6 天前保留、8 天前删 |
| `test_startup_scan_moves_pending` | NONE/PENDING 提交到 worker |
| `test_kill9_mid_extract_recovers` | 阶段 3-4 之间 kill -9,重启后不丢 candidates |
| `test_p0_idempotency` | 同一 task_id 重复派工,只成功一次 |

### 11.3 回归测试

旧 `test_pending_recovery.py` / `test_race_pending_extract.py` 全部继续通过。

---

## 十二、关键设计决策回顾

| 决策 | 选择 | 备选 | 理由 |
|------|------|------|------|
| 数据合并 | 单表 `memory_tasks` | 多表(状态/candidates 分开) | 简单,避免跨表事务 |
| 状态机 | 显式 TEXT 字段 | 整型编码 | 可读性,易调试 |
| candidates 存法 | 同表 JSON 字段 | 单独 candidates 表 | 1:1 关系,无 N:N 价值 |
| Channel A 是否取消 | 取消 JSONL,改写 SQLite | 保留 JSONL 备份 | 单源真相,减少 IO |
| 启动清理频率 | 仅启动时 | 定时任务 | 已知安全窗口,无 worker 干扰 |
| DONE 留存期 | 7 天 | 立即删 / 30 天 | 排错够用,空间可控 |
| FAILED 留存期 | 30 天 | 永久 / 7 天 | 人工复盘需要 |
| 跨进程并发 | 本期不实装 | 立即支持 | 单进程够用,降低复杂度 |
| candidates 落盘时机 | LLM 算完 → 写表 → 写 memory_store | 写 memory_store → 写表 | 早落盘降低丢失窗口 |

---

## 十三、风险与缓解

| 风险 | 影响 | 缓解 |
|------|------|------|
| SQLite 单表行数爆炸 | 查询慢,占用空间大 | 启动清理 + 监控行数 |
| candidates_payload 体积大 | 写盘慢 | 仅 DONE 状态有此字段(其他 NULL) |
| INFLIGHT 熔断误判 | 正常慢调用被中断 | 30 分钟阈值远大于正常 |
| FAILED 终态堆积 | 30 天后还在 | FAILED 30 天后单独清理(本期不实装) |
| 迁移期双写不一致 | 旧表新表状态不同步 | 阶段 1-2 旧表只读兜底,出问题回滚 |
| candidates JSON 解析失败 | 启动时反序列化出错 | try/except,降级为重做 LLM |

---

## 十四、落地步骤(分阶段)

### Phase 1:Schema + Channel A(1 周)

- [ ] 写 `memory_tasks` 表的 DDL + 索引
- [ ] `MetaDB` 加 `insert_task` / `get_task` / `update_task_state` 三个方法
- [ ] 改 `channel_a_inline_write`:写 SQLite 替代 JSONL
- [ ] 跑现有 60+ 测试,确认没回归
- [ ] 灰度开关:`config.use_task_wal = False` 时回退到旧 JSONL

### Phase 2:Channel B 改造(1 周)

- [ ] 改 `channel_b_extract_task`:CAS + candidates 落盘
- [ ] 加退避 + max_attempts
- [ ] 写 `startup_scan` 替换 `recover_pending`
- [ ] 加单元测试 + 集成测试
- [ ] Phase 1 的灰度开关保留

### Phase 3:启动清理 + env 配置(3 天)

- [ ] 写 `cleanup_done_tasks` / `cleanup_failed_tasks` 两个清理函数
- [ ] 写 `TaskWALEnvSettings`(读 .env)+ `TaskWALConfig`(单位换算)两层 Pydantic 模型
- [ ] 在 `.env.example` 追加 `MEMORY_WAL_*` 4 个字段并附注释
- [ ] `startup_scan` 步骤 1 同时调 DONE + FAILED 清理
- [ ] 加 `test_startup_scan_drops_done` / `test_startup_scan_drops_failed_terminal` / `test_cleanup_uses_env_config` 三个集成测试
- [ ] 验证性能:1 万行 DONE 清理 < 1s
- [ ] 验证 .env 缺字段时 Pydantic 走默认值,不报错

### Phase 4:清理旧表(1 周观察期后)

- [ ] 观察期 1 周,确认新表运行稳定
- [ ] DROP `cursors` / `pending_writes` 表
- [ ] 删 `recover_pending` / `_load_messages_for_retry`
- [ ] 删 IPCLock 死代码
- [ ] 更新 design doc,标记旧章节为"已废弃"

---

## 十六、实施完成(2026-06-25)

四 Phase 全部完成,实际 commit 数与计划对照:

| 阶段 | 计划 | 实际 | 关键 commit |
|------|------|------|-------------|
| Phase 1:Schema + Channel A | 8 | 8 | `0572c2a`..`6287fca` |
| Phase 2:Channel B + startup_scan | 10 | 12(+2.10a/2.10b) | `0fe72b0` 收官 |
| Phase 3:env + 启动清理 | 5 | 5 | `9937318` |
| Phase 4:清理旧表 + 抽 conftest | 7-8 | 4(4.4.1 + 4.4.2-4.4.6 bundled + 4.4.7 + 本节) | `e340abb` / `cbb4f82` / `75de3e9` |
| **合计** | **30-31** | **~29** | `git log --oneline 0572c2a^..75de3e9` |

### 16.1 主要变更

- `cursors` / `pending_writes` 表 DROP(`_DDL` 末尾追加,2026-06-25 commit `e340abb`)
- `MetaDB` 删 7 死方法(`set_cursor` / `get_cursor` / `add_pending` / `remove_pending` /
  `list_pending` / `bump_pending_attempts` / `update_pending_payload`)
- `DualChannelWriter` 删 Channel A 旧 3 IO / Channel B 旧 add_pending / `recover_pending` /
  `_load_messages_for_retry` / `_on_recovery_done` / IPCLock 全部引用
- 5 个失效旧测试删除:`test_pending_recovery.py` / `test_dual_channel_minimal.py` /
  `test_race_pending_extract.py` / `test_channel_b_writes_session_id.py` /
  `test_channel_b_secret_sanitize.py`
- `tests/conftest.py` 新建:FakeEmbedFn + 8 fixture(集中放置,pytest 自动发现)
- `tests/test_no_legacy_dead_code.py` 新建:18 测试守住死代码不再回潮

### 16.2 测试结果

```
572 passed, 3 skipped, 4 warnings in ~6m30s
```

### 16.3 回滚预案

Phase 4 已是最后阶段,回滚方案:

- **Phase 1-3**:`git revert` 对应 commit(旧表/旧方法不删,代码侧回滚安全)
- **Phase 4**:`git revert e340abb cbb4f82 75de3e9`
  - 旧 `cursors` / `pending_writes` 表 DROP 不可逆(实际不需要,新代码不读)
  - 死方法删除可逆(从 git history 恢复)
  - 抽 conftest 可逆(测试文件回滚到本地 FakeEmbedFn)

### 16.4 已知遗留

- `web/app.py` 与 `web/app_langgraph.py` 都引用 `DualChannelWriter`,Phase 4 后
  `daily_cursor` / `extract_cursor` 属性已删除;`app_langgraph.py` 仍可能引用旧属性
  (未触碰,MEMORY.md 约束)
- IPCLock 实现代码本身(`agent_core/memory/ipc_lock.py` 等)若存在,未删除
  (Phase 4 计划仅要求 DualChannelWriter 不再引用,文件本身保留供未来使用)

---

## 十五、参考

- 当前实现:[`agent_core/memory/dual_channel_writer.py`](../agent_core/memory/dual_channel_writer.py)
- 元数据库:[`agent_core/memory/meta_db.py`](../agent_core/memory/meta_db.py)
- 测试覆盖矩阵:[`docs/memory-test-coverage-matrix.md`](memory-test-coverage-matrix.md)
- 主设计文档:[`docs/memory-system-design.md`](memory-system-design.md)(v2.2 §4.1-§4.8 双通道章节将整体重构)
