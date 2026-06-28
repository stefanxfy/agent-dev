# Claude Code 安全沙箱（Sandbox）实现深度解读

> 本文档基于对 Anthropic 发布的 Claude Code TypeScript 源码（`src/`）的静态阅读整理。
> 涉及的核心实现文件共约 **2400 行**，覆盖配置层、运行时适配层、调用接入层、UI 展示层以及命令交互层。

---

## 一、总体定位

Claude Code 的 "Sandbox" 是**本地操作系统级**的命令隔离机制，**不是** Docker / 虚拟机 / gVisor 之类用户态容器。其核心目标：

> 把 BashTool 派生的子进程放进一个受限的 OS 进程沙箱，限制**文件系统写读**与**网络出口**，对破坏性命令提供可降级的执行路径。

沙箱功能由两部分协作完成：

| 层级 | 模块 | 角色 |
|------|------|------|
| 外层 | Claude Code CLI 自身（`src/utils/sandbox/`、`src/tools/BashTool/`、`src/commands/sandbox-toggle/`、`src/components/sandbox/`） | 配置定义、设置合并、决策点、UI、用户交互、Prompt 注入 |
| 内层 | `@anthropic-ai/sandbox-runtime`（外部 npm 包，作为依赖引入，源码不在本仓库） | 实际执行 OS 级隔离：macOS Seatbelt / Linux bubblewrap (bwrap) + socat + 可选 seccomp |

`src/utils/sandbox/sandbox-adapter.ts` 是**适配层**，把 Claude Code 自身的设置（`permissions.allow/deny`、`sandbox.filesystem.*`、`sandbox.network.*`、`excludedCommands` 等）翻译成 `sandbox-runtime` 所需的 `SandboxRuntimeConfig`，并在其上叠加 Claude Code 专属逻辑（路径模式解析、bare-git-repo 防护、worktree 探测等）。

---

## 二、运行机制总览

```text
                  +-----------------------------------------+
   user input --> |  BashTool.call (src/tools/BashTool)     |
                  |  - BashToolInput { command,             |
                  |      dangerouslyDisableSandbox? }        |
                  +-----------------------------------------+
                                  |
                                  v
                  +-----------------------------------------+
                  |  shouldUseSandbox(input)                |
                  |  (src/tools/BashTool/shouldUseSandbox)  |
                  |   - isSandboxingEnabled()?              |
                  |   - dangerouslyDisableSandbox +         |
                  |     areUnsandboxedCommandsAllowed()?    |
                  |   - containsExcludedCommand?            |
                  +-----------------------------------------+
                                  | true
                                  v
                  +-----------------------------------------+
                  |  Shell.exec (src/utils/Shell.ts)        |
                  |  -> SandboxManager.wrapWithSandbox(     |
                  |       command, shellPath, ...)          |
                  +-----------------------------------------+
                                  |
                                  v
        +-----------------------------------------------------------+
        | @anthropic-ai/sandbox-runtime                              |
        |   BaseSandboxManager.wrapWithSandbox (mac seatbelt /      |
        |      linux bwrap+socat [+ seccomp])                        |
        |                                                           |
        |   - allowWrite/denyWrite/allowRead/denyRead (FS)           |
        |   - allowedHosts/deniedHosts + http/socks proxy (NET)      |
        |   - allowUnixSockets / allowAllUnixSockets (mac only)      |
        |   - ripgrep 用于危险目录扫描                                |
        +-----------------------------------------------------------+
                                  |
                                  v
                  +-----------------------------------------+
                  |  Shell.exec -- result.then(() =>         |
                  |      SandboxManager.cleanupAfterCommand |
                  |      + scrubBareGitRepoFiles            |
                  +-----------------------------------------+
                                  |
                                  v
                  +-----------------------------------------+
                  |  BashTool: annotateStderrWithSandboxFai |
                  |  lures -> 把 sandbox 违规注入 stderr    |
                  +-----------------------------------------+
```

---

## 三、配置体系（`src/entrypoints/sandboxTypes.ts`）

