# Implementation Plan: Skill System (agent_core)

**Branch**: `feature/skill-system` | **Date**: 2026-07-05 | **Spec**: [spec.md](spec.md)

**Input**: Feature specification from [specs/001-skill-system/spec.md](spec.md)

**Note**: Filled by `/speckit-plan`. Constitution v1.0.0. v1 scope = 完整核心 (Complete Core) + 模型自触发+slash + bundled+workspace 双层。

## Summary

为 agent_core 自研一套 Skill 系统（Constitution Principle I 列明的核心子系统之一）：每个 skill 是一个含 `SKILL.md`（YAML frontmatter + markdown body）的目录；系统在每轮交互时把"可用 skill 目录表"（name + description + 文件 location）作为 `## Skills` 段注入 system prompt，由 LLM 在任务匹配时通过一个 `Read` 工具按需读取 `SKILL.md` 并遵循其专门指令；同时支持用户在 Streamlit UI 中 `/skill-name <args>` 显式触发。

技术上：skills 子系统与既有 `agent_core/memory/` 子系统近同构（同为 `.md`+frontmatter、文件后端、pydantic 配置、prompt 注入 handler），故**镜像 memory 子系统的目录与约定**。注入走 SETUP 链新增的 `SkillsPromptHandler`（`turn_chain.py`），消费一个带版本号 + mtime 失效的 `SkillsRegistry.snapshot()`——满足"无需重启即生效"（SC-001/FR-021a）与"同集合字节稳定"（SC-002）。一个新 `Read` `ToolDef`（`tools/builtin.py`）填补 permission 子系统早已预期但未注册的 `"Read"` 工具缺口（FR-011）。

## Technical Context

> 全部条目经源码勘察确认（文件:行引用见 research.md）。无 NEEDS CLARIFICATION——所有技术未知数在 Phase 0 已解。

**Language/Version**: Python 3.11（Constitution 技术约束）；包管理 `uv`。

**Primary Dependencies**: 仅复用**既有**依赖——`pydantic>=2`（subsystem config 约定）、`pyyaml>=6.0`（`requirements.txt:15`，frontmatter 解析）、`python-dotenv`。**不新增任何依赖**（`watchdog` 等热重载库明确排除，改用 mtime 失效的惰性快照）。

**Storage**: 文件后端。bundled skill 走包内目录（`agent_core/skills/builtin/`），workspace skill 走项目 `skills/`（缺省回退 `~/.agent_data/skills/`，env `AGENT_DATA_DIR` 覆盖——镜像 `session/storage.py:88-93` 与 `memory/config.py:106-114`）。运行时状态（快照版本号、active env 注入计数）仅内存，**不持久化**（FR-014 即时计算）。

**Testing**: `pytest`。`tests/` 扁平、116 文件、`test_*.py`/`Test*`/`test_*` 约定；无 root pytest 配置（默认发现）；共享 fixture 在 `tests/conftest.py`（含 autouse `_reset_sandbox_singleton`）。handler 测试范式见 `tests/test_l3_sm_extract_handler.py:121-160`（MagicMock agent + 真 `RunState` + `MagicMock(spec=TurnContext)` → 调 `.handle(ctx)` → 断言 `HandlerResult`+Mock 调用+state 变更）。新增 ~10 个测试文件，覆盖 frontmatter/store/index-merge/eligibility/snapshot-budget/prompt-render/commands/handler/read-tool/registry-hotreload。

**Target Platform**: 本地单用户 / 单 workspace（开发机 macOS / Linux）。Python 3.11 runtime。

**Project Type**: library（agent_core 是自研 agent 库）+ 配套 Streamlit web app（`web/app.py`，仅 slash 命令集成点；UI 由用户手动验证，Constitution 开发流程门）。

**Performance Goals**: 
- 快照构建（扫描两源 + 合并 + 渲染）在 ≤50 skill 时亚毫秒级（内存 + 少量 stat）；budget 降级二分搜索 O(log n)。
- system prompt skills 段受 `maxSkillsPromptChars=18000` 约束（OpenClaw 默认），不击穿上下文预算。
- 惰性 mtime 失效：无磁盘变化时 `snapshot()` 命中内存缓存，零 IO。

**Constraints**: 
- 不引入 agent 框架（Principle I）。
- 不破坏既有 prompt cache 行为：skills 段对**同 skill 集合**字节确定（SC-002），渲染纯函数 + 名字字典序。
- 不持久化 eligibility 状态（FR-014）；每次 snapshot 基于当前环境即时求值。
- `Read` 工具受既有 permission/sandbox 子系统约束（`safety_check.py` / `permission_engine`），不绕过。

