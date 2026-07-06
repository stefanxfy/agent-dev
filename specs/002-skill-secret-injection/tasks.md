---
description: "Task list for Skill Secret Injection (agent_core) implementation"
---

# Tasks: Skill Secret Injection (agent_core)

**Input**: Design documents from `/specs/002-skill-secret-injection/`

**Prerequisites**: [plan.md](plan.md) (required), [spec.md](spec.md) (3 user stories P1/P2/P3), [research.md](research.md) (10 decisions resolved), [data-model.md](data-model.md) (SecretRef + SkillEntryConfig + EnvOverrideSnapshot), [contracts/config-file-format.md](contracts/config-file-format.md) (YAML contract), [contracts/env-overrides-api.md](contracts/env-overrides-api.md) (Python API), [quickstart.md](quickstart.md) (7-section validation)

**Tests**: 包含 — Constitution Principle IV 强制 pytest；20 新 test 用例覆盖 SC-001..SC-007；audit script (`scripts/verify_skill_secrets_audit.py`) 覆盖 SC-005 字节相等泄漏检测。

**Organization**: 按 spec.md 3 个 user story 分阶段（P1=US1 MVP → P3=US3 多源），每阶段独立可测。

## Format: `[ID] [P?] [Story] Description`

- **[P]**: 可并行（不同文件、无未完成依赖）
- **[Story]**: 所属 user story（US1..US3），仅 user story 阶段标注
- 描述含**精确文件路径**

## Path Conventions

单项目：扩展既有 `agent_core/skills/` 包（不新建顶层项目）；新增 `agent_core/skills/env_overrides.py` 模块；修改 `agent_core/turn_chain.py` / `agent_core/builder.py`；测试在 `tests/`（扁平，`test_skill_secret*.py` / `test_skill_config_entries.py`）；audit 脚本在 `scripts/`。

---

## Phase 1: Setup (Shared Infrastructure)

**Purpose**: env_overrides 新模块骨架 + 异常类型。

- [X] T001 Create env_overrides.py module skeleton with `SecretResolutionError` exception class (subclass of `SkillError`) + module-level docstring 引用 contracts/env-overrides-api.md in agent_core/skills/env_overrides.py — 为 Phase 3 US1 的 resolve/apply 函数预留命名空间

**Checkpoint**: 包结构就位（无实现逻辑）。

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: 三个 user story 共享的纯原语（Entity 层 + 校验 + 配置加载）。镜像 `agent_core/skills/config.py` 的 pydantic 约定。

**⚠️ CRITICAL**: 本阶段完成前不得开始任何 user story。

- [X] T002 [P] Add `SecretRefKind` enum (INLINE/ENV/FILE/SECRET_REF, str-enum) + `SecretRef` frozen dataclass (`kind: SecretRefKind`, `value: str`) with pydantic `min_length=1` validator on value + FILE kind requires absolute path (`startswith("/")`) in agent_core/skills/config.py — 依赖 T001
- [X] T003 [P] Add `SkillEntryConfig` frozen dataclass (`enabled: bool = True`, `secrets: Mapping[str, SecretRef] = {}`) in agent_core/skills/config.py — 依赖 T002
- [X] T004 Add `entries: Optional[Mapping[str, SkillEntryConfig]] = None` field to `SkillsConfig` (backward compat: `None` = legacy mode) + `from_yaml(cls, path: Path) -> SkillsConfig` classmethod (纯文件读取：读 YAML、缺文件返回 entries=None 不抛；**不处理 env 覆盖**——`AGENT_CONFIG_PATH` 由 T033 `load_user_config` 统一处理) in agent_core/skills/config.py — 依赖 T003
- [X] T033 [P] Implement module-level `load_user_config(default: SkillsConfig) -> SkillsConfig`：1. 检查 `os.environ.get("AGENT_CONFIG_PATH")`；若有则用其值（expanduser + 校验 isfile，缺失抛 ConfigValidationError）；2. 否则 fallback 到 `~/.agent_data/config.yaml`（expanduser），不存在 → 返 `default` 不报错（向后兼容）；3. 文件存在 → `SkillsConfig.from_yaml(path)` 把 entries 字段 overlay 到 default.entries（默认 None 不变）；4. 文件存在但 chmod 不为 600 → log warning 不 fail；🧩 debug：log 含加载的 path + entries 数 + 是否来自 env override in agent_core/skills/env_overrides.py — 依赖 T004
- [X] T005 Add `_validate_entries_against_skills(self)` method to `SkillsRegistry` (unknown skill name → `ConfigValidationError`; secret name ⊄ requires.env → `ConfigValidationError`; 一次聚合所有错误后 raise) + `_check_config_file_permissions(path)` helper (warn if readable by group/other; 不 fail) called from `__init__`; 🧩 debug：每条 error 含 skill name + 违反字段名 in agent_core/skills/registry.py — 依赖 T004
- [X] T006 [P] Test config entries 校验 + 缺文件 legacy mode (3 用例：unknown skill name 抛、secret 不在 requires.env 抛、config 文件不存在 entries=None) in tests/test_skill_config_entries.py — 依赖 T005