所有沙箱相关设置都通过一个共享的 Zod schema 定义在 `sandboxTypes.ts`，**这是 SDK 和 settings 校验共同的单一事实源**：

```ts
SandboxSettingsSchema = z.object({
  enabled?: boolean,
  failIfUnavailable?: boolean,    // 企业部署的硬开关
  autoAllowBashIfSandboxed?: boolean,
  allowUnsandboxedCommands?: boolean, // 控制 dangerouslyDisableSandbox 是否可用
  network?: {
    allowedDomains?: string[],
    allowManagedDomainsOnly?: boolean,    // 仅允许 policy 设置中的域名
    allowUnixSockets?: string[],          // macOS only
    allowAllUnixSockets?: boolean,
    allowLocalBinding?: boolean,
    httpProxyPort?: number,
    socksProxyPort?: number,
  },
  filesystem?: {
    allowWrite?: string[],
    denyWrite?: string[],
    denyRead?: string[],
    allowRead?: string[],
    allowManagedReadPathsOnly?: boolean,
  },
  ignoreViolations?: Record<string, string[]>,
  enableWeakerNestedSandbox?: boolean,
  enableWeakerNetworkIsolation?: boolean,  // 允许 trustd，gh/gcloud 等用得上，**降低安全**
  excludedCommands?: string[],
  ripgrep?: { command: string, args?: string[] }, // 自定义 ripgrep
  // 通过 .passthrough() 透传未文档化的字段，如 enabledPlatforms
}).passthrough()
```

### 3.1 路径语义在 Permission Rule 与 `sandbox.filesystem` 中不同

代码内注释（`sandbox-adapter.ts:99-146`）明确区分两种语义：

| 来源 | `/path` 语义 | 实现 |
|------|------------|------|
| `permissions.allow/deny` 中的 Edit/Read 规则 | 相对于该设置所在目录 | `resolvePathPatternForSandbox`（`sandbox-adapter.ts:99`） |
| `sandbox.filesystem.*` 设置项 | 绝对路径（按字面意思） | `resolveSandboxFilesystemPath`（`sandbox-adapter.ts:138`） |

注释 `#30067` 提到这两套语义混用曾导致用户 bug，因此分别处理；`//path` 兼容层始终被还原为 `/path` 绝对路径。

---

## 四、核心适配器：`sandbox-adapter.ts`（985 行）

这是沙箱系统的中枢。它做四件事：**配置翻译**、**生命周期管理**、**用户通知**、**安全加固**。

### 4.1 配置翻译：`convertToSandboxRuntimeConfig`（第 172-381 行）

将 Claude Code 的多源设置合并成 `SandboxRuntimeConfig`：

1. **网络白/黑名单**
   - `allowedDomains`：从 `sandbox.network.allowedDomains` + 所有 settings 源中 `WebFetch(domain:...)` 形式的 `permissions.allow` 规则收集
   - `deniedDomains`：仅来自 `permissions.deny` 中 `WebFetch(domain:...)`
   - 当 `allowManagedDomainsOnly` 开启时**只**采用 `policySettings` 来源（企业部署场景）

2. **文件系统**
   - `allowWrite` 初始值固定为 `['.', getClaudeTempDir()]`（cwd + 沙箱内可写的 temp 目录）
   - 从所有 `SETTING_SOURCES` 中收集 Edit 工具的 `permissions.allow` 规则写入 `allowWrite`
   - 收集 Edit deny 写入 `denyWrite`，Read deny 写入 `denyRead`
   - 收集 `sandbox.filesystem.allowWrite/denyWrite/denyRead/allowRead`
   - **`managedReadPathsOnly`** 开关时 `allowRead` 仅采用 `policySettings` 来源

