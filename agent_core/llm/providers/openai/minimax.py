"""MiniMax (MiniMax M3 等)provider(2026-06-29 LLM Router 重构 Stage 3)。

thinking 提取策略(优先级):
1. `reasoning_content` 字段(GLM 风格,M3 也支持)→ 直接 yield ThinkingDelta
2. 文本里的 <think>...</think> 标签(M3 实测)→ 走 _ThinkTagSplitter 状态机

关键安全机制:_consumed_text 哨兵
- 走 splitter 时,splitter consume 了 delta.content → 必须在 _extract_thinking 内
  设 `self._consumed_text = True`,基类 _process_delta 检查后跳过 text_delta,
  避免 <think> 文本泄漏到 text 流(Stage 0 baseline 测试的 invariant)
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Generator, List

from ...config import LLMProvider
from ...registry import ProviderRegistry
from ...types import StreamChunk, TextDelta, ThinkingDelta
from .base import OpenAICompatibleProvider

if TYPE_CHECKING:
    pass

logger = logging.getLogger("llm.providers.openai.minimax")


@ProviderRegistry.register(LLMProvider.MINIMAX)
class MiniMaxProvider(OpenAICompatibleProvider):
    """MiniMax (MiniMax M3)—— thinking 在 reasoning_content 或 `` 标签里。"""

    provider_name = "minimax"
    default_base_url = "https://api.minimaxi.com/v1"
    env_key = "MINIMAX_API_KEY"

    def __init__(self, config) -> None:
        super().__init__(config)
        # M3 等模型把 thinking 包在 <think>...</think> 标签里(text_delta 内)
        self._think_splitter = self._ThinkTagSplitter()

    def _resolve_api_key(self) -> str:
        from ....config import config as _env_config  # agent_core/config.py
        return self.config.api_key or _env_config.minimax_api_key

    def _extract_thinking(self, delta) -> Generator[StreamChunk, None, None]:
        """MiniMax thinking 提取:reasoning_content 优先,否则 <think> 切标签。

        关键:走 splitter 时设 `self._consumed_text = True` → 基类跳过 text_delta。
        """
        # 优先:reasoning_content(M3/GLM 风格)
        if hasattr(delta, "reasoning_content") and delta.reasoning_content:
            yield StreamChunk(thinking_delta=ThinkingDelta(thinking=delta.reasoning_content))
            # reasoning_content 和 delta.content 是不同字段,让基类正常 yield text_delta
            return

        # 否则:走 splitter 切 <think> 标签
        if delta.content:
            # splitter 会 consume delta.content(切成 thinking+text 或 buffer 住),
            # 必须设哨兵防止基类重复 yield text_delta
            self._consumed_text = True
            yield from self._think_splitter.feed(delta.content)

    def _on_stream_end(self) -> Generator[StreamChunk, None, None]:
        """流结束:flush splitter 残留 buffer + 重置状态供下次 chat() 使用。"""
        # 1. 兜底:把残留 buffer 输出(不丢内容)
        if self._think_splitter._buf:
            yield from self._think_splitter.flush()
        # 2. 重置状态供下次 chat() 调用
        self._think_splitter = self._ThinkTagSplitter()
        self._consumed_text = False
        if False:
            yield  # type: ignore[unreachable]

    # ── Nested state machine (YAGNI: 没人用就内联) ─────────────────

    class _ThinkTagSplitter:
        """把含 `` 标签的 text 切成 (text_delta, thinking_delta) 序列。

        状态机:
            NORMAL  ──(见 <think>)──▶ THINKING
            THINKING ──(见 </think>)──▶ NORMAL

        关键能力:
        1. 标签跨 chunk 切片:`<thi` + `nk>...` + `</thin` + `king>`
        2. 多对标签:`<think>a</think> hello <think>b</think> world`
        3. 流末尾收尾:flush() 兜底未完成的缓冲区
        4. 失败降级:任何异常路径都保留原文(不丢内容)
        """

        OPEN_TAG = "<think>"
        CLOSE_TAG = "</think>"
        NORMAL = "normal"
        THINKING = "thinking"

        def __init__(self) -> None:
            self._state = self.NORMAL
            self._buf = ""  # 缓冲可能是不完整标签的后缀

        def feed(self, text: str) -> List[StreamChunk]:
            """喂入一段 text,产出 0+ 个 StreamChunk(可能为空,如果还在缓冲)。"""
            if not text:
                return []
            s = self._buf + text
            self._buf = ""
            out: List[StreamChunk] = []
            i = 0
            while i < len(s):
                tag = self.OPEN_TAG if self._state == self.NORMAL else self.CLOSE_TAG
                idx = s.find(tag, i)
                if idx == -1:
                    # 没找到完整标签 — 保留最后 len(tag)-1 字符在 _buf(可能是不完整标签)
                    tail = s[i:]
                    keep = len(tag) - 1
                    if len(tail) > keep:
                        out.append(self._emit(tail[:-keep]))
                        self._buf = tail[-keep:]
                    else:
                        self._buf = tail
                    break
                else:
                    # 标签前的内容
                    if idx > i:
                        out.append(self._emit(s[i:idx]))
                    i = idx + len(tag)
                    self._state = self.THINKING if self._state == self.NORMAL else self.NORMAL
                    # </think> 后跳过一个换行(MiniMax 实测紧跟 \n)
                    if (
                        self._state == self.NORMAL
                        and i < len(s)
                        and s[i] == "\n"
                    ):
                        i += 1
            return out

        def flush(self) -> List[StreamChunk]:
            """流结束时调用,把残留 buffer 兜底输出(不丢内容)"""
            if not self._buf:
                return []
            chunk = self._emit(self._buf)
            self._buf = ""
            return [chunk]

        def _emit(self, text: str) -> StreamChunk:
            if self._state == self.THINKING:
                return StreamChunk(thinking_delta=ThinkingDelta(thinking=text))
            return StreamChunk(text_delta=TextDelta(text=text))


__all__ = ["MiniMaxProvider"]
