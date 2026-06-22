# ReAct 版 UI 记忆系统功能测试手册

> **目标读者**:开发者 / QA / 想亲眼看到记忆系统工作的所有人
> **测试入口**:`streamlit run web/app.py`(默认 8501 端口)
> **覆盖范围**:docs/memory-system-design.md 所有核心功能
> **核心原则**:**每个测试场景给一段可直接复制粘贴的对话脚本 + 一个"如何亲眼看到数据"的路径**,不写抽象 case
> **关联 commit**:`afbeb2c`(M7 移植到 ReAct)+ `c0c2268`(router logger + settings import 修复)

---

## 〇、必读:数据存放路径

> 这是测试要用的所有路径,先把它们在 Finder/文件管理器里打开,边测边看。

### 数据根目录

```bash
# 主目录(由 agent_core.config 自动决定,fallback 到 ~/.agent_data)
~/.agent_data/
```

### 子目录清单

| 路径 | 作用 | 测试时怎么打开 |
|------|------|----------------|
| `~/.agent_data/memory/` | **长期记忆(per-file)** — 你想看到的核心数据 | `open ~/.agent_data/memory` |
| `~/.agent_data/memory/user/` | 用户偏好/事实类记忆 | `open ~/.agent_data/memory/user` |
| `~/.agent_data/memory/project/` | 项目背景类记忆 | `open ~/.agent_data/memory/project` |
| `~/.agent_data/memory/feedback/` | 用户反馈类记忆 | `open ~/.agent_data/memory/feedback` |
| `~/.agent_data/memory/reference/` | 外部资料类记忆 | `open ~/.agent_data/memory/reference` |
| `~/.agent_data/chroma/` | **向量数据库**(Chroma 持久化) | `open ~/.agent_data/chroma` |
| `~/.agent_data/meta.db` | SQLite 元数据(cursors + pending) | `sqlite3 ~/.agent_data/meta.db ".tables"` |
| `~/.agent_data/logs/` | 日常会话日志(append-only JSONL) | `ls ~/.agent_data/logs/` |
| `~/.agent_data/sessions/` | 会话持久化数据 | `ls ~/.agent_data/sessions/` |
| `~/.agent_data/memory.backup/` | M8 daily_backup 生成(脚本调) | `ls ~/.agent_data/memory.backup/` |

### 长期记忆文件长什么样?

打开 `~/.agent_data/memory/user/<hash>.md` 你会看到(类似):

```markdown
---
type: user
title: 用户姓名
created_at: 2026-06-22
schema_version: 2
item_hash: a1b2c3d4e5f6...
importance: 7
tags:
  - name
source_quote: 我叫小明
---

用户名叫小明,做 Python 开发
```

**注意**:文件名是 `item_hash[:12].md`(12 字符 SHA256 前缀),不是人类可读名。要看标题就 cat 文件。

### 向量数据库长什么样?

```bash
# Chroma 持久化目录
~/.agent_data/chroma/
├── chroma.sqlite3          ← 向量元数据(SQLite)
├── <collection_id>/       ← 向量二进制文件
└── ...
```

可以用 Python 看:

```bash
.venv/bin/python -c "
from agent_core.memory import ChromaVectorStore
vs = ChromaVectorStore('~/.agent_data/chroma', collection='react_demo')
print('count:', vs.count())
for item in vs.peek():
    print(item.keys(), '->', list(item.values())[:2])
"
```

---

## 一、测试前准备(必做)

### 1.1 清空旧数据(确保从干净状态开始)

```bash
# 备份后清空
mv ~/.agent_data/memory ~/.agent_data/memory.bak.$(date +%s)
mv ~/.agent_data/chroma ~/.agent_data/chroma.bak.$(date +%s)
mv ~/.agent_data/logs ~/.agent_data/logs.bak.$(date +%s)

# 验证
ls ~/.agent_data/memory 2>&1  # 应该 No such file
```

### 1.2 打开实时文件监听(测试时边聊边看变化)