3. **总是被拒绝的写路径（硬编码于 `convertToSandboxRuntimeConfig` 中）**
   - 所有 `SETTING_SOURCES` 的 `settings.json` 与 `managed settings drop-in` 目录
   - `cwd !== originalCwd` 时 cwd 下的 `.claude/settings.json` / `.claude/settings.local.json`
   - 原 cwd 与 cwd 下的 `.claude/skills`（注释：`#29316`，skills 同 commands/agents 同级权限）
   - **Bare-git 防御**（anthropics/claude-code#29316）：若 cwd 中存在 `HEAD`、`objects`、`refs`、`hooks`、`config`，放入 `denyWrite`；否则放入 `bareGitRepoScrubPaths`，由 `cleanupAfterCommand` 同步清理（避免 bwrap 在不存在的路径上挂 0 字节文件导致 git 假阳性）。这是一道针对 git 的逃逸攻击的修复。

4. **Git worktree 兼容**：启动时一次性调用 `detectWorktreeMainRepoPath`，把主 repo 的 `.git` 加入 `allowWrite`（worktree 操作需要写主 repo 索引）。

5. **`additionalDirectories`**：`--add-dir` CLI 参数或 `/add-dir` 命令加入的目录必须放入 `allowWrite` 才能被沙箱内 bash 访问。

6. **Ripgrep 配置**：用户自定义优先，否则使用 `ripgrepCommand()` 返回的 Claude Code 自带 rg。

### 4.2 生命周期

| 方法 | 行号 | 职责 |
|------|------|------|
| `isSandboxingEnabled()` | 532 | 检查平台支持、依赖、`enabledPlatforms`、用户开关 |
| `isSupportedPlatform()` | 491 (memoize) | macOS / Linux / WSL2+，WSL1 不支持 |
| `checkDependencies()` | 451 (memoize) | 调 `BaseSandboxManager.checkDependencies`，返回 `{errors, warnings}` |
| `getSandboxUnavailableReason()` | 562 | 当用户**显式开启**但跑不起来时返回可读原因（修复 #34044 的"零反馈"安全足枪） |
| `isPlatformInEnabledList()` | 505 | 读取未文档化设置 `enabledPlatforms`（为 NVIDIA 等企业只允许 macOS 场景设计） |
| `initialize(cb)` | 730 | 单例 Promise；解析 worktree 路径、构建配置、调 `BaseSandboxManager.initialize`、订阅 settings 变更 |
| `refreshConfig()` | 798 | 同步重读设置 → `BaseSandboxManager.updateConfig` |
| `wrapWithSandbox(cmd, shell, cfg, sig)` | 704 | 等初始化完成后转发到 runtime |
| `cleanupAfterCommand()` | 963 | 调用 runtime 自带清理 + 自定义 `scrubBareGitRepoFiles()`（bare-git 防护） |
| `reset()` | 808 | 取消订阅、清缓存、清 worktree/bare-git 状态、转发给 runtime |

`isSandboxingEnabled` 是层层短路：

```text
isSupportedPlatform() && checkDependencies().errors.length === 0 &&
isPlatformInEnabledList() && getSandboxEnabledSetting()
```

### 4.3 用户策略的"锁定"判断

`areSandboxSettingsLockedByPolicy()`（647 行）：检查 `flagSettings` 与 `policySettings` 中是否设置了 `sandbox.enabled` / `autoAllowBashIfSandboxed` / `allowUnsandboxedCommands`。一旦被这两个高优先级源写过，`localSettings` 就改不动了。

`setSandboxSettings()`（669 行）只写 `localSettings`，**不会**覆盖被策略锁定的字段。

### 4.4 Linux 平台的特殊警告

`getLinuxGlobPatternWarnings()`（597 行）：bwrap 不支持 glob，所以含有 `*`/`?`/`[` 的 Edit/Read 规则在 Linux/WSL 上是**只对部分路径生效**的——它会扫描用户的 settings 并把所有包含 glob 的规则列出来，让 `/sandbox` 页面与启动时给出 warning。

---

## 五、决策点：`shouldUseSandbox`（`src/tools/BashTool/shouldUseSandbox.ts`）

入口逻辑（130-153 行）：

```ts
export function shouldUseSandbox(input) {
  if (!SandboxManager.isSandboxingEnabled()) return false   // 全局开关
  if (input.dangerouslyDisableSandbox &&
      SandboxManager.areUnsandboxedCommandsAllowed()) return false  // 模型主动绕过
  if (!input.command) return false
  if (containsExcludedCommand(input.command)) return false  // 用户排除
  return true
}
```

