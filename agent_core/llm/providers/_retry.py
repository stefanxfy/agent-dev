"""Provider 内部重试工具 — `_` 前缀表示这是 providers 包内部细节,Router 不 import。

历史:
- Stage 1:从 router.py:174-552 抽出(常量 + 错误分类 + _stream_with_retry)
- Stage 2:加 RetryPolicy dataclass(纯数据,BaseProvider 默认值)
"""
from __future__ import annotations

import logging
import time as _time
from dataclasses import dataclass, field

logger = logging.getLogger("llm.providers.retry")


# ── HTTP 错误分类(常量) ──────────────────────────────────────────
# P1-8 / P1-9:不同 provider 的错误语义不一样,必须区分:
# - 400 (Bad Request): 客户端错误,不重试
#   特殊情况:MiniMax 等小厂商可能因格式问题返回 400 但实际可重试
#   (参数顺序、空字符串等)→ 视为"可重试 1 次"兜底
# - 401/403: 鉴权错误,不重试
# - 404: 模型不存在,不重试
# - 429 (Rate Limit): 限流,重试(尊重 Retry-After)
# - 500/502/503/504: 服务端错误,重试
# - 408 (Request Timeout): 重试

RETRYABLE_STATUS_CODES = frozenset({408, 429, 500, 502, 503, 504})
# 某些小厂商(如 MiniMax)400 也可能因临时格式问题触发,单独标记
SOFT_RETRYABLE_STATUS_CODES = frozenset({400})  # 仅 1 次重试
NON_RETRYABLE_STATUS_CODES = frozenset({401, 403, 404, 422})

MAX_STREAM_RETRY = 2
MAX_REQUEST_RETRY = 3
RETRY_BACKOFF_BASE = 0.5  # 秒

# E-1 修复:env 变量集中管理后的默认值引用
# 实际值可通过 LLM_MAX_STREAM_RETRY / LLM_MAX_REQUEST_RETRY / LLM_RETRY_BACKOFF_BASE 覆盖
# (运行时由 config.llm_max_stream_retry 等访问)


def _classify_http_error(status: int) -> str:
    """分类 HTTP 错误 → 'retry' / 'soft_retry' / 'fail'

    Returns:
        'retry': 正常重试
        'soft_retry': 仅 1 次重试(小厂商的 400 兜底)
        'fail': 不重试,直接抛给上层
    """
    if status in RETRYABLE_STATUS_CODES:
        return "retry"
    if status in SOFT_RETRYABLE_STATUS_CODES:
        return "soft_retry"
    return "fail"


def _is_stream_interruption_error(exc: Exception) -> bool:
    """判断是否是 stream 中断类错误(P1-9 触发条件)

    触发场景:
    - http.client.IncompleteRead: GLM-5.1 实测偶发
    - requests.exceptions.ChunkedEncodingError
    - urllib3.ProtocolError
    - ConnectionError / ConnectionResetError
    """
    type_name = type(exc).__name__
    if type_name in (
        "IncompleteRead",
        "ChunkedEncodingError",
        "ProtocolError",
        "RemoteDisconnected",
        "ConnectionResetError",
        "ConnectionAbortedError",
        "BrokenPipeError",
    ):
        return True
    # 字符串兜底(防止 provider 包装了异常)
    msg = str(exc).lower()
    if any(
        s in msg
        for s in (
            "incomplete read",
            "connection broken",
            "connection reset",
            "server disconnected",
            "stream is closed",
        )
    ):
        return True
    return False


@dataclass(frozen=True)
class RetryPolicy:
    """Provider 重试策略(纯数据,Stage 2d)。

    设计:
    - `frozen=True` → 不可变,线程安全,可 hash
    - 不直接读 env(env 读取由 BaseProvider.__init__ 负责)→ 单元测试时无需 patch env
    - `default()` 给 BaseProvider 一个非 env 耦合的基线
    - 自定义: `RetryPolicy(max_request_retry=5, backoff_base=1.0)`

    字段语义见原 router.py:251-331 的 `_stream_with_retry`。
    """
    max_request_retry: int = 3
    max_stream_retry: int = 2
    backoff_base: float = 0.5
    retryable_status_codes: frozenset = field(
        default_factory=lambda: frozenset({408, 429, 500, 502, 503, 504}),
    )
    soft_retryable_status_codes: frozenset = field(default_factory=lambda: frozenset({400}))
    non_retryable_status_codes: frozenset = field(
        default_factory=lambda: frozenset({401, 403, 404, 422}),
    )

    @classmethod
    def default(cls) -> "RetryPolicy":
        """工厂:返回默认 RetryPolicy(等价于 RetryPolicy())。"""
        return cls()


