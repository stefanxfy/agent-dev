# OpenClaw Skill 体系架构解读

> 本文档基于源码逐行阅读写成。所有引用均使用 repo-root 相对路径（形如 `src/agents/skills/workspace.ts:930`），遵循 [AGENTS.md](../AGENTS.md) 的引用约定。
> 文中 "skill" 指本项目自有概念（基于 `SKILL.md` 的可触发指令包）；"CanonicalSkill" 指上游依赖 `@mariozechner/pi-coding-agent` 提供的类型。

---

## TL;DR

OpenClaw 的 skill 体系是一套**面向 LLM 的"按需加载指令包"机制**：每个 skill 是一个目录（`SKILL.md` + 可选 `scripts/`、`references/`、`assets/`），系统把所有 skill 的 `name + description + location` 列成一张表注入 system prompt，由模型在对话中自行判断"当前任务匹配哪个 skill"，再用 read 工具按需读取完整 `SKILL.md`。

它**不是**普通的 plugin（plugin 是代码插件，参与工具注册和 agent 循环；skill 是文档型扩展，零代码即可工作）。整个体系在四个层次上展开：

| 层 | 核心文件 | 职责 |
|---|---|---|
| 契约层 | `src/agents/skills/types.ts`、`skill-contract.ts` | 类型定义、上游 `CanonicalSkill` 扩展、prompt 序列化 |
| 加载层 | `src/agents/skills/workspace.ts`（约 1200 行）、`local-loader.ts`、`bundled-dir.ts`、`plugin-skills.ts` | 多源发现、合并去重、过滤、prompt 构建、热重载 |
| 运维层 | `src/agents/skills-install.ts`、`skills-clawhub.ts`、`skills-status.ts`、`src/security/skill-scanner.ts` | 5 种安装器、远程市场、状态机、安全扫描 |
| 接入层 | `src/agents/skills/command-specs.ts`、`src/auto-reply/skill-commands.ts`、`cli-runner/claude-skills-plugin.ts`、`pi-embedded-runner/skills-runtime.ts` | slash command 化、agent loop prompt 注入、两种 runner 加载 |

**核心设计要点**（后文逐条展开）：

1. **最小侵入式扩展上游**：`Skill = CanonicalSkill & { source?: string }`，只加一个可选字段，prompt 渲染器本地实现并保证与上游**字节级对齐**（`compact-format.test.ts:59-65` 钉死）。
2. **六级来源优先级**：`extra < bundled < managed < agents-skills-personal < agents-skills-project < workspace`，同名 skill 高优先级覆盖低优先级，最终输出按 `name` 字典序排序——这是 prompt cache 友好性的关键。
3. **三级预算降级**：full → compact（去掉 description，保留全部 skill 名）→ truncate（二分搜索最大前缀），永不"静默丢 skill"，超限一定有 `⚠️` 警告。
4. **状态机即时计算，无持久化**：没有 "installed / needs-update" 状态文件，每次调用都基于当前 `requires.bins/env/config/os` 实时求值。
5. **安全是双轨的，且有盲区**：代码扫描器在**安装时强制阻断** critical 威胁，但**运行时加载器不扫描**——一旦 skill 落盘就不再检查；workspace symlink 逃逸由独立扫描器在 audit 时覆盖。
6. **两种 runner 加载方式截然不同**：CLI runner（`claude-cli` backend）把 skill 物化成临时 `--plugin-dir`；pi-embedded runner（同进程）直接内存持有 `SkillEntry[]` 并自拼 system prompt。

---

## 一、整体架构

### 1.1 端到端数据流

```
                          ┌─────────────────────────────────────┐
                          │  6 类 skill 来源（磁盘 / 远程）       │
                          │                                     │
                          │  extra       (config.skills.load.extraDirs + plugin symlinks)
                          │  bundled     (<packageRoot>/skills, OPENCLAW_BUNDLED_SKILLS_DIR)
                          │  managed     (~/.openclaw/skills)
                          │  agents-pers (~/.agents/skills)
                          │  agents-proj (<workspace>/.agents/skills)
                          │  workspace   (<workspace>/skills) ◄── ClawHub 装这里
                          └──────────────────┬──────────────────┘
                                             │  loadSkillEntries (workspace.ts:734)
                                             │  按 name 去重 + 优先级覆盖
                                             ▼
                          ┌─────────────────────────────────────┐
                          │  SkillEntry[]                        │
                          │   { skill, frontmatter, metadata,    │
                          │     invocation, exposure }           │
                          └──────────────────┬──────────────────┘
                                             │  filterSkillEntries (config + eligibility + agentFilter)
                                             │  isSkillVisibleInAvailableSkillsPrompt (visibility)
                                             │  compactSkillPaths + localeCompare 排序
                                             │  applySkillsPromptLimits (full→compact→truncate)
                                             ▼
                          ┌─────────────────────────────────────┐
                          │  SkillSnapshot                       │
                          │   { prompt, skills[], skillFilter,   │
                          │     resolvedSkills, version }        │
                          └──────────────────┬──────────────────┘
                                             │
              ┌──────────────────────────────┼──────────────────────────────┐
              │                              │                              │
              ▼                              ▼                              ▼
   buildWorkspaceSkillCommandSpecs   resolveSkillsPromptForRun     buildWorkspaceSkillStatus
   (command-specs.ts:60)             (workspace.ts:1025)           (skills-status.ts:267)
        │                              │                              │
        ▼                              ▼                              ▼
   SkillCommandSpec[]            "## Skills (mandatory)"        SkillStatusEntry[]
   → auto-reply slash           段塞进 buildSystemPrompt        → CLI / gateway / doctor
     /skill-name 触发           (system-prompt.ts:806)
```

### 1.2 与上游 pi-coding-agent 的关系

OpenClaw 的 skill 系统并不是从零写的，而是**叠加扩展**了 `@mariozechner/pi-coding-agent`：

| 维度 | 上游提供 | OpenClaw 扩展 |
|---|---|---|
| Skill 类型 | `CanonicalSkill`（name/description/filePath/baseDir/sourceInfo/disableModelInvocation） | 加 `source?: string` 一个字段（`skill-contract.ts:6-9`） |
| 来源模型 | `SourceInfo` 嵌套对象 | 加 `SourceScope = "user"\|"project"\|"temporary"`、`SourceOrigin = "package"\|"top-level"` 收窄枚举 |
| Prompt 渲染 | `formatSkillsForPrompt` | 本地实现，注释明确"字节对齐上游 formatter 以避免 cold path 导入整个 pi 包"（`skill-contract.ts:38-43`） |
| 元数据 | 无 | 全新 `OpenClawSkillMetadata`（emoji/os/requires/install/primaryEnv/skillKey/always） |
| 加载结构 | `Skill[]` | 包装为 `SkillEntry`（含 frontmatter + metadata + invocation + exposure） |
| 安装/命令 | 无 | 全新 `SkillInstallSpec`、`SkillCommandSpec`、`SkillSnapshot` |

> **关键契约保护**：`src/agents/skills/compact-format.test.ts:59-65` 用 `expect(local).toBe(upstreamFormatSkillsForPrompt(skills))` 显式断言本地渲染器输出与上游完全相等。这是防止本地 formatter 漂移的回归保护网。

### 1.3 分层文件地图

```
src/
├── agents/
│   ├── skills.ts                         # barrel：re-export + resolveSkillsInstallPreferences
│   ├── skills-install.ts        (578)    # 5 种安装器主逻辑
│   ├── skills-install-download.ts        # download 子流程（沙箱）
│   ├── skills-install-extract.ts         # 解压（zip-slip / TOCTOU 防御）
│   ├── skills-status.ts         (306)    # 状态机（即时计算）
│   ├── skills-clawhub.ts        (465)    # ClawHub skill 安装/更新编排
│   ├── skills/
│   │   ├── types.ts             (2.6K)   # SkillEntry / SkillSnapshot / OpenClawSkillMetadata / SkillInstallSpec
│   │   ├── skill-contract.ts    (2.0K)   # Skill 类型 + formatSkillsForPrompt
│   │   ├── workspace.ts         (40K)    # ⭐ 核心：发现/合并/过滤/prompt 构建/同步
│   │   ├── local-loader.ts               # SKILL.md 文件加载
│   │   ├── frontmatter.ts       (6.2K)   # frontmatter + install spec 安全校验
│   │   ├── bundled-dir.ts                # bundled 目录解析
│   │   ├── plugin-skills.ts     (8.4K)   # 插件贡献 skill（symlink）
│   │   ├── command-specs.ts     (7.1K)   # SkillCommandSpec 派生
│   │   ├── config.ts                     # resolveSkillConfig / shouldIncludeSkill
│   │   ├── filter.ts / agent-filter.ts   # filter 归一 / per-agent 过滤
│   │   ├── refresh.ts / refresh-state.ts # chokidar watcher + 版本号
│   │   ├── env-overrides.ts     (7.9K)   # env / apiKey 注入（含子进程防泄漏）
│   │   ├── runtime-config.ts             # 磁盘 config vs runtime snapshot 选择
│   │   ├── snapshot-hydration.ts         # resolvedSkills 水合
│   │   ├── serialize.ts                  # per-key 串行化
│   │   └── tools-dir.ts                  # per-skill tools 沙箱根
│   ├── cli-runner/
│   │   ├── claude-skills-plugin.ts (142) # CLI runner：物化 skill 为 --plugin-dir
│   │   ├── prepare.ts                    # resolveSkillsPromptForRun 调用点
│   │   └── execute.ts                    # 拼接 claude CLI 参数
│   └── pi-embedded-runner/
│       └── skills-runtime.ts             # pi runner：内存加载
│   └── (run/attempt.ts)                  # pi runner 主循环，prompt 注入点
│
├── infra/
│   ├── clawhub.ts              (1076)    # ⭐ ClawHub API 客户端（远程市场）
│   └── skills-remote.ts        (440)     # 远程配对节点 bin 探测（不是 ClawHub！）
│
├── security/
│   ├── skill-scanner.ts        (712)     # ⭐ 代码扫描器（安装时阻断）
│   └── audit-workspace-skills.ts (205)   # workspace symlink 逃逸扫描
│
├── gateway/
│   ├── server-methods/skills.ts (359)    # 6 个 RPC：status/bins/search/detail/install/update
│   └── protocol/schema/agents-models-skills.ts (511)  # TypeBox wire schema
│
├── cli/
│   ├── skills-cli.ts            (277)    # `openclaw skills` 子命令
│   └── skills-cli.format.ts     (459)    # 表格/详情/check 输出
│
├── auto-reply/
│   ├── skill-commands.ts        (134)    # workspace slash 命令发现
│   ├── skill-commands-base.ts   (99)     # slash 解析
│   └── reply/get-reply-inline-actions.ts # 聊天里 /skill 触发
│
├── agents/system-prompt.ts               # ⭐ "## Skills (mandatory)" 段拼装
│
├── config/types.skills.ts       (47)     # 配置层类型（用户视角）
│
└── cron/isolated-agent/skills-snapshot.ts # cron agent 快照（持久化复用）

skills/                                    # 54 个内置 skill（weather、notion、slack、coding-agent …）
extensions/skill-workshop/                 # skill 提案/审计扩展（含 markdown 扫描器）
```

---

## 二、数据模型与契约

### 2.1 Skill 类型层级

```ts
// src/agents/skills/skill-contract.ts:1-9
import type { Skill as CanonicalSkill, SourceInfo } from "@mariozechner/pi-coding-agent";

export type SourceScope  = "user" | "project" | "temporary";
export type SourceOrigin = "package" | "top-level";

export type Skill = CanonicalSkill & {
  source?: string;   // OpenClaw 唯一扩展字段：保留 legacy 直读 source 的代码路径
};
```

`CanonicalSkill` 实际暴露的字段（从全仓 grep + fixture `skills.test-helpers.ts:34-47` 印证）：

| 字段 | 类型 | 语义 |
|---|---|---|
| `name` | `string` | Skill 唯一名（prompt 里作 `<name>`） |
| `description` | `string` | 描述（prompt 里作 `<description>`；模型据此判断何时触发） |
| `filePath` | `string` | `SKILL.md` 绝对路径（prompt 里作 `<location>`） |
| `baseDir` | `string` | skill 目录（`filePath` 父目录），用于解析 skill 内相对路径 |
| `sourceInfo` | `SourceInfo` | 上游规范来源对象（path/source/scope/origin/baseDir） |
| `disableModelInvocation` | `boolean` | true 时从模型可见的 `<available_skills>` 中排除 |

### 2.2 OpenClawSkillMetadata — 自有元数据