### 5.1 `containsExcludedCommand`：防御性的子命令拆分

注释明确说明 `excludedCommands` **不是**安全边界，只是用户体验优化——真正安全靠的是 sandbox 权限弹窗。因此它的实现重点是**减少漏判**：

1. **拆分复合命令**（`splitCommand_DEPRECATED`）：避免 `docker ps && curl evil.com` 这种前半部分命中 `excluded` 但实际想逃逸的情况。注释里特别强调 "`&&`-split already lets `export FOO=bar && bazel ...` match"。
2. **多轮剥离（固定点迭代）**：对每个子命令的 candidate 同时应用
   - `stripAllLeadingEnvVars(cmd, BINARY_HIJACK_VARS)`：`FOO=bar bazel` → `bazel`
   - `stripSafeWrappers(cmd)`：`timeout 30 bazel` → `bazel`
   直到不再产生新 candidate；处理 `timeout 300 FOO=bar bazel run` 这种交错前缀。
3. **模式匹配**：把用户的 pattern 当作 `bashPermissionRule` 解析，按 `prefix` / `exact` / `wildcard` 分别匹配。
4. **动态 disabled 列表**：在 `process.env.USER_TYPE === 'ant'` 时，从 GrowthBook 读取 `tengu_sandbox_disabled_commands`，按 substrings 和 commands 双向黑名单检查。

---

## 六、调用接入：`BashTool` 与 `Shell.exec`

### 6.1 BashTool 层（`src/tools/BashTool/BashTool.tsx`）

1. **UI 标识**（502 行）：当 `CLAUDE_CODE_BASH_SANDBOX_SHOW_INDICATOR` 环境变量为真且 `shouldUseSandbox(input)` 为真时，UI 中 Bash 工具名变为 `SandboxedBash`——给截图/演示用的可观测标记。注释 `#21605` 提到一个 bug：因 `userFacingName` 每条消息都渲染一次，`splitCommand_DEPRECATED` 内部 `new RegExp` 的开销会让 50+ 消息场景下 shimmer tick 抛错导致死循环，所以这里加了缓存。

2. **执行链路**（881-898 行）：调 `exec(command, signal, 'bash', { shouldUseSandbox: shouldUseSandbox(input) })`。

3. **自动放行（auto-allow）**（`bashPermissions.ts:1829-1843`）：当 `isSandboxingEnabled && isAutoAllowBashIfSandboxedEnabled && shouldUseSandbox(input)` 三个条件都满足时，**先**走 `checkSandboxAutoAllow` 子流程检查显式 deny/ask 规则。这一步绕过传统 Bash 权限弹窗——因为已经决定沙箱里跑，安全风险由沙箱兜底。

4. **结果回注**（710 行）：`SandboxManager.annotateStderrWithSandboxFailures(input.command, stdout)`——把 `sandbox-runtime` 写入 violation store 的违规事件附加到 stdout 上展示给用户与模型。

### 6.2 Shell.exec 层（`src/utils/Shell.ts`）

关键路径 259-273 行：

```ts
if (shouldUseSandbox) {
  commandString = await SandboxManager.wrapWithSandbox(
    commandString, sandboxBinShell, undefined, abortSignal,
  )
  await fs.mkdir(sandboxTmpDir, { mode: 0o700 })
}
```

随后用 `spawn(spawnBinary, shellArgs, ...)` 真正执行。注释 247-257 行解释了一个特殊路径：PowerShell 走 `pwsh -NoProfile -NonInteractive -EncodedCommand <base64>`（base64 抗 `shellquote.quote`），但因为 `wrapWithSandbox` 内部硬编码 `<binShell> -c '<cmd>'`，所以**外层再套一层 `/bin/sh -c`**，让 sandbox-runtime 跑 sh，sh 再 exec 已经 base64 化的 pwsh 命令。这是为了保留 `-NoProfile -NonInteractive`，否则 pwsh 进沙箱后会触发 profile 加载，延迟 / 多余输出 / 挂起。

