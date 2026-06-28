"""
聊天界面 — 演示 SessionManager 与 ReactAgent 的融合效果

功能：
1. 创建/加载会话
2. 与 ReactAgent 对话
3. 历史自动持久化（刷新页面不丢失）
4. 显示当前 session 信息
"""

import sys
import os
import streamlit as st
from pathlib import Path

# ── 路径设置 ────────────────────────────────────────────────────────
ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(ROOT))

# ── 导入 ────────────────────────────────────────────────────────────
try:
    from agent_core.llm.router import LLMRouter
    from agent_core.tools.base import ToolRegistry
    from agent_core.agent_core import ReactAgent
    from agent_core.session.manager import SessionManager
except Exception as e:
    st.error(f"导入失败: {e}")
    st.stop()


# ── 页面配置 ────────────────────────────────────────────────────────
st.set_page_config(
    page_title="Agent 聊天",
    page_icon="💬",
    layout="wide",
)

st.title("💬 Agent 聊天 — SessionManager 融合演示")


# ── 初始化 ──────────────────────────────────────────────────────────

# Session 状态
if "chat_session_id" not in st.session_state:
    st.session_state.chat_session_id = None

if "agent" not in st.session_state:
    st.session_state.agent = None

# 数据目录
DATA_DIR = str(ROOT / "data" / "sessions")


# ── 侧边栏：会话管理 ──────────────────────────────────────────────

with st.sidebar:
    st.header("会话管理")
    
    # 新建会话
    if st.button("➕ 新建会话"):
        import uuid
        new_id = str(uuid.uuid4())[:8]
        st.session_state.chat_session_id = new_id
        st.session_state.agent = None  # 重置 Agent
        st.rerun()
    
    st.divider()
    
    # LLM 配置(provider 列表由 LLMProvider enum 派生)
    from web.llm_options import get_provider_options

    st.subheader("LLM 配置")
    # 显式 key= 让 session_state 可预测 (避免 value= 切换时 widget 状态被清)
    provider = st.selectbox("Provider", get_provider_options(), index=0, key="chat_provider")
    model = st.text_input("Model", value="claude-3-7-sonnet-20250219" if provider == "anthropic" else "", key="chat_model")
    api_key = st.text_input("API Key", type="password", key="chat_api_key")
    temperature = st.slider("Temperature", 0.0, 1.0, 0.7)
    max_tokens = st.number_input("Max Tokens", value=4096)
    
    st.divider()
    
    # 加载已有会话
    st.subheader("加载已有会话")
    
    try:
        mgr = SessionManager(data_dir=DATA_DIR)
        sessions = mgr.list_sessions()
        
        if sessions:
            for sess in sessions[:10]:  # 最多显示 10 个
                sid = sess["session_id"]
                label = sess["title"] or sid[:12] + "..."
                if st.button(f"📄 {label}", key=f"load_chat_{sid}"):
                    st.session_state.chat_session_id = sid
                    st.session_state.agent = None  # 重置 Agent（下次会自动加载）
                    st.rerun()
        else:
            st.caption("暂无会话")
    except Exception as e:
        st.error(f"加载会话列表失败: {e}")
    
    st.divider()
    
    # 当前会话信息
    if st.session_state.chat_session_id:
        st.subheader("当前会话")
        st.code(f"ID: {st.session_state.chat_session_id}")
        
        # 显示消息数
        try:
            mgr = SessionManager(session_id=st.session_state.chat_session_id, data_dir=DATA_DIR)
            msgs = mgr.get_messages()
            st.metric("消息数", len(msgs))
        except Exception:
            pass


# ── 主界面 ──────────────────────────────────────────────────────────

# 检查是否选择了会话
if not st.session_state.chat_session_id:
    st.info("👈 请在侧边栏新建会话或加载已有会话")
    st.stop()

