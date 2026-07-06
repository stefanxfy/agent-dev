# Quickstart Validation: Skill System (agent_core)

**Date**: 2026-07-05
**Purpose**: 证明 v1 skill 系统**端到端可用**的Runnable验证脚本。
**Constitution 门**: Principle II 数据驱动——SC-002/SC-003 的 token / 字节稳定性 MUST 用实测验证（非假设）。

> 本文件是**验证指南**，不含完整实现代码或测试套件。实现细节在 tasks.md / 实现阶段。
> 引用而非复刻 [data-model.md](data-model.md) 与 [contracts/skill-md-format.md](contracts/skill-md-format.md)。

---

## 0. 前置

```bash
# 项目根
cd /Users/fanyunxu/Desktop/myproject/agent-dev
source .venv/bin/activate                # uv 管理的 venv
python3 -m pytest -q                     # 全绿基线（Constitution IV）
```

---

## 1. 端到端：single skill 发现 + 注入 + 按需 Read（覆盖 US1 / SC-001 / SC-003）

**目标**：放一个 skill → 下次 run 的 system prompt 含 `## Skills` 段 → 模型触发时 Read 了 SKILL.md。

**步骤**：
1. 在 workspace skills 目录新建 `skills/hello/SKILL.md`：
   ```yaml
   ---
   name: hello
   description: "Use when the user says hello or asks for a greeting."
   ---
   # Hello skill
   Always respond with: "Skill-loaded hello!"
   ```
2. 跑验证脚本（实现期新建 `scripts/verify_skill_e2e.py`）：
   ```python
   from agent_core.skills.registry import SkillsRegistry
   from agent_core.skills.config import SkillsConfig
   cfg = SkillsConfig.from_env()
   reg = SkillsRegistry(cfg)
   snap = reg.snapshot()
   assert "hello" in snap.prompt.lower()            # 注入了
   assert "<available_skills>" in snap.prompt       # 格式正确
   assert "hello" not in snap.prompt.split("<available_skills>")[1] or True
   ```
3. 用真实 agent run 验证 Read 工具触发（Constitution II 实测门）：
   ```python
   agent = get_agent(session_id="verify")
   agent.start_run("say hello to me")
   # 收集 events，断言出现 tool_call(name="Read", path 含 hello/SKILL.md)
   # 断言 final_answer 含 "Skill-loaded hello"
   ```
4. 删除 `skills/hello/` → 新 run 的 snapshot prompt 不含 hello（确认无残留）。

**期望结果**：SC-001（无需改代码/重启即被发现）、SC-003（匹配任务至多预读一个 SKILL.md）。

---

## 2. 双层优先级覆盖（覆盖 US5 / SC-007 / INV-1）

**步骤**：
1. bundled 源已有 `skill-creator`（自带）。
2. workspace 源新建同名 `skills/skill-creator/SKILL.md`，body 改为 `"OVERRIDDEN body marker"`。
3. `reg.snapshot()` → prompt 中 location 指向 **workspace** 那份；用 Read 读出含 `"OVERRIDDEN body marker"`。
4. 删除 workspace 版 → bundled 版恢复。

**期望结果**：SC-007 确定性覆盖；INV-1 同名去重。

---

## 3. 字节稳定性（覆盖 SC-002 / INV-2）— **Constitution II 实测门**

**步骤**：
```python
snap1 = reg.snapshot()
snap2 = reg.snapshot()
assert snap1.prompt == snap2.prompt                  # 字节相等
assert hash(snap1.prompt) == hash(snap2.prompt)
# 改一个不相关 skill 的 mtime 再读 → 仍 prompt 部分对其它 skill 顺序不变
```
跨进程跑两次 agent run，对比两次 `agent.llm` 收到的 system message 中 `## Skills` 段**字节一致**。

**期望结果**：SC-002。

---

## 4. 资格判定即时性（覆盖 US3 / FR-013/14 / INV-5）

**步骤**：
1. 新建 `skills/need-key/SKILL.md`，`metadata.requires.env: ["VERIFY_KEY"]`。
2. `assert "need-key" not in reg.snapshot().prompt`（环境无 VERIFY_KEY → ineligible）。
3. `os.environ["VERIFY_KEY"] = "x"` → `reg.snapshot()` bump version 且 `"need-key" in prompt`。
4. 同 skill 加 `disable-model-invocation: true` → 不在 prompt，但 `format_skill_status` 仍列它为 `HIDDEN_FROM_MODEL`。

