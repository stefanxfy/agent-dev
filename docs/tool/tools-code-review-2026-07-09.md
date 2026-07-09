# 代码审查报告：`agent_core/tools` 工具调用模块

> 由 `/code-super-advisor` skill 生成（审查代码模式）。三层审查：架构层（Clean Architecture）→ 模式层（GoF/SOLID）→ 代码层（Fowler《重构》）。

## 审查信息

| 项目 | 内容 |
|------|------|
| 审查范围 | `agent_core/tools/`（含 `sandbox_backends/` 子包） |
| 审查日期 | 2026-07-09 |
| 代码语言 | Python 3（dataclasses + Pydantic 混用，无 LangChain） |
| 代码规模 | 23 文件 / 7,880 行 |
| 审查方法 | 4 个并行子代理全量精读 4 集群 + 主审查者核读 `base.py` 全文与 `permission_engine.py` 关键段，所有结论带 file:line 可追溯 |

## 总体评价

- **综合评级**：⭐⭐⭐☆☆（3/5）
- **架构健康度**：C+
- **模式合理度**：C+
- **代码质量**：C+
- **一句话总结**：模块功能完整、防御性强、日志规范，但名为 "tools" 的包 **65% 是权限逻辑**且平铺无 facade，权限核心存在 **"规则内容匹配失效"** 与 **"安全钩子重复执行"** 两个 P0 缺陷，工具执行管线被分散在 3 层——急需把权限子系统抽成独立子包并补齐规则内容匹配。

## 问题统计

| 严重度 | 架构层 | 模式层 | 代码层 | 合计 |
|--------|--------|--------|--------|------|
| P0 | 0 | 2 | 0 | **2** |
| P1 | 3 | 3 | 5 | **11** |
| P2 | 3 | 2 | 6 | **11** |
| P3 | 0 | 1 | 1 | **2** |

---

## 模块现状速览（回答"存在哪些架构、如何分层"）

该包实际上是 **4 个子系统平铺在一个 flat package** 里：

```
agent_core/tools/                      7,880 行
├── ① 工具核心  (~750 行)              base.py, builtin.py
├── ② 权限决策  (~5,100 行, 65%!)      permission_engine/hook/matcher/loader/types/
│                                      ui_helpers, denial_tracking, classifier,
│                                      classifier_fast_path, bash_permissions
├── ③ 沙箱执行  (~1,050 行)            sandbox_manager/decision/prompt +
│                                      sandbox_backends/(base/native/srt/_cleanup)
└── ④ 横切关注  (~800 行)              safety_check.py, audit_logger.py
```

依赖方向基本正确、**无环**：`permission_types`(叶) ← matcher/hook/loader ← engine；sandbox 子系统 inward direction 干净。这是模块最大的优点。问题不在"依赖成环"，而在**职责配重失衡 + 边界未定义 + 执行管线无单一 owner**。

---

## 一、架构层审查
> 基于《架构整洁之道》

### 1.1 组件划分

**尖叫的架构失真（A1）**：包名叫 `tools`，但 LOC 占比最高的是**权限**（65%）。一个新读者打开 `agent_core/tools/` 看到 17 个 `permission_*/classifier*/bash_permissions` 文件，根本无法一眼看出"这是工具执行模块"。按 Clean Architecture 的"组件内聚"，权限应抽成 `agent_core/tools/permission/`（或独立 `agent_core/permission/`）子包，`tools/` 只留 ① + 必要入口。

**沙箱子系统是正面样板**：`sandbox_backends/` 用 Protocol + Adapter 把机制隔离，`base.py` 零项目内依赖，是真·可独立组件。

### 1.2 依赖方向

方向正确（acyclic，types 在最底），但有两处**反向泄漏**：
- `sandbox_manager._config` 被 5 个文件当 public 读（`bash_permissions.py:579`、`sandbox_decision.py:58,135,137`、`sandbox_prompt.py:43,47`）——UseCase 的私有配置成了事实公共状态。
- `permission_hook.default_hooks()` 跨模块读 `safety_check` 的下划线私有量 `_SECRET_CHECK_TOOLS` / `_PATH_CHECK_TOOLS`（`permission_hook.py:545,613`）——hook 层依赖 safety 层的实现细节。