# 初始化/恢复 Agent
if st.session_state.agent is None:
    try:
        # 从 sidebar 读取配置
        import os
        
        # 如果 sidebar 没有输入 API Key，尝试从环境变量读取
        if not api_key:
            if provider == "anthropic":
                api_key = os.getenv("ANTHROPIC_API_KEY", "")
            elif provider == "openai":
                api_key = os.getenv("OPENAI_API_KEY", "")
            elif provider == "zhipu":
                api_key = os.getenv("ZHIPU_API_KEY", "")
        
        if not api_key:
            st.error("请先在侧边栏输入 API Key")
            st.stop()
        
        # 创建 LLM 配置
        from agent_core.llm.router import LLMConfig
        config = LLMConfig(
            provider=provider,
            model=model,
            api_key=api_key,
            temperature=temperature,
            max_tokens=max_tokens,
            stream=True,
        )
        
        # 创建 LLM Router
        llm = LLMRouter(config)
        
        # 创建 Tool Registry
        from agent_core.tools.builtin import register_builtin_tools
        tools = ToolRegistry()
        register_builtin_tools(tools)
        
        # 创建 ReactAgent（启用 session）
        agent = ReactAgent(
            llm_router=llm,
            tool_registry=tools,
            session_id=st.session_state.chat_session_id,
            session_data_dir=DATA_DIR,
        )
        
        st.session_state.agent = agent
        st.success(f"✅ Agent 已初始化，session_id={st.session_state.chat_session_id}")
    except Exception as e:
        st.error(f"Agent 初始化失败: {e}")
        st.stop()

# 显示聊天历史
st.subheader("聊天历史")

agent = st.session_state.agent

# 从 session 加载历史消息
try:
    mgr = SessionManager(session_id=agent.session_id, data_dir=DATA_DIR)
    history = mgr.get_messages()
    
    for msg in history:
        role = msg.get("role", "unknown")
        content = msg.get("content", "")
        
        if role == "user":
            with st.chat_message("user"):
                st.write(content)
        elif role == "assistant":
            with st.chat_message("assistant"):
                if isinstance(content, str):
                    st.write(content)
                else:
                    st.json(content)
except Exception as e:
    st.error(f"加载历史失败: {e}")


# ── 用户输入 ────────────────────────────────────────────────────────

user_input = st.chat_input("输入消息...")

if user_input:
    # 显示用户消息
    with st.chat_message("user"):
        st.write(user_input)
    
    # 调用 Agent
    with st.chat_message("assistant"):
        message_placeholder = st.empty()
        full_response = ""
        
        try:
            # 运行 ReAct 循环
            for chunk_type, chunk_data in agent.run(user_input):
                if chunk_type == "text":
                    full_response += chunk_data
                    message_placeholder.markdown(full_response + "▌")
                elif chunk_type == "system":
                    # 系统消息（如 "Turn 1/10"）不显示
                    pass
                elif chunk_type == "tool_call":
                    # 工具调用提示
                    if isinstance(chunk_data, dict):
                        names = chunk_data.get("names", [chunk_data.get("name")])
                        st.caption(f"🔧 调用工具: {', '.join(names)}")
                elif chunk_type == "tool_result":
                    # 工具结果提示
                    if isinstance(chunk_data, dict):
                        st.caption(f"✅ 工具返回: {chunk_data.get('name')} ({chunk_data.get('elapsed', 0):.2f}s)")
            
            # 显示完整响应
            message_placeholder.markdown(full_response)
            
            # 显示 session 信息
            st.caption(f"💾 已保存到 session: {agent.session_id}")
            
        except Exception as e:
            st.error(f"Agent 执行失败: {e}")

# ── 调试信息 ────────────────────────────────────────────────────────

with st.expander("🔧 调试信息"):
    st.write("**Session ID:**", st.session_state.chat_session_id)
    st.write("**Sidebar Model (session_state):**", model)
    st.write("**Sidebar Provider (session_state):**", provider)
    if agent:
        st.write("**Agent Session ID:**", agent.session_id)
        st.write("**History Length:**", len(agent.messages))
        st.write("**LLM Provider (agent):**", agent.llm.config.provider)
        st.write("**LLM Model (agent):**", agent.llm.config.model)
        st.write("**LLM Base URL (agent):**", agent.llm.config.base_url)
