# Agent 会话管理：需要具备哪些功能

> 基于 Claude Code 源码深度解析，提取会话管理系统的完整功能清单
>
> 源码文件：`sessionStorage.ts`（~1600行）+ `sessionRestore.ts` + `sessionHistory.ts` + `sessionState.ts`
>
> 版本：v1.0 | 日期：2026-06-11

---

## 一、会话管理的核心定位

**会话管理不是"聊天记录存磁盘"**，而是 Agent 系统的**连续性基础设施**。它解决的问题是：

```
用户启动一个 Agent 会话 → 做一系列操作 → 关闭终端 → 
第二天回来 → 继续昨天的工作，好像没中断过
```

Claude Code 的会话管理覆盖了从**创建→持久化→恢复→并发→清理**的完整生命周期。

---

## 二、功能全景图（10 大模块）

```
┌──────────────────────────────────────────────────────────────┐
│                     会话管理功能全景                            │
├──────────────────────────────────────────────────────────────┤
│ 模块1: 会话生命周期管理    创建/切换/关闭/分叉/并发               │
│ 模块2: 消息存储与加载     JSONL 持久化/渐进加载/去重             │
│ 模块3: 元数据管理         标题/标签/模型/模式/权限/状态          │
│ 模块4: 上下文压缩         自动压缩/摘要重建/状态恢复              │
│ 模块5: 状态恢复           Resume/Continue/Fork 语义            │
│ 模块6: 进度追踪           实时状态/待办事项/文件历史             │
│ 模块7: 并发会话           多会话并行/命名/切换                   │
│ 模块8: 外部持久化         CCR 远程同步/多端同步                   │
│ 模块9: 清理与归档         TTL/自动清理/会话归档                 │
│ 模块10: 跨会话记忆        MEMORY.md/记忆提取/偏好保存           │
└──────────────────────────────────────────────────────────────┘
```

---

## 三、逐模块功能详解

### 模块 1：会话生命周期管理

#### 1.1 会话创建

| 功能 | Claude Code 实现 | 设计意图 |
|------|----------------|---------|
| **自动分配 Session ID** | UUID v4，每会话唯一 | 全局唯一标识 |
| **基于项目目录隔离** | `~/.claude/projects/<sanitized-cwd>/` | 防止不同项目会话混在一起 |
| **延迟创建文件** | `sessionFile = null`，首条消息才 materialize | 避免创建空会话文件 |
| **启动元数据缓存** | agentType/model/mode 先缓存，退出时写入 | 效率 + 原子性 |

#### 1.2 会话切换

```typescript
switchSession(sessionId, projectDir?)
```

| 功能 | 说明 |
|------|------|
| 切换到已存在会话 | `--resume` 场景 |
| 跨项目切换 | 通过 `projectDir` 参数 |
| 原子性 | sessionId + sessionProjectDir 同时更新 |

#### 1.3 会话分叉（Fork）

```typescript
// Fork 语义：继承上下文，全新文件
- 新 Session ID
- 消息通过 recordTranscript 复制到新文件
- content-replacement 记录需要手动 seed
- 不继承工作树状态（防止误删）
```

#### 1.4 并发会话

```typescript
// 多会话并行运行，互不干扰
updateSessionName(result.agentName)  // 侧边栏显示名称
```

---

### 模块 2：消息存储与加载（最核心模块）

#### 2.1 存储格式：JSONL

```typescript
// 每条消息一行 JSON，append-only
{ "type": "user", "uuid": "...", "parentUuid": "...", ... }
{ "type": "assistant", "uuid": "...", "parentUuid": "...", ... }
{ "type": "tool_result", "uuid": "...", "parentUuid": "...", ... }
```

**为什么用 JSONL 而非 JSON 数组？**

| 特性 | JSON 数组 | JSONL |
|------|---------|------|
| 追加写入 | 全量 rewrite | `appendFile`，O(1) |
| 尾部读取 | O(n) | `tail -n 100`，O(1) |
| 流式处理 | 需要全量解析 | 按行解析 |
| 并发追加 | 文件锁 | 天然并发安全（每行独立） |
| 损坏容错 | 整个文件失效 | 单行损坏不影响其他行 |

#### 2.2 消息去重

```typescript
// 压缩后新消息的 UUID 可能与压缩前相同
// → appendEntry 检查 UUID 是否已存在
const isNewUuid = !messageSet.has(entry.uuid)
if (isAgentSidechain || isNewUuid) {
  void this.enqueueWrite(targetFile, entry)
}
```

