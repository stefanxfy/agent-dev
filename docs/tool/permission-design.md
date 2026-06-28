# Claude Code 工具权限（Permission）设计实现深度解读

> 本文档基于对 Anthropic 发布的 Claude Code TypeScript 源码（`src/`）的静态阅读整理。
> 涉及的核心实现文件共约 **11000 行**，覆盖类型系统、判定流水线、规则匹配、分类器集成、UI 弹窗以及和 Hook / Sandbox 的协同。
>
> 阅读本文档前建议先阅读 [`sandbox-implementation.md`](sandbox-implementation.md)，因为两者高度耦合：本权限层是"应用层规则"，沙箱层是"OS 层约束"，互为兜底。

---

## 一、总体定位

Claude Code 的"Permission"是**应用层**的工具调用授权系统，决定**每条 tool_use 在执行前应该被允许 / 拒绝 / 询问用户**。它解决的核心问题：

> 在模型产出 tool_use 后、`tool.call()` 真正执行前，用一组可由用户在 settings 中声明的规则、对当前 mode（default / plan / acceptEdits / bypassPermissions / dontAsk / auto）、对 Hook、对 Haiku 分类器、对 Shell 静态分析的结果，组合成一个 `PermissionDecision`。

它和沙箱（Sandbox）的关系：

| 维度 | Permission | Sandbox |
|------|-----------|---------|
| 层级 | 应用层（TypeScript） | OS 层（Seatbelt / bwrap） |
| 触发时机 | tool_use 进入 `runToolUse` 后、`tool.call()` 前 | `tool.call()` 内 fork 子进程时 |
| 输入 | 规则 + mode + 分类器结果 + Hook 决定 | `sandboxRuntimeConfig` 编译产物 |
| 输出 | `allow` / `deny` / `ask` | OS 级 allow/deny（不可被进程绕过） |
| 用户体验 | 弹窗询问、规则持久化 | 静默执行（违规才提示） |
| 落败方 | 进程继续推进，但被拦下 | 子进程被 OS 杀掉 |

二者协同点在 [`bashPermissions.ts:1829-1843`](../src/tools/BashTool/bashPermissions.ts)：当 `autoAllowBashIfSandboxed` 开启时，先走 `checkSandboxAutoAllow`，由 sandbox 兜底；否则走 `bashToolHasPermission` 完整链路。

---

## 二、运行机制总览

```text
                       +-----------------------------------+
   model tool_use ---->|  runToolUse (toolExecution.ts)    |
                       +-----------------------------------+
                                        |
                                        v
                       +-----------------------------------+
                       |  streamedCheckPermissionsAndCall- |
                       |  Tool → checkPermissionsAndCall-  |
                       |  Tool (toolExecution.ts:599-)     |
                       +-----------------------------------+
                                        |
                                        v
                       +-----------------------------------+
                       |  PreToolUse Hooks (并行)          |
                       |  可选返回 allow/deny/updatedInput |
                       +-----------------------------------+
                                        |
                                        v
                       +-----------------------------------+
                       |  resolveHookPermissionDecision    |
                       |  → canUseTool = hasPermissionsTo- |
                       |     UseTool (permissions.ts:473)  |
                       +-----------------------------------+
                                        |
                       ┌────────────────┴────────────────┐
                       | 1a tool 全局 deny rule?           |
                       | 1b tool 全局 ask rule?           |
                       | 1c tool.checkPermissions(input)  |
                       |     └─ Bash → bashToolHasPermission
                       |         ├─ AST parse (tree-sitter)
                       |         ├─ too-complex → ask
                       |         ├─ sandbox auto-allow?
                       |         ├─ exact match deny/ask/allow
                       |         ├─ Haiku classify deny / ask
                       |         ├─ pipe / operator 检查
                       |         ├─ splitCommand 子命令级规则
                       |         ├─ cd+git bare-repo 防御
                       |         ├─ path constraints
                       |         ├─ sed constraints
                       |         ├─ mode (acceptEdits 自动放行)
                       |         └─ read-only 命令放行
                       | 1d tool 返回 deny? → 终止
                       | 1f ask rule 来自 tool.checkPermissions? → 终止
                       | 1g safetyCheck (.claude/, .git/) → 终止
                       +-----------------+-----------------+
                                         |
                                         v
                       +-----------------------------------+
                       | 2a mode == bypassPermissions?     |
                       | 2b tool 全局 allow rule?          |
                       +-----------------------------------+
                                        |
                                        v
                       +-----------------------------------+
                       | 3 passthrough → ask               |
                       +-----------------------------------+
                                        |
                                        v
                       +-----------------------------------+
                       | mode 后处理:                      |
                       |  - dontAsk → deny                 |
                       |  - auto → Haiku YOLO classifier   |
                       |    ├─ acceptEdits fast-path?      |
                       |    ├─ 安全工具白名单?             |
                       |    └─ 否则 classifYoloAction      |
                       |  - shouldAvoidPermissionPrompts?  |
                       |    └─ PermissionRequest Hook → deny
                       +-----------------------------------+
                                        |
                                        v
                       +-----------------------------------+
                       |  决定 → tool.call()               |
                       +-----------------------------------+
```

---

## 三、类型系统（`src/types/permissions.ts`）

### 3.1 PermissionMode

```ts
EXTERNAL_PERMISSION_MODES = [
  'acceptEdits', 'bypassPermissions', 'default', 'dontAsk', 'plan'
] as const

InternalPermissionMode = ExternalPermissionMode | 'auto' | 'bubble'
PERMISSION_MODES = INTERNAL_PERMISSION_MODES
                  // 加上 feature('TRANSCRIPT_CLASSIFIER') 才暴露 'auto'
```

| 模式 | 行为 |
|------|------|
| `default` | 按规则判断，无匹配则 ask |
| `plan` | ExitPlanMode 才能落 `acceptEdits`；plan 内不执行非 read-only |
| `acceptEdits` | 文件编辑类工具自动放行（按 tool 自己 `checkPermissions` 决定） |
| `bypassPermissions` | **跳过所有 ask**（hook 与 deny/safetyCheck 仍生效） |
| `dontAsk` | 所有 ask 转 deny（auto-deny） |
| `auto` | ANT-only；用 Haiku YOLO classifier 替代用户弹窗 |
| `bubble` | 内部测试模式 |