def _stream_with_retry(
    stream_fn,
    provider: str,
    *,
    max_stream_retry: int = MAX_STREAM_RETRY,
    max_request_retry: int = MAX_REQUEST_RETRY,
    backoff_base: float = RETRY_BACKOFF_BASE,
    retryable_status_codes: frozenset = RETRYABLE_STATUS_CODES,
    soft_retryable_status_codes: frozenset = SOFT_RETRYABLE_STATUS_CODES,
    non_retryable_status_codes: frozenset = NON_RETRYABLE_STATUS_CODES,
):
    """P1-8 / P1-9 修复:流式调用统一重试包装。

    处理两类错误:
    1. HTTP 错误(_chat_* 抛 openai.BadRequestError 等):
       - 4xx (除 408/429) → 不重试,直接抛
       - 5xx / 408 / 429 → 重试,指数退避
       - 小厂商 400 兜底:仅 1 次重试(P1-8 MiniMax)
    2. Stream 中断(生成器内部抛 IncompleteRead 等):
       - P1-9 修复:GLM-5.1 偶发 stream 截断 → 重试 + 上限 2 次

    Args:
        stream_fn: 返回 StreamChunk 生成器的函数
        provider: 用于日志
        max_stream_retry / max_request_retry / backoff_base:
            允许外部覆盖(默认读模块常量)。
        *_status_codes: Stage 2 允许 RetryPolicy 覆盖(默认仍是模块常量)。
    """
    # 缓存已 yield 的 chunk,以便重试时不会重复输出
    # 注意:重试只能"从头开始",因此丢弃所有已 yield 的内容
    # 这是 streaming + retry 的固有限制:上游需要容忍少量重复内容
    def _classify(status: int) -> str:
        if status in retryable_status_codes:
            return "retry"
        if status in soft_retryable_status_codes:
            return "soft_retry"
        if status in non_retryable_status_codes:
            return "fail"
        return "fail"

    for req_attempt in range(max_request_retry + 1):
        try:
            for stream_attempt in range(max_stream_retry + 1):
                try:
                    for chunk in stream_fn():
                        yield chunk
                    return  # 成功
                except Exception as e:
                    if _is_stream_interruption_error(e) and stream_attempt < max_stream_retry:
                        backoff = backoff_base * (2 ** stream_attempt)
                        logger.warning(
                            f"🔄 [{provider}] stream interrupted "
                            f"(attempt {stream_attempt + 1}/{max_stream_retry}): {e}, "
                            f"retrying in {backoff:.1f}s"
                        )
                        _time.sleep(backoff)
                        continue
                    # 非中断错误或重试耗尽 → 抛给外层请求级重试
                    raise
        except Exception as e:
            # 尝试分类 HTTP 错误
            status = getattr(e, "status_code", None) or getattr(e, "code", None)
            if status is None:
                # 尝试从 openai SDK 的错误对象取
                status = getattr(getattr(e, "response", None), "status_code", None)

            if status is not None:
                classification = _classify(int(status))
                if classification == "fail":
                    logger.error(
                        f"❌ [{provider}] HTTP {status} (no retry): {e}"
                    )
                    raise
                if classification == "soft_retry" and req_attempt >= 1:
                    logger.error(
                        f"❌ [{provider}] HTTP {status} soft-retry exhausted: {e}"
                    )
                    raise
                if req_attempt < max_request_retry:
                    backoff = backoff_base * (2 ** req_attempt)
                    logger.warning(
                        f"🔄 [{provider}] HTTP {status} (attempt {req_attempt + 1}/"
                        f"{max_request_retry}): {e}, retrying in {backoff:.1f}s"
                    )
                    _time.sleep(backoff)
                    continue
            # 非 HTTP 错误 或 重试耗尽 → 抛给上层
            raise


__all__ = [
    "RETRYABLE_STATUS_CODES", "SOFT_RETRYABLE_STATUS_CODES", "NON_RETRYABLE_STATUS_CODES",
    "MAX_STREAM_RETRY", "MAX_REQUEST_RETRY", "RETRY_BACKOFF_BASE",
    "_classify_http_error", "_is_stream_interruption_error", "_stream_with_retry",
    "RetryPolicy",
]