**Checkpoint**: Foundation 就位——dataclass + 校验 + 加载 + entry 验证可用，user story 可并行展开。

---

## Phase 3: User Story 1 — 单 secret 端到端注入与还原 (Priority: P1) 🎯 MVP

**Goal**: skill 作者声明 `requires.env: [X]` → 用户 config 填 inline 值 → agent run 时 `os.environ["X"]` 被注入 → run 结束 reverter 还原 → secret 值不出现在 prompt / log / session。

**Independent Test** (quickstart §1): 配 1 个 skill + 1 个 inline secret → run → assert `os.environ[X]` 等于 config 值 → reverter 后 assert `X` 不在 `os.environ` → assert secret 不出现在 session.jsonl。

### Implementation for User Story 1

- [X] T007 [P] [US1] Implement `resolve_secret(ref: SecretRef) -> str` (INLINE: 返 `ref.value` 不变; SECRET_REF: raise `NotImplementedError("SECRET_REF reserved for v1.1")`; ENV/FILE: stub with `# intentionally stubbed: implemented in US3 T018/T019` raising `NotImplementedError`) + module-level `_logger = logging.getLogger("agent_core.skills.env_overrides")` 🧩 debug prefix in agent_core/skills/env_overrides.py — 依赖 T001
- [X] T008 [US1] Implement `apply_skill_env_overrides(entries: Sequence[SkillEntry], config: SkillsConfig) -> Callable[[], None]`（snapshot-on-first via `injected: dict[str, str | None]`；遍历每个 entry 的 `entry.metadata.requires.env` 与 `config.entries[entry.skill.name].secrets`；try `resolve_secret` 失败 → log warning + skip 该 key；成功后 `os.environ[name] = value`；返 reverter 函数 还原/删除 injected 各 key）；🧩 debug：注入的 skill 名 + key 名（**绝不记 value**——FR-014）；每个 try-except 包裹对应 one key 不影响其他 in agent_core/skills/env_overrides.py — 依赖 T002, T003, T005
- [X] T009 [US1] Integrate into `SkillsPromptHandler.handle(ctx)`：
    1. guard：`agent is None or `agent.skills_registry` is None or `agent.skills_config` is None or `agent.skills_config.entries` is None` → 跳过 env 注入（与现有 registry/Read guard 同级 early-return）
    2. 调 `apply_skill_env_overrides(snapshot.entries, agent.skills_config)` 获得 reverter
    3. 注册 reverter 到 `agent._pending_env_reverter`（覆盖式）
    4. **不在 finally 调 reverter** —— 由 EnvCleanupHandler (T034) 在 outputs_chain 末尾兜底调用（解决 run-end 泄漏 C2）
    5. 后续逻辑不变（build section + append system + return HandlerResult）
    🧩 debug：注入时 log 含 skill 名 + key 名（**绝不记 value**——FR-014）
    in agent_core/turn_chain.py — 依赖 T008
