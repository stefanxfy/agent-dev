"""
Streamlit Web UI — ReAct Agent + 工具调用可视化
Day 3 版本：并行工具调用、Token 预算管理、System Prompt、结构化 UI
"""

from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path

# ── 配置日志（在加载任何模块之前）──────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] %(message)s',
    datefmt='%H:%M:%S',
)
# 日志级别由 agent_core.py 统一管理，这里不再重复配置
# （避免 Streamlit 热重载导致 handler 重复添加）

# ── 加载 .env 文件（必须在最前面）────────────────────────────
from dotenv import load_dotenv
load_dotenv()

# ── 项目根目录加入 sys.path ────────────────────────────────────
PROJECT_ROOT = Path(__file__).parent.parent.resolve()  # 使用绝对路径
sys.path.insert(0, str(PROJECT_ROOT))

# ── 全局常量（必须在 sidebar 之外定义）──────────────────────────
DATA_DIR = str(PROJECT_ROOT / "data" / "sessions")

# ── 必须先设 PATH 再 import streamlit ──────────────────────────
import streamlit as st

# ── 页面配置（必须是第一个 Streamlit 命令）────────────────────
st.set_page_config(
    page_title="Agent Dev Playground",
    page_icon="🤖",
    layout="wide",
)

st.title("🤖 Agent 开发学习平台")
st.caption("Day 3 — 生产级优化 · 并行工具 · Token 预算 · System Prompt")

# ── Import Agent Core ────────────────────────────────────────────
from agent_core.llm.router import (
    LLMRouter,
    LLMConfig,
    StreamChunk,
    UsageStats,
)
from agent_core.agent_core import ReactAgent
from agent_core.tools.base import ToolRegistry
from agent_core.tools.builtin import register_builtin_tools


# ── Session State 初始化 ───────────────────────────────────────
if "messages" not in st.session_state:
    st.session_state.messages = []
if "agent" not in st.session_state:
    st.session_state.agent = None
if "token_stats" not in st.session_state:
    st.session_state.token_stats = {"input": 0, "output": 0, "thinking": 0}
if "current_thinking" not in st.session_state:
    st.session_state.current_thinking = ""
if "tool_logs" not in st.session_state:
    st.session_state.tool_logs = []
if "system_prompt" not in st.session_state:
    st.session_state.system_prompt = ""  # P2 新增：System Prompt
# Day 4: 从 URL query params 恢复 session_id（刷新不丢失）
if "chat_session_id" not in st.session_state:
    # 优先从 URL query param 获取
    url_sid = st.query_params.get("session", None)
    logging.warning(f"[DEBUG-INIT] url_sid={url_sid}, query_params={dict(st.query_params)}")
    if url_sid:
        st.session_state.chat_session_id = url_sid
    else:
        st.session_state.chat_session_id = None