```ts
// src/agents/skills/types.ts:19-33
export type OpenClawSkillMetadata = {
  always?: boolean;       // 强制 eligible，绕过 requires 校验
  skillKey?: string;      // 覆盖默认 config 索引键（默认用 skill.name）
  primaryEnv?: string;    // 主 API key 环境变量名，用于 apiKey → env 注入
  emoji?: string;
  homepage?: string;
  os?: string[];          // 限定 OS，如 ["darwin"]
  requires?: {
    bins?: string[];      // 必须二进制（全部存在才 eligible）
    anyBins?: string[];   // 任一存在即可
    env?: string[];       // 必须环境变量
    config?: string[];    // 必须为 truthy 的配置路径，如 "channels.slack"
  };
  install?: SkillInstallSpec[];
};
```

**关键字段的运行时语义**：

- `always: true` → `evaluateRuntimeEligibility`（`src/shared/config-eval.ts:108-135`）跳过 `requires` 校验直接返回 eligible。用于"不管环境如何都要加载"的核心 skill。
- `skillKey` → `resolveSkillKey`（`frontmatter.ts:221-223`）：`entry?.metadata?.skillKey ?? skill.name`。决定配置 `skills.entries.<key>` 和 bundled allowlist 用什么键索引。
- `primaryEnv` → 与 `skills.entries.<key>.apiKey` 联动：当 `requires.env` 检查的变量名等于 `primaryEnv` 时，配置里给的 `apiKey` 视作该 env 的值（`config.ts:97-102`）。
- `requires.config` → 例如 slack skill 用 `["channels.slack"]`，voice-call skill 用 `["plugins.entries.voice-call.enabled"]`。

### 2.3 SkillInstallSpec — 五种安装器

```ts
// src/agents/skills/types.ts:3-17
export type SkillInstallSpec = {
  id?: string;                                    // 缺省为 `${kind}-${index}`
  kind: "brew" | "node" | "go" | "uv" | "download";
  label?: string;
  bins?: string[];                                // 安装后会提供的二进制
  os?: string[];
  formula?: string;                               // brew
  package?: string;                               // node / uv
  module?: string;                                // go（example.com/tool@latest）
  url?: string;                                   // download
  archive?: string;                               // download 归档类型
  extract?: boolean;
  stripComponents?: number;
  targetDir?: string;                             // 受沙箱限制
};
```

每种 kind 的必填字段在 `frontmatter.ts:168-182` 强制（缺失则该 spec 被静默丢弃）：`brew→formula`、`node→package`、`go→module`、`uv→package`、`download→url`。

### 2.4 SkillEntry / SkillSnapshot — 加载后的运行时记录

```ts
// src/agents/skills/types.ts:78-84
export type SkillEntry = {
  skill: Skill;
  frontmatter: ParsedSkillFrontmatter;
  metadata?: OpenClawSkillMetadata;
  invocation?: SkillInvocationPolicy;     // { userInvocable, disableModelInvocation }
  exposure?: SkillExposure;               // { includeInRuntimeRegistry, includeInAvailableSkillsPrompt, userInvocable }
};

// src/agents/skills/types.ts:95-102
export type SkillSnapshot = {
  prompt: string;                          // 已计算好的 <available_skills> 文本
  skills: Array<{ name, primaryEnv?, requiredEnv? }>;  // 摘要，供 UI / 状态查询
  skillFilter?: string[];                  // 构建 snapshot 时的 agent filter
  resolvedSkills?: Skill[];                // 规范绝对路径（不压缩 ~），供 runtime 消费
  version?: number;                        // snapshot 版本（来自 refresh-state）
};
```

`SkillSnapshot` 是给 session 用的**完整快照**：`prompt` 已预算好（路径已 `~/` 压缩），`resolvedSkills` 保留规范绝对路径——这两个分离是为了让 prompt 字节稳定（cache 友好）同时 runtime 仍能用真实路径调 read 工具。`resolvedSkills` 是 **runtime-only**，session 持久化只保存轻量 catalog，consumers 需要时通过 `hydrateResolvedSkills`（`snapshot-hydration.ts`）重扫水合。

### 2.5 SKILL.md 文件格式

依据 `skills/skill-creator/SKILL.md:46-80` 自描述：

```
skill-name/
├── SKILL.md              (required)
│   ├── YAML frontmatter  (required，--- 包围)
│   │   ├── name:         (required)
│   │   └── description:  (required)
│   └── Markdown body     (required，仅触发后才被 read 加载)
└── (optional) scripts/   references/   assets/
```

**Frontmatter 字段**：

| 字段 | 必填 | 默认 | 说明 |
|---|---|---|---|
| `name` | ✅ | (目录名) | Skill 名；缺 name 时 loader 回退目录名，但缺 description 直接拒绝加载 |
| `description` | ✅ | — | 决定何时触发；模型可见 |
| `homepage` | ❌ | — | 文档主页 URL |
| `metadata` | ❌ | — | **必须是字符串**：内嵌 JSON5，其 `openclaw` 子对象解析为 `OpenClawSkillMetadata` |
| `user-invocable` | ❌ | `true` | 用户能否 `/skill` 调用 |
| `disable-model-invocation` | ❌ | `false` | 禁止模型自发触发 |

注意 `metadata` 在 YAML 里以 **JSON 字符串**形式给出（被 YAML 当 inline scalar 解析成字符串），随后用 `JSON5.parse` 二次解析（见 `frontmatter.ts:187-207` 的 `resolveOpenClawMetadata`）。真实示例 `skills/weather/SKILL.md:1-23`：

```yaml
---
name: weather
description: "Get current weather, rain, temperature, and forecasts for locations or travel planning."
homepage: https://wttr.in/:help
metadata:
  {
    "openclaw":
      {
        "emoji": "☔",
        "requires": { "bins": ["curl"] },
        "install":
          [
            { "id": "brew", "kind": "brew", "formula": "curl", "bins": ["curl"], "label": "Install curl (brew)" },
          ],
      },
  }
---
```

### 2.6 Frontmatter 双轨解析

`src/markdown/frontmatter.ts:195-226` 的 `parseFrontmatterBlock` 同时跑两套解析器再合并：

1. **YAML 解析**（`parseYamlFrontmatter`, 行 55-77）— 用 `yaml` 包 core schema，对象类型通过 `JSON.stringify` 转字符串（服务于 metadata 块）。
2. **逐行键值解析**（`parseLineFrontmatter`, 行 102-150）— 正则 `/^([\w-]+):\s*(.*)$/`，支持多行延续与引号剥离。

合并规则（行 207-223）：YAML 优先，但当 inline 行值含 `:` 且 YAML 把它当结构化对象时回退到行值（`shouldPreferInlineLineValue`, 行 166-181）。这保证 `description: foo: bar` 这种含冒号的描述不被 YAML 误判。所有解析失败都**静默回退**（`try/catch` 返回 `null`/`{}`）。

### 2.7 配置层（用户视角）vs 运行时层（加载视角）

配置层 `src/config/types.skills.ts` 完全是用户配置视角，不引用任何运行时类型：

```ts
export type SkillConfig = {           // 单个 skill 的配置条目
  enabled?: boolean;
  apiKey?: SecretInput;               // 支持 secret reference
  env?: Record<string, string>;
  config?: Record<string, unknown>;
};

export type SkillsConfig = {
  allowBundled?: string[];            // bundled skill 白名单
  load?: { extraDirs?: string[]; watch?: boolean; watchDebounceMs?: number };
  install?: { preferBrew?: boolean; nodeManager?: "npm"|"pnpm"|"yarn"|"bun" };
  limits?: { maxCandidatesPerRoot?; maxSkillsLoadedPerSource?; maxSkillsInPrompt?; maxSkillsPromptChars?; maxSkillFileBytes? };
  entries?: Record<string, SkillConfig>;
};
```

两层通过 `src/agents/skills/config.ts` 的四个纯函数桥接：

| 函数 | 作用 |
|---|---|
| `resolveSkillConfig(config, skillKey)` | 在 `config.skills.entries[skillKey]` 查找 |
| `resolveBundledAllowlist(config)` | 规范化 `config.skills.allowBundled` |
| `isBundledSkillAllowed(entry, allowlist?)` | 仅对 `source === "openclaw-bundled"` 应用白名单；非 bundled 一律 true |
| `shouldIncludeSkill({entry, config, eligibility})` | 综合 `enabled` + allowlist + `evaluateRuntimeEligibility` |

**两个交汇点**：
- **`skillKey`** — 配置 `entries.<skillKey>` ↔ `metadata.skillKey ?? skill.name`
- **`primaryEnv`** — 运行时 `metadata.primaryEnv` ↔ 配置 `entries.<skillKey>.apiKey`（运行时被注入到 `process.env[primaryEnv]`）

---

## 三、多源发现与加载

### 3.1 六级来源优先级

所有来源在 `workspace.ts:734-809` 的 `loadSkillEntries` 内部统一发现并合并：

| # | source 标识 | 目录 | 说明 |
|---|---|---|---|
| 1 | `openclaw-extra` | `config.skills.load.extraDirs` + `~/.openclaw/plugin-skills/`（插件 symlink） | 用户/项目额外目录，**最低**优先级 |
| 2 | `openclaw-bundled` | `resolveBundledSkillsDir()`（`<packageRoot>/skills` 或可执行文件同级） | 内置 skill |
| 3 | `openclaw-managed` | `~/.openclaw/skills`（即 `CONFIG_DIR/skills`） | 通过安装/管理命令落地 |
| 4 | `agents-skills-personal` | `~/.agents/skills` | 个人级，跨项目共享 |
| 5 | `agents-skills-project` | `<workspace>/.agents/skills` | 项目级 |
| 6 | `openclaw-workspace` | `<workspace>/skills` | 项目 workspace，**最高**优先级 |

**合并算法**（`workspace.ts:790-809`）：用 `Map<string, LoadedSkillRecord>` 按 `skill.name` 去重，**后写入覆盖先写入**：

```ts
// Precedence: extra < bundled < managed < agents-skills-personal < agents-skills-project < workspace
const merged = new Map<string, LoadedSkillRecord>();
for (const record of extraSkills)         merged.set(record.skill.name, record);
for (const record of bundledSkills)       merged.set(record.skill.name, record);
for (const record of managedSkills)       merged.set(record.skill.name, record);
for (const record of personalAgentsSkills) merged.set(record.skill.name, record);
for (const record of projectAgentsSkills)  merged.set(record.skill.name, record);
for (const record of workspaceSkills)      merged.set(record.skill.name, record);
```

> 注：**没有**独立的 "temporary" source 类型——`temporary` 只是 `SourceScope` 的一个值（`source.ts:3`），用于 `createSyntheticSourceInfo`；本地加载实际打 `scope: "project"`（`local-loader.ts:87`）。ClawHub 装下来的 skill 落在 `<workspace>/skills/<slug>`，作为标准 workspace skill 参与合并（`workspace` 优先级最高，会覆盖同名 bundled/managed skill）。

### 3.2 Bundled 目录解析的回退链

`resolveBundledSkillsDir()`（`bundled-dir.ts:36-90`）解析顺序：

1. 环境变量 `OPENCLAW_BUNDLED_SKILLS_DIR`（最高优先）；
2. `bun --compile` 场景：可执行文件同级的 `skills/`；
3. npm/dev：`resolveOpenClawPackageRootSync(...)` 解析包根 + `skills/`，并用 `looksLikeSkillsDir` 验证；
4. 从当前 module dir 向上最多走 6 层查找 `skills/`；
5. 都失败返回 `undefined`（`bundled-context.ts` 仅警告一次，避免刷屏）。

`looksLikeSkillsDir`（`bundled-dir.ts:6-27`）要求目录含 `.md` 文件或子目录含 `SKILL.md`，避免误命中空目录。

### 3.3 加载流水线

```
┌────────────────────────────────────────────────────────────────────┐
│ 磁盘 SKILL.md (per skill dir)                                       │
│  local-loader.ts: loadSkillsFromDirSafe → loadSingleSkillDirectory │
│    • openVerifiedFileSync (reject symlink, maxBytes=256KB)          │
│    • parseFrontmatter (frontmatter.ts:24)                           │
│    • resolveSkillInvocationPolicy                                   │
│    • createSyntheticSourceInfo(scope="project")                     │
└──────────────────────────┬─────────────────────────────────────────┘
                           ▼
┌────────────────────────────────────────────────────────────────────┐
│ workspace.ts: loadSkillEntries (private)                            │
│  • resolveNestedSkillsRoot: 自动识别 dir/skills/* 嵌套根              │
│  • listChildDirectories: 单层扫描（maxRawEntriesToScan 防爆）         │
│  • 嵌套二层兜底：dir/<name>/<nested>/SKILL.md                        │
│  • resolveContainedSkillPath + tryRealpath 防路径逃逸                │
│  • maxSkillFileBytes=256KB / maxCandidatesPerRoot=300               │
│  • maxSkillsLoadedPerSource=200                                     │
│  • Map<name, record> 按 6 级优先级合并去重                          │
└──────────────────────────┬─────────────────────────────────────────┘
                           ▼
                SkillEntry[] (skill + frontmatter + metadata + invocation + exposure)
```

