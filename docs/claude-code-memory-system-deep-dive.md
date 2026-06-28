# Claude Code 记忆系统完整实现解析

> 本文档基于 Claude Code 源码（`/Users/fanyunxu/Desktop/myproject/ailearning/claude-code-analysis/src/`）的完整阅读，对其中**记忆系统（Memory System）**的工程实现做一次系统性的解析。涵盖文件存储、四类记忆、写入路径、检索机制、压缩/遗忘流程、团队同步、UI 呈现与安全模型。

---

## 0. 全局视角：记忆系统架构总览

Claude Code 的"记忆"并不是单一组件，而是一组**分层、协同**的子系统，各自负责不同时间尺度 / 不同作用范围 / 不同存储介质的记忆需求。

```
┌────────────────────────────────────────────────────────────────────────┐
│                        Claude Code Memory Stack                        │
├────────────────────────────────────────────────────────────────────────┤
│                                                                        │
│  ┌──────────────────────────────────────────────────────────────────┐  │
│  │  L1  系统提示层（每次会话都加载）                                   │  │
│  │   - MEMORY.md 索引（项目自动记忆）                                  │  │
│  │   - CLAUDE.md（用户 / 项目 / 本地，含 @import）                     │  │
│  │   - Agent Memory（按 agentType 隔离）                              │  │
│  │   - Team Memory（团队共享索引）                                    │  │
│  └──────────────────────────────────────────────────────────────────┘  │
│  ┌──────────────────────────────────────────────────────────────────┐  │
│  │  L2  按需召回层（query-time relevance selection）                  │  │
│  │   - findRelevantMemories：Sonnet sideQuery 选 ≤5 个最相关文件      │  │
│  │   - 已有 / 已展示文件去重                                          │  │
│  └──────────────────────────────────────────────────────────────────┘  │
│  ┌──────────────────────────────────────────────────────────────────┐  │
│  │  L3  会话内压缩层（context overflow 时）                           │  │
│  │   - sessionMemoryCompact：用 SessionMemory 替换 /api/compact       │  │
│  │   - 保留 lastSummarizedMessageId 之后的最近消息                    │  │
│  └──────────────────────────────────────────────────────────────────┘  │
│  ┌──────────────────────────────────────────────────────────────────┐  │
│  │  L4  写入与提取层（turn-end / background）                          │  │
│  │   - extractMemories：forked agent 抓取 user/feedback/project/      │  │
│  │     reference 四类记忆                                             │  │
│  │   - SessionMemory extraction：滚动汇总当前 session                  │  │
│  └──────────────────────────────────────────────────────────────────┘  │
│  ┌──────────────────────────────────────────────────────────────────┐  │
│  │  L5  整合/遗忘层（cross-session 蒸馏）                             │  │
│  │   - autoDream：按时间 / 数量门控触发跨 session 整合                 │  │
│  │   - /dream：用户手动触发                                            │  │
│  └──────────────────────────────────────────────────────────────────┘  │
│  ┌──────────────────────────────────────────────────────────────────┐  │
│  │  L6  同步层（multi-user 共享）                                     │  │
│  │   - teamMemorySync：server-side upsert + ETag + 冲突重试          │  │
│  │   - watcher（fs.watch { recursive:true }）+ debounce                │  │
│  └──────────────────────────────────────────────────────────────────┘  │
│  ┌──────────────────────────────────────────────────────────────────┐  │
│  │  L7  持久化与安全层                                                 │  │
│  │   - path validation（realpath + NFC + null byte + symlink）         │  │
│  │   - secretScanner（gitleaks 规则）                                  │  │
│  │   - mode 0o600 / 0o700（SessionMemory）                            │  │
│  └──────────────────────────────────────────────────────────────────┘  │
└────────────────────────────────────────────────────────────────────────┘
```

**关键设计原则**（贯穿全部源码注释）：

1. **记忆是文件**，不是数据库。`MEMORY.md` 是索引；每个记忆一个文件 + frontmatter。
   > **解释**：选择文件而非 SQLite 等结构化存储，是为了让用户能直接用编辑器/git 读写记忆——可读性、可审计性、可版本化是首要指标。
2. **存在四类封闭分类法**（`user` / `feedback` / `project` / `reference`），不存"代码可推导的信息"。
   > **解释**：封闭分类法意味着 LLM 无法"发明"第五类（如 `task`、`episodic`），系统提示词里的清单是穷举的。剔除"代码可推导"是为了让记忆承载"为什么"而非"是什么"。
3. **写入双通道**：主 agent 自己写（通过系统提示词引导）+ 后台 forked agent 兜底提取；通过 `hasMemoryWritesSince` 实现互斥。
   > **解释**：互斥机制避免同一轮 turn 里主 agent 和后台 agent 都往同一个文件写、产生冲突；cursor（消息游标）随主 agent 写入推进，后台 agent 据此跳过已处理区间。
4. **三层时间尺度**：`SessionMemory`（会话内压缩）→ `autoMemory`（跨会话）→ `autoDream`（跨多会话整合）。
   > **解释**：从短到长三层对应"上一句对话 → 本次会话 → 最近 N 次会话"，每一层用不同的去重/合并策略。
5. **权限边界**：所有路径操作都走 `realpath` + `NFC` + null byte + symlink 检查；写操作严格限定在 memdir 内。
   > **解释**：`realpath` 解析所有符号链接，`NFC` 防止 Unicode 组合字符伪造路径（如 `é` 的两种写法），null byte 防止 C 风格字符串截断，symlink 检查防止"软链逃出沙箱"。

---

## 1. 记忆分类法（Taxonomy）：四类封闭语义

源码位置：[src/memdir/memoryTypes.ts](../../ailearning/claude-code-analysis/src/memdir/memoryTypes.ts)

### 1.1 四类及其边界

```ts
export const MEMORY_TYPES = ['user', 'feedback', 'project', 'reference'] as const
export type MemoryType = (typeof MEMORY_TYPES)[number]
```

> **解释**：`as const` 让 TypeScript 把数组字面量收窄为只读元组类型 `(...)`，`type MemoryType = (typeof MEMORY_TYPES)[number]` 进一步派生出字符串字面量联合类型 `'user' | 'feedback' | 'project' | 'reference'`。这两行让四类在编译期封闭——任何拼写错误或新增类目都会编译失败。

| Type       | 必存项                          | 必存触发                                              | 作用                       |
| ---------- | ------------------------------- | ----------------------------------------------------- | -------------------------- |
| `user`     | 用户角色 / 目标 / 知识背景      | "I'm a data scientist…"; "I've been writing Go…" | 调整回答视角与详略 |
| `feedback` | 用户对工作方式的纠正 / 确认     | "don't mock the database…"; "stop summarizing…"   | 沿用对的方式、避开错的   |
| `project`  | 项目背景、deadline、决策、动机  | "we're freezing merges after Thursday for release…" | 给出更贴背景的建议 |
| `reference`| 外部系统的指针（Linear、Slack） | "bugs are tracked in Linear project INGEST"      | 知道"在哪查"           |

> **解释**：每类的"必存项"与"必存触发"一一对应——`user` 抓身份，`feedback` 抓纠偏，`project` 抓背景，`reference` 抓外部指针。这是一种**用输入样例教学**的方式，比抽象规则更容易让 LLM 学会判断。

**Why-How 模板**（针对 `feedback` / `project`）：

```markdown
<rule or fact>

**Why:** <reason — often an incident or strong preference>
**How to apply:** <when/where this kicks in>
```

> **解释**：把记忆内容分成三段：**规则本身**（事实层）、**Why**（决策动机）、**How to apply**（触发条件）。这让 LLM 在面对边界情况时能推理"该不该遵守"而不是死板匹配。

注释里特别指出："Knowing *why* lets you judge edge cases instead of blindly following the rule."
> **解释**：把"为什么"写下来是抗规则漂移的关键——黑名单规则 "always do X" 在新场景下可能失效，但 "do X because Y" 让 LLM 能判断 Y 是否仍成立。

### 1.2 明确**禁止**写入的内容（WHAT_NOT_TO_SAVE_SECTION）

```ts
'- Code patterns, conventions, architecture, file paths, or project structure — these can be derived by reading the current project state.'
'- Git history, recent changes, or who-changed-what — `git log` / `git blame` are authoritative.'
'- Debugging solutions or fix recipes — the fix is in the code; the commit message has the context.'
'- Anything already documented in CLAUDE.md files.'
'- Ephemeral task details: in-progress work, temporary state, current conversation context.'
```

> **逐行解释**：
> - 第 1 条："不要存代码模式/约定/架构/文件路径"——这些每次读代码就能重新推导，存了反而会因为代码演化而立刻过期。
> - 第 2 条："不要存 git 历史/最近改动"——`git log`、`git blame` 是权威源，比 LLM 摘要更可靠。
> - 第 3 条："不要存调试解决方案"——修复已经在代码里，commit message 里有上下文；存记忆相当于复制一份会过期的副本。
> - 第 4 条："不要存 CLAUDE.md 已有的内容"——避免双源真相（single source of truth）。
> - 第 5 条："不要存临时任务细节"——进程内状态和当前对话上下文不属于跨会话记忆。

注释里强调："These exclusions apply even when the user explicitly asks you to save" — 即使用户显式要求，如果本质是活动日志 / 琐事，也要拒绝并询问"哪里 surprising / non-obvious"。
> **解释**：这条规则保护记忆库的"信噪比"——用户有时会说"把这次对话存下来"，但如果内容只是当前 PR 的细节，存了反而是噪声。LLM 应该追问"哪里 surprising / non-obvious"，把请求收敛到真正值得长期记忆的内容。

### 1.3 Recall 端的两段重要提示

源码通过两段分层的 system prompt section（**位置很关键**）来减少"recall 错答"：

**WHEN_TO_ACCESS_SECTION**（何时访问）：

```ts
'- When memories seem relevant, or the user references prior-conversation work.'
'- You MUST access memory when the user explicitly asks you to check, recall, or remember.'
'- If the user says to *ignore* or *not use* memory: proceed as if MEMORY.md were empty. Do not apply remembered facts, cite, compare against, or mention memory content.'
'- Memory records can become stale over time... If a recalled memory conflicts with current information, trust what you observe now — and update or remove the stale memory rather than acting on it.'
```

> **逐条解释**：
> - 第 1 条："当记忆似乎相关时"——默认鼓励主动回忆。
> - 第 2 条："用户显式要求时**必须**访问"——硬性约束，避免漏召回。
> - 第 3 条："用户说忽略记忆时"——彻底当作空，连"对比"和"提及"都不允许；这条很关键，否则 LLM 会习惯性地"引用但不采用"，反而干扰回答。
> - 第 4 条："记忆与现状冲突时"——以**当下观察**为准，并主动更新/删除过期记忆（形成反馈环）。

**TRUSTING_RECALL_SECTION**（**单独的 H1 段而非 WHEN 段的子项**）：

注释明确说明：H1 eval "0/2 → 3/3 via appendSystemPrompt"——把"recall 后如何行动"从 WHEN 段抽出来作为独立 section，触发效果完全不同。

> **解释**：这是一个 A/B 测试结论——同样的内容放在 H1（独立一级标题）和放在 WHEN 的子项里，模型行为差异巨大（0/2 通过 vs 3/3 通过）。Claude Code 用 `appendSystemPrompt` 把这条**追加**到主 prompt 之后（而非合并到 MEMORY 段里），从而在注意力机制里获得独立的"权重高峰"。

```ts
'## Before recommending from memory',
'',
'A memory that names a specific function, file, or flag is a claim that it existed *when the memory was written*. It may have been renamed, removed, or never merged. Before recommending it:',
'',
'- If the memory names a file path: check the file exists.',
'- If the memory names a function or flag: grep for it.',
'- If the user is about to act on your recommendation (not just asking about history), verify first.',
'',
'"The memory says X exists" is not the same as "X exists now."',
'',
'A memory that summarizes repo state (activity logs, architecture snapshots) is frozen in time. If the user asks about *recent* or *current* state, prefer `git log` or reading the code over recalling the snapshot.'
```

> **逐段解释**：
> - 开篇："A memory ... is a claim"——把记忆重新定义为**声明（claim）**而非**事实（fact）**，这是认知上的关键转换：记忆是某人在某时刻的快照，不等于现状。
> - 三条具体检查动作："check file exists / grep / verify first"——给出可执行的验证 SOP。
> - "user is about to act"——区分"用户询问历史"（用记忆即可）和"用户将行动"（必须验证），因为前者出错代价低，后者代价高。
> - 金句："The memory says X exists" is not the same as "X exists now." —— 一句话总结。
> - "frozen in time"——repo state 类快照（架构图、活动日志）天然过期；用户问"最近"或"现在"时，应优先用 `git log` 或读代码。

### 1.4 漂移防护（memoryAge.ts）

```ts
// memoryAgeDays: floor((now - mtime) / 86_400_000)
export function memoryAgeDays(mtimeMs: number): number
export function memoryAge(mtimeMs: number): string  // 'today' | 'yesterday' | '47 days ago'

// 对 >1 天的记忆追加 staleness 提示
export function memoryFreshnessText(mtimeMs: number): string {
  if (d <= 1) return ''
  return `This memory is ${d} days old. Memories are point-in-time observations, not live state — claims about code behavior or file:line citations may be outdated. Verify against current code before asserting as fact.`
}
```

> **逐行解释**：
> - `86_400_000` = 一天的毫秒数（86,400 秒 × 1000）；`floor` 向下取整得到完整天数。
> - `memoryAge` 输出可读字符串：`today` / `yesterday` / `47 days ago`，用于在 UI 上展示"上次见到这条记忆是多久前"。
> - `memoryFreshnessText`：对 >1 天的记忆返回警告文字（注入到 system prompt），让 LLM 知道自己读到的是"老数据"。≤1 天的记忆返回空字符串，避免给每条新记忆都加干扰。

驱动力来自真实用户反馈："stale code-state memories (file:line citations to code that has since changed) being asserted as fact — the citation makes the stale claim sound more authoritative, not less."
> **解释**：用户反馈指出"带 `file:line` 引用的记忆"反而更危险——LLM 看到精确引用时倾向于"信任而不验证"，但代码早就变了。"cite 增加权威感"是人类/AI 的共同认知偏差，需要专门对抗。

---

## 2. 存储模型：文件即记忆

### 2.1 目录与文件布局

源码位置：[src/memdir/paths.ts](../../ailearning/claude-code-analysis/src/memdir/paths.ts)

#### 2.1.1 默认 Auto Memory 路径解析顺序

```ts
getAutoMemPath() {
  // 1. CLAUDE_COWORK_MEMORY_PATH_OVERRIDE  (env, 来自 SDK/Cowork)
  // 2. autoMemoryDirectory in settings.json (policySettings / flagSettings / localSettings / userSettings)
  //    注意：projectSettings 故意排除 — 防恶意 repo 写 autoMemoryDirectory: "~/.ssh"
  // 3. <memoryBase>/projects/<sanitized-git-root>/memory/
  //    memoryBase = CLAUDE_CODE_REMOTE_MEMORY_DIR ?? ~/.claude
}
```

`getAutoMemPath` 用 `memoize` 缓存（按 `projectRoot` 维度），注释解释："render-path callers fire per tool-use message per Messages re-render; each miss costs getSettingsForSource × 4 → parseSettingsFile (realpathSync + readFileSync)"——这是一个**性能优化细节**，把 settings 读取 cache 住。

> **解释**：`memoize` 是函数记忆化——同一个 `projectRoot` 只算一次。源码注释解释了为什么必须缓存：每次 React 组件重新渲染都会触发 path lookup，而每次未命中需要读 4 个 settings 文件、realpathSync + readFileSync，是真实可观测的性能瓶颈。

#### 2.1.2 路径验证（防御纵深）

```ts
function validateMemoryPath(raw, expandTilde) {
  // - reject: 相对路径、根路径、Windows drive-root、UNC、null byte
  // - normalize() 处理 .. 段
  // - NFC 归一化（防 Unicode 折り返し）
  // - settings.json 路径支持 ~/ 展开，但 "~", "~/", "~/.", "~~/.." 不展开
}
```