# ── 侧边栏：LLM 配置 ─────────────────────────────────────────
with st.sidebar:
    st.header("⚙️ LLM 配置")

    # 从 .env 读取默认厂商
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
        "openai": [
            "gpt-4o",
            "gpt-4o-mini",
            "gpt-4.1",
            "o3-mini",
        ],
        "zhipu": [
            "GLM-5.1",
            "glm-5-turbo",
            "GLM-4.7",
        ],
    }
    model = st.selectbox("模型", options=model_options[provider])

    # API Key
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
    st.session_state.system_prompt = system_prompt  # 保存到 session_state

    st.divider()
    st.subheader("📊 Token 消耗（本次会话）")
    stats = st.session_state.token_stats
    st.metric("Input", f"{stats['input']:,}")
    st.metric("Output", f"{stats['output']:,}")
    st.metric("Thinking", f"{stats['thinking']:,}")
    st.metric("Total", f"{stats['input'] + stats['output'] + stats['thinking']:,}")

    st.divider()
    st.subheader("📊 Agent 状态")
    if st.session_state.agent:
        agent = st.session_state.agent
        st.metric("History 长度", f"{len(agent.history)} 条")
        # 估算 Token
        est_tokens = sum(agent._estimate_message_tokens(m) for m in agent.history)
        st.metric("预估 Token", f"{est_tokens:,}")
        st.caption(f"预算: {agent.max_context_tokens:,}")
        
        # 显示最后一条消息的 role
        if agent.history:
            last_role = agent.history[-1].get("role", "unknown")
            st.caption(f"最后消息: {last_role}")

    # 历史记录查看器
    if st.session_state.agent and st.session_state.agent.history:
        st.divider()
        st.subheader("📜 历史记录")
        history = st.session_state.agent.history
        # 显示最近 5 条
        recent = history[-5:] if len(history) > 5 else history
        for i, msg in enumerate(recent):
            role = msg.get("role", "unknown")
            content_preview = ""
            if isinstance(msg.get("content"), str):
                content_preview = msg["content"][:50]
            else:
                content_preview = f"[{len(msg.get('content', []))} blocks]"
            with st.expander(f"{i+1}. {role}: {content_preview}..."):
                st.json(msg)

    # ── 会话管理 ────────────────────────────────────────────────
    st.divider()
    st.subheader("💾 会话管理")
    
    # DATA_DIR 已移到文件顶部定义
    
    # 新建会话
    def create_session():
        """新建会话"""
        import uuid
        new_id = str(uuid.uuid4())[:8]
        try:
            mgr = SessionManager(session_id=new_id, data_dir=DATA_DIR)
            mgr.flush()
        except Exception:
            pass
        # 关闭旧 Agent
        if st.session_state.agent is not None:
            try:
                st.session_state.agent.close()
            except Exception:
                pass
        st.session_state.chat_session_id = new_id
        st.session_state.agent = None

    def switch_session(sid: str):
        """切换到指定会话"""
        if sid == st.session_state.get("chat_session_id"):
            return
        if st.session_state.agent is not None:
            try:
                st.session_state.agent.close()
            except Exception:
                pass
        st.session_state.chat_session_id = sid
        st.session_state.agent = None

    if st.button("➕ 新建会话", key="new_session", on_click=create_session):
        pass  # 回调在 on_click 中处理

    # 加载已有会话
    from agent_core.session.manager import SessionManager
    try:
        sessions = SessionManager.list_sessions(data_dir=DATA_DIR)
        logging.warning(f"[DEBUG-SIDEBAR] DATA_DIR={DATA_DIR}, sessions={len(sessions)}")
        if sessions:
            # 当前会话排第一，其余按 mtime 倒序
            current_sid = st.session_state.get("chat_session_id")
            sessions.sort(key=lambda s: (
                0 if s["session_id"] == current_sid else 1,
                -s["updated_at"].timestamp() if hasattr(s["updated_at"], "timestamp") else 0
            ))
            st.write(f"**会话列表** ({len(sessions)}个)")
            # 使用容器实现滚动
            with st.container(height=300):
                for sess in sessions:
                    sid = sess["session_id"]
                    title = sess.get("title") or "未命名"
                    preview = sess.get("preview") or ""
                    # 实时读取消息数
                    try:
                        m_mgr = SessionManager(session_id=sid, data_dir=DATA_DIR)
                        msg_count = len(m_mgr.get_messages())
                    except Exception:
                        msg_count = 0
                    
                    # 一行：加载按钮 + 操作按钮
                    cols = st.columns([4, 1, 1])
                    with cols[0]:
                        label = f"📄 {title} ({msg_count}条)"
                        help_text = f"{sid}\n最近: {preview}" if preview else sid
                        # 使用 on_click 回调避免双 rerun
                        st.button(label, key=f"load_{sid}", help=help_text,
                                  on_click=switch_session, args=(sid,))
                    with cols[1]:
                        # 重命名按钮
                        if st.button("✏️", key=f"rename_{sid}", help="修改标题"):
                            st.session_state[f"renaming_{sid}"] = True
                    with cols[2]:
                        # 删除按钮（当前会话不能删）
                        if sid != st.session_state.get("chat_session_id"):
                            if st.button("🗑️", key=f"del_{sid}", help="删除会话"):
                                try:
                                    SessionManager.delete_session(sid, data_dir=DATA_DIR)
                                    st.rerun()
                                except Exception as e:
                                    st.error(f"删除失败: {e}")
                    
                    # 重命名输入框
                    if st.session_state.get(f"renaming_{sid}", False):
                        new_title = st.text_input("新标题", value=title, key=f"title_input_{sid}")
                        c1, c2 = st.columns(2)
                        with c1:
                            if st.button("保存", key=f"save_{sid}"):
                                try:
                                    m_mgr = SessionManager(session_id=sid, data_dir=DATA_DIR)
                                    m_mgr.update_title(new_title)
                                    m_mgr.flush()
                                    del st.session_state[f"renaming_{sid}"]
                                    st.rerun()
                                except Exception as e:
                                    st.error(f"保存失败: {e}")
                        with c2:
                            if st.button("取消", key=f"cancel_{sid}"):
                                del st.session_state[f"renaming_{sid}"]
                                st.rerun()
        else:
            st.caption("暂无会话")
    except Exception as e:
        st.caption(f"加载失败: {e}")
    
    # 当前会话信息
    if st.session_state.chat_session_id:
        st.divider()
        st.caption(f"**当前会话**: `{st.session_state.chat_session_id}`")
        try:
            mgr = SessionManager(session_id=st.session_state.chat_session_id, data_dir=DATA_DIR)
            msgs = mgr.get_messages()
            st.caption(f"消息数: {len(msgs)}")
        except Exception:
            pass
    
    st.divider()
    
    if st.button("🗑️ 清空会话"):
        st.session_state.messages = []
        st.session_state.tool_logs = []
        st.session_state.token_stats = {"input": 0, "output": 0, "thinking": 0}
        st.session_state.current_thinking = ""
        if st.session_state.agent:
            st.session_state.agent.reset()
        st.rerun()