**Scale/Scope**: 单 workspace、单用户；预期 skill 数 1–100 量级（bundled 几个示例 + 用户自添）。v1 不面向多设备 / 远程市场（已确认 out of scope）。

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

对照 `.specify/memory/constitution.md` v1.0.0 Core Principles 逐条核对：

| 原则 | 核对结论 | 证据 / 措施 |
|---|---|---|
| **I. 自研优先** | ✅ PASS | Skill 系统是 Constitution 明列的核心自研子系统；全部代码落 `agent_core/skills/`；不装 agent 框架；不新增依赖（pyyaml/pydantic 已在白名单）；`Read` 工具自研。 |
| **II. 数据驱动** | ✅ PASS（带实测门） | budget 默认值（256KB / 18000 chars / 150 skills）取自 OpenClaw 公开源码（**已引用来源**，非编造）；GLM 不支持 `cache_control` 的事实来自 `context/compact.py:492-493`。**实现期门**：SC-002 字节稳定性、SC-003 token 开销 MUST 用真实 LLM `input_tokens` 实测验证（写入 quickstart.md 验证步骤）。 |
| **III. 文档即硬约束** | ✅ PASS | 本 plan 严格对齐 spec FR-001..FR-024 + FR-021a；偏离 spec 处仅"Read 工具"（spec FR-011 已anticipated "provide 或 rely on"），已透明记录。**实现完成门**：MUST 新写/更新 `docs/agent_core-skill-system-design.md`（同步设计文档）。 |
| **IV. 测试纪律** | ✅ PASS | 每个新模块配单测（~10 文件）；handler 测试镜像 `test_l3_sm_extract_handler.py` 范式；fallback 路径（malformed YAML / 缺 desc / 超限 / 空目录）同等覆盖；stub 必须写 `# intentionally stubbed:`；UI（web/app.py slash 集成）不在 pytest 覆盖范围——MUST 标注由用户手动验证。 |
| **V. 不偷偷缩范围** | ✅ PASS | 用户确认"完整核心"scope，本 plan 全量交付（10 子系统全落地）；发现的"Read 工具缺口"是 FR-011 的必要前置，**透明上报**非 silent 扩/缩；`watchdog` 改惰性 mtime 是 informed default（research.md 记录决策 + 备选）。 |
| **VI. 防御式务实工程** | ✅ PASS | LLM 调用既有 router 重试；SKILL.md 解析容错（FR-005 malformed → skip 不崩）；budget 三级降级必有 ⚠️ 警告（FR-017/18，绝不静默丢）；文件 IO（缺目录 / 权限拒）优雅降级；hot-reload 用 prev.then(task, task) 模式避免阻塞。 |

**Gates**: 无违例 → **Phase 0 放行**。Complexity Tracking 表为空（无原则违例需 justify）。

## Project Structure

### Documentation (this feature)

```text
specs/001-skill-system/
├── plan.md              # 本文件
├── research.md          # Phase 0: 技术决策 + 备选（无 NEEDS CLARIFICATION）
├── data-model.md        # Phase 1: Skill/SkillEntry/SkillSnapshot 等实体
├── quickstart.md        # Phase 1: 端到端验证脚本
├── contracts/
│   └── skill-md-format.md   # Phase 1: SKILL.md frontmatter 契约（skill 作者面）
├── checklists/
│   └── requirements.md      # /speckit-specify 产物
└── tasks.md             # (/speckit-tasks 生成，非本命令)
```

### Source Code (repository root)

> 选定 **Option 1: 单项目**（agent_core 是单体库）。skills 子系统镜像 `agent_core/memory/` 约定。