> **逐条解释**：
> - **reject**：相对路径（如 `./foo`）、根路径（`/`）、Windows drive-root（`C:\`）、UNC 网络路径（`\\server\share`）、null byte（`\0`）——这些是常见路径攻击载体，先在字符串层拒绝。
> - **normalize()**：把 `foo/../bar` 还原为 `bar`，处理 `..` 跳出。
> - **NFC**：把 Unicode 字符串标准化为"组合字符序列"形式（如 `é` 的两种写法统一为一种），防止"看起来像 `foo` 但其实不是"的绕过。注释里的"折り返し"是日语借词，意为"折回 / 还原"，说明 Claude Code 的开发团队跨时区协作。
> - **Tilde 展开**：`~/foo` 展开为 `$HOME/foo`，但裸 `~`、`~/`、`~/.`、`~/..` 拒绝展开（防止"展开后变成 `~` 自身"的回环攻击）。

#### 2.1.3 路径归属判定

```ts
isAutoMemPath(absolutePath) {
  // 防御：normalize() 先处理 ../ 段
  return normalize(absolutePath).startsWith(getAutoMemPath())
}
```

> **解释**：用 `normalize()` 把 `absPath` 标准化为无 `..` 的形式，再判断是否以 auto memory 路径开头。这避免攻击者传入 `/memdir/../../etc/passwd` 来绕过"必须以 memdir 开头"的检查。

#### 2.1.4 KAIROS 模式（assistant 模式）的特例

```ts
getAutoMemDailyLogPath(date) {
  // 形如: <autoMemPath>/logs/YYYY/MM/YYYY-MM-DD.md
  // assistant 长会话：append-only 日志；/dream skill 每晚蒸馏
}
```

> **解释**：KAIROS 是 assistant 长会话模式（一次会话可以跨多天）。它不用"维护 MEMORY.md 索引"的方式（因为索引会越来越乱），而用按日期分片的追加日志：`<memdir>/logs/2026/06/2026-06-19.md`。`/dream` 命令每晚把日志蒸馏为长期记忆（合并/删除/归档），相当于"日志 + 周期整合"的存储模式。

Assistant 模式（KAIROS）用"日志式"追加而非"维护 MEMORY.md 索引"，因为会话"effective perpetual"。
> **解释**：effective perpetual = "实际上永久"——assistant 模式不主动结束，会话可以跨周、跨月。在这种场景下，MEMORY.md 索引会无限增长且难以去重；改用按日切片 + 定期蒸馏，既保留全部历史，又不让 prompt 上下文爆炸。

### 2.2 MEMORY.md 索引的双重保护

源码位置：[src/memdir/memdir.ts](../../ailearning/claude-code-analysis/src/memdir/memdir.ts)

```ts
export const MAX_ENTRYPOINT_LINES = 200
export const MAX_ENTRYPOINT_BYTES = 25_000  // ~125 chars/line at 200 lines
```

> **解释**：MEMORY.md 索引本身**也**会被注入到 system prompt，所以必须有硬上限。25KB ≈ 200 行 × 125 字符/行，是经验估算的平均行长。注释用 `~` 表示是估算值。

`truncateEntrypointContent` 双层截断：

1. 先按 200 行截
2. 再按 25KB 截（防"长行"绕过 line cap，注释里给了一个反例："p100 observed: 197KB under 200 lines"）
3. 截断后追加 **警告**，说明触发的是行限制还是字节限制

> **解释**：注释里的 "p100" 指生产环境 p99 分位观察——发现真实场景下有人用 JSON 单行 dump 一份大对象，200 行足以装下但接近 200KB，因此行数限制不够，必须再加字节数限制做兜底。

### 2.3 安全的目录创建

```ts
export async function ensureMemoryDirExists(memoryDir: string): Promise<void> {
  const fs = getFsImplementation()
  try {
    await fs.mkdir(memoryDir)  // mkdir recursive=true，swallows EEXIST
  } catch (e) {
    // 不抛 — 让模型直接 Write，FileWriteTool 会自己 mkdir
    // 这里只 log 给 --debug
  }
}
```

> **逐行解释**：
> - `getFsImplementation()`：根据运行环境返回 Node `fs` 或 Bun `fs` 的统一抽象（兼容 Node 与 Bun 运行时）。
> - `mkdir` 内部用了 `recursive: true`，所以多级目录一次性创建；`EEXIST`（目录已存在）被 swallow——不报错。
> - catch 里**不抛异常**：因为 LLM 在它之后会自己尝试 Write，而 `FileWriteTool` 自己也会做 parent mkdir。所以这里失败不是阻塞条件。

注释强调："Claude was burning turns on `ls`/`mkdir -p` before writing"——所以把目录创建 + "directory already exists" 提示词写死。
> **解释**："burning turns" = 浪费对话轮次。早期版本让 LLM 自己跑 `ls`/`mkdir`，模型会先 `ls` 看看目录在不在（浪费一轮），再 `mkdir -p`（再浪费一轮），再 Write。把这三步合并成"Write 时自动 mkdir，目录存在就 silently swallow"，大幅压缩 turn 数。

### 2.4 Frontmatter 格式示例

```markdown
---
name: {{memory name}}
description: {{one-line description — used to decide relevance in future conversations, so be specific}}
type: {{user, feedback, project, reference}}
---

{{memory content — for feedback/project types, structure as: rule/fact, then **Why:** and **How to apply:** lines}}
```

> **逐字段解释**：
> - `name`：文件的人类可读名字，用于在 MEMORY.md 索引里展示。
> - `description`：**关键字段**——注释明确说"用于未来对话决定相关性"，所以必须写得具体（不能用 generic 的"some user preferences"），否则召回阶段无法判断是否相关。
> - `type`：四类之一，必须严格匹配，否则统计/审计会出错。
> - `{{memory content}}` 占位提示了写作结构——feedback/project 类应包含 Why/How to apply，让未来对话能推理何时遵守。

`MEMORY.md` **没有 frontmatter**——纯索引：

```markdown
- [Title](file.md) — one-line hook
```

> **解释**：MEMORY.md 是"目录"而非"记忆"，所以不需要 frontmatter。每行格式 `- [Title](relative/path.md) — one-line hook`，其中 `hook` 是一句话说明这条记忆讲什么（对应 `findRelevantMemories` 召回时给 LLM 看的内容）。"one-line hook"是写时约束，避免索引本身变成长篇大论。

---

## 3. 写入路径：双通道 + 互斥

### 3.1 通道 A：主 Agent 主动写

主 agent 的 system prompt 在 `loadMemoryPrompt()` 中注入完整的 `## Types of memory` + `## How to save memories` 章节。模型在对话中如果识别到该存的，就会用 `Write` / `Edit` 工具直接写到 memdir。

> **解释**：这条路径是"主动写"——主 agent 在生成回复时如果识别到"用户偏好"、"项目背景"等该存的信号，就直接调 Write 工具落盘。优点是 latency 最低（不需要再开一个 agent），缺点是容易被对话主题分散注意力，偶尔漏存。

### 3.2 通道 B：后台 forked agent 兜底提取

源码位置：[src/services/extractMemories/extractMemories.ts](../../ailearning/claude-code-analysis/src/services/extractMemories/extractMemories.ts)

#### 3.2.1 时机

```ts
// 在 handleStopHooks 中与 confidence rating、prompt coaching 并列调用
// fire-and-forget — 不阻塞主对话
export async function executeExtractMemories(context, appendSystemMessage)
```

> **解释**：`handleStopHooks` 是 LLM 完成一轮回复（生成完 response + tool_use + tool_result）后、即将再次等待用户输入前的钩子。此时系统已经"空闲"，可以安全地启动后台任务而不抢占主对话。`fire-and-forget` 模式：调用方不等结果就继续，防止阻塞主交互。

#### 3.2.2 触发条件

```ts
async function executeExtractMemoriesImpl(context, appendSystemMessage) {
  if (context.toolUseContext.agentId) return         // 不在 subagent 里跑
  if (!isAutoMemoryEnabled()) return
  if (getIsRemoteMode()) return                      // 远端模式跳过
  if (inProgress) {                                 // 合并：如果上一次还在跑，stash 这一次的 context
    pendingContext = { context, appendSystemMessage }
    return
  }
  await runExtraction({ context, appendSystemMessage })
}
```

> **逐行解释**：
> - 第 1 行：`toolUseContext.agentId` 存在说明当前在 subagent 里。subagent 自己有自己的记忆模型，再触发主 agent 的提取会造成递归。直接 return。
> - 第 2 行：`isAutoMemoryEnabled()` 是 settings.json / env 开关；用户关了就不跑。
> - 第 3 行：远端模式（Claude.ai 网页端）有自己的记忆系统，跳过 CLI 的本地提取。
> - 第 4-7 行：**去重关键**——如果上一次提取还在跑，把这次的 context 暂存到 `pendingContext`，等当前跑完再处理 trailing run。这避免同时跑两个提取 agent 造成冲突。
> - 最后一行：实际启动提取。

**合并 trailing run**：如果短时间内多次 stop（罕见但可能），只有最新的那次会被处理，因为：
```ts
// in finally:
const trailing = pendingContext
pendingContext = undefined
if (trailing) {
  await runExtraction({ context: trailing.context, ..., isTrailingRun: true })
}
```

> **解释**：`finally` 保证无论提取成功还是异常都检查是否有 trailing context——避免"上次失败 → 这次的 context 被吞"。注意 `pendingContext = undefined` 要在读 `trailing` 之前做（原子赋值），否则并发场景下会被覆盖。

**trailing run 跳过节流门**：
```ts
if (!isTrailingRun) {
  turnsSinceLastExtraction++
  if (turnsSinceLastExtraction < getFeatureValue_CACHED_MAY_BE_STALE('tengu_bramble_lintel', null) ?? 1) {
    return
  }
}
turnsSinceLastExtraction = 0
```

> **逐行解释**：
> - `tengu_bramble_lintel`：GrowthBook feature flag 名（"tengu" 是 Claude Code 的内部代号，下同）；通过远程配置动态调整 N 轮节流门槛。
> - `?? 1`：feature flag 未设置时的默认值——1 轮，意味着"几乎每轮都跑"。
> - `_CACHED_MAY_BE_STALE` 后缀是提醒："这个缓存可能过期，请谨慎使用"——设计选择：宁可节流判断稍微不准，也不能在主流程 hot path 上做远程调用。
> - trailing run 跳过这个门槛——保证"被合并的最新一次 context"一定被处理，不会因为节流被吞。

#### 3.2.3 与主 Agent 互斥（关键）

```ts
function hasMemoryWritesSince(messages, sinceUuid) {
  // 如果主 agent 在 cursor 之后写过 memdir 内的文件
  // 跳过本次提取，但 cursor 仍前进 — 防止双写
}
```

注释解释："The main agent's prompt has full save instructions — when it writes memories, the forked extraction is redundant. runExtraction skips the agent and advances the cursor past this range, making the main agent and the background agent mutually exclusive per turn."

> **解释**：这是"互斥机制"的核心——`sinceUuid` 是上次提取处理到的消息游标。如果 `scanMemoryFiles` 检测到主 agent 在 `[sinceUuid, 最新消息]` 区间内写过 memdir 文件，就认为"主 agent 已经覆盖了本次提取该做的工作"，直接跳过本次提取，但仍推进游标，让下一轮重新评估。这避免了两个 agent 对同一段对话重复写。

#### 3.2.4 提取 agent 的工具权限

```ts
export function createAutoMemCanUseTool(memoryDir: string): CanUseToolFn {
  return async (tool, input) => {
    if (tool.name === REPL_TOOL_NAME) return allow           // REPL mode
    if ([FILE_READ, GREP, GLOB].includes(tool.name)) return allow
    if (tool.name === BASH) {
      if (tool.isReadOnly(input)) return allow                // 只读 shell
      return deny('Only read-only shell commands are permitted...')
    }
    if ([FILE_EDIT, FILE_WRITE].includes(tool.name)) {
      if (isAutoMemPath(input.file_path)) return allow
    }
    return deny('only Read/Grep/Glob, read-only Bash, and Edit/Write within <memoryDir> are allowed')
  }
}
```

> **逐条解释**：
> - **REPL mode**：允许通过 REPL 工具（嵌入式脚本）执行任意代码。这是高权限路径，但前提是 build 类型启用了 REPL（仅内部 build）。
> - **Read/Grep/Glob**：全部允许。提取 agent 需要读对话历史和项目上下文来判断"该不该存"。
> - **Bash**：仅允许 `isReadOnly(input) === true` 的命令。这是**语义判断**（由 BashTool 自己在工具描述里声明"该命令是只读"），不是简单字符串过滤。覆盖 `ls`、`cat`、`git log`、`grep` 等常见只读命令。
> - **Edit/Write**：仅 `isAutoMemPath(file_path) === true` 才允许。即只能写 memdir 内的文件——这是写权限沙箱的核心。
> - 最后的 `deny` 是兜底——任何不在白名单里的工具都拒绝。

**这是记忆系统的"权限沙箱"**：后台 agent 只能读所有文件，但只能在 memdir 内写。`tool.isReadOnly` 是个语义判断（不是简单 `>` / `<` 检查），由 BashTool 自己声明。
> **解释**：BashTool 在工具定义里声明 `isReadOnly`，由 LLM 在生成 `Bash` tool call 时根据命令语义判定（"这是只读的吗"）。这比"命令名黑名单"更灵活——可以识别 `git status` 这种命令名像 mutating 但实际只读的情况。

#### 3.2.5 优化：预注入 manifest

```ts
// 避免提取 agent 浪费一个 turn 跑 ls
const existingMemories = formatMemoryManifest(
  await scanMemoryFiles(memoryDir, abortSignal),
)
const userPrompt = buildExtractAutoOnlyPrompt(newMessageCount, existingMemories, skipIndex)
```

> **解释**：在 fork agent 之前，先用主进程**同步**扫一遍 memdir，把已有记忆列表格式化成 markdown 文本塞进 prompt。这样提取 agent 第一轮就能直接看到"已经存了什么"，不用自己再跑 `ls` 或 Glob——节省一个 turn。

`scanMemoryFiles` 只读 frontmatter 的前 30 行：
```ts
const FRONTMATTER_MAX_LINES = 30
const MAX_MEMORY_FILES = 200
```

> **解释**：
> - `FRONTMATTER_MAX_LINES = 30`：YAML frontmatter 通常 < 10 行，30 行留足余量。读取前 30 行就够拿到 name/description/type 三个字段，不用读完整文件。
> - `MAX_MEMORY_FILES = 200`：硬截断。极端情况下用户有上千条记忆，只取最近的 200 条——保证 prompt 大小可控。

按 mtime 倒序排序，截 200。这是**召回 agent 用的同一份扫描器**（DRY）。
> **解释**：`extractMemories`（写入路径）和 `findRelevantMemories`（读取路径）共用 `scanMemoryFiles`——同一份代码产出同一份 manifest，避免两边对"哪些文件存在"产生分歧。

#### 3.2.6 召回机制：findRelevantMemories

源码位置：[src/memdir/findRelevantMemories.ts](../../ailearning/claude-code-analysis/src/memdir/findRelevantMemories.ts)

```ts
export async function findRelevantMemories(
  query, memoryDir, signal, recentTools = [], alreadySurfaced = new Set()
): Promise<RelevantMemory[]>
```

> **逐参解释**：
> - `query`：当前用户的输入或对话上下文，用于相关性判断。
> - `memoryDir`：要扫描的记忆目录。
> - `signal`：AbortSignal——支持外部取消（比如主对话被 abort）。
> - `recentTools`：最近用过的工具列表，用于"不召回正在用的工具的 API 文档"。
> - `alreadySurfaced`：本会话已展示过的记忆文件路径集合（Set），用于去重避免重复注入。

流程：

```
1. scanMemoryFiles(memoryDir) → 按 mtime 倒序的 ≤200 个文件头
2. 过滤掉 alreadySurfaced（防止重复展示）
3. 用一个**专用 side query**（走独立模型调用，256 max_tokens，JSON schema 输出）让 Sonnet 选 ≤5 个最相关的
4. 返回 { path, mtimeMs }[] — mtime 透传给主模型
```

> **逐行解释**：
> - 步骤 1：复用 `scanMemoryFiles`，按 mtime 倒序（新的优先）。
> - 步骤 2：用 `alreadySurfaced` Set 做 O(1) 去重。
> - 步骤 3：调用 Sonnet 4.5 用 JSON schema 输出，max_tokens 仅 256（节省成本）；返回 ≤5 个 file path。
> - 步骤 4：返回完整对象，mtime 透传——主对话模型可以看到"这条记忆是多久前写的"，配合 `memoryFreshnessText` 给出 staleness 警告。

**关键：这是个独立的 sideQuery，不是同一轮 main agent 调用**。它不抢占主对话的 prompt cache，但消耗一次 API call。`SELECT_MEMORIES_SYSTEM_PROMPT` 严格限制：

```ts
// If a list of recently-used tools is provided, do not select memories that are
// usage reference or API documentation for those tools (Claude Code is already
// exercising them). DO still select memories containing warnings, gotchas, or
// known issues about those tools — active use is exactly when those matter.
```

> **逐句解释**：
> - "If a list of recently-used tools is provided, do not select memories that are usage reference or API documentation for those tools"——避免召回"用户已经在用的工具的用法文档"（用不到）。
> - "Claude Code is already exercising them"——主 agent 此刻就在用这些工具，不需要再注入用法说明。
> - "DO still select memories containing warnings, gotchas, or known issues"——**例外**——如果记忆包含"坑点/警告/已知问题"，**应该**召回，因为用户正要用到这些工具时最容易踩坑。
> - 注释关键洞察："active use is exactly when those matter"——主动使用工具时最需要"避坑提示"。

