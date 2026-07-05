"""
permission_types.py 测试

覆盖:
1. Enum 值与排序(PermissionRuleSource / PermissionMode / PermissionBehavior)
2. PermissionRule dataclass 构造 + __str__
3. Pydantic discriminated union 序列化/反序列化(11 种 reason)
4. PermissionDecision 构造 + 嵌套 reason
5. ToolPermissionContext 默认值 + get_all_*_rules
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from agent_core.tools.permission_types import (
    AdditionalWorkingDirectory,
    ModeReason,
    PermissionBehavior,
    PermissionDecision,
    PermissionDecisionReason,
    PermissionMode,
    PermissionRule,
    PermissionRuleData,
    PermissionRuleSource,
    PermissionRuleValue,
    RuleReason,
    SafetyCheckReason,
    ToolPermissionContext,
    ClassifierReason,
    HookReason,
)


# ────────────────────────────────────────────────────────────────────
# PermissionRuleSource — 8 source + 排序
# ────────────────────────────────────────────────────────────────────

class TestPermissionRuleSource:
    def test_has_8_sources(self):
        """8 source 完整定义(对齐 doc §4.6)"""
        assert len(PermissionRuleSource) == 8
        assert PermissionRuleSource.COMMAND.value == "command"
        assert PermissionRuleSource.SESSION.value == "session"
        assert PermissionRuleSource.LOCAL.value == "localSettings"
        assert PermissionRuleSource.PROJECT.value == "projectSettings"
        assert PermissionRuleSource.USER.value == "userSettings"
        assert PermissionRuleSource.CLI_ARG.value == "cliArg"
        assert PermissionRuleSource.POLICY.value == "policySettings"
        assert PermissionRuleSource.FLAG.value == "flagSettings"

    def test_ordered_sources_priority_low_to_high(self):
        """ordered_sources() 按优先级从低到高(command < flag)"""
        ordered = PermissionRuleSource.ordered_sources()
        assert ordered[0] == PermissionRuleSource.COMMAND
        assert ordered[-1] == PermissionRuleSource.FLAG
        # policy > cliArg
        assert ordered.index(PermissionRuleSource.POLICY) > ordered.index(PermissionRuleSource.CLI_ARG)
        # project > local
        assert ordered.index(PermissionRuleSource.PROJECT) > ordered.index(PermissionRuleSource.LOCAL)

    def test_source_is_str_enum(self):
        """str Enum,可直接当字符串用"""
        assert PermissionRuleSource.POLICY == "policySettings"
        assert f"{PermissionRuleSource.POLICY}" == "PermissionRuleSource.POLICY"


# ────────────────────────────────────────────────────────────────────
# PermissionMode — 7 mode
# ────────────────────────────────────────────────────────────────────

class TestPermissionMode:
    def test_has_7_modes(self):
        """7 mode 完整定义(对齐 doc §4.1)"""
        assert len(PermissionMode) == 7
        assert PermissionMode.DEFAULT.value == "default"
        assert PermissionMode.ACCEPT_EDITS.value == "acceptEdits"
        assert PermissionMode.BYPASS.value == "bypassPermissions"
        assert PermissionMode.DONT_ASK.value == "dontAsk"
        assert PermissionMode.PLAN.value == "plan"
        assert PermissionMode.AUTO.value == "auto"
        assert PermissionMode.BUBBLE.value == "bubble"


# ────────────────────────────────────────────────────────────────────
# PermissionBehavior — 4 行为
# ────────────────────────────────────────────────────────────────────

class TestPermissionBehavior:
    def test_has_4_behaviors(self):
        """4 行为(allow/deny/ask/passthrough)"""
        assert len(PermissionBehavior) == 4
        assert PermissionBehavior.ALLOW.value == "allow"
        assert PermissionBehavior.DENY.value == "deny"
        assert PermissionBehavior.ASK.value == "ask"
        assert PermissionBehavior.PASSTHROUGH.value == "passthrough"


# ────────────────────────────────────────────────────────────────────
# PermissionRule + PermissionRuleValue — dataclass
# ────────────────────────────────────────────────────────────────────

class TestPermissionRule:
    def test_construct_with_content(self):
        """带 rule_content 的规则构造"""
        rule = PermissionRule(
            source=PermissionRuleSource.PROJECT,
            behavior=PermissionBehavior.DENY,
            value=PermissionRuleValue(tool_name="Bash", rule_content="rm:*"),
        )
        assert rule.tool_name == "Bash"
        assert rule.rule_content == "rm:*"
        assert rule.source == PermissionRuleSource.PROJECT
        assert rule.behavior == PermissionBehavior.DENY

    def test_construct_without_content(self):
        """无 rule_content 的规则构造(整个 tool 命中)"""
        rule = PermissionRule(
            source=PermissionRuleSource.PROJECT,
            behavior=PermissionBehavior.ALLOW,
            value=PermissionRuleValue(tool_name="Edit"),
        )
        assert rule.tool_name == "Edit"
        assert rule.rule_content is None

    def test_empty_tool_name_raises(self):
        """空 tool_name 抛 ValueError"""
        with pytest.raises(ValueError, match="tool_name 必须非空"):
            PermissionRule(
                source=PermissionRuleSource.PROJECT,
                behavior=PermissionBehavior.ALLOW,
                value=PermissionRuleValue(tool_name=""),
            )

    def test_str_format(self):
        """__str__ 与 CC formatPermissionRule 对齐"""
        rule = PermissionRule(
            source=PermissionRuleSource.PROJECT,
            behavior=PermissionBehavior.DENY,
            value=PermissionRuleValue(tool_name="Bash", rule_content="rm:*"),
        )
        assert str(rule) == "Bash(rm:*)"

        rule_no_content = PermissionRule(
            source=PermissionRuleSource.PROJECT,
            behavior=PermissionBehavior.ALLOW,
            value=PermissionRuleValue(tool_name="Edit"),
        )
        assert str(rule_no_content) == "Edit"

    def test_frozen_dataclass(self):
        """dataclass(frozen=True) 不可变"""
        rule = PermissionRule(
            source=PermissionRuleSource.PROJECT,
            behavior=PermissionBehavior.DENY,
            value=PermissionRuleValue(tool_name="Bash", rule_content="rm:*"),
        )
        with pytest.raises(Exception):  # FrozenInstanceError
            rule.tool_name = "Read"


# ────────────────────────────────────────────────────────────────────
# PermissionDecisionReason — 11 变体 Pydantic
# ────────────────────────────────────────────────────────────────────

class TestPermissionDecisionReason:
    def test_rule_reason_construct(self):
        """RuleReason 构造 + nested PermissionRuleData"""
        reason = RuleReason(
            rule=PermissionRuleData(
                source="projectSettings",
                behavior="deny",
                tool_name="Bash",
                rule_content="rm:*",
            ),
            reason="user denied rm",
        )
        assert reason.type == "rule"
        assert reason.rule.tool_name == "Bash"
        assert reason.reason == "user denied rm"

    def test_mode_reason_construct(self):
        """ModeReason 构造"""
        reason = ModeReason(mode="bypassPermissions", reason="bypass mode on")
        assert reason.type == "mode"
        assert reason.mode == "bypassPermissions"

    def test_safety_check_reason_construct(self):
        """SafetyCheckReason 构造(classifier_approvable 字段)"""
        reason = SafetyCheckReason(
            reason=".agent_data/ blocked",
            classifier_approvable=True,
        )
        assert reason.type == "safetyCheck"
        assert reason.classifier_approvable is True

    def test_classifier_reason_construct(self):
        """ClassifierReason 构造"""
        reason = ClassifierReason(classifier="bash_deny", reason="destructive")
        assert reason.type == "classifier"
        assert reason.classifier == "bash_deny"

    def test_hook_reason_construct(self):
        """HookReason 构造"""
        reason = HookReason(
            hook_name="secret-scan",
            hook_source="localSettings",
            reason="sk- key detected",
        )
        assert reason.type == "hook"
        assert reason.hook_name == "secret-scan"

    def test_type_field_is_frozen(self):
        """reason.type 字段 frozen,不允许外部覆盖"""
        reason = ModeReason(mode="default")
        with pytest.raises(ValidationError):
            reason.type = "rule"  # frozen 字段会触发 ValidationError

    def test_extra_fields_forbidden(self):
        """extra='forbid' 禁止额外字段"""
        with pytest.raises(ValidationError):
            RuleReason(
                rule=PermissionRuleData(source="project", behavior="allow", tool_name="Read"),
                unknown_field="bad",  # type: ignore
            )


# ────────────────────────────────────────────────────────────────────
# PermissionDecision — 最终决策
# ────────────────────────────────────────────────────────────────────

class TestPermissionDecision:
    def test_allow_decision(self):
        """最简单的 ALLOW 决策"""
        decision = PermissionDecision(behavior="allow")
        assert decision.behavior == "allow"
        assert decision.decision_reason is None
        assert decision.updated_input is None
        assert decision.message is None

    def test_deny_with_reason(self):
        """DENY 决策带 rule reason"""
        decision = PermissionDecision(
            behavior="deny",
            decision_reason=RuleReason(
                rule=PermissionRuleData(
                    source="projectSettings",
                    behavior="deny",
                    tool_name="Bash",
                    rule_content="rm:*",
                ),
            ),
            message="rm is not allowed",
        )
        assert decision.behavior == "deny"
        assert isinstance(decision.decision_reason, RuleReason)
        assert decision.message == "rm is not allowed"

    def test_ask_with_safety_reason(self):
        """ASK 决策带 safetyCheck reason"""
        decision = PermissionDecision(
            behavior="ask",
            decision_reason=SafetyCheckReason(
                reason="sensitive path: .agent_data/",
                classifier_approvable=False,
            ),
        )
        assert decision.behavior == "ask"
        assert decision.decision_reason.classifier_approvable is False

    def test_updated_input_field(self):
        """updated_input 字段支持 hook 改写"""
        decision = PermissionDecision(
            behavior="allow",
            updated_input={"path": "/new/path"},
        )
        assert decision.updated_input == {"path": "/new/path"}


# ────────────────────────────────────────────────────────────────────
# AdditionalWorkingDirectory — 额外工作目录
# ────────────────────────────────────────────────────────────────────

class TestAdditionalWorkingDirectory:
    def test_minimal_construct(self):
        """最少字段构造"""
        d = AdditionalWorkingDirectory(path="/tmp/shared")
        assert d.path == "/tmp/shared"
        assert d.source == "cliArg"  # default
        assert d.added_at is None

    def test_full_construct(self):
        """全部字段构造"""
        import time
        d = AdditionalWorkingDirectory(
            path="/tmp/shared",
            source="userSettings",
            added_at=time.time(),
            reason="user added via UI",
        )
        assert d.source == "userSettings"
        assert d.reason == "user added via UI"


# ────────────────────────────────────────────────────────────────────
# ToolPermissionContext — 决策上下文
# ────────────────────────────────────────────────────────────────────

class TestToolPermissionContext:
    def test_default_construct(self):
        """默认构造(mode=default, 全空 dict)"""
        ctx = ToolPermissionContext()
        assert ctx.mode == "default"
        assert ctx.additional_working_directories == {}
        assert ctx.always_allow_rules == {}
        assert ctx.always_deny_rules == {}
        assert ctx.always_ask_rules == {}
        assert ctx.sandbox_enabled is False
        assert ctx.no_settings_match is True
        assert ctx.is_anthropic_provider is True
        assert ctx.is_bypass_permissions_mode_available is False
        assert ctx.should_avoid_permission_prompts is False

    def test_with_all_fields(self):
        """填满所有字段"""
        ctx = ToolPermissionContext(
            mode="bypassPermissions",
            is_bypass_permissions_mode_available=True,
            should_avoid_permission_prompts=True,
            sandbox_enabled=True,
            no_settings_match=False,
            is_anthropic_provider=False,
            always_allow_rules={"projectSettings": ["Edit", "Read"]},
            always_deny_rules={"projectSettings": ["Bash(rm:*)"]},
            always_ask_rules={"projectSettings": ["Bash(npm publish:*)"]},
        )
        assert ctx.mode == "bypassPermissions"
        assert ctx.always_allow_rules["projectSettings"] == ["Edit", "Read"]

    def test_get_all_allow_rules_priority_order(self):
        """get_all_allow_rules 按 source 优先级排序(command < flag)"""
        ctx = ToolPermissionContext(
            always_allow_rules={
                "flagSettings": ["flag-allow"],
                "policySettings": ["policy-allow"],
                "projectSettings": ["project-allow"],
                "command": ["command-allow"],
            },
        )
        all_allow = ctx.get_all_allow_rules()
        # 按 ordered_sources() 顺序(command < session < local < project < user < cliArg < policy < flag)
        assert all_allow == [
            "command-allow",
            "project-allow",
            "policy-allow",
            "flag-allow",
        ]

    def test_get_all_deny_and_ask(self):
        """get_all_deny_rules + get_all_ask_rules 同理"""
        ctx = ToolPermissionContext(
            always_deny_rules={"projectSettings": ["deny-1"]},
            always_ask_rules={"projectSettings": ["ask-1"]},
        )
        assert ctx.get_all_deny_rules() == ["deny-1"]
        assert ctx.get_all_ask_rules() == ["ask-1"]

    def test_additional_working_directories_dict(self):
        """additional_working_directories 用 dict[str, AdditionalWorkingDirectory]"""
        d1 = AdditionalWorkingDirectory(path="/tmp/a", source="cliArg")
        d2 = AdditionalWorkingDirectory(path="/tmp/b", source="userSettings")
        ctx = ToolPermissionContext(
            additional_working_directories={
                "/tmp/a": d1,
                "/tmp/b": d2,
            },
        )
        assert ctx.additional_working_directories["/tmp/a"].source == "cliArg"
        assert len(ctx.additional_working_directories) == 2

    def test_stripped_dangerous_rules_optional(self):
        """stripped_dangerous_rules 是 Optional"""
        ctx = ToolPermissionContext()  # 默认 None
        assert ctx.stripped_dangerous_rules is None

        ctx2 = ToolPermissionContext(stripped_dangerous_rules={"projectSettings": ["Bash(*)"]})
        assert ctx2.stripped_dangerous_rules == {"projectSettings": ["Bash(*)"]}