`cleanupAfterCommand`（391-393 行）在 shell 命令 promise resolve 后**同步**调用——注释 386-393 解释了为什么必须同步：bwrap 在 Linux 上会留下 0 字节的 dotfile stub（`.bashrc`、`.HEAD` 等），调用方 `await shellCommand.result` 时必须立刻看到干净的 cwd。

`sandboxTmpDir`（204-207 行）按用户 UID 命名（`/tmp/claude-<uid>`），并以 `0o700` 创建，避免多用户权限冲突。

---

## 七、Prompt 注入：`BashTool/prompt.ts`

沙箱约束不仅在 OS 层执行，还在 prompt 中告知模型，让 LLM 知道边界。`getSimpleSandboxSection`（172-273 行）拼出：

```
## Command sandbox
By default, your command will be run in a sandbox. This sandbox controls
which directories and network hosts commands may access or modify without
an explicit override.

The sandbox has the following restrictions:
Filesystem: { "read": {...}, "write": {...} }
Network: { "allowedHosts": [...], "deniedHosts": [...], ... }
Ignored violations: {...}

- You should always default to running commands within the sandbox.
  Do NOT attempt to set `dangerouslyDisableSandbox: true` unless:
  - The user *explicitly* asks you to bypass sandbox
  - A specific command just failed and you see evidence of sandbox restrictions causing the failure.
  Evidence of sandbox-caused failures includes:
    - "Operation not permitted" errors for file/network operations
    - Access denied to specific paths outside allowed directories
    - Network connection failures to non-whitelisted hosts
    - Unix socket connection errors
  When you see evidence of sandbox-caused failure:
    - Immediately retry with `dangerouslyDisableSandbox: true` (don't ask, just do it)
    - Briefly explain what sandbox restriction likely caused the failure. ...
- Treat each command you execute with `dangerouslyDisableSandbox: true` individually.
- Do not suggest adding sensitive paths like ~/.bashrc, ~/.zshrc, ~/.ssh/*, or credential files to the sandbox allowlist.

- For temporary files, always use the `$TMPDIR` environment variable. TMPDIR is automatically
  set to the correct sandbox-writable directory in sandbox mode. Do NOT use `/tmp` directly -
  use `$TMPDIR` instead.
```

`$TMPDIR` 在 `prompt.ts` 中被刻意替换成字面量以让 prompt 在不同用户间**可缓存**（全局 prompt 缓存命中率），但 runtime 启动时会把 `$TMPDIR` 指向 `sandboxTmpDir`。

注意：`allowUnsandboxedCommands === false` 时，prompt 切换成"严格模式"版本，告知模型 `dangerouslyDisableSandbox` 在策略层被禁用。

---

## 八、UI 层（`src/components/sandbox/`、`src/components/permissions/`）

### 8.1 `SandboxSettings`（`SandboxSettings.tsx`）

`/sandbox` 命令的主面板，使用 Tabs 渲染：

- **Mode Tab**：三档 `auto-allow` / `regular` / `disabled`，分别写入 `enabled + autoAllowBashIfSandboxed` 两个字段
- **Overrides Tab**（`SandboxOverridesTab.tsx`）：切换 `allowUnsandboxedCommands`（open = 允许 fallback，closed = 严格模式）。当策略锁定时只读。
- **Config Tab**（`SandboxConfigTab.tsx`）：只读展示当前 `getFsReadConfig` / `getFsWriteConfig` / `getNetworkRestrictionConfig` / `getAllowUnixSockets` / `getExcludedCommands`；同时检查 Linux glob 模式不兼容问题并给 warning。
- **Dependencies Tab**（`SandboxDependenciesTab.tsx`）：列出 seatbelt（macOS 内建）/ ripgrep / bwrap / socat / seccomp 的状态与安装提示。

当有 errors（如 bwrap 缺失）时只显示 Dependencies Tab；如果仅 warnings 则全显示。

### 8.2 `SandboxPermissionRequest`（`src/components/permissions/SandboxPermissionRequest.tsx`）