# ── 初始化 Agent ───────────────────────────────────────────────
def get_agent(session_id=None):
    """创建或更新 Agent 实例"""
    config = LLMConfig(
        provider=provider.lower(),
        model=model,
        api_key=api_key,
        temperature=temperature,
        max_tokens=max_tokens,
        stream=True,
        system_prompt=st.session_state.get("system_prompt", ""),  # P2 新增
    )
    router = LLMRouter(config)
    registry = ToolRegistry()
    register_builtin_tools(registry)
    # Day 4: 传入 session_id 实现历史持久化
    agent = ReactAgent(router, registry, max_turns=max_turns, session_id=session_id, session_data_dir=DATA_DIR)
    return agent


# ── 初始化/更新 Agent（配置变化时自动重建）───────────────────
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
        st.session_state.get("last_agent_config") != current_config or
        st.session_state.get("last_session_id") != st.session_state.chat_session_id):
    # 如果没有 session_id，尝试加载最近的会话
    sid = st.session_state.chat_session_id
    logging.warning(f"[DEBUG-AGENT-INIT] sid={sid}, agent is None={st.session_state.agent is None}")
    if not sid:
        try:
            sessions = SessionManager.list_sessions(data_dir=DATA_DIR)
            if sessions:
                sid = sessions[0]["session_id"]  # 用最新的会话
                st.session_state.chat_session_id = sid
                st.query_params["session"] = sid
        except Exception:
            pass
    # 关闭旧 Agent 的 session
    if st.session_state.agent is not None:
        try:
            st.session_state.agent.close()
        except Exception:
            pass

    st.session_state.agent = get_agent(sid)
    st.session_state.last_agent_config = current_config
    st.session_state.last_session_id = sid

agent = st.session_state.agent

# 从 session 加载聊天历史（session_id 变化时重新加载）
_loaded_sid = st.session_state.get("_loaded_session_id")
_current_sid = st.session_state.chat_session_id
logging.warning(f"[DEBUG-LOAD] loaded_sid={_loaded_sid}, current_sid={_current_sid}, messages={len(st.session_state.messages)}")
if _loaded_sid != _current_sid and _current_sid:
    # session 切换了，先清空旧消息
    st.session_state.messages = []
    try:
        mgr = SessionManager(session_id=st.session_state.chat_session_id, data_dir=DATA_DIR)
        history = mgr.get_messages()
        logging.warning(f"[DEBUG-LOAD] session={st.session_state.chat_session_id}, history={len(history)} entries")
        loaded_count = 0
        for entry in history:
            etype = entry.get("type", "")
            msg = entry.get("message")

            # 元数据类型不显示在 UI
            if etype in ("custom-title", "ai-title", "agent-name", "mode", "tag",
                         "compact_boundary", "summary"):
                continue

            # 从 message 字段取 API 原始消息（信封套信纸）
            if not msg:
                continue

            role = msg.get("role", "")
            content = msg.get("content", "")

            # 从 entry 顶层取 thinking 和 tool_logs
            thinking = entry.get("thinking", "") or ""
            tool_logs = entry.get("tool_logs", []) or []

            # 提取文本内容（content 可能是 str 或 list）
            def extract_text(c):
                if isinstance(c, str):
                    return c
                if isinstance(c, list):
                    # 从 content blocks 中提取 text
                    texts = []
                    for block in c:
                        if isinstance(block, dict) and block.get("type") == "text":
                            texts.append(block.get("text", ""))
                    return " ".join(texts)
                return str(c) if c else ""

            text_content = extract_text(content)

            # 只显示有文本内容的 user 和 assistant
            if role == "user" and text_content:
                st.session_state.messages.append({"role": "user", "content": text_content})
                loaded_count += 1
            elif role == "assistant" and text_content:
                st.session_state.messages.append({
                    "role": "assistant",
                    "content": text_content,
                    "thinking": thinking,
                    "tool_logs": tool_logs,
                })
                loaded_count += 1
        logging.warning(f"[DEBUG-LOAD] loaded {loaded_count} messages to session_state")
        st.session_state._loaded_session_id = _current_sid
    except Exception as e:
        logging.warning(f"加载会话历史失败: {e}")