```bash
# Terminal 1:启动 UI
streamlit run web/app.py --logger.level=debug

# Terminal 2:实时监听记忆文件(每 1s 刷新)
watch -n 1 "ls -la ~/.agent_data/memory/*/"

# Terminal 3:实时查看文件内容
tail -f ~/.agent_data/logs/*.jsonl
```

### 1.3 UI 准备

1. 打开 http://localhost:8501
2. sidebar 选厂商(anthropic 演示最完整,需要 API key)+ 填模型
3. sidebar 展开 `🧠 Memory 状态` → 打开 `启用记忆检索` toggle
4. 此时 Terminal 2 应能看到 `~/.agent_data/memory/{user,project,feedback,reference}/` 4 个目录被自动创建(空)

---

## 二、核心场景(每个场景 = 一段对话脚本 + 亲眼看到什么)

---

### 场景 1:用户事实提取 — "我是谁"

> **对应设计**:§三 触发机制 + §四.1 双通道 A(内联写) + §五 写入流程(per-file)
> **触发原理**:**每次 turn 结束** → `ReactAgent._extract_and_write()` 同步调 LLM 解析最后一对 user/assistant 的 YAML → `MemoryExtractor.process()` 校验/去密/合并 → `MemoryStore.write()` per-file 落盘到 `~/.agent_data/memory/<type>/<hash>.md`。**前置条件**:sidebar `🧠 Memory 状态` 里 `启用实时提取` toggle 必须打开(否则 extractor 不注入)。

#### 对话脚本(逐条复制粘贴到 UI)

```
1. 我叫小明,做 Python 后端开发
2. 我在上海工作,目前在做 agent-dev 这个项目
3. 我最喜欢的代码风格是简洁,反对过度抽象
4. 我习惯用 uv 而不是 pip,因为 uv 更快
```

#### 预期发生(肉眼可见)

**主聊天区**:每条回复**末尾**会显示一条 system 消息:
```
🧠 正在提取记忆...
✅ 已写入 N 条记忆       ← N = 该 turn 实际抽出的条数,1-3 不等
# 或
🧠 无新增记忆            ← 当 LLM 判断这条对话不值得长期记住
```

**Terminal 2 (watch -n 1)**:每条 turn 后目录出现 1-3 个 `.md` 文件
```
~/.agent_data/memory/user/
├── a1b2c3d4e5f6.md   ← 第 1 轮:"我叫小明..."
├── b2c3d4e5f6a1.md   ← 第 1 轮:"做 Python 后端"
├── c3d4e5f6a1b2.md   ← 第 2 轮:"在上海工作"
├── d4e5f6a1b2c3.md   ← 第 3 轮:"喜欢简洁"
├── e5f6a1b2c3d4.md   ← 第 4 轮:"习惯用 uv"
```

**Terminal 1 (后端日志)**:
```
[INFO] react_agent: ✅ 记忆已写入: [user] 用户姓名/职业
[INFO] react_agent: ✅ 记忆已写入: [user] 编程语言偏好
[INFO] react_agent: ✅ 记忆已写入: [user] 地理位置
...
```

**sidebar `🧠 Memory 状态`**:
- `启用实时提取`:✅ 打开
- Searches:4(每 turn 一次)
- Total Hits:0(首次还没历史记忆可命中)
- Last Turn Hits:0
- Stored:user 类计数随 turn 累加(4 轮后约 5-8 条)

**亲眼看到**:打开 `~/.agent_data/memory/user/a1b2c3d4e5f6.md`:
```yaml
---
type: user
title: 用户姓名/职业
importance: 7
schema_version: 2
created_at: '2026-06-22T...'
source_quote: '我叫小明,做 Python 后端开发'
tags: [name, identity]
item_hash: a1b2c3d4...
---

# 用户姓名/职业

用户名叫小明,做 Python 后端开发
```

> **如果 turn 后 `~/.agent_data/memory/` 仍然为空**,检查:
> 1. sidebar toggle 是否打开
> 2. 后端日志有无 `Memory extraction failed:` 警告
> 3. `.env` 里 LLM provider 是否配好(extract 步骤会再调一次 LLM)

---

### 场景 2:记忆召回 — "你记得我吗?"