#### 2.3 parentUuid 链

```typescript
// 每条消息通过 parentUuid 形成链
// compact boundary 的消息 parentUuid = null（断链）
// resume 时重建链，遇到断链停止

if (isCompactBoundary) {
  parentUuid = null  // 断链
} else {
  parentUuid = message.uuid  // 续链
}
```

#### 2.4 渐进加载策略

```typescript
// 问题：会话可能达到 GB 级别
// 解决：分阶段加载

loadFullLog() // 加载全部（首次）
  ↓
loadMessages() // 渐进加载尾部
  ↓
readLiteMetadata() // 仅加载尾部 64KB（picker 显示）
  ↓
getTranscriptPath() // 仅获取路径（hooks）
```

| 场景 | 策略 | 读取量 |
|------|------|--------|
| 首次 resume | 全量加载 + 尾部 64KB | ~完整文件 |
| 增量 resume | 仅加载未压缩的尾部 | ~最新部分 |
| Picker 列表 | 仅尾部元数据 | 64KB |
| Hook 执行 | 仅获取路径 | 0（纯元数据） |

#### 2.5 临时删除（Tombstone）

```typescript
// 场景：流式写入失败，需要删除最后一条消息
// 方法：尾部 64KB 范围内，逐行定位 + splice
// 超出范围：全量 rewrite（慢路径）

if (fileSize > MAX_TOMBSTONE_REWRITE_BYTES) {
  // 跳过，避免 OOM
}
```

#### 2.6 写队列与批量刷新

```typescript
// 问题：高频消息写入导致磁盘 IO 瓶颈
// 解决：100ms 批量刷新，写队列合并

private writeQueues = new Map<string, Array<{ entry: Entry; resolve: () => void }>>()
// 每 100ms drain 一次
// 超过 100MB 切 chunk（新文件？）
```

---

### 模块 3：元数据管理

#### 3.1 元数据类型

| 元数据类型 | 内容 | 写入时机 |
|-----------|------|---------|
| `custom-title` | 用户自定义标题 | 任意时刻 |
| `ai-title` | AI 生成标题 | 首条消息后 |
| `tag` | 会话标签 | 任意时刻 |
| `agent-name` | Agent 名称 | 会话期间 |
| `agent-color` | Agent 颜色 | 会话期间 |
| `agent-setting` | Agent 类型 | 会话期间 |
| `mode` | Coordinator/Normal | 模式切换 |
| `last-prompt` | 最后用户输入 | 每轮更新 |
| `worktree-state` | 工作树状态 | 进/出工作树 |
| `pr-link` | PR 链接 | 创建 PR 后 |

#### 3.2 元数据的原子性保证

```typescript
// 退出时重新追加元数据到尾部
reAppendSessionMetadata()
// 确保元数据在 64KB 尾部窗口内
// → readLiteMetadata 一定能读到最新标题
```

#### 3.3 外部写入刷新

```typescript
// 外部进程（SDK renameSession）可能修改会话文件
// 退出时先读取尾部，再刷新缓存，最后重新追加
const tailLines = tail.split('\n')
const titleLine = tailLines.findLast(l => l.startsWith('{"type":"custom-title"'))
if (tailTitle !== undefined) {
  this.currentSessionTitle = tailTitle || undefined
}
// 以尾部最新值为准
```

---

### 模块 4：上下文压缩（会话管理的核心能力）

参见 `claude-code-context-management-design.md`，核心功能：

| 功能 | 说明 |
|------|------|
| 自动触发 | Token 预算耗尽 |
| 压缩策略 | BASE / PARTIAL / UP_TO 三种 |
| 摘要格式 | 9 段式结构 |
| 防漂移机制 | verbatim quotes |
| 状态重建 | 摘要 → 消息链 |
| 压缩边界 | parentUuid = null |

---

### 模块 5：状态恢复（Resume / Continue / Fork）

#### 5.1 三种恢复语义

```typescript
type ResumeType = 'resume' | 'continue' | 'fork'

// resume：回到上次会话的完整状态
// - 加载全部历史
// - 恢复文件历史、属性、待办
// - 切换 sessionId

// continue：继续当前会话的未完成消息
// - 加载尾部消息
// - 基于 parentUuid 续链

// fork：从源会话复制消息到新会话
// - 消息通过 recordTranscript 复制
// - content-replacement 需要手动 seed
// - 不继承 worktree 状态
```