`PermissionMode.ts` 把每个 mode 映射成 `{title, shortTitle, symbol, color, external}`。`isExternalPermissionMode` 在外部用户（非 `USER_TYPE=ant`）下永远返回 true（把 `auto` 隐藏）。

### 3.2 PermissionBehavior & PermissionRule

```ts
PermissionBehavior = 'allow' | 'deny' | 'ask'

PermissionRuleValue = { toolName: string, ruleContent?: string }

PermissionRule = {
  source: PermissionRuleSource,    // 哪条设置来源
  ruleBehavior: PermissionBehavior,
  ruleValue: PermissionRuleValue,
}

PermissionRuleSource =
  | 'userSettings' | 'projectSettings' | 'localSettings'
  | 'flagSettings' | 'policySettings'    // 高优先级、企业 / 启动参数
  | 'cliArg' | 'command' | 'session'     // 内存级，本会话有效
```

`PermissionUpdateDestination` 与 `PermissionRuleSource` 类似，但**去掉** `policySettings` / `flagSettings` / `command`——它们是只读的，不能写入。

### 3.3 PermissionUpdate（discriminated union）

```ts
PermissionUpdate =
  | { type: 'addRules', destination, rules, behavior }
  | { type: 'replaceRules', destination, rules, behavior }
  | { type: 'removeRules', destination, rules, behavior }
  | { type: 'setMode', destination, mode }
  | { type: 'addDirectories', destination, directories }
  | { type: 'removeDirectories', destination, directories }
```

这是 Permission 系统的"状态变化原子操作"，详见 §九。

### 3.4 PermissionDecision & PermissionResult

```ts
PermissionDecision  = { behavior: 'allow' | 'ask' | 'deny', updatedInput?, decisionReason?, ... }
PermissionResult    = PermissionDecision | { behavior: 'passthrough', ... }
```

四种结果含义：

| behavior | 含义 |
|----------|------|
| `allow` | 直接执行 |
| `ask` | 弹窗，等待用户选择 |
| `deny` | 终止，发 tool_result error 给模型 |
| `passthrough` | 当前检查未匹配，让上层继续；最终在 `hasPermissionsToUseToolInner` 的 step 3 转 `ask` |

### 3.5 PermissionDecisionReason

```ts
type PermissionDecisionReason =
  | { type: 'rule', rule: PermissionRule }              // 命中规则
  | { type: 'mode', mode: PermissionMode }              // mode 直接决定
  | { type: 'subcommandResults', reasons: Map<string, PermissionResult> }
  | { type: 'permissionPromptTool', ... }               // Agent SDK host 自管
  | { type: 'hook', hookName, hookSource?, reason? }    // PreToolUse hook
  | { type: 'asyncAgent', reason }                      // 后台 agent，无用户
  | { type: 'sandboxOverride', reason: 'excludedCommand' | 'dangerouslyDisableSandbox' }
  | { type: 'classifier', classifier: string, reason } // Bash / auto-mode
  | { type: 'workingDir', reason }                      // 路径约束
  | { type: 'safetyCheck', reason, classifierApprovable }  // 敏感路径
  | { type: 'other', reason }
```

### 3.6 ToolPermissionContext

每个 tool 的 `checkPermissions(input, context)` 拿到的就是这个对象。它是 Permission 系统的**唯一入口数据**：

```ts
type ToolPermissionContext = {
  mode: PermissionMode
  additionalWorkingDirectories: ReadonlyMap<string, AdditionalWorkingDirectory>
  alwaysAllowRules:  ToolPermissionRulesBySource   // 8 个 source × string[]
  alwaysDenyRules:   ToolPermissionRulesBySource
  alwaysAskRules:    ToolPermissionRulesBySource
  isBypassPermissionsModeAvailable: boolean        // plan 模式是否记起 bypass
  strippedDangerousRules?: ToolPermissionRulesBySource
  shouldAvoidPermissionPrompts?: boolean            // 后台 agent：禁止弹窗
  awaitAutomatedChecksBeforeDialog?: boolean        // 等 hook 先跑
  prePlanMode?: PermissionMode
}
```

注意 `alwaysAllowRules` 等是 **`ToolPermissionRulesBySource`**（按 source 索引的字符串数组），**未解析**成 `PermissionRuleValue`。Tool 自己负责用 `permissions.ts` 里的 `getAllowRules(context) / getRuleByContentsForToolName(...)` 解析。

---

## 四、核心流水线（`permissions.ts`）

### 4.1 `hasPermissionsToUseTool`（`permissions.ts:473`）

这是从 toolExecution 顶层调用的"决策器"。它把 `hasPermissionsToUseToolInner` 的结果再叠加 mode 后处理（dontAsk/auto/asyncAgent）。

完整流程（**step 编号与代码注释一致**）：

```
1a. 整个 tool 命中 deny rule?        → deny（reason: rule）
1b. 整个 tool 命中 ask rule?
       对 Bash + sandbox auto-allow 路径 → 不立即返回，让 Bash 自己处理子命令
       其他                              → ask
1c. 调 tool.checkPermissions(input, context)
       对 Bash: bashToolHasPermission（详见 §五）
1d. tool 返 deny?                    → deny（reason: subcommandResults / safetyCheck / other ...）
1e. tool.requiresUserInteraction() && ask? → ask（bypass 模式也无法跳过）
1f. tool 返 ask 且 decisionReason 是 rule.ask?
       → ask（content-specific ask 规则在 bypass 下仍生效）
1g. tool 返 ask 且 decisionReason 是 safetyCheck?
       → ask（敏感路径 .claude/、.git/、shell 配置 在 bypass 下仍生效）
2a. mode == bypassPermissions
       或 (mode == plan && isBypassPermissionsModeAvailable)
       → allow（reason: mode）
2b. 整个 tool 命中 allow rule?      → allow（reason: rule）
3.  passthrough → ask
```

之后再叠加 mode 后处理（`hasPermissionsToUseTool` 主体 `permissions.ts:473`）：

- **dontAsk mode**：`ask` 直接转 `deny`，reason: `{type: 'mode', mode: 'dontAsk'}`
- **auto mode**（`feature('TRANSCRIPT_CLASSIFIER')`）：进入 Haiku YOLO 分类器（详见 §六）
- **shouldAvoidPermissionPrompts**（后台 agent）：先跑 `PermissionRequest` Hook，再无决策则自动 deny（reason: asyncAgent）

### 4.2 `checkRuleBasedPermissions`（`permissions.ts:1071`）