> **对应设计**:§六 检索流程 + §二 主 Agent 运行时 "1. 检索相关记忆"
> **触发原理**:每次 LLM 调用前 → `MemoryRetriever.search(query, top_k=5)` → 命中后拼成 `[记忆库 / N hits]` 块追加到 system prompt

#### 对话脚本

```
5. 你还记得我的名字吗?我住在哪?
```

#### 预期发生

**Terminal 1 后端日志**:
```
[DEBUG] react_agent: 🧠 [Memory] hits=2 · stored=4 · zero_hit=False
```

**sidebar**:
- Searches:5
- Total Hits:2 ← 跳到 2
- Last Turn Hits:2 ← 跳到 2
- Stored (N):4

**主聊天区 LLM 回答**:应包含 "小明" + "上海" + "agent-dev"(从记忆召回并注入)

**亲眼看到怎么注入的** — 在 `~/.agent_data/chroma/` 里看向量:

```bash
.venv/bin/python -c "
from agent_core.memory import ChromaVectorStore
vs = ChromaVectorStore('~/.agent_data/chroma', collection='react_demo')
print(f'总向量数: {vs.count()}')
hits = vs.query('用户名字', top_k=3)
for h in hits:
    print(f'  - {h[\"title\"]} ({h[\"score\"]:.2f}): {h[\"body\"][:50]}')
"
```

输出:
```
总向量数: 4
  - 用户姓名/职业 (0.92): 用户名叫小明,做 Python 后端开发
  - 居住地/项目 (0.78): 用户在上海工作,目前在做 agent-dev 这个项目
  ...
```

---

### 场景 3:召回阈值边界 — 无关查询触发 0-hit

> **对应设计**:§三 触发机制 + §六 检索相似度阈值
> **触发原理**:`retriever_min_score` 阈值过滤,过低相似度不召回

#### 对话脚本

```
6. 今天上海天气怎么样?
```

#### 预期发生

**Terminal 1**:
```
[DEBUG] react_agent: 🧠 [Memory] hits=0 · stored=4 · zero_hit=True
```

**sidebar**:
- Searches:6
- Total Hits:仍 2
- Last Turn Hits:0
- **Last 0-hit: 1 turns ago** ← 新字段出现!

---

### 场景 4:反馈类记忆 — 提取"用户反馈"

> **对应设计**:§一 设计原则"变化慢的才存" + §五 4 类封闭分类法(user/feedback/project/reference)

#### 对话脚本

```
7. 不要在回答里用 emoji,看着太花哨
8. 以后回答尽量用中文,不要中英混杂
9. 别用 streamlit 之外的 UI 框架,我只用 streamlit
```

#### 预期发生

**Terminal 2**:新文件应出现在 `feedback/` 而非 `user/`
```
~/.agent_data/memory/feedback/
├── e5f6a1b2c3d4.md   ← "不要 emoji..."
├── f6a1b2c3d4e5.md   ← "中文回答..."
├── a1b2c3d4e5f6.md   ← "只用 streamlit..."
```

**亲眼验证** — cat `~/.agent_data/memory/feedback/e5f6a1b2c3d4.md`:
```yaml
---
type: feedback
title: UI 偏好 — 不用 emoji
importance: 8
---
不要在回答里用 emoji
```

---

### 场景 5:反馈召回验证 — LLM 真按反馈走

#### 对话脚本

```
10. 给我推荐一些 React 学习资源
```

#### 预期发生

**Terminal 1**:
```
[DEBUG] react_agent: 🧠 [Memory] hits=3 · stored=7 · zero_hit=False
```

**主聊天区 LLM 回答**:应有以下任一信号
- 不用 emoji(✓)
- 纯中文(✓)
- 提到 streamlit(✓)
- 不推荐 Next.js/Vue(✓)

如果出现 ❌ 现象(用了 emoji 或推荐了别的框架)→ **反馈没正确召回,这是 bug**

---

### 场景 6:项目类记忆 — 提取"项目背景"

#### 对话脚本

```
11. 我们项目叫 agent-dev,目标是做 Memory System v2.1
12. 技术栈是 Python + Streamlit + Chroma
13. 团队只有我一个人,AI 写码为主
```

