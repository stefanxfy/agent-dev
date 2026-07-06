# agent_core Skill 系统 — 架构与代码设计深讲

**读者**: 准备阅读或修改 `agent_core/skills/` 下代码的开发者
**前置**: 了解 [agent_state_machine 设计](../agent-state-machine-and-chain-of-responsibility-design.md)
(turn_chain / handler 机制),了解 pydantic v2 / dataclass 基础
**互补文档**: [agent_core-skill-system-design.md](../agent_core-skill-system-design.md)
(规格镜像 + 偏差记录;本文不重复 spec 引用,聚焦"为什么这么写")

---

## 1. 系统目标(再陈述)

把"领域专家指令"从代码里解耦到一个**外部可挂载的目录**:
- 作者: 写一份 `SKILL.md` (YAML frontmatter + markdown body) → 放对位置 → 完事
- agent: 每轮拿到一段"当前可用 skill 目录表",LLM 自取所需
- 用户: `/skill-name <args>` 显式触发,绕过 LLM 自动选择

三个使用面共享同一份 `SkillEntry` 数据,**只读不写** —— agent 是 skill 的消费者,
不是作者。这决定了整套数据流是"配置驱动 + 纯函数"风格:没有 mutation,
没有订阅/通知,没有运行时注册。

## 2. 架构全景(一张图读懂)

```
┌─────────────────────────────────────────────────────────────────────┐
│ Framework / Driver                                                   │
│   turn_chain.py:SkillsPromptHandler   ← 把 snapshot 拼到 system msg  │
│   web/app.py + web/pages/00_Chat.py   ← /skill-name slash 派发     │
│   scripts/skills_check.py             ← 诊断 CLI (FR-022)          │
│   config.py                           ← pydantic SkillsConfig       │
└─────────────────────────────────────────────────────────────────────┘
                              │ 依赖
                              ▼
┌─────────────────────────────────────────────────────────────────────┐
│ Interface Adapter (Facade + IO)                                      │
│   registry.py:SkillsRegistry          ← 唯一对外入口 (snapshot/entry)│
│   skill_store.py:load_single_skill    ← 单 skill 目录 → SkillEntry │
│   skill_index.py:scan_source + merge  ← 多源扫描 + 优先级合并     │
│   path_validator.py                   ← symlink 逃逸防护            │
└─────────────────────────────────────────────────────────────────────┘
                              │ 依赖
                              ▼
┌─────────────────────────────────────────────────────────────────────┐
│ Use Case (纯函数 + 业务规则)                                         │
│   snapshot.py:build_snapshot          ← 过滤 + 排序 + 预算 + render │
│   eligibility.py:evaluate_eligibility ← 5 维 requires + always    │
│   commands.py:resolve_skill_command   ← slash 解析 + sanitize      │
│   status.py:format_skill_status       ← 诊断表格                   │
└─────────────────────────────────────────────────────────────────────┘
                              │ 依赖
                              ▼
┌─────────────────────────────────────────────────────────────────────┐
│ Entity (零 IO、零外部 import)                                        │
│   types.py        ← Skill/SkillEntry/SkillSnapshot + 枚举/数据契约 │
│   frontmatter.py  ← SKILL.md → dataclass (复用 memory 的 parser)    │
│   prompt.py       ← render_skills_section(full/compact) + escape   │
└─────────────────────────────────────────────────────────────────────┘
```

**依赖铁律**: Entity 不 import 外层;UseCase 只 import Entity;Adapter import
UseCase+Entity;Framework import Adapter。**无环**。任何"我想在 types.py 里读
文件"的念头都是错的 —— 文件 IO 属于 Adapter 层。

这一约束的回报:Entity 层的所有 dataclass 都能在不启动 IO 的情况下实例化,
所以测试可以纯构造数据,毫秒级跑完(看 `tests/test_skill_types.py` /
`test_skill_frontmatter.py`,纯逻辑用例 < 10ms)。

## 3. 数据流(snapshot 是怎么构建出来的)

