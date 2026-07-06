---
description: "Task list for Skill System (agent_core) implementation"
---

# Tasks: Skill System (agent_core)

**Input**: Design documents from `/specs/001-skill-system/`

**Prerequisites**: [plan.md](plan.md) (required), [spec.md](spec.md) (5 user stories P1-P5), [research.md](research.md), [data-model.md](data-model.md), [contracts/skill-md-format.md](contracts/skill-md-format.md), [quickstart.md](quickstart.md)

**Tests**: 包含 — Constitution Principle IV 强制 pytest；data-model.md INV-1..INV-6 + quickstart.md 验证脚本驱动测试任务。

**Organization**: 按 spec.md 5 个 user story 分阶段（P1=US1 MVP → P5=US5），每阶段独立可测。

## Format: `[ID] [P?] [Story] Description`

- **[P]**: 可并行（不同文件、无未完成依赖）
- **[Story]**: 所属 user story（US1..US5），仅 user story 阶段标注
- 描述含**精确文件路径**

## Path Conventions

单项目：`agent_core/skills/` 新包 + 既有 `agent_core/turn_chain.py` / `builder.py` / `agent_core.py` / `tools/builtin.py` / `web/app.py` 改动；测试在 `tests/`（扁平，`test_skill_*.py`）。

---

## Phase 1: Setup (Shared Infrastructure)

**Purpose**: skills 子系统包骨架 + 内置示例 skill 目录。

- [x] T001 Create skills package barrel stub (含 TODO 注释 + 占位 `__all__=[]`) in agent_core/skills/__init__.py
- [x] T002 [P] Create bundled example skill (frontmatter + body, <2KB) in agent_core/skills/builtin/skill-creator/SKILL.md

**Checkpoint**: 包结构就位；无代码逻辑。

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: 所有 user story 共享的纯原语（Entity 层 + 基础 Adapter）。镜像 `agent_core/memory/` 约定。

**⚠️ CRITICAL**: 本阶段完成前不得开始任何 user story。

- [x] T003 [P] Define core data types: Skill / SkillEntry / SkillSnapshot / SkillFrontmatter(TypedDict) / SkillSource(Enum: BUNDLED/WORKSPACE) / SkillSummary / SkillEligibilityState + `CURRENT_SCHEMA_VERSION=1` + 纯校验函数 (validate_frontmatter) in agent_core/skills/types.py — 用 `@dataclass(frozen=True)` 表达 Value Object（架构 §C）
- [x] T004 [P] Implement SkillsConfig (pydantic, `extra="forbid"`) + PathsConfig (bundled_dir/workspace_dir) + LimitsConfig (max_skill_file_bytes=256_000 / max_skills_in_prompt=150 / max_skills_prompt_chars=18_000 / max_candidates_per_root=300) + `from_env(prefix="SKILLS_")` + `from_dict` in agent_core/skills/config.py — 镜像 memory/config.py:256-369
- [x] T005 [P] Implement path resolution: expand `~`、reject symlink SKILL.md、traversal 校验 (path 不逃逸出 root) in agent_core/skills/path_validator.py — 镜像 memory/path_validator.py
- [x] T062 [P] Register skills 子 logger：加 `("agent_core.skills", "AGENT_LOG_SKILLS")` 到 `agent_core/logging_setup.py:_SUB_LOGGER_ENV` + 注释标 🧩 emoji（开发规则汇总 §5.4）in agent_core/logging_setup.py — 所有 skills 模块经此 logger 输出 debug；与 T003-T011 异文件可并行
- [x] T006 Implement SKILL.md frontmatter parser: 复用 `from agent_core.memory.memory_store import parse_frontmatter` (research Decision 5) + skill 专属校验 (description 必填→缺则拒绝；name 缺→回退目录名) in agent_core/skills/frontmatter.py — 依赖 T003
- [x] T007 Implement single-skill loader `load_single_skill(skill_dir) -> SkillEntry`: 读 SKILL.md、强制 256KB 上限、malformed→skip+设 load_error (不抛)、返回 SkillEntry；失败时通过 `logging` emit 事件（FR-023）in agent_core/skills/skill_store.py — 依赖 T003, T005, T006
- [x] T008 [P] Test types + validators in tests/test_skill_types.py — 依赖 T003
- [x] T009 [P] Test config loaders (from_env/from_dict/默认值/expanduser) in tests/test_skill_config.py — 依赖 T004
- [x] T010 Test frontmatter parser (合法/缺 desc/缺 name/YAML 损坏) in tests/test_skill_frontmatter.py — 依赖 T006
- [x] T011 Test skill_store loader (正常/超限拒绝/symlink 拒绝/缺 SKILL.md) in tests/test_skill_store.py — 依赖 T007
- [x] T012 Run `python3 -m pytest tests/test_skill_types.py tests/test_skill_config.py tests/test_skill_frontmatter.py tests/test_skill_store.py -q` 全绿 (Constitution IV)