当沙箱内的命令尝试访问**未在白名单**的网络 host 时，`sandbox-runtime` 通过初始化时传入的 `sandboxAskCallback`（典型实现位于 `cli/print.ts:620` 与 `screens/REPL.tsx:2339`）回调到 UI：

- 选项：`Yes` / `Yes, and don't ask again for <host>`（在 `allowManagedDomainsOnly` 模式下隐藏持久化选项）/ `No, and tell Claude what to do differently`
- 标题：`Network request outside of sandbox`
- "yes-dont-ask-again" 会被持久化到 settings，下次自动放行。

### 8.3 违规展示

`SandboxViolationExpandedView.tsx` 订阅 `SandboxManager.getSandboxViolationStore()`，显示最近 10 条 sandbox 拦截事件（macOS 可见，Linux 直接返回 null——bwrap 的拦截不会进入 violation store）。`SandboxPromptFooterHint.tsx` 在用户输入框下方显示 "⧈ Sandbox blocked N operations · ctrl+o for details · /sandbox to disable"，每次新增违规 5 秒后自动隐藏。

### 8.4 `/sandbox` 命令（`src/commands/sandbox-toggle/`）

- `index.ts`：命令注册；`isHidden` 为不支持平台或 `enabledPlatforms` 排除当前平台时隐藏；`description` 动态生成（`sandbox enabled (auto-allow), fallback allowed (managed)` 等），使用 `figures.tick` / `figures.circle` / `figures.warning` 显示状态。
- `sandbox-toggle.tsx`：
  - 不支持平台时返回错误（包括 WSL1）
  - 不在 `enabledPlatforms` 时返回错误
  - 设置被策略锁时返回错误
  - 无参数 → 渲染 `<SandboxSettings>` 交互面板
  - 子命令 `exclude <pattern>` → 调 `addToExcludedCommands(pattern)` 写入 `localSettings.sandbox.excludedCommands`

---

## 九、生命周期挂载点

| 文件 | 行 | 角色 |
|------|---|------|
| `src/screens/REPL.tsx:2337-2344` | 启动 | `SandboxManager.initialize(sandboxAskCallback)`，fire-and-forget；失败时 graceful shutdown |
| `src/screens/REPL.tsx:2318-2336` | 启动 | 若 `isSandboxRequired()`（即 `failIfUnavailable`）且沙箱不可用，**直接退出 1**；否则展示一次性 notification |
| `src/cli/print.ts:620` | SDK 入口 | `SandboxManager.initialize(structuredIO.createSandboxAskCallback())` |
| `src/main.tsx:314-316` | 启动埋点 | 上报 `sandbox_enabled` / `are_unsandboxed_commands_allowed` / `is_auto_bash_allowed_if_sandbox_enabled` 三个维度 |
| `src/utils/sandbox/sandbox-adapter.ts:776-781` | 运行中 | `settingsChangeDetector.subscribe` → `updateConfig`，所以**用户编辑 settings.json 时沙箱规则实时生效** |

`sandboxAskCallback` 是用户授权路径的入口。典型 REPL 实现就是把 `SandboxPermissionRequest` 渲染到 TUI，并 await 用户的选择。

---

## 十、安全设计的关键细节

### 10.1 `getSandboxUnavailableReason` 的安全意图

`sandbox-adapter.ts:562` 注释明确：

> previously `isSandboxingEnabled()` silently returned false when dependencies were missing, giving users zero feedback that their explicit security setting was being ignored. This is a **security footgun** — users configure allowedDomains expecting enforcement, get none.

修复方式：当用户**显式**设置了 `sandbox.enabled = true` 但跑不起来时，REPL 会弹一个一次性通知；如果是 `failIfUnavailable`，则直接拒启动。这是一道"用户面对的反馈"，把"配置了但没生效"的危险显式化。

### 10.2 Bare-git 沙箱逃逸（`#29316`）

Git 的 `is_git_directory()` 判定是**只要 cwd 中同时存在** `HEAD + objects/ + refs/`（三个都未必完整），就**视为 bare repo**。攻击模型：