```
SkillsRegistry.snapshot()                       ← 唯一外部入口
   │
   │  sig = (bundled_mtime, workspace_mtime, hash(sorted(os.environ)))
   │  cache hit → 直接 return SkillSnapshot
   │  cache miss ↓
   │
   ├─► load_all(config)
   │     │
   │     │  config.sources_list() 升序
   │     │  每源 scan_source(目录) → list[SkillEntry]  (含 broken/load_error)
   │     │  merge_by_priority(per_source_entries) → 跨源高优先级覆盖同名
   │     │
   │     ▼
   │   list[SkillEntry]  (字典序)
   │
   └─► build_snapshot(entries, env, config, limits)
         │
         │  1. _filter_eligible_visible: evaluate_eligibility × visibility
         │     → (prompt_entries, all_evaluated)
         │  2. sort(by name)  字节稳定关键步骤
         │  3. _apply_budget_limits: full → compact → truncate + ⚠️
         │  4. render_skills_section: 序列化 → xml 字符串
         │
         ▼
       SkillSnapshot {
         prompt: str,                  ← ## Skills 段(直接拼到 system msg)
         skills: list[SkillSummary],   ← 给 UI/调试用
         version: int,                 ← INV-6 单调
         render_mode: Literal[...]     ← full / compact / truncated
         truncated_count: int,
       }
```

**消费侧**(`turn_chain.py:SkillsPromptHandler`):
- 读 `agent.skills_registry.snapshot()` → 拿 prompt
- 检查 `agent.tools` 是否含 "Read" → 没有就**整段丢弃**(spec edge case:
  模型没读文件能力,提示它 skill 也没用)
- 把 prompt 段并入 system message

## 4. 关键组件深讲(读源码前的导读)

### 4.1 `SkillsRegistry` — 整个子系统的 Facade + Cache-Aside

文件: `agent_core/skills/registry.py`

设计要点:
- **单一入口**: 调用方只能 `registry.snapshot()` 和 `registry.get_entry(name)`,
  不能直接传 `SkillsConfig` 自己造。保证了缓存一致性。
- **Cache-Aside 失效**: `sig` 包含三要素 —— bundled_dir mtime, workspace_dir mtime,
  `hash(sorted(os.environ))`。任何一个变 → 缓存失效 → 重建 → version 递增。
  这是 **INV-5 (eligibility 即时)** 和 **INV-6 (version 单调)** 的实现。
- **env-hash 触发 hot-reload**: 用户说 `requires.env: [FEATURE_X]` 然后 `export
  FEATURE_X=1`,下一次 `snapshot()` 自动重新判定 eligibility,无需重启。
  代价:每次算 env 哈希(数十项,亚毫秒),生产可接受。
- **`get_entry` 也走缓存**: 维护 `_last_entries_by_name` dict,`get_entry` 复用
  上次 snapshot 的 entries,避免重复遍历。对 slash 派发高频调用友好。

```python
# 伪代码,真实实现见 registry.py
class SkillsRegistry:
    def __init__(self, config: SkillsConfig):
        self._config = config
        self._cache_snapshot: SkillSnapshot | None = None
        self._cache_sig: tuple = ()
        self._last_entries_by_name: dict[str, SkillEntry] = {}

    def snapshot(self) -> SkillSnapshot:
        sig = self._compute_sig()
        if self._cache_sig == sig and self._cache_snapshot is not None:
            return self._cache_snapshot
        entries = load_all(self._config)
        snap = build_snapshot(entries, dict(os.environ), self._config, ...)
        self._cache_sig = sig
        self._cache_snapshot = snap
        self._last_entries_by_name = {e.skill.name: e for e in entries}
        return snap
```

**读代码时该关注的**: `_compute_sig` 包含什么?为什么 env-hash 是 sorted tuple?
`build_snapshot` 的入参是 `dict(env)`,`evaluate_eligibility` 拿到的就是稳定 dict。
(详细 spec 见 data-model.md §11 不变量。)

### 4.2 `build_snapshot` — 过滤 + 排序 + 预算 + 渲染的串珠

文件: `agent_core/skills/snapshot.py`

四步流水线,每一步都是**纯函数**(无副作用,可独立测试):

| 步骤 | 行为 | 不变量 / 备注 |
|---|---|---|
| 1. filter | `evaluate_eligibility × visibility` | 产生 `(prompt_entries, all_evaluated)`;all_evaluated 给 status/调试用 |
| 2. sort | `sorted(by skill.name)` | **INV-2 字节稳定** 的关键 —— 字典序,跨进程/跨调用结果相同 |
| 3. budget | 三级降级: full → compact → truncate+⚠️ | **INV-4 不静默丢** —— 必须有警告,作者能看到 |
| 4. render | `render_skills_section` | 输出 `## Skills` markdown 段 |