```text
agent_core/skills/
├── __init__.py              # barrel：公开 API + __all__（镜像 memory/__init__.py）
├── types.py                 # SkillKind Literal、SkillFrontmatter TypedDict、SkillEntry / SkillSnapshot dataclass、validate_* 纯函数、CURRENT_SCHEMA_VERSION
├── config.py                # pydantic SkillsConfig（paths/limits/install 占位）+ PathsConfig，from_env(prefix="SKILLS_")（镜像 memory/config.py:256-369）
├── path_validator.py        # skill 路径解析 + symlink 逃逸校验（镜像 memory/path_validator.py）
├── frontmatter.py           # 复用 memory_store.parse_frontmatter + skill 专属校验（name/description 必填等）
├── skill_store.py           # load_single_skill(dir)→SkillEntry；256KB 上限；malformed skip+warn（镜像 memory_store.py:130-160）
├── skill_index.py           # 扫描 bundled + workspace 两源，按 name 去重合并（workspace 覆盖 bundled），localeCompare 排序
├── eligibility.py           # evaluate_requires(env/bin/config/os) + disable/user-invocable/always → eligibility + visibility
├── snapshot.py              # build_snapshot()：filter→visibility→budget 三级降级(full→compact→truncate)→渲染 + ⚠️ 警告
├── prompt.py                # render_skills_section(full/compact) 纯函数 + escape_xml；输出 ## Skills 段文本
├── commands.py              # resolve_skill_command(text)→(name,args)|None；sanitize_skill_command_name（lowercase/[a-z0-9_]）
├── status.py                # format_skill_status()→表格字符串（inspect/check 用）
├── registry.py              # SkillsRegistry：snapshot() 带 mtime 失效 + 单调版本号；prev.then 串行化
└── builtin/                 # 内置示例 skill（ship 即可用 + 可测）
    └── skill-creator/SKILL.md

agent_core/tools/
└── builtin.py               # 改：新增 READ_TOOL = ToolDef(name="Read", parameters={path}, handler=read_file_handler)；register_builtin_tools 注册

agent_core/turn_chain.py     # 改：新增 SkillsPromptHandler（name="skills_prompt"，after="system_prompt"）—— 读 agent.skills_registry.snapshot()，把 ## Skills 段并入 ctx.stage_inputs 的 system message（镜像 MemoryRetrievalHandler/_merge_memory_into_system @ 411-429）
agent_core/builder.py        # 改：import SkillsPromptHandler；build_default_inputs_chain 在 SystemPromptHandler 之后插入
agent_core/agent_core.py     # 改：ReactAgent.__init__ 构造 self.skills_registry = SkillsRegistry(...)

web/app.py                   # 改：run_agent() 顶部解析 "/skill-name <args>" → prompt rewrite（前缀 "Use the <name> skill..."）→ 正常 start_run+step（镜像 OpenClaw §5.3 prompt-rewrite 路径）

scripts/
└── skills_check.py          # 新：CLI 入口，print format_skill_status(registry) 输出（FR-022 用户调用面；非 UI、可单测；analyze U1）

tests/
├── test_skill_types.py              # 数据契约 + 纯校验函数
├── test_skill_config.py             # SkillsConfig from_env/from_dict/默认值/expanduser
├── test_skill_frontmatter.py        # 解析 + 契约执行（缺 desc/YAML 损坏/超限/未知字段）
├── test_skill_store.py              # loader + 相对路径 + size 边界
├── test_skill_bundled.py            # bundled skill-creator 加载（独立文件，避免与 US5 merge 测试并行冲突，analyze I1）
├── test_skill_index_merge.py        # bundled+workspace 优先级覆盖、同名去重、确定性排序（US5）
├── test_skill_eligibility.py        # requires env/bin/config/os、disable/user-invocable、always
├── test_skill_snapshot_budget.py    # full→compact→truncate + ⚠️ 警告 + 不静默丢
├── test_skill_prompt_render.py      # 字节稳定性、escape、full/compact 格式、visibility 过滤
├── test_skill_commands.py           # /skill-name 解析、sanitize、未知命令
├── test_skills_prompt_handler.py    # 镜像 test_l3_sm_extract_handler.py 范式 + Read-guard 断言
├── test_read_tool.py                # Read 工具读文件、超限、缺文件、permission 默认放行
├── test_skill_status.py             # format_skill_status + scripts/skills_check.py 调用
└── test_skill_registry_hotreload.py # mtime 失效 + 版本号单调 + mid-session 跨 turn 刷新
```

**Structure Decision**: 单项目（`agent_core/` 单体库）。skills 子系统作为 `agent_core/skills/` 新包，**严格镜像 `agent_core/memory/`** 的目录与约定（barrel `__init__.py` + `types.py` + pydantic `config.py` + 文件后端 store + index + handler 在 `turn_chain.py`）。三个既有文件（`turn_chain.py`/`builder.py`/`agent_core.py`）做最小侵入式改动；`web/app.py` 仅在 `run_agent` 加 slash 解析（UI 由用户手动验证）。`Read` 工具加在既有 `tools/builtin.py`，复用既有 permission/sandbox 子系统。

## 架构设计（clean-architecture-advisor + gof-architecture-advisor 评审）