agent = st.session_state.agent


# ── 运行 ReAct 循环 ──────────────────────────────────────────
def run_agent(user_input: str):
    """
    运行 ReAct Agent，yield 中间过程。
    返回：(msg_type, content)
    """
    for msg_type, content in agent.run(user_input):
        yield (msg_type, content)


# ── 调试信息（临时）─────────────────────────────────────────────
_debug_sid = st.session_state.chat_session_id
_debug_msgs = len(st.session_state.messages)
_debug_agent = st.session_state.agent is not None
st.caption(f"🐛 DEBUG: session={_debug_sid}, messages={_debug_msgs}, agent={_debug_agent}, query_params={dict(st.query_params)}")

# ── 主聊天界面 ────────────────────────────────────────────────

# 渲染历史消息
logging.warning(f"[DEBUG-RENDER] rendering {len(st.session_state.messages)} messages")
for msg in st.session_state.messages:
    with st.chat_message(msg["role"]):
        if msg.get("thinking"):
            with st.expander("💭 思考过程", expanded=False):
                st.code(msg["thinking"])
        if msg.get("tool_logs"):
            with st.expander("🔧 工具调用", expanded=False):
                for log in msg["tool_logs"]:
                    if isinstance(log, dict):
                        if log["type"] == "parallel_start":
                            st.markdown(f"\n⚡ **并行调用**: {', '.join(log['names'])}")
                        elif log["type"] == "action":
                            st.markdown(f"\n🔧 **Action**: `{log['name']}`")
                            st.caption(f"参数: `{log['input']}`")
                        elif log["type"] == "result":
                            icon = "✅" if log["success"] else "❌"
                            st.markdown(f"{icon} **Observation** (`{log['name']}`)")
                            output = str(log['output'])
                            if len(output) > 200:
                                with st.expander("查看完整结果"):
                                    st.code(output)
                            else:
                                st.code(output)
                    else:
                        # 兼容旧格式（纯字符串）
                        st.markdown(log)
        # 处理 content：如果是包含 tool_use 的 JSON 数组，提取 text 部分
        content = msg.get("content", "")
        if isinstance(content, list):
            # 从 content 数组中提取 text 类型的内容
            texts = [item["text"] for item in content if isinstance(item, dict) and item.get("type") == "text"]
            display_content = "".join(texts) if texts else str(content)
        else:
            display_content = content
        st.markdown(display_content)


