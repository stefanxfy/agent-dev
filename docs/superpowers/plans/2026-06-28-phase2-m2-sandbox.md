# Phase 2 (M2) — OS 沙箱 + BashTool 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 LLM 调 BashTool 时经过应用层 `PermissionEngine` + `bash_permissions`(subcommand 级 rule check)+ OS 沙箱(`sandbox_manager` via `@anthropic-ai/sandbox-runtime`)双层防御,并写入独立的 `audit.jsonl` 通道。

**Architecture:** 8 个 task 分两阶段实施 — Task 1-5 搭基建(sandbox manager + 决策 + prompt + bash permissions + audit logger),Task 6-8 集成(BashTool 内置实现 + agent_core 串通 + 集成测试)。沙箱复用 `@anthropic-ai/sandbox-runtime`(npm 包,subprocess 调),与 Claude Code 完全对齐。

**Tech Stack:** Python 3.11+ stdlib(subprocess / hashlib / dataclass) + `@anthropic-ai/sandbox-runtime@latest`(Node.js ≥ 18,subprocess 调 npx) + 可选 `tree-sitter-bash`(env `TREE_SITTER_BASH=true` 启用)。

---

## 全局约束

**用户硬约束**(全局适用):
1. **不动 `web/app_langgraph.py`**(MEMORY.md 已记)
2. **测试只跑受影响文件,不全量跑**
3. **item_hash 保留**(记忆系统的 `item_hash` 字段不动)
4. **每个 task 1 个 commit**(1 commit per task)
5. **不要碰 master 分支**
6. **不要使用 TDD,直接改代码 + 写测试即可**(用户偏好)
7. **当前项目以学习为主,不要做简化,除非是当前条件下无法实现的**
8. **每 task 一个独立 commit**(M1 已建立,沿用)

**Phase 1 已建好的接口**(本阶段复用,不重新实现):
- `PermissionEngine.check_permissions(tool_def, tool_input, messages) -> PermissionDecision`(7-step pipeline)
- `HaikuClassifier.classify(messages, tool_name, tool_input, context) -> ClassifierResult`(M1 是 stub,默认 `unavailable=True`)
- `HookRegistry.run_pre_tool_use(tool_name, tool_input, context) -> PreToolUseResult`
- `ToolDef(name, description, parameters, handler, category="general", check_permissions=None, requires_user_interaction=False)`
- `PermissionDecision(behavior, decision_reason, updated_input, message)`
- `PermissionBehavior.{ALLOW, DENY, ASK, PASSTHROUGH}`(值都是 str)
- `ToolPermissionContext(mode, always_allow_rules, always_deny_rules, always_ask_rules, ...)`
- `SubcommandResultsReason` / `SandboxOverrideReason` / `SafetyCheckReason` / `ClassifierReason` 已存在于 `permission_types.py`
- `ReactAgent._check_tool_permission` / `_ask_user_permission` / `resolve_permission` 已接好
- 8 source 优先级、`is_anthropic_provider` / `sandbox_enabled` / `no_settings_match` 字段已在 `ToolPermissionContext`

**Phase 2 增量约束**:
9. **不引入 pytest-asyncio** — M1 已选全 sync,M2 沿用;`bash_check_permissions` 虽是 async 但 spec 文档里 async 部分在 M3+ 才真启用(M2 用 sync 包装,内部调 classifier 时仍用现有 sync path)
10. **`TREE_SITTER_BASH=false` 默认** — 与 CC 默认一致,避免引入 tree-sitter 硬依赖;真启用路径完整实现,降级路径完整保留
11. **沙箱默认禁用** — `SandboxManager.is_sandbox_enabled()` 默认 False,需 settings.json 显式 opt-in
12. **不引入 npm 依赖到 requirements.txt** — 走 `subprocess.run(["npx", "-y", "@anthropic-ai/sandbox-runtime@latest", ...])` 透明下载,失败 graceful
13. **audit_logger 写失败不能影响主流程** — spec §4.8 明确 "audit must never block decision",try/except + log warning
14. **`dangerouslyDisableSandbox` 仅 bypass sandbox 不 bypass permission** — spec §6.3 明确,sandbox 是 OS 层兜底,permission 是应用层强制

---

## 实施 8 步(按依赖排序)

### Task 1 — `sandbox_manager.py` 沙箱适配层

**文件**:
- Create: `agent_core/tools/sandbox_manager.py`(新增 ~200 行)
- Test: `tests/test_sandbox_manager.py`(新增 ~25 test)

**依赖**:无

**关键内容**(对齐 spec §5.2 + CC `sandbox-adapter.ts:985`):

**`SandboxConfig`** (dataclass):
- `enabled: bool = False` — 总开关
- `fail_if_unavailable: bool = False` — 依赖缺失是否硬退出
- `auto_allow_bash_if_sandboxed: bool = True` — 沙箱内 bash 自动 allow(spec §5.4)
- `allow_unsandboxed_commands: bool = True` — 是否允许 `dangerously_disable_sandbox` 透传
- `network_allowed_domains: list[str] = []`
- `network_denied_domains: list[str] = []`
- `fs_allow_write: list[str] = []` / `fs_deny_write: list[str] = []`
- `fs_allow_read: list[str] = []` / `fs_deny_read: list[str] = []`
- `excluded_commands: list[str] = []` — UX 而非安全(spec §5.3 注释)

**`SandboxManager`** (单例,`__new__` 模式):
- `_instance: Optional["SandboxManager"]`
- `_initialized: bool`
- `_config: SandboxConfig`

