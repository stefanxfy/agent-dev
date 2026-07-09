# Phase 0 Research: Skill System (agent_core)

**Date**: 2026-07-05
**Status**: Complete — no `[NEEDS CLARIFICATION]` remains.

> 本文件记录 `/speckit-plan` Phase 0 的 10 项技术决策。每条含 **Decision / Rationale / Alternatives**。
> 所有决策基于源码勘察（见 plan.md Technical Context 的文件:行引用）+ Constitution v1.0.0 + 已确认 spec。

---

## Decision 1 — 新增 `Read` 工具填补既有缺口（满足 FR-011）

**Decision**: 在 `agent_core/tools/builtin.py` 新增 `READ_TOOL = ToolDef(name="Read", parameters={"type":"object","properties":{"path":{"type":"string"}},"required":["path"]}, handler=read_file_handler)`，并在 `register_builtin_tools`（`builtin.py:462-466`）注册。

**Rationale**:
- 勘察确认 agent_core **没有任何读文件工具**——仅 `calc`/`search`/`bash`（`builtin.py:170/232/409`）。
- 但 permission/sandbox/safety 子系统**早已硬编码 PascalCase `"Read"` 工具名**（`tools/safety_check.py:189,193,214`、`classifier_fast_path.py:7,44`、`sandbox_decision.py:27`、`permission_matcher.py:393,396`、`permission_ui_helpers.py:40,190`）——它们在 classifier allow-list 里白名单 `"Read"`、对其做 path-secret 检查。这是一个"已规划但未注册"的工具。
- spec FR-011 明确"MUST provide（或 rely on）a read-file capability"——已 anticipated 需要新增。
- 复用既有 permission 子系统：`Read` 自动受 `safety_check`/`permission_engine` 约束，无需新建权限模型。

**Alternatives**:
- (a) **Skill-scoped `read_skill` 工具**（仅读 SKILL.md）：更窄、更安全，但与 permission 子系统已硬编码的 `"Read"` 名字冲突，且无法复用既有白名单。拒绝。
- (b) **复用 `bash` 工具 `cat`**：走 shell，绕过 SKILL.md location 的精确语义，且 bash 受更严 sandbox。拒绝。
- (c) **不在 v1 做 Read，等后续**：违反 FR-011，整个 skill 闭环不可用（模型无法读 SKILL.md）。拒绝。

---

## Decision 2 — 注入策略：per-turn `SkillsPromptHandler`（非 frozen assembler）

**Decision**: 在 `agent_core/turn_chain.py` 新增 `SkillsPromptHandler`（`name="skills_prompt"`），通过 `builder.py:build_default_inputs_chain` 插在 `SystemPromptHandler`（`turn_chain.py:875-907`）之后、`MemoryRetrievalHandler`（`turn_chain.py:752`）之前。handler 读取 `agent.skills_registry.snapshot()`，把 `## Skills` 段并入 `ctx.stage_inputs` 的 system message（镜像 `_merge_memory_into_system` @ `turn_chain.py:411-429`）。

**Rationale**:
- `SystemPromptAssembler.build()`（`turn_chain.py:320-364`）在 `ReactAgent.__init__` 仅运行一次、产物冻结于 `self._cached`——若把 skills 段塞这里，则**磁盘上新增/改 skill 需重启 agent 才生效**，违反 SC-001（"无需重启即生效"）+ FR-021a（热重载）。
- per-turn handler 每轮读当前 `snapshot()`，天然满足 hot-reload；同时因**渲染是纯函数 + 名字字典序排序**，对"同 skill 集合"输出字节确定（SC-002 仍满足）。
- 与既有 `MemoryRetrievalHandler` 同模式（同样每轮 mutate `ctx.stage_inputs`），不引入新注入范式。
- prompt-cache 角度：GLM（本项目默认 provider）**不支持 `cache_control`**（`context/compact.py:492-493`），Anthropic 分支（`llm/providers/anthropic/base.py:74-89`）在主路径因未单独传 `system_prompt=` 也未触发——故当前主路径本就无 system-prompt 级 cache 锚点；per-turn 注入不使现状变差，且 deterministic 渲染为将来接 Anthropic prefix cache 留好基础。

**Alternatives**:
- (a) **塞进 `SystemPromptAssembler.build()`（frozen prefix）**：cache 最友好，但违反 SC-001/FR-021a。拒绝。
- (b) **在 LLM Router 层注入**：违反"router 是 thin dispatch"（`llm/router.py:1-12`），且 Constitution 技术约束"MUST NOT 绕过 router"反向也不该把业务塞进 router。拒绝。
- (c) **plugin handler（`PluginHandler`）注入**：`PluginHandler`（`turn_chain.py:274-298`）只能 append 到链尾、只能 emit 受限事件——能力不足。拒绝。

---

## Decision 3 — 热重载：mtime 失效的惰性快照（不引入 watchdog）

