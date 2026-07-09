# agent_core Skill 系统设计文档

**Version**: 1.0.0 | **Date**: 2026-07-06 | **Branch**: `feature/skill-system`

> Constitution Principle III（文档即硬约束）完成门：本文件同步 `specs/001-skill-system/`
> 的 spec / plan / data-model / contracts / quickstart，并记录实现期决策与偏差。

## 1. 概述

按需加载的领域指令包：每个 skill 是含 `SKILL.md`（YAML frontmatter + markdown body）的目录。
系统在每轮交互时把"可用 skill 目录表"（name + description + 文件 location）作为
`## Skills` 段注入 system prompt；LLM 在任务匹配时通过 `Read` 工具按需读取 `SKILL.md`
并遵循其专门指令。同时支持用户 `/skill-name <args>` 显式触发。

**5 个 user story**（spec.md）全部交付：
- US1 自动发现 + 注入 + 按需 Read（MVP）
- US2 作者体验（malformed 隔离、bundled 示例、body 相对路径）
- US3 触发控制（requires 五维 + disable/user-invocable）
- US4 slash 显式调用
- US5 多来源优先级合并（workspace 覆盖 bundled）

## 2. 架构分层（同心圆，依赖指向内层）

```
Frameworks/Drivers  turn_chain.py:SkillsPromptHandler · web/app.py+00_Chat.py:slash 派发
                    config.py:from_env · path_validator.py
Interface Adapters  registry.py:SkillsRegistry(Facade+CacheAside) · skill_store.py · skill_index.py
Use Cases           snapshot.py:build_snapshot · eligibility.py · commands.py · status.py
Entities            types.py · frontmatter.py · prompt.py（零 IO、零外部 import）
```

依赖铁律：Entity 不 import 外层；UseCase 只 import Entity；Adapter import UseCase+Entity；
Framework import Adapter。跨子系统：skills → memory（单向复用 `parse_frontmatter`）；
turn_chain → skills（注册 handler）。**无环**。

## 3. 模块映射

| 模块 | 层 | 职责 |
|---|---|---|
| `types.py` | Entity | 数据契约（Skill/SkillEntry/SkillSnapshot/SkillMetadata/枚举）+ 纯校验 |
| `frontmatter.py` | Entity | SKILL.md → frontmatter（复用 memory.parse_frontmatter）+ skill 校验 |
| `prompt.py` | Entity | `render_skills_section(full/compact)` + escape_xml（字节确定） |
| `snapshot.py` | UseCase | filter(eligibility+visibility) → sort → budget 三级降级 → render |
| `eligibility.py` | UseCase | `evaluate_eligibility` 五维 requires + always（纯函数，参数注入） |
| `commands.py` | UseCase | `/skill-name` 解析 + sanitize |
| `status.py` | UseCase | `format_skill_status`（诊断表格） |
| `env_overrides.py` | UseCase | (002-skill-secret-injection) SecretRef/resolve + apply + reverter + load_user_config |
| `skill_store.py` | Adapter | 单 skill 目录 → SkillEntry；body 相对路径解析 |
| `skill_index.py` | Adapter | scan_source + `merge_by_priority` + `load_all` |
| `registry.py` | Adapter | Facade：snapshot() + mtime/env 失效缓存 + version 单调 |
| `config.py` | Framework | pydantic SkillsConfig + SourcesConfig + (002) entries + from_yaml |
| `path_validator.py` | Framework | symlink 逃逸校验 |

## 4. 数据流（snapshot 构建）

```
SkillsRegistry.snapshot()
  │  sig = (bundled_mtime, workspace_mtime, hash(os.environ))  ← 失效判定
  │  cache 命中 → return；miss → _rebuild
  ▼
load_all(config)
  │  config.sources_list() 升序 → 每源 scan_source → merge_by_priority → sort_by_name
  ▼
build_snapshot(entries, env, config, limits)
  │  _filter_eligible_visible: evaluate_eligibility × visibility → prompt_entries + all_evaluated
  │  sort → _apply_budget_limits(full→compact→truncate + ⚠️ 警告) → render_skills_section
  ▼
SkillSnapshot{prompt, skills[SkillSummary], version, render_mode, truncated_count}
```

