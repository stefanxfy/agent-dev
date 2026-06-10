"""
LangGraph 节点定义
对应自研版 agent_core.py 中的 ReAct 循环逻辑
"""

from typing import Literal, Dict, Any
from langchain_core.messages import HumanMessage, AIMessage, ToolMessage, SystemMessage
from langchain_core.tools import tool

from .state import AgentState


def llm_node(state: AgentState, config: dict) -> dict:
    """
    LLM 调用节点。
    对应自研版：agent_core.py 中 for chunk in self.llm.chat(...)
    
    输入：state["messages"]
    输出：AIMessage（可能含 tool_calls）
    """
    from agent_core.llm.router import StreamChunk
    
    # 从 config 获取依赖
    llm_router = config["configurable"]["llm_router"]
    tool_registry = config["configurable"]["tool_registry"]
    system_prompt = state.get("system_prompt") or config["configurable"].get("system_prompt")
    
    # 准备 messages（LangChain 格式 → 自研 Router 格式）
    messages_for_llm = []
    for msg in state["messages"]:
        if isinstance(msg, HumanMessage):
            messages_for_llm.append({"role": "user", "content": msg.content})
        elif isinstance(msg, AIMessage):
            # AI 消息可能包含 tool_calls
            content = msg.content
            if hasattr(msg, "tool_calls") and msg.tool_calls:
                # 转换为 Anthropic tool_use 格式
                tool_use_blocks = []
                for tc in msg.tool_calls:
                    tool_use_blocks.append({
                        "type": "tool_use",
                        "id": tc["id"],
                        "name": tc["name"],
                        "input": tc["args"],
                    })
                messages_for_llm.append({"role": "assistant", "content": tool_use_blocks})
            else:
                messages_for_llm.append({"role": "assistant", "content": content})
        elif isinstance(msg, ToolMessage):
            messages_for_llm.append({
                "role": "user",
                "content": [{
                    "type": "tool_result",
                    "tool_use_id": msg.tool_call_id,
                    "content": msg.content,
                }]
            })
    
    # 获取工具 schemas
    provider = "anthropic" if "claude" in llm_router.config.model.lower() else "openai"
    tool_schemas = tool_registry.list_schemas(provider=provider)
    
    # 调用 LLM（流式，但这里先收集完整响应）
    # 注意：自研版是流式 yield，LangGraph 需要在节点内完成
    full_response = ""
    tool_calls = []
    usage_stats = None
    
    # 如果有 system_prompt，插入消息开头（自研版在 messages_for_llm 中处理）
    if system_prompt:
        has_system = any(m.get("role") == "system" for m in messages_for_llm)
        if not has_system:
            messages_for_llm.insert(0, {"role": "system", "content": system_prompt})
    
    for chunk in llm_router.chat(messages_for_llm, tools=tool_schemas or None):
        if chunk.text_delta:
            full_response += chunk.text_delta.text
        if chunk.tool_call:
            tool_calls.append({
                "id": chunk.tool_call.tool_use_id,
                "name": chunk.tool_call.tool_name,
                "args": chunk.tool_call.tool_input,
            })
        if chunk.usage:
            usage_stats = chunk.usage
    
    # 构造 AIMessage（LangChain 格式）
    ai_message = AIMessage(
        content=full_response,
        tool_calls=tool_calls if tool_calls else [],
    )
    
    # 返回状态更新（LangGraph 自动合并）
    return {
        "messages": [ai_message],
        "turn": state["turn"] + 1,
        "total_tokens": (state.get("total_tokens", 0) + 
                        (usage_stats.total_tokens if usage_stats else 0)),
    }


def tool_node(state: AgentState, config: dict) -> dict:
    """
    工具执行节点。
    对应自研版：agent_core.py 中 ThreadPoolExecutor 并行执行工具
    
    输入：上一条 AIMessage 的 tool_calls
    输出：ToolMessage 列表
    """
    tool_registry = config["configurable"]["tool_registry"]
    
    # 获取最后一条消息（应该是 AIMessage）
    last_message = state["messages"][-1]
    if not isinstance(last_message, AIMessage) or not last_message.tool_calls:
        return {}  # 没有工具调用，返回空更新
    
    # 执行所有工具调用（串行，简化版）
    # 自研版用 ThreadPoolExecutor 并行，这里先串行演示
    tool_messages = []
    for tc in last_message.tool_calls:
        result = tool_registry.execute(tc["name"], tc["args"], timeout=10)
        if result["status"] == "success":
            output = result["output"]
        else:
            output = f"Error: {result['error']}"
        
        tool_messages.append(ToolMessage(
            content=str(output),
            tool_call_id=tc["id"],
            name=tc["name"],
        ))
    
    return {"messages": tool_messages}


def should_continue(state: AgentState) -> Literal["tool_node", "__end__"]:
    """
    条件路由函数。
    对应自研版：if not tool_calls: break
    
    有工具调用 → tool_node
    无工具调用 → 结束
    """
    last_message = state["messages"][-1]
    
    # 如果是 AIMessage 且有 tool_calls，继续执行工具
    if isinstance(last_message, AIMessage) and last_message.tool_calls:
        return "tool_node"
    
    # 否则结束
    return "__end__"


def max_turns_check(state: AgentState) -> Literal["__end__", "llm_node"]:
    """
    最大轮次检查。
    对应自研版：for turn in range(1, self.max_turns + 1)
    
    超过最大轮次 → 结束
    未超过 → llm_node
    """
    if state["turn"] >= state["max_turns"]:
        return "__end__"
    return "llm_node"