**预算降级**(`_apply_budget_limits`):
- `max_skills_in_prompt = 150` → 超过则只保留**前 N 个**,追加 `⚠️ 还有 M 个 skill 未列出`
- `max_skills_prompt_chars = 18000` → 仍超则降级到 `compact` 模式(只留 name + description)
- 仍超 → 标 `render_mode="truncated"`,`truncated_count` 显式记录被丢弃数量

设计动机:不静默丢。**作者要能看到"我写的 200 个 skill 里有 50 个没进 prompt"**,
否则会出现"我加了 skill,LLM 还是不会用"的鬼故事。

### 4.3 `evaluate_eligibility` — 5 维 requires 的纯函数

文件: `agent_core/skills/eligibility.py`

签名: `evaluate_eligibility(requires, env, bins, config, os_name) -> (state, missing)`

```python
# 5 维,全部独立判定,AND 关系
@dataclass(frozen=True)
class SkillRequires:
    bins:   tuple[str, ...] = ()     # 工具列表: ["Read", "Bash"]
    anyBins: tuple[str, ...] = ()    # 任一即可
    env:    tuple[str, ...] = ()     # 必需 env var 存在
    config: tuple[str, ...] = ()     # 必需 config key 存在且 truthy
    os:     tuple[str, ...] = ()     # 必需 OS(linux/darwin/windows)
```

**关键设计**: 全部**参数注入**,函数不读 `os.environ` / `sys.platform` / `agent.config`。
调用方(`build_snapshot`)注入当前环境。

为什么这样写:
1. **可测**: 单元测试可以构造任意 `env={"FEATURE_X": "1"}` 验证 truthy 路径,无需 monkey-patch
2. **可重放**: 同一份 env+config 多次调用结果一致 → 缓存友好
3. **可分离关注点**: eligibility 是"skill 自己的元数据 + 当前环境" 的纯函数判断,跟 registry 怎么缓存、prompt 怎么渲染解耦

**always bypass**: 任何 `requires` 字段为空 → 该维度算满足。一个全空的 `SkillRequires` = 永远 eligible。

**v1 局限**: `requires.config` 在生产 v1 视为 unmet —— 因为 `evaluate_eligibility` 的 `config` 参数
registry 传 `{}`(agent 全局 Config 未接)。snapshot 端测试可注入真实 config dict 验证 truthy 路径。
**接 agent Config 留待 v2**(已在 design doc §6.4 透明记录)。

### 4.4 `render_skills_section` — 字节稳定的字符串构造

文件: `agent_core/skills/prompt.py`

**为什么字节稳定很重要**: LLM 的 prompt cache(key 通常是 hash(完整 system message))
对**字符级一致性**敏感。如果两次 `snapshot()` 输出的 prompt 字符不同(比如
skill 列表顺序变、xml 转义变),即使内容等价,cache 也会 miss。字节稳定 = 缓存命中率
= 成本下降 + 延迟下降。

实现要点:
- **强制字典序排序**(在 `build_snapshot` 步骤 2 完成,这里只负责序列化)
- **xml escape**: 任何 skill name/description 里的 `<>&"` 必须转义,否则 LLM 把它们当
  xml 标签解析,产生幻觉/格式错乱。函数纯靠 `xml.sax.saxutils.escape`,无自定义规则。
- **compact 模式**: 只输出 name + description,无 body 摘要。预算紧张时降级用。

```python
def render_skills_section(entries: list[SkillSummary], mode: str = "full") -> str:
    # 1. 排序(snapshot 已排过,这里 defensive)
    sorted_entries = sorted(entries, key=lambda e: e.name)
    # 2. 转义
    def esc(s: str) -> str:
        return xml.sax.saxutils.escape(s, {'"': "&quot;"})
    # 3. 模板拼接
    parts = ["## Skills", ""]
    for e in sorted_entries:
        if mode == "compact":
            parts.append(f'<skill name="{esc(e.name)}">')
            parts.append(f"  {esc(e.description)}")
            parts.append("</skill>")
        else:
            parts.append(f'<skill name="{esc(e.name)}" location="{esc(e.location)}">')
            parts.append(f"  {esc(e.description)}")
            parts.append("</skill>")
    parts.append("")
    return "\n".join(parts)
```