是 `hasPermissionsToUseTool` 的"只跑规则层"版本，**只**做 1a/1b/1c/1d/1f/1g 步。它**绕过**：
- 2a bypassPermissions
- 2b always-allowed
- auto-mode classifier
- mode 转换
- PermissionRequest hooks

用于 SDK host 实现自己的 UI 时，只需要"rule-based 决策"的子集。

### 4.3 模式后处理（`permissions.ts:473-955`）

#### 4.3.1 auto mode classifier 流程

注释（`permissions.ts:518-525`）：

> Apply auto mode: use AI classifier instead of prompting user

进入条件：

```ts
if (feature('TRANSCRIPT_CLASSIFIER') &&
    (mode === 'auto' || (mode === 'plan' && autoModeStateModule?.isAutoModeActive()))) {
```

流程：

1. **safetyCheck 非 classifierApprovable** → 必须保留 ask（不交给 classifier）
2. **tool.requiresUserInteraction()** → 保留 ask
3. **PowerShell**：除非 `feature('POWERSHELL_AUTO_MODE')`，必须 ask
4. **acceptEdits fast-path**：用 `mode: 'acceptEdits'` 重新跑 tool.checkPermissions；如果 allow → 走 fast-path 放行（reason: `mode: auto`，fastPath: `acceptEdits`）。**但对 `Agent` 与 `REPL` 工具禁用**（注释：REPL 内嵌 VM 逃逸会绕过 classifier）。
5. **safe-tool allowlist**（`classifierDecision.ts:isAutoModeAllowlistedTool`）：直接放行（fastPath: `allowlist`）。
6. **否则**调 `classifyYoloAction(messages, action, tools, ctx, signal)`（详见 §六.2）。
7. **denial tracking**：每个 block 都增 `consecutiveDenials / totalDenials`（`denialTracking.ts`）。**当 consecutiveDenials 达到阈值**，强制 fall back 到 ask，并打 `tengu_auto_mode_denial_limit_exceeded` 事件。
8. **transcriptTooLong** → fallback to normal prompting（context window 永久性问题，retry 必失败）
9. **unavailable**（API 错误）：通过 GrowthBook `tengu_iron_gate_closed` 决定 fail-closed（deny）还是 fail-open（fallback to normal prompting）

#### 4.3.2 后台 agent deny fallback

```ts
if (shouldAvoidPermissionPrompts) {
  const hookDecision = await runPermissionRequestHooksForHeadlessAgent(...)
  if (hookDecision) return hookDecision
  return { behavior: 'deny', decisionReason: { type: 'asyncAgent', reason: ... } }
}
```

注释（`permissions.ts:929-952`）：后台 agent 不能弹窗；先给 `PermissionRequest` Hook 一次机会；再不行就 auto-deny。

---

## 五、Bash 工具权限判定（`bashPermissions.ts`）

这是整个 Permission 系统**最复杂**的子模块，2621 行。核心是 `bashToolHasPermission(input, context)`，决定一条 Bash 命令是否放行。

### 5.1 调用上下文（`bashPermissions.ts:1663`）

```ts
export async function bashToolHasPermission(
  input: z.infer<typeof BashTool.inputSchema>,
  context: ToolUseContext,
  getCommandSubcommandPrefixFn = getCommandSubcommandPrefix,
): Promise<PermissionResult>
```

### 5.2 AST 静态分析（`bashPermissions.ts:1670-1738`）

替代了旧的 `tryParseShellCommand + bashCommandIsSafe` regex 链：

```ts
let astResult = astRoot
  ? parseForSecurityFromAst(input.command, astRoot)
  : { kind: 'parse-unavailable' }
```

四种结果：

| `kind` | 含义 | 行为 |
|--------|------|------|
| `simple` | 干净解析出 `SimpleCommand[]`，无隐藏替换 | 进入完整流水线 |
| `too-complex` | 含 command substitution / 流程控制等无法静态分析的语法 | 直接 ask |
| `parse-unavailable` | tree-sitter 未加载或被禁用 | 走 legacy path |
| `parse-error` | 解析失败 | ask |

`TREE_SITTER_BASH_SHADOW` feature 开启时进入 shadow 模式：跑 AST，**记录 verdict，但强制走 legacy**——为了对比两条路径的命中率。

### 5.3 完整决策流水线（按代码顺序）