- [X] T034 [US1] Implement `EnvCleanupHandler` (`name="env_cleanup"`，`handle(ctx) -> HandlerResult`)：取 `getattr(agent, "_pending_env_reverter", None)`；若非 None 调之 + 置 None；幂等（连续两次调用无副作用——第二次 reverter 已 None）；🧩 debug：cleanup 触发时 log "🧩 env reverted: keys=K" (K = 还原的 key 数量——来自 reverter 内部记账或反射 inspect) in agent_core/turn_chain.py — 依赖 T009
- [X] T035 [US1] Insert `EnvCleanupHandler(agent)` into `build_default_outputs_chain`（outputs_chain **末位**——在所有现有 outputs handler 之后；在 builder.py 现有 `build_default_outputs_chain` 函数末尾 append）；同样插入 `AgentBuilder._build_default_chain` 路径（如果存在）；🧩 debug：builder 启动 log "EnvCleanupHandler wired into outputs_chain tail" in agent_core/builder.py — 依赖 T034
- [X] T036 [P] [US1] Wire `skills_config` onto agent：在 `agent_core/agent_core.py` `ReactAgent.__init__` 中 `self.skills_registry = SkillsRegistry(skills_config)` 之后加 `self.skills_config = skills_config`（仅当 skills_config 非 None）；guard：若 skills_config 为 None 保持属性为 None（不创建）；🧩 debug：构造日志 "🧩 skills_config attached: entries={N}"（N = len(skills_config.entries or {})） in agent_core/agent_core.py — 依赖 T009
- [X] T010 [P] [US1] Test resolve INLINE form（1 用例：`resolve_secret(SecretRef(INLINE, "abc"))` 返 `"abc"`；SECRET_REF 抛 `NotImplementedError`）in tests/test_skill_secret_refs.py — 依赖 T007
- [X] T011 [P] [US1] Test apply + reverter (5 用例：apply 单 key 注入；reverter 后 env 还原；reverter pop 未设过的 key；下游抛异常时 try/finally 仍 reverter；多次 apply/revert 幂等无残留) in tests/test_skill_env_overrides.py — 依赖 T008
- [X] T012 [P] [US1] Test SkillsPromptHandler 集成 (+2 用例：handler 调后 `os.environ[X]` 等于 config 值；handler 调后 `os.environ[X]` **仍 set**（不 pop，由 EnvCleanupHandler 兜底——验证 T009 的 no-in-handler-revert 语义）；mock agent + skills_registry + skills_config 范式同 tests/test_skills_prompt_handler.py 既有测试) in tests/test_skills_prompt_handler.py — 依赖 T009, T036
- [X] T037 [US1] Test `agent.skills_config` 暴露（2 用例：构造 agent with skills_config (含 1 entry) → assert `agent.skills_config is not None and agent.skills_config.entries["echo-skill"]`；构造 agent without skills_config → assert `agent.skills_config is None`）in tests/test_skills_prompt_handler.py — 依赖 T036
- [X] T038 [US1] Wire `load_user_config` into `ReactAgent.__init__`：在 `self.skills_registry = SkillsRegistry(skills_config)` 之前调 `load_user_config(skills_config)` 把 entries overlay 到新 SkillsConfig；若 skills_config 为 None → 跳过整个步骤（不创建 registry）；guard：skills_config 非 None 但 load_user_config 抛 ConfigValidationError → 透传 raise（fail-fast 在 startup）；🧩 debug：构造日志 "🧩 user config loaded: entries={N} path={path or 'default'}" in agent_core/agent_core.py — 依赖 T033, T036
- [X] T039 [US1] Test EnvCleanupHandler（3 用例：注入 X → 调 EnvCleanupHandler.handle() → os.environ 不含 X；连续两次调 EnvCleanupHandler 无异常（幂等）；reverter 为 None 时 handle 是 no-op 不报错）in tests/test_skills_prompt_handler.py — 依赖 T035
- [X] T040 [US1] Test `load_user_config` (5 用例：AGENT_CONFIG_PATH 指向合法 yaml → entries 加载；env var 未设 + 默认 `~/.agent_data/config.yaml` 存在 → 加载；默认文件不存在 → 返 default 不报错；AGENT_CONFIG_PATH 指向不存在文件 → raise ConfigValidationError；chmod 600 不对 → log warning 不 raise——用 `caplog` fixture 验证) in tests/test_skill_config_entries.py — 依赖 T038
- [X] T013 [US1] Run `python3 -m pytest tests/test_skill_secret_refs.py tests/test_skill_env_overrides.py tests/test_skill_config_entries.py tests/test_skills_prompt_handler.py -q` 全绿 (Constitution IV hard gate) — 依赖 T040
- [X] T014 [US1] Commit `feat(skills): single secret inline env injection with reverter (P1/US1 MVP)` — 依赖 T013