> 本节由两个架构 skill 评审产出（2026-07-05 补跑）。评审对齐 Constitution Principle I（自研、不过度抽象）+ skill 反复强调的分寸感："3 个类的小项目不需要 5 个模式"。

### A. 分层架构（同心圆 + 依赖方向）

依赖铁律：**所有源码依赖指向内层（Entity）**，外层（框架/IO）依赖内层（领域），绝不反向。

```
┌────────────────────────────────────────────────────────────────┐
│ Frameworks & Drivers（最外层 — 框架/IO 细节）                   │
│   • turn_chain.py: SkillsPromptHandler（CoR 的 ConcreteHandler）│
│   • web/app.py: run_agent() slash 派发                          │
│   • config.py: SkillsConfig.from_env() 读 os.environ            │
│   • path_validator.py: stat / symlink 校验                      │
├────────────────────────────────────────────────────────────────┤
│ Interface Adapters（适配层 — IO 转 domain）                     │
│   • registry.py: SkillsRegistry（Facade + cache-aside）         │
│   • skill_store.py: 文件 → SkillEntry                           │
│   • skill_index.py: 目录扫描 + 多源合并                          │
├────────────────────────────────────────────────────────────────┤
│ Use Cases（用例层 — 应用规则，纯函数）                           │
│   • snapshot.py: build_snapshot 流水线                           │
│   • eligibility.py: 资格求值（requires 五维）                    │
│   • commands.py: slash 解析                                      │
│   • status.py: 诊断格式化                                        │
├────────────────────────────────────────────────────────────────┤
│ Entities（实体层 — 核心契约 + 纯渲染，零 IO）                    │
│   • types.py: Skill/SkillEntry/SkillSnapshot/SkillFrontmatter   │
│   • frontmatter.py: SKILL.md → dataclass                        │
│   • prompt.py: render_skills_section(full/compact) + escape     │
└────────────────────────────────────────────────────────────────┘
```

**依赖方向验证（ADP 无环）**：
- Entity（types/frontmatter/prompt）**零 import 外层**——不 import os/yaml/streamlit/turn_chain ✓
- UseCase（snapshot/eligibility/commands/status）**只 import Entity** ✓
- Adapter（registry/store/index）import UseCase + Entity ✓
- Framework（handler/web）import Adapter ✓
- 跨子系统：skills → memory（单向，复用 `parse_frontmatter`）；turn_chain → skills（单向，注册 handler）；**无环** ✓

### B. 模块职责表（按层次）

| 模块 | 层 | 职责 | 变化原因 | 依赖 → |
|---|---|---|---|---|
| types.py | Entity | 数据契约 + 纯校验 | schema 演进 | （无） |
| frontmatter.py | Entity | SKILL.md → SkillEntry 解析 | frontmatter 格式变 | types |
| prompt.py | Entity | 渲染 available_skills 段 | prompt 模板变 | types |
| snapshot.py | UseCase | filter→visibility→budget→render | 流水线步骤变 | types, eligibility, prompt |
| eligibility.py | UseCase | requires 五维求值 | 资格规则变 | types |
| commands.py | UseCase | /skill-name 解析 | slash 语法变 | types |
| status.py | UseCase | 诊断格式化 | 显示需求变 | types |
| skill_store.py | Adapter | 单 skill 目录 → SkillEntry | 文件格式变 | types, frontmatter, path_validator |
| skill_index.py | Adapter | 多源扫描 + 合并 | 来源数变 | types, skill_store |
| registry.py | Adapter | Facade: snapshot() + 缓存 | 缓存策略变 | skill_index, snapshot, config |
| SkillsPromptHandler | Framework | CoR handler 注入 stage_inputs | 注入逻辑变 | registry, types |
| config.py | Framework | pydantic 配置 + env 加载 | 配置项变 | (pydantic) |
| path_validator.py | Framework | 路径解析 + 安全 | 路径策略变 | (stdlib) |

### C. 设计模式选型

**采纳**（语义上承认，不引入框架/抽象基类）：

| 模式 | 用在哪 | 理由 |
|---|---|---|
| **Facade** | `SkillsRegistry` | 把 加载+合并+缓存+失效 简化为一个 `snapshot()` 接口；handler 只依赖此接口，不碰内部 |
| **Cache-Aside** | `SkillsRegistry.snapshot()` | mtime 失效惰性缓存：hit→return / miss→rebuild+version bump |
| **Value Object**（DDD，非 GoF） | `Skill`/`SkillEntry`/`SkillSummary` 用 `@dataclass(frozen=True)` | 无身份、不可变、按值比较、可哈希——支撑 INV-2 字节稳定性测试用 `hash()` 断言 |
| **CoR 参与者** | `SkillsPromptHandler` | 复用既有 `turn_chain` 的 Handler Protocol + HandlerResult（镜像 `MemoryRetrievalHandler`） |