```text
0. parseForSecurityFromAst(input.command)
   ├─ 'too-complex' → checkEarlyExitDeny → ask（带 pendingClassifierCheck）
   ├─ 'simple'      → checkSemantics (eval/zsh builtin) → ask on fail
   └─ 'parse-unavailable' → tryParseShellCommand fallback
                           ├─ 解析失败 → ask
                           └─ 继续

1. Sandbox auto-allow?
   if (isSandboxingEnabled && isAutoAllowBashIfSandboxedEnabled && shouldUseSandbox(input))
       → checkSandboxAutoAllow(input, context)
       ├─ 命中 deny/ask rule (含子命令) → deny/ask
       └─ 否则 → allow（reason: 'Auto-allowed with sandbox ...'）
   
   说明：auto-allow 仍尊重显式 deny/ask 规则——用户 deny 的 `Bash(rm:*)` 仍会拦下。

2. 精确匹配
   bashToolCheckExactMatchPermission
   ├─ exact deny  → deny
   ├─ exact ask   → ask
   ├─ exact allow → 暂存，继续
   └─ passthrough 继续

3. Bash prompt deny / ask 描述符 (Haiku 分类)
   if (isClassifierPermissionsEnabled && !(TRANSCRIPT_CLASSIFIER && mode=='auto')) {
     const [denyResult, askResult] = await Promise.all([
       hasDeny ? classifyBashCommand(cmd, cwd, denyDescriptions, 'deny', ...),
       hasAsk  ? classifyBashCommand(cmd, cwd, askDescriptions,  'ask',  ...),
     ])
     if (denyResult?.matches && confidence==='high') → deny (reason: 'Denied by Bash prompt rule: ...')
     if (askResult?.matches  && confidence==='high') → ask  (reason: 'Required by Bash prompt rule: ...',
                                                       suggestions: suggestionForExactCommand / prefix)
   }

4. 命令操作符 (`|`, `;`, `&&`, `>` etc.) 检查
   checkCommandOperatorPermissions(input, recursive)
   ├─ 'passthrough' → 继续
   ├─ 'allow'      → 验证 path constraints (cd+redirect 防御) → 继续
   └─ 'ask'        → ask (带 pendingClassifierCheck)

5. 原始命令注入检测 (legacy path only, astSubcommands==null)
   bashCommandIsSafeAsync(input.command)
   ├─ 'ask' && isBashSecurityCheckForMisparsing
   │   ├─ 尝试 stripSafeHeredocSubstitutions 剥离
   │   └─ 仍有 misparse pattern → ask（但尊重 exact-match allow rule）
   └─ 'passthrough' 继续

6. splitCommand → subcommands[] + filterCdCwdSubcommands
   if (subcommands.length > MAX_SUBCOMMANDS_FOR_SECURITY_CHECK) → ask (CC-643: 防御性 cap)

7. 多 cd 命令?
   subcommands.filter(isNormalizedCdCommand).length > 1 → ask
   compoundCommandHasCd = (cd 数 > 0)

8. cd + git 防御 (核心: bare-git 攻击)
   if (compoundCommandHasCd && hasGitCommand) → ask
   注释 (#29316): 防止 `cd /malicious/dir && git status` 让 git 加载
   攻击者控制的 .git/config (core.fsmonitor / bare repo 识别)

9. 对每个 subcommand 调 bashToolCheckPermission
   ├─ exact match deny/ask → 立即返回
   ├─ prefix match deny/ask → 立即返回
   ├─ checkPathConstraints → deny/ask (路径白名单/黑名单)
   ├─ exact allow → allow
   ├─ prefix allow → allow
   ├─ checkSedConstraints → deny/ask
   ├─ checkPermissionMode (acceptEdits → read-only 命令自动放行)
   ├─ isReadOnly(input) → allow
   └─ passthrough → 下一步

10. 若任一 subcommand deny → deny（reason: subcommandResults）

11. 原始命令 path 重定向检查 (splitCommand 剥离过 `>`)
    checkPathConstraints(input, cwd, ctx, compoundCommandHasCd, astRedirects, astCommands)
    ├─ 'deny' → deny
    ├─ 'ask' && askSubresult == undefined → ask (单独 path-constraint ask)
    └─ 'ask' && 有 subcommand ask → fall through to merge flow

12. 若有 subcommand ask 且 non-allow count == 1 → ask

13. 全部 subcommand allow + 无 command injection → allow
    (legacy path: per-sub re-check bashCommandIsSafeAsync; AST path: 跳过)

14. 单 subcommand: checkCommandAndSuggestRules → 拼接 suggestions
    suggestions 可能是:
    - exact match → Bash(cmd)
    - prefix → Bash(prefix:*)
    - heredoc prefix / multiline first line → 同样 prefix

15. 多 subcommand: 合并所有 subcommand 的 suggestions
    收集非 allow 的 subcommand 的规则建议
    GH#28784 follow-up: 若 security-check ask 没建议 → 合成 Bash(exact) 规则
    Cap 到 MAX_SUGGESTED_RULES_FOR_COMPOUND = 5 (GH#11380)
    → ask / passthrough (one-or-another 由顶层 step 3 转 ask)
```

### 5.4 `bashToolCheckExactMatchPermission`（`bashPermissions.ts:~960-1048`）

只查 exact-match（**整条命令字符串完全等于某条规则**）：

```
1. exact deny?  → deny
2. exact ask?   → ask
3. exact allow? → allow
4. passthrough  → { suggestions: suggestionForExactCommand(command) }
```

### 5.5 `bashToolCheckPermission`（`bashPermissions.ts:1050-1178`）

子命令级规则 + 路径约束 + mode + read-only fallback：

```
1. exact match deny/ask  → return
2. prefix match (Bash(rm:*), Bash(*echo*)) deny/ask → return
   注释: 安全修复——deny 在 path constraints 之前检查，
   防止绝对路径绕过 deny rule (HackerOne report)
3. checkPathConstraints  (deny/ask) → return
4. exact allow → allow
5. prefix allow → allow
6. checkSedConstraints  → deny/ask
7. checkPermissionMode   (acceptEdits 自动放行) → return
8. BashTool.isReadOnly() → allow
9. passthrough  → suggestions
```

### 5.6 关键辅助函数

| 函数 | 行 | 职责 |
|------|----|------|
| `filterCdCwdSubcommands` | 1367 | 过滤掉 `cd ${cwd}` 前缀，让 `cd . && cmd` 等价于 `cmd` |
| `checkEarlyExitDeny` | 1391 | AST too-complex / semantics-fail 路径：先查 exact-match + prefix-deny |
| `checkSemanticsDeny` | 1431 | semantics fail 后还要按 SimpleCommand .text 逐条查 prefix-deny（防止 `echo foo \| eval rm`） |
| `matchingRulesForInput` | shellRuleMatching.ts | prefix / wildcard 模式匹配 |
| `checkCommandOperatorPermissions` | bashCommandHelpers.ts | pipe / `>` / `&&` 多段检查（递归调用） |
| `checkPathConstraints` | pathValidation.ts | 路径白/黑名单 + cd+redirect 复合检查 |
| `checkSedConstraints` | sedValidation.ts | sed -i / -e 等敏感操作 |
| `checkPermissionMode` | modeValidation.ts | acceptEdits 自动放行逻辑 |
| `isNormalizedGitCommand` / `isNormalizedCdCommand` | 2567 / 2603 | 脱掉 safe wrappers（env var / timeout）+ shell quote 归一化再判断 |
| `commandHasAnyCd` | 2617 | 复合命令是否含 cd |
| `isCompoundCommand` | shellRuleMatching.ts | pipe/`&&`/etc 判断，控制 prefix rule 是否启用 |
| `MAX_SUBCOMMANDS_FOR_SECURITY_CHECK` | 103 | 50，超过则降级到 ask（CC-643：splitCommand 极端展开防御） |
| `MAX_SUGGESTED_RULES_FOR_COMPOUND` | 110 | 5，超过则裁剪（GH#11380：UI 噪音） |

### 5.7 Classifier 集成

```ts
import {
  classifyBashCommand,
  getBashPromptAllowDescriptions, getBashPromptAskDescriptions, getBashPromptDenyDescriptions,
  isClassifierPermissionsEnabled,
} from '../../utils/permissions/bashClassifier.js'
```

`bashClassifier.ts` 在外部构建里是 **stub**（`isClassifierPermissionsEnabled() → false`，`classifyBashCommand → {matches:false,confidence:'high',reason:'This feature is disabled'}`）。ANT 构建有真正的实现。

