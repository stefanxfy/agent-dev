"""
bash_permissions.py 测试

覆盖:
1. parse_subcommands:双路径(regex / tree-sitter)
2. strip_safe_wrappers
3. is_cd_command / is_read_only
4. bash_check_permissions 完整流水线(Step 0-6)
5. _check_single_command rule match
6. check_sandbox_auto_allow
7. _parse_rule_string / _rule_matches
8. cd + git 检测(#29316)
9. MAX_SUBCOMMANDS cap
10. classifier 集成
"""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest

from agent_core.tools.bash_permissions import (
    MAX_SUBCOMMANDS,
    SAFE_WRAPPERS,
    _parse_rule_string,
    _rule_matches,
    bash_check_permissions,
    check_sandbox_auto_allow,
    is_cd_command,
    is_read_only,
    parse_subcommands,
    strip_safe_wrappers,
)
from agent_core.tools.permission_types import (
    PermissionBehavior,
    PermissionMode,
    ToolPermissionContext,
)
from agent_core.tools.sandbox_manager import SandboxManager


# ────────────────────────────────────────────────────────────────────
# fixture
# ────────────────────────────────────────────────────────────────────

@pytest.fixture
def reset_sandbox():
    mgr = SandboxManager()
    mgr._reset_for_testing()
    yield mgr
    mgr._reset_for_testing()


@pytest.fixture
def default_context():
    return ToolPermissionContext()


def _ctx(**kwargs):
    return ToolPermissionContext(**kwargs)


# ────────────────────────────────────────────────────────────────────
# parse_subcommands — regex path
# ────────────────────────────────────────────────────────────────────

class TestParseSubcommandsRegex:
    def test_empty_command_returns_empty(self):
        assert parse_subcommands("") == []
        assert parse_subcommands("   ") == []

    def test_single_command(self):
        subs = parse_subcommands("ls -la")
        assert len(subs) == 1
        assert subs[0].name == "ls"
        assert subs[0].args == ["-la"]

    def test_split_and_operator(self):
        subs = parse_subcommands("echo a && rm foo")
        assert len(subs) == 2
        assert subs[0].name == "echo"
        assert subs[1].name == "rm"

    def test_split_semicolon(self):
        subs = parse_subcommands("cmd1; cmd2")
        assert len(subs) == 2

    def test_split_pipe(self):
        subs = parse_subcommands("cmd1 | cmd2")
        assert len(subs) == 2

    def test_split_or_operator(self):
        subs = parse_subcommands("cmd1 || cmd2")
        assert len(subs) == 2

    def test_split_multiple_operators(self):
        subs = parse_subcommands("a && b; c | d")
        assert len(subs) == 4

    def test_detect_redirect(self):
        subs = parse_subcommands("echo hello > file.txt")
        assert subs[0].is_redirect is True

    def test_detect_subshell(self):
        subs = parse_subcommands("echo $(date)")
        assert subs[0].is_subshell is True

    def test_no_redirect_when_absent(self):
        subs = parse_subcommands("ls -la")
        assert subs[0].is_redirect is False

    def test_quoted_string_with_operator(self):
        # regex path 会错误拆分引号内的 &&(这是已知限制,AST path 才正确)
        # 这里只验证 regex path 能解析(即使不完美)
        subs = parse_subcommands("echo 'hello world'")
        assert len(subs) >= 1
        assert subs[0].name == "echo"

    def test_args_parsed_via_shlex(self):
        subs = parse_subcommands('git commit -m "hello world"')
        assert subs[0].name == "git"
        # shlex 把引号内容作为单个 arg
        assert "hello world" in subs[0].args or subs[0].args == ["commit", "-m", "hello world"]


# ────────────────────────────────────────────────────────────────────
# parse_subcommands — tree-sitter path
# ────────────────────────────────────────────────────────────────────

