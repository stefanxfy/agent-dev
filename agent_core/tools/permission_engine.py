"""
Permission Engine — 7-step 决策引擎(对齐 doc §4.3)

对齐 Claude Code src/utils/permissions/permissions.ts(checkPermissions):
1a. 全局 deny rule → DENY(RuleReason)
1b. 全局 ask rule → ASK(RuleReason)
1c. tool.check_permissions(input, ctx) → DENY 终止
1d. requires_user_interaction + ASK → ASK
1e. safety_check 命中 → ASK(SafetyCheckReason)
2a. mode == bypassPermissions → ALLOW(ModeReason)
2b. tool 全局 allow rule → ALLOW(RuleReason)
3. passthrough → ASK
4. mode 后处理:
   - dontAsk → ASK → DENY
   - auto → classifier.fast_path → classifier.classify
   - should_avoid_permission_prompts → auto-deny(AsyncAgentReason)

输出 PermissionDecision 写入 audit_logger(本步先用 placeholder)
"""

from __future__ import annotations

import logging
from dataclasses import asdict
from typing import Any, Callable, Optional

from .classifier import (
    ClassifierResult,
    HaikuClassifier,
    is_classifier_enabled,
)
from .classifier_fast_path import check_classifier_fast_path
from .denial_tracking import (
    DenialTrackingState,
    check_denial_limit,
    record_denial,
    record_success,
)
from .permission_hook import HookRegistry, PreToolUseResult
from .permission_types import (
    AsyncAgentReason,
    ClassifierReason,
    ModeReason,
    OtherReason,
    PermissionBehavior,
    PermissionDecision,
    PermissionMode,
    PermissionRule,
    PermissionRuleData,
    PermissionRuleSource,
    RuleReason,
    SafetyCheckReason,
    ToolPermissionContext,
)
from .safety_check import safety_check


logger = logging.getLogger(__name__)

# 🛡️ permission 子系统统一 logger — 可被 AGENT_LOG_PERMISSION 单独调级别
permission_logger = logging.getLogger("agent_core.permission")


# ────────────────────────────────────────────────────────────────────
# PermissionEngine — 主类
# ────────────────────────────────────────────────────────────────────