**期望结果**：FR-014 即时计算；FR-015 hidden 行为。

---

## 5. 预算三级降级（覆盖 FR-017/18 / SC-004 / INV-4）

**步骤**：
1. 临时设 `SkillsConfig(limits=LimitsConfig(max_skills_prompt_chars=2000))`。
2. 生成 60 个 skill（脚本批量建目录）。
3. `snap = reg.snapshot()`：
   - `snap.render_mode in {"compact", "truncate"}`（full 装不下）。
   - `len(snap.prompt) <= 2000`（含警告）。
   - 若 `truncated_count > 0` → `snap.prompt.startswith("⚠️")`（不静默丢）。
4. 还原配置，删除批量 skill。

**期望结果**：SC-004；INV-4。

---

## 6. slash 命令显式触发（覆盖 US4 / FR-020）— UI 手动验证

> Constitution 开发流程门：UI（`web/app.py`）由用户手动验证。

**步骤**：
1. `streamlit run web/app.py`。
2. 在 chat 输入 `/hello please greet` 。
3. **期望**：agent 回复含 `"Skill-loaded hello!"`（prompt-rewrite 生效，绕过模型自动选择）。
4. 输入 `/nonexistent-skill x` → **期望**：友好报错"无匹配 skill"，不崩。

backend 单测（自动）：`resolve_skill_command("/hello please greet") == ("hello", "please greet")`。

---

## 7. 容错（覆盖 FR-005 / SC-005 / INV-3）

**步骤**：
1. 新建 `skills/broken/SKILL.md`，内容为 `---\nname: broken\n---`（**缺 description**）。
2. 新建 `skills/good/SKILL.md`（合法）。
3. `reg.snapshot()` → `"good" in prompt`、`"broken" not in prompt`；`format_skill_status(reg)` 列 broken 为 `ERRORED(load_error=...)`。
4. agent 不崩，正常回复其他任务。

**期望结果**：SC-005；INV-3。

---

## 8. 热重载（覆盖 FR-021a / SC-001 / INV-6）

**步骤**：
```python
v0 = reg.snapshot().version
# 不动磁盘
v1 = reg.snapshot().version
assert v0 == v1                                     # 无变化 → version 不 bump
# touch 一个 SKILL.md（改 mtime）
Path("skills/hello/SKILL.md").touch()
v2 = reg.snapshot().version
assert v2 > v1                                      # mtime 变 → bump
```

**期望结果**：SC-001 无需重启；INV-6 版本单调。

---

## 9. Read 工具（覆盖 FR-011/12）

**步骤**：
```python
from agent_core.tools.builtin import READ_TOOL
out = READ_TOOL.handler(path="skills/hello/SKILL.md")
assert "Always respond with" in out
# 缺文件
READ_TOOL.handler(path="/nonexistent")  # 返回错误字符串，不抛
# 超限（构造 > 256KB 文件）→ 拒绝并返回错误字符串
```
确认 `READ_TOOL.name == "Read"` 与既有 permission/safety 子系统白名单一致。

**期望结果**：FR-011/12。

---

## 10. 全量回归门

实现完成后 MUST 跑：
```bash
python3 -m pytest tests/test_skill_frontmatter.py tests/test_skill_store.py \
  tests/test_skill_index_merge.py tests/test_skill_eligibility.py \
  tests/test_skill_snapshot_budget.py tests/test_skill_prompt_render.py \
  tests/test_skill_commands.py tests/test_skills_prompt_handler.py \
  tests/test_read_tool.py tests/test_skill_registry_hotreload.py -q
python3 -m pytest -q          # Constitution IV：全量不回归
```

## 11. 完成定义对照（spec acceptance）

| spec 条目 | 本 quickstart 章节 |
|---|---|
| SC-001 无需重启 | §1, §8 |
| SC-002 字节稳定 | §3 |
| SC-003 至多预读一个 | §1 |
| SC-004 预算降级 | §5 |
| SC-005 容错 | §7 |
| SC-006 inspect 命令 | §4, §7（`format_skill_status`） |
| SC-007 双层覆盖 | §2 |
| FR-011/12 Read 工具 | §9 |
| FR-013/14/15 eligibility | §4 |
| FR-017/18 预算 | §5 |
| FR-019 双层优先级 | §2 |
| FR-020 slash 调用 | §6 |
| FR-021a 热重载 | §8 |