class TestParseSubcommandsTreeSitter:
    def test_tree_sitter_default_disabled(self, monkeypatch):
        monkeypatch.delenv("TREE_SITTER_BASH", raising=False)
        # 默认走 regex path
        from agent_core.tools import bash_permissions as bp
        assert bp._tree_sitter_enabled() is False

    def test_tree_sitter_enabled_when_env_true(self, monkeypatch):
        monkeypatch.setenv("TREE_SITTER_BASH", "true")
        from agent_core.tools import bash_permissions as bp
        assert bp._tree_sitter_enabled() is True

    def test_tree_sitter_path_falls_back_to_regex_on_import_error(self, monkeypatch):
        # env true 但 tree_sitter_bash 未安装 → fallback regex
        monkeypatch.setenv("TREE_SITTER_BASH", "true")
        subs = parse_subcommands("echo a && echo b")
        # 应成功解析(regex fallback)
        assert len(subs) == 2

    def test_tree_sitter_correctly_handles_quoted_operator(self, monkeypatch):
        # env true → 尝试 AST;若未装依赖 → regex(会拆错)
        # 这个测试只验证不抛异常
        monkeypatch.setenv("TREE_SITTER_BASH", "true")
        subs = parse_subcommands("echo 'a && b'")
        # 不管 AST 还是 regex,至少返回非空 list
        assert len(subs) >= 1


# ────────────────────────────────────────────────────────────────────
# strip_safe_wrappers
# ────────────────────────────────────────────────────────────────────

class TestStripSafeWrappers:
    def test_timeout_stripped(self):
        assert strip_safe_wrappers("timeout 30 foo") == "foo"

    def test_time_stripped(self):
        assert strip_safe_wrappers("time foo") == "foo"

    def test_nice_stripped(self):
        assert strip_safe_wrappers("nice git status") == "git status"

    def test_env_var_stripped(self):
        assert strip_safe_wrappers("FOO=bar bazel run") == "bazel run"

    def test_multiple_wrappers_stripped(self):
        # 多个连续 wrapper 全剥
        assert strip_safe_wrappers("timeout 60 env FOO=bar nice git status") == "git status"

    def test_command_stripped(self):
        assert strip_safe_wrappers("command ls") == "ls"

    def test_nohup_stripped(self):
        assert strip_safe_wrappers("nohup foo") == "foo"

    def test_no_wrapper_unchanged(self):
        assert strip_safe_wrappers("rm -rf /tmp") == "rm -rf /tmp"

    def test_empty_command(self):
        assert strip_safe_wrappers("") == ""

    def test_safe_wrappers_list_contents(self):
        # 验证 SAFE_WRAPPERS 常量
        assert "timeout" in SAFE_WRAPPERS
        assert "env" in SAFE_WRAPPERS
        assert "command" in SAFE_WRAPPERS


# ────────────────────────────────────────────────────────────────────
# is_cd_command
# ────────────────────────────────────────────────────────────────────

class TestIsCdCommand:
    def test_cd_with_path(self):
        assert is_cd_command("cd /tmp") is True

    def test_cd_alone(self):
        assert is_cd_command("cd") is True

    def test_cd_with_whitespace(self):
        assert is_cd_command("  cd  ") is True

    def test_non_cd_command(self):
        assert is_cd_command("rm -rf /") is False

    def test_empty(self):
        assert is_cd_command("") is False

    def test_cdxyz_not_cd(self):
        # cdbackup 不应误判为 cd
        assert is_cd_command("cdbackup /tmp") is False


# ────────────────────────────────────────────────────────────────────
# is_read_only
# ────────────────────────────────────────────────────────────────────

class TestIsReadOnly:
    def test_cat_read_only(self):
        assert is_read_only("cat file.txt") is True

    def test_ls_read_only(self):
        assert is_read_only("ls -la") is True

    def test_echo_read_only(self):
        assert is_read_only("echo hello") is True

    def test_rm_not_read_only(self):
        assert is_read_only("rm -rf /tmp") is False

    def test_git_status_read_only(self):
        assert is_read_only("git status") is True

    def test_git_diff_read_only(self):
        assert is_read_only("git diff") is True

    def test_git_push_not_read_only(self):
        assert is_read_only("git push origin main") is False

    def test_with_safe_wrapper(self):
        assert is_read_only("timeout 30 cat file") is True

    def test_empty(self):
        assert is_read_only("") is False