**Checkpoint**: US1 MVP 端到端可用——单 skill + 单 inline secret 注入 + 还原闭环。

---

## Phase 4: User Story 2 — 单 skill 多 secret + 多 skill 共享 env (Priority: P2)

**Goal**: 一个 skill 同时需要多个 env；两个 skill 共享同一 env 名时 snapshot-on-first 语义正确（reverter 还原到 RUN 前原值，不是上一个 skill 注入后的值）。

**Independent Test** (quickstart §2 + §4 Case B): 配 1 个 skill 需 3 个 secret → run → assert 3 个 env 全部注入 + 全部还原；配 2 个 skill 共享同一 env 名 → run → assert 注入仅一次 + reverter 还原到 RUN 前原值。

### Implementation for User Story 2

- [ ] T015 [US2] (T008 已 iterate all secrets；本任务聚焦) Add 防御 in `apply_skill_env_overrides`：每个 entry 跳过自身无 `metadata.requires.env` 或 `config.entries[entry.skill.name]` 缺失的；key 注入前 log 🧩 debug 含 skill 名 + key 名（无 value）；多 skill 共享 env 名时通过 `injected.setdefault(name, ...)` 实现 snapshot-on-first（research Decision 7）in agent_core/skills/env_overrides.py — 依赖 T008
- [ ] T016 [P] [US2] Test 多 secret + 多 skill 共享 env (2 用例：单 skill 3 secret 一次 apply 全部注入 + reverter 全部还原；skill-A 与 skill-B 共享 env 名 X，apply 后 os.environ[X] 等于唯一值 + reverter 后 X 等于 RUN 前原值——pre-state 设置一不等的 X 验证) in tests/test_skill_env_overrides.py — 依赖 T015
- [ ] T017 [US2] Run `python3 -m pytest tests/test_skill_env_overrides.py -q` 全绿 + Commit `feat(skills): multi-secret + multi-skill env sharing (P2/US2)` — 依赖 T016

**Checkpoint**: US2——多 secret 闭环；多 skill 共享语义正确（snapshot-on-first）。

---

## Phase 5: User Story 3 — 多 secret 源 (inline / env:// / file://) (Priority: P3)

**Goal**: 用户可用三种形式提供 secret——inline 字符串、`env://<ENV_VAR>` 引用外部 shell env、`file:///abs/path` 引用文件内容；env/file 缺失时 warn + skip 不 fail-fast（FR-012）。

**Independent Test** (quickstart §3): 配 3 种源各一个 secret → run → assert resolve 值正确；env 源 shell 未设 → 警告日志 + 跳过注入（run 不崩）；file 源路径不存在 → 警告 + 跳过。

### Implementation for User Story 3

