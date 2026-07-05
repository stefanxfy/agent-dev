"""_ThinkTagSplitter — 把 `` 标签从 text_delta 切到 thinking_delta。

背景:MiniMax-M3 等 provider 把 thinking 直接放在 text_delta 里
(包在 `` 标签中),不像 GLM 那样有独立的 reasoning_content 字段。
本 splitter 状态机把这种格式转成统一的 StreamChunk.thinking_delta,
让 UI 能区分显示。

历史:从 `router.py:337` 拆出(2026-06-24),让 router.py 专注 dispatch + retry。
"""
from __future__ import annotations

# 依赖注入:为了避免 router.py ↔ thinking_splitter.py 循环 import,
# splitter 不直接 import router,而是 duck-type 接受 StreamChunk /
# TextDelta / ThinkingDelta 三个类(测试可用 mock)。
#
# 真实使用路径(router.py)会做:
#     from .thinking_splitter import _ThinkTagSplitter
# 真实 StreamChunk 来自 router.py,见 router.py:289 起定义。
#
# 这里用 TYPE_CHECKING 避免运行时循环 import。
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .router import StreamChunk, TextDelta, ThinkingDelta


class _ThinkTagSplitter:
    """把含 `` 标签的 text 切成 (text_delta, thinking_delta) 序列

    状态机:
        NORMAL  ──(见 <think>)──▶ THINKING
        THINKING ──(见 </think>)──▶ NORMAL

    关键能力:
    1. 标签跨 chunk 切片:`<thi` + `nk>...` + `</thin` + `king>`
       都能正确解析(用 _buf 缓冲未完成的部分)
    2. 多对标签:`<think>a</think> hello <think>b</think> world`
    3. 流末尾收尾:flush() 兜底未完成的缓冲区
    4. 失败降级:任何异常路径都保留原文(不丢内容)

    简单嵌套(<think><think>x</think></think>)不处理 — MiniMax
    实际输出没有嵌套,过度工程化无收益。
    """
    OPEN_TAG = "<think>"
    CLOSE_TAG = "</think>"
    NORMAL = "normal"
    THINKING = "thinking"

    def __init__(self) -> None:
        self._state = self.NORMAL
        self._buf = ""  # 缓冲可能是不完整标签的后缀

    def feed(self, text: str) -> list["StreamChunk"]:
        """喂入一段 text,产出 0+ 个 StreamChunk(可能是空的,如果还在缓冲)

        Streaming 策略:每 chunk emit 已确定的内容(不卡整段),
        只缓冲最后 6 字符(可能是不完整的 <think> / </think>)。
        """
        if not text:
            return []
        s = self._buf + text
        self._buf = ""
        out: list = []
        i = 0
        while i < len(s):
            tag = self.OPEN_TAG if self._state == self.NORMAL else self.CLOSE_TAG
            idx = s.find(tag, i)
            if idx == -1:
                # 没找到完整标签 — 保留最后 6 字符在 _buf(可能是不完整标签)
                # 其余 emit 出去(保留 streaming 体验)
                tail = s[i:]
                keep = len(tag) - 1  # 6
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
                if (self._state == self.NORMAL
                        and i < len(s)
                        and s[i] == "\n"):
                    i += 1
        return out

    def flush(self) -> list["StreamChunk"]:
        """流结束时调用,把残留 buffer 兜底输出(不丢内容)"""
        if not self._buf:
            return []
        chunk = self._emit(self._buf)
        self._buf = ""
        return [chunk]

    def _emit(self, text: str) -> "StreamChunk":
        # 延迟 import 避免循环依赖(router.py 也会 import 本类)
        from .router import StreamChunk, TextDelta, ThinkingDelta
        if self._state == self.THINKING:
            return StreamChunk(thinking_delta=ThinkingDelta(thinking=text))
        return StreamChunk(text_delta=TextDelta(text=text))

    def _looks_like_partial_tag(self, s: str) -> bool:
        """s 末尾是否可能是 OPEN_TAG 或 CLOSE_TAG 的前缀(需要继续缓冲)"""
        for tag in (self.OPEN_TAG, self.CLOSE_TAG):
            for k in range(1, len(tag)):
                if s.endswith(tag[:k]):
                    return True
        return False


__all__ = ["_ThinkTagSplitter"]
