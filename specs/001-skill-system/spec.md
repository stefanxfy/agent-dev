# Feature Specification: Skill System (agent_core)

**Feature Branch**: `feature/skill-system`

**Created**: 2026-07-05

**Status**: Draft

**Input**: User description: "我想为当前项目agent_core，实现 skill功能，参考 docs/skill/openclaw-skill-system.md"

> 本 spec 定义 agent_core 的 Skill 系统（**WHAT / WHY**，不涉及 HOW）。
> 概念与术语对齐参考文档 `docs/skill/openclaw-skill-system.md`（OpenClaw 的 Skill 体系），
> 但实现范围以本项目的自研定位（Constitution Principle I）为准。
>
> **v1 范围已确认（2026-07-05）**：完整核心 + (模型自触发 + slash 命令) + (bundled + workspace 双层)。
> 不纳入：安装时安全扫描、env/apiKey 注入、5 种安装器、ClawHub 市场、远程节点。

## User Scenarios & Testing *(mandatory)*

### User Story 1 — 按需加载领域指令 (Priority: P1)

作为 agent 使用者，我希望 agent 在运行时把可用 skill 的"目录表"（name + description + 文件位置）注入 system prompt；当我的任务匹配某个 skill 时，agent 自己用 read 能力读取该 skill 的 `SKILL.md` 并遵循其中的专门指令——而那些指令不应该污染基础 prompt，影响不相关任务。

**Why this priority**: 这是 skill 系统的根本价值——基础 prompt 保持稳定（cache 友好），专门行为只在相关时按需加载。没有这一条就不存在"skill 系统"，是 MVP 的最小不可分切片。

**Independent Test**: 在 skills 目录放入单个有效 `SKILL.md`（例如 `code-review` skill）；发送一个匹配其 description 的任务，验证 agent 读取并应用了该 skill 的指令；发送一个不相关任务，验证 agent **没有**读取该 skill。

**Acceptance Scenarios**:

1. **Given** skills 目录有一个有效 `SKILL.md`，**When** agent 启动一次 run，**Then** system prompt 中包含一个 available-skills 段，列出该 skill 的 name、description、文件 location。
2. **Given** system prompt 含 available-skills 段，**When** 用户任务匹配某 skill 的 description，**Then** agent 读取该 `SKILL.md` 并把其指令体现在回复中。
3. **Given** system prompt 含 available-skills 段，**When** 用户任务不匹配任何 skill，**Then** agent 不读取任何 `SKILL.md`，按正常方式回复。
4. **Given** 两个 skill 的 description 都能匹配，**When** 任务到来，**Then** agent 选择更具体的那个，且**至多预读一个** skill。

---

### User Story 2 — Skill 创作与目录管理 (Priority: P2)

作为 skill 作者（开发者 / 高级用户），我希望只要往 skills 目录里放一个含 `SKILL.md`（YAML frontmatter + markdown body，可选附带 `scripts/`、`references/`）的子目录，agent 下一次 run 就能识别它——无需改 agent 代码、无需重启。

**Why this priority**: skill 系统的价值取决于"能不能不改代码就扩展"。这一条解锁整个项目的可扩展性。

**Independent Test**: 在 skills 目录新建一个含 frontmatter（name + description）和 body 指令的 skill；触发一次 run；验证新 skill 出现在目录中且其触发条件命中时被应用。

**Acceptance Scenarios**:

1. **Given** 一个含有效 frontmatter（`name` + `description`）的 `SKILL.md`，**When** agent 加载 skills，**Then** 该 skill 被发现并进入目录。
2. **Given** 一个 `SKILL.md` 缺 `description` 字段，**When** agent 加载，**Then** 该 skill 被拒绝（清晰报错/警告），其他 skill 照常加载。
3. **Given** 一个 `SKILL.md` 的 YAML frontmatter 损坏，**When** agent 加载，**Then** 该 skill 被优雅跳过，agent 不崩溃。
4. **Given** skill 目录含 `scripts/` 或 `references/` 子目录，**When** `SKILL.md` body 引用相对路径，**Then** agent 相对该 skill 自身目录解析路径。

---

### User Story 3 — Skill 触发控制与资格判定 (Priority: P3)

作为 skill 作者，我希望通过 frontmatter 元数据控制 skill 何时、是否暴露：对模型隐藏（不参与自动选择）、仅用户可调用、或声明运行环境/二进制/配置前置条件，未满足时自动排除。

**Why this priority**: 真实的 skill 库需要治理——不是所有 skill 都该自动触发，有些有前置依赖。这一条让目录保持相关性，避免坏 skill 污染 prompt。