注入：`SkillsPromptHandler`（turn_chain）读 `agent.skills_registry.snapshot()`，把 `## Skills`
段并入 system message；若 `agent.tools` 不含 "Read" 则跳过整段（spec Edge Case）。

### 4.1 运行时 secret 注入环（002-skill-secret-injection）

```
ReactAgent.__init__
  │  skills_config → load_user_config → SkillsConfig (entries overlay)
  ▼
SkillsRegistry(skills_config)
  │  __init__ → _validate_entries_against_skills (unknown skill / secret ⊄ requires.env)
  ▼
SkillsPromptHandler.handle(ctx)  (turn_chain inputs_chain 起始)
  │  guard: agent.skills_config.entries is None → skip (legacy mode)
  │  apply_skill_env_overrides(entries, cfg)
  │    ├─ for each entry: resolve_secret(SecretRef) → os.environ[secret_name] = value
  │    │  (snapshot-on-first, multi-skill 共享 env 名 语义正确)
  │    │  resolve 失败 → log WARN + skip (FR-012 graceful degradation)
  │    └─ 返 reverter() 闭包(注册到 agent._pending_env_reverter)
  ▼
LLM call + tool execution (Bash 等继承 process.env, FR-018)
  ▼
EnvCleanupHandler.handle(ctx)  (outputs_chain 末位, C2 run-end 兜底)
  │  取 _pending_env_reverter → 调之 → 置 None (幂等)
  │  os.environ 还原到 RUN 前状态 (snapshot-on-first 语义)
```

**三层 secret hygiene 防御**（不变量 SRH-1）：
1. 配置校验：`SkillsRegistry._validate_entries_against_skills` 在 load 时 fail-fast, 阻止
   unknown skill / secret name ⊄ requires.env 进入 run 路径 (FR-005/006 + C1 wiring)。
2. Runtime gating：`apply_skill_env_overrides` 每个 secret 注入前再 check `secret_name ∈
   required_env`, 防御中间层被绕过 (FR-008)。
3. Audit script：`scripts/verify_skill_secrets_audit.py` 端到端字节相等扫描 prompt/log/session
   三个产物, 确保 SC-005 (FR-013/014/015)。

**关键设计点**:
- snapshot-on-first: reverter 还原到 RUN 前原值, 不是上一个 skill 注入后的值 (FR-011)
- reverter 走 `EnvCleanupHandler` (兜底在 outputs_chain 末位) 而非 `try/finally` 在
  `SkillsPromptHandler` 内 — 解决 C2 "run-end 泄漏" 风险, 即使下游 handler 抛异常也
  能清理 (apply 注册 reverter → handler 异常也由后续 cleanup 兜底)
- log only key names (NEVER value, FR-014) — `_logger.debug("🧩 injected: skill=%s secret=%s kind=%s", ...)` 不带 value

## 5. 关键不变量（INV-1..INV-6，data-model §11）

| 不变量 | 实现位置 | 验证测试 |
|---|---|---|
| INV-1 去重 | `merge_by_priority` 跨源高优先级覆盖 | test_skill_index_merge |
| INV-2 字节稳定 | `render_skills_section` 纯函数 + 名字字典序 | test_skill_prompt_render |
| INV-3 容错 | `load_single_skill` 失败设 load_error 不抛 | test_skill_store |
| INV-4 预算不静默丢 | `_apply_budget_limits` 降级前置 ⚠️ | test_skill_snapshot_budget |
| INV-5 eligibility 即时 | `evaluate_eligibility` 参数注入 + registry env-hash 失效 | test_skill_eligibility / hotreload |
| INV-6 version 单调 | `SkillsRegistry._rebuild` bump + cache | test_skill_registry_hotreload |

## 6. 实现期决策与偏差（透明记录）

### 6.1 合并语义（`merge_by_priority`）
- **per-source 同名先到先得**：同一 source tuple 内同名 skill，首个胜 + 告警（契约 §5.3）。
  用 per-source `seen` 集合判定，**不**用 `SkillSource` enum 值——不同目录同 enum 值视为
  跨源（允许覆盖），支撑 US5 自定义 >2 层来源。