### 1.3 分层合理性

**工具执行管线被涂 smear 在 3 层，无单一 owner（A2）**——这是本模块最关键的架构问题：

| 层 | 文件 | 职责 |
|----|------|------|
| 注册/执行 | `base.py:126-208` `ToolRegistry.execute` | jsonschema 校验 + ThreadPool 超时 + 重试分类 |
| 决策/审计 | `permission_engine.py` | 14 步 permission+safety+audit 决策（**先于** execute） |
| 编排 | `turn_chain.py:1647-1812` | 串/并行调度、ASK 短路、4 路 result sink |

`base.execute` 对 safety/audit **零感知**——它没有任何 hook point。于是横切关注点（safety/audit）被迫硬塞进 `PermissionEngine`（A3），`base.execute` 的"重试/超时"与 `turn_chain` 的"并行调度"概念重叠却无共享抽象。"一次工具调用发生了什么"要读 3 个文件才能拼出来。

### 1.4 架构层发现列表

| # | 严重度 | 问题 | 位置 | 建议 |
|---|--------|------|------|------|
| A1 | P1 | "tools" 包 65% 是权限逻辑，4 子系统平铺，命名不尖叫 | 整包 | 权限抽成 `permission/` 子包，`tools/` 只留工具核心 |
| A2 | P1 | 工具执行管线涂 smear 在 base.execute / permission_engine / turn_chain 三层，无 owner | base.py:126 + permission_engine.py + turn_chain.py:1647 | 定义 `ToolExecutionPipeline`，把 safety/audit compose 成 execute 的前后置 hook |
| A3 | P1 | 横切关注点（safety/audit）硬编码进 PermissionEngine，base.execute 无 hook point | permission_engine.py:283,704 | 在 `ToolRegistry` 上开 `PreExecuteHook`/`PostExecuteHook` 接口 |
| A4 | P2 | `sandbox_manager._config` 事实 public，5 文件 reach into private | 见 1.2 | 加只读 accessor（`runtime_view()`/`excluded_command_match()`） |
| A5 | P2 | `__init__.py` 0 字节，无 facade，~25 consumers 硬编码深路径 | __init__.py | 导出 `ToolDef/ToolRegistry/register_builtin_tools/AuditLogger/safety_check` 作稳定 facade |
| A6 | P2 | 沙箱后端默认可写路径策略不一致：Seatbelt 硬编码 `/tmp`+`/private/tmp`，srt/bwrap 没有 | native_backend.py:214-217 ↔ srt_backend.py:129 | 把默认 allowlist 提到 `_build_runtime_config`，让三后端一致 |

---

## 二、模式层审查
> 基于《设计模式》+ SOLID

### 2.1 SOLID 原则检查

| 原则 | 遵循 | 违反位置 | 说明 |
|------|------|---------|------|
| SRP | ❌ | permission_engine.py / bash_permissions.py(758行) / audit_logger.py(479行) | `PermissionEngine` 兼 pipeline+规则解析+classifier+audit+deny state+bash 分发，6 职责 |
| OCP | ❌ | permission_engine.check_permissions / 规则语法解析 | 加一步需改 460 行方法；`"Tool(content)"` 语法解析散落 3 处 |
| LSP | ⚠️ | sandbox_backends/base.py:90 NullBackend | `is_available()=True` 但 `wrap()` 返回未隔离命令，破坏 "available 即隔离" 语义契约 |
| ISP | ⚠️ | permission_types.py:361 ToolPermissionContext | 13 字段大半 optional/dormant，多数 consumer 只用 2-3 个 |
| DIP | ⚠️ | permission_engine.py | engine 依赖具体 `classifier`/`safety_check`/`bash_permissions`，无 Protocol/ABC，测试须 monkeypatch 具体模块 |
| LoD | ❌ | audit_logger.py:193-242 | 4 层 getattr 链深入 PermissionDecision/RuleReason/enum 内部 |

