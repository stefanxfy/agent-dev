"""
LangGraph State 定义
对比自研版的 self.history 列表管理
"""

from typing import Annotated, Sequence, TypedDict, Optional
from langgraph.graph import add_messages
from langchain_core.messages import BaseMessage


class AgentState(TypedDict):
    """
    LangGraph 状态 schema。
    
    关键设计：
    - messages: 用 add_messages reducer 自动合并消息（追加/去重）
    - turn / max_turns: 控制循环轮次
    - system_prompt: 系统提示词（可选）
    """
    
    # 消息列表（LangChain message 格式）
    # Annotated[..., add_messages] 让每个节点返回的 message 自动合并
    messages: Annotated[Sequence[BaseMessage], add_messages]
    
    # 当前轮次
    turn: int
    
    # 最大轮次
    max_turns: int
    
    # 系统提示词（可选）
    system_prompt: Optional[str]
    
    # Token 统计（可选，用于 UI 显示）
    total_tokens: int