**Decision**: `SkillsRegistry.snapshot()` 内部缓存上一次扫描结果 + 来源目录 mtime；调用时先 stat 两源根目录，若 mtime 未变且 env/config 未变则直接返回缓存（含 bump 过的版本号），否则重扫重建并 bump 单调版本号（`refresh-state.ts` 的 `bumpVersion` 同款逻辑：`max(now, current+1)`，用 `time.monotonic_ns()` 避免墙上时钟回拨）。串行化用 `prev.then(task, task)` 模式（`serialize.ts` 同款）。

**Rationale**:
- 引入 `watchdog` 是新依赖，违反 Principle I"极简依赖"——除非必要不引。
- spec FR-021a 措辞是"下一轮交互或可配置 debounce 后"刷新——**per-turn 调用 `snapshot()` 时做 mtime 检查即天然满足"下一轮交互刷新"**，无需后台线程。
- mtime 失效是文件系统监听的最朴素形式，跨平台（macOS/Linux `os.stat().st_mtime_ns`）。

**Alternatives**:
- (a) **`watchdog` 后台线程**：实时性最好，但新依赖 + 后台线程生命周期管理复杂 + 测试难。v1 拒绝，留待后续若 mtime 方案在大赛道下不够。
- (b) **固定 TTL 缓存（如 5s）**：粒度粗，可能用旧 skill 多轮。拒绝。
- (c) **每次全量重扫不缓存**：50 skill 时每轮 stat+parse 50 文件，浪费。拒绝。

---

## Decision 4 — 配置：pydantic `SkillsConfig` + `from_env("SKILLS_")`（镜像 memory）

**Decision**: 新建 `agent_core/skills/config.py`，定义 `class SkillsConfig(BaseModel)`（`model_config = ConfigDict(extra="forbid", validate_assignment=True)`）+ 嵌套 `PathsConfig`（bundled_dir / workspace_dir）+ `LimitsConfig`（max_skill_file_bytes=256_000 / max_skills_in_prompt=150 / max_skills_prompt_chars=18_000 / max_candidates_per_root=300）。两个 loader：`from_dict` / `from_env(prefix="SKILLS_")`（双下划线表嵌套，如 `SKILLS_PATHS__WORKSPACE_DIR=...`）。**不改全局 `agent_core/config.py:Config`**。

**Rationale**:
- memory 子系统已确立此范式（`memory/config.py:256-369`），并在 `:14` 显式写明"❌ 不修改全局 agent_core.config.Config（保持向后兼容）"——skills 沿用既有 subsystem 约定。
- pydantic v2 已是依赖；`extra="forbid"` 给 skill 配置严格契约（Principle VI 防御式）。
- env 前缀 `SKILLS_` 与 `MEMORY_` 风格一致。

**Alternatives**:
- (a) **扩 `ENV_VAR_REGISTRY`（`config.py:48-76`）**：违反 memory 子系统既定原则，污染全局单例。拒绝。
- (b) **裸 dict 配置无校验**：违反 Principle VI。拒绝。

---

## Decision 5 — frontmatter 解析：复用 `memory_store.parse_frontmatter`（跨子系统 import）

**Decision**: skills 的 SKILL.md frontmatter 解析**直接 `from agent_core.memory.memory_store import parse_frontmatter`**（`memory_store.py:89-118`），在 `skills/frontmatter.py` 内对其结果做 **skill 专属校验**（`name`/`description` 必填、metadata 结构）。`pyyaml` 已是依赖（`requirements.txt:15`）。

**Rationale**:
- `parse_frontmatter` 已是成熟公共函数，被 `memory/memory_index.py:36-45` 跨文件复用——跨子系统 import 有先例。
- SKILL.md 与 memory 的 `.md` 文件同构（YAML frontmatter + body），解析需求一致。
- 不重写解析器，避免双份维护漂移（Principle VI"同一逻辑一份实现"）。

**Alternatives**:
- (a) **下沉到共享 `agent_core/frontmatter.py`**：最干净，但需改 memory 子系统（移动函数 + 改 import），扩大范围。违反"stop and report on out-of-scope"——**记录为后续可选重构**，v1 用跨 import。
- (b) **skills 内重写一份**：违反"同一逻辑一份实现"，维护漂移风险。拒绝。

---

## Decision 6 — slash 命令：`run_agent` 顶部 prompt-rewrite（非 tool-dispatch）

**Decision**: 在 `web/app.py:run_agent`（`:1021`）顶部加 `resolve_skill_command(user_input)`（`skills/commands.py`）：
- 命中 `/skill-name <args>` → 改写 user message 为 `"Use the \"<name>\" skill for this request.\n\nUser input:\n<args>"`，再走既有 `agent.start_run + step`。
- 未命中 `/` 命令 → 原样传入。
- 同步在次要入口 `web/pages/00_Chat.py:206-221` 加同样解析（保持两面一致）。

**Rationale**:
- 勘察确认 web/ **无任何 slash 解析**——`/skill-name` 是新概念。
- prompt-rewrite 是 OpenClaw §5.3 的默认路径：用户意图通过改写注入，LLM 在 `## Skills` 段引导下自行 read SKILL.md。无需新 tool-dispatch 路径。
- `sanitize_skill_command_name`：lowercase + 非 `[a-z0-9_]` → `_` + 截 32 字符（OpenClaw `command-specs.ts:33-40` 同款），避免命令注入。