**Independent Test**: 编写两个 skill——一个 `disable-model-invocation: true`，另一个 `requires` 不满足（如缺某 env 变量）；启动 run；验证前者不进入模型可见目录（但仍登记），后者作为 ineligible 被过滤掉。

**Acceptance Scenarios**:

1. **Given** 一个 `disable-model-invocation: true` 的 skill，**When** 构建 system prompt，**Then** 该 skill **不出现在**模型可见的 available-skills 段。
2. **Given** 一个声明了 `requires`（env / 二进制 / 配置 / OS）且当前环境不满足的 skill，**When** 加载 skills，**Then** 该 skill 被标记为 ineligible 且不出现在目录。
3. **Given** 一个 `user-invocable: false` 的 skill，**When** 构建可调用命令列表，**Then** 该 skill 不作为可显式调用的命令暴露。
4. **Given** 运行环境变化（如所需 env 变量被设置），**When** skills 重新求值，**Then** eligibility 反映当前状态，无需改代码。

---

### User Story 4 — 显式调用 (Priority: P4)

作为使用者，我希望能在 UI 里用 slash 命令（如 `/skill-name <args>`）显式触发某个 skill，无论模型是否会自动选中它。

**Why this priority**: 高级用户需要对"用哪个 skill"的确定性控制。比自动发现低优先级，但能让工作流可复现。**已确认纳入 v1**（模型自触发 + slash 命令双触发模型）。

**Independent Test**: 在聊天里输入 `/code-review <输入>`；验证对应 skill 被加载并应用到该输入，绕过模型自动选择。

**Acceptance Scenarios**:

1. **Given** 一个 `user-invocable` 的 skill 名为 `code-review`，**When** 用户输入 `/code-review <input>`，**Then** 系统对该输入应用 `code-review` skill 的指令。
2. **Given** 一个 skill 的 slash 命令被触发，**When** 处理该 turn，**Then** 模型被指示本轮使用该 skill（用户意图压过自动选择）。
3. **Given** 一个未知的 slash 命令，**When** 用户输入它，**Then** 系统报告无匹配 skill 且不崩溃。

---

### User Story 5 — 多来源优先级合并 (Priority: P5)

随着系统成长，skill 可以来自多个来源（内置 bundled、项目 workspace、用户管理目录），有明确优先级，使项目级同名 skill 能覆盖内置版本。

**Why this priority**: 基础系统跑通后，真实项目既需要内置 skill、也需要让用户/项目覆盖它们。优先级较低，因为核心闭环不依赖它。**v1 已确认落地 bundled + workspace 双层**（项目可覆盖内置同名 skill）。

**Independent Test**: 在 bundled 目录和 workspace 目录各放一个同名 `X` 但 body 不同；触发匹配 X 的 run；验证 workspace（更高优先级）版本是被应用的那个。

**Acceptance Scenarios**:

1. **Given** bundled 与 workspace 各有同名 skill，**When** 构建目录，**Then** 仅高优先级（workspace）版本出现。
2. **Given** 多来源合并后的 skill 集合，**When** 渲染 available-skills 段，**Then** 按名字确定性排序，使相同 skill 集合产生的 prompt **字节级稳定**（cache 友好）。
3. **Given** 新增一个高优先级覆盖，**When** 覆盖落地，**Then** 后续 run 使用覆盖版本，无需重启。

---

### Edge Cases

- skills 目录里**零个**有效 skill 时如何处理？→ 不渲染（或渲染空）available-skills 段，agent 正常工作。
- 单个 `SKILL.md` **超过大小上限**？→ 拒绝/跳过并告警，其余照常加载。
- eligible skill **过多导致目录超出 prompt 预算**？→ 三级降级（full → compact → truncate）并发出可见 ⚠️ 警告，**绝不静默丢 skill**。
- **同一来源内**出现同名 skill？→ 定义去重策略（如先到先得或路径后缀）并告警。
- skill 的 description 与众多其他 skill **高度重叠/歧义**？→ 文档化行为（模型选最具体），并通过 check 命令告警作者。
- agent 被**限定到不含 read 的工具子集**时？→ 跳过 skills 段（无 read 即无法加载 skill）。
- skill 文件在 agent 运行中**被改写**？→ 文档化"是否热重载"行为。
- frontmatter 出现**未知字段**？→ 优雅忽略（前向兼容）。
- `disable-model-invocation: true` 但用户**仍想调用**该 skill？→ 通过显式调用路径（如支持）保留可达性。

## Requirements *(mandatory)*

### Functional Requirements

**发现与加载 (Discovery & Loading)**