#### 5.7.1 三种行为（`bashClassifier.ts:5-12`）

- `getBashPromptAllowDescriptions(context)`：用户允许的"提示描述符"列表（如 "running tests"），命中后自动 allow
- `getBashPromptAskDescriptions(context)`：用户希望 ask 的描述符列表
- `getBashPromptDenyDescriptions(context)`：用户希望 deny 的描述符列表

#### 5.7.2 Speculative allow check

```ts
// toolExecution.ts:740-752
if (tool.name === BASH_TOOL_NAME && 'command' in parsedInput.data) {
  startSpeculativeClassifierCheck(command, context, signal, isNonInteractiveSession)
}
```

`startSpeculativeClassifierCheck`（`bashPermissions.ts:1497`）立即启动 allow Haiku 检查，结果存到 `speculativeChecks` map。**设置 `setClassifierChecking` 的 UI 闪烁**留给 `interactiveHandler.ts` 在 permission 真正返 `ask + pendingClassifierCheck` 时再调用——避免 prefix rule 直接放行的命令闪烁。

#### 5.7.3 Async classifier auto-approval

```ts
// bashPermissions.ts:1605
export async function executeAsyncClassifierCheck(pendingCheck, signal, ..., callbacks)
```

在 `PermissionRequest` 弹窗渲染期间异步跑：

- 高置信度命中 allow → `callbacks.onAllow({type:'classifier', classifier:'bash_allow', ...})`
- 用户已交互（`shouldContinue` 返回 false）→ 中途放弃，不覆盖用户选择
- 信号 abort → 静默返回

`setClassifierApproval(toolUseID, matchedRule)` 会写入 `CLASSIFIER_APPROVALS`，由 UI 用来显示"自动批准"标记。

---

## 六、Auto-Mode YOLO Classifier（`permissions.ts`）

ANT-only（`feature('TRANSCRIPT_CLASSIFIER')`）。

### 6.1 `classifyYoloAction`（`yoloClassifier.ts`）

把当前对话 + 工具描述 + 待执行动作打包成 prompt，调一个快速模型（一般是 Haiku）做"允许 / 阻断"二分类。结果：

```ts
type YoloClassifierResult = {
  shouldBlock: boolean
  reason: string
  unavailable?: boolean        // API 错误
  transcriptTooLong?: boolean  // context 超限
  model: string
  usage?: ClassifierUsage
  durationMs?: number
  promptLengths?: ...
  stage?: 'fast' | 'thinking'  // 2-stage classifier
  // stage 1/2 详细数据用于 join telemetry
  ...
}
```

### 6.2 三阶段 fast-path（`permissions.ts:600-686`）

按顺序检查：

1. **`requiresUserInteraction`**：tool 声明自己需要用户 → 跳过 classifier
2. **`acceptEdits` fast-path**：用 `mode: 'acceptEdits'` 重跑 `tool.checkPermissions`；若 allow → 跳过 classifier。**对 `Agent` / `REPL` 工具禁用**（REPL 内嵌 VM 逃逸会绕过 classifier——`permissions.ts:600-603` 注释明确解释）
3. **safe-tool allowlist**（`classifierDecision.ts`）：是常量白名单，跳过 classifier

### 6.3 Denial Tracking（`denialTracking.ts`）

```ts
type DenialTrackingState = {
  totalDenials: number
  consecutiveDenials: number
  // ...
}
DENIAL_LIMITS = { maxConsecutive: ?, maxTotal: ? }
```

每次 block 增 consecutive；任何 allow 触发 `recordSuccess` 重置 consecutive。当 consecutive 超过上限 → `handleDenialLimitExceeded` 强制 fallback 到 ask，避免"模型反复撞墙"。

### 6.4 Headless mode

`shouldAvoidPermissionPrompts` 开启（sub-agent 异步 agent）：

```ts
if (classifierResult.shouldBlock) {
  if (isHeadless) {
    if (denialLimitHit) throw new AbortError('Agent aborted: too many classifier denials in headless mode')
  }
}
```

后台 agent 没有用户，denial 持续 → 直接 abort。

---

## 七、规则加载与持久化（`permissionsLoader.ts`）

### 7.1 来源优先级

`PermissionRuleSource` 顺序（**从低到高**）：

```
command < session < localSettings < projectSettings < userSettings < cliArg < policySettings < flagSettings
```

更高优先级的来源**覆盖**低优先级的同名规则（如 `policySettings` 中的 deny 不能被 `userSettings` 中的 allow 覆盖）。

### 7.2 `loadAllPermissionRulesFromDisk`

```ts
export function loadAllPermissionRulesFromDisk(): PermissionRule[] {
  if (shouldAllowManagedPermissionRulesOnly()) {
    return getPermissionRulesForSource('policySettings')  // 企业只信 managed
  }
  const rules = []
  for (const source of getEnabledSettingSources()) {
    rules.push(...getPermissionRulesForSource(source))
  }
  return rules
}
```

`allowManagedPermissionRulesOnly` 是企业级开关，开启后**只信任** `policySettings`——保证管理员设定的 deny 不被用户绕过。

### 7.3 `getPermissionRulesForSource` → `settingsJsonToRules`

把 settings.json 中的：

```json
{
  "permissions": {
    "allow": ["Edit", "Read(./docs/**)"],
    "deny":  ["Bash(rm:*)"],
    "ask":   ["Bash(npm publish:*)"],
    "additionalDirectories": ["../shared-lib"]
  }
}
```

解析成 `PermissionRule[]`。

### 7.4 编辑（`addPermissionRulesToSettings`, `deletePermissionRuleFromSettings`）

直接写 `settings.json`，**用 lenient parse**（`getSettingsForSourceLenient_FOR_EDITING_ONLY`）避免破坏未识别的字段。

规范化：parse→serialize roundtrip，保证 `Bash(*)` 和 `Bash` 匹配。

---

## 八、规则匹配（`shellRuleMatching.ts`, `permissionRuleParser.ts`）

### 8.1 规则语法

`Bash(ruleContent)` —— ruleContent 有三种语义：

| 形式 | 例 | 匹配 |
|------|----|------|
| 无 ruleContent | `Bash` | 整个 tool 命中 |
| `prefix:*` | `Bash(npm run:*)` | 命令以 `npm run ` 开头 |
| `exact` | `Bash(npm run build)` | 命令完全等于 |
| `wildcard` | `Bash(*echo*)` | 命令字符串包含 `echo` |
| `prompt:desc` | `Bash(prompt: running tests)` | 由 bashClassifier 决定 |