#### 预期

**Terminal 2**:`project/` 目录出现 3 个文件
**Terminal 1 后端**:Searches 涨到 13,Stored (N):10

---

### 场景 7:参考类记忆 — 提取"外部资料"

#### 对话脚本

```
14. 我看过的最好的 memory 系统设计文档是 Anthropic 的 Claude Code
15. ChromaDB 是我首选的向量数据库
16. miniLM 比 bge-m3 加载快很多
```

#### 预期

`~/.agent_data/memory/reference/` 出现 3 个 `.md`

---

### 场景 8:重启进程 → 记忆持久化验证

> **对应设计**:§五.5 "append-only" + §八 文件结构(per-file + 索引持久化)

#### 步骤

```bash
# 1. 在 UI 里继续聊几句,确认数据稳定
# 17. 我刚才说的都对吧?  ← LLM 应能引用前面所有事实

# 2. 关掉 UI(ctrl+c)
# 3. 重新启动
streamlit run web/app.py

# 4. 不开新会话,继续在原 session 发消息
# 18. 提醒一下我的名字和项目
```

#### 预期发生

**Terminal 2 (重启前后对比)**:文件数应保持一致(`ls ~/.agent_data/memory/*/*.md | wc -l` 数字不变)

**重启后 UI**:sidebar 仍看到 Stored (N)=10(数字从 session_state 读不到,但从 stored_total 字段推算)

**主聊天区 LLM 回答(第 18 条)**:应包含 "小明" + "agent-dev" + "Memory System v2.1" + 全栈技术细节

---

### 场景 9:跨会话记忆隔离 — 新建会话后无记忆

> **对应设计**:§四.6 独立 Context + §F 会话隔离

#### 步骤

```bash
# UI 里点 "➕ 新建会话" 按钮
# 19. 你认识我吗?
```

#### 预期发生

**Terminal 1**:`hits=0` + `stored=10`(全局库还有 10 条,但当前 session 独立)
**sidebar**:Searches 重置为 1, Last Turn Hits=0
**LLM 回答**:不应知道"小明"是谁(记忆没注入到新 session)

> 这是 **session 隔离**,不是 bug。但记忆库本身的 10 条文件还在,持久存在。

---

### 场景 10:并发场景 — 双 channel 写入

> **对应设计**:§四.1 双通道写入架构 + §四.5 不变量测试矩阵

#### 步骤

打开两个浏览器标签,都连同一个 UI 实例,都发消息。预期:
- 不丢消息
- 不重复

> 这个场景在 UI 里很难复现真实并发,建议跑 `scripts/demo_v2.1.py` 步骤 7 验证。

---

### 场景 11:向量库直接验证 — 不走 UI

#### 命令

```bash
.venv/bin/python -c "
from agent_core.memory import ChromaVectorStore, MemoryStore, MemoryRetriever, make_embed_fn
from pathlib import Path

# 加载真实数据
store = MemoryStore(Path.home() / '.agent_data/memory')
vs = ChromaVectorStore(str(Path.home() / '.agent_data/chroma'), collection='react_demo')
embed = make_embed_fn()
retriever = MemoryRetriever(memory_store=store, vector_store=vs, embed_fn=embed)

# 1. 文件系统层
files = list(Path.home() / '.agent_data/memory/user').glob('*.md')
print(f'📁 文件数: {len(files)}')

# 2. SQLite 元数据
import sqlite3
conn = sqlite3.connect(str(Path.home() / '.agent_data/meta.db'))
cur = conn.execute('SELECT key, value FROM cursor')
print(f'📊 cursors: {dict(cur.fetchall())}')

# 3. 向量库
print(f'🔍 向量数: {vs.count()}')

# 4. 检索测试
report = retriever.search('用户名字', top_k=3)
print(f'🎯 检索命中 {len(report.hits)} 条:')
for h in report.hits:
    print(f'  - [{h.type}] {h.title} (score={h.score:.3f})')
    print(f'    {h.body[:60]}')
"
```

---

### 场景 12:记忆手工编辑验证

> **对应设计**:§四.2 Edit-only 工具沙箱 + §一 设计原则 "人类审核"