# ────────────────────────────────────────────────────────────────────
# _parse_rule_string
# ────────────────────────────────────────────────────────────────────

class TestParseRuleString:
    def test_bash_with_content(self):
        assert _parse_rule_string("Bash(rm:*)") == ("Bash", "rm:*")

    def test_bash_no_content(self):
        assert _parse_rule_string("Bash") == ("Bash", None)

    def test_bash_with_exact_content(self):
        assert _parse_rule_string("Bash(npm run build)") == ("Bash", "npm run build")

    def test_edit_rule(self):
        assert _parse_rule_string("Edit(*)") == ("Edit", "*")

    def test_strip_whitespace(self):
        assert _parse_rule_string("  Bash(rm:*)  ") == ("Bash", "rm:*")


# ────────────────────────────────────────────────────────────────────
# _rule_matches
# ────────────────────────────────────────────────────────────────────

class TestRuleMatches:
    def test_exact_match(self):
        assert _rule_matches("Bash(npm run build)", "npm run build") is True

    def test_exact_no_match(self):
        assert _rule_matches("Bash(npm run build)", "npm run test") is False

    def test_prefix_match(self):
        assert _rule_matches("Bash(rm:*)", "rm -rf /") is True

    def test_prefix_no_match(self):
        assert _rule_matches("Bash(rm:*)", "ls -la") is False

    def test_wildcard_match(self):
        assert _rule_matches("Bash(*echo*)", "echo hello") is True

    def test_non_bash_rule_returns_false(self):
        assert _rule_matches("Edit(rm:*)", "rm -rf /") is False

    def test_bash_no_content_matches_any(self):
        # Bash(无 content)= 整个 tool 命中
        assert _rule_matches("Bash", "anything") is True


# ────────────────────────────────────────────────────────────────────
# bash_check_permissions — 主流水线
# ────────────────────────────────────────────────────────────────────

class TestBashCheckPermissionsPipeline:
    def test_empty_command_asks(self, default_context):
        decision = bash_check_permissions({"command": ""}, default_context)
        assert decision.behavior == PermissionBehavior.ASK.value

    def test_whitespace_command_asks(self, default_context):
        decision = bash_check_permissions({"command": "   "}, default_context)
        assert decision.behavior == PermissionBehavior.ASK.value

    def test_missing_command_asks(self, default_context):
        decision = bash_check_permissions({}, default_context)
        assert decision.behavior == PermissionBehavior.ASK.value

    def test_simple_command_passthrough_when_no_rules(self, default_context):
        # 无 rule + 非 sandbox + 非 acceptEdits → passthrough
        decision = bash_check_permissions({"command": "ls -la"}, default_context)
        assert decision.behavior == PermissionBehavior.PASSTHROUGH.value

    def test_cd_plus_git_asks(self, default_context):
        decision = bash_check_permissions(
            {"command": "cd /tmp && git status"}, default_context,
        )
        assert decision.behavior == PermissionBehavior.ASK.value
        assert decision.decision_reason.type == "safetyCheck"

    def test_cd_plus_git_with_complex(self, default_context):
        decision = bash_check_permissions(
            {"command": "cd evil_repo && git log && cat file"}, default_context,
        )
        assert decision.behavior == PermissionBehavior.ASK.value

    def test_cd_alone_no_git_not_flagged(self, default_context):
        # 只有 cd,没 git → 不触发 #29316 检测
        decision = bash_check_permissions({"command": "cd /tmp"}, default_context)
        # 不应是 safetyCheck(走 passthrough)
        assert decision.decision_reason.type != "safetyCheck"

    def test_git_alone_no_cd_not_flagged(self, default_context):
        decision = bash_check_permissions({"command": "git status"}, default_context)
        assert decision.decision_reason.type != "safetyCheck"

    def test_max_subcommands_prompts(self, default_context):
        # 51 个 subcommands → ASK
        cmd = " ; ".join(["echo x"] * 51)
        decision = bash_check_permissions({"command": cmd}, default_context)
        assert decision.behavior == PermissionBehavior.ASK.value

    def test_just_under_max_subcommands_ok(self, default_context):
        cmd = " ; ".join(["echo x"] * 50)
        decision = bash_check_permissions({"command": cmd}, default_context)
        # 50 个不超过 MAX,应走 passthrough(无 deny rule)
        assert decision.behavior != PermissionBehavior.ASK.value or \
               decision.decision_reason.type != "other" or \
               "超过" not in (decision.decision_reason.reason or "")

    def test_subcommand_deny_blocks_whole(self, default_context):
        ctx = _ctx(always_deny_rules={"projectSettings": ["Bash(rm:*)"]})
        decision = bash_check_permissions(
            {"command": "echo a && rm -rf /"}, ctx,
        )
        assert decision.behavior == PermissionBehavior.DENY.value

    def test_subcommand_ask_blocks_whole(self, default_context):
        ctx = _ctx(always_ask_rules={"projectSettings": ["Bash(git push:*)"]})
        decision = bash_check_permissions(
            {"command": "ls && git push origin main"}, ctx,
        )
        assert decision.behavior == PermissionBehavior.ASK.value

    def test_allow_rule_allows(self, default_context):
        ctx = _ctx(always_allow_rules={"projectSettings": ["Bash(ls:*)"]})
        decision = bash_check_permissions({"command": "ls -la"}, ctx)
        # sandbox disabled → 不走 auto-allow;但 subcommand 匹配 allow rule
        # 不过 _check_single_command 返 allow,但主流程 fall through 到 passthrough
        # (因为没有 fast-path 主动 allow,allow 在 subcommand 级是"通过"信号)
        # 验证不是 DENY/ASK
        assert decision.behavior != PermissionBehavior.DENY.value


