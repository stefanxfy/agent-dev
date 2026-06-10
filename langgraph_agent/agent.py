"""
LangGraph Agent 封装类
暴露与自研 ReactAgent 相同的接口，便于对比测试
"""

from typing import Generator, Optional
from langchain_core.messages import HumanMessage

from .graph import compiled_graph
from .state import AgentState


class LangGraphAgent:
    """
    LangGraph 版 Agent。
    接口与自研 ReactAgent 兼容，方便对比测试。
    """
    
    def __init__(
        self,
        llm_router,
        tool_registry,
        max_turns: int = 10,
        system_prompt: Optional[str] = None,
    ):
        self.llm_router = llm_router
        self.tool_registry = tool_registry
        self.max_turns = max_turns
        self.system_prompt = system_prompt
        
        # 历史记录（用于外部查看，非 LangGraph 管理）
        self.history: list[dict] = []
    
    def run(self, user_message: str) -> Generator:
        """
        执行 Agent 循环，返回生成器。
        接口与自研 ReactAgent.run() 一致。
        
        yield 格式：
        - ("text", str): 文本增量
        - ("thinking", str): 思考过程
        - ("tool_call", dict): 工具调用
        - ("tool_result", dict): 工具结果
        - ("usage", UsageStats): Token 统计
        - ("system", str): 系统状态
        """
        # 构造初始状态
        initial_state: AgentState = {
            "messages": [HumanMessage(content=user_message)],
            "turn": 0,
            "max_turns": self.max_turns,
            "system_prompt": self.system_prompt,
            "total_tokens": 0,
        }
        
        # 配置（注入依赖）
        config = {
            "configurable": {
                "llm_router": self.llm_router,
                "tool_registry": self.tool_registry,
                "system_prompt": self.system_prompt,
            }
        }
        
        # 执行 LangGraph 流式调用
        yield ("system", "🔄 LangGraph Agent 启动")
        
        # LangGraph stream 返回每个节点的输出
        last_ai_message = None
        for event in compiled_graph.stream(initial_state, config):
            # event 格式：{"node_name": {"messages": [...]}}
            for node_name, node_output in event.items():
                if node_name == "llm_node":
                    # LLM 节点输出
                    messages = node_output.get("messages", [])
                    if messages:
                        ai_msg = messages[-1]
                        if hasattr(ai_msg, "content") and ai_msg.content:
                            # 流式输出文本（LangGraph 不支持增量，整体输出）
                            yield ("text", ai_msg.content)
                            last_ai_message = ai_msg
                        
                        # 工具调用
                        if hasattr(ai_msg, "tool_calls") and ai_msg.tool_calls:
                            for tc in ai_msg.tool_calls:
                                yield ("tool_call", {
                                    "name": tc["name"],
                                    "input": tc["args"],
                                    "id": tc["id"][:8] + "...",
                                })
                        
                        # Turn 更新
                        turn = node_output.get("turn", 0)
                        yield ("system", f"🔄 Turn {turn}/{self.max_turns}")
                
                elif node_name == "tool_node":
                    # 工具节点输出
                    messages = node_output.get("messages", [])
                    for msg in messages:
                        if hasattr(msg, "name"):
                            yield ("tool_result", {
                                "name": msg.name,
                                "output": msg.content[:200] + "..." if len(msg.content) > 200 else msg.content,
                                "success": not msg.content.startswith("Error:"),
                            })
                
                # Token 统计
                if "total_tokens" in node_output:
                    yield ("usage", type("Usage", (), {
                        "total_tokens": node_output["total_tokens"],
                        "input_tokens": 0,
                        "output_tokens": node_output["total_tokens"],
                    })())
        
        # 结束标记
        if last_ai_message and not getattr(last_ai_message, "tool_calls", []):
            yield ("system", "✅ 回答完成")
        else:
            yield ("system", f"⚠️ 达到最大轮次（{self.max_turns}），强制结束")
        
        # 更新历史记录（用于外部查看）
        final_state = compiled_graph.get_state(config)
        self.history = [
            {"role": msg.type, "content": msg.content}
            for msg in final_state.values.get("messages", [])
        ]
    
    def reset(self):
        """重置会话历史"""
        self.history.clear()