### 2.2 模式使用评估

**用得好的**：`HookRegistry`（真·Chain of Responsibility/Observer）、沙箱 Strategy + NullBackend(Null Object) + Factory-method 混合、`permission_types` 的 discriminated union 干净。

**用偏的**：
- **两套并行 Rule Matcher，语义不一致（M1，P0）**——这是本模块最严重的模式问题。`permission_engine._check_global_*_rule`（`engine:605-633`）只比 `rule.tool_name == tool_name`，**完全忽略解析出的 `rule_content`**；而 `permission_matcher.matching_rules_for_input` 是完整 content-aware 的 Specification——**却是 dead code**（无生产调用）。后果：内容级规则 `Edit(/tmp/*)` allow → **退化为允许所有 Edit**（越权）；`Edit(/secrets/*)` deny → 退化为拒绝所有 Edit（过度拦截）。Bash 因有 Step 1c' `_run_bash_check_permissions` content-aware 旁路而幸免，非 Bash 工具则裸奔。**Specification 模式没贯彻到 engine。**
- **PreToolUse 钩子链被实例化执行两次（M2，P0）**——`engine:308`（Step 1.5）和 `engine:505`（Step 5）都调 `run_pre_tool_use`。每次 `check_permissions` 跑两遍 secret-scan，任何有状态 hook 静默 double-fire。docstring 只列了一次，疑似 Step 5 残留回归。
- **想做 CoR 实为 monolith（M3）**：`check_permissions` 是 460 行单方法，14 个 inline step，每个"step"是 `if` 块而非 handler 对象，无法独立测试/重排。

### 2.3 模式层发现列表

| # | 严重度 | 问题 | 位置 | 建议 |
|---|--------|------|------|------|
| M1 | **P0** | 全局规则只匹配 tool_name，忽略 rule_content；全版 matcher 是 dead code → 内容级 allow 越权 / deny 过度拦截 | engine:605-633 ↔ matcher:427 | engine 接入 `matching_rules_for_input`，或明确 tool-level 语义并修文档+测试 |
| M2 | **P0** | PreToolUse 钩子链 Step 1.5 与 Step 5 重复执行 | engine:308 + engine:505 | 删 Step 5（或确认意图后保留其一） |
| M3 | P1 | check_permissions 460 行 monolith，伪 CoR | engine:114-577 | 抽 `PermissionStep` handler 接口 / 至少参数化 `_check_global_*` 三方法 + 提取两个 hook 块 |
| M4 | P1 | SRP 违规：PermissionEngine 6 职责；bash_permissions 4 职责；AuditLogger 3 职责 | 见 2.1 | 按 `bash_parser` / `bash_rule_checker` 拆分；AuditLogger 拆 writer/queryer |
| M5 | P1 | OCP 违规：规则语法解析散落 3 处 | engine:635 / matcher:385 / ui_helpers:188 | 统一到 `permission_matcher` 单一 parser |
| M6 | P2 | NullBackend 破坏 "available 即隔离" LSP 式契约 | base.py:90 | 文档明确 NullBackend 是 fail-loud passthrough，或让 `is_available()=False` |
| M7 | P2 | 缺 Template Method：两后端 wrap() 骨架 + is_available 缓存重复；classifier 调用块在 bash_permissions↔engine 重复 | native/srt backends；bash_permissions:443-476 ↔ engine:420-463 | 提 `BaseBackend` 模板方法；提 `run_classifier_check()` helper |
| M8 | P3 | YAGNI：webhook/retry factory、speculative classifier 均无生产调用 | hook:686,733；classifier:382,404 | 接线或删除 |

---

## 三、代码层审查
> 基于《重构：改善既有代码的设计》

### 3.1 坏味道诊断