### 8.2 `parsePermissionRule` → `ShellPermissionRule`

```ts
type ShellPermissionRule =
  | { type: 'prefix', prefix: string }
  | { type: 'exact', command: string }
  | { type: 'wildcard', pattern: string }
  | { type: 'compound', parts: ShellPermissionRule[] }   // `cmd1; cmd2`
  | { type: 'unsupported' }
```

### 8.3 `matchingRulesForInput`

返回 `{matchingDenyRules, matchingAskRules, matchingAllowRules}`，**按 source 优先级**（`command < session < localSettings < projectSettings < userSettings < cliArg < policySettings < flagSettings`）。**注意 bashPermissions.ts 里 step 2 注释**：

> filterRulesByContentsMatchingInput has a compound-command guard
> (splitCommand().length > 1 → prefix rules return false)

这是为什么 `checkSemanticsDeny` 必须按 SimpleCommand **逐条** 查 prefix rule 的原因。

### 8.4 `matchWildcardPattern` 与 `permissionRuleExtractPrefix`

wildcard 模式用 glob 匹配；prefix 规则提取出 `cmd subcmd` 前缀（参考 §5.7 中的 `getSimpleCommandPrefix` / `getFirstWordPrefix`）。

---

## 九、PermissionUpdate 应用与持久化（`PermissionUpdate.ts`）

### 9.1 `applyPermissionUpdate` (in-memory)

`applyPermissionUpdate(context, update)` 是纯函数，返回新的 `ToolPermissionContext`：

| update.type | 效果 |
|------------|------|
| `setMode` | `context.mode = update.mode` |
| `addRules` | `context.alwaysAllowRules[dest] = [...existing, ...newRules]` |
| `replaceRules` | `context.alwaysAllowRules[dest] = newRules` |
| `removeRules` | 过滤掉匹配项 |
| `addDirectories` | `additionalWorkingDirectories.set(path, {path, source})` |
| `removeDirectories` | `additionalWorkingDirectories.delete(path)` |

### 9.2 `persistPermissionUpdate` (disk)

只对 `localSettings / userSettings / projectSettings` 落盘；其他来源（`session` / `cliArg`）在内存中。

每个 case 调用 `updateSettingsForSource(destination, { permissions: ... })` 写 settings.json。

### 9.3 `createReadRuleSuggestion`

工具在 path constraint 失败时常用——为某个目录生成 `Read(/path/**)` allow rule suggestion，让用户一次授权永久放行。

---

## 十、Hook 集成（`utils/hooks.ts`）

### 10.1 PreToolUse Hook

在 `toolExecution.ts:800-862` 并行执行：

- 钩子可以返 `permissionRequestResult: {behavior: 'allow' | 'deny', ...}` → 直接决定
- 钩子可以返 `updatedInput` 但不决定 → 仍走正常 pipeline
- 钩子可以返 `additionalContext`（注入上下文）/ `preventContinuation`（停机）

钩子先于 `tool.checkPermissions` 执行——这是顺序重要的设计。

### 10.2 PermissionRequest Hook（headless mode only）

`runPermissionRequestHooksForHeadlessAgent`（`permissions.ts:400-471`）只在 `shouldAvoidPermissionPrompts` 时调用——给后台 agent 一个"外部决策"通道，没有就自动 deny。

### 10.3 PermissionDenied Hook

`executePermissionDeniedHooks`（`toolExecution.ts:1080-1100`）：仅在 auto-mode classifier 拒绝时调用。若钩子 `retry: true` → 给模型一个提示"可以重试"。

### 10.4 `awaitAutomatedChecksBeforeDialog`

UI 上的 `awaitAutomatedChecksBeforeDialog: true` 让 PermissionRequest 弹窗等所有 PreToolUse hook 跑完才显示，避免 hook 改完 input 后弹窗和实际不一致。

---

## 十一、UI 层（`src/components/permissions/`）

### 11.1 `PermissionRequest.tsx`

按 tool 类型路由：

```ts
function permissionComponentForTool(tool: Tool) {
  switch (tool) {
    case BashTool:          return BashPermissionRequest
    case FileEditTool:      return FileEditPermissionRequest
    case FileWriteTool:     return FileWritePermissionRequest
    case FileReadTool | GlobTool | GrepTool: return FilesystemPermissionRequest
    case PowerShellTool:    return PowerShellPermissionRequest
    case NotebookEditTool:  return NotebookEditPermissionRequest
    case WebFetchTool:      return WebFetchPermissionRequest
    case SkillTool:         return SkillPermissionRequest
    case EnterPlanModeTool: return EnterPlanModePermissionRequest
    case ExitPlanModeV2Tool: return ExitPlanModePermissionRequest
    case AskUserQuestionTool: return AskUserQuestionPermissionRequest
    case WorkflowTool | MonitorTool | ReviewArtifactTool: ... // feature gated
    default:                return FallbackPermissionRequest
  }
}
```

### 11.2 `BashPermissionRequest.tsx`（481 行）

Bash 弹窗的核心：

- **SedEdit 走 subcomponent**：parseSedEditCommand 成功 → 渲染 `SedEditPermissionRequest`
- **沙箱可视化**：右上角小标记，区分普通 Bash 与 sandbox 内 Bash（`SandboxedBash` 标签）
- **选项构建**（`bashToolUseOptions.tsx`）：
  - `yes`：单次允许
  - `yes-apply-suggestions`：把建议规则应用到 settings 后允许
  - `yes-prefix-edited`：用户可编辑的前缀（`npm run:*`）
  - `yes-classifier-reviewed`：ANT-only，保存 classifier 描述符
  - `no`：拒绝
- **Speculative classifier shimmer**：`<ClassifierCheckingSubtitle>` 子组件独立 20fps 时钟，避免整个弹窗每 50ms re-render

### 11.3 其他工具弹窗

每个工具有自己的 sub-folder：

- `FileEditPermissionRequest`：diff + allow/deny + "Yes, allow Edits to {path}/**"
- `FileWritePermissionRequest`：同上
- `FilesystemPermissionRequest`：Read/Glob/Grep 的目录级 allow
- `SandboxPermissionRequest`：沙箱外网络请求的二次确认
- `PowerShellPermissionRequest`：PowerShell 专属
- `NotebookEditPermissionRequest`：Jupyter
- `WebFetchPermissionRequest`：URL fetch 域白名单
- `SkillPermissionRequest`：Skill 调用授权
- `EnterPlanModePermissionRequest` / `ExitPlanModePermissionRequest`：plan mode 切换
- `FallbackPermissionRequest`：默认通用

