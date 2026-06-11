"""
LangGraph StateGraph 构建
对比自研版的 while 循环控制流
"""

import warnings
import os

# 在最前面设置环境变量抑制警告
os.environ["LANGCHAIN_TRACING_V2"] = "false"

# 抑制已知警告（必须在导入 langgraph 之前执行）
warnings.filterwarnings("ignore", category=UserWarning)
warnings.filterwarnings("ignore", category=DeprecationWarning)

# 抑制 urllib3 OpenSSL 警告
warnings.filterwarnings("ignore", message=".*NotOpenSSL.*")
warnings.filterwarnings("ignore", message=".*LibreSSL.*")

# 抑制 LangChain 弃用警告（需要在导入前定义）
try:
    from langchain_core.warnings import LangChainPendingDeprecationWarning
    warnings.filterwarnings("ignore", category=LangChainPendingDeprecationWarning)
except ImportError:
    pass  # langchain-core 版本不支持

from langgraph.graph import StateGraph, END
from langgraph.checkpoint.memory import MemorySaver  # 🔑 阶段1：内存持久化

from .state import AgentState
from .nodes import llm_node, tool_node, should_continue, max_turns_check


def build_graph(checkpointer=None):
    """
    构建并编译 LangGraph 图。
    
    图结构：
        START → llm_node → [有工具?] → tool_node → llm_node → ...
                         ↓
                      [无工具] → END
    
    🔑 阶段1改进：支持 Checkpointer 持久化
    - checkpointer=None：无持久化（原版行为）
    - checkpointer=MemorySaver()：内存持久化（重启后状态丢失，但同会话可恢复）
    - checkpointer=SqliteSaver()：SQLite 持久化（重启后状态保留）
    
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
    
    # 🔑 阶段1：编译时注入 Checkpointer
    return graph.compile(checkpointer=checkpointer)


# 🔑 阶段1改进：不再使用模块级单例，改为动态构建
# 原因：checkpointer 需要在 Agent 初始化时注入，不能在模块导入时创建
# 使用方式：LangGraphAgent 内部调用 build_graph(checkpointer)