#### 5.2 恢复流程

```typescript
// sessionRestore.ts 的恢复流程
processResumedConversation()
  1. 匹配 coordinator/normal 模式
  2. 切换 sessionId
  3. 恢复会话元数据
  4. 恢复 worktree 工作目录
  5. adoptResumedSessionFile（指向旧文件）
  6. 恢复 context-collapse 状态
  7. 恢复 agent 设置
  8. 保存当前模式
```

#### 5.3 Agent 恢复

```typescript
// 如果会话使用了自定义 agent，恢复它
restoreAgentFromSession(agentSetting, currentAgentDef, agentDefs)
// - 找到对应的 agent 定义
// - 恢复 agentType
// - 恢复模型（除非用户指定了 --agent）
```

---

### 模块 6：进度追踪

#### 6.1 会话状态机

```typescript
type SessionState = 'idle' | 'running' | 'requires_action'

// 状态转换触发器
notifySessionStateChanged(state, details?)
  → 通知 UI 更新（侧边栏 badge）
  → 写入 external_metadata
  → 推送 SDK 事件（scmuxd/VS Code）
```

#### 6.2 requires_action 详情

```typescript
type RequiresActionDetails = {
  tool_name: string
  action_description: string  // "Editing src/foo.ts"
  tool_use_id: string
  request_id: string
  input?: Record<string, unknown>  // 工具参数
}
// → 前端可以显示具体在等待什么
```

#### 6.3 待办事项恢复

```typescript
// 从 transcript 提取最后的 TodoWrite tool_use
extractTodosFromTranscript(messages)
// → 恢复 AppState.todos
```

#### 6.4 文件历史快照

```typescript
// 每次操作后记录文件状态快照
insertFileHistorySnapshot(messageId, snapshot, isUpdate)
// → resume 时恢复文件历史
```

---

### 模块 7：并发会话管理

#### 7.1 会话列表

```typescript
// 每个项目目录下的 .jsonl 文件 = 一个会话
getProjectDir(cwd) → join(projectsDir, sanitizePath(cwd))
// → ls 项目目录 → 所有 .jsonl → 会话列表
```

#### 7.2 轻量元数据读取

```typescript
// 仅读取尾部 64KB，提取标题/标签/最后操作
readLiteMetadata(sessionFile)
  → customTitle
  → tag
  → lastPrompt
  → agentName
```

#### 7.3 会话命名

```typescript
updateSessionName(name)  // 侧边栏显示
generateSessionTitle(description, signal)  // Haiku 生成
```

---

### 模块 8：外部持久化（CCR 远程同步）

#### 8.1 双写架构

```typescript
// 本地：appendEntry → JSONL 文件
// 远程：persistToRemote → Session Ingress API

// CCR v2 路径
if (this.internalEventWriter) {
  await this.internalEventWriter('transcript', entry, ...)
  return
}

// v1 Session Ingress 路径
await sessionIngress.appendSessionLog(sessionId, entry, remoteIngressUrl)
```

#### 8.2 多端同步

```typescript
// CCR Session History API
fetchLatestEvents(ctx, limit)  // 分页加载历史
fetchOlderEvents(ctx, beforeId, limit)  // 翻页
// → web/移动端可以看到 CLI 会话
```

---

### 模块 9：清理与归档

#### 9.1 清理策略

```typescript
// 清理策略
getSettings_DEPRECATED()?.cleanupPeriodDays === 0
  → 禁用会话持久化（--no-session-persistence）

isEnvTruthy(process.env.CLAUCE_CODE_SKIP_PROMPT_HISTORY)
  → 跳过所有写入

// 测试环境自动跳过
getNodeEnv() === 'test' && !allowTestPersistence
```

#### 9.2 退出清理钩子

```typescript
registerCleanup(async () => {
  await project?.flush()  // 刷新写队列
  project?.reAppendSessionMetadata()  // 重新追加元数据
})
```

---

### 模块 10：跨会话记忆

这是会话管理与记忆系统的交叉地带：

| 功能 | 说明 |
|------|------|
| MEMORY.md 索引 | 每会话有独立的会话记忆文件 |
| 自动提取 | 提取用户偏好、反馈、项目知识 |
| 四类记忆 | user / feedback / project / reference |
| 持久化位置 | `~/.claude/projects/<slug>/memory/` |