- **broken 高优先级 shadow 低优先级 valid**：契约 §5.3 "workspace 无条件覆盖 bundled" 的
  字面执行——workspace 的 `load_error` entry 仍覆盖 bundled valid。结果：broken skill 在
  prompt 中被 eligibility 过滤（ERRORED），不出现；但其 shadow 导致 bundled 也不出现。
  "删除恢复"按 quickstart §1/§2 语义 = **删除整个 skill 目录**（scan_source 不再产出 entry），
  bundled 自然恢复。如需 "broken 不 shadow valid" 的 fallback 语义，留待 v2。

### 6.2 env 变化触发 hot-reload（INV-5）
registry sig 除目录 mtime 外，加入 `hash(tuple(sorted(os.environ.items())))`。env 变化 →
sig 变 → 缓存失效 → 重建 → eligibility 即时重算 + version bump。代价：每次 snapshot() 计算
env 哈希（数十项，亚毫秒）。生产可接受。

### 6.3 `DISABLED` 状态 v1 不可达
`SkillEligibilityState.DISABLED` 枚举保留，但 v1 `SkillsConfig`（data-model §9）无 per-skill
disable 机制，`evaluate_eligibility` 不会产生 DISABLED。disable 走 frontmatter
`disable-model-invocation` → `HIDDEN_FROM_MODEL`（visibility，非 eligibility）。

### 6.4 `requires.config` 参数注入（架构 §E 陷阱 1）
`evaluate_eligibility(config=...)` 参数注入；registry v1 传 `config={}`（agent 全局 Config 未
接），故 `requires.config` 的 skill 在生产 v1 视为 unmet。snapshot 端测试可注入真实 config dict
验证 truthy 路径解析。接 agent Config 留待 v2。

### 6.5 slash prompt-rewrite
`/name args` → registry.get_entry(name) 校验（存在 + user_invocable）→ rewrite 为
`Use the "name" skill. Read its SKILL.md at "<location>" ... <args>` → 走正常 start_run+step。
显式带 location 兼容 HIDDEN skill（不在 `<available_skills>` 目录表）。未知/不可调用 → 友好报错。

### 6.6 body 相对路径（US2 T029 + Convergence T064）
`resolve_body_references` 扫描 body 中 `references/`/`scripts/`/`assets/` 引用 → 解析为相对
base_dir 的绝对路径，存入 `SkillEntry.body_references`（去重 + 字典序）。纯 normpath 拼接，
无 IO（不验证存在）；含 `..` 段丢弃（防逃逸）。句尾标点（`. - /`）rstrip 防贪婪吞入。

**消费者（T064 决策=(b) 接入 status 诊断）**：`SkillSummary.body_references` 由 snapshot 回填，
`format_skill_status` 每 skill 行附 `refs=N` 并列出解析后绝对路径（作者跑 `scripts/skills_check.py`
即可诊断资产引用）。未接 model prompt（避免扰动 SC-002 字节稳定/cache）；模型从 `<location>` 推导
dirname 构造 sibling 路径对 v1 读可靠性已够用。**已知局限**：regex 扫描无法区分真实引用与举例性
提及（如 bundled skill-creator body 里 `(e.g. references/cheatsheet.md)` 会被计入），属可接受的
诊断噪声，作者据列表判断。

### 6.7 Secret Injection 偏差/扩展（002-skill-secret-injection）

**为什么加 SkillEntryConfig.entries 而非扩展 SkillsConfig.paths**：spec 考虑过两类入口
（per-skill `entries` 字段 vs agent 全局 `Config` 顶层字段）。最终选前者：(a) 与 skill
lifecycle 同生命周期（registry 重建时 entries 失效，热加载语义统一）；(b) 校验更紧凑
（unknown skill / secret name ⊄ requires.env 在 registry 构造时一次性聚合 fail-fast）；
(c) 数据局部性 — secret 跟随 skill 配置, 不污染 agent `Config` 全局 schema。

**为什么 reverter 走 EnvCleanupHandler 而非 try/finally in SkillsPromptHandler**：原
plan 写 try/finally in handler。实现期发现风险：
- handler 抛异常时, 后续 handler 仍会运行 → 此时 os.environ 已被注入但 cleanup 未触发
- run-end 路径 (SessionFlushHandler 后) 无明确清理点 → C2 run-end 泄漏风险

