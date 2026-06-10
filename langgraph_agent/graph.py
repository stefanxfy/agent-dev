"""
LangGraph StateGraph 构建
对比自研版的 while 循环控制流
"""

from langgraph.graph import StateGraph, END

from .state import AgentState
from .nodes import llm_node, tool_node, should_continue, max_turns_check


def build_graph():
    """
    构建并编译 LangGraph 图。
    
    图结构：
        START → llm_node → [有工具?] → tool_node → llm_node → ...
                         ↓
                      [无工具] → END
    
    对比自研版：
        while True:
            LLM 响应
            if tool_calls:
                执行工具
            else:
                break
    """
    graph = StateGraph(AgentState)
    
    # 添加节点
    graph.add_node("llm_node", llm_node)
    graph.add_node("tool_node", tool_node)
    
    # 设置入口
    graph.set_entry_point("llm_node")
    
    # 条件边：llm_node 之后判断是否继续
    # 有工具 → tool_node，无工具 → END
    graph.add_conditional_edges(
        "llm_node",
        should_continue,
        {
            "tool_node": "tool_node",
            "__end__": END,
        }
    )
    
    # tool_node 执行完后回到 llm_node
    graph.add_edge("tool_node", "llm_node")
    
    return graph.compile()


# 构建单例图实例
compiled_graph = build_graph()