# ────────────────────────────────────────────────────────────────────
# acceptEdits 模式
# ────────────────────────────────────────────────────────────────────

class TestAcceptEditsMode:
    def test_accept_edits_read_only_allows(self):
        ctx = _ctx(mode=PermissionMode.ACCEPT_EDITS.value)
        decision = bash_check_permissions({"command": "ls -la"}, ctx)
        assert decision.behavior == PermissionBehavior.ALLOW.value

    def test_accept_edits_write_command_passthrough(self):
        ctx = _ctx(mode=PermissionMode.ACCEPT_EDITS.value)
        # rm 非只读 → 不走 acceptEdits fast-allow
        decision = bash_check_permissions({"command": "rm -rf /tmp"}, ctx)
        assert decision.behavior == PermissionBehavior.PASSTHROUGH.value

    def test_accept_edits_git_status_read_only(self):
        ctx = _ctx(mode=PermissionMode.ACCEPT_EDITS.value)
        decision = bash_check_permissions({"command": "git status"}, ctx)
        assert decision.behavior == PermissionBehavior.ALLOW.value

    def test_default_mode_no_accept_edits_fast_path(self):
        ctx = _ctx(mode=PermissionMode.DEFAULT.value)
        decision = bash_check_permissions({"command": "ls -la"}, ctx)
        # default mode 不走 acceptEdits fast-allow → passthrough
        assert decision.behavior == PermissionBehavior.PASSTHROUGH.value


# ────────────────────────────────────────────────────────────────────
# classifier 集成
# ────────────────────────────────────────────────────────────────────