### 11.4 `useShellPermissionFeedback`

收集用户对拒绝的反馈文本，传回模型（"No, you should..."）—— 这就是把 "deny + reason" 反馈给 LLM 的通道。

---

## 十二、ToolPermissionContext 的同步路径

`ToolPermissionContext` 的字段是 readonly，但它会**异步变化**：

```text
                    +-------------------+
settings.json  ---->| permissionsLoader |  (启动时 loadAllPermissionRulesFromDisk)
                    +---------+---------+
                              |
                              v
                  +-----------+-----------+
                  | permissionSetup.ts     |  (组装初始 context)
                  +-----------+-----------+
                              |
                              v
                  +-----------+-----------+
                  |  AppState (Zustand)    |  -> toolPermissionContext
                  +-----------+-----------+
                              |
                              v (每次 tool call)
                  +-----------+-----------+
                  |  hasPermissionsTo-     |
                  |  UseTool              |  (应用层 mutation)
                  +-----------+-----------+
                              |
                              v (用户选择 "Yes, and don't ask again")
                  +-----------+-----------+
                  |  persistPermission-    |
                  |  Update               |  (写 settings.json)
                  +-----------+-----------+
                              |
                              v (settings 监听)
                  +-----------+-----------+
                  |  settingsChange-       |
                  |  Detector             |  (refresh -> setAppState)
                  +-----------------------+
```

---

## 十三、与 Sandbox 的耦合点

| 文件 | 耦合 |
|------|------|
| `bashPermissions.ts:1829-1843` | `autoAllowBashIfSandboxed` 开启时绕开普通 Bash 权限弹窗（仍尊重 deny/ask） |
| `bashPermissions.ts:1829-1843` `checkSandboxAutoAllow` | 即使在 sandbox 内，也先查 deny/ask rule（防止用户 deny 的 `Bash(rm:*)` 被绕过） |
| `shouldUseSandbox.ts` | 复合命令 `dangerouslyDisableSandbox` 走普通 Bash 权限弹窗 |
| `permissions.ts:1094-1109` | tool-level ask rule 在 sandbox auto-allow 路径下不立即返回——Bash 自己去检查子命令级规则 |
| `PermissionResult.sandboxOverride` | `sandboxOverride` 决定 reason 表示是因 `excludedCommand` 或 `dangerouslyDisableSandbox` 走普通 Bash 权限 |
| `sandbox-adapter.ts:convertToSandboxRuntimeConfig` | 反向：把 Permission 的 allow/deny 规则翻译成 sandbox 的 allowWrite/denyWrite |

设计哲学：**Permission 是编辑时规则，Sandbox 是运行时保险**。两层都失败的概率极低。

---

## 十四、安全设计的关键细节

### 14.1 规则优先级与覆盖

`policySettings` / `flagSettings` 是**最高优先级**：

- `permissionSetup.ts:stripDangerousRules` 把不可信 source 中的危险规则（如 `Bash(*)` 之类的过宽规则）剥离到 `strippedDangerousRules`，但保留它们的"覆盖"语义
- `shouldAllowManagedPermissionRulesOnly()` 让企业只信任 managed settings
- `deletePermissionRule` 对 `policySettings / flagSettings / command` 抛错——**只读**

### 14.2 路径语义差异

`PermissionRule`（Edit/Read）的 `/path` **相对**于该设置所在目录；`sandbox.filesystem.*` 的 `/path` **绝对**。这是 `sandbox-adapter.ts:99-146` 显式注释说明的"双语义"，并通过 `resolvePathPatternForSandbox` / `resolveSandboxFilesystemPath` 分别处理。

### 14.3 Bash subcommand 拆分的陷阱

`splitCommand_DEPRECATED`（regex）会**错误拆分** mid-word `#` 或被引号包围的内容。AST 路径用 tree-sitter 解决。Legacy 路径里：

- `MAX_SUBCOMMANDS_FOR_SECURITY_CHECK = 50`（CC-643）：防止 splitCommand 在恶意输入下指数展开（曾导致 REPL 100% CPU 死循环）
- `MAX_SUGGESTED_RULES_FOR_COMPOUND = 5`：UI 噪音控制

### 14.4 cd+git 攻击防御

`compoundCommandHasCd && hasGitCommand → ask`（`bashPermissions.ts:2209-2225`）：

注释明确：防止 `cd /malicious/dir && git status` 让 git 加载攻击者的 `.git/config`（`core.fsmonitor`、bare-repo 检测）。`isNormalizedGitCommand` 必须先脱掉 `env / timeout / quotes`，否则 `"git" status` 会被绕过。

### 14.5 PowerShell 特殊防御

```ts
// permissions.ts:572-591
if (tool.name === POWERSHELL_TOOL_NAME && !feature('POWERSHELL_AUTO_MODE')) {
  // 不让 PowerShell 走 auto classifier；保留 ask
  return result
}
```

`POWERSHELL_AUTO_MODE` 是 ANT-only feature flag。ANT 用户可以在 prompt 里加 `POWERSHELL_DENY_GUIDANCE` 让 classifier 识别 `iex (iwr ...)` 这种"下载即执行"模式。

### 14.6 Classifier Fail-Closed

`auto mode` + classifier unavailable：

- `tengu_iron_gate_closed` GrowthBook 开关（默认 true）→ **deny**（fail-closed）
- 关闭 → fallback 到 normal prompting（fail-open）

注释（`permissions.ts:843-876`）：fail-closed 是默认，因为 auto mode 不弹窗时 deny 比 silently allow 安全。

### 14.7 `prePlanMode` 状态

`ToolPermissionContext.prePlanMode` 记录 plan mode **之前**的 mode。退出 plan 时恢复。

### 14.8 `requiresUserInteraction()` 

部分 tool（e.g. `AskUserQuestionTool`）声明自己必须交互——在 bypass mode 下也强制 ask（`permissions.ts:1232-1236`）。

---

## 十五、关键文件速查