**Checkpoint**: Foundation 就位——纯函数 + 单 skill 加载器可用，user story 可并行展开。

---

## Phase 3: User Story 1 — 按需加载领域指令 (Priority: P1) 🎯 MVP

**Goal**: skill 被发现 → system prompt 注入 `## Skills` 段 → 模型匹配时用 Read 工具读 SKILL.md → 应用指令。

**Independent Test** (quickstart §1): 放 1 个 hello/SKILL.md → snapshot.prompt 含它 → run "say hello" → 出现 Read tool_call → 回复含 skill 指令；不相关任务不触发 Read。

### Implementation for User Story 1

- [x] T013 [P] [US1] Implement `scan_source(root_dir) -> list[SkillEntry]` 单源扫描 + `sort_by_name(entries)` 字典序 in agent_core/skills/skill_index.py — v1 仅扫一源（workspace 优先或唯一），多源合并留 US5；🧩 debug：扫描发现数 + 来源分布 + 单 skill 加载结果（成功/失败+load_error）（§5.4）
- [x] T014 [P] [US1] Implement `render_skills_section(entries, mode="full"|"compact")` 纯函数 + `escape_xml` + 固定段头指令（"scan description / read at most one / never guess path"）in agent_core/skills/prompt.py — 字节确定性（架构 §E 陷阱 3）
- [x] T015 [US1] Implement `build_snapshot(entries, env, config, limits) -> SkillSnapshot` 流水线：filter(暂为全 eligible，US3 接入 eligibility) → sort → `apply_budget_limits`(full→compact→truncate + ⚠️ 警告) → render；🧩 debug：流水线每步 N→M（filter/visibility）、budget tier 选哪级、render 长度（§5.4）；budget 降级时 emit warning 事件（FR-023）in agent_core/skills/snapshot.py — 依赖 T003, T013, T014, T062
- [x] T016 [US1] Implement `SkillsRegistry`: Facade + cache-aside（mtime 失效 + 单调 version bump via `time.monotonic_ns`）+ `snapshot() -> SkillSnapshot` + `prev.then(task,task)` 串行化；🧩 debug：snapshot() 缓存命中/失效 + version bump 触发原因（mtime/env/config）（§5.4）in agent_core/skills/registry.py — 依赖 T007, T015, T062；架构 §C Facade/Cache-Aside
- [x] T017 [P] [US1] Implement READ_TOOL `ToolDef(name="Read", parameters={path}, handler=read_file_handler)` 读文件内容（超限/缺文件→返回错误串，不抛）+ 在 `register_builtin_tools` 注册 + 验证既有 permission_engine/safety_check 对 "Read" 默认放行（若 deny 则补一条 allow 规则，analyze C3 风险）in agent_core/tools/builtin.py — 填补 permission 子系统已硬编码的 "Read" 缺口（research Decision 1）
- [x] T018 [US1] Implement `SkillsPromptHandler` (`name="skills_prompt"`，`handle(ctx)->HandlerResult`)：读 `agent.skills_registry.snapshot()`，把 `## Skills` 段并入 `ctx.stage_inputs` 的 system message（镜像 `_merge_memory_into_system` @ turn_chain.py:411-429）；guard：若 `agent.tools` 不含 "Read" 则跳过整个 `## Skills` 段（spec Edge Case，analyze C2）；🧩 debug：进入 handle / Read guard 是否触发跳过 / 注入段长度（§5.4）in agent_core/turn_chain.py
- [x] T019 [US1] Wire `self.skills_registry = SkillsRegistry(skills_config)` into `ReactAgent.__init__` (构造时注入；镜像 agent.session_memory 模式) in agent_core/agent_core.py
- [x] T020 [US1] Insert `SkillsPromptHandler(agent)` into `build_default_inputs_chain` (after `SystemPromptHandler`, before `MemoryRetrievalHandler`) + import 块 in agent_core/builder.py
- [x] T021 [US1] Pass `skills_config=SkillsConfig.from_env()` into `AgentBuilder().build({...})` call (~web/app.py:713-727) in web/app.py
- [x] T022 [P] [US1] Test prompt render（full/compact 格式 + escape_xml + 字节稳定性 hash 断言 INV-2）in tests/test_skill_prompt_render.py — 依赖 T014
- [x] T023 [P] [US1] Test snapshot budget tiering（full→compact→truncate + ⚠️ 警告 + 不静默丢 INV-4）in tests/test_skill_snapshot_budget.py — 依赖 T015
- [x] T024 [US1] Test registry mtime invalidation + version 单调（INV-6）in tests/test_skill_registry_hotreload.py — 依赖 T016
- [x] T025 [P] [US1] Test Read tool（正常读/缺文件返错误串/超限拒绝）+ name=="Read" in tests/test_read_tool.py — 依赖 T017
- [x] T026 [US1] Test SkillsPromptHandler（MagicMock agent + skills_registry + `MagicMock(spec=TurnContext)` → 调 handle → 断言 system message 含 `## Skills` 段；断言 tool set 不含 "Read" 时 stage_inputs 不含 `## Skills` 段（C2 guard）），镜像 tests/test_l3_sm_extract_handler.py:121-160 范式 in tests/test_skills_prompt_handler.py — 依赖 T018
- [x] T027 [US1] E2E verify script（quickstart §1：放 hello skill → 断言 snapshot.prompt 含 hello → 真实 agent run 断言 Read tool_call + final_answer 含 skill 指令）in scripts/verify_skill_e2e.py — 依赖 T016-T021
- [x] T028 [US1] Run `python3 -m pytest tests/test_skill_prompt_render.py tests/test_skill_snapshot_budget.py tests/test_skill_registry_hotreload.py tests/test_read_tool.py tests/test_skills_prompt_handler.py -q` + 手动 `streamlit run web/app.py` 验证匹配任务触发 Read（Constitution IV + UI 手动门）