1. 沙箱内的命令在 cwd 写入 `HEAD`、`objects`、`refs`、`hooks`、`config`
2. 沙箱外（Claude 自己后续调用的 `git` 是**不带 sandbox** 的）执行 `git status` 时被劫持，因为 `HEAD` 里的 refspec 会把 git 引导到攻击者控制的 objects。

修复：见 `sandbox-adapter.ts:257-280` + `scrubBareGitRepoFiles`：

- 若这些文件**已存在** → `denyWrite`（ro-bind in place）
- 若**不存在** → 加入 `bareGitRepoScrubPaths`，`cleanupAfterCommand` 中**同步** `rmSync` 删除，避免留下 0 字节 stub 影响后续 `git log HEAD`（注意：sync 是为了调用方 `await shellCommand.result` 后立刻看到干净 cwd，注释里特意解释过）。

### 10.3 Settings 写入拦截

`convertToSandboxRuntimeConfig` 中**总是** deny write 到所有 SETTING_SOURCES 的 settings.json 与 drop-in 目录，以及 `.claude/skills`。这是防止沙箱内的命令修改自身配置来扩大权限。

### 10.4 auto-allow + 显式 deny

`bashPermissions.ts:1829-1843`：即便 `autoAllowBashIfSandboxed` 打开，**仍然**先调 `checkSandboxAutoAllow` 检查显式 deny / ask 规则。也就是说"沙箱"不等于"任何事都行"——用户已经 deny 的 `Bash(rm -rf)` 仍会被拦下。

### 10.5 `dangerouslyDisableSandbox` 是 fallback 路径

当 `allowUnsandboxedCommands = true` 且沙箱内命令因权限失败时，模型可以**主动**用 `dangerouslyDisableSandbox: true` 重跑——这条路径会触发**普通 Bash 权限弹窗**（不再走 auto-allow）。它被 `shouldUseSandbox` 显式拦截：

```ts
if (input.dangerouslyDisableSandbox &&
    SandboxManager.areUnsandboxedCommandsAllowed()) return false
```

而 `areUnsandboxedCommandsAllowed()` 默认 `true`，可通过 `/sandbox` 切到 "Strict sandbox mode" 关闭。

### 10.6 macOS `enableWeakerNetworkIsolation` 的取舍

注释（`sandboxTypes.ts:130-133`）：

> macOS only: Allow access to com.apple.trustd.agent in the sandbox.
> Needed for Go-based CLI tools (gh, gcloud, terraform, etc.) to verify TLS certificates when using httpProxyPort with a MITM proxy and custom CA.
> **Reduces security** — opens a potential data exfiltration vector through the trustd service. Default: false

这是一个**故意**降低隔离强度的逃生口——为了兼容通过 MITM 代理 + 自定义 CA 跑 Go 工具链的场景。

### 10.7 `excludedCommands` 的诚实标注

`shouldUseSandbox.ts:18-20`：

> NOTE: excludedCommands is a user-facing convenience feature, not a security boundary. It is not a security bug to be able to bypass excludedCommands — the sandbox permission system (which prompts users) is the actual security control.

实现里做了大量 `&&`-split + 多轮剥前缀，**目的是避免漏判**，而不是真正防御：即使子命令被错误地判为 excluded，最终仍走 `BashTool.checkPermissions` 弹权限窗，所以安全性不依赖此函数的完备性。

---

## 十一、关键文件速查