- [ ] T018 [P] [US3] Extend `resolve_secret` ENV form: `os.environ[ref.value]` 读取；不存在 raise `SecretResolutionError(f"env var {ref.value} not set")`；返回值不做 strip（与 shell 行为一致）in agent_core/skills/env_overrides.py — 依赖 T007
- [ ] T019 [P] [US3] Extend `resolve_secret` FILE form: `Path(ref.value).read_text()`；文件不存在或不可读 raise `SecretResolutionError(f"file {ref.value} unreadable: {e}")`；返回值 `.strip()` 移除首尾空白（spec FR-004 Case 3 + quickstart §3）in agent_core/skills/env_overrides.py — 依赖 T018
- [ ] T020 [US3] (T008 已含 try-except 包裹 resolve_secret)；本任务 add 测试验证 warn + skip 行为（FR-012）；guard：`SECRET_REF` kind 在 apply 路径同样 try-except，但 log warning 内容区分 (`not implemented v1.1`)；🧩 debug：每次 warn 含 skill 名 + env key 名 + 源类型 in agent_core/skills/env_overrides.py — 依赖 T019
- [ ] T021 [P] [US3] Test resolve_secret 全部 kind (5 用例：INLINE 返字面值；ENV 返 `os.environ[name]`；ENV 未设抛 `SecretResolutionError`；FILE 读文件 strip 空白；FILE 不存在抛 `SecretResolutionError`；SECRET_REF 抛 `NotImplementedError`) in tests/test_skill_secret_refs.py — 依赖 T020
- [ ] T022 [P] [US3] Test 源缺失 warn + skip 集成 (2 用例：env 源 os.environ 未设 → capture log 含 "WARN" + 该 key 未注入 + run 不 raise；file 源路径不存在 → capture log 含 "WARN" + 该 key 未注入 + run 不 raise) 用 `caplog` fixture in tests/test_skill_env_overrides.py — 依赖 T020
- [ ] T023 [US3] Run `python3 -m pytest tests/test_skill_secret_refs.py tests/test_skill_env_overrides.py -q` 全绿 + Commit `feat(skills): inline/env/file secret source forms with warn-on-error (P3/US3)` — 依赖 T022

**Checkpoint**: US3——三种源端到端可用；缺失源 graceful degradation。

---

## Phase 6: Polish & Cross-Cutting Concerns

**Purpose**: 跨 story 审计能力 + 文档同步 + 全量回归 + secret 热加载。

- [ ] T024 [P] Create `scripts/verify_skill_secrets_audit.py`：CLI 接受 `--config` / `--skills-workspace` / `--run-count N` 参数；workflow：(1) 构造 test skill 含 1 个 secret；(2) run agent N 次；(3) 收集 N 个 session.jsonl + 1 个 agent.log + N 个 SkillSnapshot.prompt；(4) 断言每个 secret 字面值 0 次出现在上述 3 个产物中；(5) 任一非零出现 → exit 1 + diff 打印；为 SC-005 字节相等泄漏检测 (FR-013/FR-014/FR-015) in scripts/verify_skill_secrets_audit.py — 依赖 T023
- [ ] T025 [P] Test audit script (subprocess.run scripts/verify_skill_secrets_audit.py，assert 退出码 0 + stdout 含 "Audit PASSED") in tests/test_skill_secrets_audit.py — 依赖 T024
- [ ] T026 [P] Test hot-reload (1 用例：构造 registry 注入 value="v1" + reverter 还原 + 改 config 文件 value="v2" + 构造新 registry + 注入 assert value="v2"——验证 SC-006 同 process 不重启感知 config 变化) in tests/test_skill_env_overrides.py — 依赖 T023
- [ ] T027 [P] Update barrel `agent_core/skills/__init__.py`：导出 `SecretRef`、`SecretRefKind`、`SkillEntryConfig`、`SecretResolutionError`、`resolve_secret`、`apply_skill_env_overrides` + 加进 `__all__` in agent_core/skills/__init__.py — 依赖 T023
- [ ] T028 [P] Doc sync (Constitution III)：更新 `docs/agent_core-skill-system-design.md` —— §3 数据流图加 env_overrides 注入→revert 环；§6.7 新增章节 "Secret Injection 偏差/扩展" 说明 SkillEntryConfig.entries + reverter pattern + chmod 600 warn 决策 in docs/agent_core-skill-system-design.md — 依赖 T023
- [ ] T029 [P] Doc sync (Constitution III)：更新 `docs/skill/agent_core-skill-architecture.md` —— §4.9 新增章节 "env_overrides"（与 既有 §4.1-§4.8 并列）；§5.7 新增核心设计思想 "Secret Hygiene"（log 仅 key 不 log value + 三层防御：config 校验 + runtime gating + audit script）in docs/skill/agent_core-skill-architecture.md — 依赖 T028
- [ ] T030 Run `python3 -m pytest tests/test_skill_*.py -q` 全套不回归（既有 175+ + 新 20 = 195+；Constitution IV 总门）— 依赖 T029
- [ ] T031 Run `python3 scripts/verify_skill_secrets_audit.py` 端到端 SC-005 + 手动跑 quickstart.md §1..§7 (除 §6 hot-reload 已被 T026 自动化)；每节报告完成什么 / 测试结果 / 偏差说明 (Constitution V) — 依赖 T030
- [ ] T032 Commit `docs(skills): sync design + architecture docs for secret injection` + final summary commit `feat(skills): 002-skill-secret-injection complete (P1+P2+P3, SC-001..SC-007)` 推送当前分支 — 依赖 T031