---

## 四、JSONL Entry 类型完整清单

```typescript
// 消息类
type Entry = 
  | { type: 'user', uuid, parentUuid, ... }
  | { type: 'assistant', uuid, parentUuid, ... }
  | { type: 'tool_result', uuid, parentUuid, ... }
  | { type: 'system', uuid, ... }
  | { type: 'attachment', uuid, ... }
  // 元数据类
  | { type: 'custom-title', customTitle, sessionId }
  | { type: 'ai-title', aiTitle, sessionId }
  | { type: 'tag', tag, sessionId }
  | { type: 'last-prompt', lastPrompt, sessionId }
  | { type: 'agent-name', agentName, sessionId }
  | { type: 'agent-color', agentColor, sessionId }
  | { type: 'agent-setting', agentSetting, sessionId }
  | { type: 'mode', mode, sessionId }
  | { type: 'worktree-state', worktreeSession, sessionId }
  | { type: 'pr-link', prNumber, prUrl, prRepository, sessionId }
  // 压缩类
  | { type: 'summary', summary, ... }
  | { type: 'compact-boundary', ... }
  // 追踪类
  | { type: 'file-history-snapshot', ... }
  | { type: 'content-replacement', ... }
  | { type: 'context-collapse-commit', ... }
  | { type: 'context-collapse-snapshot', ... }
```

---

## 五、给 agent-dev 项目的功能清单

### P0（必须实现）

| # | 功能 | 优先级原因 |
|---|------|-----------|
| 1 | JSONL 消息持久化 | 基础中的基础 |
| 2 | Session ID 管理 | 全局唯一标识 |
| 3 | 消息链（parentUuid） | Resume/Continue 的前提 |
| 4 | 延迟创建文件 | 避免空文件 |
| 5 | 元数据管理（标题/标签） | 会话可识别性 |
| 6 | 基本 Resume | 核心用户体验 |
| 7 | Token 预算 + 自动压缩 | 无限上下文的保障 |

### P1（生产级）

| # | 功能 | 说明 |
|---|------|------|
| 8 | 渐进加载（首尾分离） | 大会话不 OOM |
| 9 | 写队列 + 批量刷新 | 减少磁盘 IO |
| 10 | Fork 会话 | 并行探索 |
| 11 | 进度状态机 | UI 状态同步 |
| 12 | 工作树状态持久化 | Git worktree 隔离 |
| 13 | 压缩边界断链 | 正确的消息链 |
| 14 | 外部持久化（可选） | 多端同步 |

### P2（高级）

| # | 功能 | 说明 |
|---|------|------|
| 15 | 并发会话管理 | 多会话并行 |
| 16 | AI 生成标题 | 自动命名 |
| 17 | 远程 Session History API | Web 端查看 |
| 18 | Tombstone 临时删除 | 流式容错 |
| 19 | Context Collapse | 更高阶压缩 |
| 20 | 跨会话 MEMORY | 长期偏好 |

---

## 六、核心设计原则

```
1. 延迟优于提前 — 不创建不需要的文件
2. Append-only — 追加写入优于全量重写
3. 尾部元数据原子性 — 退出时 re-append 保证元数据在尾部窗口
4. 消息链是恢复的基础 — parentUuid 链断了就丢了历史
5. Fork 不继承危险状态 — worktree 状态不继承，防止误删
6. 外部写入要刷新 — 重新读取尾部，以最新值为准
7. 写队列合并 — 100ms 批量刷新，减少 IO
8. 压缩边界断链 — compact boundary 消息 parentUuid = null
```

---

## 附录：文件索引

| 源码文件 | 行数 | 核心职责 |
|---------|------|---------|
| `sessionStorage.ts` | ~1600 | JSONL 写入/读取/去重/队列 |
| `sessionRestore.ts` | ~500 | Resume/Continue/Fork 流程 |
| `sessionState.ts` | ~150 | 状态机 + metadata 通知 |
| `sessionHistory.ts` | ~150 | 远程历史 API 分页 |
| `sessionTitle.ts` | ~150 | Haiku 标题生成 |
| `sessionActivity.ts` | ~100 | 会话活跃度追踪 |
| `sessionStoragePortable.ts` | ~300 | 跨平台兼容工具函数 |

---

> 文档生成时间：2026-06-11
> 基于 Claude Code 源码深度解析
> 适用项目：agent-dev