- **FR-001**: 系统 MUST 通过扫描配置好的 skill 来源目录（寻找 `SKILL.md`）来发现 skill。
- **FR-002**: 每个 skill MUST 是一个含 `SKILL.md`（YAML frontmatter：`name`、`description` 必填 + markdown body）的目录。
- **FR-003**: 系统 MUST 拒绝（跳过 + 告警）任何缺 `description` 的 `SKILL.md`；缺 `name` MAY 回退到目录名。
- **FR-004**: 系统 MUST 对单个 `SKILL.md` 强制大小上限（取 OpenClaw 默认值 256 KB，可在配置中覆盖）；超限拒绝。
- **FR-005**: 系统 MUST 容错解析 frontmatter：YAML 损坏或非法元数据 MUST 跳过该 skill，绝不崩溃 agent。
- **FR-006**: 系统 MUST 支持 skill 内可选同级目录（`scripts/`、`references/`、`assets/`），并把 body 中的相对路径解析到该 skill 自身目录。

**目录表与 Prompt 注入 (Catalog & Prompt Injection)**

- **FR-007**: 系统 MUST 渲染一个 available-skills 段（列出每个 eligible skill 的 name、description、文件 location）并注入 agent 的 system prompt。
- **FR-008**: 系统 MUST 使该段为稳定、cache 友好的文本：对相同 eligible 集合，**排序与内容字节级确定**。
- **FR-009**: 系统 MUST 在段头附加固定指令：告诉模型如何使用该段——扫描 description、按精确 location 读取匹配 skill、**至多预读一个**、无匹配时不读。
- **FR-010**: 系统 MUST NOT 把完整 skill body 嵌入 system prompt；body 通过 read 能力按需加载。

**按需执行 (On-Demand Execution)**

- **FR-011**: 系统 MUST 提供（或复用）一个 read-file 能力（v1 实现为新 `Read` `ToolDef`，见 plan / research Decision 1），使模型能按目录中宣告的 location 读取 `SKILL.md`。
- **FR-012**: 系统 MUST 广告**精确、模型可用**的文件 location（禁止模型猜测/硬编码）。

**资格判定与元数据 (Eligibility & Metadata)**

- **FR-013**: 系统 MUST 支持声明可选运行前置（env / 二进制 / 配置 / OS）的元数据，并把不满足前置的 skill 排除出目录。
- **FR-014**: 系统 MUST 在**加载时基于当前环境即时计算** eligibility（无持久化陈旧状态）。
- **FR-015**: 系统 MUST 支持 `disable-model-invocation` 标志：把 skill 从模型自动选择目录中隐藏，但保留登记。
- **FR-016**: 系统 MUST 支持 `user-invocable` 标志：控制该 skill 是否作为可显式调用的命令暴露。

**预算控制 (Budget Control)**

- **FR-017**: 系统 MUST 对 available-skills 段强制字符预算并分级降级：full → compact（去 description、保留 name）→ truncate（二分搜索最大前缀），且**降级时必有可见 ⚠️ 警告**。
- **FR-018**: 系统 MUST NOT 静默丢弃 skill——任何 drop 必有可观测告警。

**多来源 (Multi-Source)** — *v1 确认落地 bundled + workspace 双层*

- **FR-019**: 系统 MUST 支持两级来源：`bundled`（agent_core 自带的内置 skill 目录）< `workspace`（项目 skills 目录）。同名 skill MUST 由更高优先级（workspace）版本覆盖；合并后的输出 MUST 按名字字典序确定性排序。v1 不纳入 extra/managed/personal/project 等更高级别。

**触发方式 (Triggering)** — *v1 确认同时落地模型自触发 + 用户 slash 调用*

- **FR-020**: 系统 MUST 同时支持 (a) 模型自触发——目录驱动的自动选择；以及 (b) 用户 slash 命令显式调用——`/skill-name <args>` 在 Streamlit UI 中触发，强制本轮使用指定 skill，绕过模型自动选择。

**子系统广度 (Subsystem Breadth)** — *v1 确认为"完整核心"范围*

- **FR-021**: v1 确认纳入：**发现与加载**、**目录表注入**、**按需 read**、**资格/前置判定**、**触发控制元数据**（`disable-model-invocation` / `user-invocable`）、**预算三级降级**、**bundled+workspace 双层优先级**、**显式 slash 调用**、**inspect/check 命令**、**文件热重载**。
  v1 确认**不纳入**（留待后续）：**安装时安全扫描**（skill 内脚本的代码扫描器）、**env/apiKey 注入**（`primaryEnv` + 子进程防泄漏）、**5 种安装器**（brew/node/go/uv/download）、**ClawHub 远程市场**、**远程节点 bin 探测**。

**热重载 (Hot Reload)**

- **FR-021a**: 当来源目录中的 `SKILL.md` 在 agent 运行期间被新增/修改/删除时，系统 MUST 能（在下一轮交互或可配置的 debounce 后）刷新 skill 快照，使变更无需重启即生效；MUST 维护单调递增的快照版本号用于失效判断。