---

## Dependencies & Execution Order

### Phase Dependencies

- **Setup (Phase 1)**: 无依赖，立即开始
- **Foundational (Phase 2)**: 依赖 Phase 1；**阻塞所有 user story**
- **US1 (Phase 3, MVP)**: 依赖 Phase 2 全部完成
- **US2 (Phase 4)**: 依赖 Phase 3 全部完成（在 US1 的 multi-iteration 之上加 multi-skill 共享语义）
- **US3 (Phase 5)**: 依赖 Phase 4 全部完成（在 US1+US2 之上加多源支持 + warn-on-error）
- **Polish (Phase 6)**: 依赖所有 US 完成

### User Story 内部顺序

- 类型/契约 → 加载器/服务 → 集成/wiring → 测试 → pytest 验证 → commit
- 每 phase 末尾 `pytest -q` 子集是 Constitution IV hard gate

### Parallel Opportunities

- **Phase 2**：T002/T003（T003 依赖 T002）顺序；T006 测试可与 T002-T005 实现并行（但严格说需 T005 完成；标记 [P] 仅表"实现内部可与同 phase 其他 [P] 并行"，**不可与 T005 并行**——见下注）
- **Phase 3 (US1)**：T007 (resolve_secret stub) 与 T008 (apply_skill_env_overrides) 不同函数可并行；T010/T011/T012 测试编写可并行（独立文件）
- **Phase 4-5 (US2-US3)**：US2 改 env_overrides.py + 加测试；US3 改同一文件但不同函数（resolve_secret vs apply）——顺序执行；测试独立文件可并行
- **Phase 6**：T024/T025/T026/T027/T028/T029 跨不同文件 / 脚本 / 文档，全部 [P] 可并行

> ⚠️ 注释：[P] 标记的"可并行"指**与同 phase 其它 [P] 任务可同时由不同协作者/agent 实现**。Phase 2 内 T006 虽标 [P]，实际需 T005 完成才能跑（pytest 依赖 registry 校验）；并行的语义是写代码不冲突，**不是同时跑**。

---

## Parallel Example: User Story 1

```bash
# 并行启动 US1 的独立模块实现（不同文件、无相互依赖）：
Task: "T007 [US1] env_overrides.py — resolve_secret (INLINE + SECRET_REF stub)"
Task: "T008 [US1] env_overrides.py — apply_skill_env_overrides (snapshot + reverter)"

# 并行启动 US1 的独立测试编写（独立文件）：
Task: "T010 [US1] test_skill_secret_refs.py — resolve INLINE"
Task: "T011 [US1] test_skill_env_overrides.py — apply + reverter (5 cases)"
Task: "T012 [US1] test_skills_prompt_handler.py — handler 集成 (+2)"
```

---

## Implementation Strategy

### MVP First (US1 only)