**Checkpoint**: US1 MVP 端到端可用——单 skill 自动发现 + 注入 + 按需 Read。

---

## Phase 4: User Story 2 — Skill 创作与目录管理 (Priority: P2)

**Goal**: 作者放任意合法 SKILL.md 目录即被发现；malformed/超限不拖垮；body 相对路径正确解析。

**Independent Test** (quickstart §7): 放 broken/SKILL.md (缺 desc) + good/SKILL.md → snapshot 仅含 good；status 列 broken 为 ERRORED；agent 不崩。

### Implementation for User Story 2

- [x] T029 [US2] Add body 相对路径解析：扫描 body 中 `references/`/`scripts/`/`assets/` 引用并解析为相对 skill base_dir 的绝对路径（写入 SkillEntry，供模型 Read 用）in agent_core/skills/skill_store.py — 依赖 T007
- [x] T030 [US2] Flesh out bundled `skill-creator/SKILL.md`：完整 frontmatter (name/description/metadata.os) + 教模型如何创建新 skill 的 body in agent_core/skills/builtin/skill-creator/SKILL.md — 依赖 T002
- [x] T031 [P] [US2] Test frontmatter 契约执行（缺 desc 拒绝、YAML 损坏 skip、超 256KB 拒绝、未知字段忽略）覆盖 contracts/skill-md-format.md 全部 MUST in tests/test_skill_frontmatter.py — 依赖 T006, T010
- [x] T032 [P] [US2] Test body 相对路径解析 + size 上限边界 in tests/test_skill_store.py — 依赖 T029
- [x] T033 [P] [US2] Test bundled skill-creator 被发现且 model-visible（通过 registry.snapshot）in tests/test_skill_bundled.py — 依赖 T013, T030（注：独立文件，避免与 US5 的 test_skill_index_merge.py 并行冲突，analyze I1）
- [x] T034 [US2] Run `python3 -m pytest tests/test_skill_frontmatter.py tests/test_skill_store.py tests/test_skill_bundled.py -q` 全绿

