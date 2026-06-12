"""
测试 LangGraph Agent Checkpointer 持久化功能
验证：
1. 同 thread_id 多轮对话自动累积历史
2. 不同 thread_id 会话隔离
3. reset() 切换 thread_id
4. switch_thread() 恢复旧会话
5. get_history() 从 Checkpointer 获取历史
"""

import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv
load_dotenv()

from agent_core.llm.router import LLMRouter, LLMConfig
from agent_core.tools.base import ToolRegistry
from agent_core.tools.builtin import register_builtin_tools
from langgraph_agent.agent import LangGraphAgent

# ── 配置 Agent ──────────────────────────────────────
provider = os.getenv("DEFAULT_PROVIDER", "zhipu").lower()
model = os.getenv("DEFAULT_MODEL", "GLM-4.7")

config = LLMConfig(
    provider=provider,
    model=model,
    stream=True,
)
router = LLMRouter(config)
registry = ToolRegistry()
register_builtin_tools(registry)

agent = LangGraphAgent(router, registry, max_turns=5)

# ── 测试 1：同 thread_id 多轮对话累积 ──────────────────
print("=" * 60)
print("📝 测试 1: 同 thread_id 多轮对话自动累积历史")
print("=" * 60)

# 第一轮
print("\n[第1轮对话]")
full_text = ""
for msg_type, content in agent.run("我叫小明"):
    if msg_type == "text":
        full_text += content
print(f"用户: 我叫小明")
print(f"助手: {full_text}")

# 第二轮（验证 Checkpointer 是否自动恢复第一轮的历史）
print("\n[第2轮对话 — 验证记忆]")
full_text = ""
for msg_type, content in agent.run("你还记得我叫什么名字吗？"):
    if msg_type == "text":
        full_text += content
print(f"用户: 你还记得我叫什么名字吗？")
print(f"助手: {full_text}")

# 从 Checkpointer 获取历史
history = agent.get_history()
print(f"\n📚 Checkpointer 历史消息数: {len(history)}")
for i, msg in enumerate(history):
    print(f"  [{i}] {msg['role']}: {msg['content'][:50]}...")

# ── 测试 2：不同 thread_id 会话隔离 ──────────────────
print("\n" + "=" * 60)
print("📝 测试 2: 不同 thread_id 会话隔离")
print("=" * 60)

old_thread = agent.get_thread_id()
agent.reset()  # 切换到新 thread_id
new_thread = agent.get_thread_id()
print(f"旧 thread: {old_thread} → 新 thread: {new_thread}")

# 新会话中提问（不应该知道"小明"）
print("\n[新会话 — 应不记得小明]")
full_text = ""
for msg_type, content in agent.run("我叫什么名字？"):
    if msg_type == "text":
        full_text += content
print(f"用户: 我叫什么名字？")
print(f"助手: {full_text}")

# ── 测试 3：switch_thread 恢复旧会话 ──────────────────
print("\n" + "=" * 60)
print("📝 测试 3: switch_thread() 恢复旧会话")
print("=" * 60)

agent.switch_thread(old_thread)  # 回到旧会话
print(f"切换回 thread: {agent.get_thread_id()}")

# 在旧会话继续（应该还记得"小明")
print("\n[恢复旧会话 — 应记得小明]")
full_text = ""
for msg_type, content in agent.run("我刚才告诉你我叫什么？"):
    if msg_type == "text":
        full_text += content
print(f"用户: 我刚才告诉你我叫什么？")
print(f"助手: {full_text}")

history = agent.get_history()
print(f"\n📚 旧会话历史消息数: {len(history)}")

print("\n" + "=" * 60)
print("✅ Checkpointer 持久化测试完成")
print("=" * 60)