`loadSingleSkillDirectory` 的几个关键安全约束（`local-loader.ts`）：

- **拒绝 symlink**：`openVerifiedFileSync` 显式 reject symlink 的 SKILL.md（plugin-skills 路径有独立的 symlink 校验逻辑，见 3.4）。
- **大小上限**：单文件 256KB，超出直接拒绝。
- **缺 description 直接拒绝**：缺 name 可回退目录名，但缺 description 视为非法 skill。

### 3.4 Plugin skill 的 symlink 约束

插件通过 `~/.openclaw/plugin-skills/` 下的 symlink 贡献 skill。`workspace.ts:419-508` 强制：

- 每个插件 skill 目录**必须是 symlink**；
- symlink 的 `realpath` **必须落在** `resolvePluginSkillDirs()` 返回的 root 集合内（防 symlink 逃逸）；
- SKILL.md 本身**必须是普通文件**，不能是 symlink（`workspace.ts:472`）；
- 逃逸会被记录为 `bundled-symlink-escape` / `bundled-root-escape`（`workspace.ts:254-317`）。

### 3.5 buildWorkspaceSkillSnapshot 核心算法

定义在 `workspace.ts:930-947`，但全部逻辑委托给私有函数 `resolveWorkspaceSkillPromptState`（`workspace.ts:979-1023`）：

1. **entries 获取**：`opts.entries ?? loadSkillEntries(workspaceDir, opts)`（允许外部预加载传入）。
2. **effective skillFilter 解析**（`workspace.ts:967-977`）：显式 `opts.skillFilter` > `resolveEffectiveAgentSkillFilter(config, agentId)`。
3. **过滤**：`filterSkillEntries(skillEntries, config, effectiveSkillFilter, eligibility)`。
4. **可见性筛选**：`promptEntries = eligible.filter(isSkillVisibleInAvailableSkillsPrompt)`。
5. **路径压缩**：`compactSkillPaths(resolvedSkills)` 把 home 前缀替换为 `~/`，每 skill 省 5-6 token。
6. **确定性排序**：`.sort((a, b) => a.name.localeCompare(b.name, "en"))`（`workspace.ts:1003-1004`）。
7. **预算自适应**：`applySkillsPromptLimits` 决定 full / compact / truncate。
8. **返回**：`eligible`（含被 visibility 隐藏的）+ `prompt`（已渲染）+ `resolvedSkills`（仅可见，规范路径）。

**去重与排序总结**：

- 去重仅按 `skill.name` 字符串相等，在 6 级优先级 Map 中"后者覆盖前者"。
- 所有数组在多个层级都用 `name.localeCompare(other.name, "en")` 排序（`workspace.ts:812, 504-505, 727-728, 1004`）——**这是 prompt cache 友好性的关键**，同 skill 集合生成的 prompt 字节级稳定。
- **不按 source/precedence 排序**——优先级只在合并去重时起作用，最终输出统一按名字排序。

### 3.6 三级预算降级

`applySkillsPromptLimits`（`workspace.ts:878-928`），limits 来自 `resolveSkillsLimits`（`workspace.ts:157-171`）：

| Limit | 默认值 | 行 |
|---|---|---|
| `maxCandidatesPerRoot` | 300 | `workspace.ts:124` |
| `maxSkillsLoadedPerSource` | 200 | `workspace.ts:125` |
| `maxSkillsInPrompt` | 150 | `workspace.ts:126` |
| `maxSkillsPromptChars` | 18_000 | `workspace.ts:127` |
| `maxSkillFileBytes` | 256_000 (256KB) | `workspace.ts:128` |

`maxSkillsPromptChars` 可被 per-agent `skillsLimits.maxSkillsPromptChars` 覆盖（`agent-filter.ts:38-51`）。

**降级算法**（`workspace.ts:894-925`）：

```
1. 先按 maxSkillsInPrompt 截断：byCount = skills.slice(0, maxSkillsInPrompt)

2. Tier 1 — Full：若 formatSkillsForPrompt(byCount).length ≤ maxSkillsPromptChars，直接用

3. Tier 2 — Compact：full 超预算但 formatSkillsCompact(...) fits
   （预算 = maxSkillsPromptChars - 150，预留 warning overhead）
   → 全部保留但降级为 compact（不丢 skill，只丢 description）

4. Tier 3 — Binary search truncate：compact 仍超
   → 对 compact 表达做二分搜索找最大前缀 slice(0, lo)
   → truncated = true
```

最终 `truncationNote`（`workspace.ts:1010-1014`）会前置警告：

```
⚠️ Skills truncated: included 87 of 142 (compact format, descriptions omitted). Run `openclaw skills check` to audit.
```

或非截断的 compact 模式：

```
⚠️ Skills catalog using compact format (descriptions omitted). Run `openclaw skills check` to audit.
```

> **设计意图**：永远先尝试保留"存在感"（全部 skill 名）再考虑丢 skill，且超限一定有可观测的 `⚠️` 警告——不静默缩水。

### 3.7 syncSkillsToWorkspace — 沙箱同步

`workspace.ts:1122-1188`。当 `workspaceAccess !== "rw"` 时（sandbox 场景），把源 workspace 的 skill 复制到 sandbox workspace：

```ts
// src/agents/sandbox/context.ts:62 调用
await syncSkillsToWorkspace({
  sourceWorkspaceDir: agentWorkspaceDir,
  targetWorkspaceDir: sandboxWorkspaceDir,
  config, skillFilter, agentId, eligibility, ...
});
```

关键设计：

- **串行化**：`serializeByKey("syncSkills:<target>", task)`（`serialize.ts`）保证对同一 target 不并发同步。`prev.then(task, task)` 意味着前一个失败也不阻塞下一个。
- **destination 名称唯一性**：`resolveUniqueSyncedSkillDirName` 先用源 `baseDir` 名，冲突则后缀 `-2/-3/...`。
- **沙箱安全**：`resolveSyncedSkillDestinationPath` 用 `resolveSandboxPath` 把目标路径限定在 `targetSkillsDir` 内。
- **过滤拷贝**：`fsp.cp` 的 `filter` 排除 `.git` 和 `node_modules`。

---

## 四、Prompt 构建与 agent loop 注入

### 4.1 Prompt 序列化：`<available_skills>` XML 块

完整格式 `formatSkillsForPrompt`（`skill-contract.ts:44-64`），与上游字节对齐：

```
\n\nThe following skills provide specialized instructions for specific tasks.
Use the read tool to load a skill's file when the task matches its description.
When a skill file references a relative path, resolve it against the skill directory (parent of SKILL.md / dirname of the path) and use that absolute path in tool commands.

<available_skills>
  <skill>
    <name>{escapeXml(skill.name)}</name>
    <description>{escapeXml(skill.description)}</description>
    <location>{escapeXml(skill.filePath)}</location>
  </skill>
  ...
</available_skills>
```

`escapeXml`（`skill-contract.ts:29-36`）转义 `& < > " '`。

紧凑格式 `formatSkillsCompact`（`workspace.ts:856-873`），仅 `name + location`，省略 `description`，第二行从 "matches its **description**" 改为 "matches its **name**"。

### 4.2 可见性策略

`isSkillVisibleInAvailableSkillsPrompt`（`workspace.ts:91-99`）三档回退：

```ts
function isSkillVisibleInAvailableSkillsPrompt(entry: SkillEntry): boolean {
  if (entry.exposure) {
    return entry.exposure.includeInAvailableSkillsPrompt !== false;     // 新策略
  }
  if (entry.invocation) {
    return entry.invocation.disableModelInvocation !== true;            // 兼容
  }
  return entry.skill.disableModelInvocation !== true;                   // legacy
}
```

`exposure` 在 `loadSkillEntries` 末尾构建（`workspace.ts:829-836`）：

- `includeInRuntimeRegistry: true`
- `includeInAvailableSkillsPrompt: invocation.disableModelInvocation !== true`
- `userInvocable: invocation.userInvocable !== false`

即 `disable-model-invocation: true` 的 skill **不出现在 prompt 的 `<available_skills>`**，但仍可进入 runtime registry（用户可显式 `/invoke`）。这正是 CLI `formatSkillsCheck` 里 "Ready but hidden from model prompt" 分类的来源。

### 4.3 system prompt 注入点

注入发生在 `src/agents/system-prompt.ts:806-809`：

```ts
const skillsSection = buildSkillsSection({
  skillsPrompt,    // 来自 params.skillsPrompt
  readToolName,    // 例如 "read"
});
```

`buildSkillsSection`（`system-prompt.ts:197-213`）生成固定的 `## Skills (mandatory)` 头部指令 + 把 `skillsPrompt` 原文粘到尾部。头部指令要点：

- "Before replying: scan `<available_skills>` `<description>` entries."
- 命中一个 → 用 read 工具读 SKILL.md 的 `<location>`，**必须用 available_skills 里的精确 location**，禁止猜/编/硬编码路径。
- 多个候选 → 选最具体的。
- 无命中 → 不读任何 SKILL.md。
- "never read more than one skill up front"。
- 对外部 API 写操作注意 rate limit / 429。

**段在最终 prompt 中的相对位置**（`system-prompt.ts:977`）：

```
...safetySection → OpenClaw CLI Quick Reference →
  ## Skills (mandatory) ▶ ← 这里
→ memorySection → Self-Update → Model Aliases → Workspace → ...
```

### 4.4 skillsPrompt 字符串的构造链

核心函数 `resolveSkillsPromptForRun`（`workspace.ts:1025-1046`）：

```ts
export function resolveSkillsPromptForRun(params) {
  const snapshotPrompt = params.skillsSnapshot?.prompt?.trim();
  if (snapshotPrompt) return snapshotPrompt;        // 优先用预构建的 snapshot（cache 友好）
  if (params.entries && params.entries.length > 0) {
    return buildWorkspaceSkillsPrompt(params.workspaceDir, { entries: params.entries, ... });
  }
  ...
}
```

底层 builder `buildWorkspaceSkillsPrompt`（`workspace.ts:949-954`）→ `resolveWorkspaceSkillPromptState`（见 §3.5）。

最终输出（`workspace.ts:1015-1021`）：

```ts
const prompt = [
  remoteNote,            // 远端 macOS node 提示（如有）
  truncationNote,        // ⚠️ truncation/compact 警告
  compact ? formatSkillsCompact(skillsForPrompt) : formatSkillsForPrompt(skillsForPrompt),
].filter(Boolean).join("\n");
```

### 4.5 两种 runner 的加载差异

两种 runner 代表两种根本不同的架构：

#### CLI runner（外部 CLI 子进程模型）

`src/agents/cli-runner/claude-skills-plugin.ts:78-142` 的 `prepareClaudeCliSkillsPlugin`：

- **触发条件**（`claude-skills-plugin.ts:82-84`）：仅当 `backendId === "claude-cli"` 才生效。
- **物化 skill 到磁盘**：在 `resolvePreferredOpenClawTmpDir()` 下 `mkdtemp` 一个临时目录（`mode: 0o700`），建出 `openclaw-skills/.claude-plugin/` + `openclaw-skills/skills/` 两级。
- **写 plugin manifest**（`claude-skills-plugin.ts:100-113`）：

  ```json
  { "name": "openclaw-skills", "version": "0.0.0",
    "description": "Session-scoped OpenClaw skills selected for this agent run.",
    "skills": "./skills" }
  ```
- **link 或 copy**（`claude-skills-plugin.ts:62-76`）：先 `fs.symlink`（Windows 用 junction），失败 fallback 到 `fs.cp -r`。
- **产出 CLI 参数**：`{ args: ["--plugin-dir", pluginDir], cleanup, pluginDir }`，cleanup 删除临时目录。
- **调用点**：`cli-runner/execute.ts:277-285` 把 args 拼到 `claude` 命令行前部。

> **关键**：CLI runner **不靠 system prompt 注入**让 Claude 找 SKILL.md，而是利用 Claude Code 原生的 `--plugin-dir` 机制把 skill 物化成 Claude 自己识别的 plugin 形态。OpenClaw 的 `skillsPrompt` 仍然会进 system prompt，但 Claude CLI 还会额外通过 plugin 机制发现这些 skill。

#### Pi embedded runner（同进程 / API 模型）

`src/agents/pi-embedded-runner/skills-runtime.ts:5-22` 的 `resolveEmbeddedRunSkillEntries`：

