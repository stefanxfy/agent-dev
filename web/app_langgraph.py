"""
Streamlit Web UI — LangGraph Agent
对比自研 ReactAgent，学习框架设计思想
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

# ── 配置日志 ──────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] %(message)s',
    datefmt='%H:%M:%S',
)

# ── 加载 .env 文件 ────────────────────────────────────────────────
from dotenv import load_dotenv
load_dotenv()

# ── 项目根目录加入 sys.path ────────────────────────────────────────
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# ── 必须先设 PATH 再 import streamlit ──────────────────────────────
import streamlit as st

# ── 页面配置（必须是第一个 Streamlit 命令）──────────────────────
st.set_page_config(
    page_title="LangGraph Agent",
    page_icon="🔀",
    layout="wide",
)

st.title("🔀 LangGraph Agent")
st.caption("用 LangGraph 重构 ReAct 循环 · 对比自研版本设计思想")

# ── Import Agents ───────────────────────────────────────────────────
from agent_core.llm.router import LLMRouter, LLMConfig, UsageStats
from agent_core.tools.base import ToolRegistry
from agent_core.tools.builtin import register_builtin_tools
from langgraph_agent import LangGraphAgent


# ── Session State 初始化 ─────────────────────────────────────────
if "messages" not in st.session_state:
    st.session_state.messages = []
if "agent" not in st.session_state:
    st.session_state.agent = None
if "token_stats" not in st.session_state:
    st.session_state.token_stats = {"input": 0, "output": 0, "thinking": 0}
if "system_prompt" not in st.session_state:
    st.session_state.system_prompt = ""


# ── 侧边栏：LLM 配置 ─────────────────────────────────────────────
with st.sidebar:
    st.header("⚙️ LLM 配置")

    default_provider = os.getenv("DEFAULT_PROVIDER", "zhipu").lower()
    provider_index = 2  # 默认 zhipu
    if default_provider in ["anthropic", "openai", "zhipu"]:
        provider_index = ["anthropic", "openai", "zhipu"].index(default_provider)

    provider = st.selectbox(
        "厂商",
        options=["anthropic", "openai", "zhipu"],
        index=provider_index,
    )

    model_options = {
        "anthropic": [
            "claude-sonnet-4-20250514",
            "claude-opus-4-20250514",
            "claude-haiku-4-20250514",
        ],
        "openai": ["gpt-4o", "gpt-4o-mini", "gpt-4.1", "o3-mini"],
        "zhipu": ["GLM-5.1", "glm-5-turbo", "GLM-4.7"],
    }
    model = st.selectbox("模型", options=model_options[provider])

    env_key_map = {
        "anthropic": "ANTHROPIC_API_KEY",
        "openai": "OPENAI_API_KEY",
        "zhipu": "ZHIPU_API_KEY",
    }
    env_key_var = env_key_map.get(provider, "OPENAI_API_KEY")
    default_key = os.getenv(env_key_var, "")
    api_key = st.text_input(
        "API Key（留空则使用 .env）",
        value=default_key,
        type="password",
    )
    if not api_key:
        api_key = default_key

    temperature = st.slider("Temperature", 0.0, 2.0, 0.7, 0.1)
    max_tokens = st.number_input("Max Tokens", 256, 16384, 4096, 256)
    max_turns = st.number_input("最大工具调用轮次", 1, 20, 10, 1)

    st.divider()
    st.subheader("📝 System Prompt")
    system_prompt = st.text_area(
        "系统提示词（留空则不使用）",
        value=st.session_state.system_prompt,
        height=100,
        help="设置 Agent 的角色和行为规则",
    )
    st.session_state.system_prompt = system_prompt

    st.divider()
    st.subheader("📊 Token 消耗（本次会话）")
    stats = st.session_state.token_stats
    st.metric("Total", f"{stats['input'] + stats['output'] + stats['thinking']:,}")

    st.divider()
    if st.button("🗑️ 清空会话"):
        st.session_state.messages = []
        st.session_state.token_stats = {"input": 0, "output": 0, "thinking": 0}
        if st.session_state.agent:
            st.session_state.agent.reset()
        st.rerun()


# ── 初始化 LangGraph Agent ───────────────────────────────────────
def get_agent():
    """创建 LangGraph Agent 实例"""
    config = LLMConfig(
        provider=provider.lower(),
        model=model,
        api_key=api_key,
        temperature=temperature,
        max_tokens=max_tokens,
        stream=True,
        system_prompt=st.session_state.get("system_prompt", ""),
    )
    router = LLMRouter(config)
    registry = ToolRegistry()
    register_builtin_tools(registry)
    
    # 使用 LangGraph Agent（对比自研 ReactAgent）
    agent = LangGraphAgent(router, registry, max_turns=max_turns)
    return agent


# 配置变化时自动重建 Agent
current_config = {
    "provider": provider,
    "model": model,
    "api_key": api_key,
    "temperature": temperature,
    "max_tokens": max_tokens,
    "max_turns": max_turns,
    "system_prompt": st.session_state.get("system_prompt", ""),
}
if (st.session_state.agent is None or
        st.session_state.get("last_agent_config") != current_config):
    st.session_state.agent = get_agent()
    st.session_state.last_agent_config = current_config

agent = st.session_state.agent


# ── 主聊天界面 ────────────────────────────────────────────────────
for msg in st.session_state.messages:
    with st.chat_message(msg["role"]):
        if msg.get("tool_logs"):
            with st.expander("🔧 工具调用", expanded=False):
                for log in msg["tool_logs"]:
                    st.markdown(f"- {log}")
        st.markdown(msg["content"])

# 用户输入
if prompt := st.chat_input("输入消息..."):
    if not api_key:
        st.error("请先在侧边栏填写 API Key（或配置 .env 文件）")
        st.stop()

    with st.chat_message("user"):
        st.markdown(prompt)
    st.session_state.messages.append({"role": "user", "content": prompt})

    with st.chat_message("assistant"):
        text_placeholder = st.empty()
        tool_expander = st.expander("🔧 工具调用", expanded=True)
        status = st.status("🔄 LangGraph 思考中...", expanded=True)
        
        full_text = ""
        tool_logs = []
        
        for msg_type, content in agent.run(prompt):
            if msg_type == "text":
                full_text += content
                text_placeholder.markdown(full_text + "▌")
            elif msg_type == "tool_call":
                tool_logs.append(f"🔧 调用: {content['name']}")
                status.update(label=f"🔧 执行工具: {content['name']}...")
            elif msg_type == "tool_result":
                tool_logs.append(f"✅ 结果: {content['output'][:100]}...")
                status.update(label=f"✅ {content['name']} 完成")
            elif msg_type == "system":
                if "完成" in str(content):
                    status.update(label=content, state="complete")
                else:
                    status.update(label=content)
            elif msg_type == "usage":
                stats = st.session_state.token_stats
                stats["input"] += getattr(content, "input_tokens", 0)
                stats["output"] += getattr(content, "output_tokens", 0)
                stats["thinking"] += getattr(content, "thinking_tokens", 0)
        
        text_placeholder.markdown(full_text)
        
        with tool_expander:
            for log in tool_logs:
                st.markdown(log)
    
    st.session_state.messages.append({
        "role": "assistant",
        "content": full_text,
        "tool_logs": tool_logs,
    })