**运维 (Operational)**

- **FR-022**: 系统 MUST 提供一种方式查看当前 skill 目录及每个 skill 的状态（eligible / blocked / hidden / errored），用于调试。
- **FR-023**: 系统 MUST 记录 skill 加载失败、eligibility 排除、预算降级等事件（日志）。
- **FR-024**: 系统 MUST 自研实现于 `agent_core/skills/`，**不引入 agent 框架依赖**（Constitution Principle I）。

### Key Entities *(include if feature involves data)*

- **Skill**: 一个含 `SKILL.md`（frontmatter + body）及可选资产子目录的目录；由 `name` 唯一标识；具有 source、文件 location、eligibility 状态、invocation/exposure 标志。
- **SkillEntry**: 已发现 skill 的运行时表示——解析后的 Skill 加上 frontmatter、元数据、eligibility 求值、可见性/调用策略。
- **SkillSource / Precedence Level**: 命名来源（如 bundled、workspace、user-managed）及其在同名冲突时用的优先级。
- **Skill Eligibility**: 每次 load 基于当前环境的即时计算（disabled? blocked by config? requires 是否满足？），决定是否进入运行时目录。
- **Available-Skills Section**（运行时字段：`SkillSnapshot.prompt`）: 由 eligible 且可见的 skill 渲染出的 prompt 片段，受字符预算分级约束，注入 system prompt。
- **Skill Status**（运行时：`SkillSummary` 列表 + `format_skill_status()` 函数）: 每个 skill 的诊断记录（eligible / missing-requirements / blocked / hidden / errored），供 inspect/check 命令使用。
- **Skill Metadata**: 控制 trigger 行为的可选 frontmatter 字段（`disable-model-invocation`、`user-invocable`、`requires`、OS 限制等）。

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: 往 skills 目录新增一个 skill **无需改 agent_core 代码、无需重启**，下一次 run 即被发现可用。
- **SC-002**: 在 eligible skill 集合不变时，base system prompt **字节稳定**（cache 友好）；增删一个不相关 skill 不扰动其余 skill 的相对顺序。
- **SC-003**: 匹配任务**至多预读一个** `SKILL.md`；不匹配任务**预读零个**——token 开销与实际使用成正比，而非与目录大小成正比。（注：此为模型策略提示而非代码硬保证；通过 quickstart §1 e2e 验证。）
- **SC-004**: 至少 50 个 eligible skill 时，available-skills 段仍落在配置的字符预算内，通过 compact/truncate 降级 + 可见警告解决，不溢出、不静默丢条目。
- **SC-005**: 单个 `SKILL.md` 损坏或超限，**不拖垮整个 agent**；同来源其他有效 skill 照常加载。
- **SC-006**: 用户能通过单个命令查看 skill 目录与每个 skill 的 eligibility，**精确复现**下一次 run 模型会看到哪些 skill。
- **SC-007**: （启用多来源时）项目级 skill **确定性覆盖**同名内置 skill，覆盖语义可预测。
- **SC-008**: 整个子系统**自研、不引入 agent 框架依赖**，满足 Constitution Principle I。

## Assumptions

- agent 已有（或将已有）一个模型可调用的 **read-file 能力**；skill 按需加载**复用**该能力，不引入新机制。（待 plan 阶段在 `agent_core/tools/` 中确认该能力现状。）
- agent 已通过 handler 链（SystemPromptHandler）组装 system prompt；skills 段注入到该既有组装点，不另起炉灶。
- skill 文件格式**对齐 OpenClaw `SKILL.md` 约定**（frontmatter + markdown body），使本项目的 skill 与参考设计概念兼容；任何偏离均为有意为之并记录。
- 环境为 Python 3.11 + uv（Constitution）；**不新增框架级依赖**。
- v1 面向**本地单用户 / 单 workspace** 场景；OpenClaw 参考中的 ClawHub 远程市场、远程节点 bin 探测等多设备/托管特性**out of scope**（已确认）。
- 默认大小/预算上限沿用 OpenClaw 参考值（256 KB / 18 000 chars / 150 skills 等），除非用户另行指定。
- **v1 已确认范围（"完整核心"）**：发现+加载、目录表注入、按需 read、资格/前置判定、触发控制元数据、预算三级降级、bundled+workspace 双层、slash 显式调用、inspect/check、文件热重载。
- **v1 已确认 out of scope**：安装时安全扫描、env/apiKey 注入、5 种安装器、ClawHub 远程市场、远程节点 bin 探测。（后续版本可再加。）
- 主线 UI 为 Streamlit（`web/app.py`）；slash 命令集成点在该 UI（由用户手动验证），backend 由本系统提供。