# 用户输入
if prompt := st.chat_input("输入消息..."):
    if not api_key:
        st.error("请先在侧边栏填写 API Key（或配置 .env 文件）")
        st.stop()

    # 显示用户消息
    with st.chat_message("user"):
        st.markdown(prompt)
    st.session_state.messages.append({"role": "user", "content": prompt})

    # 自动生成标题（第一条用户消息）
    # 标题生成已由 SessionManager._on_user_message 自动处理
    # （即时占位 + 异步 AI 生成），不再在 app.py 手动调用 update_title

    # 调用 Agent（流式）
    with st.chat_message("assistant"):
        thinking_expander = st.expander("💭 思考过程", expanded=False)
        tool_expander = st.expander("🔧 工具调用", expanded=True)
        tool_status = st.status("🔄 思考中...", expanded=True)
        text_placeholder = st.empty()
        turn_indicator = st.empty()  # P2 新增：Turn 指示器
        
        full_text = ""
        thinking_text = ""
        tool_logs = []
        turn_count = 0  # P2 新增：Turn 计数

        # 逐 chunk 处理
        for msg_type, content in run_agent(prompt):
            if msg_type == "text":
                full_text += content
                text_placeholder.markdown(full_text + "▌")
            elif msg_type == "thinking":
                thinking_text += content
                # P2 改进：实时流式显示思考过程
                with thinking_expander:
                    thinking_expander.markdown(f"**💭 思考过程**\n\n{thinking_text}▌")
            elif msg_type == "tool_call":
                # Day 3 支持并行工具调用
                if content.get("parallel"):
                    names = content.get("names", [])
                    tool_status.update(label=f"⚡ 并行执行 {len(names)} 个工具: {', '.join(names)}...")
                    tool_logs.append({"type": "parallel_start", "names": names})
                else:
                    tool_name = content.get("name", "")
                    tool_input = content.get("input", {})
                    tool_status.update(label=f"🔧 执行工具: {tool_name}...")
                    tool_logs.append({"type": "action", "name": tool_name, "input": tool_input})
            elif msg_type == "tool_result":
                tool_name = content.get("name", "")
                tool_output = content.get("output", "")
                success = content.get("success", False)
                elapsed = content.get("elapsed", 0)
                
                if success:
                    elapsed_str = f" ({elapsed:.2f}s)" if elapsed else ""
                    tool_status.update(label=f"✅ {tool_name} 完成{elapsed_str}", state="complete")
                    tool_logs.append({"type": "result", "name": tool_name, "output": tool_output, "success": True, "elapsed": elapsed})
                else:
                    tool_status.update(label=f"❌ {tool_name} 失败", state="error")
                    tool_logs.append({"type": "result", "name": tool_name, "output": tool_output, "success": False})
            elif msg_type == "system":
                # P2 改进：区分 Turn 信息和最终完成
                if "Turn" in str(content):
                    turn_count += 1
                    turn_indicator.markdown(f"📍 **Turn {turn_count}**")
                    tool_status.update(label=f"🔄 Turn {turn_count}：思考中...")
                elif "回答完成" in str(content):
                    turn_indicator.empty()  # 完成后清除 Turn 指示器
                    tool_status.update(label="✅ 回答完成", state="complete")
                else:
                    st.info(content)
            elif msg_type == "usage":
                stats = st.session_state.token_stats
                stats["input"] += content.input_tokens
                stats["output"] += content.output_tokens
                stats["thinking"] += content.thinking_tokens

        # 流式结束：清理 UI
        text_placeholder.markdown(full_text)
        thinking_expander.empty()  # 清除流式思考过程

        # P2 改进：结构化显示思考过程
        with thinking_expander:
            if thinking_text:
                st.markdown("**💭 LLM 思考过程**")
                st.code(thinking_text)
        
        # P2 改进：结构化显示工具调用
        with tool_expander:
            if tool_logs:
                st.markdown("**🔧 工具调用时间线**")
                for log in tool_logs:
                    if log["type"] == "parallel_start":
                        st.markdown(f"\n⚡ **并行调用**: {', '.join(log['names'])}")
                    elif log["type"] == "action":
                        st.markdown(f"\n🔧 **Action**: `{log['name']}`")
                        st.caption(f"参数: `{log['input']}`")
                    elif log["type"] == "result":
                        icon = "✅" if log["success"] else "❌"
                        elapsed_str = f" ({log.get('elapsed', 0):.2f}s)" if log.get('elapsed') else ""
                        st.markdown(f"{icon} **Observation** (`{log['name']}`{elapsed_str})")
                        output = str(log['output'])
                        # 长结果折叠显示
                        if len(output) > 200:
                            with st.expander("查看完整结果"):
                                st.code(output)
                        else:
                            st.code(output)
            else:
                st.caption("本轮无工具调用（直接回答）")

        # Token 消耗摘要
        stats = st.session_state.token_stats
        st.caption(
            f"📊 Token: input={stats['input']:,} · "
            f"output={stats['output']:,} · "
            f"thinking={stats['thinking']:,} · "
            f"total={stats['input'] + stats['output'] + stats['thinking']:,}"
        )

    # 保存助手消息（兼容新旧格式）
    # 历史消息中 tool_logs 保持 dict 格式，渲染时按结构化显示
    st.session_state.messages.append({
        "role": "assistant",
        "content": full_text,
        "thinking": thinking_text,
        "tool_logs": tool_logs,
    })

    # 更新 agent history
    # （agent.run() 内部已经更新了 agent.history）
