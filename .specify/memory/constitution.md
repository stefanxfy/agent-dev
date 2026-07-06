<!--
Sync Impact Report
==================
Version change: (unfilled template) → 1.0.0
Bump rationale: Initial ratification. All template placeholders replaced with
principles derived from the project's own docs (README, agent-dev 开发规则汇总,
agent-state-machine-and-chain-of-responsibility-design.md) and the user's
global CLAUDE.md anti-laziness rules. MAJOR 1.0.0 = first adopted constitution.

Modified principles: (none — all new)
  - [PRINCIPLE_1]  → I.  自研优先 (Self-Built First)
  - [PRINCIPLE_2]  → II. 数据驱动，绝不编造 (Data-Driven, No Hallucination)
  - [PRINCIPLE_3]  → III. 文档即硬约束 (Documentation is a Hard Constraint)
  - [PRINCIPLE_4]  → IV. 测试纪律 (Test Discipline — NON-NEGOTIABLE)
  - [PRINCIPLE_5]  → V.  不偷偷缩范围 (No Silent Scope Shrink)
  - (added)        → VI. 防御式务实工程 (Defensive & Pragmatic Engineering)

Added sections:
  - 技术约束 (Technical Constraints)              ← [SECTION_2]
  - 开发流程与质量门 (Development Workflow & Gates) ← [SECTION_3]
  - Governance (filled)                           ← [GOVERNANCE_RULES]

Removed sections: (none)

Templates requiring updates:
  - .specify/templates/plan-template.md    — ✅ no change needed (Constitution
        Check section is intentionally generic; filled dynamically by
        /speckit-plan against this file)
  - .specify/templates/spec-template.md    — ✅ no change needed (generic
        user-story structure; no principle-specific references)
  - .specify/templates/tasks-template.md   — ✅ no change needed (generic
        phase structure; no principle-specific references)
  - .specify/templates/checklist-template.md — ✅ no change needed (generic)
  - .specify/templates/commands/           — N/A (directory does not exist)

Follow-up TODOs: (none — all placeholders filled; no deferred items)
-->

# Agent Dev Constitution

> 自研生产级 Agent 系统的工程治理基线。本文件在所有开发实践与项目文档之上；
> 冲突时以本文件为准，或发起修正案（见 Governance）。

## Core Principles

### I. 自研优先 (Self-Built First / Learning by Building)

- 从零手写生产级 Agent 系统，核心逻辑（ReAct 循环、工具系统、记忆系统、
  Skill 系统、上下文管理、多 Agent 编排）全部自研。
- **极简依赖**：只装必须的 SDK（`anthropic` / `openai` / `zhipuai`），
  不装 Agent 框架。`langgraph_agent/` 仅作教学对比，不替代 `agent_core/`。
- **学习路径**：先手写原生理解本质 → 再学框架设计思想 → 最后回归自研改进。
  新增框架级依赖前 MUST justify。
- **理由**：本项目定位是"理解底层原理"。框架抽象会遮蔽原理，自研既是手段
  也是目的。

### II. 数据驱动，绝不编造 (Data-Driven, No Hallucination)

- 所有量化结论（Token 估算、cache 命中率、压缩比）MUST 基于实测，
  不使用假设数字。
- **权威来源**：LLM API 返回的 `input_tokens` 是 Token 计数的唯一权威来源；
  `SimpleTokenCounter`（±20%）仅作预算管理近似，且 MUST 与 tiktoken 校准。
- **不编造铁律**：不确定的 MUST 明确说"不确定"，绝不凭印象编一个听起来
  合理的说法。
- **工程决策**：没有 baseline 的实验不做；没有离线评估的模型不上线；
  推理服务 MUST 有降级策略；务实优先于追最新论文。
- **理由**：Agent 系统行为高度依赖真实 API 表现，编造或假设会让压缩 / 预算
  / 缓存策略系统性失准。

### III. 文档即硬约束 (Documentation is a Hard Constraint)

- 已确认的设计文档与 plan（如
  `docs/agent-state-machine-and-chain-of-responsibility-design.md`、已 approve
  的 implementation plan）是 hard constraint，不是参考意见。
- **偏离 MUST confirm**：实施中发现需要偏离文档，MUST 先 AskUserQuestion 与
  用户确认，不允许"我觉得这个 §X 可以不做"式自行缩减。
- **同步更新**：重大功能落地后 MUST 同步更新对应设计文档；文档版本号 +
  变更历史 + commit 三者对齐；测试数、行数等数字 MUST 与代码现状一致。
- **理由**：本项目文档即架构来源（design doc 驱动实现），文档与代码漂移会让
  后续所有工作失去基准。

### IV. 测试纪律 (Test Discipline — NON-NEGOTIABLE)

- 每次代码改动后 MUST 跑 `python3 -m pytest -q`，全过才算通过；不允许跳过测试。
- **stub MUST 明确标注**：handler / function 写 `return HandlerResult()` 或
  `...` 时，docstring MUST 写 `# intentionally stubbed: <reason>`，不允许
  悄悄 no-op。
- **回退路径同等严谨**：fallback 路径与主路径 MUST 同样严格测试，不能因为
  "反正不走"就放水（参考 `SimpleTokenCounter` v2 启发式回退的校准）。
- **测试范围匹配改动范围**：小改动只跑相关单测即可，不启动长任务；改动触及
  哪一层就测哪一层。
- **未覆盖区域 MUST 显式标注**：如 `web/app.py`（Streamlit UI）不在 pytest
  覆盖范围，需手动验证并记录。