1. Phase 1 Setup + Phase 2 Foundational（CRITICAL 阻塞）
2. Phase 3 US1 全部 → pytest + manual UI verify（quickstart §1）
3. **STOP and VALIDATE**：US1 端到端（单 skill + 单 secret + 注入 + reverter）
4. 此时已是最小可用 secret injection（inline only）

### Incremental Delivery

1. Foundational → dataclass + 校验 + 加载就位
2. +US1 → MVP（inline 单 secret 注入 + reverter）✅ 可 demo
3. +US2 → 多 secret + 多 skill 共享语义 ✅
4. +US3 → env:// + file:// 多源 ✅
5. Polish → audit script + 文档 + 全量回归 + 热加载

每步独立可测、不破坏前序 story。

### 关键风险与 Constitution 对齐

- **Principle III（文档硬约束）**：T028/T029 doc sync 是 hard gate；任何偏离 plan/spec MUST 先 AskUserQuestion，不允许 silent 改方案。
- **Principle IV（测试纪律）**：stub 必须写 `# intentionally stubbed:`（T007 ENV/FILE stub）；每个 phase 末尾 pytest 子集是 gate；audit script (T024) 是 SC-005 hard gate。
- **Principle V（不偷偷缩范围）**：3 US 全部交付；vault / per-agent / 加密 at-rest 已显式 OOS（spec §Out of Scope），不悄悄回退。
- **VI（防御式务实工程）**：EnvCleanupHandler (T034) 兜底 reverter (解决 C2 run-end 泄漏)；warn-on-error (T020)；multi-skill snapshot-on-first (T015)；chmod 600 warn 不 fail (T005)；三层 secret hygiene：config 校验 + runtime gating + audit script (T005 + T008 + T024)；agent.skills_config 暴露 (T036) + load_user_config (T033) 保证 env 注入路径不会因 wiring 缺失而静默跳过 (解决 C1 + C3)。

---

## Notes

- [P] = 不同文件、无未完成依赖
- [Story] 标签映射到 spec.md user story，便于追溯
- 每个 US 阶段独立可完成、可测；MVP = 仅 US1
- 每个 phase 末尾 `pytest -q` 子集是 Constitution IV 的 hard gate
- 实现期每步报告三件套（完成什么 / 测试结果 / 偏差说明），不攒到最后
- 总计 **40 tasks**（含 8 个新增：T033 C3 修复 + T034/T035/T036/T037/T038/T039/T040 C1+C2 修复），覆盖 spec 全部 18 FR + 3 NFR + 7 SC

---

## Test Coverage Matrix (per spec SC-007)

| Test File | Tests | Tasks | SCs / FRs Covered |
|---|---|---|---|
| `tests/test_skill_secret_refs.py` | 6 | T010 (2) + T021 (4, 重叠 INLINE) | FR-004 (3 源), SC-001 |
| `tests/test_skill_env_overrides.py` | 10 | T011 (5) + T016 (2) + T022 (2) + T026 (1) | SC-001/002/003/006, FR-009/010/011/012/018 |
| `tests/test_skill_config_entries.py` | 8 | T006 (3) + T040 (5) | SC-004, FR-005/006, AGENT_CONFIG_PATH 加载 (C3) |
| `tests/test_skills_prompt_handler.py` | 7 | T012 (2) + T037 (2) + T039 (3) | SC-001/002 (handler 集成), C1 (skills_config 暴露), C2 (EnvCleanupHandler) |
| `tests/test_skill_secrets_audit.py` | 1 | T025 (1) | SC-005 (audit 自动化) |
| `scripts/verify_skill_secrets_audit.py` | (CLI) | T024 | SC-005 (audit 实际执行) |
| **总计** | **32** | — | **SC-001..SC-007 + C1/C2/C3 修复验证** 全部覆盖 |

> 注：32 ≥ spec SC-007 的 "20 new test cases" 目标；INLINE form 在 T010 + T021 中重复测试是 acceptable 的冗余（US1 stub 验证 + US3 多源集成验证的视角不同）。新增的 10 个测试（T037+T039+T040）覆盖三个 CRITICAL 修复的验证——若实施期发现冗余可合并。