**测试断言** ([tests/test_skill_prompt_render.py](../../tests/test_skill_prompt_render.py)):
- 同一份 entries 两次调用 → 字符串完全相同(byte-equal)
- skill name 含 `<` `>` → 正确 escape
- 字典序而非插入序

### 4.5 `merge_by_priority` — 跨源覆盖 + 同源首胜

文件: `agent_core/skills/skill_index.py`

**US5 多源场景**: workspace 用户写的 skill 要覆盖 bundled 内置的同名 skill。
合并语义要清晰。

实现: `per-source seen` 集合 —— 每个 source tuple 内,**首次出现**的 name 胜出并告警;
不同 source 之间,**优先级高**的覆盖优先级低的。

```python
def merge_by_priority(per_source_entries: list[list[SkillEntry]]) -> list[SkillEntry]:
    """per_source_entries 已按优先级升序(末尾最高)"""
    by_name: dict[str, SkillEntry] = {}
    for source_entries in per_source_entries:
        seen_in_source: set[str] = set()
        for entry in source_entries:
            if entry.skill.name in seen_in_source:
                # 同源内同名 → 跳过(首胜)
                continue
            seen_in_source.add(entry.skill.name)
            # 跨源: 高优先级覆盖低优先级
            by_name[entry.skill.name] = entry
    # 最终字典序
    return sorted(by_name.values(), key=lambda e: e.skill.name)
```

**为什么不直接按 `SkillSource` enum 值比较?**
不同目录可能共用同一个 enum 值(都是 `WORKSPACE`)。用 per-source `seen` 才能正确处理
"workspace 路径 A 的 skill 覆盖 workspace 路径 B 的 skill"(US5 扩展: > 2 层来源)。
具体取舍见 design doc §6.1。

**known sharp edge**: broken(load_error)entry 仍按 workspace 优先级覆盖 bundled valid。
"删除恢复"必须删**整个 skill 目录**(scan_source 不再产出 entry),bundled 自然回来。
不删整个目录的话,workspace 的 broken shadow 会让 bundled 的同名 skill 也不出现在 prompt。

### 4.6 `resolve_skill_command` — slash 解析的纯函数

文件: `agent_core/skills/commands.py`

签名: `resolve_skill_command(text: str) -> (skill_name, args) | None`

**为什么独立模块,不放在 web/app.py?**
slash 解析规则跟"web 是否存在"无关 —— 同一函数被 web/app.py 和 web/pages/00_Chat.py
两个入口复用,未来 CLI/TUI/API 也会用。**UseCase 层的纯函数**属于"业务规则",放
web 层会污染可移植性。

```python
_SLASH_RE = re.compile(r"^/([a-z0-9_-]+)(?:\s+(.*))?$", re.DOTALL)

def resolve_skill_command(text: str) -> tuple[str, str] | None:
    m = _SLASH_RE.match(text.strip())
    if m is None:
        return None
    name, args = m.group(1), (m.group(2) or "").strip()
    return (sanitize_skill_command_name(name), args)

def sanitize_skill_command_name(raw: str) -> str:
    s = raw.lower()
    s = re.sub(r"[^a-z0-9_]", "_", s)   # 路径分隔符等 → _
    s = re.sub(r"_+", "_", s).strip("_")
    return s[:32]                        # 截 32 字符
```

**sanitize 的目的**: 防止 path traversal(`/../etc/passwd`)、特殊字符、过长名。
LLM 注入的 name 永远走 sanitize → 安全进入 `get_entry` / `Read` 路径。

### 4.7 slash prompt-rewrite(US4 触发机制)

文件: `web/app.py` (主入口) + `web/pages/00_Chat.py` (子页)

**为什么不直接调 skill.run()?**
skill 不是可执行实体 —— 它是一份"给 LLM 的指令"。`/hello please greet` 的真实流程是:

1. `resolve_skill_command` 解析出 `(name="hello", args="please greet")`
2. `registry.get_entry("hello")` 拿 SkillEntry
3. 校验: 存在 + `load_error is None` + `user_invocable == True`
4. **prompt-rewrite**:
   ```python
   rewrite = f'Use the "{name}" skill. Read its SKILL.md at "{entry.skill.file_path}" and follow its instructions. {args}'
   ```
5. 走正常 `start_run + step`,LLM 拿到的是一段自然语言指令,自取自读

**为什么显式带 `location`?**
spec `disable-model-invocation: true` 的 skill 在 `<available_skills>` 目录表里被隐藏,
LLM 看不见。但用户 `/xxx` 触发时仍可用 —— 因为我们在 rewrite 里**直接告诉 LLM 文件路径**,
跳过"先看见 skill 列表再选"的环节。

**失败路径** (`web/app.py:1763` 附近): 未知 skill 或不可调用 → 友好报错 +
**可用列表**(可选,默认开),让用户知道有哪些可调。走 `("text", ...)` 通道
而不是 `("system", ...)` —— 因为 `st.rerun()` 会清掉 system 频道的临时元素,
text 频道作为 assistant 消息持久化。

### 4.8 `SkillsPromptHandler` — turn_chain 集成

文件: `agent_core/turn_chain.py`(`SkillsPromptHandler` 类)

turn_chain 的责任链模式(参考 [agent-state-machine 设计](../agent-state-machine-and-chain-of-responsibility-design.md))
给 skills 系统提供了一个干净的接入点:在 system message 构造后、模型调用前,
handler 注入 `## Skills` 段。

```python
class SkillsPromptHandler:
    """在 system message 中注入 skills 目录表。"""
    name = "skills_prompt"

    def __call__(self, ctx: HandlerContext) -> HandlerResult:
        if "Read" not in ctx.agent.tools.list_names():
            # spec edge case: 模型没读文件能力 → 整段不注入
            return HandlerResult()
        registry = ctx.agent.skills_registry
        if registry is None:
            return HandlerResult()
        snap = registry.snapshot()
        if not snap.prompt:
            return HandlerResult()
        # 追加到 system message
        ctx.system_message = ctx.system_message + "\n\n" + snap.prompt
        return HandlerResult()
```

**读这段代码该关注的**:
- handler 自身**不构造** snapshot,只**消费** registry 的输出 → 缓存可命中
- "没有 Read 工具就不注入" 是 spec edge case 的硬要求:提示 LLM 有 skill 但它读不了 = 浪费 token + 制造幻觉
- `HandlerResult()` 空返回 = 不修改其它状态(责任链不强制链式副作用)

## 5. 核心设计思想(读源码时该带着的几个原则)

### 5.1 纯函数优先,IO 集中到 Adapter

Entity + UseCase 全部是**纯函数**:入参决定出参,无副作用,无全局读。
IO 只发生在:
- `skill_store.py:load_single_skill` (读目录 + 读 SKILL.md)
- `skill_index.py:scan_source` (listdir)
- `registry.py:SkillsRegistry` (cache 读写)
- `path_validator.py` (os.path.realpath)

回报: 175+ 单测中绝大部分 < 10ms,核心 eligibility/snapshot/merge 测试无需 mock IO。
test pyramid 稳: 多数用例是 entity/use-case 层的纯逻辑测,少量 adapter 集成测,
e2e 走 `verify_skill_quickstart.py` 19 节端到端验证。

### 5.2 字节稳定 = 缓存命中

prompt 段是 LLM 的 system message 一部分,**字符级一致**才能命中 prompt cache。
这倒推了:
- skill 列表**字典序**排序
- 字段值**xml escape** 而非自定义
- 预算降级的输出**确定性**(同样 entries 同样预算 → 同样输出)
- env-hash in sig 保证"环境变了 → version 变了 → 旧 cache 自然失效"

### 5.3 容错:永远不抛,带 load_error 继续

`load_single_skill` 失败(读 IO 错、frontmatter 不合法、required field 缺失)→ 不抛,
`SkillEntry.load_error = "..."`,**仍纳入 snapshot**。

后果:
- 一个 broken skill 不会拖垮整个 registry
- 作者能看到 `format_skill_status` 里的 "errored" 分类,直接定位
- **代价**: broken entry 仍占名字,会按优先级 shadow 同名 valid(见 §4.5 sharp edge)

