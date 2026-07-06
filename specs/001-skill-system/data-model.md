# Phase 1 Data Model: Skill System (agent_core)

**Date**: 2026-07-05
**Source**: spec.md FR-001..FR-024 + FR-021a；research.md 10 项决策。

> 本文件定义 skills 子系统的核心实体、字段、关系、校验规则与状态转换。
> 实现细节（具体类名/库）由 tasks.md / 实现阶段定，这里只到"数据契约"层。

---

## 1. 实体总览

```
SkillSource(枚举)          ──┐
                             ├──> Skill(原始) ──frontmatter──> SkillEntry(运行时)
SkillFrontmatter(TypedDict) ─┘                                    │
                                                                  │  evaluate
                                                                  ▼
                                                           SkillEligibility
                                                                  │
                                                                  ▼  filter+visibility+budget
                                                           SkillSnapshot
                                                            ├─ prompt: str        (注入 system prompt 的 ## Skills 段)
                                                            ├─ skills: [...]      (摘要)
                                                            └─ version: int       (单调递增)

SkillStatus(诊断)  ←── format_skill_status(SkillSnapshot + 拒绝/排除项)
```

---

## 2. SkillSource（来源枚举）

v1 仅两级（Q3 确认）：

| 值 | 优先级 | 解析目录 | 说明 |
|---|---|---|---|
| `BUNDLED` | 低（1） | 包内 `agent_core/skills/builtin/` | 随库 shipped 的内置 skill |
| `WORKSPACE` | 高（2） | 项目 `skills/`，缺省回退 `~/.agent_data/skills/`，env `SKILLS_PATHS__WORKSPACE_DIR` 覆盖 | 用户/项目级 skill，**覆盖同名 bundled** |

**校验**: 同名 skill 由高优先级覆盖低优先级；合并后**按 `name` 字典序确定性排序**（不按 source 排序）——保证 prompt 字节稳定（SC-002）。

---

## 3. SkillFrontmatter（frontmatter TypedDict）

skill 作者在 `SKILL.md` 顶部 `---` 块中声明。**对齐 OpenClaw §2.5 字段集**（v1 子集）：

| 字段 | 必填 | 默认 | 类型 / 语义 |
|---|---|---|---|
| `name` | ✅ | 回退目录名 | `str`；skill 唯一名；prompt `<name>` |
| `description` | ✅ | — | `str`；模型据此判断触发；prompt `<description>` |
| `homepage` | ❌ | — | `str` URL；文档主页 |
| `disable-model-invocation` | ❌ | `false` | `bool`；true → 不进模型可见目录（仍登记） |
| `user-invocable` | ❌ | `true` | `bool`；false → 不作为 slash 命令暴露 |
| `metadata` | ❌ | — | `dict`；含 `requires`/`always`/`os`（见 §4） |

**校验规则**（`skills/frontmatter.py`）：
- `description` 缺失 → **拒绝加载该 skill** + 告警（FR-003）。
- `name` 缺失 → 回退到目录名（FR-003）。
- YAML 损坏 → 优雅跳过，不崩 agent（FR-005）。
- 未知字段 → 前向兼容忽略（Edge case）。

---

## 4. SkillMetadata（metadata 子对象）

| 字段 | 类型 | 语义 |
|---|---|---|
| `always` | `bool?` | true → 绕过 requires 校验，恒 eligible |
| `os` | `list[str]?` | OS 白名单，如 `["darwin"]`；当前 `platform.system().lower()` 不在则 ineligible |
| `requires.bins` | `list[str]?` | 必须全部 `shutil.which` 命中的二进制 |
| `requires.anyBins` | `list[str]?` | 任一命中即可 |
| `requires.env` | `list[str]?` | 必须全部在 `os.environ` 的环境变量 |
| `requires.config` | `list[str]?` | 必须为 truthy 的配置路径（点号分隔） |

> v1 **不纳入** `primaryEnv`/`install`/`apiKey`（env 注入子系统 out of scope，Q1 确认）。

---

## 5. SkillEntry（运行时记录）

```
SkillEntry:
  skill: Skill                      # name / description / file_path / base_dir / source
  frontmatter: SkillFrontmatter     # 原始解析结果
  metadata: SkillMetadata | None
  source: SkillSource               # BUNDLED | WORKSPACE
  eligibility: SkillEligibility     # 见 §6
  visibility: SkillVisibility       # MODEL_VISIBLE | HIDDEN_FROM_MODEL
  user_invocable: bool              # 是否可 /skill-name 调用
  load_error: str | None            # 加载失败原因（缺 desc / 超限 / YAML 损坏）
```

**SkillVisibility 推导**（镜像 OpenClaw `isSkillVisibleInAvailableSkillsPrompt`）：
- `disable-model-invocation: true` → `HIDDEN_FROM_MODEL`（不进 `<available_skills>`，但仍登记可 `/invoke`）。
- 否则 `MODEL_VISIBLE`。

---

## 6. SkillEligibility（资格状态机，即时计算）

每次 `snapshot()` 基于当前环境求值，**无持久化**（FR-014）。

