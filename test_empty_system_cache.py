"""
端到端验证：空 system_prompt 场景下 Fork Compact 与主 agent 的 cache prefix 一致

复现：用户实际场景 system_prompt=""，GLM 5.1
- 主 agent 最后一次调用 LLM
- Fork Compact 调用 LLM
- 比较两次发给 LLM 的实际参数（system / tools / messages）

预期：两次的 system + tools + 前 N 条 messages 完全一致 → cache 能命中
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))

from agent_core.llm.router import LLMRouter, LLMConfig, LLMProvider
from agent_core.context.compact import (
    COMPACT_INSTRUCTION, build_compact_fork_prompt
)


def main():
    cfg = LLMConfig(
        provider="zhipu",
        model="GLM-5.1",
        api_key="test",
        system_prompt="",  # 用户实际场景
    )
    router = LLMRouter(cfg)

    # 模拟主 agent 的最后一次调用
    parent_messages = [
        {"role": "user", "content": "请同时执行以下三个任务：..."},
        {"role": "assistant", "content": "[并行调用 3 个工具]"},
        {"role": "user", "content": "你好"},
    ]
    tools = [
        {"name": "calc", "description": "calc", "input_schema": {"type": "object"}},
        {"name": "search", "description": "search", "input_schema": {"type": "object"}},
    ]

    # ── 抓主 agent 调用 ──
    captured_main = {}
    RouterClass = LLMRouter

    def mock_zhipu_main(self, msgs, tools, tool_choice=None):
        captured_main["messages"] = msgs
        captured_main["tools"] = tools
        return iter([])

    original = RouterClass._chat_zhipu
    RouterClass._chat_zhipu = mock_zhipu_main
    try:
        list(router.chat(messages=parent_messages, tools=tools))
    finally:
        RouterClass._chat_zhipu = original

    # ── 抓 Fork Compact 调用 ──
    captured_fork = {}
    def mock_zhipu_fork(self, msgs, tools, tool_choice=None):
        captured_fork["messages"] = msgs
        captured_fork["tools"] = tools
        captured_fork["tool_choice"] = tool_choice
        return iter([])

    RouterClass._chat_zhipu = mock_zhipu_fork
    try:
        # Fork 模式：传 parent_messages + build_compact_fork_prompt() 作为新 user
        forked_messages = list(parent_messages) + [
            {"role": "user", "content": build_compact_fork_prompt()}
        ]
        list(router.chat(
            messages=forked_messages,
            tools=tools,
            system_prompt_override="",  # 空 system（与主 agent 一致）
            tool_choice="none",
        ))
    finally:
        RouterClass._chat_zhipu = original

    # ── 对比 cache prefix ──
    main_msgs = captured_main["messages"]
    fork_msgs = captured_fork["messages"]
    main_tools = captured_main["tools"]
    fork_tools = captured_fork["tools"]

    print("=" * 60)
    print("📊 Cache Prefix 对比")
    print("=" * 60)
    print(f"\n主 agent 实际发送:")
    print(f"  messages[0]: {main_msgs[0]}")
    print(f"  tools: {len(main_tools)} 个")
    print(f"\nFork Compact 实际发送:")
    print(f"  messages[0]: {fork_msgs[0]}")
    print(f"  tools: {len(fork_tools)} 个")
    print(f"  tool_choice: {captured_fork['tool_choice']}")

    # ── 关键断言 ──
    # 1. 第一个 message 必须完全一致
    same_prefix = main_msgs[0] == fork_msgs[0]
    # 2. tools 必须完全一致（list 顺序 + 内容）
    same_tools = main_tools == fork_tools

    print(f"\n{'='*60}")
    print(f"{'✅' if same_prefix else '❌'} 第一个 message 一致: {same_prefix}")
    print(f"{'✅' if same_tools else '❌'} tools 一致: {same_tools}")

    if same_prefix and same_tools:
        print("\n🎉 Cache prefix 完全对齐，GLM 应能命中 cache！")
        sys.exit(0)
    else:
        print("\n❌ Cache prefix 错位，需要修代码")
        sys.exit(1)


if __name__ == "__main__":
    main()