```ts
export function resolveEmbeddedRunSkillEntries(params) {
  const shouldLoadSkillEntries = !params.skillsSnapshot || !params.skillsSnapshot.resolvedSkills;
  const config = resolveSkillRuntimeConfig(params.config);
  return {
    shouldLoadSkillEntries,
    skillEntries: shouldLoadSkillEntries
      ? loadWorkspaceSkillEntries(params.workspaceDir, { config, agentId: params.agentId })
      : [],
  };
}
```

- **不物化到磁盘** — 直接内存持有 `SkillEntry[]`。
- **快照优先**：若 `skillsSnapshot.resolvedSkills` 已存在，跳过重复加载。
- **不创建 plugin 目录、不传 `--plugin-dir`** — 因为没有外部 CLI 进程。
- 调用点 `run/attempt.ts:716-738`：拿到 entries 后做三件事：
  1. `applySkillEnvOverridesFromSnapshot` 或 `applySkillEnvOverrides`（取决于是否有 snapshot）
  2. `resolveSkillsPromptForRun` 把元数据变成 prompt 字符串
  3. 把 prompt 字符串塞进同进程构造的 system prompt

attempt 还在 `attempt.ts:1205` 做了条件屏蔽：

```ts
const effectiveSkillsPrompt = params.toolsAllow?.length ? undefined : skillsPrompt;
```

即 **当 toolsAllow 显式限定工具集时，skill prompt 被跳过**（模型看不到完整工具，按 skills 指令去 `read` SKILL.md 可能失败）。

#### 差异对比

| 维度 | claude-skills-plugin（CLI runner） | skills-runtime（pi-embedded） |
|---|---|---|
| 目标 runner | `claude-cli` backend | pi 同进程 runner |
| 加载形态 | 物化到磁盘 → `--plugin-dir` CLI 参数 | 内存 `SkillEntry[]` |
| 发现机制 | Claude CLI 原生 plugin 发现 | OpenClaw 自拼 system prompt |
| 是否依赖 system prompt | 仍注入但非主要通道 | 完全依赖 |
| 资源管理 | 需要 cleanup（删临时目录） | 无磁盘副作用 |

### 4.6 端到端调用链示例

**用户聊天发 `/weather 北京`** →

1. `auto-reply` 接收 → `resolveReplyDirectives`（`get-reply-directives.ts:143`）检测到 `/`、加载 `listSkillCommandsForWorkspace` → reserve 命令名
2. `resolveInlineActions`（`get-reply-inline-actions.ts`）→ `resolveSlashCommandName` 拿到 `weather`（非内置）→ `resolveSkillCommandInvocation` 匹配到 `SkillCommandSpec`
3. 检查 `command.isAuthorizedSender` → 走 dispatch 或 prompt rewrite
4. 若 prompt rewrite → 改 `ctx.BodyForAgent` → 进 pi-embedded-runner/run/attempt.ts
5. attempt.ts 在 `prepStages` 阶段：
   - `resolveEmbeddedRunSkillEntries`（`attempt.ts:716`）加载 skill entries
   - `applySkillEnvOverrides`（`attempt.ts:727`）注入 env（带 reverter）
   - `resolveSkillsPromptForRun`（`attempt.ts:732`）构造 skillsPrompt
6. skillsPrompt 进 `buildSystemPrompt`（`attempt.ts:1271`）→ `buildSkillsSection`（`system-prompt.ts:806`）→ 拼到 `## Skills (mandatory)` 段
7. LLM 看到指令后用 read 工具读对应 SKILL.md 的 `<location>` → 执行
8. run 结束 → reverter 还原 `process.env`

---

## 五、Skill 命令化（slash command）

skill 除了"模型自发触发"外，还可以暴露为**用户可调用的 slash command**（`/skill-name`）。这条路径由 `command-specs.ts` 派生、由 `auto-reply` 触发。

### 5.1 SkillCommandSpec 派生

核心函数 `buildWorkspaceSkillCommandSpecs()`（`command-specs.ts:60-199`）：

```
1. loadVisibleWorkspaceSkillEntries(workspaceDir, ...)   加载可见 entries
2. filter: entry.invocation?.userInvocable !== false      只保留用户可调用
3. reservedNames = listReservedChatSlashCommandNames()    从内置命令收集保留名（/approve, /status ...）
4. sanitizeSkillCommandName(name)                         清洗：lowercase + 非 [a-z0-9_] → _，截 32 字符
5. resolveUniqueSkillCommandName(name, reserved)          冲突追加 _2, _3…
6. 解析 dispatch（从 frontmatter）:
     command-dispatch: tool           仅支持 "tool"
     command-tool: <toolName>         要调度的工具名
     command-arg-mode: raw            仅支持 "raw"
   不满足 → dispatch 为 undefined → 走默认 prompt-rewrite 路径
7. bundle commands: loadEnabledClaudeBundleCommands()     额外加载 Claude bundle 命令
```

输出类型 `SkillCommandSpec`（`types.ts:51-63`）：

```ts
export type SkillCommandSpec = {
  name: string;                       // slash 命令名（已清洗去重）
  skillName: string;                  // 原 skill 名
  description: string;
  descriptionLocalizations?: Record<string, string>;
  dispatch?: SkillCommandDispatchSpec;   // { kind:"tool", toolName, argMode? }
  promptTemplate?: string;               // bundle 命令模板
  sourceFilePath?: string;
};
```

`sanitizeSkillCommandName`（`command-specs.ts:33-40`）保证命令名安全：lowercase + 非 `[a-z0-9_]` 替换为 `_` + 截断 32 字符 + 空名回退 `"skill"`。

### 5.2 auto-reply 触发机制

`resolveSkillCommandInvocation`（`skill-commands-base.ts:59-99`）支持两种调用形式：

- **形式 A — 泛型 slash**：直接 `/skill-name [args]`
- **形式 B — 显式 skill 命令**：`/skill <name> [args]`

`findSkillCommand`（`skill-commands-base.ts:35-57`）四种匹配：精确名 / 精确 skillName / 空格归一化（`normalizeSkillCommandLookup` 把空格和下划线都转 `-`）。

### 5.3 两种触发后处理路径

触发后在 `get-reply-inline-actions.ts:283-353` 分流：

**(a) Tool dispatch**（`dispatch?.kind === "tool"`）：

- `createOpenClawTools(...)` 创建 tool 集，按 `applyOwnerOnlyToolPolicy` 过滤
- 找到 `dispatch.toolName` 对应的 tool，调 `tool.execute(toolCallId, { command, commandName, skillName, ... })`
- 把 tool result 当作 reply 返回 — **不进入 LLM**

**(b) Prompt rewrite**（默认）：

- 若 `promptTemplate` 存在（bundle 命令）：用 `expandBundleCommandPromptTemplate(template, args)` 展开
- 否则拼装：`Use the "${skillName}" skill for this request.` + `User input:\n${args}`
- 让改写后的 body 走正常 LLM 回复流程，LLM 在 system prompt 的 `## Skills` 段引导下 read SKILL.md

**权限门**（`get-reply-inline-actions.ts:275-281`）：`skillInvocation` 解析成功但 `!command.isAuthorizedSender` 时静默丢弃（`Ignoring /${name} from unauthorized sender`）。

### 5.4 Gateway 暴露给 UI 的命令列表

`src/gateway/server-methods/commands.ts:206-242` 的 `buildCommandsListResult`：

- 调 `listSkillCommandsForAgents({cfg, agentIds: [agentId]})`
- 用 `skillKeys = Set(skillCommands.map(sc => "skill:${sc.skillName}"))` 给每个命令打 `source: "skill" | "native"` 标签
- 这是 Control UI / 客户端枚举可用 `/命令` 的接口

---

## 六、安装与生命周期

### 6.1 安装偏好解析

`resolveSkillsInstallPreferences`（`src/agents/skills.ts:41-50`）：

```ts
export function resolveSkillsInstallPreferences(config?) {
  const raw = config?.skills?.install;
  const preferBrew = raw?.preferBrew ?? true;                       // 默认 true
  const manager = normalizeLowercaseStringOrEmpty(normalizeOptionalString(raw?.nodeManager));
  const nodeManager = ["pnpm","yarn","bun","npm"].includes(manager) ? manager : "npm";
  return { preferBrew, nodeManager };
}
```

- `preferBrew` 默认 `true`（来源 `config.skills.install.preferBrew`）。
- `nodeManager` 默认 `"npm"`；合法值 `pnpm/yarn/bun/npm`，其他值回退 `npm`。
- 交互式 onboard 界面只暴露 `npm/pnpm/bun` 三项（`onboard-helpers.ts:184-193`）；非交互式 onboard 校验 `["npm","pnpm","bun"]`（**不允许 yarn**，`onboard-non-interactive/local/skills-config.ts:11-31`）。

### 6.2 完整安装流程

主入口 `installSkill`（`skills-install.ts:452-568`）：

```
Step 0  入参规整：timeoutMs 钳制到 [1s, 15min]，默认 5min

Step 1  loadWorkspaceSkillEntries(workspaceDir) → 找到 entry.skill.name === skillName

Step 2  findInstallSpec(installId) 定位 install spec
        （installId 缺省时由 resolveInstallId 生成 `${kind}-${index}`）

Step 3  ⭐ 安全扫描 scanSkillInstallSource
        → scanResult.blocked 为真直接失败
          （错误码 security_scan_blocked / security_scan_failed）
        → 非 bundled 源追加 WARNING（元数据可能被攻击者控制）

Step 4  kind === "download" 走独立子流程 installDownloadSpec（见 §6.5）

Step 5  resolveSkillsInstallPreferences + buildInstallCommand(spec, prefs)
        构造命令（严格正则白名单，见 §6.3）

Step 6  依赖/前置工具补齐：
        brew → hasBinary("brew") 或 resolveBrewExecutable()
        uv   → ensureUvInstalled（brew 可用则 brew install uv，否则报错指向官方文档）
        go   → ensureGoInstalled（brew → apt-get+sudo → 报错指向 go.dev）

Step 7  环境变量准备：
        node → buildNodeInstallEnv（仅 npm 设 NPM_CONFIG_PREFIX 到 stateDir/tools/node/npm）
        go   → 若 brew 可用，resolveBrewBinDir 设为 GOBIN（不信任 HOMEBREW_PREFIX！）

Step 8  executeInstallCommand（runCommandSafely 包裹 runCommandWithTimeout）
        code === 0 → createInstallSuccess；否则 formatInstallFailureMessage

Step 9  withWarnings 包裹 warnings 返回
```

### 6.3 五种安装器的命令构造与安全白名单

`buildInstallCommand`（`skills-install.ts:162-218`）：

| kind | 命令 | 必填字段 | 校验正则 |
|---|---|---|---|
| brew | `brew install <formula>` | `formula` | `SAFE_BREW_FORMULA` |
| node | 由 `buildNodeInstallCommand` 决定 | `package` | `SAFE_NODE_PACKAGE` |
| go | `go install <module>` | `module` | `SAFE_GO_MODULE`（强制 `@version`） |
| uv | `uv tool install <package>` | `package` | `SAFE_UV_PACKAGE` |
| download | 单独处理 | `url` | — |

`nodeManager` 命令分支（`skills-install.ts:100-111`）：

```ts
switch (prefs.nodeManager) {
  case "pnpm": return ["pnpm", "add", "-g", "--ignore-scripts", packageName];
  case "yarn": return ["yarn", "global", "add", "--ignore-scripts", packageName];
  case "bun":  return ["bun", "add", "-g", "--ignore-scripts", packageName];
  default:     return ["npm", "install", "-g", "--ignore-scripts", packageName];
}
```

> **关键安全点**：所有 node 命令统一带 `--ignore-scripts`，避免 npm 包 postinstall 脚本执行（供应链防御）。仅 npm 附加 `NPM_CONFIG_PREFIX` env（`skills-install.ts:130-142`）。

所有正则在 `assertSafeInstallerValue`（`skills-install.ts:151-160`）作为**选项注入防御**：空值、以 `-` 开头、字符不在白名单一律拒绝。

### 6.4 多 spec 时的优先级选择

`selectPreferredInstallSpec`（`skills-status.ts:69-111`）：仅当某 skill 声明多个 install spec 且**不全是 download** 时，按"表驱动 + 首匹配胜出"链选择**一个**暴露给用户/CLI：

```ts
const pickers = [
  () => (prefs.preferBrew && brewAvailable ? brewSpec : undefined),  // 1. preferBrew && brew 可用
  () => uvSpec,                                                       // 2. uv
  () => nodeSpec,                                                     // 3. node
  () => (brewAvailable ? brewSpec : undefined),                       // 4. brew（仅当可用）
  () => goSpec,                                                       // 5. go
  () => downloadSpec,                                                 // 6. download
  () => brewSpec,                                                     // 7. brew（即便不可用，用于暴露错误）
  () => indexed[0],                                                   // 8. 兜底：第一个
];
```