#### 步骤

```bash
# 1. 打开一个记忆文件
cat ~/.agent_data/memory/user/a1b2c3d4e5f6.md

# 2. 手动修改 body(模拟人工编辑)
sed -i '' 's/小明/小红/' ~/.agent_data/memory/user/a1b2c3d4e5f6.md

# 3. UI 重新发
# 20. 我叫什么名字?
```

#### 预期发生

**LLM 回答**:应说 "小红"(因为记忆文件被人工改了)

> 这就是 per-file 设计的好处 — 记忆和原始日志分离,可以被人手编辑而不破坏其他数据。

---

### 场景 13:手工删除验证

#### 步骤

```bash
# 1. 删掉一条记忆
rm ~/.agent_data/memory/user/a1b2c3d4e5f6.md

# 2. UI 重新发
# 21. 你还记得我叫什么?
```

#### 预期

**LLM 回答**:不应再叫"小明"(除非其他记忆里也有这个名字)

**Terminal 1**:`hits` 可能下降

---

### 场景 14:检索模式切换(高级 — 测试需修改 config)

> **对应设计**:§6.6 三模式共存(vector / file / hybrid)
> **说明**:UI 默认 `mode=hybrid`,改模式需要重启 UI

#### 步骤

```bash
# 1. 改环境变量
export MEMORY_RETRIEVAL_MODE=file
# 2. 重启 UI
streamlit run web/app.py

# 3. 发
# 22. 找一条关于"小明"的记忆
```

#### 预期

**Terminal 1**:`hits > 0`(file 模式 = 关键词匹配,不应受相似度影响)

---

### 场景 15:满容量验证(M8 capacity_govern)

> **对应设计**:M8 A6 capacity_govern

#### 步骤

```bash
# 1. 造 1000 个低 importance 记忆
.venv/bin/python -c "
from agent_core.memory import MemoryStore
from pathlib import Path
import os, time
store = MemoryStore(Path.home() / '.agent_data/memory')
for i in range(1000):
    store.write(type='user', title=f'tmp {i}', body='临时记忆', source_quote='', tags=['tmp'])
print('造 1000 条')
"

# 2. UI 触发一次检索(可能很慢)
# 23. 临时记忆?

# 3. 看容量
.venv/bin/python -c "
from agent_core.memory import capacity_govern
from pathlib import Path
r = capacity_govern(Path.home() / '.agent_data/memory', max_files=500)
print(f'before: {r.total_files}, pruned: {r.pruned_count}')
"
```

#### 预期

`pruned > 0`,旧临时记忆被自动淘汰

---

### 场景 16:备份与恢复(M8 lifecycle)

> **对应设计**:M8 A6 daily_backup + restore_backup

#### 步骤

```bash
# 1. 手动备份
.venv/bin/python -c "
from agent_core.memory import daily_backup, list_backups
from pathlib import Path
r = daily_backup(Path.home() / '.agent_data/memory',
                 meta_db=Path.home() / '.agent_data/meta.db',
                 vector_index=Path.home() / '.agent_data/chroma',
                 today='2026-06-22')
print(r)
"
ls ~/.agent_data/memory.backup/
# 应看到 2026-06-22/ 目录

# 2. 删几条记忆
rm ~/.agent_data/memory/user/a1b2c3d4e5f6.md

# 3. 恢复
.venv/bin/python -c "
from agent_core.memory import restore_backup
from pathlib import Path
restore_backup('2026-06-22', Path.home() / '.agent_data/memory',
               meta_db=Path.home() / '.agent_data/meta.db',
               vector_index=Path.home() / '.agent_data/chroma')
print('restored')
"
```

#### 预期

文件被恢复,UI 重新检索能召回"小明"

---

### 场景 17:完整性检查(M8 integrity_check)

```bash
# 1. 故意制造损坏
echo "broken file" > ~/.agent_data/memory/user/broken.md

# 2. 跑完整性检查
.venv/bin/python -c "
from agent_core.memory import integrity_check
from pathlib import Path
r = integrity_check(Path.home() / '.agent_data/memory')
print(r)
print('healthy:', r.is_healthy)
"
```

