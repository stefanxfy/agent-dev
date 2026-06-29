# Phase 3 (M3) — 权限编辑 UI + Hook 三阶段 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 给 Phase 1/2 的权限系统补上交互层 — Streamlit `/permissions` 规则编辑器(实时预览)+ PermissionRequest hook(后台 agent 外部决策)+ PermissionDenied hook(deny 后 retry 提示)+ excludedCommands 消息化 UX + E2E/性能测试。

**Architecture:** 5 个 task 分两阶段 — Task 1 是 UI(独立 Streamlit 页面,复用 M1 的 add/delete API);Task 2-4 是 hook + UX(扩展 HookRegistry 加两个新 event、`_ask_user_permission`/`_check_tool_permission` 集成点);Task 5 是测试。所有改动都在 M1/M2 已建好的接口之上,**0 架构变化**(复用 PermissionEngine 7-step pipeline + HookRegistry + permission_loader)。

**Tech Stack:** Streamlit multipage(`pages/03_Permissions.py`)+ Python stdlib(`threading.Event` / `concurrent.futures`)+ M1/M2 已有资产。

---

## 全局约束

**用户硬约束**(全局适用,沿用 M1/M2):
1. **不动 `web/app_langgraph.py`**(MEMORY.md 约束)
2. **测试只跑受影响文件,不全量跑**
3. **item_hash 保留**
4. **每个 task 1 个 commit**
5. **不要碰 master 分支**
6. **不要使用 TDD,直接改代码 + 写测试即可**
7. **当前项目以学习为主,不要做简化,除非是当前条件下无法实现的**