**工程权衡**：

- 只在 brew **实际可用时**偏好 brew，避免在 Linux/Docker 上必然失败（`skills-status.ts:93`）。
- download 优先级低于 brew，是为了在 brew 不可用时仍优先 download（`skills-status.ts:96`）。
- 若全是 download，则**全部暴露**（`skills-status.ts:165-168`）。

### 6.5 download 子流程（沙箱化）

`src/agents/skills-install-download.ts`。

**per-skill tools 沙箱根**（`tools-dir.ts:7-11`）：

```ts
export function resolveSkillToolsRootDir(entry) {
  const key = resolveSkillKey(entry.skill, entry);
  const safeKey = safePathSegmentHashed(key);          // 哈希避免非法路径片段
  return path.join(resolveConfigDir(), "tools", safeKey);   // ~/.openclaw/tools/<hash>
}
```

**targetDir 越界防护**（`skills-install-download.ts:31-50`）：

- 缺省 targetDir 为沙箱根；
- 相对路径视为相对于沙箱根；`~` / 绝对路径 / Windows 盘符走 `resolveUserPath`；
- `isWithinDir(safeRoot, resolved)` 强制拒绝任何逃逸。

**下载流程**：

- 写入 `<rootDir>/.openclaw-download-staging/<uuid>.tmp` 暂存；
- `fetchWithSsrFGuard`（带 SSRF 防护与超时）拉取，stream pipeline 落盘；
- `writeFileFromPathWithinRoot` 把暂存搬到最终路径，finally 块清理。

**归档类型推断**：`spec.archive` 显式 > 文件名后缀推断（`.tar.gz`/`.tgz`/`.tar.bz2`/`.tbz2`/`.zip`）。

### 6.6 解压子流程：zip-slip 与 TOCTOU 防御

`src/agents/skills-install-extract.ts`。

- `zip` / `tar.gz` → 走 `extractArchiveSafe`（内部已处理 traversal）；
- `tar.bz2` → **额外三层防御**（走系统 tar）：
  1. **preflight list**：`tar tf` 列条目 + `tar tvf` 取元数据；
  2. **逐条目 zip-slip traversal 校验**（`createTarEntryPreflightChecker`）；
  3. **SHA-256 hash 稳定性校验**（`verifyArchiveHashStable`）：preflight 前后 hash 必须一致，防 preflight 期间归档被篡改（TOCTOU）；
  4. **staging 解压 + 合并**：先解到 staging，再 `mergeExtractedTreeIntoDestination`，避免半解压状态。

### 6.7 状态机：即时计算，无持久化

> **重要**：openclaw **不维护** "installed / needs-update / error" 状态文件。skill 的"状态"是**每次调用时即时计算**的——基于当前进程环境下 requirement 是否满足。

核心函数 `buildSkillStatus`（`skills-status.ts:200-265`）。`SkillStatusEntry`（`skills-status.ts:32-55`）字段：

| 字段 | 来源 |
|---|---|
| `disabled` | `config.skills.entries[skillKey].enabled === false` |
| `blockedByAllowlist` | `!isBundledSkillAllowed(entry, allowBundled)` |
| `blockedByAgentFilter` | 当前 agent 的 `agentSkillFilter` 不含此 skill |
| `always` | `entry.metadata.always === true` |
| `requirements` / `missing` | `evaluateEntryRequirementsForCurrentPlatform(...)` 覆盖 `bins/anyBins/env/config/os` 五维 |
| `eligible` | `!disabled && !blockedByAllowlist && requirementsSatisfied` |
| `modelVisible` | `eligible && !blockedByAgentFilter && isSkillVisibleInAvailableSkillsPrompt(entry)` |

`buildWorkspaceSkillStatus`（`skills-status.ts:267-306`）每次调用都 `loadWorkspaceSkillEntries` + 重算，**无缓存**（符合 "no persistent metadata caches for discovery" 原则）。

### 6.8 onboard-skills 初始化

`src/commands/onboard-skills.ts:50-221`：

1. `buildWorkspaceSkillStatus` 得全量报告 → 分类：`eligible` / `missing`(os 兼容) / `unsupportedOs` / `blocked`。
2. `Configure skills now?` confirm。
3. `installable` 筛选 = `missing` ∩ (`install.length > 0`) ∩ (`missing.bins.length > 0`)。
4. multiselect 让用户勾选要安装的 skill（含 `__skip__`）。
5. **brew 预警**：若选中 skill 含 brew kind 且 brew 不在 PATH，提示官方安装命令。
6. **nodeManager 选择**：若选中 skill 含 node kind，弹 select（npm/pnpm/bun），写入 `config.skills.install.nodeManager`。
7. 逐个安装（`prompter.progress` spinner + `installSkill`），失败打印 stderr/stdout + `openclaw doctor` 提示。
8. **API key 配置**：对 `missing.env` 非空且 skill 有 `primaryEnv` 的，逐个 confirm → `sensitive` 输入 → `upsertSkillEntry(cfg, skillKey, {apiKey})`。

### 6.9 doctor-skills 诊断

`src/commands/doctor-skills.ts`。

**诊断对象**（`collectUnavailableAgentSkills`, `:9-17`）：当前 agent 允许但运行时不可用的 skill，即

```
!eligible && !disabled && !blockedByAllowlist && !blockedByAgentFilter
```

（"配置上开了，但 bins/env/config/os 不满足"）

**报告格式**（`formatUnavailableSkillDoctorLines`, `:46-59`）：

- 每个 skill 一行 `- <name>: <missing summary>`（汇总 bins/anyBins/env/config/os 缺失项）。
- 末尾给修复建议：`openclaw doctor --fix` / `openclaw skills check --agent <id>`。

**自动修复**（`disableUnavailableSkillsInConfig`, `:61-82`）：把不可用 skill 在 `config.skills.entries[skillKey].enabled` 置为 `false`。

> **注意**：doctor 的"修复"语义是 **disable，不是 reinstall**。真正的"修复"是让用户回到 onboard 或手动 `openclaw skills install`。

### 6.10 失败回退策略

openclaw **没有独立的 fallback 模块**（`skills-install-fallback.test.ts` 是覆盖 `skills-install.ts` 内部回退逻辑的测试）。回退行为散落在 `skills-install.ts`：

| 场景 | 回退行为 |
|---|---|
| brew 缺失 | Linux：提示 brew.sh 或 apt/dnf/pacman；其他平台：仅提示 brew.sh |
| uv 缺失 | brew 可用 → `brew install uv`；否则报错指向官方文档（**永不**自动 curl 安装——fallback 测试钉死） |
| go 缺失 | brew → apt-get（root 直接 / 非 root + sudo 免密）→ 报错指向 go.dev（sudo 失败不触发 apt） |
| GOBIN 解析 | `brew --prefix` → 候选常量 `/opt/homebrew/bin`、`/usr/local/bin` → **不信任 `HOMEBREW_PREFIX` env**（防注入） |
| uv/python env | uv 安装命令**不附加 env**（让 uv 继承系统 `UV_PYTHON`/`PIP_INDEX_URL` 等） |

> **核心安全设计**：uv/go 缺失时**永不**通过 curl 远程脚本自动安装，仅给官方文档链接。这是显式设计并由 fallback 测试钉死的行为。

### 6.11 安装输出格式

`summarizeInstallOutput`（`skills-install-output.ts:7-31`）摘取失败关键行：

- 优先级：首个 `/^error\b/i` → 首个 `/\b(err!|error:|failed)\b/i` → 最后一行；
- 折叠多空白为单空格；
- 截断到 200 字符（超出加 `…`）。

`formatInstallFailureMessage`（`skills-install-output.ts:33-40`）：

```ts
const code = typeof result.code === "number" ? `exit ${result.code}` : "unknown exit";
const summary = summarizeInstallOutput(result.stderr) ?? summarizeInstallOutput(result.stdout);
return summary ? `Install failed (${code}): ${summary}` : `Install failed (${code})`;
```

---

## 七、ClawHub 远程市场

### 7.1 ⚠️ 名字陷阱澄清（重要）

研究 ClawHub 时最容易踩的坑是三个名字相似的文件，它们服务**完全不同**的场景：

| 文件 | 实际职责 |
|---|---|
| `src/infra/clawhub.ts` (1076 行) | ⭐ **ClawHub API 客户端**（远程市场）—— 真正的 ClawHub 集成 |
| `src/infra/skills-remote.ts` (440 行) | **远程配对节点 bin 探测**（本地集群内的远程 Mac 节点），与 ClawHub **无关** |
| `src/plugins/clawhub.ts` (1273 行) | ClawHub **plugin/bundle** 安装器（远程市场的另一制品族），不是 skill |

`skills-remote.ts` 与 ClawHub 唯一的间接联系：它产出的 `getRemoteSkillEligibility()` 会传给 `buildWorkspaceSkillSnapshot`，与 ClawHub 装下来的 skill 一起决定最终 eligibility——但它本身**从不调用 ClawHub API**。

### 7.2 ClawHub 是什么：市场分层

ClawHub 是 openclaw 的**远程包市场**，托管三类制品（family，`src/infra/clawhub.ts:17-18`）：

```ts
export type ClawHubPackageFamily = "skill" | "code-plugin" | "bundle-plugin";
export type ClawHubPackageChannel = "official" | "community" | "private";
```

**同一个市场服务三种制品族**，内部按 family 分流到两条独立管线：

- **`family = "skill"`** → 走 `src/agents/skills-clawhub.ts`（轻量 zip，装到 `<workspace>/skills/<slug>`）
- **`family = "code-plugin" / "bundle-plugin"`** → 走 `src/plugins/clawhub.ts`（ClawPack / npm-pack，完整性校验更严格）

两条管线**互斥**：plugin 安装器显式拒绝 skill 家族的包（`src/plugins/clawhub.ts:967-972`），提示用户改用 skill 命令。

**基础 URL**：`https://clawhub.ai`（`clawhub.ts:14`）。

### 7.3 API 端点结构（skill 线）

所有端点都在 `src/infra/clawhub.ts`，前缀 `/api/v1/`：

| 操作 | 方法 | 路径 | 行 |
|---|---|---|---|
| 搜索 skill | `searchClawHubSkills` | `/api/v1/search?q=...&limit=...` | `clawhub.ts:822-842` |
| skill 详情 | `fetchClawHubSkillDetail` | `/api/v1/skills/{slug}` | `clawhub.ts:844-858` |
| skill 列表 | `listClawHubSkills` | `/api/v1/skills?limit=...` | `clawhub.ts:860-877` |
| skill 下载 | `downloadClawHubSkillArchive` | `/api/v1/download?slug=...&version=...&tag=...` | `clawhub.ts:997-1036` |

### 7.4 API 客户端设计

请求核心 `clawhubRequest`（`clawhub.ts:563-587`）：

```ts
async function clawhubRequest(params) {
  const url = buildUrl(params);
  const token = normalizeOptionalString(params.token) || (await resolveClawHubAuthToken());
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(
    new Error(`ClawHub request timed out after ${params.timeoutMs ?? DEFAULT_FETCH_TIMEOUT_MS}ms`)
  ), params.timeoutMs ?? DEFAULT_FETCH_TIMEOUT_MS);   // 默认 30s
  try {
    const response = await (params.fetchImpl ?? fetch)(url, {
      headers: token ? { Authorization: `Bearer ${token}` } : undefined,
      signal: controller.signal,
    });
    return { response, url, hasToken: Boolean(token) };
  } finally { clearTimeout(timeout); }
}
```

设计要点：

- **默认超时** 30 秒，可按调用覆盖。
- **认证**：Bearer token via `Authorization` header。**无 token 时请求也会发出**（匿名访问，受限于更严的 rate limit）。
- **可注入 fetch**：`fetchImpl?: FetchLike`，全部公开 API 都接受，便于测试。
- **重试**：**客户端层不实现自动重试**。仅 HTTP 429 时把 `RateLimit-Reset`/`Retry-After` header 拼进错误消息（`clawhub.ts:604-629`）。
- **错误**：任何 `!response.ok` 抛 `ClawHubRequestError`（`clawhub.ts:360-372`）。

**Token 解析的层级查找**（`resolveClawHubAuthToken`, `clawhub.ts:424-445`）：