class TestClassifierIntegration:
    def test_classifier_none_skipped(self, default_context):
        # classifier=None → 不调 classifier
        decision = bash_check_permissions(
            {"command": "ls"}, default_context, classifier=None,
        )
        assert decision.behavior == PermissionBehavior.PASSTHROUGH.value

    def test_classifier_deny_blocks(self):
        ctx = _ctx(is_anthropic_provider=True, no_settings_match=True)
        # mock classifier 返 should_block=True
        class FakeClassifier:
            def classify(self, messages, tool_name, tool_input, context):
                from agent_core.tools.classifier import ClassifierResult
                return ClassifierResult(
                    should_block=True,
                    reason="dangerous command",
                    unavailable=False,
                )

        with patch("agent_core.tools.bash_permissions.is_classifier_enabled") if False else patch(
            "agent_core.tools.classifier.is_classifier_enabled", return_value=True
        ), patch.dict(os.environ, {"TRANSCRIPT_CLASSIFIER_ENABLED": "true"}):
            # need is_classifier_enabled in bash_permissions to return True
            # bash_permissions imports it lazily inside the function
            import agent_core.tools.classifier as clf
            with patch.object(clf, "is_classifier_enabled", return_value=True):
                decision = bash_check_permissions(
                    {"command": "ls"}, ctx, classifier=FakeClassifier(),
                )
        assert decision.behavior == PermissionBehavior.DENY.value
        assert decision.decision_reason.type == "classifier"

    def test_classifier_allow_falls_through(self):
        ctx = _ctx(is_anthropic_provider=True, no_settings_match=True)
        class FakeClassifier:
            def classify(self, messages, tool_name, tool_input, context):
                from agent_core.tools.classifier import ClassifierResult
                return ClassifierResult(
                    should_block=False,
                    reason="safe",
                    unavailable=False,
                )

        import agent_core.tools.classifier as clf
        with patch.object(clf, "is_classifier_enabled", return_value=True):
            decision = bash_check_permissions(
                {"command": "ls"}, ctx, classifier=FakeClassifier(),
            )
        # classifier allow + 无 rule → passthrough(非 sandbox)
        assert decision.behavior == PermissionBehavior.PASSTHROUGH.value

    def test_classifier_unavailable_skipped(self):
        ctx = _ctx(is_anthropic_provider=True)
        class FakeClassifier:
            def classify(self, messages, tool_name, tool_input, context):
                from agent_core.tools.classifier import ClassifierResult
                return ClassifierResult(
                    should_block=False,
                    reason="unavailable",
                    unavailable=True,
                )

        decision = bash_check_permissions(
            {"command": "ls"}, ctx, classifier=FakeClassifier(),
        )
        # unavailable → 跳过 classifier → passthrough
        assert decision.behavior == PermissionBehavior.PASSTHROUGH.value

    def test_non_anthropic_skips_classifier(self):
        ctx = _ctx(is_anthropic_provider=False)
        class FakeClassifier:
            def __init__(self):
                self.called = False
            def classify(self, messages, tool_name, tool_input, context):
                self.called = True
                from agent_core.tools.classifier import ClassifierResult
                return ClassifierResult(should_block=True, unavailable=False)

        fc = FakeClassifier()
        bash_check_permissions({"command": "ls"}, ctx, classifier=fc)
        assert fc.called is False  # 非 anthropic 不调


# ────────────────────────────────────────────────────────────────────
# sandbox auto-allow 协同
# ────────────────────────────────────────────────────────────────────

class TestSandboxAutoAllow:
    @pytest.fixture
    def enabled_sandbox(self, reset_sandbox):
        reset_sandbox.load_config({"enabled": True, "autoAllowBashIfSandboxed": True})
        with patch.object(reset_sandbox, "_is_supported_platform", return_value=True), \
             patch.object(reset_sandbox, "_check_dependencies", return_value=True), \
             patch.object(reset_sandbox, "initialize", lambda: setattr(reset_sandbox, "_initialized", True)):
            yield reset_sandbox

    def test_sandbox_auto_allow_when_all_clean(self, enabled_sandbox):
        ctx = _ctx()
        decision = bash_check_permissions({"command": "npm install"}, ctx)
        assert decision.behavior == PermissionBehavior.ALLOW.value
        assert "Auto-allowed in sandbox" in (decision.decision_reason.reason or "")

    def test_sandbox_auto_allow_respects_deny(self, enabled_sandbox):
        ctx = _ctx(always_deny_rules={"projectSettings": ["Bash(rm:*)"]})
        decision = bash_check_permissions({"command": "rm -rf /"}, ctx)
        assert decision.behavior == PermissionBehavior.DENY.value

    def test_sandbox_disabled_no_auto_allow(self, reset_sandbox):
        # sandbox 禁用 → 不走 auto-allow
        ctx = _ctx()
        decision = bash_check_permissions({"command": "npm install"}, ctx)
        assert decision.behavior == PermissionBehavior.PASSTHROUGH.value

    def test_sandbox_auto_allow_false_no_auto_allow(self, reset_sandbox):
        reset_sandbox.load_config({
            "enabled": True,
            "autoAllowBashIfSandboxed": False,
        })
        with patch.object(reset_sandbox, "_is_supported_platform", return_value=True), \
             patch.object(reset_sandbox, "_check_dependencies", return_value=True), \
             patch.object(reset_sandbox, "initialize", lambda: setattr(reset_sandbox, "_initialized", True)):
            ctx = _ctx()
            decision = bash_check_permissions({"command": "npm install"}, ctx)
        assert decision.behavior == PermissionBehavior.PASSTHROUGH.value

    def test_dangerously_disable_skips_auto_allow_only(self, enabled_sandbox):
        # dangerously_disable_sandbox=True → should_use_sandbox=False → 不走 auto-allow
        # 但 deny rule 仍生效(走正常 pipeline)
        ctx = _ctx(always_deny_rules={"projectSettings": ["Bash(rm:*)"]})
        decision = bash_check_permissions(
            {"command": "rm -rf /", "dangerously_disable_sandbox": True}, ctx,
        )
        assert decision.behavior == PermissionBehavior.DENY.value


