#!/usr/bin/env python3
"""
测试 LangGraph Agent 流式输出
验证 StreamWriter 是否正常工作
"""

import os
import sys
from pathlib import Path

# 添加项目根目录到 Python 路径
project_root = Path(__file__).parent
sys.path.insert(0, str(project_root))

# 加载环境变量
from dotenv import load_dotenv
load_dotenv()

from agent_core.llm.router import LLMRouter, LLMConfig
from agent_core.tools.base import ToolRegistry
from langgraph_agent.agent import LangGraphAgent


def test_streaming():
    """测试流式输出"""
    
    # 初始化 LLM Router（用智谱 GLM）
    config = LLMConfig(
        provider="zhipu",
        model="glm-4-flash",
        temperature=0.7,
    )
    router = LLMRouter(config)
    
    # 初始化工具注册表
    registry = ToolRegistry()
    
    # 初始化 LangGraph Agent
    agent = LangGraphAgent(
        llm_router=router,
        tool_registry=registry,
        max_turns=5,
        system_prompt="你是一个友好的AI助手，请简洁回答问题。"
    )
    
    print("=" * 60)
    print("测试 LangGraph Agent 流式输出")
    print("=" * 60)
    
    # 测试纯文本对话
    print("\n📝 测试 1: 纯文本对话")
    print("-" * 60)
    user_input = "用一句话介绍 Python 编程语言"
    
    print(f"用户: {user_input}")
    print(f"助手: ", end="", flush=True)
    
    text_chunks = []  # 收集所有文本块
    for msg_type, content in agent.run(user_input):
        if msg_type == "text":
            text_chunks.append(content)  # 收集
            print(f"[chunk:{len(text_chunks)}]", end="", flush=True)  # 显示收到几个 chunk
        elif msg_type == "system":
            print(f"\n[系统] {content}")
        elif msg_type == "usage":
            print(f"\n[Token] 总计: {content.total_tokens}")
    
    # 最后输出完整文本
    print(f"\n完整响应: {''.join(text_chunks)}")
    
    print("\n")
    
    # 测试工具调用（如果有 calc 工具）
    print("\n📝 测试 2: 工具调用")
    print("-" * 60)
    user_input = "计算 123 + 456"
    
    print(f"用户: {user_input}")
    print(f"助手: ", end="", flush=True)
    
    for msg_type, content in agent.run(user_input):
        if msg_type == "text":
            print(content, end="", flush=True)
        elif msg_type == "tool_call":
            print(f"\n[工具调用] {content['name']}({content['input']})")
        elif msg_type == "tool_result":
            print(f"[工具结果] {content['name']}: {content['output']}")
        elif msg_type == "system":
            print(f"\n[系统] {content}")
    
    print("\n")
    print("=" * 60)
    print("✅ 测试完成")
    print("=" * 60)


if __name__ == "__main__":
    test_streaming()
