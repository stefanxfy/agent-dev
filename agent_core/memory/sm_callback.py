"""L3 SM LLM callback — 把 sm_layer 跟具体 LLM provider 解耦。

callback = prompt → response 的纯函数映射。
sm_layer 不需要知道：
- 用哪个 LLM provider (minimax / zhipu / openai / anthropic)
- 是否流式 / 是否 cache
- 拼 messages 用什么 system prompt
- 怎么聚合 chunks

这些全是 callback 的事。

设计原则：
1. callback 是同步函数（sm_layer 的 extract_incremental 是同步的）
2. callback 接 str 返回 str（最简协议）
3. callback 内部调真实 LLM router，但同步等待结果
4. callback 可注入（测试时传 mock，生产时传这个真实实现）
5. router 实例由 caller 注入（sm_callback 不构造 router，避免循环依赖）

用法（生产）：
    from agent_core.llm.router import LLMRouter
    from agent_core.memory.sm_callback import make_sm_extract_callback

    router = LLMRouter(llm_config)  # 由 web 层构造
    cb = make_sm_extract_callback(router=router, cache_namespace="sm_extract")
    sm_layer = SessionMemoryLayer(..., llm_callback=cb)

用法（测试）：
    cb = lambda prompt: "[]"  # 直接返空操作
    # 或 mock router：
    cb = make_sm_extract_callback(router=MagicMock(chat=lambda **kw: iter([...])))
"""

from __future__ import annotations

import logging
from typing import Callable, Optional

from .sm_prompts import (
    SM_EDIT_SYSTEM_PROMPT,
    build_extract_prompt,
)

logger = logging.getLogger(__name__)


def make_sm_extract_callback(
    *,
    router: object,  # LLMRouter 实例（用 object 避免循环 import，运行时 duck-type）
    cache_namespace: str = "sm_extract",
    max_retries: int = 2,
    backoff_base: float = 0.5,
    on_failure: str = "raise",  # "raise" | "return_empty"
) -> Callable[[str], str]:
    """工厂函数：返回一个真实 LLM callback 实例。

    工厂模式而不是直接函数的好处：
    - 注入参数（router / cache_namespace / retries）不需要每次改签名
    - 测试时可以传不同的 router 或 mock
    - 未来要加 metrics / tracing / fallback，工厂函数内部加，不影响 sm_layer

    Args:
        router: LLMRouter 实例。sm_callback 不构造 router，由 caller 注入。
        cache_namespace: 路由层 cache key 隔离。不同业务用不同 ns，避免串。
        max_retries: LLM 调用失败重试次数（仅 callback 层面的重试；
            router 内部已有 stream / request 级重试，这里是外层 retry）。
        backoff_base: 重试退避基数（秒），实际 backoff = base * 2^attempt。
        on_failure: 失败时如何处理。
            - "raise": 抛 RuntimeError 给 sm_layer，让上层决定。
            - "return_empty": 返空字符串，sm_layer 会走 fallback（推进 last_id 但不更新 sections）。

    Returns:
        Callable[[str], str]: 一个 (prompt) -> response_text 的同步函数。
        sm_layer 直接调用它，不需要懂 LLM 的任何事。
    """
    def _callback(prompt: str) -> str:
        """同步 callback：内部用同步聚合的方式处理流式 chunks。

        sm_layer 在 worker 线程调 extract_incremental，所以这里即使是 sync
        也不会卡主线程。

        重试 / chunk 聚合 / 空响应检测 全部由 router.invoke() 收归
        (参 docs/llm-invoke-retry-design.md),callback 只负责拼 messages。
        """
        messages = [
            {"role": "system", "content": SM_EDIT_SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ]

        def _on_failure(err: Exception) -> str:
            """on_failure="return_empty" 时返空走 fallback,否则穿透抛 RuntimeError。"""
            if on_failure == "return_empty":
                logger.warning(
                    f"[L3 SM callback] 重试{max_retries}次仍失败,返空字符串(走 fallback)"
                )
                return ""
            raise RuntimeError(
                f"SM extract LLM callback 失败 "
                f"重试{max_retries}次仍无法获取响应: {err}"
            ) from err

        response = router.invoke(  # type: ignore[attr-defined]
            messages=messages,
            cache_namespace=cache_namespace,
            max_retries=max_retries,
            on_failure=_on_failure,
        )
        logger.debug(
            f"[L3 SM callback] LLM 响应成功 chars={len(response)}"
        )
        return response

    return _callback


# ──────────────────────────────────────────────────────────────────
# 便捷函数：直接拼 messages + 调 callback（供 SM 内部调用）
# ──────────────────────────────────────────────────────────────────


def call_sm_extract(
    callback: Callable[[str], str],
    *,
    sm_full_text: str,
    new_messages: list[dict],
    last_compacted_msg_id: Optional[str],
) -> str:
    """高层便捷函数：拼 user prompt + 调 callback。

    sm_layer 不应该关心 prompt 怎么拼，所以这一步也封装在 callback 模块里。
    sm_layer 拿到 callback 后直接调 call_sm_extract(cb, sm_text=..., msgs=..., last_id=...).

    Args:
        callback: 真实 LLM callback 同步函数 (prompt) -> response_text。
        sm_full_text: 当前 sm.md 完整内容（SM 文件可能还不存在）。
        new_messages: 自 last_compacted_msg_id 之后的新对话消息。
        last_compacted_msg_id: 上次 extract 推进到的 message id（边界标记）。

    Returns:
        str: LLM 的原始响应文本（未解析）。调用方负责解析（用 sm_prompts.parse_sm_response）。
    """
    prompt = build_extract_prompt(
        sm_full_text=sm_full_text,
        new_messages=new_messages,
        last_compacted_msg_id=last_compacted_msg_id,
    )
    return callback(prompt)