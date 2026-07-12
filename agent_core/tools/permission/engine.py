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
from .fast_path import check_classifier_fast_path
from .denial import (
    DenialTrackingState,
    check_denial_limit,
    record_denial,
    record_success,
)
from .hook import HookRegistry, PreToolUseResult
from ._logging import permission_logger
from .rule_checker import (
    RuleChecker,
    derive_input_str,
    is_path_tool,
    parse_rule_str,
)
from .types import (
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
from .safety import safety_check


logger = logging.getLogger(__name__)

# permission_logger 从 ._logging 共享(engine + step 共用,见 _logging.py)


# ────────────────────────────────────────────────────────────────────
# PermissionEngine — 主类
# ────────────────────────────────────────────────────────────────────

class PermissionEngine:
    """
    权限决策引擎(对齐 doc §4.3 + CC permissions.ts)

    完整 pipeline(check_permissions):
      Step 1a: 全局 deny rule(内容级,T-M1 修)
      Step 1b: 全局 ask rule(内容级,T-M1 修)
      Step 1c: tool.check_permissions
      Step 1d: requires_user_interaction → ASK
      Step 1e: safety_check
      Step 2a: bypass mode → ALLOW
      Step 2b: tool global allow rule(内容级,T-M1 修)
      Step 3: passthrough / global ask → ASK
      Step 4: mode 后处理(dontAsk / auto / async)
      Step 1.5(已合入 Step 1/2 之间):hook chain(PreToolUse)
      Step 6: denial limit check
      Step 7: 写 audit_logger(placeholder)

      注意:Step 5 (legacy hook position) 已删除(2026-07,commit 940a61d 标记
           "legacy position" 的清理)。PreToolUse 仅在 Step 1.5 触发一次。
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
        # 内容级 rule 匹配器(D.3b 抽离的 RuleChecker;engine 保留 delegate 方法向后兼容)
        self._rule_checker = RuleChecker(context)
        # D.3c:check_permissions 拆为 step handler 链。
        # step 顺序与原 inline 实现一致(对齐 doc §4.3);改顺序 = 改权限语义,慎改。
        # BashCheckStep 注入 self._run_bash_check_permissions bound method,
        # 保留 monkeypatch 能力(test_agent_core_bash_sandbox.py patch 该方法)。
        self._steps = self._build_steps()

    def _build_steps(self) -> list:
        """构造 step 链(每次调用重建,确保 bound method 指向当前 self)。

        放方法而非 __init__ 内联,是为了让测试可在子类覆盖 step 组合。
        """
        from .steps.bash_check import BashCheckStep
        from .steps.bypass_mode import BypassModeStep
        from .steps.default_ask import DefaultAskStep
        from .steps.denial_limit import DenialLimitStep
        from .steps.fast_path import FastPathStep
        from .steps.global_allow import GlobalAllowStep
        from .steps.global_ask import GlobalAskStep
        from .steps.global_deny import GlobalDenyStep
        from .steps.hook import HookStep
        from .steps.mode_postprocess import ModePostProcessStep
        from .steps.requires_user import RequiresUserStep
        from .steps.safety import SafetyStep
        from .steps.tool_check import ToolCheckStep

        return [
            GlobalDenyStep(),           # 1a
            GlobalAskStep(),            # 1b
            ToolCheckStep(),            # 1c
            # 1c': lambda 延迟绑定 — 调用时才查 self.__dict__,
            # 这样 patch.object(engine, "_run_bash_check_permissions") 能生效(对齐原 monolith 语义)
            BashCheckStep(lambda tool_input: self._run_bash_check_permissions(tool_input)),
            RequiresUserStep(),         # 1d
            SafetyStep(),               # 1e
            HookStep(),                 # 1.5
            BypassModeStep(),           # 2a
            GlobalAllowStep(),          # 2b
            FastPathStep(),             # 3
            ModePostProcessStep(),      # 4
            DenialLimitStep(),          # 6
            DefaultAskStep(),           # 7 (兜底,总返 ASK)
        ]

    # ── 主入口:check_permissions ──────────────────────────────

    def check_permissions(
        self,
        tool: Any,
        tool_input: dict,
        messages: Optional[list[dict]] = None,
    ) -> PermissionDecision:
        """同步决策权限(对齐 doc §4.3 + CC checkPermissions)。

        D.3c 重构:原 425 行 inline pipeline 拆为 13 个 step handler 链。
        行为与原 monolith 1:1 等价(24 个 engine 测试 + logging 测试为硬门槛)。

        step 顺序见 _build_steps();改顺序 = 改权限语义。
        每个 step 返 None → 继续;返 PermissionDecision → 经 _log_and_return 终止。
        DefaultAskStep(step 7)是兜底,总返 ASK,保证不会走到 raise。

        Args:
            tool: ToolDef 实例(duck-typed)
            tool_input: 工具输入参数 dict
            messages: 对话历史(classifier 用,可空)

        Returns:
            PermissionDecision(behavior / decision_reason / updated_input / message)
        """
        from .steps import PermissionContext

        tool_name = getattr(tool, "name", "unknown")

        permission_logger.debug(
            "🛡️ [engine_entry] tool=%s input=%s mode=%s",
            tool_name, tool_input, getattr(self.context, "mode", "?"),
        )

        pctx = PermissionContext(
            tool=tool,
            tool_input=tool_input,
            context=self.context,
            messages=messages,
            hook_registry=self.hook_registry,
            classifier=self.classifier,
            denial_state=self.denial_state,
            audit_logger=self.audit_logger,
            provider=self.provider,
            rule_checker=self._rule_checker,
            tool_name=tool_name,
        )

        for step in self._steps:
            result = step(pctx)
            if result is None:
                continue
            # step 返回 (decision, stage) tuple:stage 是细粒度决策点标识
            # (如 step_1c_bash_deny / step_4_classifier_allow),对齐原 monolith 的 audit 粒度
            decision, stage = result
            return self._log_and_return(tool, tool_input, decision, stage=stage)

        # 兜底:DefaultAskStep 总返 (ASK, stage),理论上不会到这里
        raise RuntimeError(
            f"check_permissions pipeline exhausted without decision for tool={tool_name}; "
            f"DefaultAskStep must be the last step"
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
            from .bash import bash_check_permissions
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

    # ── path 类工具集合(MCP 工具命名 mcp__<server>__<tool>,在此统一走 glob)──
    # Bash 走 matching_rules_for_input 的 shell 语义(prefix:/exact/wildcard)
    # D.3b:逻辑已抽到 rule_checker.py(PATH_TOOLS / is_path_tool);此处保留类属性
    # 引用 rule_checker.PATH_TOOLS,向后兼容外部读 engine._PATH_TOOLS 的代码。
    _PATH_TOOLS: frozenset[str] = frozenset({
        "Read", "Write", "Edit", "MultiEdit",
        "Glob", "Grep", "NotebookEdit",
    })

    def _is_path_tool(self, tool_name: str) -> bool:
        """判断是否走 glob 路径匹配(delegate 到 rule_checker)。"""
        return is_path_tool(tool_name)

    def _derive_input_str(self, tool_name: str, tool_input: dict) -> str:
        """把 tool_input 序列化为 matcher 期望的 input_str(delegate 到 rule_checker)。"""
        return derive_input_str(tool_name, tool_input)

    def _check_global_deny_rule(
        self, tool_name: str, tool_input: dict,
    ) -> Optional[PermissionRule]:
        """Step 1a: 内容级 deny rule 匹配(delegate 到 RuleChecker)。"""
        return self._rule_checker.check_deny(tool_name, tool_input)

    def _check_global_ask_rule(
        self, tool_name: str, tool_input: dict,
    ) -> Optional[PermissionRule]:
        """Step 1b: 内容级 ask rule 匹配(delegate 到 RuleChecker)。"""
        return self._rule_checker.check_ask(tool_name, tool_input)

    def _check_global_allow_rule(
        self, tool_name: str, tool_input: dict,
    ) -> Optional[PermissionRule]:
        """Step 2b: 内容级 allow rule 匹配(delegate 到 RuleChecker)。"""
        return self._rule_checker.check_allow(tool_name, tool_input)

    def _check_global_rule_with_input(
        self,
        tool_name: str,
        tool_input: dict,
        rules_dict: dict,
        behavior: PermissionBehavior,
    ) -> Optional[PermissionRule]:
        """统一的内容级 rule 查找入口(delegate 到 RuleChecker 内部方法)。"""
        return self._rule_checker._check_global_rule_with_input(
            tool_name, tool_input, rules_dict, behavior,
        )

    def _check_path_tool_rule(
        self,
        tool_name: str,
        tool_input: dict,
        rules_dict: dict,
        behavior: PermissionBehavior,
    ) -> Optional[PermissionRule]:
        """path 类工具 glob 匹配(delegate 到 RuleChecker 内部方法)。"""
        return self._rule_checker._check_path_tool_rule(
            tool_name, tool_input, rules_dict, behavior,
        )

    def _parse_rule_str(
        self,
        rule_str: str,
        source: PermissionRuleSource,
        behavior: PermissionBehavior,
    ) -> Optional[PermissionRule]:
        """从 rule 字符串解析 PermissionRule(delegate 到 rule_checker.parse_rule_str)。"""
        return parse_rule_str(rule_str, source, behavior)

    # ── audit_logger + 状态更新 ────────────────────────────────

    def _log_and_return(
        self,
        tool: Any,
        tool_input: dict,
        decision: PermissionDecision,
        stage: str,
    ) -> PermissionDecision:
        """
        写 audit log(如果 audit_logger 存在)+ 更新 deny state + 返回 decision

        这是唯一审计点(对齐 doc §4.8):engine 每条 decision 都经此,
        记录 stage + context + classifier + denial_state + tool_category。

        Args:
            tool: ToolDef 实例(duck-typed;取 .name + .category)
            tool_input: 工具输入(只存 hash)
            decision: PermissionDecision
            stage: 决策阶段
        """
        tool_name = getattr(tool, "name", "unknown")
        tool_category = getattr(tool, "category", None)

        # 🛡️ [decision] 同步打 INFO:每条决策都进主日志(与 audit.jsonl 互为补充)
        try:
            reason_type = "unknown"
            reason_text = ""
            if decision.decision_reason is not None:
                reason_type = getattr(decision.decision_reason, "type", "unknown") or "unknown"
                reason_text = getattr(decision.decision_reason, "reason", "") or ""
            permission_logger.info(
                "🛡️ [decision] stage=%s tool=%s category=%s behavior=%s reason_type=%s reason=%s",
                stage, tool_name, tool_category, decision.behavior, reason_type, reason_text[:120],
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
                    tool_category=tool_category,
                )
            except Exception as e:
                logger.warning("audit_logger.log 失败: %s", e)

        return decision

    # ── 状态查询 ──────────────────────────────────────────────

    def get_denial_state(self) -> DenialTrackingState:
        """获取当前 deny state(测试用)"""
        return self.denial_state