### 5.4 配置即行为,数据驱动

US5 多源合并不是写死在代码里的"`bundled` + `workspace` 两层",而是
`SourcesConfig(ordered list[(source, dir, priority)])`。加第三层来源(比如
`/etc/agent_core/skills`) = 改配置,不改代码。

类似:
- eligibility 是 frontmatter `metadata.requires` 字段驱动
- 预算是 `SkillsConfig.limits` 字段驱动
- visibility 是 `disable-model-invocation` 字段驱动

**作者 = 配置者**。代码 = 配置的解释器。

### 5.5 唯一入口,Facade 模式

`SkillsRegistry` 是**唯一外部入口**:
- 不能绕过它直接 `load_all(config)` 然后自己 build_snapshot —— 会绕过 cache
- 不能直接传 `(config, env)` 给 `evaluate_eligibility` 然后自己拼字符串 —— 会绕过 visibility/visibility 集成

唯一入口 = 一致性保证。reader 读代码时,看到任何 `from agent_core.skills import
...` 然后直接用 UseCase 函数的,都应该警惕"这是不是绕过了 registry"。

### 5.6 不变量是测试,不是注释

6 条不变量(见 design doc §5)**全部**有对应测试:
| 不变量 | 测试 |
|---|---|
| INV-1 去重 | test_skill_index_merge |
| INV-2 字节稳定 | test_skill_prompt_render |
| INV-3 容错 | test_skill_store (load_error 用例) |
| INV-4 预算不静默丢 | test_skill_snapshot_budget |
| INV-5 env 触发 hot-reload | test_skill_eligibility + test_skill_registry_hotreload |
| INV-6 version 单调 | test_skill_registry_hotreload |

**改代码前**: 跑相关不变量测试 → 全绿 → 改 → 跑 → 全绿。
**改代码后**: 新加的不变量必须有测试,否则等于没加。

## 6. 已知偏差与取舍(透明记录)

