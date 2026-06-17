"""
验证 GLM 流式 thinking 与 text 的到达顺序

修复前：text 流式显示 → 流结束后才一次性 yield thinking
修复后：thinking 逐块 yield，text 逐块 yield（交错或先后到达，看 GLM 实际行为）

这个脚本模拟真实的 GLM 流，验证 router.py 的修改是否正确处理 thinking 的实时 yield。
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))

from agent_core.llm.router import LLMRouter, LLMConfig, StreamChunk
from dataclasses import dataclass, field


def main():
    # 构造模拟的 GLM 流（按真实 GLM 行为：reasoning_content 和 content 交错到达）
    @dataclass
    class MockDelta:
        content: str = ""
        reasoning_content: str = ""
        tool_calls: list = field(default_factory=list)

    @dataclass
    class MockChoice:
        delta: MockDelta

    @dataclass
    class MockChunk:
        choices: list = field(default_factory=list)
        usage: object = None

    @dataclass
    class MockUsage:
        prompt_tokens: int = 100
        completion_tokens: int = 50
        prompt_tokens_details: object = None

    # 模拟真实流：thinking 先来（3 块），然后 text 来（3 块）
    mock_chunks = [
        MockChunk(choices=[MockChoice(delta=MockDelta(reasoning_content="我需要") )]),
        MockChunk(choices=[MockChoice(delta=MockDelta(reasoning_content="计算 123") )]),
        MockChunk(choices=[MockChoice(delta=MockDelta(reasoning_content=" * 456") )]),
        MockChunk(choices=[MockChoice(delta=MockDelta(content="计算结果是") )]),
        MockChunk(choices=[MockChoice(delta=MockDelta(content=" 56088") )]),
        MockChunk(usage=MockUsage()),
    ]

    # Monkey-patch zhipu client
    cfg = LLMConfig(provider="zhipu", model="GLM-5.1", api_key="test", system_prompt="")
    router = LLMRouter(cfg)

    def mock_create(**kwargs):
        return iter(mock_chunks)

    # 替换内部的 zhipu client
    class MockClient:
        class chat:
            class completions:
                create = staticmethod(mock_create)

    router._zhipu_client = MockClient()

    # ── 收集所有 yield 出来的 chunk，按顺序记录 ──
    events = []
    for chunk in router.chat(
        messages=[{"role": "user", "content": "123 * 456 是多少"}],
        tools=None,
    ):
        if chunk.text_delta:
            events.append(("text", chunk.text_delta.text))
        elif chunk.thinking_delta:
            events.append(("thinking", chunk.thinking_delta.thinking))
        elif chunk.usage:
            events.append(("usage", f"in={chunk.usage.input_tokens}"))

    # ── 打印事件序列 ──
    print("=" * 60)
    print("📡 Router yield 事件序列（实时）")
    print("=" * 60)
    for i, (etype, content) in enumerate(events):
        print(f"  [{i}] {etype}: {content!r}")

    # ── 验证 ──
    has_thinking_before_text = any(
        etype == "thinking" for etype, _ in events
    ) and any(etype == "text" for etype, _ in events)

    # 检查 thinking 是不是真的逐块 yield（不是攒起来一次性）
    thinking_chunks = [c for etype, c in events if etype == "thinking"]
    multi_chunk_thinking = len(thinking_chunks) > 1

    print("\n" + "=" * 60)
    print("✅ 验证结果")
    print("=" * 60)
    print(f"  thinking 逐块 yield（多次）: {multi_chunk_thinking}")
    print(f"  thinking 与 text 都有: {has_thinking_before_text}")

    if multi_chunk_thinking:
        print("\n🎉 GLM thinking 真正流式 yield，UI 可以实时显示！")
        sys.exit(0)
    else:
        print("\n❌ thinking 仍然是一次性 yield，需要再排查")
        sys.exit(1)


if __name__ == "__main__":
    main()