改为 outputs_chain 末位 `EnvCleanupHandler(agent)` 兜底：
- 收口所有 handler 异常路径 (即使整个 run 抛错, cleanup 在 outputs_chain 末位一定执行)
- idempotent (二次调用 no-op: reverter 已 None)
- 与 input/output chain 分层清晰对齐 (输入侧注入 + 输出侧清理)

**为什么 ENV form 不 strip + FILE form strip**：
- ENV form: shell `env` 不 strip, 一致行为; 用户显式控制空白语义
- FILE form: 文件通常带 trailing newline (POSIX text file 惯例), `.strip()` 隐式剔除
  否则 secret value 末尾会带 `\n`, 多数下游 (HTTP header / JWT) 会失败

**chmod 600 warn-not-fail (FR-017)**：OS-dependent (Windows ACL / FAT32 无 unix mode
bit)。实现走 `stat.S_IMODE` + `0o077` 屏蔽检查, 不支持平台 silent no-op。**不 fail**
避免引入 OS-specific 错误路径, 符合防御式务实工程。

**三层 secret hygiene 实际效果**（覆盖 SC-005 hard gate）：
1. 配置校验 + fail-fast: 阻止 100% schema-错的配置进入 run (SC-004)
2. Runtime gating + warn-skip: 即使有未声明 secret 滑入, runtime 不注入 (FR-008)
3. Audit script 字节相等扫描: 端到端验证 secret 不出现在产物 (SC-005)

## 7. 配置（SkillsConfig）

```
paths:    bundled_dir(包内 builtin) · workspace_dir(项目 ./skills → 回退 ~/.agent_data/skills；AGENT_DATA_DIR 覆盖)
limits:   max_skill_file_bytes(256KB) · max_skills_in_prompt(150) · max_skills_prompt_chars(18000) · max_candidates_per_root(300)
load:     enabled(true)
sources:  Optional[SourcesConfig]  # 显式多源；None → paths 派生 bundled(1)+workspace(2)
```

workspace_dir 默认解析（plan.md:23 + data-model §2，镜像 session/storage.py:88-93）：
项目根(cwd 含 `.git`/`agent_core`) → `cwd/skills`；否则 `~/.agent_data/skills`；
`AGENT_DATA_DIR` env 覆盖家目录基。default_factory 内用 `Path.home()` 直接展开
（pydantic v2 `validate_default=False`，default 产出不经 `_expand` validator）。

env 加载：`SKILLS_PATHS__WORKSPACE_DIR=...`（双下划线表嵌套）。

## 8. 测试地图（13 文件，~165 用例）

`test_skill_types` · `test_skill_config` · `test_skill_frontmatter`(契约 MUST) ·
`test_skill_store`(loader+body refs+size 边界) · `test_skill_bundled`(bundled 发现) ·
`test_skill_eligibility`(5 维+always+INV-5) · `test_skill_prompt_render`(render+字节稳定+visibility) ·
`test_skill_snapshot_budget`(三级降级+⚠️) · `test_skill_index_merge`(多源覆盖+去重+字典序) ·
`test_skill_commands`(slash 解析+sanitize) · `test_skill_registry_hotreload`(mtime/env 失效+version 单调+mid-session) ·
`test_skill_status`(format_skill_status + skills_check.py CLI subprocess) · `test_skills_prompt_handler`(handler+Read guard) ·
`test_read_tool`(Read 工具)。

**UI 手动门**（Constitution IV）：`web/app.py` + `web/pages/00_Chat.py` slash 集成由用户
`streamlit run web/app.py` 手动验证（quickstart §6）。

## 9. 参考

- [spec.md](../specs/001-skill-system/spec.md)（FR-001..FR-024 + FR-021a）
- [plan.md](../specs/001-skill-system/plan.md)（架构 §A-F + Constitution Check）
- [data-model.md](../specs/001-skill-system/data-model.md)（实体 §1-9 + INV §11）
- [contracts/skill-md-format.md](../specs/001-skill-system/contracts/skill-md-format.md)（作者契约）
- [quickstart.md](../specs/001-skill-system/quickstart.md)（11 节端到端验证）
- [docs/skill/openclaw-skill-system.md](skill/openclaw-skill-system.md)（参考来源）