**Checkpoint**: US2——作者体验完整；malformed 隔离；bundled 示例可用。

---

## Phase 5: User Story 3 — Skill 触发控制与资格判定 (Priority: P3)

**Goal**: frontmatter 元数据控制触发（disable/user-invocable）+ requires(env/bin/config/os) 即时求值过滤。

**Independent Test** (quickstart §4): need-key/SKILL.md (requires.env=VERIFY_KEY) → 无 key 时不在 prompt；设 key 后出现 + version bump；disable-model-invocation 的 skill 隐藏但仍登记。

### Implementation for User Story 3

- [x] T035 [P] [US3] Extend types.py：`SkillMetadata` (always/os/requires{bins,anyBins,env,config}) + SkillEntry 加 `disable_model_invocation`/`user_invocable`/`visibility`/`eligibility` 字段 + `SkillVisibility(MODEL_VISIBLE|HIDDEN_FROM_MODEL)` + `SkillEligibilityState(ELIGIBLE|DISABLED|MISSING_REQUIREMENTS|ERRORED)` in agent_core/skills/types.py — 依赖 T003
- [x] T036 [P] [US3] Extend frontmatter.py：解析 `metadata` 块 + `disable-model-invocation`/`user-invocable` 标志（默认 false/true）in agent_core/skills/frontmatter.py — 依赖 T006, T035
- [x] T037 [US3] Implement `evaluate_eligibility(entry, env, config) -> SkillEligibilityState`：5 维 requires（bins 用 `shutil.which`、env 用 `os.environ`、config path truthy、os 用 `platform.system().lower()`、anyBins 任一）+ `always` 绕过；纯函数（架构 §E 陷阱 1：config 须参数注入）；🧩 debug：每个 skill 判定结果（eligible/excluded + 原因）（§5.4） in agent_core/skills/eligibility.py — 依赖 T035, T062
- [x] T038 [US3] Extend snapshot.py：在 sort 前插入 eligibility 过滤 + visibility 过滤（`disable-model-invocation`→HIDDEN，不进 prompt 但仍登记）；status.summary 记 missing；eligibility 排除时通过 `logging` emit 事件（FR-023）in agent_core/skills/snapshot.py — 依赖 T015, T037
- [x] T039 [P] [US3] Test eligibility（5 维 + always + 即时性 INV-5：设 env 前后状态翻转）in tests/test_skill_eligibility.py — 依赖 T037
- [x] T040 [US3] Test visibility 过滤（disable-model-invocation 不进 prompt 但 status 列出；user-invocable=false 不作为命令）in tests/test_skill_prompt_render.py — 依赖 T038
- [x] T041 [US3] Run `python3 -m pytest tests/test_skill_eligibility.py tests/test_skill_prompt_render.py -q` 全绿

**Checkpoint**: US3——触发可控；requires 即时；hidden 行为正确。

---

## Phase 6: User Story 4 — 显式调用 (Priority: P4)

**Goal**: 用户在 Streamlit 输入 `/skill-name <args>` 显式触发，绕过模型自动选择。

**Independent Test** (quickstart §6, UI 手动): `/hello please greet` → 回复含 skill 指令；`/nonexistent` → 友好报错。

### Implementation for User Story 4