**显式拒绝**（避免过度设计；每条理由须写入代码注释，防"后人误以为是待重构"）：

| 候选模式 | 拒绝理由 |
|---|---|
| Strategy（预算三级降级） | tier 顺序固定（full→compact→truncate）+ 数量固定（3）+ 判定是字符串长度比较——if-else 比 3 个策略类更直接可读 |
| Chain of Responsibility（降级） | CoR 是"任一处理器消费请求"；降级是"有序试装"——语义不符 |
| Template Method（snapshot 流水线） | v1 只一条固定流水线，无需子类定制步骤；一个函数 + 局部变量顺序推进足够 |
| Composite（多源合并） | 不需递归部分-整体；用 **`list[(source, dir, priority)]` 配置数据** 封装"未来加源"，加源 = 加一行配置，不动合并纯函数 |
| Builder（SkillSnapshot） | 表示固定（非"同过程产多表示"）；dataclass + 工厂函数足够 |
| Singleton（SkillsRegistry） | 每 agent 一实例（非全局）；生命周期由 `ReactAgent.__init__` 管理即可 |

**核心策略：配置数据 > 对象模式**。源合并的变化点（v1 两级，未来可能 6 级）用配置 list 封装，而非 Strategy/Composite——加源只改配置，不动 `merge_by_priority(per_source_entries)` 纯函数。

### D. 边界与依赖注入

**组合根（Composition Root）**：`agent_core.py: ReactAgent.__init__` + `builder.py: build_default_inputs_chain`
- `__init__` 构造 `self.skills_registry = SkillsRegistry(skills_config)`
- `build_default_inputs_chain` 把 `SkillsPromptHandler(agent)` 插入链（agent 持有 registry）
- handler 经 `agent.skills_registry.snapshot()` 访问——镜像 `MemoryRetrievalHandler` 经 `agent.session_memory` 访问的既有模式

**跨边界数据**（穿越边界的是 Entity 层简单结构，**非**框架对象）：
- handler ↔ registry：`SkillSnapshot`（types.py 定义）
- registry ↔ snapshot：`list[SkillEntry]` + env dict + `SkillsConfig`
- snapshot ↔ prompt：`list[SkillEntry]`（visible 子集）+ mode
- web ↔ commands：`(skill_name, args)` 元组
- **无任何边界传递 turn_chain / streamlit / llm sdk 对象** ✓

**测试隔离**（按层次递增的 fixture 成本）：
- Entity / UseCase 纯函数 → 直接断言，零 fixture
- Adapter（store/index/registry）→ pytest `tmp_path` 建临时 skill 目录（镜像 `tests/conftest.py:memory_root` 既有模式）
- Framework（handler）→ `MagicMock(spec=TurnContext)` + 真 `RunState`（镜像 `tests/test_l3_sm_extract_handler.py:121-160`）

### E. 架构权衡与风险（clean-arch 评审发现，须实现期遵守）

| 权衡点 | 现状决策 | 理由 / 风险 |
|---|---|---|
| `config.from_env()` 混合 IO 与实体 | 保留（pydantic classmethod） | memory 既有约定（`memory/config.py:291`）；一致性 > 教条纯度 |
| `SkillsRegistry` 聚合 5 件事 | 保留单类 + 私有方法拆分 | CCP 共同闭包（缓存/失效/版本都为 `snapshot()`）；拆多类反散；`_scan_if_stale/_build/_serialize` 私有方法 |
| `SkillsPromptHandler` 放 `turn_chain.py` | 遵循项目约定 | `MemoryRetrievalHandler` 也如此；handler 体量小不显著膨胀；外→内依赖方向正确 |
| `parse_frontmatter` 跨子系统 import | v1 跨 import，标记技术债 | research Decision 5；不下沉共享模块避免扩范围；v2 可重构为 `agent_core/frontmatter.py` |
| 过度设计风险 | 严守 YAGNI | 实现期不预先抽接口（如 `ISkillSource`）；v1 scope 不需要的全推迟 |
| Read 工具不可用时跳过 skills 段 | handler guard（analyze C2） | `SkillsPromptHandler` 检测 `agent.tools` 不含 "Read" → 跳过 `## Skills` 段（spec Edge Case）；防"模型看到目录却读不到 SKILL.md"的破损 UX |
| 事件可观测性（FR-023） | `logging` emit（analyze C1） | load 失败 / eligibility 排除 / budget 降级 三类事件 MUST 经 `logging` 输出；不只是写 prompt 警告或 load_error 字段 |
| Read 工具 permission 默认策略 | 实现期验证（analyze C3） | safety_check 已白名单 "Read"，但默认 allow/deny 须实测；deny 则补 allow 规则 |
| FR-022 用户调用面 | `scripts/skills_check.py` CLI（analyze U1） | 仅 `format_skill_status()` 函数不满足 spec "一种查看方式"；交付非 UI 的可测 CLI 入口 |