| 文件 | 行数 | 作用 |
|------|------|------|
| `src/entrypoints/sandboxTypes.ts` | 156 | Zod schema，配置单一事实源 |
| `src/utils/sandbox/sandbox-adapter.ts` | 985 | 适配层主文件：配置合并、生命周期、决策接口 |
| `src/utils/sandbox/sandbox-ui-utils.ts` | 12 | `removeSandboxViolationTags` 用于清理 UI 字符串 |
| `src/tools/BashTool/shouldUseSandbox.ts` | 153 | 单条命令是否沙箱化 |
| `src/tools/BashTool/BashTool.tsx` | — | 调 `shouldUseSandbox`、`annotateStderrWithSandboxFailures` |
| `src/tools/BashTool/bashPermissions.ts` | — | sandbox auto-allow 子流程 |
| `src/tools/BashTool/prompt.ts` | — | 注入沙箱规则到 system prompt |
| `src/utils/Shell.ts` | — | 实际 `wrapWithSandbox` + `cleanupAfterCommand` |
| `src/components/sandbox/SandboxSettings.tsx` | 295 | `/sandbox` 主面板 |
| `src/components/sandbox/SandboxConfigTab.tsx` | 44 | 展示当前规则 |
| `src/components/sandbox/SandboxOverridesTab.tsx` | 192 | 切换 `allowUnsandboxedCommands` |
| `src/components/sandbox/SandboxDependenciesTab.tsx` | 119 | 检查 ripgrep/bwrap/socat/seccomp |
| `src/components/sandbox/SandboxDoctorSection.tsx` | 45 | `/doctor` 集成 |
| `src/components/permissions/SandboxPermissionRequest.tsx` | 162 | 沙箱外网络请求确认弹窗 |
| `src/components/SandboxViolationExpandedView.tsx` | 98 | macOS 上的拦截事件面板 |
| `src/components/PromptInput/SandboxPromptFooterHint.tsx` | 63 | 输入框下方的拦截提醒 |
| `src/commands/sandbox-toggle/index.ts` | 50 | `/sandbox` 命令注册 |
| `src/commands/sandbox-toggle/sandbox-toggle.tsx` | 82 | `/sandbox` 与 `/sandbox exclude <pat>` 处理 |
| `src/screens/REPL.tsx` (2337-2344, 2318-2336) | — | `initialize` + `failIfUnavailable` |
| `src/cli/print.ts` (620) | — | SDK 入口的 `initialize` |
| `src/main.tsx` (314-316) | — | 启动埋点 |

---

## 十二、设计评价（阅读源码的感想）

1. **清晰的关注点分离**：`sandbox-adapter.ts` 把"配置语义"（CC 自己的设置）与"运行时语义"（runtime 的配置）解耦得很干净，路径规则在不同来源下的语义差异（`/path` 的两套含义）单独抽出 `resolvePathPatternForSandbox` / `resolveSandboxFilesystemPath`。

2. **诚实的"用户便利"边界**：`excludedCommands` 的注释直说它不是安全边界——这是少见的安全工程坦白，避免了把 UX 功能当防御措施带来的认知错位。

3. **运行时多源 merge 的复杂性**：注释多处提到 "merge without dedup" 导致 `allowWrite` 出现 `~/.cache` 三次——他们选择在 prompt 层 dedup（`prompt.ts:167`）而**不**改 sandbox 配置 merge，因为后者是 runtime 内部的去重责任。这个分工有意识。

4. **同步清理 vs 异步**：bare-git scrub、cleanupAfterCommand 都刻意保持同步（`rmSync` 而非 `rm`），配合调用方 `await result.then(...)` 的同步微任务预期，是一道细但必要的正确性保证。

5. **缺失的依赖对用户透明**：`getSandboxUnavailableReason` + `/sandbox Dependencies Tab` 是对用户最有用的设计之一——把"我装了沙箱但没跑起来"的状态显式暴露出来，比"看起来在沙箱里但其实啥都没拦"安全得多。

6. **可改进点**：
   - `convertToSandboxRuntimeConfig` 中仍有少量 `console.log`-级别注释暴露的"脆弱点"（worktree 路径只在启动时探测一次；如果用户在会话中 `cd` 切换 worktree 会失效）。
   - `enableWeakerNetworkIsolation` 默认 `false` 但被注释反复提醒"Reduces security"——这种开关的存在本身提示了 macOS seatbelt 与 MITM 代理组合的局限性。
   - `dangerouslyDisableSandbox` 的 fallback 模型依赖 LLM 主动识别 sandbox 失败原因（prompt 里写得相当细致）——一旦 LLM 误判就会反复尝试，越权风险显著。

---

> 本文档仅基于 `src/` 源码静态阅读，未运行时验证。所有引用的代码路径均来自上述文件，标注的代码位置为相对路径 + 起始行号。