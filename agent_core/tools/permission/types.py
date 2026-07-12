"""
权限系统类型系统(Pydantic + Enum + dataclass)

对齐 Claude Code src/types/permissions.ts:
- PermissionRuleSource(8 source,policy > flag > cliArg > project > local > session > user > command)
- PermissionMode(7 mode:default / acceptEdits / bypass / dontAsk / plan / auto / bubble)
- PermissionBehavior(4:allow / deny / ask / passthrough)
- PermissionDecisionReason(11 variants,Pydantic discriminated union)
- ToolPermissionContext(同步所有 CC 字段,见 §4.1)

注意:
- PermissionRule / PermissionRuleValue 用 dataclass(对齐 doc §4.1,纯数据结构,无需校验)
- PermissionDecision / *Reason / ToolPermissionContext / AdditionalWorkingDirectory 用 Pydantic
  (需要字段校验 + model_copy + serialization)
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field


# ────────────────────────────────────────────────────────────────────
# PermissionRuleSource — 8 source(按优先级排序)
# ────────────────────────────────────────────────────────────────────

class PermissionRuleSource(str, Enum):
    """
    规则来源枚举(按优先级从低到高排序,索引越小优先级越低)

    对齐 CC PermissionRuleSource + doc §4.6 优先级链:
      command < session < localSettings < projectSettings
              < userSettings < cliArg < policySettings < flagSettings
    """
    COMMAND = "command"           # 最低,内存级,本会话有效
    SESSION = "session"           # 内存级
    LOCAL = "localSettings"       # .agent_data/settings.local.json
    PROJECT = "projectSettings"   # .agent_data/settings.json
    USER = "userSettings"         # ~/.agent_data/settings.json
    CLI_ARG = "cliArg"            # 启动参数 --permission
    POLICY = "policySettings"     # 企业 managed,最高(只读)
    FLAG = "flagSettings"         # flagSettings,企业最高(只读)

    @classmethod
    def ordered_sources(cls) -> list["PermissionRuleSource"]:
        """按优先级从低到高返回(用于匹配时按顺序聚合)"""
        return [
            cls.COMMAND,
            cls.SESSION,
            cls.LOCAL,
            cls.PROJECT,
            cls.USER,
            cls.CLI_ARG,
            cls.POLICY,
            cls.FLAG,
        ]


# ────────────────────────────────────────────────────────────────────
# PermissionMode — 7 mode
# ────────────────────────────────────────────────────────────────────

class PermissionMode(str, Enum):
    """
    权限模式枚举

    对齐 CC PermissionMode + doc §4.1:
    - default: 按规则判断,无匹配则 ask
    - acceptEdits: 文件编辑类工具自动放行(按 tool.check_permissions 决定)
    - bypass: 跳过所有 ask(hook 与 deny / safetyCheck 仍生效)
    - dontAsk: 所有 ask 转 deny(auto-deny)
    - plan: plan 模式,只读 + ExitPlanMode 才落 acceptEdits
    - auto: ANT-only,用 Haiku YOLO classifier 替代用户弹窗
    - bubble: 内部测试模式
    """
    DEFAULT = "default"
    ACCEPT_EDITS = "acceptEdits"
    BYPASS = "bypassPermissions"
    DONT_ASK = "dontAsk"
    PLAN = "plan"
    AUTO = "auto"        # ANT-only
    BUBBLE = "bubble"    # 内部测试


# ────────────────────────────────────────────────────────────────────
# PermissionBehavior — 4 行为
# ────────────────────────────────────────────────────────────────────

class PermissionBehavior(str, Enum):
    """
    决策行为枚举

    对齐 CC PermissionBehavior + doc §4.1:
    - allow: 直接执行
    - deny: 终止,发 tool_result error 给模型
    - ask: 弹窗,等待用户选择
    - passthrough: 当前检查未匹配,让上层继续
    """
    ALLOW = "allow"
    DENY = "deny"
    ASK = "ask"
    PASSTHROUGH = "passthrough"


# ────────────────────────────────────────────────────────────────────
# PermissionRule + PermissionRuleValue — 规则数据(dataclass)
# ────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class PermissionRuleValue:
    """
    规则内容值(对齐 CC PermissionRuleValue)

    Bash(rule_content) 中 rule_content 的几种形态:
    - 无 content (None): 整个 tool 命中
    - prefix:* (Bash(rm:*)): 命令以 rm 开头
    - exact (Bash(npm run build)): 完全等于
    - wildcard (Bash(*echo*)): 字符串包含
    - compound (Bash(cmd1; cmd2)): 复合(由 permission_matcher 拆解)
    - prompt:desc (Bash(prompt: running tests)): 由 bashClassifier 决定
    """
    tool_name: str
    rule_content: Optional[str] = None


@dataclass(frozen=True)
class PermissionRule:
    """
    单条权限规则(对齐 CC PermissionRule)

    例:settings.json 中的 "Bash(rm:*)" 解析为:
        PermissionRule(
            source=PermissionRuleSource.PROJECT,
            behavior=PermissionBehavior.DENY,
            value=PermissionRuleValue(tool_name="Bash", rule_content="rm:*"),
        )
    """
    source: PermissionRuleSource
    behavior: PermissionBehavior
    value: PermissionRuleValue

    def __post_init__(self):
        """基本字段校验"""
        if not self.value.tool_name:
            raise ValueError("PermissionRule.value.tool_name 必须非空")

    @property
    def tool_name(self) -> str:
        return self.value.tool_name

    @property
    def rule_content(self) -> Optional[str]:
        return self.value.rule_content

    def __str__(self) -> str:
        """字符串化,与 CC formatPermissionRule 对齐"""
        if self.rule_content is None:
            return f"{self.tool_name}"
        return f"{self.tool_name}({self.rule_content})"


# ────────────────────────────────────────────────────────────────────
# PermissionDecisionReason — 11 变体(Pydantic discriminated union)
# ────────────────────────────────────────────────────────────────────

class _ReasonBase(BaseModel):
    """所有 reason 的基类,提供统一的 'type' 字段"""
    model_config = ConfigDict(extra="forbid")


class RuleReason(_ReasonBase):
    """命中规则(对齐 CC { type: 'rule', rule: PermissionRule })"""
    type: str = Field(default="rule", frozen=True)
    rule: "PermissionRuleData"
    reason: Optional[str] = None


class ModeReason(_ReasonBase):
    """mode 直接决定(对齐 CC { type: 'mode', mode: PermissionMode })"""
    type: str = Field(default="mode", frozen=True)
    mode: str  # PermissionMode value
    reason: Optional[str] = None


class SubcommandResultsReason(_ReasonBase):
    """Bash subcommand 级决策汇总(对齐 CC subcommandResults)"""
    type: str = Field(default="subcommandResults", frozen=True)
    # 简化:M3 再展开为 sub-reason map
    allow_count: int = 0
    ask_count: int = 0
    deny_count: int = 0
    reason: Optional[str] = None


class PermissionPromptReason(_ReasonBase):
    """Agent SDK host 自管(对齐 CC permissionPromptTool)"""
    type: str = Field(default="permissionPromptTool", frozen=True)
    reason: Optional[str] = None


class HookReason(_ReasonBase):
    """PreToolUse hook 决策(对齐 CC { type: 'hook', hookName, hookSource?, reason? })"""
    type: str = Field(default="hook", frozen=True)
    hook_name: str
    hook_source: Optional[str] = None
    reason: Optional[str] = None


class AsyncAgentReason(_ReasonBase):
    """后台 agent,无用户,auto-deny(对齐 CC asyncAgent)"""
    type: str = Field(default="asyncAgent", frozen=True)
    reason: Optional[str] = None


class SandboxOverrideReason(_ReasonBase):
    """sandbox 覆盖(对齐 CC { type: 'sandboxOverride', reason })"""
    type: str = Field(default="sandboxOverride", frozen=True)
    reason: str  # 'excludedCommand' | 'dangerouslyDisableSandbox'


class ClassifierReason(_ReasonBase):
    """Haiku classifier 决策(对齐 CC { type: 'classifier', classifier, reason })"""
    type: str = Field(default="classifier", frozen=True)
    classifier: str  # 'bash_deny' | 'bash_ask' | 'bash_allow' | 'yolo'
    reason: Optional[str] = None


class WorkingDirReason(_ReasonBase):
    """路径约束失败(对齐 CC { type: 'workingDir', reason })"""
    type: str = Field(default="workingDir", frozen=True)
    reason: str


class SafetyCheckReason(_ReasonBase):
    """敏感路径 / secret 检测(对齐 CC { type: 'safetyCheck', reason, classifierApprovable })"""
    type: str = Field(default="safetyCheck", frozen=True)
    reason: str
    classifier_approvable: bool = False


class OtherReason(_ReasonBase):
    """其他 / 兜底(对齐 CC { type: 'other', reason })"""
    type: str = Field(default="other", frozen=True)
    reason: str


# PermissionRule 的 Pydantic 镜像(Pydantic discriminated union 必须是 BaseModel)
class PermissionRuleData(BaseModel):
    """PermissionRule 的 Pydantic 镜像(用于嵌入 PermissionDecisionReason 等 BaseModel 场景)"""
    model_config = ConfigDict(arbitrary_types_allowed=True)

    source: str          # PermissionRuleSource value
    behavior: str        # PermissionBehavior value
    tool_name: str
    rule_content: Optional[str] = None

    @classmethod
    def from_dataclass(cls, rule: PermissionRule) -> "PermissionRuleData":
        return cls(
            source=rule.source.value,
            behavior=rule.behavior.value,
            tool_name=rule.tool_name,
            rule_content=rule.rule_content,
        )


# 更新前向引用
RuleReason.model_rebuild()


# PermissionDecisionReason 类型别名(Union of all variants)
PermissionDecisionReason = (
    RuleReason
    | ModeReason
    | SubcommandResultsReason
    | PermissionPromptReason
    | HookReason
    | AsyncAgentReason
    | SandboxOverrideReason
    | ClassifierReason
    | WorkingDirReason
    | SafetyCheckReason
    | OtherReason
)


# ────────────────────────────────────────────────────────────────────
# PermissionDecision — 最终决策
# ────────────────────────────────────────────────────────────────────

class PermissionDecision(BaseModel):
    """
    单次 tool_use 的最终权限决策(对齐 CC PermissionDecision)

    字段:
    - behavior: 决策行为
    - decision_reason: 决策原因(11 种变体之一)
    - updated_input: hook 链 / tool.check_permissions 改写后的 input(M3 可选)
    - message: 给用户的提示语(弹窗显示用)
    - allow_once_bypass: 一次性 bypass(用户选 "yes, allow once")
    """
    model_config = ConfigDict(arbitrary_types_allowed=True)

    behavior: str                       # PermissionBehavior value
    decision_reason: Optional[PermissionDecisionReason] = None
    updated_input: Optional[dict] = None
    message: Optional[str] = None
    allow_once_bypass: bool = False


# ────────────────────────────────────────────────────────────────────
# AdditionalWorkingDirectory — 额外工作目录
# ────────────────────────────────────────────────────────────────────

class AdditionalWorkingDirectory(BaseModel):
    """
    额外工作目录(对齐 CC AdditionalWorkingDirectory)

    来源于 `addDirectories` permission_update,常见场景:
    - 用户在 UI 点 "Add directory to access"
    - 项目 settings.json 声明 `additionalDirectories`
    """
    path: str
    source: str = "cliArg"  # PermissionRuleSource value,默认 cliArg
    added_at: Optional[float] = None  # time.time()
    reason: Optional[str] = None


# ────────────────────────────────────────────────────────────────────
# ToolPermissionRulesBySource — 按 source 索引的规则字典
# ────────────────────────────────────────────────────────────────────

ToolPermissionRulesBySource = dict[str, list[str]]
"""
8 source × list[str] 字典形态(对齐 CC ToolPermissionRulesBySource)