这条规则针对的失败模式是：用户在 query 里提到 "spawn"，memory 里也有 "spawn" → 关键词假阳。
> **解释**：纯关键词匹配会把"用户说 spawn，memory 里有 spawn 工具的 API 文档"当成强相关，召回之。但用户问的是"spawn 进程"而非"用 spawn 工具"，是误召。这条规则正是切断这类假阳。

**回退 / telemetry**：

```ts
if (feature('MEMORY_SHAPE_TELEMETRY')) {
  const { logMemoryRecallShape } = require('./memoryShapeTelemetry.js')
  logMemoryRecallShape(memories, selected)
}
```

> **解释**：feature flag 控制下，发送遥测事件记录"扫描到 N 条、选中 K 条"。注释解释了为什么需要区分 `-1`（跑了但没选中）和 `null`（从没跑过）——前者是"召回系统有效但没匹配上"，后者是"召回系统未启用"，两者含义完全不同，统计时要分开计。

"selection-rate needs the denominator, and -1 ages distinguish 'ran, picked nothing' from 'never ran'."
> **解释**：分母（扫描到的总数）必须上报，才能算出 selection rate（召回命中率）。`mtime = -1` 这个 sentinel 表示"跑过但没选中"，与"从未跑过"区分开。

#### 3.2.7 优雅退出

```ts
// drainer 在 print.ts 之后、gracefulShutdownSync 之前调用
// 5s shutdown failsafe 之前完成提取
export async function drainPendingExtraction(timeoutMs?: number): Promise<void> {
  await drainer(timeoutMs)
}
```

> **解释**：`drainPendingExtraction` 在进程关闭流程里被调用——位于"打印最终输出"之后、`gracefulShutdownSync`（硬关闭）之前。目的是给在飞的提取留 5-60 秒完成，避免"写到一半被 kill"导致记忆库损坏。

```ts
drainer = async (timeoutMs = 60_000) => {
  if (inFlightExtractions.size === 0) return
  await Promise.race([
    Promise.all(inFlightExtractions).catch(() => {}),
    new Promise<void>(r => setTimeout(r, timeoutMs).unref()),
  ])
}
```

> **逐行解释**：
> - `inFlightExtractions` 是 Set<Promise>，跟踪所有在飞的提取。
> - `Promise.race`：第一个 resolve/reject 就返回——要么全部提取完成，要么超时。
> - `.catch(() => {})`：忽略单个提取的失败（不让某个失败阻塞 drainer）。
> - `setTimeout(...).unref()`：让 timer 不阻止 Node 进程退出——如果提取已完成，timer 就让 Node 顺利退出，不用等满 60 秒。

注意 `unref()`——timer 不阻塞 exit。
> **解释**：Node.js 中活跃的 timer 会让进程保持运行。`unref()` 把 timer 标记为"不引用 event loop"，于是如果没有其他活跃句柄，进程可以立即退出，即使 timer 还没触发。这对 shutdown 场景至关重要——不能因为等待 timer 而延误进程退出。

---

## 4. SessionMemory：会话内压缩源

源码位置：[src/services/SessionMemory/sessionMemory.ts](../../ailearning/claude-code-analysis/src/services/SessionMemory/sessionMemory.ts)

### 4.1 目的

传统 `/compact` 调用 `/api/compact`（一个 LLM 调用）做总结。SessionMemory 是**预维护的滚动笔记**，压缩时直接当 summary 用，省一次 LLM 调用，保留最近对话原样。

> **解释**：核心思路是"用渐进式笔记替代瞬时总结"——传统 compact 是"超阈值后调用 LLM 一次性总结"，需要一次 API round-trip；而 SessionMemory 是"每次对话滚动维护一份笔记"，压缩时直接当 summary 复用，省一次 LLM 调用、降低压缩延迟。

### 4.2 触发门控

```ts
function shouldExtractMemory(messages) {
  if (!isSessionMemoryInitialized()) {
    if (!hasMetInitializationThreshold(currentTokenCount)) return false
    markSessionMemoryInitialized()
  }
  // 双门：tokens 增长 AND tool calls
  // 或：无 tool calls 在最后一轮 + tokens 增长
  const shouldExtract =
    (hasMetTokenThreshold && hasMetToolCallThreshold) ||
    (hasMetTokenThreshold && !hasToolCallsInLastTurn)
}
```

> **逐行解释**：
> - 顶部：检查是否已经初始化；未初始化则需要达到 `minimumMessageTokensToInit` 才标记为已初始化。
> - 注释 "双门" 指：必须同时满足 token 阈值和 tool call 阈值才提取。这避免"模型只输出文字没干活"时反复空提取。
> - 第二个条件 `(tokens && !hasToolCallsInLastTurn)`：如果上一轮没工具调用（纯文本回复），但 token 增长也够了，也允许提取。这是兜底——纯对话场景不应被遗忘。
> - "tokens AND tool calls" 的双重要求是经过 A/B 测试调出来的——单看 tokens 太频繁（对话一直在增），单看 tool calls 又会漏掉纯对话。

注释强调："The token threshold (minimumTokensBetweenUpdate) is ALWAYS required. Even if the tool call threshold is met, extraction won't happen until the token threshold is also satisfied."

> **解释**：注释特别强调 token 阈值是**硬性最低门槛**。即便 tool call 频繁触发，没有足够的 token 增量就不提取——避免"提取 agent 抽取几乎相同的内容"造成冗余 IO 和 API 浪费。

### 4.3 阈值

源码位置：[src/services/SessionMemory/sessionMemoryUtils.ts](../../ailearning/claude-code-analysis/src/services/SessionMemory/sessionMemoryUtils.ts)

```ts
DEFAULT_SESSION_MEMORY_CONFIG = {
  minimumMessageTokensToInit: 10000,  // 初始化门槛
  minimumTokensBetweenUpdate: 5000,   // 两次提取之间至少 5K token 增长
  toolCallsBetweenUpdates: 3,
}
```

> **逐项解释**：
> - `minimumMessageTokensToInit: 10000`——会话累计超过 10K token 才"启用" SessionMemory。会话太短时不需要摘要。
> - `minimumTokensBetweenUpdate: 5000`——两次提取之间至少 5K token 增长。低于此值认为"内容变化不够大"，跳过。
> - `toolCallsBetweenUpdates: 3`——同时累计 ≥3 个 tool call 才触发，保证"真的有干活"。

可被 GrowthBook 远程配置 `tengu_sm_config` 覆盖。
> **解释**：GrowthBook 是一个 A/B 测试与 feature flag 平台。`tengu_sm_config` 是 Claude Code 内部的远程配置项，可以动态调整这些阈值做实验（例如把 10000 改成 5000 看是否影响压缩质量）。

### 4.4 文件创建

```ts
async function setupSessionMemoryFile(toolUseContext) {
  // 权限：0o700 目录、0o600 文件
  await fs.mkdir(sessionMemoryDir, { mode: 0o700 })
  try {
    await writeFile(memoryPath, '', { mode: 0o600, flag: 'wx' })  // O_CREAT|O_EXCL
    const template = await loadSessionMemoryTemplate()
    await writeFile(memoryPath, template, { mode: 0o600 })
  } catch (e) {
    if (getErrnoCode(e) !== 'EEXIST') throw e
  }
  // 关键：清缓存！否则 FileReadTool 的 dedup 会返回 file_unchanged stub
  toolUseContext.readFileState.delete(memoryPath)
  const result = await FileReadTool.call({ file_path: memoryPath }, toolUseContext)
  ...
}
```

> **逐行解释**：
> - `mkdir mode: 0o700`：目录权限 `rwx------`——仅 owner 可读写执行。
> - `writeFile flag: 'wx'`：`wx` = `O_CREAT | O_EXCL`，只在文件不存在时创建。如果已存在抛 `EEXIST`。
> - 第一次写空字符串（创建文件），再写 template 内容。
> - catch 里只 swallow `EEXIST`，其他错误抛——避免静默失败。
> - **关键**：`toolUseContext.readFileState.delete(memoryPath)`——清缓存。FileReadTool 内部用 readFileState 缓存"已读过的文件状态"，如果不清，新建的 memory 文件会被认为"和上次一样"，返回 stub。
> - 最后用 FileReadTool 显式调用一次——把新文件注入主对话上下文，让后续对话"看到" SessionMemory 的存在。

### 4.5 Forked agent 提取

```ts
await runForkedAgent({
  promptMessages: [createUserMessage({ content: userPrompt })],
  cacheSafeParams: createCacheSafeParams(context),
  canUseTool: createMemoryFileCanUseTool(memoryPath),  // 只允许 Edit 这一文件
  querySource: 'session_memory',
  forkLabel: 'session_memory',
  overrides: { readFileState: setupContext.readFileState },
})
```

> **逐参解释**：
> - `promptMessages`：给 forked agent 的初始消息（系统会自动加 system prompt）。
> - `cacheSafeParams`：与主对话共享 prompt cache 的安全参数——避免子 agent 调用破坏主 prompt 的缓存命中。
> - `canUseTool`：权限钩子——SessionMemory 用最严格的"只能 Edit 这个具体文件"。
> - `querySource: 'session_memory'`：用于遥测分类。
> - `forkLabel: 'session_memory'`：fork label，用于日志/debug 区分。
> - `overrides.readFileState`：子 agent 共享读状态缓存。

`createMemoryFileCanUseTool` 是最严格的"工具权限"——只能 Edit 这一个文件：

```ts
return async (tool, input) => {
  if (tool.name === FILE_EDIT && input.file_path === memoryPath) return allow
  return deny('only Edit on <memoryPath> is allowed')
}
```

> **解释**：返回的函数严格匹配 `FILE_EDIT` 工具 + 精确路径 `memoryPath`。任何其他工具（Read、Write、Bash、Grep）都被拒绝，连 `FILE_WRITE`（创建新文件）都不允许——避免 agent "另存一份"绕开审计。这是最严格的沙箱。

### 4.6 用 SessionMemory 做压缩

源码位置：[src/services/compact/sessionMemoryCompact.ts](../../ailearning/claude-code-analysis/src/services/compact/sessionMemoryCompact.ts)

```ts
export async function trySessionMemoryCompaction(messages, agentId?, autoCompactThreshold?)
```

> **解释**：函数返回压缩后的消息数组；如果 SessionMemory 不可用，返回 `null`，调用方回退到传统 `/compact`。`autoCompactThreshold` 是触发自动压缩的 token 阈值（外部传入）。

#### 4.6.1 决定哪些消息保留

```ts
const lastSummarizedMessageId = getLastSummarizedMessageId()
let lastSummarizedIndex = messages.findIndex(m => m.uuid === lastSummarizedMessageId)
// 从 lastSummarizedIndex + 1 开始，向后扩展：
//   - 至少保留 config.minTokens (10K) token
//   - 至少保留 config.minTextBlockMessages (5) 条带 text block 的消息
//   - 但不超过 config.maxTokens (40K)
const startIndex = calculateMessagesToKeepIndex(messages, lastSummarizedIndex)
```

> **核心模型：保留窗口 = `[startIndex, messages.length - 1]`**
>
> 压缩后留下的消息是"从某个起点到最新一条"——**窗口终点永远是 `messages.length - 1`（最新消息）**。`calculateMessagesToKeepIndex` 的工作就是算出 `startIndex`，让窗口满足三条约束。

**逐行解释**：

- `lastSummarizedMessageId`：上一次 SessionMemory 提取处理到的最后一条消息 uuid。这条**及之前**的消息已经被摘要进 SessionMemory 文件，压缩时可以直接丢掉。
- `findIndex`：找到这个 uuid 在 `messages[]` 数组里的下标。
- `startIndex` 由 `calculateMessagesToKeepIndex` 算出，受三条约束联合决定（见下方三种情况）。

**关键不变量**：

| 不变量 | 含义 |
| --- | --- |
| 最新消息永远在窗口内 | 窗口终点固定为 `messages.length - 1` |
| 不重复已摘要内容 | `startIndex >= lastSummarizedIndex + 1` |
| 不让上下文爆掉 | 窗口总 token ≤ `maxTokens (40K)` |

**三种典型情况**：

```
情况 1：未摘要区 < 40K（典型，几乎所有情况）

  [0 ... 50]                      [51 ... 200]
  ─────────────────────────       ─────────────────────────
  已被摘要进 SessionMemory          未摘要：15K tokens, 12 条 text
  
  ↑ lastSummarizedIndex = 50       startIndex = 51
  
  startIndex = lastSummarizedIndex + 1 = 51
  保留窗口 = [51 .. 200]   ← 全部保留，最新消息 200 在窗口内
```

```
情况 2：未摘要区 > 40K（异常长会话，提取 agent 长期没跑）

  [0 ... 50]      [51 ... 319]      [320 ... 800]
  ───────────     ─────────────     ─────────────
  已摘要           未摘要但超长       未摘要：约 40K
                  51K tokens
                  
                  ↑ ↑              ↑
                被丢弃（最老       startIndex = 320
                的未摘要部分）
                
  保留窗口 = [320 .. 800]   ← 最新消息 800 仍在
  丢的是"最老的未摘要消息"，最新消息始终保留
```

```
情况 3：未摘要区极短（刚初始化 SM）

  [0 ... 195]      [196 ... 200]
  ─────────────    ──────────────
  已摘要            未摘要：8K tokens, 3 条 text
  
  窗口 = [196 .. 200]   ← 8K < minTokens(10K), 3 < minTextBlockMessages(5)
  约束"放宽"：保留窗口里就这么多，没法扩展（前面已摘要）
  不能回到 lastSummarizedIndex 之前，那会重复摘要
```

**对应三条源码注释的语义**：

- `minTokens (10K)` —— **sanity floor**："保证模型至少有 10K token 的最近上下文"。如果未摘要区本身就 < 10K（如情况 3），约束自动放宽。
- `minTextBlockMessages (5)` —— **sanity floor**："保证窗口里至少有 5 条 text-block 消息"。纯 tool call 串不够"对话感"，需要一些文本块。
- `maxTokens (40K)` —— **cap**："未摘要区膨胀到 40K 以上时，从窗口最老端开始丢"。这是唯一会丢消息的情况，触发条件是 SessionMemory 提取 agent 长期没跑（被节流 / 失败 / 排队中），让未摘要区突破上限。遇到这种情况，4.6.4 里的 `autoCompactThreshold` 检查会让 SM-compact **主动放弃**并回退到传统 `/compact`（调 LLM 一次性总结），不会无限重试。

**结论**：典型情况下（未摘要区 < 40K），`startIndex = lastSummarizedIndex + 1`，**所有未摘要的消息都保留**，**最新消息永远在窗口内**。即使发生情况 2 的窗口截断，丢的也是"最老的未摘要消息"，最新消息始终不受影响。

#### 4.6.2 关键的 API 不变量保护

```ts
export function adjustIndexToPreserveAPIInvariants(messages, startIndex) {
  // 1. tool_use / tool_result 不能被切开
  // 2. thinking blocks 与后续同 message.id 的 tool_use 块不能分开
}
```

注释里给了**两个反例场景**（非常具体，体现对 API 行为的深入理解）：

**Tool pair 场景**：
```
[N]   assistant, message.id: X, content: [thinking]
[N+1] assistant, message.id: X, content: [tool_use: ORPHAN_ID]
[N+2] assistant, message.id: X, content: [tool_use: VALID_ID]
[N+3] user,        content: [tool_result: ORPHAN_ID, tool_result: VALID_ID]
```
如果 startIndex = N+2 → 旧代码只查 N+2 的 tool_results（空）→ 切片后 ORPHAN tool_use 被排掉，但 ORPHAN tool_result 还在 → API 报 orphan 错误。

> **解释**：API 要求每个 `tool_use` 必须有对应的 `tool_result` 配对。如果一刀切把 N+1（含 ORPHAN tool_use）切掉，但 N+3（含 ORPHAN tool_result）保留，API 会报错"orphan tool_result"。`adjustIndexToPreserveAPIInvariants` 必须把 startIndex **往前移**到包含 ORPHAN tool_use 的位置（即 N+1），保证 pair 完整。

**Thinking 块场景**：同一 message.id 的流式输出若被切开，thinking 块会丢失。
> **解释**：Anthropic API 中，assistant 的 thinking（推理链）和 tool_use 是同一 message 的不同 content block。如果切掉前面的消息只留 tool_use，thinking 块就丢了——而 Anthropic API 会因为"前面没有 thinking 上下文"拒绝或行为异常。这条不变量保证"同一 message.id 的 blocks 不能被切开"。

#### 4.6.3 边界处理

```ts
const idx = messages.findLastIndex(m => isCompactBoundaryMessage(m))
const floor = idx === -1 ? 0 : idx + 1
for (let i = startIndex - 1; i >= floor; i--) { ... }
```

> **逐行解释**：
> - `isCompactBoundaryMessage(m)`：识别"已经被之前 compact 处理过的消息"——这类消息带有"compact 边界标记"。
> - `findLastIndex`：找到**最后一个**边界消息的位置。
> - `floor`：边界消息**之后**的位置（不能跨过边界继续往前吃）。
> - for 循环：从 startIndex-1 向前试探（不能切坏 API 不变量），但不能越过 floor。