完整偏差见 [design doc §6](../../docs/agent_core-skill-system-design.md#6-实现期决策与偏差透明记录)。
这里只强调**读代码时容易踩的** 3 个:

### 6.1 DISABLED 枚举存在但 v1 不可达
`SkillEligibilityState.DISABLED` 是 spec 预留的 v2 状态。v1 没 per-skill disable
机制,`evaluate_eligibility` 不会产生这个状态。代码里看到 `state == DISABLED` 的
分支 = dead code(显式保留以备 v2)。

### 6.2 `requires.config` 在生产 v1 始终 unmet
`evaluate_eligibility(config=...)` 参数是空的,registry 暂时没接 agent 全局 Config。
snapshot 端测试可注入真实 config dict 验证 truthy 路径(看
[test_skill_eligibility.py](../../tests/test_skill_eligibility.py))。
**生产用户写 `requires.config: [...]` 等于无 op** —— 接通 Config 留待 v2。

### 6.3 body 相对路径解析是诊断工具,不是运行时依赖
`resolve_body_references` 把 body 里 `references/`/`scripts/`/`assets/` 引用解析成
绝对路径,存到 `SkillEntry.body_references`。**LLM 不会看到**这些路径(为避免扰动
SC-002 字节稳定),**只用于 status 诊断**(`scripts/skills_check.py` 输出 `refs=N` +
路径列表)。

作者用 skill 时,模型从 `<location>` 推导 dirname 构造 sibling 路径对 v1 读可靠性已够用。
**已知噪声**: regex 扫描无法区分"真实引用"与"举例性提及"(bundled skill-creator body
里 `(e.g. references/cheatsheet.md)` 会被计入),属可接受的诊断噪声,作者据列表判断。

## 7. 扩展入口(给"我要加新东西"的读者)

### 7.1 加一种新的 eligibility 维度
1. `types.py:SkillRequires` 加字段(`+ os: tuple[str, ...] = ()` 这种)
2. `eligibility.py:evaluate_eligibility` 加判定分支
3. `frontmatter.py:parse_metadata` 解析新字段
4. `tests/test_skill_eligibility.py` 加新维度用例
5. `data-model.md` 更新字段定义

### 7.2 加一种新的 snapshot 过滤维度
跟 §7.1 类似,但入口在 `snapshot.py:_filter_eligible_visible`。
**记住**: 任何过滤都是纯函数,env/config 都是参数注入,**不要读全局**。

### 7.3 加一种新的 visibility
1. `types.py:SkillVisibility` 加枚举值
2. `frontmatter.py:parse_metadata` 映射 frontmatter 字段 → 枚举
3. `snapshot.py:_filter_eligible_visible` 应用新 visibility
4. `status.py:format_skill_status` 分类展示

### 7.4 接 agent 全局 Config 到 eligibility
1. agent 构造时把 `Config` 传给 `SkillsRegistry`
2. `registry.py:snapshot` 把 `config.model_dump()` 注入 `build_snapshot`
3. `snapshot.py` 把 config dict 透传给 `evaluate_eligibility`
4. `requires.config` 自动生效

## 8. 代码地图(快速 reference)

| 文件 | 行数(约) | 职责 | 测试文件 |
|---|---|---|---|
| `agent_core/skills/types.py` | 350+ | 数据契约、枚举、解析 dataclass | test_skill_types |
| `agent_core/skills/frontmatter.py` | 150 | SKILL.md → SkillFrontmatter | test_skill_frontmatter |
| `agent_core/skills/prompt.py` | 80 | render + escape (纯函数) | test_skill_prompt_render |
| `agent_core/skills/eligibility.py` | 100 | 5 维 requires 判定 | test_skill_eligibility |
| `agent_core/skills/snapshot.py` | 150 | filter+sort+budget+render 串珠 | test_skill_snapshot_budget |
| `agent_core/skills/commands.py` | 60 | slash 解析 + sanitize | test_skill_commands |
| `agent_core/skills/status.py` | 100 | format_skill_status 诊断 | test_skill_status |
| `agent_core/skills/skill_store.py` | 180 | 单 skill 加载 + body refs | test_skill_store |
| `agent_core/skills/skill_index.py` | 120 | 多源 scan + merge | test_skill_index_merge |
| `agent_core/skills/registry.py` | 180 | Facade + Cache-Aside + version | test_skill_registry_hotreload |
| `agent_core/skills/config.py` | 100 | pydantic SkillsConfig + SourcesConfig | test_skill_config |
| `agent_core/skills/path_validator.py` | 40 | symlink 逃逸防护 | (覆盖在 test_skill_store) |
| `agent_core/skills/builtin/skill-creator/SKILL.md` | 80 | bundled 示例(给作者当模板) | test_skill_bundled |
| `agent_core/skills/__init__.py` | 80 | barrel 导出(37 个公共符号) | — |
| `agent_core/turn_chain.py:SkillsPromptHandler` | 60 | turn_chain 注入入口 | test_skills_prompt_handler |
| `agent_core/tools/builtin.py:Read` | 80 | LLM 按需读 SKILL.md 的工具 | test_read_tool |
| `web/app.py:slash_dispatch` | 50 | 主入口 slash 派发 | (UI 手动 T047) |
| `web/pages/00_Chat.py:slash_dispatch` | 30 | 子页 slash 派发 | (同上) |
| `scripts/skills_check.py` | 50 | 诊断 CLI (FR-022) | test_skill_status |
| `scripts/verify_skill_quickstart.py` | 600+ | 19 节端到端验证 | (e2e) |

## 9. 进一步阅读

- [agent_core-skill-system-design.md](../agent_core-skill-system-design.md) —
  规格镜像 + 实现期偏差 6 项(本文不重复)
- [spec.md](../../specs/001-skill-system/spec.md) — 24 个 FR + 5 个 user story
- [data-model.md](../../specs/001-skill-system/data-model.md) — 实体定义 + 6 条不变量
- [contracts/skill-md-format.md](../../specs/001-skill-system/contracts/skill-md-format.md) —
  作者契约(SKILL.md frontmatter 规范)
- [quickstart.md](../../specs/001-skill-system/quickstart.md) — 11 节端到端
- [openclaw-skill-system.md](openclaw-skill-system.md) — 参考来源(92KB,设计灵感)
- [agent-state-machine-and-chain-of-responsibility-design.md](../agent-state-machine-and-chain-of-responsibility-design.md) —
  turn_chain 责任链机制