未解析形态(rule 字符串),Tool 自己在 check_permissions 时用
permission_matcher.get_rule_by_contents_for_tool_name 解析。

例:
{
    "command": [],
    "session": ["Bash(npm test:*)"],
    "localSettings": [],
    "projectSettings": ["Read(./docs/**)", "Bash(rm:*)"],
    "userSettings": [],
    "cliArg": [],
    "policySettings": ["Bash(rm:*)"],
    "flagSettings": [],
}
"""


# ────────────────────────────────────────────────────────────────────
# ToolPermissionContext — 决策上下文(对齐 CC ToolPermissionContext)
# ────────────────────────────────────────────────────────────────────

class ToolPermissionContext(BaseModel):
    """
    工具权限决策上下文(对齐 CC ToolPermissionContext)

    这是 Permission 系统的**唯一入口数据**,所有 tool.check_permissions
    都拿这个对象。Tool 自己负责解析 always_allow_rules 等字段。

    字段全 CC 对齐(doc §4.1 已列出):
    - mode: 当前权限模式
    - additional_working_directories: 额外工作目录 dict
    - always_allow_rules / always_deny_rules / always_ask_rules: 按 source 索引的字符串
    - is_bypass_permissions_mode_available: plan 模式是否记起 bypass
    - stripped_dangerous_rules: 被 stripDangerousRules 剥离的规则(语义保留但不再匹配)
    - should_avoid_permission_prompts: 后台 agent,禁止弹窗
    - await_automated_checks_before_dialog: 等 hook 先跑完才显示弹窗
    - pre_plan_mode: plan 之前的 mode
    - sandbox_enabled: 是否启用 sandbox(M2 才用)
    - no_settings_match: 是否有 settings 命中
    - is_anthropic_provider: 是否 anthropic provider(决定 auto mode 可用)
    """
    model_config = ConfigDict(arbitrary_types_allowed=True)

    mode: str = PermissionMode.DEFAULT.value

    # 额外工作目录(Pydantic dict[str, BaseModel] 形态)
    additional_working_directories: dict[str, AdditionalWorkingDirectory] = Field(default_factory=dict)

    # 按 source 索引的规则字符串(未解析形态)
    always_allow_rules: ToolPermissionRulesBySource = Field(default_factory=dict)
    always_deny_rules: ToolPermissionRulesBySource = Field(default_factory=dict)
    always_ask_rules: ToolPermissionRulesBySource = Field(default_factory=dict)

    # 可选字段
    is_bypass_permissions_mode_available: bool = False
    stripped_dangerous_rules: Optional[ToolPermissionRulesBySource] = None
    should_avoid_permission_prompts: bool = False
    await_automated_checks_before_dialog: bool = False
    pre_plan_mode: Optional[str] = None

    # 内部扩展字段(doc §4.1)
    sandbox_enabled: bool = False
    no_settings_match: bool = True  # 默认 True(无 settings 命中 → classifier 启用条件之一)
    is_anthropic_provider: bool = True  # 默认 True(classifier 默认 ANT-only)

    def get_all_allow_rules(self) -> list[str]:
        """所有 source 的 allow rules 扁平化(按优先级排序)"""
        result = []
        for source in PermissionRuleSource.ordered_sources():
            result.extend(self.always_allow_rules.get(source.value, []))
        return result

    def get_all_deny_rules(self) -> list[str]:
        """所有 source 的 deny rules 扁平化(按优先级排序)"""
        result = []
        for source in PermissionRuleSource.ordered_sources():
            result.extend(self.always_deny_rules.get(source.value, []))
        return result

    def get_all_ask_rules(self) -> list[str]:
        """所有 source 的 ask rules 扁平化(按优先级排序)"""
        result = []
        for source in PermissionRuleSource.ordered_sources():
            result.extend(self.always_ask_rules.get(source.value, []))
        return result