| # | 坏味道 | 位置 | 严重度 | 推荐重构手法 |
|---|--------|------|--------|-------------|
| 1 | 安全遍历逻辑重复 | safety_check.py:246-280 ↔ permission_hook.py:551-593 | P1 | Extract Method `_iter_string_values(tool_input)` |
| 2 | classifier 调用块重复 | bash_permissions.py:443-476 ↔ engine:420-463 | P1 | Extract `run_classifier_check()` |
| 3 | subcommand 扫描循环重复 | bash_permissions.py:484-517 ↔ 722-747 | P1 | Extract `_scan_subcommands()` |
| 4 | 过长函数 | check_permissions(460) / bash_handler(152) / AuditLogger.log(~95) / run_pre_tool_use(~160) | P1 | Extract Method（拆到 <40 行） |
| 5 | 静默丢数据 | permission_loader.py:191-197 deny 分支 `pass` | P1 | 补全或删除该 helper |
| 6 | 注释/代码矛盾 | sandbox_manager.py:86,17 称 "__new__已去" 但 :92 仍强制单例；engine Step 7 称 "audit placeholder" 但已在 _log_and_return 实现 | P1 | 修注释或修代码，二选一 |
| 7 | 注入风险 | native_backend.py:220,225,229,232 路径未转义插值进 S-expression（bwrap 用了 shlex.join，不一致） | P1 | 转义 S-expr 字符串字面量 |
| 8 | Feature Envy | audit_logger.py:193-242 四层 getattr | P2 | 给 PermissionDecision 加 `to_audit_dict()` |
| 9 | Data Clump | AuditLogger.query 9 过滤参数重复 3 处 | P2 | 引入 `AuditQuery` dataclass |
| 10 | Magic Strings | tool 名/stage 名/hook 事件名全字符串无 enum | P2 | enum 化（typo 即静默失效） |
| 11 | Dead Code | matching_rules_for_input / _swap_mode / run_default_cleanup import / _current_cancel_event legacy / 两个 factory | P2 | 删除 |
| 12 | 资源浪费 | base.py:179 每个 attempt 新建 ThreadPoolExecutor(max_workers=1)，成功路径也建 | P2 | executor 移出重试循环 |
| 13 | 数学不一致 | classifier.py:44 `_MAX_TRANSCRIPT_TOKENS=100_000` 配 `len*1000` → 实际 100 条消息即禁用 | P2 | 改名 `_MAX_TRANSCRIPT_MESSAGES=100` 或用真 tokenizer |

### 3.2 代码组织

**正面**：子 logger 命名规范（`agent_core.permission`/`agent_core.sandbox`/`agent_core.audit`）、防御性 fallback 链（tree-sitter→regex）、docstring 普遍存在、与 `docs/tool/tool-security-architecture.md` 章节对齐标注清晰——符合"核心环节 MUST 打 debug 日志"的项目偏好。

**负面**：`base.execute`（`base.py:126-208`）是 80 行 God Method，混了校验/cancel 注入/重试/超时/错误分类 5 件事；inline import（time/concurrent/jsonschema/requests）散见多处；`_estimate_tokens` 名实不符。

### 3.3 代码层发现列表（P1 及以上；P2/P3 见 3.1）