**实现期最易违反依赖规则的三处**（须 code review 把关）：
1. `eligibility.py` 求 `requires.config` 时若**直接读全局 Config** → 破坏纯函数；必须**参数注入 config dict**。
2. `snapshot.py` 若为"方便"**直接 import skill_store 读盘** → 破坏分层；必须**接收 entries 参数**。
3. `prompt.py` 渲染若**依赖外部状态**（时间戳/随机/order by insertion）→ 破坏 INV-2 字节稳定；必须**纯函数 + 名字字典序**。

---

## Complexity Tracking

> 无 Constitution Check 违例 → 本表为空。

| Violation | Why Needed | Simpler Alternative Rejected Because |
|-----------|------------|-------------------------------------|
| — | — | — |

## Constitution Check — Post-Design Re-check (Phase 1 后)

*GATE: Phase 1 设计落地后再次核对。*

| 原则 | 复核结论 | 设计后证据 |
|---|---|---|
| **I. 自研优先** | ✅ 仍 PASS | 全部新代码落 `agent_core/skills/`（data-model §1-9）；无新依赖（pyyaml/pydantic 既有）；`Read` 工具自研于既有 `tools/builtin.py`；hot-reload 用 stdlib mtime，不引 watchdog。 |
| **II. 数据驱动** | ✅ 仍 PASS | budget 默认值引自 OpenClaw 源码（research Decision 8）；**实测门**写入 quickstart §3（字节稳定 hash 对比）+ §5（prompt 长度断言）+ §1（input_tokens 不增量）。 |
| **III. 文档即硬约束** | ✅ 仍 PASS | data-model / contract / quickstart 与 spec FR-001..FR-024+FR-021a 全对齐；contracts/skill-md-format.md 是 spec FR-002/003/005 的可执行契约。**完成门**：实现后 MUST 同步 `docs/agent_core-skill-system-design.md`。 |
| **IV. 测试纪律** | ✅ 仍 PASS | data-model §11 列 6 条关键不变量（INV-1..INV-6），每条映射到具体测试文件；fallback 路径（malformed/缺 desc/超限/空目录）在 quickstart §7 覆盖；UI（slash）标注手动验证（quickstart §6）。 |
| **V. 不偷偷缩范围** | ✅ 仍 PASS | 用户确认的"完整核心"10 子系统在 Project Structure 全部落地（registry/snapshot/index/eligibility/prompt/commands/status/config/store/frontmatter + handler + Read 工具 + slash）；发现的 Read 缺口透明记录（research Decision 1）；watchdog 改 mtime 是 documented informed default 非 silent。 |
| **VI. 防御式务实工程** | ✅ 仍 PASS | data-model §6 状态机 + §8 流水线显式处理 ERRORED/DISABLED/MISSING 三类失败；INV-3/INV-4 钉死容错与预算不静默丢；snapshot.py 串行化（prev.then）防并发。 |

**Post-design 结论**：无新违例。Phase 1 设计放行，可进入 `/speckit-tasks`。

---

## Phase 0 / Phase 1 产物

- [Phase 0 — research.md](research.md)：10 项技术决策 + 备选（Read 工具 / 注入策略 / 热重载 / 配置 / frontmatter 复用 / slash / eligibility / budget / inspect / bundled 示例）。
- [Phase 1 — data-model.md](data-model.md)：Skill / SkillEntry / SkillSnapshot / SkillStatus 等实体与状态机。
- [Phase 1 — contracts/skill-md-format.md](contracts/skill-md-format.md)：SKILL.md frontmatter 契约（skill 作者面）。
- [Phase 1 — quickstart.md](quickstart.md)：端到端验证脚本（含 Constitution II 的实测门）。