1. 环境变量 `OPENCLAW_CLAWHUB_TOKEN` > `CLAWHUB_TOKEN` > `CLAWHUB_AUTH_TOKEN`
2. 配置文件（`OPENCLAW_CLAWHUB_CONFIG_PATH` 显式 > macOS `~/Library/Application Support/clawhub/config.json` > XDG `$XDG_CONFIG_HOME/clawhub/config.json`）
3. 配置 JSON 内递归查找：`accessToken`/`authToken`/`apiToken`/`token`/嵌套 `auth`/`session`/`credentials`/`user`

> **dotenv 安全阻断**：这些 `OPENCLAW_CLAWHUB_*` 变量在 `src/infra/dotenv.ts` 被列入 `BLOCKED_WORKSPACE_DOTENV_KEYS`——**工作区级 .env 不能覆盖**，只有用户级/系统级可设，防止恶意项目劫持 ClawHub registry 指向钓鱼服务器。

**下载缓存**：**没有跨调用的磁盘缓存**。每次下载生成新的 `openclaw-clawhub-skill-<slug>.zip` 临时文件，`cleanup` 回调由调用方在解压完成后释放（`skills-clawhub.ts:331-333` 的 finally）。

**完整性校验**：

- skill 下载：计算 sha256 但**不与服务器声明值对比**（只返回 `sha256Hex`/`integrity`）
- ClawPack（plugin）下载：**强制**对比 `X-ClawHub-Artifact-Sha256` header 与实际 sha256，不匹配抛错

这反映了 skill 与 plugin 两条线**安全模型不同**：plugin 执行任意代码需严格校验；skill 是 Markdown 文档，威胁面较小。

### 7.5 编排层：`src/agents/skills-clawhub.ts`

这一层包装 `infra/clawhub.ts` 的原始 API，加上 **workspace 锁文件管理**和 **slug 校验**。

#### 锁文件与 origin 元数据

每个 workspace 维护两份状态：

1. **workspace 级锁文件** `<workspaceDir>/.clawhub/lock.json`（`skills-clawhub.ts:29-38, 143-173`）：

   ```json
   { "version": 1, "skills": { "<slug>": { "version": "...", "installedAt": 1234567890 } } }
   ```

   旧路径 `.clawdhub/lock.json` 仍被读取以保持向后兼容。

2. **每个 skill 的 origin 文件** `<workspaceDir>/skills/<slug>/.clawhub/origin.json`（`skills-clawhub.ts:19-27, 175-206`）：

   ```json
   { "version": 1, "registry": "https://clawhub.ai", "slug": "...",
     "installedVersion": "...", "installedAt": 1234567890 }
   ```

> **关键设计**：`registry` 字段记录该 skill 当时从哪个 ClawHub 实例安装，因此自建/私有 registry 的 skill 在 update 时会回到原 registry，而非全局默认。

#### 安装流程（`installSkillFromClawHub` → `performClawHubSkillInstall`, `skills-clawhub.ts:267-340`）

1. 解析版本（`fetchClawHubSkillDetail`，未指定则用 `latestVersion`）
2. 检查目标目录：若 `<workspace>/skills/<slug>` 已存在且未传 `force`，拒绝
3. 下载归档 `downloadClawHubSkillArchive`
4. `withExtractedArchiveRoot` 以 `SKILL.md` 为 root marker 解压，`installPackageDir` 复制到目标
5. 校验 SKILL.md 存在（接受 `SKILL.md`/`skill.md`/`skills.md`/`SKILL.MD` 四种大小写）
6. 写 origin 文件 + 更新锁文件
7. finally 清理临时归档

#### 更新流程（`updateSkillsFromClawHub`, `skills-clawhub.ts:408-460`）

- 读取锁文件；指定 `slug` 时先尝试作为已跟踪 slug 解析；
- 未指定 `slug` 时更新锁文件中**所有** slug；
- 每个 slug 调 `resolveTrackedUpdateTarget`：优先从 `origin.json` 读 `registry` 决定 baseUrl（自建 registry 不会丢失）；
- `installTrackedSkillFromClawHub(force: true)` 覆盖安装。

#### Slug 校验