class PermissionEngine:
    """
    权限决策引擎(对齐 doc §4.3 + CC permissions.ts)

    完整 pipeline(check_permissions):
      Step 1a: 全局 deny rule
      Step 1b: 全局 ask rule
      Step 1c: tool.check_permissions
      Step 1d: requires_user_interaction → ASK
      Step 1e: safety_check
      Step 2a: bypass mode → ALLOW
      Step 2b: tool global allow rule → ALLOW
      Step 3: passthrough / global ask → ASK
      Step 4: mode 后处理(dontAsk / auto / async)
      Step 5: hook chain(PreToolUse)
      Step 6: denial limit check
      Step 7: 写 audit_logger(placeholder)
    """

    def __init__(
        self,
        context: ToolPermissionContext,
        hook_registry: Optional[HookRegistry] = None,
        classifier: Optional[HaikuClassifier] = None,
        denial_state: Optional[DenialTrackingState] = None,
        audit_logger: Optional[Any] = None,
        provider: str = "anthropic",
    ):
        """
        Args:
            context: ToolPermissionContext(对齐 CC)
            hook_registry: PreToolUse hook 注册表
            classifier: Haiku classifier(M1 stub 默认)
            denial_state: 当前 deny 计数 state
            audit_logger: 审计日志(本步先用 None,M2 实装)
            provider: LLM provider 名(classifier enable 判定用)
        """
        self.context = context
        self.hook_registry = hook_registry or HookRegistry()
        self.classifier = classifier or HaikuClassifier()
        self.denial_state = denial_state or DenialTrackingState()
        self.audit_logger = audit_logger
        self.provider = provider

    # ── 主入口:check_permissions ──────────────────────────────

    def check_permissions(
        self,
        tool: Any,
        tool_input: dict,
        messages: Optional[list[dict]] = None,
    ) -> PermissionDecision:
        """
        同步决策权限(对齐 doc §4.3 + CC checkPermissions)

        Args:
            tool: ToolDef 实例(duck-typed)
            tool_input: 工具输入参数 dict
            messages: 对话历史(classifier 用,可空)

        Returns:
            PermissionDecision(behavior / decision_reason / updated_input / message)
        """
        tool_name = getattr(tool, "name", "unknown")

        # 🛡️ [engine_entry] 决策入口 — 7 步 pipeline 起点
        permission_logger.debug(
            "🛡️ [engine_entry] tool=%s input=%s mode=%s",
            tool_name, tool_input, getattr(self.context, "mode", "?"),
        )

        # ── Step 1a: 全局 deny rule ──────────────────────────────
        permission_logger.debug("🛡️ [step_1a_global_deny] checking deny rules for tool=%s", tool_name)
        deny_rule = self._check_global_deny_rule(tool_name)
        if deny_rule is not None:
            permission_logger.info(
                "🛡️ [step_1a_global_deny] HIT tool=%s rule=%s",
                tool_name, deny_rule,
            )
            return self._log_and_return(
                tool_name, tool_input,
                PermissionDecision(
                    behavior=PermissionBehavior.DENY.value,
                    decision_reason=RuleReason(
                        rule=PermissionRuleData.from_dataclass(deny_rule),
                        reason=f"global deny rule: {deny_rule}",
                    ),
                    message=f"Denied by rule: {deny_rule}",
                ),
                stage="step_1a_global_deny",
            )

        # ── Step 1b: 全局 ask rule ──────────────────────────────
        permission_logger.debug("🛡️ [step_1b_global_ask] checking ask rules for tool=%s", tool_name)
        ask_rule = self._check_global_ask_rule(tool_name)
        if ask_rule is not None:
            permission_logger.info(
                "🛡️ [step_1b_global_ask] HIT tool=%s rule=%s",
                tool_name, ask_rule,
            )
            return self._log_and_return(
                tool_name, tool_input,
                PermissionDecision(
                    behavior=PermissionBehavior.ASK.value,
                    decision_reason=RuleReason(
                        rule=PermissionRuleData.from_dataclass(ask_rule),
                        reason=f"global ask rule: {ask_rule}",
                    ),
                    message=f"Asked by rule: {ask_rule}",
                ),
                stage="step_1b_global_ask",
            )

        # ── Step 1c: tool.check_permissions ──────────────────────
        check_permissions_fn = getattr(tool, "check_permissions", None)
        permission_logger.debug(
            "🛡️ [step_1c_tool_check] tool=%s has_check_permissions=%s",
            tool_name, check_permissions_fn is not None,
        )
        if check_permissions_fn is not None:
            try:
                tool_decision = check_permissions_fn(tool_input, self.context)
                if tool_decision.behavior == PermissionBehavior.DENY.value:
                    permission_logger.info(
                        "🛡️ [step_1c_tool_check_deny] tool=%s behavior=deny reason=%s",
                        tool_name, (getattr(tool_decision, "message", "") or "")[:120],
                    )
                    return self._log_and_return(
                        tool_name, tool_input,
                        tool_decision,
                        stage="step_1c_tool_check_deny",
                    )
            except Exception as e:
                logger.warning(
                    "tool %s check_permissions 抛异常: %s — 降级继续",
                    tool_name, e,
                )

        # ── Step 1c': BashTool 专属(对齐 spec §4.5)─────────────
        # Bash 是最容易被 prompt injection 利用的工具,所有 Bash 调用走
        # bash_check_permissions(subcommand 级 rule + classifier + sandbox auto-allow)
        # ToolDef.check_permissions 保持 None — 由这里专属路径调,避免闭包循环 import
        if tool_name == "Bash":
            permission_logger.debug("🛡️ [step_1c_bash] tool=Bash → run bash_check_permissions")
            try:
                bash_decision = self._run_bash_check_permissions(tool_input)
            except Exception as e:
                # defense in depth:_run_bash_check_permissions 内部已 try/except,
                # 这里再包一层,任何意外都不破坏主 pipeline
                logger.warning(
                    "_run_bash_check_permissions 异常: %s — 降级继续", e,
                )
                bash_decision = None
            if bash_decision is not None:
                if bash_decision.behavior == PermissionBehavior.DENY.value:
                    permission_logger.info(
                        "🛡️ [step_1c_bash_deny] tool=Bash reason=%s",
                        (getattr(bash_decision, "message", "") or "")[:120],
                    )
                    return self._log_and_return(
                        tool_name, tool_input,
                        bash_decision,
                        stage="step_1c_bash_deny",
                    )
                if bash_decision.behavior == PermissionBehavior.ASK.value:
                    permission_logger.info(
                        "🛡️ [step_1c_bash_ask] tool=Bash reason=%s",
                        (getattr(bash_decision, "message", "") or "")[:120],
                    )
                    return self._log_and_return(
                        tool_name, tool_input,
                        bash_decision,
                        stage="step_1c_bash_ask",
                    )
                # ALLOW → 直接返(不走后续 rule match;bash sandbox auto-allow 已覆盖)
                if bash_decision.behavior == PermissionBehavior.ALLOW.value:
                    permission_logger.info(
                        "🛡️ [step_1c_bash_allow] tool=Bash reason=%s",
                        (getattr(bash_decision, "message", "") or "")[:120],
                    )
                    return self._log_and_return(
                        tool_name, tool_input,
                        bash_decision,
                        stage="step_1c_bash_allow",
                    )
                # PASSTHROUGH → fall through 到后续 Step 1d+ 继续判断
                permission_logger.debug(
                    "🛡️ [step_1c_bash_passthrough] tool=Bash fall through to 1d+",
                )

        # ── Step 1d: requires_user_interaction ──────────────────
        requires_user = getattr(tool, "requires_user_interaction", False)
        permission_logger.debug(
            "🛡️ [step_1d_requires_user] tool=%s requires_user_interaction=%s",
            tool_name, requires_user,
        )
        if requires_user:
            permission_logger.info(
                "🛡️ [step_1d_requires_user] HIT tool=%s → ASK",
                tool_name,
            )
            return self._log_and_return(
                tool_name, tool_input,
                PermissionDecision(
                    behavior=PermissionBehavior.ASK.value,
                    decision_reason=OtherReason(
                        reason=f"tool {tool_name} requires user interaction",
                    ),
                    message=f"Tool {tool_name} requires user interaction",
                ),
                stage="step_1d_requires_user",
            )

        # ── Step 1e: safety_check ───────────────────────────────
        permission_logger.debug("🛡️ [step_1e_safety_check] running for tool=%s", tool_name)
        if safety_check(tool_name, tool_input):
            permission_logger.info(
                "🧪 [step_1e_safety_check] HIT tool=%s → ASK (敏感路径或含 secret)",
                tool_name,
            )
            return self._log_and_return(
                tool_name, tool_input,
                PermissionDecision(
                    behavior=PermissionBehavior.ASK.value,
                    decision_reason=SafetyCheckReason(
                        reason=f"safety check flagged {tool_name} input",
                        classifier_approvable=False,
                    ),
                    message="Safety check flagged this action",
                ),
                stage="step_1e_safety_check",
            )

        # ── Step 1.5: hook chain(PreToolUse) 在 safety_check 之后、
        #    bypass mode 之前;hook 可覆盖后续 global allow(对齐 CC)───
        hook_count_1_5 = len(self.hook_registry.list_hooks("PreToolUse")) if self.hook_registry else 0
        permission_logger.debug(
            "🛡️ [step_1_5_hook] running PreToolUse hook chain (n=%d)",
            hook_count_1_5,
        )
        hook_result = self.hook_registry.run_pre_tool_use(
            tool_name, tool_input, self.context,
        )
        if hook_result.behavior == PermissionBehavior.DENY.value:
            permission_logger.info(
                "🛡️ [step_1_5_hook_deny] hook=%s reason=%s",
                hook_result.hook_name, (hook_result.reason or "")[:120],
            )
            return self._log_and_return(
                tool_name, tool_input,
                PermissionDecision(
                    behavior=PermissionBehavior.DENY.value,
                    decision_reason=OtherReason(
                        reason=f"hook {hook_result.hook_name} denied: {hook_result.reason or 'no reason'}",
                    ),
                    message=hook_result.reason,
                ),
                stage="step_1_5_hook_deny",
            )
        if hook_result.behavior == PermissionBehavior.ASK.value:
            permission_logger.info(
                "🛡️ [step_1_5_hook_ask] hook=%s reason=%s",
                hook_result.hook_name, (hook_result.reason or "")[:120],
            )
            updated = hook_result.updated_input or tool_input
            return self._log_and_return(
                tool_name, tool_input,
                PermissionDecision(
                    behavior=PermissionBehavior.ASK.value,
                    decision_reason=OtherReason(
                        reason=f"hook {hook_result.hook_name} asked: {hook_result.reason or 'no reason'}",
                    ),
                    updated_input=updated,
                    message=hook_result.reason,
                ),
                stage="step_1_5_hook_ask",
            )

        # ── Step 2a: bypass mode ────────────────────────────────
        permission_logger.debug(
            "🛡️ [step_2a_bypass] mode=%s",
            getattr(self.context, "mode", "?"),
        )
        if self.context.mode == PermissionMode.BYPASS.value:
            permission_logger.info(
                "🛡️ [step_2a_bypass_mode] mode=bypassPermissions → ALLOW tool=%s",
                tool_name,
            )
            return self._log_and_return(
                tool_name, tool_input,
                PermissionDecision(
                    behavior=PermissionBehavior.ALLOW.value,
                    decision_reason=ModeReason(
                        mode=PermissionMode.BYPASS.value,
                        reason="bypassPermissions mode",
                    ),
                ),
                stage="step_2a_bypass_mode",
            )

        # ── Step 2b: tool 全局 allow rule ───────────────────────
        permission_logger.debug(
            "🛡️ [step_2b_global_allow] checking allow rules for tool=%s",
            tool_name,
        )
        allow_rule = self._check_global_allow_rule(tool_name)
        if allow_rule is not None:
            permission_logger.info(
                "🛡️ [step_2b_global_allow] HIT tool=%s rule=%s",
                tool_name, allow_rule,
            )
            return self._log_and_return(
                tool_name, tool_input,
                PermissionDecision(
                    behavior=PermissionBehavior.ALLOW.value,
                    decision_reason=RuleReason(
                        rule=PermissionRuleData.from_dataclass(allow_rule),
                        reason=f"global allow rule: {allow_rule}",
                    ),
                ),
                stage="step_2b_global_allow",
            )

        # ── Step 3: classifier fast-path ────────────────────────
        permission_logger.debug("🛡️ [step_3_fast_path] running check_classifier_fast_path")
        fast_path = check_classifier_fast_path(tool, tool_input, self.context)
        if fast_path.hit:
            permission_logger.info(
                "🛡️ [step_3_fast_path_hit] stage=%s behavior=%s reason=%s",
                fast_path.stage, fast_path.behavior,
                getattr(fast_path, "reason", "")[:120] if hasattr(fast_path, "reason") else "",
            )
            return self._log_and_return(
                tool_name, tool_input,
                fast_path.to_permission_decision(),
                stage=f"step_3_fast_path_{fast_path.stage}",
            )

        # ── Step 4: mode 后处理 ─────────────────────────────────
        # auto mode → classifier(classifier 在 fast-path 阶段未命中才用)
        classifier_enabled = (
            self.context.mode == PermissionMode.AUTO.value
            and is_classifier_enabled(
                provider=self.provider,
                mode=PermissionMode.AUTO,
                no_settings_match=self.context.no_settings_match,
            )
        )
        permission_logger.debug(
            "🛡️ [step_4_classifier] mode=%s classifier_enabled=%s",
            self.context.mode, classifier_enabled,
        )
        if classifier_enabled:
            import time as _classifier_time
            _cls_t0 = _classifier_time.time()
            result = self.classifier.classify(
                messages or [],
                tool_name,
                tool_input,
                self.context,
            )
            _cls_ms = (_classifier_time.time() - _cls_t0) * 1000
            permission_logger.info(
                "🤖 [step_4_classifier_result] tool=%s should_block=%s unavailable=%s "
                "duration_ms=%.1f reason=%s",
                tool_name, result.should_block, result.unavailable,
                _cls_ms, (result.reason or "")[:120],
            )
            if not result.unavailable and result.should_block:
                return self._log_and_return(
                    tool_name, tool_input,
                    PermissionDecision(
                        behavior=PermissionBehavior.DENY.value,
                        decision_reason=ClassifierReason(
                            classifier=result.model,
                            reason=result.reason,
                        ),
                        message=f"Classifier denied: {result.reason}",
                    ),
                    stage="step_4_classifier_deny",
                )
            elif not result.unavailable and not result.should_block:
                return self._log_and_return(
                    tool_name, tool_input,
                    PermissionDecision(
                        behavior=PermissionBehavior.ALLOW.value,
                        decision_reason=ClassifierReason(
                            classifier=result.model,
                            reason=result.reason,
                        ),
                        message=f"Classifier allowed: {result.reason}",
                    ),
                    stage="step_4_classifier_allow",
                )
            # unavailable → fall through

        # should_avoid_permission_prompts(后台 agent)→ auto-deny
        if self.context.should_avoid_permission_prompts:
            permission_logger.info(
                "🛡️ [step_4_async_agent] should_avoid_permission_prompts=True → DENY tool=%s",
                tool_name,
            )
            return self._log_and_return(
                tool_name, tool_input,
                PermissionDecision(
                    behavior=PermissionBehavior.DENY.value,
                    decision_reason=AsyncAgentReason(
                        reason="async agent without user prompts",
                    ),
                ),
                stage="step_4_async_agent",
            )

        # dontAsk mode → ASK 强制转 DENY(到这步还没匹配 → 默认 ASK → 转 DENY)
        if self.context.mode == PermissionMode.DONT_ASK.value:
            permission_logger.info(
                "🛡️ [step_4_dontask] mode=dontAsk → DENY tool=%s",
                tool_name,
            )
            return self._log_and_return(
                tool_name, tool_input,
                PermissionDecision(
                    behavior=PermissionBehavior.DENY.value,
                    decision_reason=ModeReason(
                        mode=PermissionMode.DONT_ASK.value,
                        reason="dontAsk mode auto-denies",
                    ),
                ),
                stage="step_4_dontask",
            )

        # ── Step 5: hook chain(PreToolUse)───────────────────────
        hook_count_5 = len(self.hook_registry.list_hooks("PreToolUse")) if self.hook_registry else 0
        permission_logger.debug(
            "🛡️ [step_5_hook] running PreToolUse hook chain (n=%d)",
            hook_count_5,
        )
        hook_result = self.hook_registry.run_pre_tool_use(
            tool_name, tool_input, self.context,
        )
        if hook_result.behavior == PermissionBehavior.DENY.value:
            permission_logger.info(
                "🛡️ [step_5_hook_deny] hook=%s reason=%s",
                hook_result.hook_name, (hook_result.reason or "")[:120],
            )
            return self._log_and_return(
                tool_name, tool_input,
                PermissionDecision(
                    behavior=PermissionBehavior.DENY.value,
                    decision_reason=OtherReason(
                        reason=f"hook {hook_result.hook_name} denied: {hook_result.reason or 'no reason'}",
                    ),
                    message=hook_result.reason,
                ),
                stage="step_5_hook_deny",
            )
        if hook_result.behavior == PermissionBehavior.ASK.value:
            permission_logger.info(
                "🛡️ [step_5_hook_ask] hook=%s reason=%s",
                hook_result.hook_name, (hook_result.reason or "")[:120],
            )
            updated = hook_result.updated_input or tool_input
            return self._log_and_return(
                tool_name, tool_input,
                PermissionDecision(
                    behavior=PermissionBehavior.ASK.value,
                    decision_reason=OtherReason(
                        reason=f"hook {hook_result.hook_name} asked: {hook_result.reason or 'no reason'}",
                    ),
                    updated_input=updated,
                    message=hook_result.reason,
                ),
                stage="step_5_hook_ask",
            )

        # ── Step 6: denial limit ─────────────────────────────────
        consecutive = getattr(self.denial_state, "consecutive_denials", 0)
        total = getattr(self.denial_state, "total_denials", 0)
        permission_logger.debug(
            "🛡️ [step_6_denial_limit] consecutive=%d total=%d",
            consecutive, total,
        )
        limit_decision = check_denial_limit(self.denial_state)
        if limit_decision is not None:
            permission_logger.info(
                "🛡️ [step_6_denial_limit] HIT tool=%s consecutive=%d total=%d",
                tool_name, consecutive, total,
            )
            return self._log_and_return(
                tool_name, tool_input,
                limit_decision,
                stage="step_6_denial_limit",
            )

        # ── Step 7: 默认 ASK(passthrough)───────────────────────
        permission_logger.info(
            "🛡️ [step_7_default_ask] no matching rule → ASK tool=%s",
            tool_name,
        )
        return self._log_and_return(
            tool_name, tool_input,
            PermissionDecision(
                behavior=PermissionBehavior.ASK.value,
                decision_reason=OtherReason(
                    reason="no matching rule, default ask",
                ),
                message="No matching permission rule",
            ),
            stage="step_7_default_ask",
        )

    # ── 全局 rule 查找 helper ──────────────────────────────────

    def _run_bash_check_permissions(self, tool_input: dict) -> Optional[PermissionDecision]:
        """
        调用 bash_check_permissions(对齐 spec §4.5 + §6.3)

        lazy import 避免模块加载时强制依赖(测试可独立 mock)。
        classifier 注入 self.classifier(ANT-only stub 默认 unavailable)。

        Returns:
            PermissionDecision 或 None(调用失败时返 None,让 engine 继续 fall through)
        """
        try:
            from .bash_permissions import bash_check_permissions
            return bash_check_permissions(
                tool_input,
                self.context,
                classifier=self.classifier,
            )
        except Exception as e:
            logger.warning(
                "bash_check_permissions 异常: %s — 降级继续正常 pipeline",
                e,
            )
            return None

    def _check_global_deny_rule(self, tool_name: str) -> Optional[PermissionRule]:
        """检查 tool 是否命中全局 deny rule(按 source 优先级,第一个匹配胜出)"""
        for source in PermissionRuleSource.ordered_sources():
            rules = self.context.always_deny_rules.get(source.value, [])
            for rule_str in rules:
                rule = self._parse_rule_str(rule_str, source, PermissionBehavior.DENY)
                if rule and rule.tool_name == tool_name:
                    return rule
        return None

    def _check_global_ask_rule(self, tool_name: str) -> Optional[PermissionRule]:
        """检查 tool 是否命中全局 ask rule"""
        for source in PermissionRuleSource.ordered_sources():
            rules = self.context.always_ask_rules.get(source.value, [])
            for rule_str in rules:
                rule = self._parse_rule_str(rule_str, source, PermissionBehavior.ASK)
                if rule and rule.tool_name == tool_name:
                    return rule
        return None

    def _check_global_allow_rule(self, tool_name: str) -> Optional[PermissionRule]:
        """检查 tool 是否命中全局 allow rule"""
        for source in PermissionRuleSource.ordered_sources():
            rules = self.context.always_allow_rules.get(source.value, [])
            for rule_str in rules:
                rule = self._parse_rule_str(rule_str, source, PermissionBehavior.ALLOW)
                if rule and rule.tool_name == tool_name:
                    return rule
        return None

    def _parse_rule_str(
        self,
        rule_str: str,
        source: PermissionRuleSource,
        behavior: PermissionBehavior,
    ) -> Optional[PermissionRule]:
        """
        从 "Bash(rm:*)" / "Edit" 字符串解析 PermissionRule

        注:完整 parse 在 permission_matcher.parse_all_rules_from_strings 里;
        这里简化版只切 (tool_name, rule_content) 形态
        """
        import re
        from .permission_types import PermissionRuleValue
        rule_str = rule_str.strip()
        if not rule_str:
            return None
        match = re.match(r"^([A-Za-z][A-Za-z0-9_]*)\s*\((.*)\)\s*$", rule_str, re.DOTALL)
        if match:
            tool_name = match.group(1)
            rule_content = match.group(2).strip()
        else:
            tool_name = rule_str
            rule_content = None
        return PermissionRule(
            source=source,
            behavior=behavior,
            value=PermissionRuleValue(
                tool_name=tool_name,
                rule_content=rule_content,
            ),
        )

    # ── audit_logger + 状态更新 ────────────────────────────────

    def _log_and_return(
        self,
        tool_name: str,
        tool_input: dict,
        decision: PermissionDecision,
        stage: str,
    ) -> PermissionDecision:
        """
        写 audit log(如果 audit_logger 存在)+ 更新 deny state + 返回 decision

        这是唯一审计点(对齐 doc §4.8):engine 每条 decision 都经此,
        记录 stage + context + classifier + denial_state。
        """
        # 🛡️ [decision] 同步打 INFO:每条决策都进主日志(与 audit.jsonl 互为补充)
        try:
            reason_type = "unknown"
            reason_text = ""
            if decision.decision_reason is not None:
                reason_type = getattr(decision.decision_reason, "type", "unknown") or "unknown"
                reason_text = getattr(decision.decision_reason, "reason", "") or ""
            permission_logger.info(
                "🛡️ [decision] stage=%s tool=%s behavior=%s reason_type=%s reason=%s",
                stage, tool_name, decision.behavior, reason_type, reason_text[:120],
            )
        except Exception as _log_e:
            logger.warning("_log_and_return INFO 日志失败: %s", _log_e)

        # 更新 deny state
        if decision.behavior == PermissionBehavior.DENY.value:
            self.denial_state = record_denial(self.denial_state)
        elif decision.behavior == PermissionBehavior.ALLOW.value:
            self.denial_state = record_success(self.denial_state)

        # 写 audit log
        if self.audit_logger is not None:
            try:
                self.audit_logger.log(
                    tool_name=tool_name,
                    tool_input=tool_input,
                    decision=decision,
                    context=self.context,
                    stage=stage,
                    hook_chain=self.hook_registry.list_hooks("PreToolUse")
                    if self.hook_registry else [],
                    classifier_used=self.classifier is not None,
                    denial_state=asdict(self.denial_state),
                )
            except Exception as e:
                logger.warning("audit_logger.log 失败: %s", e)

        return decision

    # ── 状态查询 ──────────────────────────────────────────────

    def get_denial_state(self) -> DenialTrackingState:
        """获取当前 deny state(测试用)"""
        return self.denial_state