#### 预期

`is_healthy=False`,`frontmatter_invalid=1`,`frontmatter_invalid_paths=[broken.md]`

---

### 场景 18:Schema 迁移验证(M7 migration)

```bash
# 1. 写一个 v0 文件(无 schema_version)
cat > ~/.agent_data/memory/user/v0_test.md << 'EOF'
---
title: v0 测试
created_at: 2024-01-01
---
老格式记忆
EOF

# 2. 触发迁移(读这个文件时自动)
.venv/bin/python -c "
from agent_core.memory.memory_store import MemoryStore
from pathlib import Path
store = MemoryStore(Path.home() / '.agent_data/memory')
store.read_by_title('v0 测试')  # 触发懒迁移
"

# 3. 看 .bak sidecar
ls ~/.agent_data/memory/user/v0_test.md*
# 应有 v0_test.md(已升级) + v0_test.md.bak(原 v0 内容)

# 4. 批量迁移
.venv/bin/python -c "
from agent_core.memory.migration import migrate_all
from pathlib import Path
r = migrate_all(Path.home() / '.agent_data/memory')
print(r)
"
```

#### 预期

v0 文件被自动升级到 schema_version=2,所有必填字段补全

---

### 场景 19:cache_namespace 跨厂商验证

> **对应设计**:M7 cache_namespace 透传到 router

#### 步骤

```bash
# 1. UI 切到 anthropic,发消息
# 24. 你好

# 2. 后端日志应包含
[INFO] llm.router: [Anthropic] cache_control: ephemeral applied

# 3. 切 zhipu,再发
# 25. 你好

# 4. 后端日志
[WARNING] llm.router: cache_namespace='react:xxx' passed but zhipu doesn't support cache_control; ignored
```

---

### 场景 20:跨进程并发(M8 Scenario 3)

> **对应设计**:§四.5 不变量测试矩阵场景 3 + M8 IPCLock flock

这个场景 UI 触发不了(需要多进程)。跑脚本:

```bash
# 跑 demo_v2.1.py 的步骤 6(跨进程 flock)
.venv/bin/python scripts/demo_v2.1.py
# 看 "✅ 步骤 6: 跨进程 flock 互斥"
```

---

## 三、故障排查速查表

| 现象 | 怎么排查 |
|------|----------|
| toggle 打开后没任何文件 | `tail ~/.agent_data/logs/*.jsonl` 看是否日志写入正常 |
| 检索 hits=0 但文件存在 | 向量库可能不同步:`rm ~/.agent_data/chroma && restart UI` |
| `Memory system init failed` | 检查 `~/.agent_data/` 是否可写 + `agent_core.config.config.agent_data_dir` 输出 |
| 报 `LLM 流式响应中断: NameError: name 'logger' is not defined` | 升级到 commit `c0c2268` |
| Stored (N) 不出现 | stored_total=0 时不显示,需至少一次召回 |
| LLM 用了 emoji 反馈失效 | 检查 `~/.agent_data/memory/feedback/` 是否有该条 + 向量库能否召回 |

---

## 四、自动化建议

如果要把这些场景转成 pytest,推荐覆盖:

| 场景 | 自动化路径 |
|------|------------|
| 1-4 | mock retriever + memory_store + verify 文件创建 |
| 5 | mock extractor → verify LLM 收到 memory 注入 |
| 8 | 杀进程 → 重启 → verify 文件还在 |
| 11 | 跳过(纯文件 IO,不是 UI 测试) |
| 16-18 | 直接调 lifecycle / migration 函数,UI 不参与 |

> UI 自动化建议用 Selenium / Playwright 跑端到端。但本项目目前更注重"开发者手测 + pytest 单元覆盖",UI 自动化性价比低。

---

## 五、变更历史

| 日期 | commit | 内容 |
|------|--------|------|
| 2026-06-22 | `afbeb2c` | M7 移植到 ReAct + ReAct 版 UI(初版) |
| 2026-06-22 | `c0c2268` | fix: router logger + settings import |
| 2026-06-22 | (本文件) | 重写 UI 测试手册为场景式 + 真实路径指引 |