注释："Reactive compact already slices at the boundary via getMessagesAfterCompactBoundary; this is the same invariant."——压缩不能跨过已有的 compact boundary，否则 preserved-segment 链就乱了。
> **解释**：reactive compact 是另一种自动压缩机制（在每轮 turn 结束时自动 compact）。它和 SM-compact 都遵循同一不变量："不能跨越已有的 compact boundary"——否则保留段的 UUID 引用链会断裂，下次 compact 时找不回上次的"分界点"。

#### 4.6.4 与传统 /compact 的关系

```ts
if (!shouldUseSessionMemoryCompaction()) return null  // 回退到传统

// 等待在跑的提取
await waitForSessionMemoryExtraction()  // 最多 15s

const sessionMemory = await getSessionMemoryContent()
if (!sessionMemory) return null                        // 还没建立 → 传统
if (await isSessionMemoryEmpty(sessionMemory)) return null  // 是模板 → 传统
```

> **逐行解释**：
> - 第 1 行：feature flag 控制——用户关了 SM-compact（env 或 settings）就回退到传统。
> - 第 2 行：如果提取 agent 正在跑，最多等 15s——避免"压缩时文件还没更新"。超时则跳过。
> - 第 4 行：如果 SessionMemory 文件不存在（`getSessionMemoryContent()` 返回 null），用不上，回退。
> - 第 5 行：如果是空模板（只有 header 没正文），摘要意义不大，回退。

**Threshold 检查**：

```ts
if (autoCompactThreshold !== undefined && postCompactTokenCount >= autoCompactThreshold) {
  return null
}
```

如果 SM-compact 之后的 token 数仍然 >= 阈值，放弃——避免无限重试。

### 4.7 SessionMemory 的真实价值：成本、收益与 prompt 对比

> 这一节专门回应一个常见的疑问：**SessionMemory 不就是给 compact 做缓存吗？它真正的收益在哪里？**

#### 4.7.1 一句话回答

SessionMemory 的核心价值是**用「渐进式后台笔记」替代「阻塞式 LLM 一次性总结」**——压缩时**省一次 LLM round-trip**，同时**保留最近对话的原文**而非二手 summary。表面上看起来"提取 agent 跑了多次"，但**总成本远低于一次大 compact**。

#### 4.7.2 两条压缩路径的对比

**传统 `/compact`**（[src/services/compact/compact.ts](../../ailearning/claude-code-analysis/src/services/compact/compact.ts)）：

```
触发：context 接近上限（e.g. 80% / autoCompactThreshold）
   ↓
stripImages + stripReinjectedAttachments（剥离图片/附件）
   ↓
runForkedAgent(querySource: 'compact', forkLabel: 'compact')
   ↓  ← 这里调一次 LLM，输入可能 100K+ tokens
LLM 生成 9 段 summary（Primary Request / Files / Errors / All user messages / Current Work ...）
   ↓
summary 替换 [0..boundary] 全部消息
recent messages 不保留原文（除非 partial compact）
   ↓
用户看到 "compacting..." status 5~15 秒
```

**SessionMemory-compact**（[src/services/compact/sessionMemoryCompact.ts](../../ailearning/claude-code-analysis/src/services/compact/sessionMemoryCompact.ts)）：

```
后台滚动（每个 ~5K tokens 增长触发）：
  runForkedAgent(querySource: 'session_memory')
  forked agent 用 Edit 更新 SessionMemory 文件
  模板结构保留，只更新正文
   ↓
压缩触发时：
  trySessionMemoryCompaction()
   ↓
读 SM 文件 → 作为 summary（0 LLM 调用）
保留 [lastSummarizedIndex+1 .. end] 原文
   ↓
用户无感（无 "compacting..." 等待）
```

#### 4.7.3 真实成本对比（粗略估算）

假设一个 200K tokens 的长 session，传统 compact 触发一次：

| 维度 | 传统 compact | SM 路径 |
| --- | --- | --- |
| 压缩时的 LLM 调用 | **1 次**（100K+ tokens 输入） | **0 次** |
| 后台小调用 | 0 次 | 8~15 次（每 5K tokens 增长 + 3 tool calls） |
| 每次小调用输入 | — | 约 10K tokens（增量对话 + 当前 SM 文件） |
| 每次小调用输出 | — | 约 2K tokens（更新几个 section） |
| 总输入 tokens | 100K+ | 80~150K（多次小调用） |
| 总输出 tokens | 5K | 16~30K |
| **cache 命中率** | 差（输入大、cache miss 严重） | **高**（小调用、相同 system prompt + 渐变 SM 文件） |
| **是否阻塞主对话** | **是**（5~15 秒） | **否**（后台跑） |
| **最近消息原文** | 否（被总结） | **是**（保留 [lastSummarizedIndex+1, end]） |

**关键观察**：

1. **总 token 消耗**：SM 路径的输入 token 看似更多（80~150K vs 100K+），但因为是**多次小调用、cache 命中率高**，实际计费 tokens 更少。一次 100K 输入的传统 compact 因 cache miss，可能完全按 fresh tokens 计费。

2. **延迟收益最大**：传统 compact 阻塞主对话——用户提交 prompt 后要等 5~15 秒才能看到结果。SM extraction 全在后台跑（forked agent + post-sampling hook），**用户完全无感**。

3. **保真度收益**：传统 compact 后，最近 5~10 轮对话被 LLM 总结成文字——file paths / error messages / 代码片段都经过 LLM 二次加工。SM-compact 后，最近的对话**原文保留**，LLM 直接看到原始的 tool_use/tool_result。

#### 4.7.4 Prompt 对比：为什么 SM 不是"小一号"的传统 compact

很多读者会以为 SM 提取 prompt 就是传统 compact prompt 的简化版。**实际上完全不同**：

