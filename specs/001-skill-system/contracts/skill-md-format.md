# Contract: SKILL.md Format (v1)

**Audience**: skill 作者（开发者 / 高级用户）。
**Status**: v1 契约，对齐 `docs/skill/openclaw-skill-system.md` §2.5（字段子集）。

> 这是 skills 子系统面向作者的**唯一对外契约**。loader（`skills/frontmatter.py` + `skill_store.py`）
> 按此契约解析；任何 `MUST` 违反会导致该 skill 被拒绝加载（其他 skill 不受影响）。

---

## 1. 文件布局

```
my-skill/                     # 目录名任意（name 缺省时回退到此）
├── SKILL.md                  # MUST 存在（文件名大小写：SKILL.md）
├── scripts/                  # 可选；skill 内脚本
├── references/               # 可选；参考资料
└── assets/                   # 可选；静态资产
```

约束：
- `SKILL.md` **MUST** 是普通文件（v1 不支持 symlink；防逃逸，镜像 OpenClaw `local-loader`）。
- 单文件 **MUST** ≤ 256 KB（`SkillsConfig.limits.max_skill_file_bytes`），超限拒绝。

## 2. SKILL.md 结构

```markdown
---
name: my-skill
description: "One-line: when to trigger this skill (model reads this)."
homepage: https://example.com/my-skill-docs      # 可选
disable-model-invocation: false                  # 可选，默认 false
user-invocable: true                             # 可选，默认 true
metadata:                                        # 可选
  always: false
  os: ["darwin", "linux"]
  requires:
    bins: ["curl"]
    anyBins: []
    env: ["MY_API_KEY"]
    config: []
---

（markdown body — 模型仅在触发后通过 Read 工具按 location 读取）
```

## 3. Frontmatter 字段契约

| 字段 | 必填 | 默认 | 类型 | 违反后果 |
|---|---|---|---|---|
| `name` | 否（推荐填） | 目录名 | 非空 `str` | 缺则回退目录名 |
| `description` | **MUST** | — | `str` | **拒绝加载**该 skill + 告警 |
| `homepage` | 否 | — | URL `str` | 忽略非法值 |
| `disable-model-invocation` | 否 | `false` | `bool` | 非法值 → 按 `false` |
| `user-invocable` | 否 | `true` | `bool` | 非法值 → 按 `true` |
| `metadata` | 否 | `{}` | `dict` | 见 §4 |

## 4. metadata 子结构

```yaml
metadata:
  always: false              # bool；true → 绕过 requires，恒 eligible
  os: ["darwin"]             # list[str]；OS 白名单（platform.system().lower()）
  requires:
    bins: ["curl"]           # list[str]；shutil.which 全部命中
    anyBins: ["rg", "grep"]  # list[str]；任一命中
    env: ["MY_API_KEY"]      # list[str]；os.environ 必含
    config: ["channels.x"]   # list[str]；配置 path truthy（点号分隔）
```

> v1 **不**支持 `primaryEnv` / `install` / `apiKey` / `command-dispatch`（out of scope，Q1 确认）。
> 出现这些字段 → 前向兼容**静默忽略**。

## 5. 行为契约（loader 保证）

1. YAML 损坏 / 解析异常 → 该 skill 优雅跳过，**不影响其他 skill**，agent 不崩溃。
2. body 中的相对路径（如 `references/cheatsheet.md`）→ 解析为相对**该 skill 目录**的绝对路径（模型用 Read 工具读时拿到的是 skill base_dir 拼接后的绝对路径）。
3. 同名 skill 跨源（bundled vs workspace）→ **workspace 覆盖 bundled**；同源内同名 → 先到先得 + 告警。
4. `disable-model-invocation: true` → 不进模型可见目录，但若 `user-invocable: true` 仍可 `/my-skill` 调用。

## 6. 模型可见的目录表格式（prompt 段，作者需了解）

loader 把每个 `MODEL_VISIBLE` skill 渲染为（full 模式）：

```
## Skills (mandatory)

Before replying: scan <available_skills> <description> entries.
When the task matches a skill, use the Read tool to load its SKILL.md at <location>.
Read at most one skill up front; read none when no match.
Use the exact location from <available_skills>; never guess or hardcode paths.

<available_skills>
  <skill>
    <name>my-skill</name>
    <description>One-line: when to trigger...</description>
    <location>/abs/path/to/my-skill/SKILL.md</location>
  </skill>
</available_skills>
```

compact 模式省略 `<description>`；truncate 模式前置 `⚠️ Skills truncated: ...` 警告。
作者据此写**清晰、具体、与任务关键词相关**的 description——它是模型唯一的触发依据。

## 7. 最小可工作示例（bundled `skill-creator`）

```markdown
---
name: skill-creator
description: "Use when creating or editing a SKILL.md for the agent_core skill system."
metadata:
  os: ["darwin", "linux"]
---

# Skill Creator

When asked to create a new skill:
1. Confirm the skill's purpose and trigger keywords.
2. Create a directory under the workspace skills/ folder.
3. Write SKILL.md with required frontmatter (name + description).
4. Keep description specific; it's the model's only trigger signal.
```

## 8. 版本与兼容

- `CURRENT_SCHEMA_VERSION = 1`（v1）。
- 未来增字段 → minor bump，旧 skill 仍可加载（前向兼容）。
- 删/改字段语义 → major bump，loader 按 version 路由解析。