- **理由**：Agent 系统状态多、路径组合多，没有测试纪律就无法重构。

### V. 不偷偷缩范围 (No Silent Scope Shrink)

- 用户说"完整实现 / 方案 A"是硬要求，不是参考意见。工作量再大、风险再高
  也不是自行缩减的依据。
- **大任务先列 plan**：>500 行或 6+ 步的任务，MUST 先在 plan mode 列清单
  （每步 estimated effort + 完成定义），等用户 approve 再写代码。
- **缩 scope MUST confirm**：实施中若要窄化方案，MUST AskUserQuestion 与用户
  确认，不允许悄悄换成小方案。
- **每步报告三件套**：完成什么 / 测试结果 / 偏差说明，不攒到最后。
- **范围外先报告再动手**：发现范围外的 bug，先报告再决定是否处理，不连环
  自修、不扩范围。
- **理由**：silent 缩 scope 是历史上 Plan A 翻车的根因；用户宁可花更多 token
  也要完整实现。

### VI. 防御式务实工程 (Defensive & Pragmatic Engineering)

- 面向真实 API 的不稳定与边界，所有外部调用 MUST 有降级、重试、熔断与
  质量校验。
- **必填防御**：LLM 调用分类错误处理（硬重试 / 软重试 / 失败三分）、流式
  中断重试、摘要质量检查（长度 / 泄漏 / 重复 / 压缩比）、PTL 剥洋葱超限重试。
- **同一逻辑一份实现**：Fork 模式与旧模式共用一份压缩提示词模板，只换
  builder；避免双份维护漂移。
- **硬限制优于软限制**：Preserved Head 用硬 stop，不软超 budget；零截断零例外。
- **理由**：Agent 链路上每一环都可能失败，务实防御比优雅抽象优先级更高。

## 技术约束 (Technical Constraints)

- **语言 / 版本**：Python 3.11，`uv` 管理虚拟环境。
- **依赖白名单**：`anthropic` / `openai` / `zhipuai` / `pydantic` / `streamlit`
  / `python-dotenv` 及必要工具库。新增框架级依赖前 MUST justify。
- **LLM Router 统一入口**：所有 LLM 调用经 `agent_core/llm/router.py`；
  `router.invoke()` 收归所有非流式调用，业务代码 MUST NOT 绕过 router 直连 SDK。
- **多 Provider 已验证事实**（变更前 MUST 重新实测）：
  - Anthropic：system prompt MUST 顶层传；支持 thinking blocks；支持
    `cache_control` 与 `tool_choice`。
  - GLM：不支持 `cache_control` marker（自动 prefix matching）；不支持
    Claude thinking blocks API；GLM-5.1 流式偶发断流 MUST 重试。
  - OpenAI / Zhipu：system prompt 走 messages 首条；thinking 与 tool_choice
    行为以实测为准。
- **UI**：Streamlit，主线入口 `web/app.py`；`web/app_langgraph.py` 仅对比版，
  未经允许不动。
- **持久化**：JSONL 会话存储（`agent_core/session/`）；usage baseline 用
  `asdict(usage)`，不加额外字段。

## 开发流程与质量门 (Development Workflow & Quality Gates)

- **改动 → 测试 → 报告**：每步改动跑相关测试 → 报告三件套
  （完成什么 / 测试结果 / 偏差说明）。
- **Commit 规范**：原子化提交，一个 commit 一件事；message 格式
  `type: description`（`feat` / `fix` / `docs` / `refactor` / `test` / `data`）。
- **分支**：master 稳定，feature 分支开发重大功能；当前活跃分支随阶段调整。
- **UI 测试边界**：主线 UI（`web/app.py`）由用户手动验证；backend / 逻辑层
  由 AI 写测试与逻辑。
- **文档同步门**：功能落地后，对应 design doc 章节与"开发规则汇总"MUST
  同步更新才能视为完成。
- **Claude Code 源码参考纪律**：只看
  `/Users/fanyunxu/Desktop/myproject/ailearning/claude-code-analysis/` 下非
  minified 源码；MUST NOT 读
  `/usr/local/lib/node_modules/@anthropic-ai/claude-code/` 下 minified `cli.js`。

## Governance

- **最高优先级**：本 Constitution 在所有开发实践与项目文档之上。冲突时以本
  文件为准，或发起修正案。
- **修正案流程**：
  1. 在 PR / 改动描述中写明：被改原则、改动理由、迁移计划。
  2. 用户 approve 后才能合入。
  3. 同步 propagate 到 plan / spec / tasks / checklist 模板与运行时文档。
- **版本策略（SemVer）**：
  - **MAJOR**：删除或重新定义既有原则（向后不兼容）。
  - **MINOR**：新增原则 / 章节或实质性扩展指引。
  - **PATCH**：措辞、错别字、非语义澄清。
- **合规审查**：每次 `/speckit-plan` 的 Constitution Check MUST 对照本文
  Core Principles 逐条核对；复杂度超标 MUST 在 plan 的 Complexity Tracking
  表中 justify。
- **运行时开发指引**：
  - `docs/agent-dev-开发规则与经验汇总-2026-06-18.md`（规则汇总）
  - `docs/agent-state-machine-and-chain-of-responsibility-design.md`
    （状态机 + 责任链设计，hard constraint）

**Version**: 1.0.0 | **Ratified**: 2026-07-05 | **Last Amended**: 2026-07-05
