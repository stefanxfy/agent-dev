"""
LangGraph 节点定义
对应自研版 agent_core.py 中的 ReAct 循环逻辑
"""

from typing import Literal, Dict, Any
from langchain_core.messages import HumanMessage, AIMessage, ToolMessage, SystemMessage
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool
from langgraph.types import StreamWriter  # 🔑 新增：支持流式输出
from concurrent.futures import ThreadPoolExecutor, as_completed  # 🔑 并行工具调用

from .state import AgentState


def llm_node(
    state: AgentState, 
    config: RunnableConfig,
    writer: StreamWriter,  # 🔑 新增第三个参数：流式写入器
) -> dict:
    """
    LLM 调用节点（支持流式输出）。
    对应自研版：agent_core.py 中 for chunk in self.llm.chat(...)
    
    输入：state["messages"]
    输出：AIMessage（可能含 tool_calls）
    流式：通过 writer 增量输出文本/工具调用
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
    
    # 调用 LLM（流式，通过 writer 增量输出）
    # 🔑 关键改进：边接收边写入 Stream，不等待完整响应
    full_response = ""
    tool_calls = []
    usage_stats = None
    
    # 如果有 system_prompt，插入消息开头（自研版在 messages_for_llm 中处理）
    if system_prompt:
        has_system = any(m.get("role") == "system" for m in messages_for_llm)
        if not has_system:
            messages_for_llm.insert(0, {"role": "system", "content": system_prompt})
    
    # 🔑 错误处理：LLM 调用异常时优雅降级
    try:
        chunk_iter = llm_router.chat(messages_for_llm, tools=tool_schemas or None)
    except Exception as e:
        error_msg = f"LLM 调用失败: {str(e)}"
        writer({"type": "error", "message": error_msg})
        ai_message = AIMessage(content=error_msg)
        return {
            "messages": [ai_message],
            "turn": state["turn"] + 1,
            "total_tokens": state.get("total_tokens", 0),
        }
    
    try:
        for chunk in chunk_iter:
            if chunk.text_delta:
                full_response += chunk.text_delta.text
                # 🔑 流式输出：每个 token 通过 writer 发送
                writer({"type": "text", "content": chunk.text_delta.text})
            
            if chunk.thinking_delta:
                # 🔑 流式输出：思考过程（Claude 3.7+）
                writer({"type": "thinking", "content": chunk.thinking_delta.thinking})
            
            if chunk.tool_call:
                tool_calls.append({
                    "id": chunk.tool_call.tool_use_id,
                    "name": chunk.tool_call.tool_name,
                    "args": chunk.tool_call.tool_input,
                })
                # 🔑 流式输出：工具调用
                writer({
                    "type": "tool_call",
                    "name": chunk.tool_call.tool_name,
                    "input": chunk.tool_call.tool_input,
                    "id": chunk.tool_call.tool_use_id[:8] + "...",
                })
            
            if chunk.usage:
                usage_stats = chunk.usage
    except Exception as e:
        # LLM 流式输出中途异常
        error_msg = f"LLM 流式输出中断: {str(e)}"
        writer({"type": "error", "message": error_msg})
        # 如果已有部分文本，返回部分结果；否则返回错误信息
        if not full_response:
            full_response = error_msg
    
    # 构造 AIMessage（LangChain 格式）
    ai_message = AIMessage(
        content=full_response,
        tool_calls=tool_calls if tool_calls else [],
    )
    
    # 🔑 流式输出：Turn 更新
    new_turn = state["turn"] + 1
    writer({"type": "turn", "turn": new_turn, "max_turns": state["max_turns"]})
    
    # 返回状态更新（LangGraph 自动合并）
    return {
        "messages": [ai_message],
        "turn": new_turn,
        "total_tokens": (state.get("total_tokens", 0) + 
                        (usage_stats.total_tokens if usage_stats else 0)),
    }


def tool_node(
    state: AgentState, 
    config: RunnableConfig,
    writer: StreamWriter,  # 🔑 新增第三个参数：流式写入器
) -> dict:
    """
    工具执行节点（支持流式输出）。
    对应自研版：agent_core.py 中 ThreadPoolExecutor 并行执行工具
    
    输入：上一条 AIMessage 的 tool_calls
    输出：ToolMessage 列表
    流式：通过 writer 输出工具执行结果
    """
    tool_registry = config["configurable"]["tool_registry"]
    
    # 获取最后一条消息（应该是 AIMessage）
    last_message = state["messages"][-1]
    if not isinstance(last_message, AIMessage) or not last_message.tool_calls:
        return {}  # 没有工具调用，返回空更新
    
    # 🔑 并行执行所有工具调用（与自研版一致）
    tool_messages = []
    
    def _execute_tool(tc):
        """单个工具执行的闭包（用于并行）"""
        result = tool_registry.execute(tc["name"], tc["args"], timeout=10)
        if result["status"] == "success":
            output = result["output"]
        else:
            output = f"Error: {result['error']}"
        return tc, output, result["status"] == "success"
    
    # 使用 ThreadPoolExecutor 并行执行（与自研版 agent_core.py 一致）
    with ThreadPoolExecutor(max_workers=min(len(last_message.tool_calls), 5)) as executor:
        futures = {executor.submit(_execute_tool, tc): tc for tc in last_message.tool_calls}
        # 按 completion 顺序收集结果（as_completed），但按原始顺序排列
        results_by_index = {}
        for future in as_completed(futures):
            try:
                tc, output, success = future.result(timeout=15)
                idx = last_message.tool_calls.index(tc)
                results_by_index[idx] = (tc, output, success)
            except Exception as e:
                # 超时或异常处理
                tc = futures[future]
                idx = last_message.tool_calls.index(tc)
                results_by_index[idx] = (tc, f"Timeout/Error: {str(e)}", False)
        
        # 按原始顺序输出（保持可预测性）
        for idx in sorted(results_by_index.keys()):
            tc, output, success = results_by_index[idx]
            
            # 🔑 流式输出：工具执行结果
            writer({
                "type": "tool_result",
                "name": tc["name"],
                "output": str(output)[:200] + "..." if len(str(output)) > 200 else str(output),
                "success": success,
            })
            
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