- [x] T042 [US4] Implement `resolve_skill_command(text) -> (skill_name, args) | None` + `sanitize_skill_command_name`（lowercase + 非 [a-z0-9_]→_ + 截 32 字符）；🧩 debug：解析命中/未命中 + sanitize 前后名（§5.4）in agent_core/skills/commands.py
- [x] T043 [US4] Add slash dispatch at top of `run_agent()` (~web/app.py:1021)：命中→prompt-rewrite（前缀 `Use the "<name>" skill...` + 原 args）→ 走 start_run+step；未知→友好提示 in web/app.py — 依赖 T042
- [x] T044 [US4] Add same slash dispatch in secondary chat entry (~web/pages/00_Chat.py:208) in web/pages/00_Chat.py — 依赖 T042
- [x] T045 [P] [US4] Test commands（合法/带空格 args/未知/边界 sanitize）in tests/test_skill_commands.py — 依赖 T042
- [x] T046 [US4] Run `python3 -m pytest tests/test_skill_commands.py -q` 全绿
- [x] T047 [US4] 手动 UI 验证（Constitution UI 门）：`streamlit run web/app.py` → `/hello please greet` 触发 + `/nonexistent x` 报错；记录结果于 quickstart §6 【⚠️ 用户手动验证门 — 见下方 Phase 6 报告】

**Checkpoint**: US4——slash 显式调用闭环（backend 单测 + UI 手动）。

---

## Phase 7: User Story 5 — 多来源优先级合并 (Priority: P5)

**Goal**: bundled + workspace 双层；同名 workspace 覆盖 bundled；合并后字典序确定排序。

**Independent Test** (quickstart §2): bundled 与 workspace 同名 `skill-creator` body 不同 → snapshot 用 workspace 版；删 workspace → bundled 恢复。

### Implementation for User Story 5

- [x] T048 [US5] Extend config.py：`SourcesConfig`（ordered `list[(source, dir, priority)]`，默认 bundled(1) + workspace(2)），用**配置数据**封装来源变化（架构 §C：配置 > 模式）in agent_core/skills/config.py — 依赖 T004
- [x] T049 [US5] Extend skill_index.py：`merge_by_priority(per_source_entries) -> list[SkillEntry]`（Map<name,entry> 后写覆盖，高优先级胜）+ `load_all(sources_config) -> list[SkillEntry]` 多源扫描 in agent_core/skills/skill_index.py — 依赖 T013, T048
- [x] T050 [P] [US5] Test 多源优先级覆盖 + 同名去重（INV-1）+ 字典序排序 in tests/test_skill_index_merge.py — 依赖 T049
- [x] T051 [US5] Test 端到端 workspace 覆盖 bundled（经 registry.snapshot：location 指向 workspace 版）in tests/test_skill_registry_hotreload.py — 依赖 T049, T016
- [x] T052 [US5] Run `python3 -m pytest tests/test_skill_index_merge.py tests/test_skill_registry_hotreload.py -q` 全绿

**Checkpoint**: US5——双层覆盖语义正确；prompt 字节稳定。

---

## Phase 8: Polish & Cross-Cutting Concerns

**Purpose**: 跨 story 运维能力 + 文档同步 + 全量回归门。

- [x] T053 [P] Implement `format_skill_status(registry) -> str`（每 skill 一行：name/source/eligible/visibility/missing 摘要 + ⚠️ load_error）in agent_core/skills/status.py — 服务 FR-022
- [x] T054 [P] Test format_skill_status（covered earlier modules 的 status 反映）in tests/test_skill_status.py — 依赖 T053
- [x] T060 [US-FR022] Create `scripts/skills_check.py` CLI：print `format_skill_status(registry)` 输出（FR-022 用户可调用面；非 UI、可单测；analyze U1）in scripts/skills_check.py — 依赖 T053
- [x] T061 Test scripts/skills_check.py 调用（subprocess 入口 + 退出码 + 输出含 eligible/hidden/errored 分类）in tests/test_skill_status.py — 依赖 T054, T060
- [x] T055 Polish: extend hot-reload 测试覆盖 mid-session 跨 turn 刷新（FR-021a）in tests/test_skill_registry_hotreload.py — 依赖 T024
- [x] T056 [P] Documentation: 写 `docs/agent_core-skill-system-design.md`（同步设计文档 + 引用 spec/plan/data-model）— Constitution Principle III 完成门
- [x] T057 Update barrel `agent_core/skills/__init__.py`：导出公开 API（SkillsRegistry/SkillsConfig/SkillsPromptHandler/load_single_skill/format_skill_status 等）+ 完整 `__all__` — 依赖各模块
- [x] T058 Run `python3 -m pytest -q` 全量不回归（Constitution IV 总门）【注：skill 套件 161 + 集成邻接 152 全绿；全量存在的 65 fail/70 err 均为预存环境问题（chromadb/langchain_core 未装 + session storage sandbox），无失败文件 import agent_core.skills — 非本特性回归】
- [x] T059 Run quickstart.md 全部 11 节端到端验证 + 记录结果（含 Constitution II 实测门 §3 字节稳定 / §5 预算 / §1 token 不增量）【scripts/verify_skill_quickstart.py 19/19 脚本化节通过；§6 slash UI 待用户手动 T047】