**方法**:
- `is_sandbox_enabled() -> bool` — 4 段短路(config.enabled / 平台支持 / 依赖存在 / 初始化成功)
- `initialize() -> None` — 检查依赖;失败 graceful(除非 `fail_if_unavailable=True`)
- `wrap_with_sandbox(command, shell_path="/bin/bash", working_dir=".") -> str` — 返回 wrap-cli 命令字符串(完整 npx 命令,subprocess.run 时直接执行)
- `cleanup_after_command() -> None` — bare-git scrub + sandbox_tmp_dir mtime 过期
- `_build_runtime_config(working_dir) -> dict` — 翻译为 sandbox-runtime JSON config
- `_is_supported_platform() -> bool` — macOS(True 内建) / Linux(WSL2 检查 `/proc/version`)
- `_check_dependencies() -> bool` — `shutil.which("bwrap")` 或 darwin
- `_check_dependencies_detailed() -> dict` — 返 `{"errors": [...], "warnings": [...]}`(bwrap 缺失 → error;socat 缺失 → warning)
- `_scrub_bare_git() -> None` — bare git 目录清理(防 #29316)
- `_cleanup_sandbox_tmp_dir() -> None` — mtime > 24h 过期
- `_get_sandbox_tmp_dir() -> str` — `/tmp/claude-<uid>` mode 0o700

**全局单例**:`sandbox_manager = SandboxManager()`(模块底部)

**关键边界 case**:
- 平台不支持 → `is_sandbox_enabled()` 返 False,但 `initialize()` 仍跑(让 warning 出来)
- npx 失败 → `wrap_with_sandbox` fallback 返回原 command(spec §5.2 注释 "不沙箱化,原样返回")
- `bwrap` 不存在但 darwin → 仍 enabled(Seatbelt 内建)
- `cleanup_after_command` 异常 → log warning,不抛

**测试覆盖**:
- `test_disabled_by_default` — 不传 enabled → is_sandbox_enabled() 返 False
- `test_enabled_when_config_enabled` — config.enabled=True + 平台支持 → True
- `test_unsupported_platform_linux_non_wsl_returns_false` — mock `/proc/version` 不含 microsoft
- `test_unsupported_platform_returns_false_for_windows` — mock sys.platform="win32" → False
- `test_wrap_with_sandbox_disabled_returns_command_unchanged` — config.enabled=False → 原 command
- `test_wrap_with_sandbox_enabled_returns_npx_prefix` — 含 `npx -y @anthropic-ai/sandbox-runtime@latest wrap`
- `test_wrap_with_sandbox_includes_config_json` — `--config {shlex.quote(config_json)}`
- `test_wrap_with_sandbox_includes_command_after_dash_dash` — `-- {shlex.quote(command)}`
- `test_initialize_logs_warning_on_dependency_missing` — bwrap 不存在 + linux → log warning,initialized=False
- `test_initialize_with_fail_if_unavailable_raises_system_exit` — SystemExit(1)
- `test_initialize_succeeds_on_macos` — sys.platform="darwin" → initialized=True
- `test_check_dependencies_macos_no_bwrap_ok` — darwin + bwrap 缺失 → True(Seatbelt 内建)
- `test_check_dependencies_linux_requires_bwrap` — linux + bwrap 缺失 → False
- `test_get_sandbox_tmp_dir_creates_0o700_dir` — `os.stat(tmp).st_mode & 0o777 == 0o700`
- `test_cleanup_after_command_runs_when_enabled` — smoke test,disabled 时 no-op
- `test_scrub_bare_git_finds_bare_git_dirs` — 创建 fake `foo.git/` 目录,调 scrub,验证被删
- `test_cleanup_sandbox_tmp_dir_removes_old_dirs` — 创建 old mtime dir,验证被删
- `test_build_runtime_config_includes_filesystem_and_network` — dict 结构正确
- `test_singleton_returns_same_instance` — `SandboxManager() == SandboxManager()`
- `test_singleton_config_independent_between_instances` — 不允许重置 config(单例)
- `test_excluded_commands_matched_by_substring` — pattern "git commit" 匹配 "git commit -m"
- `test_dangerously_disable_sandbox_with_allow_unsandboxed_returns_false` — should_use_sandbox 路径(spec §5.3)

**Commit**:`feat(tools): add sandbox manager (Seatbelt/bwrap + bare-git scrub + cleanup)`

---

### Task 2 — `sandbox_decision.py` per-tool 沙箱判定

**文件**:
- Create: `agent_core/tools/sandbox_decision.py`(新增 ~60 行)
- Test: `tests/test_sandbox_decision.py`(新增 ~15 test)

**依赖**:T1(用 `sandbox_manager`)

**关键内容**(对齐 spec §5.3 + CC `shouldUseSandbox`):

**`should_use_sandbox(tool_name: str, tool_input: dict) -> bool`**:
- 4 段短路:
  1. `not sandbox_manager.is_sandbox_enabled()` → False
  2. `tool_input.get("dangerously_disable_sandbox") is True and sandbox_manager._config.allow_unsandboxed_commands` → False(模型主动绕过 + 用户允许)
  3. `tool_name not in ("Bash", "Read", "Write", "Edit")` → False(calc/search 不需要)
  4. `_is_excluded_command(tool_name, tool_input)` → False(用户排除)

**`_is_excluded_command(tool_name, tool_input) -> bool`**:
- 只对 `tool_name == "Bash"` 检查
- `cmd = tool_input.get("command", "")`
- 遍历 `sandbox_manager._config.excluded_commands`,sub-string match
- UX 而非安全(spec §5.3 注释)

**测试覆盖**:
- `test_calc_never_uses_sandbox` — tool_name="calc" → False(即使 sandbox enabled)
- `test_search_never_uses_sandbox` — 同上
- `test_bash_uses_sandbox_when_enabled` — sandbox enabled + tool="Bash" + 无 disable → True
- `test_dangerously_disable_sandbox_skips_sandbox_when_allowed` — `dangerously_disable_sandbox=True` + config.allow_unsandboxed_commands=True → False
- `test_dangerously_disable_sandbox_still_uses_sandbox_when_not_allowed` — config.allow_unsandboxed_commands=False → 仍 True
- `test_excluded_command_skips_sandbox` — excluded_commands=["git commit"] + cmd 含 "git commit" → False
- `test_excluded_command_substring_match` — pattern "npm" 匹配 "npm install"
- `test_read_uses_sandbox` — tool="Read" + sandbox enabled → True
- `test_write_uses_sandbox` — tool="Write" → True
- `test_edit_uses_sandbox` — tool="Edit" → True
- `test_sandbox_disabled_returns_false_for_all_tools` — 全 False
- `test_excluded_command_check_is_case_sensitive` — spec 用 substring match,大小写敏感
- `test_non_bash_tool_excluded_command_returns_false` — 即使 tool="Read" + excluded_commands=["Read"] → False(spec §5.3 注释 "只对 Bash")

**Commit**:`feat(tools): add sandbox decision (should_use_sandbox + excluded_commands)`

---

### Task 3 — `sandbox_prompt.py` 沙箱规则 prompt 注入

**文件**:
- Create: `agent_core/tools/sandbox_prompt.py`(新增 ~100 行)
- Test: `tests/test_sandbox_prompt.py`(新增 ~12 test)

**依赖**:T1(读 `sandbox_manager._config`)

**关键内容**(对齐 spec §5.4 + CC `BashTool/prompt.ts:172-273 getSimpleSandboxSection`):

**`get_sandbox_prompt_section() -> str`**:
- 如果 `not sandbox_manager.is_sandbox_enabled()` → 返 `""`
- 否则返回 CC 同款 prompt 模板,变量替换:
  - `tmpdir_literal = sandbox_manager._get_sandbox_tmp_dir()` — 字面化(非 `$TMPDIR`),便于跨用户 prompt cache
  - `strict_mode = not cfg.allow_unsandboxed_commands`
  - `fs_read = cfg.fs_allow_read or []`
  - `fs_write_allowed = cfg.fs_allow_write or ['.', tmpdir_literal]`
  - `network_allowed = cfg.network_allowed_domains or []`
- strict_mode 追加"STRICT MODE"块,告知 LLM 不允许 `dangerously_disable_sandbox`

**Prompt 关键约束**(spec §5.4 明确):
- 沙箱默认启用,告诉 LLM 不要主动设 `dangerously_disable_sandbox`
- 失败时才用,要解释原因("Operation not permitted" / "Access denied" / Network failure / Unix socket error)
- 警告不要建议加 `~/.bashrc` / `~/.zshrc` / `~/.ssh` / 凭证文件到 allowlist
- 临时文件必须用 `{tmpdir_literal}` 字面路径,不要 `$TMPDIR`

**测试覆盖**:
- `test_disabled_returns_empty_string` — sandbox 禁用 → `""`
- `test_enabled_returns_prompt_with_filesystem_section` — 含 `"Filesystem:"` / `"read"` / `"write"`
- `test_enabled_returns_prompt_with_network_section` — 含 `"Network:"`
- `test_prompt_includes_tmpdir_literal_path` — 含 `/tmp/claude-` 路径
- `test_prompt_does_not_use_dollar_tmpdir` — 不含 `$TMPDIR`
- `test_strict_mode_adds_strict_section` — `allow_unsandboxed_commands=False` → 含 `"STRICT MODE"`
- `test_non_strict_mode_omits_strict_section` — 默认无 `"STRICT MODE"`
- `test_prompt_explains_dangerously_disable_sandbox_when_to_use` — 含 sandbox-caused failure 列表
- `test_prompt_warns_against_sensitive_paths` — 含 `~/.bashrc` / `~/.ssh` 警告
- `test_prompt_uses_configured_fs_read` — config.fs_allow_read=["/tmp/test"] → prompt 含
- `test_prompt_uses_configured_network_allowed` — config.network_allowed_domains=["api.example.com"] → prompt 含
- `test_prompt_caches_across_users` — 同一 tmpdir path → 同一 prompt 字符串(便于 cache)

**Commit**:`feat(tools): add sandbox prompt section (system prompt injection)`

---

### Task 4 — `bash_permissions.py` Bash 工具专属权限(双路径 + classifier 集成 + bare-git scrub)

**文件**:
- Create: `agent_core/tools/bash_permissions.py`(新增 ~250 行)
- Test: `tests/test_bash_permissions.py`(新增 ~35 test)

**依赖**:T2(用 `should_use_sandbox`)+ T3(用 `get_sandbox_prompt_section`)+ 现有 `HaikuClassifier` + `PermissionEngine` + `permission_matcher`(`parse_permission_rule` / `match_permission_rule`)

**关键内容**(对齐 spec §4.5 + CC `bashPermissions.ts:1663 bashToolHasPermission`):

**`Subcommand` dataclass**:
```python
@dataclass
class Subcommand:
    command: str           # "git push origin main"
    name: str              # "git"
    args: list[str]        # ["push", "origin", "main"]
    operator: str          # 连接符 ";" / "&&" / "||" / "|" / "&"
    is_redirect: bool = False   # 是否含 > < >> <<
    is_subshell: bool = False   # 是否在 $(...) / `...` 内
```

**`parse_subcommands(command: str) -> list[Subcommand]`**:
- 双路径:env `TREE_SITTER_BASH=true` → AST;否则 regex
- 返回 list (空 cmd 返 `[]`)

**`_parse_via_regex(command) -> list[Subcommand]`**:
- 用 `re.split(r"\s*(?:&&|\|\||;|\|)\s*", command)` 拆
- 每个 part 用 `shlex.split(p)` 拿 tokens
- 第一个 token = name;其余 = args
- `is_redirect = bool(re.search(r"[<>]", p))`
- `is_subshell = "$(" in p or "`" in p`

**`_parse_via_tree_sitter(command) -> list[Subcommand]`**:
- 完整实现 spec §4.5.1 示例:遍历 AST `command` / `list` / `pipeline` / `subshell` 节点
- 处理嵌套引号/命令替换/heredoc/重定向
- `try import tree_sitter_bash` 失败 → fallback regex(spec §4.5)

**`_strip_safe_wrappers(cmd: str) -> str`**:
- `SAFE_WRAPPERS = ["timeout", "time", "nice", "env", "command", "nohup"]`
- 循环剥前缀 wrapper + `FOO=bar` 变量赋值
- 例: `timeout 30 FOO=bar bazel run` → `bazel run`

**`_is_cd_command(cmd: str) -> bool`**:
- `cmd.startswith("cd ") or cmd == "cd"`

**`bash_check_permissions(tool_input, context, classifier=None) -> PermissionDecision`** (M2 同步版本):
- 流程(spec §6.3):
  - **Step 0**: `sandbox_manager.is_sandbox_enabled() and _config.auto_allow_bash_if_sandboxed and should_use_sandbox("Bash", tool_input)` → `check_sandbox_auto_allow(...)`,deny/ask 透传,allow fall through(继续 subcommand check)
  - **Step 1**: `subcommands = parse_subcommands(command)`
  - **Step 1.5**: `len(subcommands) > MAX_SUBCOMMANDS(50)` → ASK(`OtherReason("subcommand 数过多")`)
  - **Step 2**: `has_cd and has_git` → ASK(`SafetyCheckReason("cd + git 组合可能加载恶意 .git/config (#29316 bare-git scrub)")`)
  - **Step 3**: classifier speculative — M2 简化:同步调(不为 classifier 引入异步);若 `classifier and context.is_anthropic_provider`,同步 `classify()` 拿结果;否则跳过
  - **Step 4**: per-subcommand `_check_single_command`,任一 DENY/ASK → 立即返(`SubcommandResultsReason`)
  - **Step 5**: 整合 classifier 决策:deny → DENY,allow → fall through
  - **Step 6**: 全 allow → ALLOW

**`_check_single_command(cmd, tool_input, context) -> PermissionDecision`**:
- 按 source 优先级(仅 4 个高层:policy/flag/project/local,spec §4.5.1 简化)遍历 deny → ask → allow
- `_rule_matches(rule_str, cmd, tool_input) -> bool`:调 `permission_matcher.parse_permission_rule("Bash", content)` + `match_permission_rule(rule, {"command": cmd, ...})`
- acceptEdits mode + 只读命令 → ALLOW(`OtherReason("acceptEdits + read-only")`)
- 默认 PASSTHROUGH(让上层 decide)

**`check_sandbox_auto_allow(tool_input, context) -> PermissionDecision`**:
- 拆 subcommand + cap check
- 对每 subcommand 跑 `_check_single_command`,只查 deny(ask 转 allow 由沙箱兜底)
- 全 allow → ALLOW(`OtherReason("Auto-allowed in sandbox; deny rules already checked")`)

**`_parse_rule_string(rule_str) -> tuple[str, Optional[str]]`**:
- `Bash(rm:*)` → `("Bash", "rm:*")`;`Bash` → `("Bash", None)`

**关键边界 case**:
- `command == ""` → ASK(`OtherReason("empty command")`)
- 嵌套引号 regex 拆错:AST path 处理,regex path 接受错误拆分(M2 简化,不深度 quote 处理)
- classifier unavailable(默认 ANT-only stub)→ fall through to user prompt(spec §4.5.4)
- `dangerously_disable_sandbox=True` 但 `auto_allow_bash_if_sandboxed=False` → 不走 auto-allow

**测试覆盖**:
- `test_empty_command_asks` — `{"command": ""}` → ASK
- `test_simple_command_allows` — `{"command": "ls -la"}` + 无 rule → PASSTHROUGH
- `test_split_command_handles_and_operator` — `"echo a && rm foo"` → 2 subcommands
- `test_split_command_handles_semicolon` — `"cmd1; cmd2"` → 2
- `test_split_command_handles_pipe` — `"cmd1 | cmd2"` → 2
- `test_split_command_handles_or` — `"cmd1 || cmd2"` → 2
- `test_strip_safe_wrappers_timeout` — `"timeout 30 foo"` → `"foo"`
- `test_strip_safe_wrappers_env_var` — `"FOO=bar bazel run"` → `"bazel run"`
- `test_strip_safe_wrappers_multiple` — `"timeout 60 env FOO=bar nice git status"` → `"git status"`
- `test_is_cd_command_simple` — `"cd /tmp"` → True;`"cd"` → True;`"rm"` → False
- `test_cd_plus_git_asks_user` — `"cd /tmp && git status"` → ASK(SafetyCheckReason)
- `test_max_subcommands_50_prompts` — 51 subcommands → ASK
- `test_subcommand_deny_blocks_whole_command` — deny rule `Bash(rm:*)` + `"echo a && rm -rf /"` → DENY
- `test_subcommand_ask_blocks_whole_command` — ask rule + `"ls && git push"` → ASK
- `test_sandbox_auto_allow_when_all_deny_clean` — sandbox enabled + auto_allow_bash_if_sandboxed=True + deny rules 不命中 → ALLOW
- `test_sandbox_auto_allow_still_respects_deny` — sandbox enabled + command 含 deny rule subcommand → DENY
- `test_sandbox_auto_allow_skipped_when_disabled` — sandbox disabled → 走正常 path
- `test_sandbox_auto_allow_skipped_when_auto_allow_false` — auto_allow_bash_if_sandboxed=False → 不走 auto-allow
- `test_classifier_deny_blocks_command` — classifier 返 DENY → DENY(ClassifierReason)
- `test_classifier_allow_falls_through` — classifier 返 ALLOW → 全 allow → ALLOW
- `test_classifier_unavailable_skipped` — classifier=None 或 unavailable → 跳过
- `test_parse_rule_string_bash_with_content` — `"Bash(rm:*)"` → `("Bash", "rm:*")`
- `test_parse_rule_string_bash_no_content` — `"Bash"` → `("Bash", None)`
- `test_rule_matches_exact` — `Bash(npm run build)` matches `"npm run build"`
- `test_rule_matches_prefix` — `Bash(rm:*)` matches `"rm -rf /"`
- `test_rule_matches_wildcard` — `Bash(*echo*)` matches `"echo hello"`
- `test_rule_matches_non_bash_returns_false` — `Edit(rm:*)` matches `Bash("rm")` → False
- `test_accept_edits_read_only_allows` — mode=acceptEdits + `cat` / `ls` / `echo` → ALLOW
- `test_accept_edits_write_command_asks` — mode=acceptEdits + `rm` → ASK(非 read-only)
- `test_dangerously_disable_sandbox_skips_auto_allow_only` — disable + command 含 ask rule → 仍 ASK(不绕过 permission)
- `test_tree_sitter_path_uses_ast_when_enabled` — env `TREE_SITTER_BASH=true` + 引号内 `&&` → AST 正确处理
- `test_tree_sitter_path_falls_back_to_regex_on_import_error` — env true + import 失败 → regex
- `test_tree_sitter_default_disabled` — env 不设 → regex path
- `test_subcommand_results_reason_includes_rule` — DENY 时 `SubcommandResultsReason.allow_count`/`ask_count`/`deny_count` 正确

**Commit**:`feat(tools): add bash_permissions (subcommand parse + tree-sitter AST + classifier + sandbox auto-allow)`

---

### Task 5 — `audit_logger.py` 独立审计通道

**文件**:
- Create: `agent_core/tools/audit_logger.py`(新增 ~120 行)
- Test: `tests/test_audit_logger.py`(新增 ~15 test)

**依赖**:现有 `PermissionDecision` + `PermissionBehavior` + `ToolPermissionContext`

**关键内容**(对齐 spec §4.8 + CC `analyticsHooks.ts`):

**`AuditRecord` dataclass**:
- `timestamp: float` — `time.time()`
- `session_id: str`
- `tool_name: str`
- `tool_input_hash: str` — `hashlib.sha256(json.dumps(tool_input, sort_keys=True).encode()).hexdigest()[:16]`(**不存原文,可能含密钥**)
- `decision: str` — `decision.behavior.value`
- `reason_type: str` — `decision.decision_reason.type`(若为 None 走 `"unknown"`)
- `reason_detail: Optional[str]`
- `rule_source: Optional[str]` — 命中 rule 时填 source value
- `mode: Optional[str]`
- `sandbox_used: bool` — 是否走 sandbox(由 caller 传,默认 False)
- `hook_chain: list[str]`
- `classifier_used: bool`
- `classifier_decision: Optional[str]`
- `denial_state: Optional[dict]`

**`AuditLogger`** 类:
- `__init__(session_data_dir: str)`:
  - `self.session_id = Path(session_data_dir).name`
  - `self.path = Path(session_data_dir) / "audit.jsonl"`
  - `self.path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)`
- `log(tool_name, tool_input, decision, context, hook_chain=None, classifier_used=False, classifier_decision=None, denial_state=None) -> None`:
  - **try/except 全包** — audit 失败绝不阻断主流程(spec §4.8)
  - 计算 `tool_input_hash`
  - 构造 `AuditRecord`
  - `open(self.path, "a", encoding="utf-8") as f: f.write(json.dumps(asdict(record), ensure_ascii=False) + "\n"); f.flush(); os.fsync(f.fileno())` — atomic durability
- `query(since_ts=None, tool_name=None, decision=None) -> list[AuditRecord]`(M2 简化版,只读 + 简单 filter,M3 再加复杂查询):
  - 读 jsonl 行,反序列化,filter

**全局单例**:
- `_audit_logger: Optional[AuditLogger] = None`
- `init_audit_logger(session_data_dir: str) -> AuditLogger`
- `get_audit_logger() -> Optional[AuditLogger]`

**测试覆盖**:
- `test_log_creates_file_with_correct_mode` — 创建 dir 0o700
- `test_log_writes_valid_jsonl` — 1 行 → 1 个 JSON record,含所有字段
- `test_log_appends_multiple_records` — 3 次 log → 3 行
- `test_tool_input_hash_does_not_contain_secret` — input 含 fake secret,file 里只有 hash,无 secret 字面
- `test_tool_input_hash_stable_for_same_input` — 同 input → 同 hash
- `test_log_handles_failure_gracefully` — path 不可写(monkeypatch `open` 抛异常)→ 不抛,log warning
- `test_log_atomic_write_with_fsync` — mock `os.fsync`,验证调过
- `test_init_audit_logger_sets_global_singleton` — 调 `init_audit_logger` 后 `get_audit_logger()` 返该实例
- `test_query_filters_by_tool_name` — log 3 个不同 tool,query 1 个 → 返 1
- `test_query_filters_by_decision` — log allow/deny/ask,query "deny" → 返 deny only
- `test_query_filters_by_since_ts` — log t1, sleep, log t2, query since=t1+0.5 → 返 t2 only
- `test_audit_record_serializable_to_json` — asdict → JSON
- `test_audit_record_unicode_safe` — 中文 tool_name 正确序列化(ensure_ascii=False)
- `test_log_with_denial_state_includes_state` — denial_state={"consecutive_denials": 3} → record.denial_state 含
- `test_log_failure_does_not_block_caller` — 模拟写失败,验证 log() 返 None(不抛)

**Commit**:`feat(tools): add audit logger (independent audit.jsonl channel)`

---

### Task 6 — BashTool 内置实现(含 `dangerouslyDisableSandbox` 透传)

**文件**:
- Modify: `agent_core/tools/builtin.py`(改 149 → ~230 行)
- Test: `tests/test_builtin_bash_tool.py`(新增 ~20 test)

**依赖**:T1(`sandbox_manager.wrap_with_sandbox`)+ T2(`should_use_sandbox`)+ T4(`bash_check_permissions`)

**关键内容**(对齐 spec §6.3 + CC `BashTool.execute`):

**`bash_handler(**kwargs) -> str`**:
- 读 `kwargs.get("command", "")`,空 → `ValueError("missing command")`
- 读 `kwargs.get("timeout", 30.0)` 默认 30s
- 读 `kwargs.get("working_dir", None)`,默认 `os.getcwd()`
- 读 `kwargs.get("dangerously_disable_sandbox", False)` — 透传,供 sandbox decision 用
- 决定执行命令:
  - `from .sandbox_manager import sandbox_manager`
  - `from .sandbox_decision import should_use_sandbox`
  - 构造 `effective_input = {**kwargs}`
  - `effective_command = effective_input["command"]`
  - 若 `should_use_sandbox("Bash", effective_input)` → `effective_command = sandbox_manager.wrap_with_sandbox(effective_command, working_dir=working_dir)`
- `subprocess.run(effective_command, shell=True, cwd=working_dir, capture_output=True, text=True, timeout=timeout)`(用 list 不用 shell=True 防止 injection 但 spec 用 wrap,保留 shell=True 以支持 compound command)
- 返回 `stdout + stderr`(合并,前 5000 字符截断)
- 异常:
  - `subprocess.TimeoutExpired` → `return f"Bash command timed out after {timeout}s"`
  - `subprocess.CalledProcessError` → `return f"Bash command failed (exit {e.returncode}): {e.stderr[:2000]}"`
  - `FileNotFoundError`(sandbox binary 不存在)→ `return f"Sandbox binary not found, please run 'npm install -g @anthropic-ai/sandbox-runtime' or disable sandbox in settings.json"`

**`BASH_TOOL` (ToolDef)**:
```python
BASH_TOOL = ToolDef(
    name="Bash",
    description="""Run a shell command on the local system...

Use this tool when you need to perform shell operations like:
- Running tests: `pytest tests/`
- Installing dependencies: `npm install`
- File operations: `ls -la`, `find . -name "*.py"`
- System queries: `df -h`, `ps aux`

The command will be executed in a sandbox by default, which restricts:
- File writes to the working directory
- Network access to whitelisted domains

To bypass the sandbox for a specific command (use sparingly), set `dangerously_disable_sandbox: true`.
""",
    parameters={
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "Shell command to execute",
            },
            "timeout": {
                "type": "number",
                "description": "Timeout in seconds (default 30)",
                "default": 30.0,
            },
            "working_dir": {
                "type": "string",
                "description": "Working directory (default: cwd)",
            },
            "dangerously_disable_sandbox": {
                "type": "boolean",
                "description": "⚠️ Bypass sandbox for this command. Use only when sandbox restrictions cause failures.",
                "default": False,
            },
        },
        "required": ["command"],
    },
    handler=bash_handler,
    category="shell",
    check_permissions=None,  # 由 engine 在 Step 1c 调 bash_check_permissions 通过 tool_def.check_permissions
    requires_user_interaction=False,
)
```

**等等 — 关键架构决策**:`check_permissions` 字段已在 ToolDef 里(`base.py:46`)。但 BashTool 的 check_permissions 需要 classifier 实例,classifier 由 PermissionEngine 注入,ToolDef 构造时拿不到。

**解决方案**:BashTool 的 `check_permissions` 字段先用 None,改为 PermissionEngine 在 Step 1c 调一个特殊路径:
- M2 实施时,在 `PermissionEngine.check_permissions` 的 Step 1c 之前加一段:若 `tool_name == "Bash"` → 直接调 `bash_check_permissions(tool_input, context, classifier=self.classifier)`,不走 `tool.check_permissions` callback 路径。

OR 在 BashTool 注册时,通过闭包注入 classifier(模块级单例或工厂模式)。

**选 closure 方案**(学习项目 — 更 CC-style):
- 在 `builtin.py` 顶部:`from .bash_permissions import bash_check_permissions` 但不直接传 classifier
- BashTool 的 `check_permissions` 暂设为 None(避免循环 import),由 PermissionEngine 专门处理 BashTool(spec §4.5 注释 "Bash 是最容易被 prompt injection 利用的工具,所有 Bash 调用走 classifier 兜底")

**最终实现**:
- `BASH_TOOL.check_permissions = None`(明确语义:BashTool 的 check 由 PermissionEngine 专属路径调)
- 在 `PermissionEngine.check_permissions` 加 Step 1c' 段(在通用 Step 1c 之后,bypass mode 之前):
  ```python
  # ── Step 1c': BashTool 专属(对齐 spec §4.5)─────
  if tool_name == "Bash":
      bash_decision = bash_check_permissions(tool_input, self.context, classifier=self.classifier)
      if bash_decision.behavior in (PermissionBehavior.DENY.value, PermissionBehavior.ASK.value):
          return self._log_and_return(...)
  ```
- 修改 `permission_engine.py`,导入 `bash_check_permissions`(在 PermissionEngine.__init__ 时可选注入;默认从模块导入)

**测试覆盖**:
- `test_bash_handler_runs_simple_command` — `bash_handler(command="echo hello")` → "hello"
- `test_bash_handler_returns_stderr_on_failure` — `bash_handler(command="ls /nonexistent")` → 含 "No such file"
- `test_bash_handler_timeout_kills_command` — `bash_handler(command="sleep 5", timeout=0.1)` → "timed out"
- `test_bash_handler_uses_cwd_when_specified` — `working_dir="/tmp"` + `bash_handler(command="pwd")` → "/tmp"
- `test_bash_handler_missing_command_raises_value_error` — `bash_handler()` → ValueError
- `test_bash_handler_passes_dangerously_disable_sandbox_to_decision` — `dangerously_disable_sandbox=True` + sandbox enabled → 不 wrap
- `test_bash_handler_wraps_with_sandbox_by_default` — sandbox enabled + `dangerously_disable_sandbox=False` → wrap
- `test_bash_tool_schema_requires_command` — jsonschema 校验:无 command → error
- `test_bash_tool_schema_accepts_optional_timeout` — 有 timeout → OK
- `test_bash_tool_schema_rejects_dangerously_disable_sandbox_non_bool` — `dangerously_disable_sandbox="yes"` → jsonschema error
- `test_bash_tool_registered_with_category_shell` — `BASH_TOOL.category == "shell"`
- `test_bash_tool_registered_with_check_permissions_none` — `BASH_TOOL.check_permissions is None`(由 engine 走特殊路径)
- `test_register_builtin_tools_includes_bash` — `register_builtin_tools(reg)` 后 `reg.get("Bash") is BASH_TOOL`
- `test_bash_handler_empty_command_returns_error` — `command=""` → ValueError
- `test_bash_handler_nonzero_exit_returns_stderr` — `command="exit 1"` → 含 "exit 1"
- `test_bash_handler_sandbox_binary_not_found_returns_helpful_error` — mock sandbox_manager wrap 含 `npx` → 模拟 FileNotFoundError → 返 helpful msg
- `test_bash_handler_truncates_long_output` — 长输出 → 前 5000 字符
- `test_bash_handler_preserves_unicode` — 中文 command output 正确
- `test_bash_tool_description_mentions_sandbox` — `description` 含 "sandbox"
- `test_bash_tool_description_warns_dangerously_disable_sandbox` — `description` 含 "sparingly" / "bypass"

**Commit**:`feat(tools): add BashTool built-in (with dangerouslyDisableSandbox passthrough)`

---

### Task 7 — `agent_core.py` 整合 BashTool + sandbox wrap + audit logger

**文件**:
- Modify: `agent_core/agent_core.py`(改 ~110 行)
- Modify: `agent_core/tools/permission_engine.py`(改 ~30 行 — 加 Step 1c' BashTool 专属路径)
- Test: `tests/test_agent_core_bash_sandbox.py`(新增 ~15 test)

**依赖**:T1-T6 全部

**关键内容**:

**改动 1: `permission_engine.py` 加 Step 1c'**
- Import:`from .bash_permissions import bash_check_permissions`(lazily 在 PermissionEngine.__init__ 时检查,避免模块加载时强制依赖)
- 在 `check_permissions` 现有 Step 1c(`tool.check_permissions`)之后,Step 1d 之前插入:
  ```python
  # ── Step 1c': BashTool 专属(对齐 spec §4.5)─────
  if tool_name == "Bash":
      try:
          bash_decision = bash_check_permissions(
              tool_input, self.context, classifier=self.classifier,
          )
      except Exception as e:
          logger.warning("bash_check_permissions 异常: %s — 降级继续", e)
          bash_decision = None
      if bash_decision is not None and bash_decision.behavior == PermissionBehavior.DENY.value:
          return self._log_and_return(
              tool_name, tool_input, bash_decision,
              stage="step_1c_bash_deny",
          )
      if bash_decision is not None and bash_decision.behavior == PermissionBehavior.ASK.value:
          return self._log_and_return(
              tool_name, tool_input, bash_decision,
              stage="step_1c_bash_ask",
          )
      # bash_decision == ALLOW 或 None → fall through
  ```
- 在 `_log_and_return` 时如果 `context.sandbox_enabled and tool_name == "Bash"`,额外标 `sandbox_used=True`(通过把 context 传给 audit_logger 时计算;或在 log 时再读一次)

**改动 2: `agent_core.py` audit_logger 集成**
- `__init__` 已接收 `audit_logger: Optional[Any] = None`(line 191),已存为 `self.audit_logger`(line 258)— 不改
- `_check_tool_permission` 在调 `permission_engine.check_permissions` 之后,根据 decision 调用 audit:
  ```python
  decision = self.permission_engine.check_permissions(tool_def, tool_input, list(self.messages))
  
  # 写 audit log(spec §4.8)
  if self.audit_logger is not None:
      try:
          self.audit_logger.log(
              tool_name=tool_name,
              tool_input=tool_input,
              decision=decision,
              context=self.permission_engine.context,
              hook_chain=[],  # TODO M3: 实际 hook 链
              classifier_used=self.permission_engine.classifier is not None,
              classifier_decision=None,  # TODO M3: classifier 实际决策
              denial_state=asdict(self.permission_engine.get_denial_state()) if self.permission_engine.denial_state else None,
          )
      except Exception as e:
          _logger.warning("audit_logger.log 失败: %s", e)
  ```

**改动 3: web/app.py 注入 audit_logger + BashTool 注册**
- 在 `web/app.py:580-581` `register_builtin_tools(registry)` 已注册 — BashTool 注册后自动可用
- 在 `web/app.py:683-708` PermissionEngine 注入后,加 audit_logger:
  ```python
  from agent_core.tools.audit_logger import init_audit_logger
  
  session_data_dir = Path(__file__).parent.parent / "data" / "sessions" / session_id
  audit_logger = init_audit_logger(str(session_data_dir))
  agent.audit_logger = audit_logger
  ```
- BashTool description 自动出现在 tool_schemas → LLM 看得到 sandbox 说明 + dangerously_disable_sandbox warning

**改动 4: `system_prompt` 注入 sandbox section**
- 在 `_build_system_prompt` 或等效处(找现有 system prompt 构造点):
  ```python
  from .tools.sandbox_prompt import get_sandbox_prompt_section
  sandbox_section = get_sandbox_prompt_section()
  if sandbox_section:
      base_prompt += "\n\n" + sandbox_section
  ```
- (若现有 system prompt 是 hardcoded string,加一段 `if self.permission_engine is not None and getattr(self.permission_engine.context, "sandbox_enabled", False)` 拼接)

**改动 5: BashTool 在 ReactAgent.run() 内执行**
- 现有 line 1007 `self.tools.execute(tc.tool_name, effective_input, max_retries=3)` — 已能调 BashTool
- BashTool 的 `check_permissions=None` → PermissionEngine 走 Step 1c' 专属路径
- BashTool 的 handler 自己处理 sandbox wrap(在 bash_handler 内部调 sandbox_manager)
- **验证不动 `self.tools.execute` 的调用方式**(最小改动)

**测试覆盖**:
- `test_engine_routes_bash_to_bash_check_permissions` — PermissionEngine + Bash tool → bash_check_permissions 被调
- `test_engine_falls_back_to_normal_pipeline_for_non_bash` — tool_name=Read → 走 Step 1c 通用路径
- `test_engine_bash_deny_blocks_whole_pipeline` — Bash command `rm -rf /` → DENY
- `test_engine_bash_ask_blocks_whole_pipeline` — Bash command `npm publish` + no rule → ASK via bash_check_permissions
- `test_engine_bash_allow_falls_through` — Bash `ls` 无 deny rule + sandbox disabled → ALLOW(继续 engine pipeline)
- `test_audit_logger_called_on_each_decision` — agent + audit_logger → log 至少 1 次
- `test_audit_logger_failure_does_not_block_execution` — mock audit_logger.log 抛异常 → agent.run 继续,tool 仍执行
- `test_bash_tool_executed_via_agent_run` — agent.run + Bash tool input → subprocess.run 被调(用 mock)
- `test_bash_tool_with_sandbox_enabled_wraps_command` — sandbox enabled → bash_handler 走 wrap 路径
- `test_bash_tool_with_dangerously_disable_skips_sandbox` — `dangerously_disable_sandbox=True` → 不 wrap
- `test_sandbox_prompt_section_injected_into_system_prompt_when_enabled` — system_prompt 含 "Command sandbox"
- `test_sandbox_prompt_section_omitted_when_disabled` — system_prompt 不含 "Command sandbox"
- `test_existing_read_tool_still_works` — 回归:Read tool 行为不变
- `test_existing_calc_tool_still_works` — 回归:calc tool 行为不变

**Commit**:`feat(agent_core): wire BashTool + sandbox wrap + audit logger into ReactAgent.run()`

---

### Task 8 — 集成测试(含 bare-git scrub 回归 + sandbox e2e)

**文件**:
- Create: `tests/test_sandbox_e2e.py`(新增 ~15 test,e2e 集成)
- Modify: `tests/test_permission_engine.py`(新增 ~10 test — bash 路径覆盖)
- Modify: `tests/test_agent_core.py`(新增 ~5 test — Bash + audit 回归)

**依赖**:T1-T7 全部

**关键内容**(对齐 spec §9.4 M2 测试矩阵):

**e2e 测试** (`tests/test_sandbox_e2e.py`):
- `test_bare_git_scrub_attack_blocked` — 模拟 CC #29316 attack:`cd /tmp && git clone evil && cd evil && cat .git/config`(或更直接:`cd /tmp/evil && git status`),通过 bash_check_permissions → ASK(SafetyCheckReason)
- `test_subcommand_deny_still_blocks_in_sandbox` — sandbox enabled + command 含 deny subcommand → DENY(不绕过)
- `test_sandbox_auto_allow_for_safe_command` — sandbox enabled + auto_allow=True + `npm install` (无 deny rule) → ALLOW(无弹窗)
- `test_sandbox_disabled_asks_for_any_command` — sandbox disabled + `rm -rf /tmp/test` → ASK
- `test_bash_tool_e2e_with_mock_subprocess` — full flow: agent.run → BashTool → sandbox_manager.wrap → mock subprocess.run → tool_result 含 stdout
- `test_bash_tool_e2e_sandbox_disabled` — sandbox disabled + Bash `echo hello` → 直接执行,tool_result = "hello"
- `test_bash_tool_e2e_dangerously_disable` — `dangerously_disable_sandbox=True` + Bash `echo hi` → 不 wrap,直接执行
- `test_bash_tool_e2e_permission_deny_blocks` — settings.json 加 `Bash(rm:*)` deny + Bash `rm -rf /tmp/test` → DENY,tool 不执行
- `test_bash_tool_e2e_classifier_deny` — mock classifier 返 DENY → tool 不执行
- `test_bash_tool_e2e_audit_jsonl_created` — 跑完 Bash command → `data/sessions/<id>/audit.jsonl` 存在且含 records
- `test_audit_jsonl_contains_hash_not_input` — 写 Bash command 含 fake secret → audit.jsonl 含 hash,不含 secret 字面
- `test_audit_failure_does_not_block` — mock audit_logger 抛 → agent.run 不中断
- `test_sandbox_tmp_dir_created_with_0700` — bash_handler 跑后 `/tmp/claude-<uid>` 存在且 mode 0o700
- `test_sandbox_cleanup_runs_after_command` — mock `_scrub_bare_git` 和 `_cleanup_sandbox_tmp_dir`,验证 bash_handler 完后调过
- `test_prompt_includes_sandbox_section_when_enabled` — agent run + sandbox enabled → system_prompt 含 "Command sandbox"
- `test_prompt_omits_sandbox_section_when_disabled` — agent run + sandbox disabled → system_prompt 不含 "Command sandbox"
- `test_regression_existing_calc_tool_still_works` — calc(2+3) → "5.0"
- `test_regression_existing_search_tool_still_works` — search query → 返 results
- `test_regression_read_safety_check_still_blocks_sensitive` — Read(.ssh/id_rsa) → ASK

**permission_engine 新增覆盖** (`tests/test_permission_engine.py`):
- `test_step_1c_bash_routes_to_bash_check_permissions` — engine + Bash → bash_check_permissions 被调
- `test_step_1c_bash_deny_returns_deny` — Bash deny → engine DENY
- `test_step_1c_bash_ask_returns_ask` — Bash ask → engine ASK
- `test_step_1c_bash_allow_falls_through_to_normal_pipeline` — Bash allow → 继续 engine 2b allow rule check
- `test_bash_check_permissions_exception_does_not_break_pipeline` — mock bash_check_permissions 抛异常 → engine 不挂,继续
- `test_engine_audit_log_includes_sandbox_used` — sandbox enabled → audit record.sandbox_used=True
- `test_engine_audit_log_includes_classifier_used` — classifier 调过 → audit record.classifier_used=True
- `test_engine_audit_log_failure_silent` — mock audit 失败 → engine 仍返 decision
- `test_engine_denial_state_increments_on_deny` — DENY → denial_state.consecutive_denials++
- `test_engine_denial_state_resets_on_allow` — ALLOW → consecutive_denials=0

**agent_core 新增覆盖** (`tests/test_agent_core.py`):
- `test_react_agent_with_bash_tool_runs_command` — agent.run + Bash `echo hi` → tool_result = "hi"
- `test_react_agent_bash_with_permission_deny` — agent + permission engine + Bash deny → tool_result = error msg,tool 不执行
- `test_react_agent_bash_with_sandbox_wrap` — agent + sandbox enabled + Bash `echo` → subprocess 被调(用 mock)
- `test_react_agent_audit_logger_called` — agent + audit_logger → log 被调
- `test_react_agent_audit_logger_failure_does_not_break` — agent + audit 抛 → run 仍继续

**Commit**:`test(tools): add Phase 2 sandbox + bash e2e + regression suite`

---

## 实施时间表

| Task | 内容 | 估时 | 风险 |
|---|---|---|---|
| 1 | sandbox_manager.py | 3.5h | 中(bare-git scrub 实现 + 平台支持检测) |
| 2 | sandbox_decision.py | 1h | 低 |
| 3 | sandbox_prompt.py | 1h | 低 |
| 4 | bash_permissions.py | 4.5h | 高(双路径 AST/regex + classifier 集成 + sandbox auto-allow 协同) |
| 5 | audit_logger.py | 1.5h | 低 |
| 6 | BashTool 内置 | 1.5h | 中(check_permissions 注入决策 — 闭包 vs engine 特殊路径) |
| 7 | agent_core 整合 | 2h | 中(改 permission_engine.py 加 Step 1c' + audit 集成 + system prompt 拼接) |
| 8 | 集成测试 | 4h | 中(bare-git scrub attack 回归 + sandbox e2e) |

总耗时:~19h ≈ 2.5 天,8 个独立 commit。

---

## 文件清单(预期)

```
agent_core/tools/
├── __init__.py                    (更新导出 bash_check_permissions / sandbox_manager / audit_logger)
├── base.py                        (不变,Phase 1 已完成)
├── builtin.py                     (149 → ~230 行:加 BASH_TOOL + bash_handler)
├── permission_types.py            (不变,Phase 1 已完成 — SubcommandResultsReason 等已存在)
├── permission_matcher.py          (不变,Phase 1 已完成)
├── permission_loader.py           (不变,Phase 1 已完成)
├── permission_engine.py           (改 ~530 → ~570 行:加 Step 1c' BashTool 专属路径)
├── permission_hook.py             (不变,Phase 1 已完成)
├── classifier.py                  (不变,Phase 1 已完成 — HaikuClassifier stub 已可用)
├── denial_tracking.py             (不变,Phase 1 已完成)
├── classifier_fast_path.py        (不变,Phase 1 已完成)
├── safety_check.py                (不变,Phase 1 已完成)
├── sandbox_manager.py             (新增 ~200 行)
├── sandbox_decision.py            (新增 ~60 行)
├── sandbox_prompt.py              (新增 ~100 行)
├── bash_permissions.py            (新增 ~250 行)
└── audit_logger.py                (新增 ~120 行)

agent_core/
├── agent_core.py                  (改 ~110 行:audit logger 集成 + system prompt sandbox section)
└── exceptions.py                  (不变)

web/
├── app.py                         (改 ~30 行:audit logger 注入 + BashTool 自动注册)
└── app_langgraph.py               (不动)

tests/
├── test_sandbox_manager.py        (新增 ~25 test)
├── test_sandbox_decision.py       (新增 ~15 test)
├── test_sandbox_prompt.py         (新增 ~12 test)
├── test_bash_permissions.py       (新增 ~35 test)
├── test_audit_logger.py           (新增 ~15 test)
├── test_builtin_bash_tool.py      (新增 ~20 test)
├── test_agent_core_bash_sandbox.py (新增 ~15 test)
├── test_sandbox_e2e.py            (新增 ~15 test,e2e)
├── test_permission_engine.py      (新增 ~10 test,bash 路径)
├── test_agent_core.py             (新增 ~5 test,Bash + audit 回归)
└── test_app_permission_dialog.py  (不变,Phase 1 已完成)
```

总计:5 个新增 Python 文件 + 8 个新增 test 文件 + 3 个文件改造 + 8 个独立 commit。

---

## 关键边界 case 与风险

| 风险 | 概率 | 缓解 |
|---|---|---|
| Task 4 bash_check_permissions 改 engine 改坏 Phase 1 测试 | 中 | Step 1c' 加 try/except 降级,所有 Phase 1 engine 测试应仍通过 |
| Task 6 BashTool 的 check_permissions 注入决策 | 低 | 选 closure 方案失败 fallback 到 engine 专属路径,文档明确 |
| Task 7 audit_logger 集成破坏现有 `_check_tool_permission` | 中 | audit 在 decision 之后调,try/except 包,失败 log warning 不抛 |
| Task 7 system_prompt 拼接点找不到 | 低 | `_build_system_prompt` 已有(在 agent_core.py 早期),grep 定位 |
| sandbox binary 不存在(无 npx / 无 Node.js) | 中 | bash_handler 返 helpful error,`is_sandbox_enabled()` 返 False(graceful) |
| tree-sitter import 失败 + TREE_SITTER_BASH=true | 中 | `_parse_via_tree_sitter` try/except fallback regex |
| bare-git scrub 删错文件 | 低 | 只删 `.git/` 结尾的 dir,且仅 `cleanup_after_command` 调(每次 bash 后),不影响 sandbox 内 |
| classifier 默认 ANT-only → 自动 unavailable | 已知 | 本项目用 glm-4 / DeepSeek 等,M2 不依赖 classifier 实际工作(spec §4.5.2 已说明) |
| `dangerously_disable_sandbox` 真绕过 OS 兜底 | 已知 | spec §6.3 明确:仅 bypass sandbox 不 bypass permission,permission rule check 仍生效 |
| audit_logger.log 阻塞主线程 | 低 | `f.flush() + os.fsync` 单条 < 1ms,异步写是 M3+ 优化 |

---

## 端到端验证

### 1. 单测矩阵(只跑受影响文件)
```bash
cd /Users/fanyunxu/Desktop/myproject/agent-dev && \
  pytest tests/test_sandbox_manager.py \
         tests/test_sandbox_decision.py \
         tests/test_sandbox_prompt.py \
         tests/test_bash_permissions.py \
         tests/test_audit_logger.py \
         tests/test_builtin_bash_tool.py \
         tests/test_agent_core_bash_sandbox.py \
         tests/test_sandbox_e2e.py \
         tests/test_permission_engine.py \
         tests/test_agent_core.py \
         -v
```
期望:新增 ~165 test 全 passed + Phase 1 现有 test 仍 passed(无破坏)。

### 2. Phase 1 不破坏
```bash
pytest tests/test_permission_types.py \
       tests/test_permission_matcher.py \
       tests/test_permission_loader.py \
       tests/test_permission_hook.py \
       tests/test_classifier.py \
       tests/test_classifier_fast_path.py \
       tests/test_denial_tracking.py \
       tests/test_safety_check.py \
       tests/test_tool_registry.py \
       tests/test_permission_integration.py \
       tests/test_app_permission_dialog.py \
       -v
```
期望:Phase 1 全部 ~100 test 仍 passed。

### 3. Streamlit 冒烟
```bash
# 启动 streamlit
streamlit run web/app.py

# 1. settings.json 加 sandbox 启用(测试用):
mkdir -p ~/.agent_data
cat > ~/.agent_data/settings.json << 'EOF'
{
  "permissions": {
    "allow": ["Bash(ls:*)", "Bash(echo:*)"],
    "deny": ["Bash(rm:*)"],
    "ask": ["Bash(git push:*)"]
  },
  "sandbox": {
    "enabled": true,
    "autoAllowBashIfSandboxed": true
  }
}
EOF

# 2. UI 验证:
#    - 问 "列出当前目录" → Bash(ls) auto-allowed (sandbox + allow rule)
#    - 问 "删除 /tmp/test" → Bash(rm) → 弹 st.dialog(deny rule)
#    - 问 "echo hello" → Bash(echo) auto-allowed
#    - 系统 prompt 应含 "Command sandbox" 段
```

### 4. audit.jsonl 冒烟
```bash
# 跑完一个 Bash 命令后:
cat ~/.agent_data/sessions/<session_id>/audit.jsonl | head -5
# 期望:每个 Bash tool_use 一行 JSON,含 tool_name/decision/sandbox_used/tool_input_hash

# 验证 secret 不在 audit:
cat ~/.agent_data/sessions/<session_id>/audit.jsonl | grep -i "sk-ant\|ghp_\|AKIA"
# 期望:无输出(只有 hash,无原文)
```

### 5. bare-git scrub 回归测试
```bash
pytest tests/test_sandbox_e2e.py::TestBareGitScrub -v
# 期望:cd + git 组合 → ASK(SafetyCheckReason)
```

### 6. 沙箱冒烟(可选,需 Node.js + npx)
```bash
# 启动 streamlit 后,问 "安装 express"(LLM 会调 Bash(npm install express))
# 期望:BashTool 实际执行,subprocess.run(npx ... wrap ...) 被调
# 失败场景:无 npx → bash_handler 返 "Sandbox binary not found"
```

---

## 关键文件总览(复用资产)

| 资产 | 位置 | Task 用法 |
|---|---|---|
| `PermissionEngine.check_permissions` | `agent_core/tools/permission_engine.py:110` | T7 加 Step 1c' 插入 BashTool 专属路径 |
| `HaikuClassifier.classify` | `agent_core/tools/classifier.py:139` | T4 `bash_check_permissions` 内部调(M2 同步调用,不引入异步) |
| `SubcommandResultsReason` / `SandboxOverrideReason` / `SafetyCheckReason` | `agent_core/tools/permission_types.py:188/218/237` | T4 bash_check_permissions 返各种 reason |
| `parse_permission_rule` / `match_permission_rule` | `agent_core/tools/permission_matcher.py:131/197` | T4 `_rule_matches` 内部用 |
| `PermissionDecision` | `agent_core/tools/permission_types.py:294` | T4 返回值 |
| `tool.check_permissions` field | `agent_core/tools/base.py:46` | T6 文档说明:bash 走 engine 特殊路径,字段保持 None |
| `ReactAgent._check_tool_permission` | `agent_core/agent_core.py:496` | T7 audit_logger.log 集成点(在 check_permissions 后调) |
| `ReactAgent.audit_logger` field | `agent_core/agent_core.py:191/258` | T7 已存在,只需在 web/app.py 注入 |
| `concurrent.futures.ThreadPoolExecutor` | `agent_core/agent_core.py:1055` | 不动,Phase 1 已用 |
| `data/sessions/<id>/` | `web/app.py` | T7 audit_logger 写到此处 session 子目录 |
| `jsonschema.validate` | `agent_core/tools/base.py:210` | T6 BashTool schema 校验(自动) |
| `subprocess.run` | Python stdlib | T6 BashTool 实际执行命令 |

---

## 不要做的事

1. **不要引入 npm 依赖到 requirements.txt** — 走 subprocess + npx,避免污染 Python 依赖
2. **不要让 fast-path 主动 deny** — spec §4.9 + CC permissions.ts:600-686 都只 allow/ask
3. **不要让 audit_logger 阻塞主流程** — spec §4.8 明确"audit must never block decision",try/except 全包
4. **不要让 sandbox binary 缺失让整个 agent 挂掉** — `is_sandbox_enabled()` 短路 + bash_handler 返 helpful error
5. **不要让 `dangerously_disable_sandbox` 绕过 permission rule check** — spec §6.3 明确:仅 bypass sandbox 不 bypass permission
6. **不要写 BashTool 的 check_permissions callback** — 闭包循环 import + classifier 注入困难,改走 engine Step 1c' 特殊路径
7. **不要改 web/app_langgraph.py** — MEMORY.md 约束
8. **不要合并多个 task 到一个 commit** — 1 task 1 commit
9. **不要简化 classifier 集成** — 即使 M2 用 stub,M3+ 接真 LLM 时不需要重写,接口完整
10. **不要让 tree-sitter 失败影响默认路径** — env false 时根本不走 AST,import 都不调

---

## 与 Phase 1 的边界

**Phase 1 (M1) 已完成且本阶段不动的接口**:
- `PermissionEngine.check_permissions`(7 步 pipeline)
- `HaikuClassifier`(默认 stub)
- `HookRegistry.run_pre_tool_use`
- `ToolDef` 字段(category / check_permissions / requires_user_interaction)
- `PermissionDecision` 11 种 reason
- `ReactAgent._check_tool_permission` / `resolve_permission`
- `web/app.py` PermissionEngine 注入 + st.dialog 弹窗
- `audit.jsonl` 路径(Phase 1 doc §4.8 已设计,本阶段实装)

**Phase 1 已有但本阶段才实装的组件**:
- `audit_logger.py`(doc §4.8 已设计,代码 M2 实施)
- `sandbox_manager.py` / `sandbox_decision.py` / `sandbox_prompt.py`(doc §5 已设计)
- `bash_permissions.py`(doc §4.5 已设计,含 `TREE_SITTER_BASH` env)
- BashTool builtin 实现(doc §6.2 / §6.3 已规划)

**Phase 1 测试应全通过本阶段不修改**:
- `test_permission_types.py` / `test_permission_engine.py` / `test_permission_matcher.py` / `test_permission_loader.py` / `test_permission_hook.py` / `test_classifier.py` / `test_classifier_fast_path.py` / `test_denial_tracking.py` / `test_safety_check.py` / `test_tool_registry.py` / `test_permission_integration.py` / `test_app_permission_dialog.py`

---

> 本计划完全对齐 `docs/tool/tool-security-architecture.md` §9 Phase 2 + §4.5 bash_permissions + §4.8 audit_logger + §5 OS 层沙箱 + §6 关键交互点。doc 已 commit,实施时严格按 doc 实现,不偏离。
