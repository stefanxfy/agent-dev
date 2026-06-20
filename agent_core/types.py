"""
agent_core 公共类型定义

P2-9 修复：统一 role 字符串管理
- 之前：散落的 `m.get("role") == "user"` / `m["role"] == "assistant"` 等比较
- 现在：通过 MessageRole 枚举统一管理，避免拼写错误和"user"/"User"/"USER"漂移
"""

from __future__ import annotations

from enum import Enum


class MessageRole(str, Enum):
    """
    消息角色枚举

    继承 str 以保持与现有 dict 接口的兼容性：
    `MessageRole.USER == "user"` 返回 True
    `{"role": MessageRole.USER}` 可直接序列化

    用法：
        role = MessageRole(m["role"])  # 安全转换（无效值抛 ValueError）
        if role == MessageRole.USER:    # 比字符串比较更安全
            ...
    """

    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"  # tool 消息（OpenAI 风格）
    TOOL_RESULT = "tool_result"  # 工具结果（Anthropic 风格，嵌在 user 中）

    @classmethod
    def is_main_chain(cls, role: "str | MessageRole") -> bool:
        """
        是否属于主对话链（user / assistant / system）

        P0 修复配套：排除元数据 entry（custom-title / ai-title / tag / agent-name / mode /
        fork-info 等）的 parentUuid 链接问题（7f071c62 现场）
        """
        if isinstance(role, str) and not isinstance(role, MessageRole):
            role = cls(role) if role in cls._value2member_map_ else None
            if role is None:
                return False
        return role in (cls.SYSTEM, cls.USER, cls.ASSISTANT)

    @classmethod
    def normalize(cls, role: "str | MessageRole | None") -> "str | None":
        """
        归一化为小写字符串（不抛异常）

        用于：dict.get("role") 后做不区分大小写的比较
        返回：原始字符串的小写形式（无效值原样返回）
        """
        if role is None:
            return None
        if isinstance(role, MessageRole):
            return role.value
        return str(role).lower().strip()

    @classmethod
    def safe_cast(cls, role: "str | MessageRole | None", default: "MessageRole | None" = None) -> "MessageRole | None":
        """
        安全转换：无效值返回 default（默认 None）

        用于：解析外部输入（API 返回、文件读取）时，避免 ValueError
        """
        if role is None:
            return default
        if isinstance(role, MessageRole):
            return role
        try:
            return cls(str(role).lower().strip())
        except ValueError:
            return default