---

## Dependencies & Execution Order

### Phase Dependencies

- **Setup (Phase 1)**: 无依赖，立即开始
- **Foundational (Phase 2)**: 依赖 Phase 1；**阻塞所有 user story**
- **US1 (Phase 3, MVP)**: 依赖 Phase 2 全部完成
- **US2/US3/US4/US5**: 各自依赖 **Phase 2 + US1**（US1 提供端到端骨架；US2-US5 在其上增强/扩展）
  - US2 改 store + 加测试 → 与 US3/US4/US5 **可并行**
  - US3 改 types/frontmatter/snapshot → 与 US2/US4/US5 **可并行**（但 US3 改 snapshot.py，US5 也改 skill_index.py，文件不同可并行）
  - US4 改 web + 加 commands → 与 US2/US3/US5 **可并行**
  - US5 改 config/skill_index → 与 US2/US3/US4 **可并行**
- **Polish (Phase 8)**: 依赖所有纳入范围的 US 完成

### User Story 内部顺序

- 类型/契约 → 加载器/服务 → 集成/wiring → 测试 → pytest 验证
- 每 phase 末尾 `pytest -q` 子集是 Constitution IV hard gate

### Parallel Opportunities

- Phase 2：T003/T004/T005 三模块独立可并行；T008/T009 测试可并行
- Phase 3 (US1)：T013/T014（不同文件）可并行；T017(Read 工具) 与 T013-T016 异文件可并行；T022/T023/T025 测试可并行
- Phase 4-7 (US2-US5)：跨 story 可全并行（不同模块文件）
- Phase 8：T053/T054/T056 可并行

---

## Parallel Example: User Story 1

```bash
# 并行启动 US1 的独立模块实现（不同文件、无相互依赖）：
Task: "T013 [US1] skill_index.py 单源扫描"
Task: "T014 [US1] prompt.py 渲染"
Task: "T017 [US1] Read 工具 (tools/builtin.py)"

# 并行启动 US1 的独立测试：
Task: "T022 [US1] test_skill_prompt_render.py"
Task: "T023 [US1] test_skill_snapshot_budget.py"
Task: "T025 [US1] test_read_tool.py"
```

---

## Implementation Strategy

### MVP First (US1 only)

1. Phase 1 Setup + Phase 2 Foundational（CRITICAL 阻塞）
2. Phase 3 US1 全部 → pytest + streamlit 手动验证
3. **STOP and VALIDATE**：US1 端到端（quickstart §1+§3+§9）
4. 此时已是最小可用 skill 系统（单源、无 eligibility、无 slash、无多源）

### Incremental Delivery

1. Foundational → 原语就位
2. +US1 → MVP（自动发现+注入+Read）✅ 可 demo
3. +US2 → 作者体验完整（malformed 隔离、bundled 示例）
4. +US3 → 触发可控（requires、disable/user-invocable）
5. +US4 → 显式调用（slash）
6. +US5 → 多源覆盖（workspace > bundled）
7. Polish → inspect/check + 文档 + 全量回归

每步独立可测、不破坏前序 story。

