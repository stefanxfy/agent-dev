"""
agent_core 统一异常体系

P2-10 修复：所有领域错误统一一个根异常，便于上层捕获和分类。
根类：AgentError
子类按领域划分：
  - SessionError: 会话管理（JSONL 损坏、resume 失败、metadata 不一致）
  - ContextError: 上下文管理（token 超限、压缩失败、circuit breaker 触发）
  - ProviderError: LLM provider（HTTP 错误、stream 中断、auth 失败）
  - StorageError: 存储层（文件 IO、临时文件 rename 失败、磁盘满）
  - ToolError: 工具调用（参数错误、执行失败、权限拒绝）
  - ConfigError: 配置（env 缺失、env 解析失败、模型配置缺失）

设计原则：
1. 统一根类 `AgentError`，便于 `except AgentError as e:` 兜底
2. 每个领域异常带 `code` 属性（字符串），便于日志/UI 展示
3. 兼容旧代码：很多地方用 `raise ValueError(...)`，保留但不强制替换
4. 不使用 `assert` 抛出，遵循 EAFP 原则
"""

from __future__ import annotations

from typing import Optional


class AgentError(Exception):
    """
    agent-dev 领域异常根类

    Attributes:
        code: 机器可读错误码（用于日志聚合、UI 提示）
        message: 人类可读消息
        cause: 底层异常（如果有）
    """

    code: str = "AGENT_ERROR"

    def __init__(
        self,
        message: str = "",
        code: Optional[str] = None,
        cause: Optional[Exception] = None,
    ):
        super().__init__(message)
        self.message = message or self.__class__.__name__
        if code:
            self.code = code
        self.cause = cause

    def __str__(self) -> str:
        if self.cause:
            return f"[{self.code}] {self.message} (caused by: {type(self.cause).__name__}: {self.cause})"
        return f"[{self.code}] {self.message}"


# ── Session 异常族 ──────────────────────────────────────────────

class SessionError(AgentError):
    """会话管理领域异常"""
    code = "SESSION_ERROR"


class SessionNotFoundError(SessionError):
    code = "SESSION_NOT_FOUND"


class SessionCorruptedError(SessionError):
    """JSONL 文件损坏、parentUuid 链断裂等"""
    code = "SESSION_CORRUPTED"


class MetadataConflictError(SessionError):
    """元数据冲突（如：custom-title 与 ai-title 同时存在且不一致）"""
    code = "METADATA_CONFLICT"


# ── Context 异常族 ──────────────────────────────────────────────

class ContextError(AgentError):
    """上下文管理领域异常"""
    code = "CONTEXT_ERROR"


class TokenOverflowError(ContextError):
    """token 超过模型上限，且 PTL 防御已耗尽"""
    code = "TOKEN_OVERFLOW"


class CompactFailedError(ContextError):
    """压缩失败（LLM 返回空 / API 错误 / 质量不达标）"""
    code = "COMPACT_FAILED"


class CircuitOpenError(ContextError):
    """熔断器打开：连续失败次数超过阈值"""
    code = "CIRCUIT_OPEN"


class PreservedHeadEmptyError(ContextError):
    """P1-2 修复相关：preserved head 为空（不抛，仅用于诊断）"""
    code = "PRESERVED_HEAD_EMPTY"


# ── Provider 异常族 ──────────────────────────────────────────────

class ProviderError(AgentError):
    """LLM provider 领域异常"""
    code = "PROVIDER_ERROR"


class ProviderAuthError(ProviderError):
    """401/403 鉴权失败（不重试）"""
    code = "PROVIDER_AUTH"


class ProviderRateLimitError(ProviderError):
    """429 限流（可重试）"""
    code = "PROVIDER_RATE_LIMIT"


class ProviderBadRequestError(ProviderError):
    """400 客户端错误（MiniMax 等小厂商可能软重试 1 次）"""
    code = "PROVIDER_BAD_REQUEST"


class ProviderServerError(ProviderError):
    """5xx 服务端错误（可重试）"""
    code = "PROVIDER_SERVER"


class StreamInterruptedError(ProviderError):
    """P1-9 修复：stream 中断（IncompleteRead / 连接断开）"""
    code = "STREAM_INTERRUPTED"


class UnsupportedProviderError(ProviderError):
    """不支持的 provider"""
    code = "UNSUPPORTED_PROVIDER"


# ── Storage 异常族 ──────────────────────────────────────────────

class StorageError(AgentError):
    """存储层异常"""
    code = "STORAGE_ERROR"


class StorageWriteError(StorageError):
    """写盘失败（IO 错误、磁盘满、临时文件 rename 失败）"""
    code = "STORAGE_WRITE"


class StorageReadError(StorageError):
    """读盘失败（文件不存在、权限拒绝、JSON 解析失败）"""
    code = "STORAGE_READ"


# ── Tool 异常族 ──────────────────────────────────────────────

class ToolError(AgentError):
    """工具调用领域异常"""
    code = "TOOL_ERROR"


class ToolArgumentError(ToolError):
    """工具参数错误（参数缺失、类型不匹配）"""
    code = "TOOL_ARGUMENT"


class ToolExecutionError(ToolError):
    """工具执行失败（运行时错误）"""
    code = "TOOL_EXECUTION"


class ToolPermissionError(ToolError):
    """权限拒绝"""
    code = "TOOL_PERMISSION"


# ── Config 异常族 ──────────────────────────────────────────────

class ConfigError(AgentError):
    """配置异常"""
    code = "CONFIG_ERROR"


class ConfigMissingError(ConfigError):
    """必填 env 变量缺失"""
    code = "CONFIG_MISSING"


class ConfigParseError(ConfigError):
    """env 变量解析失败（如：数字字段非数字）"""
    code = "CONFIG_PARSE"


# ── 辅助函数 ──────────────────────────────────────────────

def wrap_exception(
    exc: Exception,
    target_cls: type[AgentError],
    message: Optional[str] = None,
    code: Optional[str] = None,
) -> AgentError:
    """
    把任意异常包装为指定领域异常

    用法：
        try:
            ...
        except ValueError as e:
            raise wrap_exception(e, CompactFailedError, "压缩失败")
    """
    return target_cls(message=message or str(exc), code=code, cause=exc)