```
                  ┌──────────────────────────┐
                  │ entry 加载成功？           │
                  └────────────┬─────────────┘
                      no       │           yes
            load_error 设置     │
            status=ERRORED      │
                               ▼
                  ┌──────────────────────────┐
                  │ config 显式 enabled=false?│ ── yes ──> DISABLED
                  └────────────┬─────────────┘
                              no│
                               ▼
                  ┌──────────────────────────┐
                  │ metadata.always == true?  │ ── yes ──> ELIGIBLE（绕过）
                  └────────────┬─────────────┘
                              no│
                               ▼
                  ┌──────────────────────────┐
                  │ evaluate_requires(...)    │
                  │  bins/anyBins/env/config/os │
                  └────────────┬─────────────┘
                        全满足 │           有缺
                              ▼               ▼
                          ELIGIBLE      MISSING_REQUIREMENTS
```

`eligible = status == ELIGIBLE`；`model_visible = eligible AND visibility == MODEL_VISIBLE`。

---

## 7. SkillSnapshot（快照，注入 handler 消费）

```
SkillSnapshot:
  prompt: str                # 已渲染的 ## Skills 段（含 ⚠️ 警告，若有）
  skills: list[SkillSummary] # [{name, source, eligible, missing:[...]}] 供 status/调试
  skill_filter: list[str]?   # 本次构建用的 agent filter（v1 不用，预留）
  version: int               # 单调递增；mtime 变化或环境变化时 bump
  render_mode: "full"|"compact"|"truncate"   # 本次降级到哪一级
  truncated_count: int       # truncate 模式下被截掉的 skill 数（0 表示未截）
```

**不变量**:
- `prompt` 对相同 `(eligible_entries, env, config)` **字节确定**（纯函数渲染 + 名字字典序）。
- `version` 单调非递减（`max(now, prev+1)`）。
- `prompt` 为空（零 eligible skill）→ handler 不向 system message 注入任何内容（Edge case：空目录）。

---

## 8. SkillSnapshot 构建流水线（snapshot.py）

```
load_all()
  │ 扫描 bundled + workspace 两源
  │ 每源：list_child_dirs → load_single_skill(dir) → SkillEntry（含 load_error）
  ▼
merge_by_priority()
  │ Map<name, entry>，WORKSPACE 覆盖 BUNDLED
  ▼
evaluate()
  │ 对每个 entry 跑 §6 状态机
  ▼
filter_visible()
  │ eligible AND model_visible → prompt_entries
  ▼
sort_by_name()           # localeCompare 风格字典序
  ▼
apply_budget_limits()    # §research Decision 8：full→compact→truncate
  ▼
render(prompt_entries, mode)
  │ render_full  → <available_skills><skill>name/desc/location...
  │ render_compact → name/location only
  ▼
prepend_warning_if_degraded()
  ▼
SkillSnapshot
```

---

## 9. SkillsConfig（pydantic 配置）

```
SkillsConfig:
  paths:
    bundled_dir: Path       # 缺省包内 agent_core/skills/builtin
    workspace_dir: Path     # 缺省 ./skills，回退 ~/.agent_data/skills
  limits:
    max_skill_file_bytes: int = 256_000        # 256 KB
    max_candidates_per_root: int = 300
    max_skills_in_prompt: int = 150
    max_skills_prompt_chars: int = 18_000
  load:
    enabled: bool = true                       # 全局开关
```

loader：`SkillsConfig.from_env(prefix="SKILLS_")`（双下划线表嵌套）。
**不改全局 `agent_core.config.Config`**（research Decision 4）。

---

## 10. 状态转换总结

| 实体 | 状态来源 | 持久化？ |
|---|---|---|
| SkillEntry.load_error | 加载时一次性 | 否 |
| SkillEligibility | snapshot 即时求值 | 否 |
| SkillVisibility | frontmatter 推导 | 否 |
| SkillSnapshot.version | mtime/env 变化 bump | 否（内存） |
| SkillsRegistry cache | snapshot() 命中/失效 | 否（内存） |

**全部运行时状态在内存、即时计算**——符合 Principle II + spec FR-014"no persistent metadata caches"。

---

## 11. 关键不变量（测试必验）

- INV-1（去重）：同名 skill 跨源只保留最高优先级一份（test_skill_index_merge）。
- INV-2（排序）：相同 entry 集合 → 相同 `prompt` 字节（test_skill_prompt_render，多次运行 hash 一致）。
- INV-3（容错）：1 个 malformed SKILL.md 不影响同源其他 skill 加载（test_skill_store）。
- INV-4（预算）：eligible > 上限时 prompt 长度 ≤ `max_skills_prompt_chars` 且 `truncated_count > 0` 时 prompt 以 `⚠️` 开头（test_skill_snapshot_budget）。
- INV-5（eligibility 即时）：设 `os.environ[必需变量]` 前后两次 snapshot，skill 从 MISSING → ELIGIBLE（test_skill_eligibility）。
- INV-6（版本单调）：连续两次 snapshot（无磁盘变化）version 相等；改一个 SKILL.md mtime 后 version 严格增大（test_skill_registry_hotreload）。