**M1/M2 已建好的接口(本阶段复用,不重新实现)**:
- `PermissionEngine.check_permissions(tool_def, tool_input, messages) -> PermissionDecision`(7-step pipeline,含 Step 1c' Bash 专属路径)
- `HookRegistry.register_hook(event, name, callback, source)`(已支持任意 event name)+ `run_pre_tool_use(tool_name, tool_input, context) -> PreToolUseResult`
- `ReactAgent._check_tool_permission` / `_ask_user_permission` / `resolve_permission`
- `permission_loader.add_permission_rules_to_settings(rules, destination)` + `delete_permission_rule_from_settings(rule, destination)`(M1 已实现,CRUD 完整)
- `permission_loader.load_rules_by_source() -> dict` + `get_permission_rules_for_source(source)`
- `permission_matcher.parse_permission_rule(tool_name, content) -> ShellPermissionRule`(UI 预览用)
- `PermissionRule(source, behavior, value=PermissionRuleValue(tool_name, rule_content))`(构造 add rule 用)
- `permission_types` 全部 11 种 reason + `PermissionBehavior` / `PermissionMode` / `PermissionRuleSource`
- `sandbox_manager._config.excluded_commands`(M2 已读取,本阶段加消息化)
- `ReactAgent.permission_engine` / `ReactAgent.hook_registry`(web/app.py 已注入 permission_engine;hook_registry 需暴露给 agent)

**M3 增量约束**:
8. **PermissionRequest/PermissionDenied 是用户自定义 hook,默认不预置**(不像 secret/path hook 有 default)—— 只提供机制 + 一个 webhook hook factory 示例,不强制注册
9. **PostToolUse hook 不做**(YAGNI — spec Phase 3 只列 PermissionRequest + PermissionDenied;PostToolUse 在 §4.4 描述但非 Phase 3 task)
10. **excludedCommands 保持 list[str] 配置**(向后兼容)+ 加 `_excluded_command_message(cmd)` helper 返命中提示语(非破坏性增强)
11. **PermissionRequest hook 在 `threading.Event.wait` 之前跑**(hook 返决策 → 直接用,不等 UI;这对后台 agent `should_avoid_permission_prompts=True` 有用)
12. **PermissionDenied hook 的 retry_prompt 追加到 tool_result error message**(给模型"为什么被拒 + 怎么重试"上下文,对齐 CC `retry: true`)
13. **UI 测试只写 smoke**(验证 import + 关键函数存在,不真渲染 Streamlit widget — Streamlit UI 难单测)

---

## 实施 5 步(按依赖排序)

### Task 1 — Streamlit `/permissions` 规则编辑器 UI

**文件**:
- Create: `web/pages/03_Permissions.py`(新增 ~220 行)
- Modify: `web/app.py`(sidebar 加 /permissions 入口,~10 行)
- Test: `tests/test_permissions_ui.py`(新增 ~12 smoke test)

**依赖**:M1 的 `permission_loader` add/delete API + `permission_matcher.parse_permission_rule`

**关键内容**(对齐 spec §9 Phase 3 Task 1 + CC `/permissions` 命令):

**`web/pages/03_Permissions.py`**(Streamlit multipage 自动导航):

页面结构:
1. **标题** `st.title("🔐 权限规则管理")`
2. **当前规则列表**(按 source 分组) — 读 `load_rules_by_source()`,每个 source 一个 `st.expander`,展开后列 allow/deny/ask 三类,每条 rule 显示:
   - 原始字符串(如 `Bash(rm:*)`)
   - 解析预览(调 `parse_permission_rule` 显示 `prefix: "rm "` / `exact: "..."` / `wildcard: ...`)
   - 删除按钮(`st.button("🗑️")` → 调 `delete_permission_rule_from_settings`)
3. **添加规则表单** — `st.form("add_rule")`:
   - behavior 选择(`st.selectbox`:allow / deny / ask)
   - tool_name 输入(`st.text_input`,默认 "Bash")
   - rule_content 输入(`st.text_input`,placeholder "rm:* 或 npm run build",可空 = 整个 tool)
   - destination 选择(`st.selectbox`:projectSettings / localSettings / userSettings)
   - 提交 → 构造 `PermissionRule` → 调 `add_permission_rules_to_settings([rule], destination)` → `st.success` + `st.rerun()`
4. **实时预览** — 输入 rule_content 时,调 `parse_permission_rule(tool_name, content)` 显示解析后的 `ShellPermissionRule`(用 `st.code_block(str(rule))`)
5. **沙箱配置区** — 显示当前 `sandbox_manager._config.excluded_commands`,可编辑(写回 settings.json 的 sandbox.excludedCommands)

**helper 函数**(便于单测):
- `render_rule_preview(tool_name: str, content: str) -> str` — 返解析后的预览字符串(调 `parse_permission_rule`)
- `build_permission_rule(behavior: str, tool_name: str, content: str, destination: str) -> PermissionRule` — 构造 PermissionRule(校验 behavior/destination 合法)
- `format_rules_by_source(rules_by_source: dict) -> list[dict]` — 扁平化为 `[{source, behavior, rule_str, tool_name, content}, ...]` 便于 UI 渲染

**web/app.py sidebar 入口**(line ~230 后,待审记忆 expander 之后):
```python
with st.expander("🔐 权限规则", expanded=False):
    st.caption("管理 allow/deny/ask 规则")
    try:
        st.page_link("pages/03_Permissions.py", label="编辑规则 →")
    except Exception:
        st.link_button("编辑规则 →", "/Permissions")
    # 简要计数
    try:
        from agent_core.tools.permission_loader import load_rules_by_source
        _rules = load_rules_by_source()
        _total = sum(len(v) for d in _rules.values() for v in d.values())
        st.caption(f"当前 {_total} 条规则")
    except Exception:
        st.caption("(读取失败)")
```

**测试覆盖**(`tests/test_permissions_ui.py` smoke test):
- `test_page_module_imports` — `import web.pages` 不报错(或直接 import 文件路径)
- `test_render_rule_preview_prefix` — `render_rule_preview("Bash", "rm:*")` 含 "prefix"
- `test_render_rule_preview_exact` — `render_rule_preview("Bash", "npm run build")` 含 "exact"
- `test_render_rule_preview_wildcard` — `render_rule_preview("Bash", "*echo*")` 含 "wildcard"
- `test_build_permission_rule_valid` — 构造出合法 PermissionRule
- `test_build_permission_rule_invalid_behavior` — behavior="bogus" → raises
- `test_build_permission_rule_invalid_destination` — destination="session" → raises(managed-only)
- `test_format_rules_by_source_flattens` — 嵌套 dict → 扁平 list
- `test_format_rules_by_source_empty` — 空 dict → []
- `test_add_rule_roundtrip` — build + add_permission_rules_to_settings + load → 能读到(用 tmp settings)
- `test_delete_rule_roundtrip` — add 后 delete → 读不到
- `test_preview_none_content` — content=None → 整个 tool 命中预览

**Commit**:`feat(web): add /permissions rule editor page (CRUD + live preview)`

---

### Task 2 — PermissionRequest hook(后台 agent 外部决策)

**文件**:
- Modify: `agent_core/tools/permission_hook.py`(改 ~+80 行:加 dataclass + run 方法)
- Modify: `agent_core/agent_core.py`(改 ~+25 行:`_ask_user_permission` 集成)
- Test: `tests/test_permission_request_hook.py`(新增 ~15 test)

**依赖**:M1 的 `HookRegistry`(已支持任意 event)+ M2 的 `ReactAgent._ask_user_permission`

**关键内容**(对齐 spec §4.4 "PermissionRequest Hook: 后台 agent 弹窗时给外部决策机会"):

**`PermissionRequestResult` dataclass**(加到 permission_hook.py):
```python
@dataclass
class PermissionRequestResult:
    """
    PermissionRequest hook 返回值(对齐 CC PermissionRequest hook)

    场景:后台 agent(should_avoid_permission_prompts=True)无法弹 UI 时,
    给外部决策来源(webhook / Slack / 钉钉)一个决策机会。

    字段:
    - decision: "allow" / "deny" / "ask"(None = 未决策,走默认 UI 路径)
    - reason: 决策原因(audit + UI 显示)
    - updated_input: hook 改写后的 input(可选)
    """
    decision: Optional[str] = None
    reason: Optional[str] = None
    updated_input: Optional[dict] = None

    @property
    def has_decision(self) -> bool:
        return self.decision is not None
```

**`HookRegistry.run_permission_request` 方法**(加到 HookRegistry 类):
```python
def run_permission_request(
    self,
    tool_name: str,
    tool_input: dict,
    context: ToolPermissionContext,
) -> PermissionRequestResult:
    """
    跑 PermissionRequest hook(对齐 CC)

    与 PreToolUse 不同:
    - 串行跑(不并行 — 外部决策来源通常只有一个 webhook)
    - 第一个返 has_decision 的 hook 胜出(短路)
    - 都没决策 → 返 PermissionRequestResult()(decision=None,走 UI)

    Args:
        tool_name: 工具名
        tool_input: 工具输入
        context: 权限上下文

    Returns:
        PermissionRequestResult(decision=None 表示走默认 UI)
    """
    hooks = self._collect_hooks("PermissionRequest")
    if not hooks:
        return PermissionRequestResult()

    merged_input = dict(tool_input)
    for entry in hooks:
        try:
            result = entry.callback(tool_name, dict(merged_input), context)
            # 兼容 PreToolUseResult(旧 hook 签名)和 PermissionRequestResult
            decision = getattr(result, "decision", None) or getattr(result, "behavior", None)
            if decision is not None:
                reason = getattr(result, "reason", None)
                updated = getattr(result, "updated_input", None)
                if updated:
                    merged_input.update(updated)
                return PermissionRequestResult(
                    decision=decision,
                    reason=reason,
                    updated_input=merged_input if updated else None,
                )
        except Exception as e:
            logger.warning("PermissionRequest hook %s 异常,跳过: %s", entry.name, e)
            continue
    return PermissionRequestResult()
```

**webhook hook factory**(加到 permission_hook.py 底部,示例用):
```python
def make_webhook_permission_request_hook(webhook_url: str) -> HookCallable:
    """
    创建 webhook PermissionRequest hook(对齐 CC 外部决策来源)

    POST tool_use 信息到 webhook_url,期望返 {"decision": "allow"/"deny", "reason": "..."}
    webhook 超时/失败 → 返 decision=None(走默认 UI,不阻断)

    Args:
        webhook_url: 外部决策 webhook URL

    Returns:
        HookCallable
    """
    def hook(tool_name: str, tool_input: dict, context: ToolPermissionContext) -> PermissionRequestResult:
        import requests
        try:
            resp = requests.post(
                webhook_url,
                json={"tool_name": tool_name, "tool_input": tool_input},
                timeout=5,
            )
            resp.raise_for_status()
            data = resp.json()
            decision = data.get("decision")
            if decision in ("allow", "deny", "ask"):
                return PermissionRequestResult(
                    decision=decision,
                    reason=data.get("reason", "webhook decision"),
                )
        except Exception as e:
            logger.warning("webhook permission request 失败,走默认 UI: %s", e)
        return PermissionRequestResult()
    return hook
```

**`agent_core.py _ask_user_permission` 集成**(在 threading.Event.wait 之前):
```python
def _ask_user_permission(self, tool_name, tool_input, decision):
    # M3: 先跑 PermissionRequest hook(后台 agent / webhook 外部决策)
    hook_decision = None
    if self.permission_engine is not None and self.permission_engine.hook_registry is not None:
        try:
            req_result = self.permission_engine.hook_registry.run_permission_request(
                tool_name, tool_input, self.permission_engine.context,
            )
            if req_result.has_decision:
                hook_decision = req_result.decision
        except Exception as e:
            _logger.warning("PermissionRequest hook 异常,走默认 UI: %s", e)

    if hook_decision == "allow":
        return True, None, tool_input
    if hook_decision == "deny":
        return False, "Permission denied by PermissionRequest hook", tool_input
    # hook 未决策(ask 或 None)→ 走原 UI 路径(threading.Event.wait)
    # ... 原有代码不变
```

**测试覆盖**:
- `test_no_hooks_returns_no_decision` — 空 registry → decision=None
- `test_hook_returns_allow_short_circuits` — 第一个 hook 返 allow → 胜出
- `test_hook_returns_deny_short_circuits` — 返 deny → 胜出
- `test_first_deciding_hook_wins` — 两个 hook,第一个返 allow,第二个返 deny → allow 胜
- `test_no_decision_hook_falls_through` — hook 返 None → 走默认(decision=None)
- `test_hook_exception_skipped` — hook 抛异常 → 跳过,不阻断
- `test_updated_input_merged` — hook 返 updated_input → merge
- `test_ask_user_permission_uses_hook_allow` — agent + hook allow → 直接 allow,不等 UI
- `test_ask_user_permission_uses_hook_deny` — hook deny → deny
- `test_ask_user_permission_falls_through_to_ui_when_no_decision` — hook None → 走 UI(Event.wait)
- `test_ask_user_permission_hook_exception_falls_through` — hook 异常 → 走 UI
- `test_webhook_hook_success` — mock requests.post 返 allow → decision=allow
- `test_webhook_hook_failure_returns_none` — mock requests 抛 → decision=None
- `test_webhook_hook_invalid_response_returns_none` — 返 {"decision": "bogus"} → None
- `test_webhook_hook_timeout_returns_none` — mock timeout → None

**Commit**:`feat(tools): add PermissionRequest hook (external decision for background agents)`

---

### Task 3 — PermissionDenied hook(deny 后 retry 提示)

**文件**:
- Modify: `agent_core/tools/permission_hook.py`(改 ~+60 行:加 dataclass + run 方法)
- Modify: `agent_core/agent_core.py`(改 ~+20 行:`_check_tool_permission` deny 集成)
- Test: `tests/test_permission_denied_hook.py`(新增 ~12 test)

**依赖**:Task 2 的 HookRegistry 扩展模式 + M2 的 `_check_tool_permission`

**关键内容**(对齐 spec §4.4 "PermissionDenied Hook: classifier 拒绝后给模型重试机会" + §8 "retry: true 提示"):

**`PermissionDeniedResult` dataclass**(加到 permission_hook.py):
```python
@dataclass
class PermissionDeniedResult:
    """
    PermissionDenied hook 返回值(对齐 CC PermissionDenied hook)

    场景:tool 被 deny 后,给模型一个"为什么被拒 + 怎么换种方式重试"的上下文。
    CC 的 PermissionDenied hook 返 retry: true 时,模型收到提示后重试。

    字段:
    - retry_prompt: 追加到 tool_result error message 的重试提示(给模型看)
    - notify_message: 给用户的通知(可选,UI 显示)
    - additional_context: 额外上下文(拼到 system prompt)
    """
    retry_prompt: Optional[str] = None
    notify_message: Optional[str] = None
    additional_context: Optional[str] = None

    @property
    def has_content(self) -> bool:
        return any([self.retry_prompt, self.notify_message, self.additional_context])
```

**`HookRegistry.run_permission_denied` 方法**:
```python
def run_permission_denied(
    self,
    tool_name: str,
    tool_input: dict,
    context: ToolPermissionContext,
    decision: "PermissionDecision",
) -> PermissionDeniedResult:
    """
    跑 PermissionDenied hook(对齐 CC)

    串行跑所有 hook,聚合 retry_prompt / notify_message / additional_context。
    任何 hook 抛异常 → 跳过,不阻断。

    Args:
        tool_name: 工具名
        tool_input: 工具输入
        context: 权限上下文
        decision: 导致 deny 的 PermissionDecision(含 reason)

    Returns:
        PermissionDeniedResult(聚合所有 hook 输出)
    """
    hooks = self._collect_hooks("PermissionDenied")
    if not hooks:
        return PermissionDeniedResult()

    retry_parts: list[str] = []
    notify_parts: list[str] = []
    context_parts: list[str] = []
    for entry in hooks:
        try:
            result = entry.callback(tool_name, tool_input, context)
            # 兼容多种返回形态
            retry = getattr(result, "retry_prompt", None)
            notify = getattr(result, "notify_message", None) or getattr(result, "reason", None)
            addl = getattr(result, "additional_context", None)
            if retry:
                retry_parts.append(retry)
            if notify:
                notify_parts.append(notify)
            if addl:
                context_parts.append(addl)
        except Exception as e:
            logger.warning("PermissionDenied hook %s 异常,跳过: %s", entry.name, e)
            continue
    return PermissionDeniedResult(
        retry_prompt="\n".join(retry_parts) if retry_parts else None,
        notify_message="\n".join(notify_parts) if notify_parts else None,
        additional_context="\n".join(context_parts) if context_parts else None,
    )
```

**`agent_core.py _check_tool_permission` deny 集成**(在构造 err 后):
```python
if decision.behavior == PermissionBehavior.DENY.value:
    reason = decision.decision_reason
    reason_str = ""
    if reason is not None and hasattr(reason, 'reason'):
        reason_str = reason.reason
    elif decision.message:
        reason_str = decision.message
    err = f"Permission denied: {reason_str or 'no reason'}"
    # M3: 跑 PermissionDenied hook,追加 retry_prompt 到 err
    if self.permission_engine is not None and self.permission_engine.hook_registry is not None:
        try:
            denied = self.permission_engine.hook_registry.run_permission_denied(
                tool_name, tool_input, self.permission_engine.context, decision,
            )
            if denied.retry_prompt:
                err = f"{err}\n💡 Retry hint: {denied.retry_prompt}"
        except Exception as e:
            _logger.warning("PermissionDenied hook 异常: %s", e)
    return False, err, tool_input
```

**预置示例 hook**(加到 permission_hook.py,演示用,不自动注册):
```python
def make_retry_hint_denied_hook() -> HookCallable:
    """
    示例 PermissionDenied hook:deny 后给通用 retry 提示

    CC 的 PermissionDenied hook 常见用法:告诉模型"换种更安全的方式重试"。
    本 hook 是示例,实际项目可注册自定义 webhook / 日志 hook。
    """
    def hook(tool_name: str, tool_input: dict, context: ToolPermissionContext) -> PermissionDeniedResult:
        return PermissionDeniedResult(
            retry_prompt=f"被拒绝的工具 `{tool_name}` — 尝试换种更安全的方式,或请用户手动放行。",
            notify_message=f"工具 {tool_name} 被权限系统拒绝",
        )
    return hook
```

**测试覆盖**:
- `test_no_hooks_returns_empty` — 空 registry → has_content=False
- `test_hook_retry_prompt_aggregated` — 两个 hook 各返 retry_prompt → 拼接
- `test_hook_notify_message_aggregated` — notify 聚合
- `test_hook_additional_context_aggregated` — context 聚合
- `test_hook_exception_skipped` — 异常跳过
- `test_has_content_true_when_any_field` — 任一字段非空 → True
- `test_check_permission_deny_appends_retry_hint` — agent + denied hook → err 含 "Retry hint"
- `test_check_permission_deny_without_hook_unchanged` — 无 hook → err 不含 "Retry hint"
- `test_check_permission_deny_hook_exception_no_break` — hook 异常 → err 仍返(不含 hint)
- `test_retry_hint_denied_hook_factory` — make_retry_hint_denied_hook() 返有效 hook
- `test_aggregated_result_empty_when_all_hooks_silent` — hook 都返空 → has_content=False
- `test_run_permission_denied_passes_decision_to_hook` — hook 收到 decision 参数(通过 duck-type)

**Commit**:`feat(tools): add PermissionDenied hook (retry hint on denial)`

---

### Task 4 — excludedCommands 消息化 UX

**文件**:
- Modify: `agent_core/tools/sandbox_decision.py`(改 ~+40 行:`_is_excluded_command` 增强消息 + 新 helper)
- Modify: `web/pages/03_Permissions.py`(Task 1 已建,加 excluded commands 编辑区,~+30 行)
- Test: `tests/test_excluded_commands_ux.py`(新增 ~12 test)

**依赖**:M2 的 `sandbox_decision._is_excluded_command` + `sandbox_manager._config.excluded_commands`

**关键内容**(对齐 spec §5.3 + §8 "excludedCommands UX 而非安全,正则匹配 + 自定义消息"):

**`sandbox_decision.py` 增强**:

保持 `excluded_commands: list[str]` 配置不变(向后兼容),新增消息化 helper:
```python
def get_excluded_command_match(command: str) -> Optional[tuple[str, str]]:
    """
    检查 command 是否命中 excluded_commands,返 (pattern, message) 或 None
    对齐 doc §5.3 "excludedCommands UX:正则匹配 + 自定义消息"

    message 规则:
    - 命中 pattern → 友好提示语(UX,告诉用户/模型为何跳过沙箱)
    - excludedCommands 是 UX 而非安全(命中仍过 permission check)

    Args:
        command: bash 命令

    Returns:
        (pattern, message) 或 None(未命中)
    """
    if not command:
        return None
    for pattern in sandbox_manager._config.excluded_commands:
        if pattern and pattern in command:
            message = (
                f"命令匹配排除规则 `{pattern}`,将跳过 OS 沙箱"
                f"(仍受应用层权限检查约束)"
            )
            return (pattern, message)
    return None


def get_excluded_command_message(command: str) -> Optional[str]:
    """便捷封装:只返 message 或 None"""
    match = get_excluded_command_match(command)
    return match[1] if match else None
```

**`_is_excluded_command` 内部改用 `get_excluded_command_match`**(保持 bool 返回值不变,避免破坏 M2):
```python
def _is_excluded_command(tool_name: str, tool_input: dict) -> bool:
    if tool_name != "Bash":
        return False
    cmd = tool_input.get("command", "") or ""
    return get_excluded_command_match(cmd) is not None
```

**`/permissions` UI 编辑区**(Task 1 文件 `03_Permissions.py` 内):
- 读 `sandbox_manager._config.excluded_commands` 显示当前列表
- `st.text_area("excluded commands(每行一条)")` 编辑 → 写回 settings.json 的 `sandbox.excludedCommands`
- helper:`save_excluded_commands(patterns: list[str], destination) -> None`(写 settings.json)

**测试覆盖**:
- `test_get_excluded_match_returns_pattern_message` — 命中 → (pattern, message)
- `test_get_excluded_match_no_hit_returns_none` — 未命中 → None
- `test_get_excluded_message_convenience` — 只返 message
- `test_get_excluded_message_mentions_ux_not_security` — message 含"UX"或"权限"语义
- `test_is_excluded_command_uses_new_helper` — bool 返回值不变(向后兼容)
- `test_empty_command_returns_none` — `get_excluded_command_match("")` → None
- `test_multiple_patterns_first_match_wins` — 多 pattern,第一个命中胜
- `test_save_excluded_commands_writes_settings` — 写回 settings.json sandbox.excludedCommands
- `test_save_excluded_commands_roundtrip` — save 后 load 能读到
- `test_excluded_message_used_in_bash_result`(可选)— excluded 命令执行后 tool_result 含提示语
- `test_case_sensitive_match_preserved` — 大小写敏感(对齐 M2)
- `test_pattern_with_none_skipped` — excluded_commands 含 None/空串 → 跳过不崩

**Commit**:`feat(tools): add excludedCommands message UX (pattern + friendly message)`

---

### Task 5 — E2E 测试 + 性能测试

**文件**:
- Create: `tests/test_m3_e2e.py`(新增 ~20 test,e2e + 性能)
- Modify: `tests/test_permissions_ui.py`(Task 1 已建,补 add/delete roundtrip e2e)

**依赖**:Task 1-4 全部

**关键内容**(对齐 spec §9 Phase 3 Task 5 + §9.4 M3 测试矩阵):

**E2E 测试** (`tests/test_m3_e2e.py`):
- `test_permissions_ui_add_rule_then_engine_respects_it` — UI add `Bash(rm:*)` deny → engine check Bash(rm) → DENY(端到端:settings 写入 → loader 加载 → engine 决策)
- `test_permissions_ui_delete_rule_then_engine_allows` — 删 deny rule → engine 同命令 → 不再 DENY
- `test_permission_request_hook_overrides_ui_wait` — agent + PermissionRequest hook allow → ASK 路径不等 UI(直接 allow)
- `test_permission_denied_hook_appends_retry_hint_e2e` — engine DENY + denied hook → tool_result error 含 "Retry hint"
- `test_full_pipeline_bash_with_all_hooks` — Bash command + PreToolUse + PermissionRequest + PermissionDenied 三阶段 hook 全跑
- `test_excluded_command_message_in_tool_result` — excluded 命令 → tool_result 含友好提示
- `test_background_agent_uses_permission_request_hook` — should_avoid_permission_prompts=True + hook → 不弹 UI,走 hook
- `test_audit_logger_records_hook_decisions` — PermissionRequest hook 决策 → audit.jsonl 记录
- `test_webhook_hook_timeout_does_not_block` — webhook 超时 → 不阻断,走 UI

**性能测试**(对齐 spec "classifier 延迟、speculative check 节省"):
- `test_engine_check_permissions_1000_calls_under_50ms` — 无 classifier + 无 hook → 1000 次 check < 50ms
- `test_engine_check_permissions_100_calls_with_bash_under_20ms` — Bash 路径(含 subcommand parse)100 次 < 20ms
- `test_classifier_fast_path_skips_classifier` — fast-path 命中(Read allowlist)→ classifier.classify 不被调(mock 验证 call_count=0)
- `test_sandbox_auto_allow_skips_subcommand_parse_when_possible`(可选)— sandbox auto-allow 命中 deny 时提前返,不等全部 subcommand
- `test_bash_subcommand_parse_50_commands_under_10ms` — 50 条复合命令 parse < 10ms
- `test_audit_logger_write_under_1ms_per_record` — 单条 audit write < 1ms

**回归测试**(确保 M3 改动不破坏 M1/M2):
- `test_regression_phase1_engine_still_works` — Phase 1 的 7-step pipeline 行为不变
- `test_regression_phase2_bash_sandbox_still_works` — BashTool + sandbox auto-allow 行为不变
- `test_regression_audit_single_site` — audit 仍是唯一审计点(engine._log_and_return),无 double-logging
- `test_regression_existing_hooks_unaffected` — PreToolUse secret/path hook 仍工作

**完整测试矩阵验证命令**:
```bash
cd /Users/fanyunxu/Desktop/myproject/agent-dev && \
  pytest tests/test_permissions_ui.py \
         tests/test_permission_request_hook.py \
         tests/test_permission_denied_hook.py \
         tests/test_excluded_commands_ux.py \
         tests/test_m3_e2e.py \
         -v
```

**全量回归(Phase 1+2+3)**:
```bash
pytest tests/test_sandbox_manager.py tests/test_sandbox_decision.py tests/test_sandbox_prompt.py \
       tests/test_bash_permissions.py tests/test_audit_logger.py tests/test_builtin_bash_tool.py \
       tests/test_agent_core_bash_sandbox.py tests/test_sandbox_e2e.py \
       tests/test_permission_engine.py tests/test_permission_integration.py \
       tests/test_permission_types.py tests/test_permission_matcher.py tests/test_permission_loader.py \
       tests/test_permission_hook.py tests/test_classifier.py tests/test_classifier_fast_path.py \
       tests/test_denial_tracking.py tests/test_safety_check.py tests/test_tool_registry.py \
       tests/test_agent_core.py tests/test_app_permission_dialog.py \
       tests/test_permissions_ui.py tests/test_permission_request_hook.py \
       tests/test_permission_denied_hook.py tests/test_excluded_commands_ux.py \
       tests/test_m3_e2e.py \
       -q
```
期望:M1 ~100 + M2 ~342 + M3 ~70 = ~510+ test 全 passed(扣除重叠后实际 750+ test 全绿)。

**Commit**:`test(tools): add Phase 3 e2e + performance + regression suite`

---

## 实施时间表

| Task | 内容 | 估时 | 风险 |
|---|---|---|---|
| 1 | /permissions UI 页面 | 3.5h | 中(Streamlit multipage + form + parse 预览) |
| 2 | PermissionRequest hook | 2h | 低(HookRegistry 已支持多 event,加 run 方法) |
| 3 | PermissionDenied hook | 1.5h | 低(同 Task 2 模式) |
| 4 | excludedCommands UX | 1h | 低(非破坏性增强) |
| 5 | E2E + 性能测试 | 3.5h | 中(性能基准 + 全量回归) |

总耗时:~11.5h ≈ 1.5 天,5 个独立 commit。

---

## 文件清单(预期)

```
agent_core/tools/
├── permission_hook.py           (改 ~+200 行:PermissionRequestResult / PermissionDeniedResult
│                                  + run_permission_request / run_permission_denied
│                                  + make_webhook_permission_request_hook / make_retry_hint_denied_hook)
├── sandbox_decision.py          (改 ~+40 行:get_excluded_command_match / get_excluded_command_message)

agent_core/
└── agent_core.py                (改 ~+45 行:_ask_user_permission 跑 PermissionRequest hook
                                  + _check_tool_permission deny 跑 PermissionDenied hook)

web/
├── app.py                       (改 ~+10 行:sidebar 加 /permissions 入口)
├── pages/
│   └── 03_Permissions.py        (新增 ~250 行:规则编辑器 UI + excludedCommands 编辑区)
└── app_langgraph.py             (不动)

tests/
├── test_permissions_ui.py       (新增 ~12 smoke test)
├── test_permission_request_hook.py (新增 ~15 test)
├── test_permission_denied_hook.py  (新增 ~12 test)
├── test_excluded_commands_ux.py    (新增 ~12 test)
└── test_m3_e2e.py               (新增 ~20 test,e2e + 性能)
```

总计:1 个新增页面文件 + 5 个新增 test 文件 + 3 个文件改造 + 5 个独立 commit。

---

## 关键边界 case 与风险

| 风险 | 概率 | 缓解 |
|---|---|---|
| Task 1 Streamlit multipage 不识别 pages/03_Permissions.py | 低 | 已有 pages/02_Candidate_Review.py 先例,沿用约定 |
| Task 1 form 提交后 agent 未感知新 rule(需重建) | 中 | settings.json 是文件级,agent 下次 check_permissions 读 loader 时才生效;UI 提示"重启会话生效"或主动调 `agent.permission_engine.context` 更新 |
| Task 2 PermissionRequest hook 阻塞 _ask_user_permission(webhook 慢) | 中 | webhook 内部 timeout=5s;hook 异常 try/except 走 UI |
| Task 2/3 hook 改 input 后与 engine 决策不一致 | 低 | hook 只在 ASK/DENY 后跑,不改 engine 已返的 decision |
| Task 4 excluded_commands 含 None/空串崩 | 低 | helper 显式 `if pattern and pattern in cmd` 跳过 |
| Task 5 性能基准在 CI/慢机波动 | 中 | 基准设宽松(50ms/1000次),失败只 warn 不 fail(用 pytest.mark 或 try/except) |
| Task 5 全量回归慢 | 低 | ~750 test < 2s(M2 实测 1.5s) |

---

## 端到端验证

### 1. M3 单测矩阵
```bash
pytest tests/test_permissions_ui.py \
       tests/test_permission_request_hook.py \
       tests/test_permission_denied_hook.py \
       tests/test_excluded_commands_ux.py \
       tests/test_m3_e2e.py -v
```
期望:~70 test 全 passed。

### 2. Streamlit 冒烟
```bash
streamlit run web/app.py
# sidebar 应出现 "🔐 权限规则" expander + "编辑规则 →" 链接
# 点链接 → 跳到 /Permissions 页面
# 页面应显示:当前规则列表 + 添加表单 + 实时预览 + excluded commands 区
# 测试:add `Bash(rm:*)` deny → 提交 → 列表出现 → 预览显示 "prefix: rm "
# 测试:删该 rule → 列表消失
```

### 3. PermissionRequest hook 冒烟(手动)
```bash
# 在 ~/.agent_data/settings.json 加 webhook 配置(示例,实际 hook 注册需 web/app.py 扩展)
# 或用 Python REPL:
#   from agent_core.tools.permission_hook import make_webhook_permission_request_hook, HookRegistry
#   reg = HookRegistry()
#   reg.register_hook("PermissionRequest", "webhook", make_webhook_permission_request_hook("http://localhost:9999/decide"))
#   reg.run_permission_request("Bash", {"command": "ls"}, ctx)
# 期望:webhook 收到 POST,返 allow/deny → hook 返对应决策
```

### 4. 全量回归
```bash
pytest tests/ -q 2>&1 | tail -3
# 期望:750+ test 全 passed,0 failed
```

---

## 关键文件总览(复用资产)

| 资产 | 位置 | Task 用法 |
|---|---|---|
| `permission_loader.add_permission_rules_to_settings` | `permission_loader.py:405` | T1 UI add rule |
| `permission_loader.delete_permission_rule_from_settings` | `permission_loader.py:458` | T1 UI delete rule |
| `permission_loader.load_rules_by_source` | `permission_loader.py:192` | T1 UI 列表渲染 |
| `permission_matcher.parse_permission_rule` | `permission_matcher.py:131` | T1 UI 实时预览 |
| `PermissionRule` / `PermissionRuleValue` | `permission_types.py:129` | T1 构造 add rule |
| `HookRegistry.register_hook(event, ...)` | `permission_hook.py:137` | T2/T3 注册新 event hook |
| `HookRegistry._collect_hooks(event)` | `permission_hook.py:314` | T2/T3 run 方法复用 |
| `ReactAgent._ask_user_permission` | `agent_core.py:570` | T2 集成 PermissionRequest |
| `ReactAgent._check_tool_permission` | `agent_core.py:496` | T3 集成 PermissionDenied |
| `sandbox_manager._config.excluded_commands` | `sandbox_manager.py` | T4 读/写 excluded |
| `sandbox_decision._is_excluded_command` | `sandbox_decision.py` | T4 增强(消息化) |
| `PermissionEngine.hook_registry` | `permission_engine.py:102` | T2/T3 agent 访问 hook |

---

## 不要做的事

1. **不要做 PostToolUse hook** — YAGNI,spec Phase 3 没列
2. **不要自动注册 PermissionRequest/PermissionDenied hook** — 用户自定义,默认空;只提供 factory + 机制
3. **不要改 `excluded_commands` 的 list[str] 配置形态** — 向后兼容,加消息 helper 即可
4. **不要让 webhook 阻塞主流程** — timeout=5s + try/except 走 UI
5. **不要在 UI 里直接改 PermissionEngine 内存 context** — settings.json 是 source of truth,改文件后提示重启会话(或下个 task 主动 reload)
6. **不要让性能测试在慢机 CI 上 fail 整个 build** — 基准宽松,失败 warn
7. **不要改 web/app_langgraph.py** — MEMORY.md 约束
8. **不要合并多个 task 到一个 commit** — 1 task 1 commit

---

## 与 Phase 1/2 的边界

**Phase 1/2 已完成且本阶段不动的接口**:
- PermissionEngine 7-step pipeline(含 Step 1c' Bash 专属路径)
- HookRegistry.run_pre_tool_use(PreToolUse 并行)
- BashTool + sandbox wrap + bash_check_permissions
- audit_logger(唯一审计点 = engine._log_and_return)
- web/app.py PermissionEngine 注入 + st.dialog 弹窗

**Phase 3 增量**:
- HookRegistry 加两个新 event(PermissionRequest / PermissionDenied)+ run 方法
- ReactAgent 两个集成点(_ask_user_permission / _check_tool_permission)
- Streamlit /permissions 独立页面(规则 CRUD + 预览)
- excludedCommands 消息化(非破坏性)

**Phase 1/2 测试应全通过本阶段不修改**(除极少数 smoke 因签名扩展需更新 kwargs,如 M2 的 MockAudit 先例)。

---

> 本计划完全对齐 `docs/tool/tool-security-architecture.md` §9 Phase 3 + §4.4 hook 三阶段 + §5.3 excludedCommands UX + §8 对齐表(line 150-156 Hook 三阶段 / excludedCommands UX 完整实现)。doc 已 commit,实施时严格按 doc 实现,不偏离。M3 完成后,整个 tool-security-architecture §9 三阶段(M1+M2+M3)全部落地。