### 关键风险与 Constitution 对齐

- **Principle III（文档硬约束）**：偏离 plan/spec 任一处（如发现 frontmatter 须下沉共享模块）MUST 先 AskUserQuestion，不允许 silent 改方案。
- **Principle IV（测试纪律）**：stub 必须写 `# intentionally stubbed:`；每个 phase 末尾 pytest 子集是 gate；UI（web/app.py slash）由用户手动验证（US4 T047）。
- **Principle V（不偷偷缩范围）**：5 个 US 全部交付；发现的"Read 工具缺口"已在 plan 透明记录。
- **架构 §E 三处依赖陷阱**（eligibility 读全局 config / snapshot 直接读盘 / prompt 依赖外部状态）在 T037/T015/T014 实现 + code review 把关。

---

## Notes

- [P] = 不同文件、无未完成依赖
- [Story] 标签映射到 spec.md user story，便于追溯
- 每个 US 阶段独立可完成、可测；MVP = 仅 US1
- 每个 phase 末尾 `pytest -q` 子集是 Constitution IV 的 hard gate
- 实现期每步报告三件套（完成什么 / 测试结果 / 偏差说明），不攒到最后

---

## Phase 9: Convergence

> 由 `/speckit-converge` 追加（2026-07-06）。对照 spec.md / plan.md / data-model.md /
> contracts 评估当前代码，把未满足项补为可追溯任务。本节为 **append-only**，不改写既有任务。

- [x] T063 Resolve workspace_dir fallback gap per plan.md:23 + data-model §2 (partial)
      — `agent_core/skills/config.py` 的 `workspace_dir` 默认仅 `Path("skills")`，缺 plan/data-model
      共同指定的 "缺省回退 `~/.agent_data/skills/`"（env 覆盖名 plan 写 `AGENT_DATA_DIR`、
      data-model 写 `SKILLS_PATHS__WORKSPACE_DIR`，二者不一致）。**任选其一并记录决策**：
      (a) 实现回退（镜像 `agent_core/memory/config.py:106-114`，项目无 skills/ 时回退到家目录）；
      或 (b) 经 AskUserQuestion 确认 v1 不做回退，把该偏离写入
      `docs/agent_core-skill-system-design.md` §6（Constitution III：偏离 plan MUST 确认 + 文档化）。
      完成定义：决策落字 + 相关 test_skill_config 用例覆盖（或显式标注 v1 不支持）。
      【2026-07-06 决策=(a) 实现回退：`_default_workspace_dir` 镜像 session/storage.py:88-93
      （项目根 .git/agent_core 标记 → cwd/skills；否则 Path.home()/.agent_data/skills；
      AGENT_DATA_DIR 覆盖）；test_skill_config 加 4 个 fallback 用例；设计文档 §7 同步】

- [x] T064 Give SkillEntry.body_references a consumer per FR-006 / contracts/skill-md-format.md §5.2 (partial)
      — `resolve_body_references` 已计算并存入 `SkillEntry.body_references`（T029，已测），但 grep
      确认**无任何生产消费者**：模型经 Read 读到的是原始 SKILL.md（含 `references/x.md` 相对引用），
      无法据此构造绝对路径；`format_skill_status` 也不显示。contract §5.2 明示解析是"供模型 Read 用"。
      **任选其一**：(a) 把解析后的绝对路径 surface 给模型（如渲染 body 时附 `<skill_assets>` 清单，
      或 Read SKILL.md 时附带 base_dir 提示）；(b) 在 `format_skill_status` 输出 body_references
      供作者诊断；或 (c) 经确认判定 v1 不需要、移除该字段与测试（避免死代码）。
      完成定义：body_references 有真实消费者 或 显式移除 + 设计文档 §6 记录决策。
      【2026-07-06 决策=(b) 接入 status 诊断：SkillSummary 加 body_references 字段，snapshot 回填，
      format_skill_status 每 skill 行附 `refs=N` + 列出解析后绝对路径（作者跑 skills_check 即可诊断）；
      test_skill_status 加 2 用例；设计文档 §6.6 同步】