| # | 严重度 | 问题 | 位置 | 建议 |
|---|--------|------|------|------|
| C1 | P1 | Seatbelt 路径未转义插值进 S-expression（注入） | native_backend.py:220-232 | 转义 `"`/`\` 或 regex 校验路径 |
| C2 | P1 | 三处重复代码（安全遍历/classifier 调用/subcommand 扫描） | 见 3.1 #1-3 | Extract Method + 共享 helper |
| C3 | P1 | 多个 100~460 行长方法 | 见 3.1 #4 | Extract Method |
| C4 | P1 | `_parse_settings_dict` deny 分支 `pass` 静默丢规则 | permission_loader.py:191-197 | 补全或删 helper |
| C5 | P1 | 注释/代码矛盾（单例、audit placeholder） | sandbox_manager.py:17,86,92；engine:18 | 统一 |

---

## 四、问题归因分析
> 代码层症状 ← 模式层根因 ← 架构层根因

| 代码层症状 | ← 模式层根因 | ← 架构层根因 |
|-----------|-------------|-------------|
| 三处重复代码（C2） | Specification/Template Method 未提取（M7） | 横切关注点未 compose 进 execute path，engine monolith（A2/A3） |
| `check_permissions` 460 行长方法（C3） | 想做 CoR 但无 handler 抽象（M3） | 执行管线无单一 owner，涂 smear 在 3 层（A2） |
| Magic strings + stale docstrings（C5/3.1#10） | 无单一 public API 边界 | `__init__.py` 空，模块边界未定义（A1/A5） |
| rule_content 匹配失效（M1，P0） | 两套并行 matcher，Specification 未贯彻到 engine | 权限子系统未独立，engine 承担过多职责（A1/M4） |
| `_config` 被到处读（A4） | SandboxConfig 无 accessor，依赖倒置不彻底 | facade 不完整（A5） |

**核心因果链**：A1（权限未独立成包）+ A2（执行管线无 owner）→ M3/M4（engine 变 monolith、SRP 失守）→ C2/C3（重复与长方法蔓延）。修架构层 1-2 项，模式层与代码层大量症状会自然消退。

---

## 五、改进路线图

### P0 — 立即修复
- [ ] **M1**：engine 接入 content-aware matcher（或明确 global rules 为 tool-level 并修文档+测试）→ 堵住"内容级 allow 越权"（~0.5 天）
- [ ] **M2**：删除 Step 5 重复 hook 执行，确认 Step 1.5 为唯一入口（~0.5 天）
- [ ] **C1**：Seatbelt 路径转义（~0.5 天）

### P1 — 短期改进（1-2 周）
- [ ] **A5**：建 `__init__.py` facade，导出稳定公共 API（~1 天）
- [ ] **A6**：统一三后端默认可写路径策略（~0.5 天）
- [ ] **C5**：修 sandbox 单例注释/代码矛盾 + engine Step 7 stale docstring（~0.5 天）
- [ ] **C4**：修 `_parse_settings_dict` deny 分支静默丢规则（~0.5 天）
- [ ] **C2**：提取 3 处重复（安全遍历 / classifier 调用 / subcommand 扫描）（~1 天）

### P2 — 中期优化（1 个月）
- [ ] **A1**：权限抽成 `permission/` 子包（~3 天）
- [ ] **M3**：`check_permissions` 拆为 step handler 链（~2 天）
- [ ] **A2/A3**：定义 `ToolExecutionPipeline` + Pre/PostExecute hook，把 safety/audit 从 engine 移出（~3 天）
- [ ] **C3**：bash_handler / AuditLogger.log 拆方法（~1 天）
- [ ] **A4/C8**：SandboxConfig accessor + enum 化 magic strings（~1.5 天）
- [ ] **C9/C11/C12**：清 dead code、executor 移出循环、修 token 估算（~1 天）

### P3 — 长期改善
- [ ] **M8**：speculative classifier / webhook factory 接线或删除
- [ ] 统一 audit_logger 双 logger；补 `ToolLike` Protocol

---

## 附录

### 审查依据
- 《架构整洁之道》— 组件划分、依赖规则、边界（系统层）
- 《设计模式》+ SOLID — Strategy/CoR/Specification/Template Method/Null Object（模式层）
- 《重构：改善既有代码的设计》— 22 种坏味道 + Extract Method（代码层）

### 备注
- 本报告为只读审查，未改动任何代码。
- 沙箱子系统是模块内的架构亮点（Protocol + Null Object + 中央化 cleanup），建议作为权限子系统重构的参照样板。
- 相关设计文档：`docs/tool/permission-design.md`、`docs/tool/sandbox-pluggable-design.md`、`docs/tool/tool-security-architecture.md`。