| 文件 | 行数 | 作用 |
|------|------|------|
| `src/types/permissions.ts` | 441 | 全部 Permission 类型（mode / behavior / rule / update / decision / reason / context） |
| `src/utils/permissions/PermissionMode.ts` | 141 | mode 配置 + title/symbol/颜色 + Zod schema |
| `src/utils/permissions/PermissionRule.ts` | 40 | re-export + Zod schema |
| `src/utils/permissions/PermissionUpdate.ts` | 389 | in-memory apply + disk persist |
| `src/utils/permissions/PermissionUpdateSchema.ts` | ~80 | Zod schema for `PermissionUpdate` |
| `src/utils/permissions/bashClassifier.ts` | 61 | **STUB** for external builds；ANT-only 真实实现 |
| `src/utils/permissions/permissions.ts` | 1486 | `hasPermissionsToUseTool` / `checkRuleBasedPermissions` / 模式后处理 / denial tracking |
| `src/utils/permissions/permissionsLoader.ts` | ~400 | settings.json ↔ PermissionRule 转换 |
| `src/utils/permissions/permissionRuleParser.ts` | ~250 | `Bash(ruleContent)` 字符串 ↔ `PermissionRuleValue` |
| `src/utils/permissions/shellRuleMatching.ts` | ~500 | prefix / exact / wildcard 匹配 |
| `src/utils/permissions/yoloClassifier.ts` | ~600 | auto-mode Haiku classifier |
| `src/utils/permissions/denialTracking.ts` | ~150 | 连续 / 累计 deny 限制 |
| `src/utils/permissions/bypassPermissionsKillswitch.ts` | ~150 | bypass mode 杀死开关 |
| `src/utils/permissions/autoModeState.ts` | ~50 | auto mode 状态 |
| `src/utils/permissions/classifierDecision.ts` | ~200 | safe-tool allowlist + fast-path |
| `src/utils/classifierApprovals.ts` | 89 | UI 显示 classifier-approved 标记的 in-memory store |
| `src/tools/BashTool/bashPermissions.ts` | 2621 | bashToolHasPermission / checkSandboxAutoAllow / classifier integration |
| `src/tools/BashTool/BashTool.tsx` | — | BashTool 的 `checkPermissions` 委托 |
| `src/services/tools/toolExecution.ts` | 1745 | `runToolUse` / `checkPermissionsAndCallTool` / PreToolUse Hook / OTel |
| `src/components/permissions/PermissionRequest.tsx` | 216 | 工具弹窗路由器 |
| `src/components/permissions/BashPermissionRequest/BashPermissionRequest.tsx` | 481 | Bash 弹窗主组件 |
| `src/components/permissions/BashPermissionRequest/bashToolUseOptions.tsx` | 146 | 选项构建（yes / yes-prefix / yes-classifier / no） |
| `src/components/permissions/PermissionDialog.tsx` | — | 通用 dialog 框架 |
| `src/components/permissions/PermissionRuleExplanation.tsx` | — | 解释"为什么这条规则匹配" |
| `src/components/permissions/PermissionExplanation.tsx` | — | Haiku 生成的风险说明（reasoning / risk level） |
| `src/components/permissions/PermissionDecisionDebugInfo.tsx` | — | debug 模式展示决策树 |
| `src/components/permissions/FallbackPermissionRequest.tsx` | — | 通用 fallback |
| `src/components/permissions/SandboxPermissionRequest.tsx` | 162 | 沙箱外网络请求二次确认 |
| `src/components/permissions/{Edit,Write,Read,Notebook,PowerShell,SedEdit,Skill,WebFetch,EnterPlanMode,ExitPlanMode}PermissionRequest/*` | — | 各工具专属弹窗 |

---

## 十六、设计评价

1. **清晰的分层语义**：
   - `PermissionDecisionReason` 8 个变体把"为什么"完整暴露（rule / mode / classifier / hook / subcommandResults / safetyCheck / sandboxOverride / asyncAgent / other），决策日志和 UI 解释都有依据
   - `PermissionBehavior` 三态 + `passthrough` 第四态让流水线每个环节能"不决定 / 让上层决定"

2. **安全与可用的平衡**：
   - `bypassPermissions` 不是"完全放开"——`1d / 1f / 1g`（deny / content-ask rule / safetyCheck）仍生效
   - `auto mode` 不是"完全自动"——`denialTracking` + `transcriptTooLong` fallback + classifier unavailable fail-closed
   - `shouldAvoidPermissionPrompts` 的后台 agent 也不是"无规则"——给 `PermissionRequest` Hook 一次机会

3. **诚实的安全边界标注**：
   - `excludedCommands` 在 sandbox 是 UX 而非安全（已在 [sandbox-implementation.md](sandbox-implementation.md) 中分析）
   - `MAX_SUBCOMMANDS_FOR_SECURITY_CHECK = 50` 是 fail-safe 上限而非完整安全保证
   - `auto mode classifier` 的 `tengu_iron_gate_closed` 默认 fail-closed 体现了"宁可误拒，不可误放"

4. **复杂的性能工程**：
   - 整个 `bashToolHasPermission` **正卡在 Bun 的 `feature()` DCE complexity threshold**——注释反复警告"再加 5 行会破坏 pendingClassifierCheck"
   - 因此把 `checkEarlyExitDeny` / `checkSemanticsDeny` / `filterCdCwdSubcommands` 拆成独立函数
   - `ClassifierCheckingSubtitle` 子组件独立 20fps 时钟，避免整个 PermissionDialog 反复 re-render
   - `speculativeChecks` map 让 allow classifier 与 PreToolUse Hook / permission dialog setup **并行**跑

5. **可改进点**：
   - `permissions.ts` 的 auto-mode 块嵌套极深（530-920 行），建议拆出独立的 `autoModeClassifierDecision.ts`
   - `BashPermissionRequest` 用 `useCompiler-runtime` (`_c(21)`) 大量手动 memoization 表明 React Compiler 仍不能完全推断——可读性受影响
   - `permissionSetup.ts` 与 `permissionsLoader.ts` 有重复的"按 source 分组"逻辑，应抽 `PermissionRuleRepository`
   - `BashPermissionRequest` 仍是 AST/shadow 两套路径并存，未来应统一（注释：TREE_SITTER_BASH 在 external builds 仍为 false）

---

> 本文档仅基于 `src/` 源码静态阅读，未运行时验证。所有引用的代码路径均来自上述文件，标注的代码位置为相对路径 + 起始行号。
>
> 与之配套阅读：[`sandbox-implementation.md`](sandbox-implementation.md) — 应用层权限 ↔ OS 层沙箱的协同。