**传统 compact prompt**（[src/services/compact/prompt.ts:62-143](../../ailearning/claude-code-analysis/src/services/compact/prompt.ts#L62-L143)）：

```
NO_TOOLS_PREAMBLE：拒绝任何工具调用（一次机会，浪费了就 fallback）
+ BASE_COMPACT_PROMPT：9 段叙事性总结
  1. Primary Request and Intent
  2. Key Technical Concepts
  3. Files and Code Sections        ← 文件信息
  4. Errors and fixes
  5. Problem Solving
  6. All user messages              ← 全部 user 消息（占大量 tokens）
  7. Pending Tasks
  8. Current Work
  9. Optional Next Step
+ NO_TOOLS_TRAILER
```

**关键特征**：
- 输出**自由文本**，一次成型
- 包含 "All user messages"——把所有用户消息原文塞进 summary
- 强调 "thorough, capture all technical details"
- 一次性生成，不可增量更新

**SM update prompt**（[src/services/SessionMemory/prompts.ts:43-80](../../ailearning/claude-code-analysis/src/services/SessionMemory/prompts.ts#L43-L80)）：

```
1. Read the file <current_notes_content>
2. Use Edit tool to update sections
3. STRICT: preserve section headers + italic descriptions
4. ONLY update content below descriptions
5. Make multiple Edit calls in parallel, then stop
```

**关键特征**：
- **结构化模板固定**（10 个 section），LLM 只能用 Edit 改 section 内的正文
- **增量更新**：section header + italic description 是**模板指令**，永远不能改
- 强调 "info-dense, no filler"——不要写 "No info yet" 这种空话
- **可以跨多次压缩累积**：SM 文件是单一来源，多次压缩都基于它
- **section 限额**：每 section ≤ 2000 tokens，总文件 ≤ 12000 tokens

**SM 模板**（[prompts.ts:11-41](../../ailearning/claude-code-analysis/src/services/SessionMemory/prompts.ts#L11-L41)）：

```markdown
# Session Title              # 5-10 字标题
# Current State              # 当前活跃工作
# Task specification         # 用户原始需求
# Files and Functions        # 关键文件
# Workflow                   # 通常运行的 bash 命令
# Errors & Corrections       # 错误与修复（含用户纠错）
# Codebase and System Documentation
# Learnings                  # 什么有效，什么无效
# Key results                # 用户的具体输出（表格、答案）
# Worklog                    # 步骤日志
```

#### 4.7.5 为什么 SM extraction 跑多次反而更划算？

用户的疑问："SM-compact 可能触发多次 SM extraction，是不是浪费？"

**答案：不浪费，反而更便宜**。三个原因：

1. **每次小调用 cache 命中率高**：
   - 传统 compact 输入 100K+ tokens，几乎必然 cache miss
   - SM extraction 输入 ~10K tokens（增量 + 当前 SM），cache 命中率高
   - Anthropic API 对 cache hit 部分按 10% 价格计费
   - 8 次小调用 + 高 cache 命中 < 1 次大调用

2. **后台不阻塞主对话**：
   - 传统 compact：用户在主对话里等 5~15 秒（"compacting..." 状态）
   - SM extraction：forked agent 在后台跑，用户**完全无感**
   - "等待时间"也是一种成本——SM 路径彻底省掉

3. **增量更新比一次性总结更准确**：
   - 传统 compact：LLM 一次性总结整个对话，容易遗漏
   - SM extraction：每次只关注"上次以来新发生的事"，注意力集中
   - 多个 section 各自维护最新状态，不会"summary 偏向最近对话"的问题

#### 4.7.6 关键不变量：SM-compact 不是万能的

代码里 [sessionMemoryCompact.ts:514-630](../../ailearning/claude-code-analysis/src/services/compact/sessionMemoryCompact.ts#L514-L630) 显式列出回退条件：

```typescript
// 1. feature gate 未开启
if (!shouldUseSessionMemoryCompaction()) return null

// 2. 没有 SessionMemory 文件（feature 启用但未达到 10K 初始化门槛）
if (!sessionMemory) return null

// 3. SM 文件存在但内容是模板（没真实提取过）
if (await isSessionMemoryEmpty(sessionMemory)) return null

// 4. SM extraction 正在跑且 15s 超时
await waitForSessionMemoryExtraction()  // 最多 15s

// 5. SM-compact 后 token 数仍 >= autoCompactThreshold（避免无限重试）
if (postCompactTokenCount >= autoCompactThreshold) return null

// 6. SM 文件 ID 在当前 messages 中找不到（异常情况）
if (lastSummarizedIndex === -1) return null
```

**回退设计哲学**：SM-compact 是个**优化路径**，不是替代品。当 SessionMemory 还没建立、跑挂了、或压缩不充分时，无缝回退到传统 compact，**用户完全感知不到差异**。

#### 4.7.7 SM 文件的额外价值：跨压缩累积 + 可观测性

**跨压缩累积**（传统 compact 没有）：

```
第 1 次 compact：SM 文件 = "User is debugging X" → summary
   ↓
第 2 次 compact：SM 文件 = "User was debugging X, found Y, now working on Z" → summary
   ↓
第 3 次 compact：SM 文件 = "User debugged X→Y→Z, currently designing W" → summary
```

传统 compact 每次都是 single-shot summary——上次 compact 的 summary 不会被新 summary 引用（实际上被丢弃）。SM 文件是**持续累积**的，**每条信息都不会丢**。

**可观测性**（传统 compact 没有）：

```typescript
// 用户可以读：
// ~/.claude/projects/<project>/session-memory/notes.md

// SessionMemoryCompact.ts:472 在 summary 里告知用户
if (wasTruncated) {
  summaryContent += `\n\nSome session memory sections were truncated for length. The full session memory can be viewed at: ${memoryPath}`
}
```

用户能直接看 SM 文件的内容、知道 summary 截断了哪些信息、必要时手动编辑。这是透明性设计。

#### 4.7.8 总结：SM-compact 的真正收益

| 收益维度 | 传统 compact | SM-compact | 量化 |
| --- | --- | --- | --- |
| **压缩延迟** | 5~15 秒（阻塞主对话） | < 1 秒（读文件） | **~10x 提速** |
| **LLM 调用次数** | 1 次大调用 | 0 次（提取在后台） | 压缩时**省 1 次** |
| **cache 命中率** | 低（大输入 cache miss） | 高（小调用 + 共享 system prompt） | 计费 tokens 减少 |
| **最近消息保真** | 被总结 | **保留原文** | 不丢细节 |
| **跨压缩累积** | 每次 single-shot | SM 文件持续累积 | 信息不丢 |
| **用户可观测** | 不可见 | SM 文件可读可编辑 | 透明性 |

**核心公式**：

```
SM 路径总成本 = N × 小 SM extraction（后台，cache 友好）
              + 1 × 0 LLM compact（即时）

传统路径总成本 = 0（无后台）
             + 1 × 大 compact（阻塞，cache miss）
```

当会话足够长（≥ 50K tokens）时，SM 路径的总成本 + 用户体验都优于传统路径。**这就是为什么 Claude Code 默认开启 SM（feature gate `tengu_session_memory`）**。
> **解释**：SM-compact 后**已经**把"上次摘要之后的所有消息"保留了。即便如此 token 数仍超阈值，说明上下文就是真的长（不是 SessionMemory 的问题）。这种情况下不能反复重试 SM-compact，应该回退到传统 compact 调用 LLM 二次摘要。

#### 4.7.9 SM-compact 是"增量"还是"重新生成"？

**答案：增量。SM 文件永不被重新生成。**

每次 SM extraction（fork agent 在后台跑）拿到的输入是：

```text
[当前 SM 文件完整内容]   ← 始终是上一次 extraction 的成果（含历史累积）
+ [lastSummarizedMessageId 之后的新消息]   ← 本次新增
```

fork agent 收到的 system prompt（`prompts.ts:43-80`）里**严格禁止**修改 section header：

```typescript
CRITICAL: Do NOT modify, remove, or add to the section headers
(## Session Goal, ## Progress, ...). 
Only edit content UNDER each header.
- You MUST answer "true" to the permission prompt for the Edit tool
  when it asks to edit session memory.
```

fork agent 只能 **Edit** 现有 section 下的正文，**不能**重写整个文件、不能调整 section 顺序、不能新增/删除 section。

> **解释**：这是"增量更新"的关键证据——LLM 被约束为"在保留 schema 的前提下改正文"。即便 LLM 写出来一些错误（比如漏掉了重要信息），下次 extraction 也会从"被污染的当前 SM 文件"出发继续增量修补，而不是 reset。

**对应到 messages 数组的丢弃**（`sessionMemoryCompact.ts:572`）：

```typescript
const keptMessages = messages.slice(startIndex)  // 丢弃 lastSummarizedIndex 之前的
```

`startIndex` 是 `lastSummarizedMessageId` 在 messages 数组中的位置。**被丢弃的消息的"信息"**并没有丢——它们已经在 SM 文件中以更高密度的形式保留了。**真正丢的是"原文措辞"**（每次 compact 本来就会丢）。

> **解释**：用 git 来类比——SM 文件是"压缩后的 commit 历史"（不可变 + 累积），messages 数组是"工作区 + 暂存区"（每次 compact 会 reset 到 startIndex 之后）。`lastSummarizedMessageId` 就是"HEAD 指针"，指向 messages 中"还没被 SM 摘要"的第一条。

**反例：为什么传统 compact 是"重新生成"？**

传统 compact 把"已压缩的消息"（含上一次的 summary 消息）整体作为 LLM 输入，**让 LLM 重新生成一份新 summary**。这份新 summary 会替换旧 summary，旧 summary 的措辞、细节、格式选择都会变化（甚至丢失）。这就是为什么传统 compact 是 **single-shot、不可累积**。

#### 4.7.10 传统 compact 与 SM 的互斥关系

两条压缩路径**完全独立、互不读取**：

| 维度 | SM-compact | 传统 compact |
| --- | --- | --- |
| 读 SM 文件？ | ✅（作为 summary 内容） | ❌（zero grep matches） |
| 调 LLM？ | ❌ | ✅ |
| 改 SM 文件？ | ❌（只读） | ❌（不感知） |
| 改 messages？ | ✅ `messages.slice(startIndex)` | ✅ 完整替换 |
| 改 `lastSummarizedMessageId`？ | ❌（保持指向文件最后覆盖的消息） | ✅ `setLastSummarizedMessageId(undefined)` |

**关键证据 1：传统 compact 不读 SM 文件**

```bash
$ grep -r "sessionMemory\|SessionMemory" src/services/compact/compact.ts
# 无任何匹配
```

传统 compact 走的是 `compactConversation()` 路径，把原始 messages 全部塞给 LLM 重新摘要。它**完全不知道 SM 文件存在**。

**关键证据 2：传统 compact 之后会重置 SM 跟踪**

源码位置：[src/commands/compact/compact.ts:110-112](../../ailearning/claude-code-analysis/src/commands/compact/compact.ts#L110-L112)

```typescript
// 传统 compact 完成后
setLastSummarizedMessageId(undefined)
```

为什么重置？因为传统 compact 的 summary 已经"覆盖"了 SM 文件的所有信息（甚至比 SM 更精简），如果继续保留 `lastSummarizedMessageId`，下次 SM-compact 会用 SM 文件去拼 summary，但 SM 文件的信息已经被传统 compact 的 summary 包含了——重复且浪费。重置后，**下次 SM extraction 就会从"传统 compact summary 之后"开始重新累积 SM 文件**。

> **解释**：用户手动 `/compact` 时，dispatch 流程会先尝试 SM-compact（`compact.ts:54-83`），失败才走传统 compact。所以传统 compact 是"plan B"，每次触发都会**破坏** SM 文件与 messages 的同步关系（通过重置 ID）。下一次 SM extraction 之前，SM 文件其实已经"过期"了。

**两次 SM-compact vs 一次传统 compact 的"成本账"**：

| 场景 | 累计 LLM 调用 | 累计延迟 | SM 文件状态 |
| --- | --- | --- | --- |
| 5 次 SM-compact（中间穿插） | 5× 小（后台） | 5× ~2s（不可见） | 持续累积 |
| 1 次传统 compact | 1× 大（阻塞） | 1× ~10s（可见） | 重置 |

虽然 SM 路径的 LLM 调用更多（5 次 vs 1 次），但**总成本更低**（小调用 cache 命中高）且**延迟更分散**（后台跑不影响用户交互）。

#### 4.7.11 SM-compact 时 SM 文件的保留与截断

SM-compact 不会"丢"SM 文件的内容——它**会按 section 截断**，以保证 summary 不会太长。

源码位置：[src/services/SessionMemory/prompts.ts:256-296](../../ailearning/claude-code-analysis/src/services/SessionMemory/prompts.ts#L256-L296)

```typescript
function truncateSessionMemoryForCompact(memory: string): string {
  // 解析 10 个 section
  const sections = parseSections(memory)
  
  // 每个 section 单独截断
  return sections.map(s => 
    s.content.length > 2000 * 4  // 2000 tokens ≈ 8000 chars
      ? s.content.slice(0, 8000) + '\n\n[... truncated for brevity ...]'
      : s.content
  ).join('\n\n')
}
```

**截断规则**：

| 维度 | 限制 | 来源 |
| --- | --- | --- |
| 单 section | 2000 tokens（8000 chars） | `truncateSessionMemoryForCompact` |
| 总文件 | 软上限 12000 tokens | sessionMemory.ts warning |
| 截断提示 | 附加 `[... truncated for brevity ...]` | prompts.ts:280 |
| 用户告知 | summary 末尾写明"SM 文件被截断" | sessionMemoryCompact.ts:472 |

**总文件大小没有硬上限**——如果所有 10 个 section 都堆到 2000 tokens，理论最大 20000 tokens。但 prompts 里有 warning 提醒："Don't let any single section grow too large. If approaching 2000 tokens, condense older information into a more compact form."

> **解释**：这就是"soft cap"——不强制截断，但提示 LLM 自己主动压缩。如果 LLM 不压缩，超过 12000 tokens 会在 UI 显示警告但仍能工作。**真正的硬限制在 post-compact 检查**（`sessionMemoryCompact.ts:605-614`）：如果 `truncatedMemory` 拼到 messages 里**仍然超 autoCompactThreshold**，SM-compact 放弃、回退传统 compact。

**SM 文件不会被删除/重置**——即使某些 section 被截断、即使整体超 12K，**文件始终保留在磁盘上**。下次 SM extraction 会**从磁盘读完整文件**作为输入（不读截断后的内容），所以截断是"展示层"操作，不影响后台累积。

#### 4.7.12 SM-compact 不调 LLM 的关键证据

**`trySessionMemoryCompaction` 是纯本地操作，零 LLM 调用。**

源码位置：[src/services/compact/sessionMemoryCompact.ts:461-475](../../ailearning/claude-code-analysis/src/services/compact/sessionMemoryCompact.ts#L461-L475)

```typescript
function createCompactionResultFromSessionMemory(
  sessionMemory: string,
  // ... 其他参数
) {
  // 1. 截断 SM 文件
  const truncatedMemory = truncateSessionMemoryForCompact(sessionMemory)
  
  // 2. 直接拼装 summary 消息
  const summaryContent = `The following session memory has been condensed 
  for context efficiency. The full session memory file is available at: 
  ${memoryPath}\n\n${truncatedMemory}`
  
  // 3. 返回 CompactionResult，零 LLM 调用
  return {
    summary: summaryContent,
    compactableMessages: messagesToCompact,
    // ... 不包含 usage / stop_reason
  }
}
```

**对比传统 compact**（`compact.ts`）的同步调用：

```typescript
async function compactConversation(messages) {
  const response = await client.messages.create({
    model: 'claude-sonnet-4-6',
    messages: [{ role: 'user', content: buildSummaryPrompt(messages) }],
    max_tokens: 4000,
  })
  // 阻塞等待 5~15s，消耗 LLM 配额
}
```

> **解释**：SM-compact 的延迟只有"读文件 + 字符串拼接 + parse sections"，**整个过程在毫秒级完成**。这是为什么 SM-compact 几乎"无感"——用户感觉不到压缩发生。

**唯一的 LLM 调用：SM extraction（在后台）**

`extractMemories`（`sessionMemory.ts:310-325`）会 `runForkedAgent` 异步调用 LLM，让它更新 SM 文件。这个调用：

- 在**后台**跑（不阻塞主对话）
- 用 **fork agent**（独立 model + 独立 context）
- 处理的是**已经压缩过的**消息窗口（不是完整 messages）
- **cache 命中率高**（小输入 + 共享 system prompt）

```typescript
// sessionMemory.ts:310
async function extractMemories(messages, sessionMemory) {
  const forkedAgent = await runForkedAgent({
    model: 'claude-haiku-4-5',  // 用小模型，节省成本
    systemPrompt: buildSessionMemoryUpdatePrompt(sessionMemory),
    prompt: formatNewMessages(messages, lastSummarizedId),
    tools: [createMemoryFileCanUseTool()],  // 只能用 Edit
  })
  // 后台跑完就完事，不阻塞主对话
}
```

> **解释**：SM extraction 是 SM 系统的**唯一** LLM 调用点。SM-compact 不调 LLM（"快路径"），SM extraction 调 LLM（"慢路径"在后台）。

#### 4.7.13 SM 文件结构与 mock 示例

**单文件结构**：

```text
{projectDir}/
└── {sessionId}/
    └── session-memory/
        └── summary.md    ← 唯一文件
```

源码位置：[src/utils/permissions/filesystem.ts:261-271](../../ailearning/claude-code-analysis/src/utils/permissions/filesystem.ts#L261-L271)

```typescript
function getSessionMemoryDir(sessionId: string): string {
  return join(getProjectDir(), sessionId, 'session-memory') + sep
}

function getSessionMemoryPath(sessionId: string): string {
  return join(getSessionMemoryDir(sessionId), 'summary.md')
}
```

**文件结构**：10 个固定 section，每个 section 有固定 header + italic description（LLM 不能修改）：

源码位置：[src/services/SessionMemory/prompts.ts:11-41](../../ailearning/claude-code-analysis/src/services/SessionMemory/prompts.ts#L11-L41)

```markdown
# Session Memory

## Session Goal
*High-level objective of this session — what the user is ultimately trying to accomplish.*

## Progress
*What has been done so far — completed tasks, files modified, decisions made.*

## Current State
*Where work is right now — what's in progress, what's blocked, immediate next steps.*

## Key Details
*Important specifics — file paths, function names, configuration values, exact error messages.*

## Files and Code
*Files read, edited, or created with relevant excerpts and line references.*

## Errors and Fixes
*Encountered errors with their resolutions — particularly non-obvious fixes.*

## Decisions
*Architectural or design decisions with reasoning, especially non-obvious trade-offs.*

## User Context
*User-specific information — preferences, working style, domain knowledge level, environment.*

## Todo
*Outstanding tasks the user wants done — track work that was promised but not yet completed.*

## Next Steps
*What to do next when continuing this session — prioritized list of pending work.*
```

> **解释**：section header 是"硬 schema"（LLM 不能改），italic description 是"软引导"（LLM 看到后知道这一 section 该写什么）。**正文（description 之后的部分）才是 LLM 实际编辑的区域**。

**真实 mock 示例**（Go REST API 场景）：

```markdown
# Session Memory

## Session Goal
*High-level objective of this session...*
Add a /users/:id/orders endpoint to the existing Go REST API, 
including validation, error handling, and integration tests.

## Progress
*What has been done so far...*
- Set up handler skeleton at `internal/handlers/orders.go`
- Implemented `OrderService.GetOrdersByUserID` (returns `[]Order, error`)
- Added `repository.OrderRepo.FindByUserID` with SQL query
- Wrote table-driven tests for service layer (10/10 passing)

## Current State
*Where work is right now...*
Handler `GetUserOrders` is partially wired — reads user ID from path, 
calls service, but **returns raw error without HTTP status mapping**. 
Service tests pass; handler tests fail with `500 Internal Server Error` 
when service returns `ErrUserNotFound`.

## Key Details
*Important specifics...*
- `internal/errors/errors.go` defines: `ErrNotFound`, `ErrValidation`, `ErrUnauthorized`
- Existing convention: handlers call `errors.MapToHTTP(err)` to convert domain errors to HTTP status
- User's request: return `404` for missing user, `400` for validation errors
- Path param: `:id` is a UUID, need to validate format

## Files and Code
*Files read, edited, or created...*
- `internal/handlers/orders.go:42` — current handler (incomplete error mapping)
- `internal/service/order_service.go:88` — `GetOrdersByUserID` signature
- `internal/repository/order_repo.go:55` — SQL query uses JOIN with `users` table

## Errors and Fixes
*Encountered errors with their resolutions...*
- `pq: invalid input syntax for type uuid: ""` — fixed by adding UUID validation in handler
- `sql: no rows in result set` — service wraps this as `ErrUserNotFound`

## Decisions
*Architectural or design decisions...*
- Chose `errors.MapToHTTP` over per-handler switch — matches existing pattern in `users.go`
- Decided NOT to add new error type for "no orders" — return `200` with empty slice

## User Context
*User-specific information...*
- Prefers table-driven tests (observed in existing test files)
- Uses `gofmt` + `golangci-lint` strict mode
- Domain: e-commerce backend, familiar with REST conventions

## Todo
*Outstanding tasks...*
- [ ] Complete error mapping in `GetUserOrders` handler
- [ ] Add handler-level tests (validation + 404 case)
- [ ] Verify `MapToHTTP` handles all relevant error types

## Next Steps
*What to do next...*
1. Implement `errors.MapToHTTP(err) → (statusCode, message)` in handler
2. Add UUID validation before calling service
3. Write handler tests for: valid request, missing user (404), invalid UUID (400)
4. Run `make test` and confirm all green
```

> **解释**：这个 mock 展示了"高密度信息保留"——4000 tokens 的 SM 文件能保留几十轮对话的"目标 + 进度 + 决策 + 下一步"。相比传统 compact 的"2000 tokens summary"，**信息密度高 2x、且不会随时间丢失**。

#### 4.7.14 SM extraction 时 LLM 看到的完整文件

**LLM 看到的不是"摘要后的版本"——是磁盘上的完整 SM 文件，含 section header 和 italic description。**

源码位置：[src/services/SessionMemory/sessionMemory.ts:217-225](../../ailearning/claude-code-analysis/src/services/SessionMemory/sessionMemory.ts#L217-L225)

```typescript
// 读 SM 文件
const memoryContent = await readFile(getSessionMemoryPath(sessionId), 'utf-8')

// 拼接到 update prompt 里
const prompt = buildSessionMemoryUpdatePrompt({
  currentNotes: memoryContent,  // 完整文件内容，含 header
  newMessages: formatMessages(messagesSinceLastSummary),
})
```

`buildSessionMemoryUpdatePrompt`（`prompts.ts:226-247`）模板：

```text
Current session memory notes:

<currentNotes>
{完整 SM 文件内容，含 ## Session Goal 之类的 header 和 *italic description*}
</currentNotes>

New messages since last summary:

<newMessages>
[新消息格式化后的文本]
</newMessages>

CRITICAL: 
- Do NOT modify, remove, or add to the section headers. 
- Only edit content UNDER each header.
- You MUST use the Edit tool (not Write) to update the file.
```

> **解释**：LLM 看到的 SM 文件**和用户看到的、磁盘上存储的一模一样**——包括：
> - 10 个 `##` 开头的 section header（作为 schema 参考）
> - 每个 header 下的 italic description（作为填写指引）
> - 之前所有 extraction 累积的正文（作为编辑基线）

**LLM 只能用 Edit，不能用 Write**：

源码位置：[src/services/SessionMemory/sessionMemory.ts:460-482](../../ailearning/claude-code-analysis/src/services/SessionMemory/sessionMemory.ts#L460-L482)

```typescript
function createMemoryFileCanUseTool() {
  return {
    name: 'Edit',
    description: 'Edit the session memory file',
    input_schema: {
      type: 'object',
      properties: {
        file_path: { const: getSessionMemoryPath(currentSessionId) },
        // ... Edit tool 必有字段
      },
      required: ['file_path', 'old_string', 'new_string'],
    },
  }
}
```

`createMemoryFileCanUseTool` 只暴露 **Edit** 工具——LLM 不能 Write 整个文件、不能用 NotebookEdit、不能用其他工具。**这强制 LLM 只能做"局部替换"，保证 schema 不被破坏**。

> **解释**：这种"工具白名单 + 路径白名单"的设计是 LLM 系统中的经典安全模式——既限制"能做什么"（只用 Edit），也限制"对谁做"（只能改 SM 文件路径）。即便 LLM 出现幻觉或被 prompt injection，也只能在 SM 文件的小范围内操作。

**为什么不让 LLM 看"摘要后的版本"？**

如果给 LLM 看的是截断/摘要后的 SM 文件，会出现两个问题：
1. **信息丢失**——LLM 不知道之前的 extraction 已经提取了哪些细节，可能重复提取
2. **schema 漂移**——没有 header 参考，LLM 容易写出"自由格式"的笔记，破坏后续 extraction 的稳定性

> **解释**：SM extraction 的 LLM 看到的**和用户读 SM 文件时看到的是同一份内容**——这是可观测性、可调试性的关键。用户任何时候 `cat summary.md` 都能"看到 LLM 看到的世界"。

---

## 5. AutoDream：跨会话的"做梦"整理

源码位置：[src/services/autoDream/autoDream.ts](../../ailearning/claude-code-analysis/src/services/autoDream/autoDream.ts)

### 5.1 类比

如果 `extractMemories` 是"刚才的对话 → 长期记忆"，那么 `autoDream` 是"过去 24h/5 个 session → 重新提炼长期记忆"。**它不创建新记忆，而是整合、合并、删除过时的。**

> **解释**：Dream（做梦）的隐喻很形象——人脑在睡眠中整理白天的记忆，把琐碎细节丢弃、把重要事件整合、把矛盾信息调和。autoDream 同样做"清理、整合、调和"——把多个 session 积累的零散记忆重新组织成更精炼的形式。

### 5.2 四道门控（从最便宜到最贵）

```ts
function runAutoDream(context, appendSystemMessage) {
  // 0. enabled gate
  if (!isGateOpen()) return

  // 1. 时间门（1 个 stat）
  const lastAt = await readLastConsolidatedAt()
  if (Date.now() - lastAt < minHours * 3600000) return
  //   默认 minHours = 24

  // 2. 扫描节流（10 分钟一次）
  if (Date.now() - lastSessionScanAt < 10 * 60 * 1000) return
  lastSessionScanAt = Date.now()

  // 3. session 数量门
  const sessionIds = await listSessionsTouchedSince(lastAt)
  sessionIds = sessionIds.filter(id => id !== getSessionId())  // 排除当前
  if (sessionIds.length < minSessions) return  // 默认 minSessions = 5

  // 4. 锁（PID + mtime 双重保护）
  const priorMtime = await tryAcquireConsolidationLock()
  if (priorMtime === null) return  // 其他进程在跑
}
```

> **逐门解释**：
> - **门 0**：`isGateOpen()` 检查 settings / feature flag——用户关闭 autoDream 就直接 return。
> - **门 1（时间门）**：读取 lock 文件的 mtime 作为"上次整合时间"。如果距今 < 24 小时，return。这是最便宜的检查——只读一个 stat。
> - **门 2（扫描节流）**：避免短时间内反复扫描 session 目录。每 10 分钟最多扫一次。
> - **门 3（session 数量）**：扫描 lastConsolidatedAt 之后改动的 session 文件，过滤掉当前 session。如果数量 < 5，return。这是相对昂贵的检查——需要 listdir。
> - **门 4（锁）**：用 lock 文件的 PID + mtime 检查是否有其他 Claude Code 实例正在跑 dream。`priorMtime === null` 说明被锁。

注释："Gate order (cheapest first): 1. Time → 2. Sessions → 3. Lock. Under force, skip acquire entirely."——精心安排的"cheap-first"避免每个 turn 跑 stat + scan。
> **解释**：门控顺序按"成本由低到高"排列——最便宜的 stat 先做，把大多数请求在前几道门就 reject 掉。"Under force" 指 `/dream` 手动触发模式——此时跳过锁获取，让用户强制运行（但仍会写到 lock 文件）。

### 5.3 锁文件 = mtime

源码位置：[src/services/autoDream/consolidationLock.ts](../../ailearning/claude-code-analysis/src/services/autoDream/consolidationLock.ts)

```ts
const LOCK_FILE = '.consolidate-lock'
const HOLDER_STALE_MS = 60 * 60 * 1000  // 1 小时

tryAcquireConsolidationLock() {
  // 读 mtime + PID
  // 如果 (now - mtime < 1h) && PID 活 → return null（被锁）
  // 否则写自己的 PID → mtime 自动更新为 now
  // 双重写竞争：readFile 验证自己赢了
  return mtimeMs ?? 0
}
```

> **逐行解释**：
> - `LOCK_FILE = '.consolidate-lock'`：lock 文件名，藏在 auto memory 根目录（点文件开头）。
> - `HOLDER_STALE_MS = 1h`：如果 lock 文件 mtime 距今超过 1 小时，认为锁持有者已死（崩溃 / kill -9 后没清理），可以强行覆盖。
> - `tryAcquireConsolidationLock` 流程：
>   1. 读 lock 文件获取 (mtime, PID)
>   2. 如果 mtime 在 1h 内 + PID 进程存活 → return null（被锁）
>   3. 否则把自己的 PID 写入文件 → mtime 自动更新
>   4. readFile 验证自己写的内容赢了（防两个进程同时写）
>   5. return mtime（成功）或 0（失败）

**"mtime IS lastConsolidatedAt"**——把锁信息和"上次整合时间"合并在一个文件上，节省一次 IO：

```ts
// rollback: 把 mtime 倒回 priorMtime，让下个 turn 能再过时间门
rollbackConsolidationLock(priorMtime) {
  if (priorMtime === 0) await unlink(path)
  else { await writeFile(path, ''); await utimes(path, priorMtime/1000, priorMtime/1000) }
}
```

> **解释**：精妙设计——通常"上次整合时间"和"当前 lock 状态"是两个独立概念、两个独立文件。这里把它们**合并**：lock 文件的 mtime 就是"上次整合时间"。读取 mtime 就同时拿到两个信息。
> - 如果 dream 跑成功，mtime 自动更新为 now（无需额外写）。
> - 如果 dream 跑失败需要回滚：用 `utimes()` 把 mtime 改回 priorMtime——等于"假装这次没发生"，下次还能再过时间门。
> - 如果 priorMtime = 0（之前没有整合），直接 `unlink` 删除 lock 文件——让下次从全新状态开始。
> - 这种"单一文件承担多职责"的设计是减少 IO 的经典技巧。

### 5.4 触发后的执行

```ts
const prompt = buildConsolidationPrompt(memoryRoot, transcriptDir, extra)

const result = await runForkedAgent({
  promptMessages: [createUserMessage({ content: prompt })],
  cacheSafeParams: createCacheSafeParams(context),
  canUseTool: createAutoMemCanUseTool(memoryRoot),  // 与 extract 同一套
  querySource: 'auto_dream',
  forkLabel: 'auto_dream',
  skipTranscript: true,  // 不写主转录
  overrides: { abortController },
  onMessage: makeDreamProgressWatcher(taskId, setAppState),  // UI 进度
})
```

> **逐参解释**：
> - `buildConsolidationPrompt`：构造 prompt，告诉 forked agent "请把过去 N 个 session 的记忆整合一下"。
> - `canUseTool: createAutoMemCanUseTool(memoryRoot)`：复用 extract 阶段的工具沙箱——同样只能在 memdir 内写。
> - `querySource: 'auto_dream'`：遥测分类。
> - `skipTranscript: true`：**关键**——dream 不写主对话的转录文件（因为 dream 是后台任务，不是用户可见的对话）。
> - `overrides.abortController`：注册外部 abort 信号，让用户能中途取消。
> - `onMessage: makeDreamProgressWatcher`：每个 assistant turn 都通过 watcher 转译为 UI 进度（"正在看 session 3 of 5"）。

`DreamTask` 是独立注册的任务（在 task system 里可见、可被 user 取消）。Watcher 把每个 assistant turn 折叠为 text + tool count + touched paths，送给 task state。
> **解释**：Dream 不只是"跑完才有结果"——它通过 `onMessage` 回调实时把进度推给 UI。`makeDreamProgressWatcher` 把每个 turn 折叠为："当前处理的文本摘要 + 已经用了多少工具 + 已经读/写了哪些路径"——用户在 UI 上能看到 dream agent 的进展。

### 5.5 取消支持

```ts
const abortController = new AbortController()
const taskId = registerDreamTask(setAppState, { sessionsReviewing: sessionIds.length, priorMtime, abortController })

// 在 catch 里
if (abortController.signal.aborted) {
  // user 主动 kill — DreamTask.kill 已 rollback lock + set status=killed
  return
}
// 其他错误：rollback + failDreamTask
```

> **逐行解释**：
> - `AbortController`：标准的取消机制——调用 `controller.abort()` 会让所有监听 `signal.aborted` 的代码路径感知到取消。
> - `registerDreamTask`：把 dream 注册到 task system，UI 上能看到这个任务、可以点击取消。
> - catch 里区分两种错误：
>   - **aborted**：用户主动 kill——task 系统已经做了 rollback（恢复 mtime）和 status='killed'，这里直接 return。
>   - **其他错误**：dream 跑挂了——手动 rollback lock 并标记 task 为 'failed'。

---

## 6. TeamMemorySync：多人共享

源码位置：[src/services/teamMemorySync/](../../ailearning/claude-code-analysis/src/services/teamMemorySync/)

### 6.1 整体语义

- 按 **git remote 标识**（github `owner/repo`）分区，每个 repo 一份独立 memory。
- 服务端用 **ETag + per-entry sha256** 双向同步。
- **本地优先写，server-wins on pull**——注释解释：
  > "Local-wins-on-conflict is the opposite of syncTeamMemory's pull-first semantics. This is intentional: pushTeamMemory is triggered by a local edit, and that edit must not be silently discarded just because a teammate pushed in the meantime."

> **解释**：
> - **按 repo 分区**：识别用户的 GitHub `owner/repo` slug，每个 repo 一份独立 team memory。在同一台机器上为多个 repo 工作时不会串台。
> - **ETag + sha256**：HTTP 层用 ETag 标识"整个 repo 的 server 状态"；entry 层用 sha256 标识"每个文件内容"。两层粒度让冲突检测既快又准。
> - **本地优先写（push 冲突时）**：默认 push 是本地编辑触发的——编辑内容必须保留，即使 teammate 同时推了更新也以本地为准。pull 则相反，server 是权威，server-wins。

### 6.2 API 契约

```
GET  /api/claude_code/team_memory?repo={owner/repo}                → 完整内容
GET  /api/claude_code/team_memory?repo={owner/repo}&view=hashes    → 仅 entryChecksums
PUT  /api/claude_code/team_memory?repo={owner/repo}                → upsert

404 = no data yet
304 = not modified (ETag)
412 = ETag mismatch → 触发冲突重试
413 + extra_details.max_entries → 学习 server cap
```

> **逐行解释**：
> - `GET ?view=hashes`：只取 entryChecksums（每个文件的 sha256 列表），用于 push 时算 delta 时不需要拉全文。
> - `PUT`：upsert 语义——服务端根据 entry key 决定新增或覆盖。
> - `404`：服务端无数据（首次同步或 repo 不存在）。
> - `304`：携带 `If-None-Match` ETag 时服务端返回——表示"没变化，无需下载"。
> - `412 Precondition Failed`：ETag 不匹配——服务端在这期间被其他人更新过。需要触发冲突重试。
> - `413 + extra_details.max_entries`：超出服务端条数上限，响应里告知最大允许数。

### 6.3 Pull

```ts
export async function pullTeamMemory(state, options?) {
  // 1. ETag 缓存（If-None-Match）→ 304
  // 2. 404 → state.serverChecksums.clear()（防止 push 漏算）
  // 3. 200 → 解析 entryChecksums → 写本地
  //    写时：validateTeamMemKey（防 path traversal / symlink escape）→ readFile 比对
  //    内容相同则跳过 — 保持 mtime（不让 getMemoryFiles cache 失效）
}
```

> **逐行解释**：
> - **步骤 1**：用 `If-None-Match` 携带上次服务端 ETag。如果服务端没变化，直接 304 short-circuit，省一次下载。
> - **步骤 2**：404 表示服务端没数据（首次同步），**清空** `serverChecksums`——必须清，否则 push 阶段会以为 server 仍持有旧 checksum，导致本地写不上去。
> - **步骤 3**：200 时解析每个 entry 的 content，写到本地。
>   - 写前先 `validateTeamMemKey`——多层防御（详见 6.7）。
>   - 写前 `readFile` 比对——如果内容完全一样，**不写**。

**mtime 保持的小聪明**：
> "Skips entries whose on-disk content already matches, so unchanged files keep their mtime and don't spuriously invalidate the getMemoryFiles cache or trigger watcher events."

> **解释**：如果 pull 把同样内容再写一遍，mtime 会被更新。这有两个副作用：(1) `getMemoryFiles` 缓存的"哪些文件变了"判断失效；(2) watcher 看到 mtime 变化就误以为"用户编辑了"，再次触发 push——形成死循环。读后比对、不变不写，保持 mtime 稳定，避开这两个副作用。

### 6.4 Push（delta + 冲突重试）

```ts
export async function pushTeamMemory(state) {
  // 1. 读本地 + secret 扫描（PSR M22174）→ 跳过含 secret 的文件
  // 2. 对每个 entry 计算 sha256
  // 3. 算 delta：仅 hash 与 serverChecksums[key] 不同的 key
  // 4. deltaCount == 0 → 直接 return success
  // 5. 批分批（≤200KB/PUT）→ 串行 PUT
  // 6. 412 → 用 GET ?view=hashes 刷新 serverChecksums → 重试
  //    最多 2 次重试
  // 7. 413 + extra_details.max_entries → 缓存到 state.serverMaxEntries，下一次 push 截断
}
```

> **逐行解释**：
> - **步骤 1**：每个文件先过 secret 扫描（gitleaks 规则集）。命中则跳过该文件，不上传。
> - **步骤 2-3**：算 sha256，与 `state.serverChecksums`（上次 pull 时缓存的服务端 hash）比对——只传"本地 hash ≠ server hash"的 entry。这是 delta 算法。
> - **步骤 4**：如果 delta 是空（什么都没改），直接返回 success，不发 PUT。
> - **步骤 5**：把所有 delta 按 200KB/PUT 切批（避免 gateway 413 body size 上限），逐批 PUT。
> - **步骤 6**：412 ETag mismatch 说明 push 期间服务端被其他人更新过。重新 GET ?view=hashes 刷新 serverChecksums，重新算 delta，再尝试 PUT。最多重试 2 次。
> - **步骤 7**：413 Payload Too Large 表示超出服务端条数上限。从 `extra_details.max_entries` 读取上限并缓存，下次 push 时本地截断（保留最重要的 N 条）。

**冲突重试的巧妙之处**：

```ts
// 第 N 次冲突重试中：
// serverChecksums 已用 GET ?view=hashes 刷新
// delta 自然排除：本地 hash == server hash 的 key（即 teammate 推的相同内容）
// → 我们不会把 teammate 推的内容再推一遍
// 但若本地版本和 server 版本对同一 key 都改了，local-wins（注释明确：宁可丢 teammate 的，也要保留 user 的）
```

> **解释**：delta 算法天然支持冲突重试——每次冲突后 GET 刷新 serverChecksums，新 delta 自动排除"已经被 teammate 推过的相同内容"。对于"双方都改了同一 key"的冲突，代码选择 local-wins：宁可丢 teammate 的改动，也要保留 user 的本地编辑。这与"pull 是 server-wins"形成对照——push 触发于本地编辑，要尊重编辑；pull 是被服务端推送覆盖，要以服务端权威为准。

### 6.5 Watcher：fs.watch + debounce

源码位置：[src/services/teamMemorySync/watcher.ts](../../ailearning/claude-code-analysis/src/services/teamMemorySync/watcher.ts)

```ts
const DEBOUNCE_MS = 2000

function schedulePush() {
  if (pushSuppressedReason !== null) return  // 永久失败抑制
  hasPendingChanges = true
  if (debounceTimer) clearTimeout(debounceTimer)
  debounceTimer = setTimeout(() => {
    if (pushInProgress) { schedulePush(); return }
    currentPushPromise = executePush()
  }, DEBOUNCE_MS)
}
```

> **逐行解释**：
> - `DEBOUNCE_MS = 2000`：debounce 时间窗。文件变化后等 2 秒"平息"再 push——避免一次编辑触发多次 push。
> - 顶部 if：永久失败抑制——如果之前 push 因为不可恢复原因（no_oauth / 4xx）失败，停止重试。
> - `hasPendingChanges = true`：标记有变更（用于 graceful shutdown 时 flush）。
> - 重置 timer：每次新变更都重置 2s 窗口——典型 debounce。
> - timer 触发时：如果当前 push 还在跑，递归 schedulePush（让 push 完成后**立即**再触发一次）；否则发起 push。

**`fs.watch` 的玄学**：

```ts
// 注释里说：
// - chokidar 4+ 放弃了 fsevents 依赖
// - Bun fallback kqueue：每个文件一个 fd，500 文件 = 500 fds
//   （用 lsof + repro 验证过）
// - `recursive: true` 走 FSEvents (macOS) 或 inotify (linux) — O(1)/O(subdirs) fds
// - 验证：2 fds for 60 files across 5 subdirs
watch(teamDir, { persistent: true, recursive: true }, (_event, filename) => {
  if (filename === null) { schedulePush(); return }
  if (pushSuppressedReason !== null) {
    // 用 stat 区分 unlink vs write（fs.watch 不能区分）
    void stat(join(teamDir, filename)).catch(err => {
      if (err.code !== 'ENOENT') return
      if (pushSuppressedReason !== null) {
        pushSuppressedReason = null  // user 删文件了 → 解除 too-many-entries 抑制
      }
      schedulePush()
    })
    return
  }
  schedulePush()
})
```

> **逐行解释**：
> - 注释解释了为什么不直接用 `chokidar`：chokidar 4+ 放弃了 fsevents 依赖；Bun fallback 走 kqueue，每个 watched 文件占一个 fd——500 个 team memory 文件 = 500 个永久占用的 fd。
> - 用 Node 自带的 `fs.watch { recursive: true }`：macOS 走 FSEvents（O(1) fds）、Linux 走 inotify（O(subdirs) fds）。已验证：60 个文件跨 5 个子目录只占 2 个 fd。
> - 回调里 `filename === null` 表示"目录本身的变化或无文件名"——直接 schedulePush。
> - 如果 `pushSuppressedReason !== null`（永久抑制中），需要区分"用户写了文件"和"用户删了文件"——`fs.watch` 不区分，所以用 `stat` 试探：ENOENT 即已删除。
> - **删除文件会清除抑制**：因为删除是用户主动的"恢复操作"（比如清理太多 entries 后想重新 push）。其他类型的失败（如 no_oauth）不会因删除而清除——no_oauth 用户不会通过删文件恢复，他们需要重启 OAuth。

**永久失败抑制**：

```ts
// 一个 no_oauth 设备曾在 2.5 天内 emit 167K push events（BQ Mar 14-16）
// — 抑制避免无限重试

function isPermanentFailure(r) {
  if (r.errorType === 'no_oauth' || r.errorType === 'no_repo') return true
  if (r.httpStatus >= 400 && r.httpStatus < 500 && r.httpStatus !== 409 && r.httpStatus !== 429) return true
  return false
}
```

> **解释**：注释里的"BQ Mar 14-16"指 BigQuery 上的某次事故数据——一个 no_oauth 设备在 2.5 天内发出了 167,000 次 push 事件，全是失败。原因是 watcher 触发 → push 失败 → 没有抑制 → 下一波文件变化又触发 → 死循环。永久失败抑制就是切断这个循环。

409 (conflict) 和 429 (rate limit) **不**算永久失败——它们应当自然恢复。
> **解释**：
> - `409 Conflict`：服务端冲突，是临时状态——下一轮 pull + push 就能解决。
> - `429 Too Many Requests`：服务端限流，等一会儿就好。
> - 这两种临时错误必须保留重试能力。注释特别排除这两种。

### 6.6 密钥扫描

```ts
// 同步前每个文件过 gitleaks 规则集
const secretMatches = scanForSecrets(content)
if (secretMatches.length > 0) {
  // 跳过该文件（不传服务器）
  // log ruleId + label，不 log 文件路径、值
  skippedSecrets.push({ path, ruleId, label })
}
```

注释里 PSR M22174 反复出现——这是一个安全合规要求。
> **解释**：PSR = Product Security Requirement。**M22174** 是 Anthropic 内部对"team memory 同步前必须做 secret 扫描"的合规要求。日志只记 `ruleId` 和 `label`（如 "AWS Access Token"），不记 `path` 或具体命中值——避免日志本身成为泄露源。`skippedSecrets` 通过 UI 反馈给用户"这些文件因为含密钥被跳过"。

### 6.7 路径安全（teamMemPaths.ts）

`validateTeamMemKey` 是**两阶段检查**：

```ts
async function validateTeamMemKey(relativeKey) {
  // 阶段 1：字符串层 sanitize
  //   - null byte
  //   - URL 编码（%2e%2e%2f = ../）
  //   - Unicode 归一化攻击（全角 ．．／ → ASCII ../）
  //   - 反斜杠（Windows 风格）
  //   - 绝对路径
  
  // 阶段 2：symlink 防逃逸
  //   - path.resolve() 处理 .. 段
  //   - realpathDeepestExisting()：沿路径向上找到第一个存在的祖先，realpath 它
  //   - 防 dangling symlink（lstat 区别 ENOENT vs 真的不存在）
  //   - 防 ELOOP 循环
}
```

> **逐条解释**：
> - **阶段 1（字符串层）**：
>   - `null byte`：C 风格字符串截断（`/team/\0../../etc/passwd` 在某些库看来是 `/team/`）。
>   - URL 编码：`%2e%2e%2f` = `../`，必须在解码后再检查。
>   - Unicode NFKC：全角 `．．／` 经 NFKC 归一化后会变成 ASCII `../`，必须先归一再检查。
>   - 反斜杠：Windows 路径分隔符 `\` 在某些环境下被等同 `/`，可能绕开基于 `/` 的检查。
>   - 绝对路径：`/etc/passwd` 这类以 `/` 或盘符开头的直接拒绝。
> - **阶段 2（symlink 防逃逸）**：
>   - `path.resolve()`：把 `../` 段消解，但不解析 symlink。
>   - `realpathDeepestExisting()`：从路径起点向上找到第一个存在的祖先目录，对它 realpath；剩余部分作为相对路径拼接。这样即使中间有不存在的目录（dangling symlink）也能正确处理。
>   - `lstat`：区别"ENOENT 因为 dangling symlink"和"ENOENT 因为路径真的不存在"——前者要拒绝，后者可以接受。
>   - ELOOP：symlink 循环会让 `realpath` 报 ELOOP——必须 catch 后拒绝。

这是**PSR M22186 / M22187**的安全设计——`path.resolve()` 不解析 symlink，因此必须 `realpath` 才能阻止攻击者放置 `team/../../.ssh/authorized_keys` 形式的 symlink。
> **解释**：PSR M22186/M22187 是针对"team memory 路径安全"的合规要求。攻击模型：恶意用户在 `team/` 目录下创建一个 `mem.md` → `../../.ssh/authorized_keys` 的 symlink。pull 流程如果没做 symlink 防逃逸，就会写入 symlink 指向的真实路径——覆盖用户的 SSH 公钥文件。

### 6.8 入口与可用性

```ts
function isUsingOAuth() {
  // 必须 firstParty + firstPartyAnthropic + OAuth with profile scope
}

function isTeamMemorySyncAvailable() {
  return isUsingOAuth()  // 没 OAuth 就完全不可用
}

async function startTeamMemoryWatcher() {
  if (!feature('TEAMMEM')) return         // build flag
  if (!isTeamMemoryEnabled()) return
  if (!isTeamMemorySyncAvailable()) return
  const repoSlug = await getGithubRepo()
  if (!repoSlug) return  // 必须 github.com remote
  // → 初始 pull + 启 watcher
}
```

注释里特别强调："The early github.com check prevents a noisy failure mode where the watcher starts, it fires on local edits, and every push/pull logs `errorType: no_repo` forever."

> **解释**：早期不做 github.com 检查的版本有"噪音失败模式"——watcher 启动后看到本地编辑就尝试 push，但 repo 不是 github.com → 服务端永远返回 `no_repo` → 反复日志告警污染。提前检查可以一次性 return，避免无意义的循环。

---

## 7. 系统提示词注入：loadMemoryPrompt

源码位置：[src/memdir/memdir.ts](../../ailearning/claude-code-analysis/src/memdir/memdir.ts) `loadMemoryPrompt()`

```ts
export async function loadMemoryPrompt(): Promise<string | null> {
  const autoEnabled = isAutoMemoryEnabled()
  const skipIndex = getFeatureValue_CACHED_MAY_BE_STALE('tengu_moth_copse', false)

  // 优先级 1：KAIROS 日志模式
  if (feature('KAIROS') && autoEnabled && getKairosActive()) {
    return buildAssistantDailyLogPrompt(skipIndex)
  }

  // 优先级 2：team memory（隐含需要 auto enabled）
  if (feature('TEAMMEM') && teamMemPaths.isTeamMemoryEnabled()) {
    await ensureMemoryDirExists(teamDir)  // team 路径 = join(auto, 'team') → mkdir 创建父链
    return teamMemPrompts.buildCombinedMemoryPrompt(extraGuidelines, skipIndex)
  }

  // 优先级 3：仅 auto memory
  if (autoEnabled) {
    await ensureMemoryDirExists(autoDir)
    return buildMemoryLines('auto memory', autoDir, extraGuidelines, skipIndex).join('\n')
  }

  return null  // 全部关闭
}
```

> **逐行解释**：
> - 顶部读取两个状态：`autoEnabled`（用户开关）+ `skipIndex`（GrowthBook 控制"是否跳过 MEMORY.md 索引"——A/B 测试用）。
> - **优先级 1 KAIROS**：assistant 模式特殊路径——直接走"按日追加日志"的 prompt，不走 MEMORY.md 索引。
> - **优先级 2 TEAMMEM**：team memory build + 用户开启了 team memory。`ensureMemoryDirExists(teamDir)` 注意 `teamDir = join(autoDir, 'team')`——team 是 auto 的子目录，但 `mkdir recursive:true` 会顺带创建父链。
> - **优先级 3 auto**：普通用户场景。注入 MEMORY.md 索引内容。
> - **全关**：返回 null——上层不注入任何 memory 相关 prompt。

**Cowork 注入**：

```ts
const coworkExtraGuidelines = process.env.CLAUDE_COWORK_MEMORY_EXTRA_GUIDELINES
const extraGuidelines = coworkExtraGuidelines?.trim().length > 0 ? [coworkExtraGuidelines] : undefined
```

通过 `CLAUDE_COWORK_MEMORY_EXTRA_GUIDELINES` env var 给 Cowork 平台追加自有策略（同时配 `CLAUDE_COWORK_MEMORY_PATH_OVERRIDE` 改路径）。
> **解释**：Cowork 是 Anthropic 的协作产品（类似 Claude.ai 工作区）。它需要在 Claude Code CLI 之上注入自己的策略（比如"workspace 内的文件共享"），通过环境变量传给 CLI，CLI 在 system prompt 里追加这段 guidelines。同时配 `CLAUDE_COWORK_MEMORY_PATH_OVERRIDE` 改变 memdir 路径（默认是 CLI 路径，Cowork 想用自己的存储）。

### 7.1 Searching past context

```ts
export function buildSearchingPastContextSection(autoMemDir: string): string[] {
  if (!getFeatureValue_CACHED_MAY_BE_STALE('tengu_coral_fern', false)) return []
  
  // Ant-native builds: Grep tool 被别名到 embedded ugrep 并删除
  // REPL mode: Grep/Bash 隐藏在主列表，模型从 REPL 脚本里调用
  const embedded = hasEmbeddedSearchTools() || isReplModeEnabled()
  
  const memSearch = embedded
    ? `grep -rn "<search term>" ${autoMemDir} --include="*.md"`
    : `${GREP_TOOL_NAME} with pattern="<search term>" path="${autoMemDir}" glob="*.md"`
  // ...
}
```

两套调用语法根据 build 形态切换（这是 Ant 内部 vs 外部 build 的差异点）。
> **解释**：
> - **Ant-native build**：Anthropic 内部版本。Grep 工具被"别名"到 embedded ugrep（一个嵌入式二进制），并从工具列表里**删除** Grep 工具——这样 LLM 看到的工具是 Bash，调用 Bash 时通过 `grep -rn ...` 字符串触发。这种 build 用于内部测试新功能。
> - **REPL mode**：CLI 提供一个 REPL（嵌入式 JS 运行时），Bash/Grep 工具被"折叠"进 REPL 脚本。LLM 通过 REPL 间接调用，避免每次都让 LLM 选工具。
> - **外部 build（默认）**：标准 CLI 用户，看到 Grep 工具在主列表里，按 `Grep(pattern=..., path=..., glob=...)` 调用。
> - 这段代码生成的 prompt 段是给 LLM 看的"如何搜索记忆"——必须根据当前 build 形态给出对应语法，否则 LLM 会用不存在的工具调用。

---

## 8. Agent Memory：按 agentType 隔离

源码位置：[src/tools/AgentTool/agentMemory.ts](../../ailearning/claude-code-analysis/src/tools/AgentTool/agentMemory.ts)

```ts
export type AgentMemoryScope = 'user' | 'project' | 'local'

export function getAgentMemoryDir(agentType, scope) {
  // 'user'    → <memoryBase>/agent-memory/<agentType>/
  // 'project' → <cwd>/.claude/agent-memory/<agentType>/
  // 'local'   → <cwd>/.claude/agent-memory-local/<agentType>/
  //              或 <remote>/projects/<slug>/agent-memory-local/<agentType>/
}
```

> **逐 scope 解释**：
> - **`user`**：跨项目通用，存到 `~/.claude/agent-memory/<agentType>/`。比如 `general-purpose` agent 学到"用户喜欢详细解释"——所有项目都适用。
> - **`project`**：项目级，存到 `<cwd>/.claude/agent-memory/<agentType>/`，**入 VCS**。比如某项目里 `Explore` agent 学到的"这个 monorepo 的特定约定"，应该和团队共享。
> - **`local`**：项目级但**不入库**，存到 `<cwd>/.claude/agent-memory-local/<agentType>/`。比如个人调试用的笔记，不污染团队。
> - 注意 `sanitizeAgentTypeForPath(agentType)`：把 `:` 替换成 `-`（plugin-namespaced 类型如 `my-plugin:my-agent` 在 Windows 上不能有 `:`）。

三档 scope：

| scope    | 路径                                          | 适合                          |
| -------- | --------------------------------------------- | ----------------------------- |
| `user`   | `~/.claude/agent-memory/<agentType>/`         | 跨项目的通用经验               |
| `project`| `<cwd>/.claude/agent-memory/<agentType>/`     | 项目级约定，VCS 入库          |
| `local`  | `<cwd>/.claude/agent-memory-local/<agentType>/` | 项目级但不入库（个人实验）   |

**与 auto memory 的关系**：

- agent memory 的 prompt 复用 `buildMemoryPrompt`（同一套四类分类法）。
- 通过 `agentType`（如 `general-purpose`、`statusline-setup`）目录隔离。
- `loadAgentMemoryPrompt` 同步调用，fire-and-forget `ensureMemoryDirExists`——注释说"agent-spawn in React render, so it cannot be async"。
- "agent-spawn 后才第一次 Write, by which time mkdir 完成"。

> **逐条解释**：
> - **复用 `buildMemoryPrompt`**：agent memory 与 auto memory 使用同一套 system prompt 模板——四类分类法、frontmatter 格式、trusting recall 等都一致。差异只在**存储路径**。
> - **agentType 目录隔离**：每个 agent 类型（如 `general-purpose`、`statusline-setup`、`Explore`）有自己的子目录，避免不同 agent 的记忆串台。
> - **fire-and-forget mkdir**：`loadAgentMemoryPrompt` 是在 React 组件渲染期间同步调用的（不能用 async）。它发起 mkdir 但不等结果——注释保证：agent 第一次 Write 之前会经过一次完整的 API round-trip，那时 mkdir 早就完成了。

**安全**：

```ts
isAgentMemoryPath(absolutePath) {
  // 三种 scope 路径都判断
  // SECURITY: normalize() 先处理 ../ 段
}
```

> **解释**：判断某个绝对路径是否在 agent memory 范围内，用于工具权限判定（如 Bash/Edit 是否允许写）。三种 scope 都覆盖，且先 `normalize()` 防 `../` 段绕过。`SECURITY` 注释强调这是**显式的安全检查**，不是普通的路径处理。

---

## 9. UI 呈现：MemoryFileSelector

源码位置：[src/components/memory/MemoryFileSelector.tsx](../../ailearning/claude-code-analysis/src/components/memory/MemoryFileSelector.tsx)

```tsx
// 列出的记忆类型：
// - User memory      (~/.claude/CLAUDE.md)
// - Project memory   (./CLAUDE.md)
// - All @imported CLAUDE.md 树
// - 嵌套的 @-imported 子文件
// - Agent memory     (per active agent)
// - "Open auto-memory folder"
// - "Open team memory folder"
```

> **解释**：这是 CLI 设置面板里的"MEMORY" 区域，列出所有相关记忆文件 + 入口：
> - **User memory**：用户全局的 `~/.claude/CLAUDE.md`
> - **Project memory**：当前项目的 `./CLAUDE.md`
> - **@-imported CLAUDE.md 树**：CLAUDE.md 支持 `@path/to/other.md` 语法递归引入其他文件——这里列出所有被引入的文件
> - **Agent memory**：当前活跃 agent 的记忆（如 `general-purpose` agent 的 `agent-memory/general-purpose/MEMORY.md`）
> - **Auto-memory folder / Team memory folder**：打开本地文件管理器到对应目录的快捷入口

### 9.1 三个开关

```tsx
const [autoMemoryOn, setAutoMemoryOn] = useState(isAutoMemoryEnabled)
const [autoDreamOn, setAutoDreamOn] = useState(isAutoDreamEnabled)
const showDreamRow = isAutoMemoryEnabled  // 关闭 auto 才隐藏 dream 行

function handleToggleAutoMemory() {
  updateSettingsForSource('userSettings', { autoMemoryEnabled: newValue })
  logEvent('tengu_auto_memory_toggled', { enabled: newValue })
}
function handleToggleAutoDream() {
  updateSettingsForSource('userSettings', { autoDreamEnabled: newValue })
  logEvent('tengu_auto_dream_toggled', { enabled: newValue })
}
```

> **逐项解释**：
> - `useState(isAutoMemoryEnabled)`：从当前 settings 读初值。
> - `showDreamRow`：dream 行的可见性依赖 autoMemory 开关——auto 都关了，dream 行没意义，隐藏。
> - `updateSettingsForSource('userSettings', ...)`：写入**用户级** settings.json。故意写 user source 而非 project——这是用户偏好，不应该跟着项目走。
> - `logEvent`：每次切换发遥测事件，记录开关变化（用于 A/B 测试或监控）。

### 9.2 dream 状态行

```tsx
const isDreamRunning = useAppState(s =>
  Object.values(s.tasks).some(t => t.type === 'dream' && t.status === 'running')
)
const [lastDreamAt, setLastDreamAt] = useState<number | null>(null)
useEffect(() => {
  if (!showDreamRow) return
  void readLastConsolidatedAt().then(setLastDreamAt)
}, [showDreamRow, isDreamRunning])

const dreamStatus = isDreamRunning ? 'running'
  : lastDreamAt === null ? ''
  : lastDreamAt === 0 ? 'never'
  : `last ran ${formatRelativeTimeAgo(new Date(lastDreamAt))}`
```

> **逐行解释**：
> - `isDreamRunning`：从全局 appState 里查所有 task，看有没有 `type === 'dream' && status === 'running'`。
> - `lastDreamAt`：用 `useState` + `useEffect` 异步读取 lock 文件的 mtime（即"上次整合时间"）。
> - 依赖数组 `[showDreamRow, isDreamRunning]`：dream 启动/完成时触发重读，让 UI 实时更新。
> - `dreamStatus` 三态：
>   - `running`：dream agent 正在跑。
>   - `never`：lastDreamAt === 0 表示从未跑过。
>   - `last ran X ago`：有历史，显示相对时间（如 "3 hours ago"）。

呈现："Auto-dream: on · last ran 3 hours ago · /dream to run"

`/dream` 提示只在 `!isDreamRunning && autoDreamOn` 时出现——避免重复触发。
> **解释**：用户看到这行能直观知道：(1) autoDream 开关状态；(2) 上次跑是多久前；(3) 怎么手动触发。`/dream to run` 是 SlashCommand 的提示——用户输入 `/dream` 即可立即触发一次整合（跳过 24h/5 sessions 门控）。但只在 dream 没在跑时显示，否则提示"/dream to run"会让用户重复触发。

---

## 10. 安全模型（横切关注点）

### 10.1 路径安全多层防御

| 层 | 位置 | 防御内容 |
| --- | --- | --- |
| L1 | `validateMemoryPath` (paths.ts) | relative, root, drive-root, UNC, null byte |
| L2 | `validateMemoryPath` | NFC 归一化（防 Unicode trick） |
| L3 | `isAutoMemPath` | normalize() 防 ../ 段逃逸 |
| L4 | `validateTeamMemKey` (teamMemPaths.ts) | null, URL-encode, NFKC, backslash, abs |
| L5 | `realpathDeepestExisting` (teamMemPaths.ts) | symlink 防逃逸（含 dangling symlink） |
| L6 | `isRealPathWithinTeamDir` | realpath 双向对比，separator 后缀防 `/team-evil` 攻击 |

> **逐层解释**：
> - **L1 字符串层**：在 path 字符串阶段就拒绝明显异常——相对路径（`./foo`）、根路径（`/`）、Windows drive-root（`C:\`）、UNC（`\\server\share`）、null byte（`\0` 截断）。
> - **L2 Unicode 层**：用 NFC 把 `é` 的两种 Unicode 写法统一为一种，阻止"看起来像 `foo` 但用组合字符构造"的绕过。
> - **L3 normalize 层**：用 `normalize()` 处理 `..` 段，让路径比较时无歧义。
> - **L4 teamMemKey 层**：专门防御 team memory 的多层攻击向量——null、URL 编码、NFKC Unicode 归一化（`．．／` → `../`）、Windows 反斜杠、绝对路径。
> - **L5 symlink 层**：`realpathDeepestExisting` 沿路径向上找第一个存在祖先做 realpath——既处理已存在的 symlink，也处理 dangling symlink。
> - **L6 separator suffix 层**：realpath 双向对比后，用 separator (`/`) 后缀检查 `startsWith`——避免 `team` 目录被 `team-evil` 这种"前缀相同但目录不同"的攻击绕过。

### 10.2 写权限沙箱

| 工具 | 限制 |
| --- | --- |
| SessionMemory `createMemoryFileCanUseTool` | 只允许 `Edit` 一个具体文件路径 |
| extractMemories / autoDream `createAutoMemCanUseTool` | Read/Grep/Glob 全开；Bash 只读；Edit/Write 仅 memdir 内 |

> **解释**：
> - **SessionMemory 的最严格沙箱**：只能 Edit **一个具体路径**。Read/Bash/Write 全部拒绝。这是"agent 只能写这一个文件"的极致限制。
> - **extract / autoDream 的中等沙箱**：可以 Read/Grep/Glob（理解项目）、可以 Bash 但仅 `isReadOnly()`（探索代码）、可以 Edit/Write 但仅 memdir 内（写记忆）。给 agent 留出"读取 + 整理"的能力，但阻止它"乱写其他文件"。

### 10.3 文件权限

```ts
// SessionMemory：
await fs.mkdir(sessionMemoryDir, { mode: 0o700 })  // 仅 owner
await writeFile(memoryPath, '', { mode: 0o600, flag: 'wx' })  // 仅 owner + 不覆盖

// Auto memdir：依赖 umask（默认安全）
```

> **逐行解释**：
> - `mkdir mode: 0o700` = `rwx------`：目录仅 owner 可读写执行，其他用户无法访问。
> - `writeFile mode: 0o600, flag: 'wx'` = 权限 `rw-------` + `O_CREAT|O_EXCL`：文件仅 owner 可读写，且只在文件不存在时创建（防覆盖已有文件）。
> - `0o600` 比常见的 `0o644` 严格——记忆可能含敏感信息（用户偏好、项目机密），不能让同机器的其他用户读到。
> - Auto memdir 不显式设置 mode，依赖系统的默认 umask（通常 `0o022`）——这种信任是因为 auto memdir 的内容预期是"非敏感的项目偏好"，但仍然在 team sync 时做 secret 扫描。

### 10.4 密钥防护

`teamMemorySync/secretScanner.ts` 用 gitleaks 规则，**PSR M22174**：
- 命中则跳过该文件，不上传；
- 日志只记录 `ruleId` 和 `label`，不记路径、值；
- 同步结果通过 `skippedSecrets` 告知用户。

> **解释**：
> - **gitleaks 规则**：业界标准的密钥扫描规则集，覆盖 AWS key、GitHub token、Stripe key、SSH private key 等数百种模式。
> - **跳过而非失败**：命中密钥不抛错，而是悄悄跳过该文件 + 通知用户——避免"上传密钥"或"中断用户的同步流程"两个极端。
> - **日志脱敏**：只记 `ruleId`（如 "stripe-access-token"）和 `label`（如 "Stripe Access Token"），**不**记文件路径和匹配到的密钥值本身——避免日志本身成为泄露源。
> - `skippedSecrets` 通过 UI 展示给用户："以下文件因含密钥被跳过"，让用户知情。

### 10.5 settings.json 的"信任层级"

```ts
// getAutoMemPathSetting 故意排除 projectSettings
// 防恶意 repo 写 autoMemoryDirectory: "~/.ssh" 拿到 ssh 目录的静默写权限
```

这是 `filesystem.ts` 写权限的特例——`isAutoMemPath()` + `!hasAutoMemPathOverride()` 时绕过 `DANGEROUS_DIRECTORIES` 检查；这意味着如果 settings.json 能任意指定 auto memdir，则能写 `~/.ssh`。所以 settings.json 也只信任非 project 源。
> **解释**：正常情况下，文件系统模块对 `~/.ssh` 等敏感目录有 `DANGEROUS_DIRECTORIES` 拒绝写入。但 `isAutoMemPath()` 命中时会绕过这个检查（因为 auto memory 需要能写到 memdir）。如果项目里的 `settings.json` 能设置 `autoMemoryDirectory: "~/.ssh"`，就等于绕过所有安全检查、写到 `~/.ssh` 里去。所以 `getAutoMemPathSetting` 显式排除 `projectSettings`——只允许 `policySettings / flagSettings / localSettings / userSettings` 这些"用户控制"的源。

### 10.6 Telemetry 与用户控制

- 关闭 auto memory：`CLAUDE_CODE_DISABLE_AUTO_MEMORY=1` 或 `settings.json { autoMemoryEnabled: false }`
- 关闭 team memory：feature gate `tengu_herring_clock`（GB 远程）
- 关闭 autoDream：settings.json `{ autoDreamEnabled: false }` 或 feature gate `tengu_onyx_plover.enabled`
- 关闭 SM-compact：`DISABLE_CLAUDE_CODE_SM_COMPACT=1` 或 feature gate

> **逐项解释**：
> - 每个功能都有**双重关闭路径**：env 变量（开发者调试用）+ settings.json（普通用户用）。
> - team memory 和 SM-compact 额外有 feature gate（GB = GrowthBook 远程 feature flag）——Anthropic 可以灰度发布 / A/B 测试。
> - 命名风格 `tengu_*`：tengu 是 Claude Code 的内部代号（日本佛教中的"天狗"），所有 feature flag 都用这个前缀——`tengu_bramble_lintel`（节流）、`tengu_moth_copse`（skip index）、`tengu_coral_fern`（search section）等。

---

## 11. 性能优化点

| 优化 | 位置 | 效果 |
| --- | --- | --- |
| `getAutoMemPath` memoize | paths.ts | 避免每次 render 都 parse settings × 4 sources |
| `ensureMemoryDirExists` 异步 fire-and-forget | memdir.ts | 主线程 render 不阻塞 |
| `loadMemoryPrompt` 在 system prompt section 缓存 | memdir.ts | 命中 prompt cache，避免重复生成 |
| `scanMemoryFiles` 单 pass 读 frontmatter | memoryScan.ts | 比 stat-sort-read 少一半 syscalls |
| `MAX_MEMORY_FILES = 200` | memoryScan.ts | 截断极端情况下的开销 |
| `formatMemoryManifest` 预注入到 extract prompt | extractMemories.ts | 避免提取 agent 自己跑 `ls` |
| `drainer.unref()` 定时器 | extractMemories.ts | 不阻塞 shutdown |
| `fs.watch({ recursive: true })` | teamMemorySync/watcher.ts | macOS FSEvents O(1) fds |
| `batchDeltaByBytes` greedy bin-packing | teamMemorySync/index.ts | 避免 gateway 413 body-size 上限 |
| `if-none-match` ETag | teamMemorySync | 304 short-circuit pull |
| KAIROS 日志路径描述为 pattern | memdir.ts | "prompt is cached by systemPromptSection('memory', ...) and NOT invalidated on date change" |
| `drainPendingExtraction` 60s soft timeout | extractMemories.ts | shutdown 时 5s 之前完成 |

> **逐行解释**：
> - **memoize**：函数记忆化，同一 projectRoot 只算一次。注释解释原因："每次 React 重新渲染都会触发 path lookup"。
> - **fire-and-forget mkdir**：发起 mkdir 但不等结果，LLM 第一次 Write 时早就完成了。
> - **prompt cache 兼容**：memory 段的 prompt 内容在 session 内尽量稳定，让 Anthropic API 的 prompt cache 持续命中。
> - **scanMemoryFiles 单 pass**：直接 `readFile` 读 frontmatter 前 30 行，比"stat → sort → read"少一半 syscall。
> - **MAX_MEMORY_FILES = 200**：硬截断防极端情况。
> - **预注入 manifest**：避免 forked agent 自己跑 `ls`——节省一轮。
> - **unref() timer**：timer 不阻塞 Node event loop，shutdown 时能立即退出。
> - **fs.watch recursive**：macOS FSEvents 只占 1-2 个 fd，对比 chokidar 的 O(N) fds 是巨大优势。
> - **bin-packing**：把 push 的 delta 按 200KB 切批上传，避免 gateway 限制。
> - **ETag 304**：服务端没变就 304 short-circuit，省一次下载。
> - **KAIROS 日志路径 pattern**：把"今天"作为变量描述在 prompt 里，让 prompt 内容稳定 → 命中 cache；date 改变不会 invalidate cache。
> - **drain 60s**：shutdown 时给在飞的提取留 60s 完成时间，软超时而非硬 kill。

---

## 12. 与 agent-dev 项目记忆系统对比要点

对比 [agent-dev 的 memory-system-design.md](memory-system-design.md)：

| 维度 | Claude Code（本文） | agent-dev |
| --- | --- | --- |
| **存储介质** | 文件 + frontmatter | 文件 + frontmatter（设计相同） |
| **分类法** | 四类固定（user/feedback/project/reference） | 概念相同，可能未严格落地 |
| **写入通道** | 主 agent 写 + 后台 forked agent 兜底（互斥） | 待对齐 |
| **跨会话整合** | autoDream（24h/5 sessions 门控） | 未实现 |
| **会话压缩** | SessionMemory + SessionMemoryCompact（5K/10K/40K token 阈值） | [context-management-implementation-design.md](context-management-implementation-design.md) v4 hard limit |
| **团队同步** | teamMemorySync（ETag + sha256 + 冲突重试 + secret 扫描 + symlink 防逃逸） | 未实现 |
| **Agent 隔离** | 三档 scope（user/project/local） | 未实现 |
| **漂移防护** | memoryAge.ts：>1 天追加 staleness 提示 | 待对齐 |
| **trusting recall** | 独立的 H1 section（位置很关键） | 待对齐 |
| **路径安全** | 6 层防御（settings trust、null byte、symlink、realpath、UTF NFC、separator suffix） | 较弱 |
| **写权限沙箱** | 严格 per-agent canUseTool | 较粗 |
| **key-binding UI** | MemoryFileSelector 列出所有记忆文件 + 三个开关 | 待对齐 |
| **遥测** | 大量 `tengu_*` 事件（selection rate、turn count、cache hit %） | 待对齐 |
| **graceful shutdown** | drainPendingExtraction + unref timer | 待对齐 |

### 可借鉴的实现要点

1. **后端提取互斥设计（`hasMemoryWritesSince`）**——主 agent 与后台 agent 不会双写。
2. **tool_use/tool_result 不能切开**（`adjustIndexToPreserveAPIInvariants`）——这是 streaming 消息切片的硬性约束。
3. **trusting recall 作为独立 section**——位置决定效果。
4. **Mtime = 锁 = 状态**——一个文件承担多个职责，减少 IO。
5. **greedy bin-packing**——上传按字节切片，避 gateway 413。
6. **secretScanner + symlink 防逃逸**——同步安全的两大支柱。
7. **`local-wins on conflict` 的取舍**——明确的工程决策。
8. **递归 `fs.watch`**——macOS FSEvents / Linux inotify 的 fd 数控制。
9. **KAIROS 模式的日志式追加**——长会话用 append-only 替代活索引。

> **逐点解释**：
> 1. **互斥写**：通过消息游标（cursor）判断主 agent 是否已写——避免双写冲突。
> 2. **API 不变量保护**：Anthropic API 要求 `tool_use` 必须有配对的 `tool_result`，且同一 message.id 的 blocks 不能被切开。压缩时必须检查。
> 3. **trusting recall 独立化**：A/B 测试验证——H1 独立 section 比 WHEN 段子项效果好得多（0/2 → 3/3）。
> 4. **mtime 多用途**：把"上次整合时间"和"当前锁状态"合并到一个 lock 文件的 mtime 上，省一次 IO。
> 5. **bin-packing**：上传按字节贪心装箱，避免触发服务端 body size 限制。
> 6. **secret + symlink 双重防线**：上传前扫描密钥、防恶意 symlink 覆盖敏感文件。
> 7. **local-wins**：push 触发于本地编辑，必须保留用户最新意图，即便 teammate 也推了更新。
> 8. **fs.watch recursive**：平台相关优化——macOS 用 FSEvents（O(1) fds），Linux 用 inotify（O(subdirs) fds）。
> 9. **KAIROS 日志**：长会话场景下，append-only 日志 + 定期蒸馏比维护 MEMORY.md 索引更可持续。

---

## 13. 关键源码位置速查

| 关注点 | 文件 |
| --- | --- |
| 路径解析、gate 优先级 | `src/memdir/paths.ts` |
| 系统提示词、ensureMemoryDirExists、truncateEntrypoint | `src/memdir/memdir.ts` |
| 四类分类法、frontmatter 示例、WHAT_NOT_TO_SAVE | `src/memdir/memoryTypes.ts` |
| 扫描器 + manifest 格式化 | `src/memdir/memoryScan.ts` |
| 按需召回（sideQuery 选 ≤5） | `src/memdir/findRelevantMemories.ts` |
| 漂移提示 | `src/memdir/memoryAge.ts` |
| Team 路径 + 6 层安全 | `src/memdir/teamMemPaths.ts` |
| 提取（后通道） | `src/services/extractMemories/extractMemories.ts` + `prompts.ts` |
| SessionMemory 提取 | `src/services/SessionMemory/sessionMemory.ts` + `sessionMemoryUtils.ts` + `prompts.ts` |
| SessionMemory 压缩 | `src/services/compact/sessionMemoryCompact.ts` |
| 跨会话整合 | `src/services/autoDream/autoDream.ts` + `config.ts` + `consolidationLock.ts` + `consolidationPrompt.ts` |
| 团队同步 | `src/services/teamMemorySync/index.ts` + `watcher.ts` + `secretScanner.ts` + `types.ts` |
| Agent 隔离 | `src/tools/AgentTool/agentMemory.ts` |
| UI 选择器 | `src/components/memory/MemoryFileSelector.tsx` |
| 任务系统（DreamTask） | `src/tasks/DreamTask/DreamTask.ts` |

> **解释**：本表按"主题 → 源文件"映射，便于按需查阅源码。每个模块的边界很清晰——memdir/* 是核心数据层、services/* 是后台服务层、tools/AgentTool/* 是工具集成层、components/memory/* 是 UI 层、tasks/* 是任务系统。

---

## 14. 一句话总结

Claude Code 的记忆系统是一个**多时间尺度、多权限沙箱、严格四类分类法、跨进程/跨设备同步**的文件型记忆体系：

- **L1 即时**（MEMORY.md + CLAUDE.md）→ **L2 按需**（findRelevantMemories 5 选 1）→ **L3 会话压缩**（SessionMemory）→ **L4 提取**（extractMemories）→ **L5 整合**（autoDream）→ **L6 同步**（teamMemorySync），每一层都有独立的 gate、独立的工具沙箱、独立的失败兜底策略，且路径安全纵深防御 6 层。

> **总结解释**：
> - **多时间尺度**：从"当前 turn"（MEMORY.md）到"最近 N 个 session"（autoDream），覆盖从秒到天的所有尺度。
> - **多权限沙箱**：每个层（SessionMemory / extract / autoDream）有自己的工具白名单，越往下越宽松。
> - **严格四类分类法**：user / feedback / project / reference 封闭枚举，LLM 无法发明第五类。
> - **跨进程/跨设备同步**：team memory 在 GitHub OAuth + ETag + per-entry sha256 上做实时同步。
> - **6 层路径安全**：从字符串层到 symlink 层到 separator suffix，每一层防一种攻击向量。
