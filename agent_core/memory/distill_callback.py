"""L5 Distiller LLM callback — 把 Distiller 跟具体 LLM provider 解耦。

callback = prompt → response 的纯函数映射。
Distiller 不需要知道:
- 用哪个 LLM provider
- 是否流式 / 是否 cache
- 拼 messages 用什么 system prompt
- 怎么聚合 chunks

设计原则与 sm_callback 完全一致:
1. callback 是同步函数
2. callback 接 str 返回 str
3. callback 内部调真实 LLM router,同步等待结果
4. callback 可注入(测试时 mock,生产时传这个真实实现)
5. router 实例由 caller 注入,distill_callback 不构造 router

用法(生产):
    from agent_core.llm.router import LLMRouter
    from agent_core.memory.distill_callback import make_distill_callback

    router = LLMRouter(llm_config)
    cb = make_distill_callback(router=router, cache_namespace="distill")
    DistillationScheduler(memory_root, llm_callback=cb)

用法(测试):
    cb = lambda prompt: "[]"
"""

from __future__ import annotations

import logging
import time
from typing import Callable, Optional

logger = logging.getLogger(__name__)


DISTILL_SYSTEM_PROMPT = """你是 L5 autoDream 蒸馏器。

输入: <existing_memories>...</existing_memories> 当前 .md 记忆库全量内容

任务:
1. 整理 / 去重 / 修正现有记忆(合并相似项 / 调和冲突 / 删除过时 / 修正错误)
2. 输出 JSON 数组,每个候选一个 dict

不重复提取现有已有内容;只产出"实质变化"的候选。

输出格式(严格 JSON 数组):
[
  {
    "type": "user" | "feedback" | "project" | "reference",
    "title": "<标题>",
    "why": "<重要性理由>",
    "body": "<记忆正文>",
    "confidence": 0.8,
    "sources": ["原文件名 1, 原文件名 2, ..."],
    "tags": ["偏好"]
  }
]

如果现有记忆已经很干净不需要整理,返回空数组 []。
"""


def make_distill_callback(
    *,
    router: object,  # LLMRouter 实例(用 object 避免循环 import)
    cache_namespace: str = "distill",
    max_retries: int = 2,
    backoff_base: float = 0.5,
    on_failure: str = "raise",  # "raise" | "return_empty"
) -> Callable[[str], str]:
    """工厂函数:返回一个真实 LLM callback 实例。

    Args:
        router: LLMRouter 实例。distill_callback 不构造 router,由 caller 注入。
        cache_namespace: 路由层 cache key 隔离。
        max_retries: LLM 调用失败重试次数。
        backoff_base: 重试退避基数(秒),实际 backoff = base * 2^attempt。
        on_failure: 失败时如何处理。
            - "raise": 抛 RuntimeError 给上层。
            - "return_empty": 返空字符串,distiller 会走 fallback。

    Returns:
        Callable[[str], str]: 一个 (prompt) -> response_text 的同步函数。
    """
    def _callback(prompt: str) -> str:
        messages = [
            {"role": "system", "content": DISTILL_SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ]

        last_err: Optional[Exception] = None
        for attempt in range(1, max_retries + 1):
            try:
                chunks: list[str] = []
                for chunk in router.chat(  # type: ignore[attr-defined]
                    messages=messages,
                    cache_namespace=cache_namespace,
                ):
                    text_delta = getattr(chunk, "text_delta", None)
                    if text_delta is not None:
                        text = getattr(text_delta, "text", None)
                        if text:
                            chunks.append(text)
                response = "".join(chunks)
                if not response.strip():
                    raise RuntimeError("LLM 返回空响应")
                logger.debug(
                    f"[L5 distill callback] LLM 响应成功 "
                    f"attempt={attempt} chars={len(response)}"
                )
                return response
            except Exception as e:
                last_err = e
                logger.warning(
                    f"[L5 distill callback] LLM 调用失败 "
                    f"attempt={attempt}/{max_retries} err={type(e).__name__}: {e}"
                )
                if attempt < max_retries:
                    backoff = backoff_base * (2 ** (attempt - 1))
                    time.sleep(backoff)
                    continue

        if on_failure == "return_empty":
            logger.warning(
                f"[L5 distill callback] 重试{max_retries}次仍失败,返空字符串(走 fallback)"
            )
            return ""
        raise RuntimeError(
            f"Distill LLM callback 失败 "
            f"重试{max_retries}次仍无法获取响应: {last_err}"
        ) from last_err

    return _callback


__all__ = ["make_distill_callback", "DISTILL_SYSTEM_PROMPT"]