**Alternatives**:
- (a) **tool-dispatch（不进 LLM，直接调 tool 返回）**：OpenClaw 仅对 `command-dispatch: tool` 子集用；v1 skill 无此元数据，过度设计。拒绝。
- (b) **新建独立 Streamlit 页面管理 skill**：超出 v1（UI 由用户手动验证），后续可加。拒绝。

---

## Decision 7 — eligibility：5 维 requires 即时求值（无持久化）

**Decision**: `skills/eligibility.py` 的 `evaluate_requires(entry, env, config)` 覆盖 OpenClaw 五维：`bins`（`shutil.which` 全存在）、`anyBins`（任一）、`env`（`os.environ` 含）、`config`（配置 path truthy）、`os`（`platform.system().lower()` 在列表）。`always: true` 绕过 requires。每次 `snapshot()` 基于当前进程环境即时计算，**不落盘**。

**Rationale**:
- Constitution Principle II + spec FR-014：状态即时计算，避免 stale state。
- 既有 `agent_core/config.py` 提供 env 读取；`shutil.which` 是标准库。

**Alternatives**:
- (a) **持久化 installed/needs-update 状态文件**：OpenClaw 明确不做（§6.7"即时计算，无持久化"），引入 stale 风险。拒绝。
- (b) **只支持 env 维度**：覆盖不足（如 `curl` skill 需检测 binary）。拒绝。

---

## Decision 8 — 预算三级降级：full → compact → truncate + ⚠️ 必警

**Decision**: `skills/snapshot.py` 的 `apply_skills_prompt_limits(entries, limits)`：
1. 先按 `max_skills_in_prompt=150` 截断；
2. Tier-1 full：若 `render_full(entries).len ≤ max_skills_prompt_chars=18000` 直接用；
3. Tier-2 compact：full 超预算但 `render_compact`（去 description，保留 name+location）fits（预算预留 150 字符给警告），全部保留降级渲染；
4. Tier-3 truncate：compact 仍超，二分搜索最大前缀 `slice(0, lo)`；
5. 任何降级前置 `⚠️ Skills truncated/compact: ... Run check to audit.` 警告（OpenClaw §3.6 同款文案）。

**Rationale**:
- spec FR-017/FR-018 + SC-004：永不静默丢 skill，超限必有可见警告。
- 默认值取自 OpenClaw `workspace.ts:124-128`（**已引用来源**，非编造）。
- 二分搜索 O(log n)，50 skill 量级无感。

**Alternatives**:
- (a) **超限直接报错中断**：用户体验差，skill 多了就不能用。拒绝。
- (b) **静默截断无警告**：违反 FR-018。拒绝。

---

## Decision 9 — inspect/check：纯函数 `format_skill_status()`，UI 集成延后

**Decision**: `skills/status.py` 提供 `format_skill_status(registry) -> str`（每 skill 一行：name / source / eligible / visibility / missing-requires 摘要），供：
- 测试直接断言（不依赖 UI）；
- 后续 Streamlit 调试页面 / CLI 子命令调用（v1 仅交付函数 + 一个 pytest 调用样例，UI 由用户手动验证）。

**Rationale**:
- Constitution 开发流程门：UI 由用户手动验证；backend/逻辑层由 AI 写测试。`format_skill_status` 是 backend 纯函数，可测。
- spec FR-022 要求"一种方式查看"——纯函数即满足，最小可用。

**Alternatives**:
- (a) **v1 即建完整 Streamlit 管理页**：超出 v1，UI 测试不在 pytest 范围。拒绝，留后续。

---

## Decision 10 — bundled 示例 skill：ship 1 个 `skill-creator`

**Decision**: `agent_core/skills/builtin/skill-creator/SKILL.md`——一个自描述元 skill（教模型如何创建新 skill），内容精简（< 2KB），frontmatter 完整（name/description/metadata）。使系统开箱即有 ≥1 skill，可端到端验证（quickstart.md 依赖它）。

**Rationale**:
- bundled+workspace 双层优先级（Q3 确认）需要 bundled 源非空才能验证覆盖语义。
- SC-001/SC-005/SC-006 的验证脚本需要一个真实 skill。
- `skill-creator` 是 OpenClaw 真实示例（§2.5 引用），概念熟悉，无业务依赖。

**Alternatives**:
- (a) **ship 空 bundled 目录**：双层优先级无法测，quickstart 无依托。拒绝。
- (b) **ship 多个示例**：增加 v1 体量，1 个足证闭环。拒绝，留后续。

---

## 假设（Assumptions，承自 spec + 本 Phase 确认）

- agent_core 主路径默认 provider 为 GLM（不支持 `cache_control`）——skills 段的 cache 友好性是**为 Anthropic 接入预留**，当前不产生实际 cache 命中（**实测门**：实现后须用 `input_tokens` 验证不增加额外开销）。
- `Read` 工具的 permission 默认策略沿用既有 `permission_engine`（本 plan 不改 permission 规则）。
- web/app.py slash 集成由用户手动验证（Constitution 开发流程门）。