- `normalizeTrackedSlug`（`skills-clawhub.ts:69-75`）：拒绝含 `/`、`\`、`..` 的 slug（防路径穿越）。
- `validateRequestedSlug`（`skills-clawhub.ts:77-83`）：要求匹配 `^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$` 且纯 ASCII。

### 7.6 ClawHub skill 如何进入本地合并

**核心结论**：ClawHub skill 不走特殊合并路径，而是作为**标准 skill 目录**参与合并。

安装时直接落到 `<workspace>/skills/<slug>/`（`workspace` 优先级最高），就像用户手写的一样，然后通过通用加载器 `loadSkillEntries`（§3.1）被纳入快照。同名时按 `skill.name`（来自 SKILL.md frontmatter）去重，**而非 slug 或目录名**——所以 ClawHub skill 会覆盖同名 bundled/managed skill。

### 7.7 Gateway RPC 方法

`skillsHandlers`（`src/gateway/server-methods/skills.ts:69-359`）暴露 **6 个 RPC**：

| 方法 | 参数 | 用途 |
|---|---|---|
| `skills.status` | `{ agentId? }` | 返回 workspace 所有 skill 状态 |
| `skills.bins` | `{}` | 聚合所有 skill 所需 binaries（远程节点 bin 探测用） |
| `skills.search` | `{ query?, limit? }` | 代理调 `searchSkillsFromClawHub` |
| `skills.detail` | `{ slug }` | 代理调 `fetchClawHubSkillDetail` |
| `skills.install` | **联合**：`{name, installId, ...}` 或 `{source:"clawhub", slug, version?, force?}` | 双用途：本地 installer 或 ClawHub skill |
| `skills.update` | **联合**：`{skillKey, enabled?, apiKey?, env?}` 或 `{source:"clawhub", slug?, all?}` | 双用途：改本地配置或更新 ClawHub skill |

参数 schema 在 `src/gateway/protocol/schema/agents-models-skills.ts:228-302` 用 TypeBox 定义，全部 `additionalProperties: false` 严格契约。

### 7.8 远程节点 bin 探测（`src/infra/skills-remote.ts`）

**场景**：用户在 Linux 服务器跑 openclaw gateway，配对了家里的 Mac 笔记本作为远程 node。某 skill 要求 `brew`，Linux 上没有但 Mac 上有——通过这套机制，gateway 知道"远程 Mac 可用且有 brew"，该 skill 在 gateway 上也算 eligible，可通过 `exec host=node` 在远程 Mac 执行。

**核心数据**（`skills-remote.ts:14-23`）：

```ts
type RemoteNodeRecord = {
  nodeId: string;
  displayName?, platform?, deviceFamily?, commands?: string[];
  bins: Set<string>;          // 该节点被探测出存在的 binaries
  connected: boolean;
};
const remoteNodes = new Map<string, RemoteNodeRecord>();
```

**Bin 探测流程** `refreshRemoteNodeBins`（`skills-remote.ts:290-391`）：

1. 去重并发（同节点同时到达的探测合并）；
2. **只探测 Mac 节点**（`isMacPlatform`）；
3. 节点必须支持 `system.which` 或 `system.run` 命令；
4. 扫描所有 workspace 的 skill entries，收集 OS=darwin 的 skill 需要的 bins；
5. 远程调用：优先 `system.which`（返回 `{bins: {bin: path}}`），退化 `system.run`（执行 `for b in 'b1' 'b2'; do command -v "$b" ...; done` 解析 stdout）；
6. bin 集变化才 bump 快照版本（避免无意义刷新）。

**Eligibility 输出** `getRemoteSkillEligibility`（`skills-remote.ts:393-424`）：

```ts
return {
  platforms: ["darwin"],
  hasBin: (bin) => bins.has(bin),
  hasAnyBin: (required) => required.some((bin) => bins.has(bin)),
  ...(note ? { note } : {}),   // "Remote macOS node available ... Run macOS-only skills via exec host=node"
};
```

返回的 `SkillEligibilityContext["remote"]` 被 `evaluateEntryRequirementsForCurrentPlatform` 使用，让 `requires.bins`/`requires.anyBins` 检查能落到远程节点上。

### 7.9 隔离 agent（cron）如何使用 skill 快照

入口 `resolveCronSkillsSnapshot`（`src/cron/isolated-agent/skills-snapshot.ts:14-52`）：

```ts
export async function resolveCronSkillsSnapshot(params) {
  if (params.isFastTestEnv) return params.existingSnapshot ?? { prompt: "", skills: [] };
  const runtime = await loadSkillsSnapshotRuntime();   // 懒加载
  const snapshotVersion = runtime.getSkillsSnapshotVersion(params.workspaceDir);
  const skillFilter = runtime.resolveAgentSkillsFilter(params.config, params.agentId);
  const shouldRefresh =
    !existingSnapshot ||
    existingSnapshot.version !== snapshotVersion ||
    !matchesSkillFilter(existingSnapshot.skillFilter, skillFilter);
  if (!shouldRefresh) return existingSnapshot;        // 版本未变 + filter 未变 → 复用
  return runtime.buildWorkspaceSkillSnapshot(...);
}
```

设计要点：

- **懒加载**：runtime 通过 `createLazyImportLoader(() => import("./skills-snapshot.runtime.js"))` 加载，避免测试 fast path 加载完整 skills 模块。
- **三步失效检查**：existing 不存在 / 版本不匹配 / skillFilter 不匹配，任一为真才重建。
- **远程 eligibility 透传**：和 gateway `skills.status` 一样传 `eligibility.remote`，cron agent 也能感知远程 Mac 节点。
- 快照被**持久化到 cron session entry**（`cronSession.sessionEntry.skillsSnapshot`），跨运行复用。

---

## 八、安全审计

### 8.1 双轨扫描架构

OpenClaw 的 skill 安全审计实际由**两套独立的扫描器**组成，加上 skill-workshop 扩展的第三套：

| 扫描器 | 位置 | 扫描对象 | 触发时机 |
|---|---|---|---|
| **代码扫描器**（核心） | `src/security/skill-scanner.ts` | skill/plugin 内的 JS/TS 源码 | **安装时强制** + `audit --deep` |
| **workspace 符号链接逃逸扫描器** | `src/security/audit-workspace-skills.ts` | workspace `skills/**/SKILL.md` 的 realpath | `audit`（非 deep 也跑） |
| **Markdown 内容扫描器** | `extensions/skill-workshop/src/scanner.ts` | skill 提案的 markdown 文本 | skill-workshop 提案应用时 |

### 8.2 代码扫描器检测的威胁类别

#### LINE_RULES（按行匹配，`skill-scanner.ts:152-178`）

| 规则 ID | 严重度 | 检测内容 |
|---|---|---|
| `dangerous-exec` | **critical** | Shell 执行（要求上下文出现 `child_process`，避免误报 RegExp.exec） |
| `dynamic-code-execution` | **critical** | `eval(` 或 `new Function(` |
| `crypto-mining` | **critical** | `stratum+tcp`、`coinhive`、`cryptonight`、`xmrig` |
| `suspicious-network` | warn | 非标准端口 WebSocket（白名单 `80,443,8080,8443,3000`） |

#### SOURCE_RULES（全文匹配 + 上下文窗口，`skill-scanner.ts:183-212`）

| 规则 ID | 严重度 | 检测内容 |
|---|---|---|
| `potential-exfiltration` | warn | `readFile` AND 网络发送上下文（`fetch`/`post`/`http.request`） |
| `obfuscated-code` (hex) | warn | `/(\\x[0-9a-fA-F]{2}){6,}/` |
| `obfuscated-code` (base64) | warn | `atob`/`Buffer.from` 后跟 ≥200 字符 base64 |
| `env-harvesting` | **critical** | `process.env` AND 网络发送，**且窗口为 8 行**（`requiresContextWindowLines: 8`） |

#### 反误报工程

- **注释剥离**（`stripCommentsForHeuristics`, `skill-scanner.ts:239-299`）：源规则在剥离注释后的文本上运行，避免把 `// fetch(...)` 注释当网络发送上下文。
- **良性 exec 识别**（`isBenignMemberExecMatch`）：识别 `cp.exec`/`childProcess.exec` 合法用法。
- **打包文件远距离组合不误报**：8 行窗口限制使 bundle 中距离远的 env+send 组合不触发。
- **去重**：每条 line 规则每文件只产 1 个 finding；source 规则按 `ruleId::message` 去重。

### 8.3 扫描器输入输出

**可扫描扩展名**（`skill-scanner.ts:40-49`）：`.js .ts .mjs .cjs .mts .cts .jsx .tsx` —— 仅 JS/TS 系列，**不扫 `.md`、`.sh`、`.py`、`.json`**。

**扫描选项** `SkillScanOptions`：

- `excludeTestFiles`：跳过 `__tests__/__fixtures__/__mocks__/test/tests/` 和 `.spec/.test/.mock.xxx`（安装时强制开启）。
- `includeFiles`：强制扫描的文件（仍受根目录 + 扩展名约束）。
- `maxFiles`：默认 500；workspace skill 上限放宽到 2000。
- `maxFileBytes`：默认 1 MB。

**遍历规则**（`walkDirWithLimit`）：**跳过 hidden 目录（`.` 开头）和 `node_modules`**。

**输出** `SkillScanFinding`（`skill-scanner.ts:12-19`）：

```ts
{ ruleId, severity: "info"|"warn"|"critical", file, line, message, evidence }
```

`evidence` 截断到 120 字符。`SkillScanSummary` 含 `scannedFiles/critical/warn/info/findings[]`。

**性能优化**：LRU 文件扫描缓存（5000 条，按 size+mtime 失效）+ 目录条目缓存（5000 条，按 mtimeMs 失效）。

### 8.4 workspace symlink 逃逸扫描

`collectWorkspaceSkillSymlinkEscapeFindings`（`audit-workspace-skills.ts:111-205`）：

- 在 `src/security/audit.ts:1014` 被**非 deep 审计**调用，即 `openclaw security audit`（不带 --deep）即运行。
- **仅检测 workspace skills 的符号链接逃逸**，不做代码内容扫描。
- 流程：枚举 workspace → BFS 遍历 `skills/` 找 SKILL.md（**跟踪 symlink**）→ 每个 SKILL.md 做 `realpathWithTimeout`（2 秒超时）比对 → `isPathInside(workspaceRealPath, skillRealPath)` 检查是否仍在 workspace 内。
- **realpath 超时/失败 → 视为潜在逃逸**（`realpath timed out - symlink target unverifiable`）。

两类 finding：

- `skills.workspace.symlink_escape`（warn）：标题 "Workspace skill files resolve outside the workspace root"，detail 列最多 12 个逃逸条目。
- `skills.workspace.scan_truncated`（warn）：BFS 访问上限触发（`maxDirVisits` 默认 `maxFiles * 20` = 40000）。

### 8.5 ⚠️ 扫描结果如何影响加载（含盲区）

#### 安装时（强制阻断）

`scanSkillInstallSourceRuntime`（`src/plugins/install-security-scan.runtime.ts:943-991`）决策：

| 内置扫描结果 | 行为 |
|---|---|
| `status === "error"` | **阻断**（code: `security_scan_failed`） |
| `critical > 0` 且无 override | **阻断**（code: `security_scan_blocked`），reason 列所有 critical 详情 |
| `critical > 0` 且 `dangerouslyForceUnsafeInstall: true` | **放行** + warn |
| `critical > 0` 且 `trustedSourceLinkedOfficialInstall: true` | 放行（**但 skill 路径硬编码 false**，line 963——**skill 永远不能享受 official trust 豁免**） |
| 仅 `warn > 0` | **放行** + warn |
| 全清 | 放行 |

之后还调 `runBeforeInstallHook` 把 `builtinScan` 喂给用户配置的 `before_install` 钩子，钩子可独立 block。`install.ts:491-492` 拿到 `scanResult.blocked` 直接 `buildBlockedInstallResult`，**不执行后续安装**。

**严重度等级后果**：critical = 阻断（除非显式 `--dangerously-force-unsafe-install`）；warn = 仅警告不阻断；info = 静默。

#### 审计时（仅报告）

`collectInstalledSkillsCodeSafetyFindings`（`audit-extra.async.ts:827-905`）：

- 遍历所有 workspace 的 skill entries；
- **跳过 `openclaw-bundled` 源**（内置 skill 免审）；
- **跳过 `extensions/` 下的 skill**（已被 plugin 审计覆盖）；
- critical/warn → `checkId: "skills.code_safety"`；
- **仅产出报告，不阻断任何运行时行为**。

#### skill-workshop 提案时（运行时阻断写入）

`applyProposalToWorkspace` 调 `assertSkillContentSafe`，**遇到任何 critical finding 直接 throw**，阻止 skill 文件写入磁盘。被阻断的提案标记为 `status: "quarantined"` 进入隔离队列。

#### ⚠️ 关键盲区：运行时无防御层

> **grep 确认**：`src/agents/skills/workspace.ts`（运行时 skill 加载器）的 `loadWorkspaceSkillEntries`/`loadSkillsFromDirSafe` **完全不调用** `skill-scanner` 或 `assertSkillContentSafe`。**skill 一旦落盘，运行时加载不再重新扫描**，安全防护完全依赖安装时阻断 + 主动审计。

这意味着：

1. 恶意 skill 若通过任意非安装路径进入 workspace（手工 `cp`、git clone、agent 自写 support JS），代码扫描器永远不会跑。
2. **`.sh`/`.py`/`.md`/`.json` 不在扫描范围**——skill 可携带任意 `.sh` 脚本而代码扫描器视而不见。仅 skill-workshop 的 markdown 正则会扫到文本中的 `curl|sh` 模式。
3. **8 行窗口可被绕过**：攻击者只需在 env 访问和网络发送之间插入 9 行无关代码即可绕过 `env-harvesting`。
4. **`dangerouslyForceUnsafeInstall` 是全局逃生口**：UI、clawhub.ts、marketplace.ts、git-install.ts 都透传此 flag，用户可一键跳过所有 critical 阻断。

### 8.6 测试用例反映的威胁模型

从 `skill-scanner.test.ts` 测试名推断设计者建模的攻击面：

**应阻断**：命令注入（动态拼接 shell）、shell 执行、`eval`/`new Function`、数据外泄（读敏感文件 + POST）、挖矿、凭证/TOKEN 窃取、代码混淆（hex/base64）、隐蔽 C2 通道（非标准端口）。

**不应误报（合法基线）**：仅 import 不调用、RegExp.exec 方法、注释里的 fetch/env、函数名含 fetch（`closeFetchHandles`）、bundle 远距离组合、单纯 GET、本地 `process.env` 真实同窗口发送（仍要抓）。

**路径逃逸攻击面**：symlink SKILL.md 指向 workspace 外、NFS/SMB 卡死或 symlink 循环导致 realpath 不可解析、目录炸弹（深嵌套拖垮扫描器）。

**skill-workshop markdown 扫描攻击面**：prompt-injection（ignore-instructions / system / tool）、`curl ... | sh`、secret-exfiltration、destructive-delete（`rm -rf /`）、unsafe-permissions（`chmod 777`）。

### 8.7 env-overrides 的安全考量

`src/agents/skills/env-overrides.ts` 是 skill 向子进程注入环境变量的核心，含多层防护：

**(a) Always-blocked 模式**（`env-overrides.ts:84-96`）：

```ts
const SKILL_ALWAYS_BLOCKED_ENV_PATTERNS = [/^OPENSSL_CONF$/i];
// isAlwaysBlockedSkillEnvKey 还检查 isDangerousHostEnvVarName / isDangerousHostEnvOverrideVarName
```

即使用户显式声明，`OPENSSL_CONF`（可改变运行时加载行为）等永远被屏蔽。

**(b) Sensitive 键白名单**：被通用 `sanitizeEnvVars` 标记 blocked 的键，必须同时满足：

- `isAlwaysBlockedSkillEnvKey(key)` 不为 true；
- 在 `allowedSensitiveKeys`（即 `primaryEnv` + `requiredEnv`）集合里；
- `validateEnvVarValue` 通过（含 null bytes 直接 blocked）。

**(c) ⭐ 子进程隔离**（关键设计，注释 `env-overrides.ts:24-29`）：

> Tracks env var keys that are currently injected by skill overrides. Used by ACP harness spawn to strip skill-injected keys so they don't leak to child processes (e.g., OPENAI_API_KEY leaking to Codex CLI). @see https://github.com/openclaw/openclaw/issues/36280

`getActiveSkillEnvKeys()` 暴露当前注入键集合，ACP harness 在 spawn 子进程时用这个集合剥除 skill 注入的键——防止 OpenClaw 自己注入的 `OPENAI_API_KEY` 泄漏给它 spawn 的 Codex CLI 等其他模型 CLI（真实修复过的 bug #36280）。

**(d) 引用计数**（`env-overrides.ts:37-75`）：`acquireActiveSkillEnvKey`/`releaseActiveSkillEnvKey` 用 `count` 跟踪，多个 skill 注入同一键时正确还原 baseline，避免并发/嵌套场景过早删除。

**(e) 不覆盖外部托管键**（`env-overrides.ts:166-167`）：`process.env[envKey] !== undefined && !activeSkillEnvEntries.has(envKey)` → 不覆盖用户已设的值。

---

## 九、配置与热重载

### 9.1 完整配置 schema（用户视角）

`src/config/types.skills.ts`：

```ts
type SkillConfig = {           // per-skill
  enabled?: boolean;
  apiKey?: SecretInput;        // 支持 secret reference（运行时解析）
  env?: Record<string, string>;
  config?: Record<string, unknown>;
};

type SkillsConfig = {
  allowBundled?: string[];                         // bundled skill 白名单
  load?: {
    extraDirs?: string[];                          // 额外 skill 目录（最低优先级）
    watch?: boolean;                               // 是否启用文件 watcher（默认 true）
    watchDebounceMs?: number;                      // 默认 250ms
  };
  install?: { preferBrew?: boolean; nodeManager?: "npm"|"pnpm"|"yarn"|"bun" };
  limits?: {
    maxCandidatesPerRoot?; maxSkillsLoadedPerSource?;
    maxSkillsInPrompt?; maxSkillsPromptChars?; maxSkillFileBytes?;
  };
  entries?: Record<string, SkillConfig>;           // per-skill 配置
};
```

per-agent 还可独立配置（`agent-filter.ts:24-51`）：

- `agents.entries.<id>.skills: string[]` —— 该 agent 的 skill 白名单（覆盖 `agents.defaults.skills`）。
- `agents.entries.<id>.skillsLimits.maxSkillsPromptChars` —— 该 agent 的字符预算覆盖。

### 9.2 runtime-config：磁盘 config vs runtime snapshot

`resolveSkillRuntimeConfig`（`runtime-config.ts:21-35`）在「磁盘 config」与「运行时 snapshot config」之间挑选用于 skill 解析的那一份：

```ts
export function resolveSkillRuntimeConfig(config?) {
  const runtimeConfig = getRuntimeConfigSnapshot();
  if (!runtimeConfig) return config;
  if (!config) return runtimeConfig;
  const runtimeHasRawSkillSecretRefs = hasConfiguredSkillApiKeyRef(runtimeConfig);
  const configHasRawSkillSecretRefs = hasConfiguredSkillApiKeyRef(config);
  if (runtimeHasRawSkillSecretRefs && !configHasRawSkillSecretRefs) return config;
  return runtimeConfig;
}
```

意义：

- 支持 **secret hot-reload**：用户通过 UI / `config set skills.entries.<key>.apiKey` 改 key 后无需重启即可生效。
- 避免双源竞争：两处都配 secret ref 时 runtime snapshot 优先（最新的赢）。
- 被 `env-overrides.ts` 和 `skills-runtime.ts` 复用，统一配置源。

### 9.3 文件 watcher（refresh.ts）

`ensureSkillsWatcher({ workspaceDir, config })`（`refresh.ts:105-179`）：

- 用 `chokidar` 监听（`refresh.ts:142-149`）：`ignoreInitial: true`，`awaitWriteFinish` 用 debounce（默认 250ms）。
- **监听路径**（`resolveWatchPaths`, `refresh.ts:56-73`）：
  - `<workspace>/skills`、`<workspace>/.agents/skills`
  - `~/.openclaw/skills`
  - `~/.agents/skills`
  - `config.skills.load.extraDirs`
  - plugin skill 目录（`resolvePluginSkillDirs`）
- **忽略**（`shouldIgnoreSkillsWatchPath`, `refresh.ts:88-103`）：`.git / node_modules / dist / venv / __pycache__ / build / .cache`，且只关注 `SKILL.md` 文件名变化。
- 事件 `add / change / unlink / unlinkDir` 都触发 debounce 后的 `bumpSkillsSnapshotVersion({ reason: "watch", changedPath })`。

### 9.4 refresh-state：版本号与订阅

进程内维护（`refresh-state.ts`）：

- `listeners: Set<(event) => void>` — 订阅者集合；
- `workspaceVersions: Map<workspaceDir, number>` — per-workspace 版本号；
- `globalVersion: number` — 全局版本号。

`bumpVersion(current)`（`refresh-state.ts:12-15`）用 `Date.now()`，若 `now <= current` 则 `current + 1`，**保证单调递增**。

`bumpSkillsSnapshotVersion({ workspaceDir?, reason, changedPath? })`（`refresh-state.ts:38-55`）：

- 给定 workspaceDir → bump per-workspace 版本并 emit 事件；
- 否则 bump global 版本并 emit。

`reason` 类型：`"watch" | "manual" | "remote-node" | "config-change"`。

`shouldRefreshSnapshotForVersion(cached, next)`（`refresh-state.ts:65-72`）：`next === 0 ? cached > 0 : cached < next`。

**触发时机汇总**：

1. **watch（自动）**：用户编辑 `skills/*/SKILL.md` → chokidar → debounce → bump。
2. **remote-node**（`skills-remote.ts:370, 383, 388`）：远端 Mac 节点探测到 bin 集变化或断开。
3. **manual**：手动调用。
4. **config-change**：config 重载时。

`registerSkillsChangeListener` 让上层（gateway、session manager）订阅事件，决定何时让缓存的 snapshot 失效并重建。

### 9.5 快照水合（snapshot-hydration）

`SkillSnapshot.resolvedSkills`（具体 `SKILL.md` 路径数组）是 **runtime-only**：session 持久化只保存轻量 catalog + prompt，consumers 需要具体路径时再"水合"：

```ts
export function hydrateResolvedSkills<T>(snapshot, rebuild): T {
  if (snapshot.resolvedSkills !== undefined) return snapshot;
  return { ...snapshot, resolvedSkills: rebuild().resolvedSkills };
}
```

`rebuild()` 通常调 `buildWorkspaceSkillSnapshot` 重新扫描。这与 `resolveEmbeddedRunSkillEntries` 的判断一致：当 snapshot 没有 `resolvedSkills` 时才补 load entries。

---

## 十、关键架构观察与设计权衡

### 10.1 设计意图总结

| 设计决策 | 动机 / 收益 | 代价 / 风险 |
|---|---|---|
| **最小侵入式扩展 CanonicalSkill**（只加 `source?:string`） | 上游升级无痛；prompt 渲染字节对齐有测试钉死 | 上游契约变更仍需跟 |
| **六级来源优先级 + localeCompare 排序** | prompt cache 友好（字节稳定）；同名 skill 覆盖语义清晰 | 优先级只在合并去重时起作用，最终输出不按 source 排序——调试时需注意 |
| **三级预算降级 full→compact→truncate** | 永不"静默丢 skill"；超限必有 ⚠️ 警告 | compact 模式丢了 description，模型只能按 name 判断 |
| **状态机即时计算，无持久化** | 无 stale state；符合 "no persistent metadata caches" 原则 | 每次调用重算（有 LRU 缓存弥补） |
| **安装时强制扫描，运行时不扫描** | 冷启动快；不阻塞 agent loop | **盲区**：非安装路径落盘的 skill 不被检查 |
| **5 种安装器统一 `--ignore-scripts`** | 防供应链 postinstall 攻击 | 部分 npm 包功能依赖 postinstall，可能装不全 |
| **uv/go 永不自动 curl 安装** | 防远程脚本执行 | 用户体验略差，需手动装前置 |
| **`OPENSSL_CONF` always-blocked + 子进程防泄漏** | 防 env 注入改变运行时行为 / 泄漏到其他 CLI（bug #36280） | 引用计数增加复杂度 |
| **GOBIN 不信任 `HOMEBREW_PREFIX` env** | 防攻击者注入恶意 prefix | brew 路径推断需 fallback 候选常量 |
| **tar.bz2 三重防御（preflight + TOCTOU hash + staging）** | 防 zip-slip 与解压时篡改 | 仅 bz2 走系统 tar，zip/tar.gz 由 `extractArchiveSafe` 内部处理 |
| **ClawHub skill 不强制 sha256 对比**（plugin 强制） | skill 是 Markdown，威胁面小 | 若 skill 携带 support JS 且被 install spec 触发，仍有风险 |
| **ClawHub registry 字段记录在 origin.json** | 自建/私有 registry 的 skill update 回到原 registry | 多 registry 共存时管理复杂 |

### 10.2 与 plugin 系统的对比

| 维度 | skill | plugin |
|---|---|---|
| 本质 | 文档型扩展（SKILL.md + 可选脚本） | 代码型扩展（参与工具注册、agent 循环） |
| 加载时机 | LLM 在对话中按需 read | 进程启动时注册 |
| 安全模型 | 安装时代码扫描 + 安装时 sha256（不强制）+ symlink 逃逸审计 | 安装时强制 sha256 + plugin API 兼容性检查 + manifest 验证 |
| 优先级 | 六级来源覆盖 | registry/manifest 驱动 |
| 完整性校验 | 计算 sha256 但不强制对比 | **强制**对比 server 声明值 |
| 失败影响 | 单个 skill 不影响其他 | manifest 错误可能影响整个 agent |
| CLI runner 集成 | 物化为 `--plugin-dir`（借用 Claude plugin 机制） | 直接走 plugin loader |

### 10.3 整体调用链（关键路径速查）

**模型自发触发 skill**（最常见路径）：

```
buildSystemPrompt (system-prompt.ts:806)
  └─ buildSkillsSection
       └─ skillsPrompt = resolveSkillsPromptForRun (workspace.ts:1025)
            └─ buildWorkspaceSkillsPrompt (workspace.ts:949)
                 └─ resolveWorkspaceSkillPromptState (workspace.ts:979)
                      ├─ loadSkillEntries (workspace.ts:734)   ← 6 级来源合并
                      ├─ filterSkillEntries (workspace.ts:101)  ← config + eligibility + agentFilter
                      ├─ isSkillVisibleInAvailableSkillsPrompt (workspace.ts:91)
                      ├─ compactSkillPaths + localeCompare 排序
                      └─ applySkillsPromptLimits (workspace.ts:878)  ← full→compact→truncate
                           └─ formatSkillsForPrompt / formatSkillsCompact
```

**用户 `/skill-name` 触发**：

```
auto-reply 收消息
  └─ resolveReplyDirectives (get-reply-directives.ts:143)
       └─ listSkillCommandsForWorkspace → reserve 命令名
  └─ resolveInlineActions (get-reply-inline-actions.ts)
       └─ resolveSlashCommandName → resolveSkillCommandInvocation (skill-commands-base.ts:59)
            └─ findSkillCommand（4 种匹配）
       └─ 权限门：isAuthorizedSender
       └─ 分流：
          ├─ dispatch.kind === "tool" → tool.execute（不进 LLM）
          └─ prompt rewrite → 改 BodyForAgent → 进 attempt.ts → 走上面的自发触发链
```

**`openclaw skills install <slug>`（ClawHub）**：

```
registerSkillsCli (skills-cli.ts:91)
  └─ skills install action (skills-cli.ts:133)
       └─ installSkillFromClawHub (skills-clawhub.ts:267)
            ├─ fetchClawHubSkillDetail (clawhub.ts:844)         ← 解析版本
            ├─ downloadClawHubSkillArchive (clawhub.ts:997)     ← 下载到临时文件
            ├─ withExtractedArchiveRoot → installPackageDir      ← 解压到 <workspace>/skills/<slug>
            ├─ ensureSkillRoot                                    ← 校验 SKILL.md 存在
            └─ 写 origin.json + 更新 lock.json
```

**ClawHub skill 立即可用**：装到 `<workspace>/skills/<slug>` 后，下次 `loadSkillEntries` 扫描时作为 workspace 级 skill 被发现（最高优先级），无需重启。

### 10.4 给阅读源码者的建议

1. **从 `src/agents/skills/workspace.ts` 开始读**——它是整个体系的"心脏"，1200 行覆盖发现/合并/过滤/prompt 构建/同步/预算控制。
2. **`skill-contract.ts` 是契约锚点**——理解 `Skill = CanonicalSkill & {source?}` 和 `formatSkillsForPrompt` 后，整个系统的类型骨架就清晰了。
3. **想理解安全边界，重点看 `skill-scanner.ts` 的测试**——测试名直接反映了威胁模型与反误报基线。
4. **调试 skill 不生效**：先 `openclaw skills check`（看 ready/visible/blocked 分类），再看 `formatSkillsCheck` 的 "Ready but hidden from model prompt" 是否命中 `disable-model-invocation`。
5. **理解远程市场时务必先区分三个名字相似的文件**（§7.1）——这是研究 ClawHub 时最容易踩的坑。
6. **想加新 skill 来源**：在 `loadSkillEntries`（`workspace.ts:734-809`）的优先级链里插入，并相应更新 `resolveWatchPaths`（`refresh.ts:56-73`）和注释里的优先级声明。

### 10.5 已知盲区与潜在改进点

（这些是研究过程中发现的设计权衡或可改进点，仅作记录，非结论）

1. **运行时无安全扫描**：依赖安装时阻断 + 主动审计。若通过 git clone / 手工 cp 进入 workspace 的 skill，永远不会被代码扫描。可考虑在 `loadSkillEntries` 加 opt-in 的轻量扫描，或至少在 audit 时强制覆盖。
2. **`.sh`/`.py` 不在扫描范围**：仅 JS/TS。skill 可携带 `.sh` 脚本而代码扫描器视而不见。skill-workshop 的 markdown 正则仅覆盖提案路径。
3. **8 行窗口可绕过**：`env-harvesting` 的窗口限制可被插入无关代码绕过。
4. **warn 级别不阻断**：`potential-exfiltration`、`obfuscated-code`、`suspicious-network` 仅日志告警。
5. **`dangerouslyForceUnsafeInstall` 全局逃生口**：多个安装路径都透传此 flag。
6. **文档/代码不一致**：`skills/clawhub/SKILL.md:75` 文档写默认 registry 是 `https://clawhub.com`，但代码权威值是 `https://clawhub.ai`（`clawhub.ts:14`）。

---

## 附录：核心文件速查表

| 关注点 | 文件 |
|---|---|
| 类型定义 | `src/agents/skills/types.ts` |
| 上游契约扩展 + prompt 渲染 | `src/agents/skills/skill-contract.ts` |
| ⭐ 核心：发现/合并/过滤/prompt 构建 | `src/agents/skills/workspace.ts` |
| SKILL.md 加载 | `src/agents/skills/local-loader.ts` |
| Frontmatter 解析 | `src/agents/skills/frontmatter.ts` + `src/markdown/frontmatter.ts` |
| bundled 目录解析 | `src/agents/skills/bundled-dir.ts` |
| Plugin skill（symlink） | `src/agents/skills/plugin-skills.ts` |
| SkillCommandSpec 派生 | `src/agents/skills/command-specs.ts` |
| 配置桥接 + eligibility | `src/agents/skills/config.ts` |
| 过滤 | `src/agents/skills/filter.ts` + `agent-filter.ts` |
| 文件 watcher | `src/agents/skills/refresh.ts` + `refresh-state.ts` |
| env/apiKey 注入 | `src/agents/skills/env-overrides.ts` |
| 安装主逻辑 | `src/agents/skills-install.ts` |
| download 子流程 | `src/agents/skills-install-download.ts` |
| 解压子流程 | `src/agents/skills-install-extract.ts` |
| 状态机 | `src/agents/skills-status.ts` |
| ⭐ ClawHub API 客户端 | `src/infra/clawhub.ts` |
| ClawHub skill 编排 | `src/agents/skills-clawhub.ts` |
| 远程节点 bin 探测 | `src/infra/skills-remote.ts` |
| ⭐ 代码扫描器 | `src/security/skill-scanner.ts` |
| workspace symlink 逃逸 | `src/security/audit-workspace-skills.ts` |
| 安装时扫描集成 | `src/plugins/install-security-scan.runtime.ts` |
| Gateway RPC | `src/gateway/server-methods/skills.ts` |
| Gateway wire schema | `src/gateway/protocol/schema/agents-models-skills.ts` |
| CLI 子命令 | `src/cli/skills-cli.ts` + `skills-cli.format.ts` |
| slash 命令解析 | `src/auto-reply/skill-commands-base.ts` + `skill-commands.ts` |
| 聊天触发 | `src/auto-reply/reply/get-reply-inline-actions.ts` |
| ⭐ system prompt 注入 | `src/agents/system-prompt.ts:806` |
| CLI runner 加载 | `src/agents/cli-runner/claude-skills-plugin.ts` |
| pi runner 加载 | `src/agents/pi-embedded-runner/skills-runtime.ts` |
| cron 快照 | `src/cron/isolated-agent/skills-snapshot.ts` |
| 配置层类型 | `src/config/types.skills.ts` |
| 54 个内置 skill | `skills/` |
| skill 提案扩展 | `extensions/skill-workshop/` |

---

*本文档基于 openclaw `2026.5.7` 版本源码写成。如需了解相邻系统，可参考 [memory-system.md](memory-system.md)（记忆系统）和 [openclaw-react-loop-and-flows.md](openclaw-react-loop-and-flows.md)（agent 循环与 flows）。*