# ────────────────────────────────────────────────────────────────────
# check_sandbox_auto_allow
# ────────────────────────────────────────────────────────────────────

class TestCheckSandboxAutoAllow:
    @pytest.fixture
    def enabled_sandbox(self, reset_sandbox):
        reset_sandbox.load_config({"enabled": True})
        with patch.object(reset_sandbox, "_is_supported_platform", return_value=True), \
             patch.object(reset_sandbox, "_check_dependencies", return_value=True), \
             patch.object(reset_sandbox, "initialize", lambda: setattr(reset_sandbox, "_initialized", True)):
            yield reset_sandbox

    def test_all_clean_returns_allow(self, enabled_sandbox):
        decision = check_sandbox_auto_allow({"command": "npm install"}, _ctx())
        assert decision.behavior == PermissionBehavior.ALLOW.value

    def test_deny_subcommand_returns_deny(self, enabled_sandbox):
        ctx = _ctx(always_deny_rules={"projectSettings": ["Bash(rm:*)"]})
        decision = check_sandbox_auto_allow({"command": "rm -rf /"}, ctx)
        assert decision.behavior == PermissionBehavior.DENY.value

    def test_deny_in_compound_blocks_whole(self, enabled_sandbox):
        ctx = _ctx(always_deny_rules={"projectSettings": ["Bash(rm:*)"]})
        decision = check_sandbox_auto_allow(
            {"command": "echo a && rm -rf / && echo b"}, ctx,
        )
        assert decision.behavior == PermissionBehavior.DENY.value

    def test_empty_command_asks(self, enabled_sandbox):
        decision = check_sandbox_auto_allow({"command": ""}, _ctx())
        assert decision.behavior == PermissionBehavior.ASK.value

    def test_max_subcommands_asks(self, enabled_sandbox):
        cmd = " ; ".join(["echo x"] * 51)
        decision = check_sandbox_auto_allow({"command": cmd}, _ctx())
        assert decision.behavior == PermissionBehavior.ASK.value

    def test_allow_reason_mentions_sandbox(self, enabled_sandbox):
        decision = check_sandbox_auto_allow({"command": "ls"}, _ctx())
        assert "Auto-allowed in sandbox" in (decision.decision_reason.reason or "")


# ────────────────────────────────────────────────────────────────────
# SubcommandResultsReason 内容
# ────────────────────────────────────────────────────────────────────

class TestSubcommandResultsReason:
    def test_deny_includes_counts(self):
        ctx = _ctx(always_deny_rules={"projectSettings": ["Bash(rm:*)"]})
        decision = bash_check_permissions(
            {"command": "ls && rm -rf / && echo done"}, ctx,
        )
        assert decision.behavior == PermissionBehavior.DENY.value
        reason = decision.decision_reason
        assert reason.type == "subcommandResults"
        assert reason.deny_count >= 1
