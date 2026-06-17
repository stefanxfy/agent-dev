"""
Streamlit Web UI — ReAct Agent + 工具调用可视化
Day 3 版本：并行工具调用、Token 预算管理、System Prompt、结构化 UI
"""

from __future__ import annotations

import json
import logging
import os
import sys
import uuid
from pathlib import Path

# ── 配置日志（在加载任何模块之前）──────────────────────────────
# 用法: python3 -m streamlit run web/app.py -- --log-level=DEBUG
#       python3 -m streamlit run web/app.py -- --log-level debug
#       不传默认 INFO
_argv = sys.argv[1:]
_log_level = logging.INFO
for i, arg in enumerate(_argv):
    _val = None
    if arg.startswith("--log-level="):
        _val = arg.split("=", 1)[1]
    elif arg == "--log-level" and i + 1 < len(_argv):
        _val = _argv[i + 1]
    if _val:
        _log_level = getattr(logging, _val.upper(), logging.INFO)
        break

logging.basicConfig(
    level=_log_level,
    format='[%(asctime)s] [%(levelname)s] %(name)s: %(message)s',
    datefmt='%H:%M:%S',
)
# 安静第三方库（DEBUG 模式下避免刷屏）
if _log_level <= logging.DEBUG:
    for _noisy in ("httpx", "httpcore", "urllib3", "openai", "anthropic",
                   "watchdog", "git"):
        logging.getLogger(_noisy).setLevel(logging.WARNING)

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
from agent_core.session.manager import SessionManager
from agent_core.session.storage import SessionStorage
from agent_core.tools.base import ToolRegistry
from agent_core.tools.builtin import register_builtin_tools


# ── Session State 初始化 ───────────────────────────────────────
if "messages" not in st.session_state:
    st.session_state.messages = []
if "agent" not in st.session_state:
    st.session_state.agent = None
if "token_stats" not in st.session_state:
    st.session_state.token_stats = {"input": 0, "output": 0, "thinking": 0, "cached": 0}
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
    if url_sid:
        st.session_state.chat_session_id = url_sid
    else:
        st.session_state.chat_session_id = None


# ── 侧边栏：LLM 配置 ─────────────────────────────────────────
with st.sidebar:
    # ── Token 消耗面板（永久显示） ───────────────────────────────
    stats = st.session_state.token_stats
    total = stats["input"] + stats["output"] + stats["thinking"]
    st.subheader("📊 Token 消耗")
    if total > 0:
        st.metric(
            label="累计",
            value=f"{total:,}",
            delta=f"in {stats['input']:,} · out {stats['output']:,}",
        )
        if stats["thinking"]:
            st.caption(f"💭 thinking: {stats['thinking']:,}")
        # P3 cache 命中累计
        cached_total = stats.get("cached", 0)
        if cached_total:
            hit_rate = cached_total / max(stats["input"], 1) * 100
            st.caption(f"🔥 cache: {cached_total:,} ({hit_rate:.1f}%)")
    else:
        st.caption("📝 发送消息后显示")

    st.divider()
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

    # ── 上下文预算面板 ────────────────────────────────────────
    st.subheader("📐 上下文预算")
    if st.session_state.agent:
        agent = st.session_state.agent
        cm = agent.context_manager
        usage = cm.get_usage_info(agent.messages)

        # 预算进度条
        ratio = usage["usage_ratio"]
        used = usage["used_tokens"]
        total = usage["total_budget"]
        available = usage["available_tokens"]

        # 颜色判断（四档：正常/警告/压缩/临界）
        if usage["is_critical"]:
            bar_color = "🔴"
            status_text = "临界"
        elif usage["should_compact"]:
            bar_color = "🟡"
            status_text = "需压缩"
        elif usage.get("is_warning"):
            bar_color = "🟠"
            status_text = "警告"
        else:
            bar_color = "🟢"
            status_text = "正常"

        st.metric(
            "上下文使用",
            f"{used:,} / {total:,}",
            delta=f"{available:,} 可用 ({ratio:.0%}) {bar_color} 阈值={usage.get('compact_threshold', 'N/A'):,}",
        )

        # 进度条
        progress_bar = st.progress(min(ratio, 1.0),
                                   text=f"{bar_color} {status_text} · {ratio:.0%} 已用")

        # 熔断状态
        fails = usage.get("consecutive_failures", 0)
        if fails > 0:
            st.warning(f"⚠️ 连续压缩失败 {fails} 次")

        # 压缩统计
        stats_cm = cm.get_stats()
        if stats_cm["compact_count"] > 0:
            st.caption(
                f"📦 已压缩 {stats_cm['compact_count']} 次 · "
                f"释放 {stats_cm['total_tokens_freed']:,} tokens"
            )

        # 消息数
        st.metric("History 长度", f"{len(agent.messages)} 条")
    else:
        st.caption("Agent 未初始化")

    # 历史记录查看器（包含压缩前的旧消息，按 boundary 分段 + 滚动列表）
    _view_sid = st.session_state.get("chat_session_id")
    if _view_sid:
        try:
            _jsonl_path = Path(DATA_DIR) / f"{_view_sid}.jsonl"
            # 全部消息（含 boundary 之前的旧消息），上限 500
            _all_msgs = SessionStorage.read_messages_lightweight(_jsonl_path, limit=500, include_all_types=True)
            if _all_msgs:
                st.divider()
                _total = len(_all_msgs)
                st.subheader(f"📜 历史记录 ({_total} 条)")

                # 检测 boundary 位置（按完整列表算）
                _boundary_idx = -1
                for _idx, _e in enumerate(_all_msgs):
                    if _e.get("type") == "system" and _e.get("subtype") == "compact_boundary":
                        _boundary_idx = _idx
                        break

                # 工具函数：取内容预览
                def _content_preview(entry):
                    msg = entry.get("message", {})
                    content = msg.get("content", "")
                    if isinstance(content, str):
                        return content[:50]
                    return f"[{len(content)} blocks]"

                # 滚动容器：高度 400px（与会话列表一致），可上下滑动
                with st.container(height=400):
                    for i, entry in enumerate(_all_msgs):
                        entry_type = entry.get("type", "")
                        msg = entry.get("message", {}) or {}

                        # 识别 entry 类型
                        if entry_type == "system" and entry.get("subtype") == "compact_boundary":
                            role = "🚧 压缩边界"
                            content_preview = entry.get("compactMetadata", {})
                            expander_title = f"⏸️ 压缩边界 #{i+1}"
                            show_raw_json = False
                            show_metadata = True
                        elif entry_type == "user" and msg.get("isCompactSummary"):
                            role = "📝 压缩摘要"
                            content_preview = msg.get("content", "")[:50]
                            expander_title = f"📝 压缩摘要（{len(msg.get('content', ''))} 字符）"
                            show_raw_json = True
                            show_metadata = False
                        else:
                            role = msg.get("role", "unknown")
                            content_preview = (
                                msg.get("content", "")[:50]
                                if isinstance(msg.get("content", ""), str)
                                else f"[{len(msg.get('content', []))} blocks]"
                            )
                            expander_title = f"{i+1}. {role}: {content_preview}..."
                            show_raw_json = True
                            show_metadata = False

                        # 在 boundary 处显示「压缩前/后」标签
                        if i == _boundary_idx:
                            st.caption("⏸️ --- 以下是压缩前的旧消息 ---")

                        with st.expander(expander_title):
                            if show_metadata:
                                # boundary 显示 compactMetadata
                                st.json(entry.get("compactMetadata", {}))
                            if show_raw_json:
                                st.json(msg)

                        if _boundary_idx >= 0 and i == _boundary_idx:
                            st.caption("⏸️ --- 压缩后新对话开始 ---")
        except Exception as e:
            logging.warning(f"侧边栏历史记录加载失败: {e}")

    # ── 会话管理 ────────────────────────────────────────────────
    st.divider()
    st.subheader("💾 会话管理")
    
    # DATA_DIR 已移到文件顶部定义
    
    # 新建会话
    if st.button("➕ 新建会话", key="new_session"):
        new_id = str(uuid.uuid4())[:8]
        try:
            mgr = SessionManager(session_id=new_id, data_dir=DATA_DIR)
            mgr.flush()
        except Exception as e:
            logging.warning(f"新建会话写入失败: {e}")
        if st.session_state.agent is not None:
            try:
                st.session_state.agent.close()
            except Exception as e:
                logging.warning(f"关闭旧 Agent 失败: {e}")
        st.session_state.chat_session_id = new_id
        st.session_state.agent = None
        st.rerun()

    # 加载已有会话
    try:
        sessions = SessionManager.list_sessions(data_dir=DATA_DIR)
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
                    # 实时读取消息数（轻量静态方法，不创建 SessionManager）
                    try:
                        _path = Path(DATA_DIR) / f"{sid}.jsonl"
                        msg_count = SessionStorage.count_messages(_path)
                    except Exception as e:
                        logging.warning(f"会话消息数读取失败 (sid={sid}): {e}")
                        msg_count = 0

                    # P3 cache 累计统计（侧车文件）
                    try:
                        cache_stats = sess.get("cache_stats", {})
                        cache_hit = cache_stats.get("hit_rate", 0.0)
                        cache_calls = cache_stats.get("total_calls", 0)
                    except Exception as e:
                        logging.warning(f"cache stats 读取失败 (sid={sid}): {e}")
                        cache_hit, cache_calls = 0.0, 0

                    # 一行：加载按钮 + 操作按钮
                    cols = st.columns([4, 1, 1])
                    with cols[0]:
                        cache_label = ""
                        if cache_calls > 0 and cache_hit > 0:
                            cache_label = f" 🔥{cache_hit*100:.0f}%"
                        label = f"📄 {title} ({msg_count}条){cache_label}"
                        # hover 上同时显示 cache 明细
                        hit_detail = (
                            f"\n\n🔥 Cache 命中率: {cache_hit*100:.1f}%"
                            f"\n   调用 {cache_calls} 次"
                            f"\n   命中 {cache_stats.get('cached_tokens', 0):,} / "
                            f"{cache_stats.get('input_tokens', 0):,} tokens"
                            if cache_calls > 0 else ""
                        )
                        help_text = (
                            f"{sid}\n最近: {preview}{hit_detail}"
                            if preview else f"{sid}{hit_detail}"
                        )
                        if st.button(label, key=f"load_{sid}", help=help_text):
                            if sid != st.session_state.get("chat_session_id"):
                                if st.session_state.agent is not None:
                                    try:
                                        st.session_state.agent.close()
                                    except Exception as e:
                                        logging.warning(f"切换会话时关闭旧 Agent 失败: {e}")
                                st.session_state.chat_session_id = sid
                                st.session_state.agent = None
                                st.rerun()
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
            _path = Path(DATA_DIR) / f"{st.session_state.chat_session_id}.jsonl"
            msg_count = SessionStorage.count_messages(_path)
            st.caption(f"消息数: {msg_count}")
        except Exception as e:
            logging.warning(f"当前会话消息数读取失败: {e}")
    
    st.divider()
    
    if st.button("🗑️ 清空会话"):
        st.session_state.messages = []
        st.session_state.tool_logs = []
        st.session_state.token_stats = {"input": 0, "output": 0, "thinking": 0, "cached": 0}
        st.session_state.current_thinking = ""
        if st.session_state.agent:
            st.session_state.agent.reset()
        st.rerun()


# ── 辅助函数 ─────────────────────────────────────────────────
def extract_text(content):
    """从消息 content 中提取纯文本。content 可能是 str 或 content blocks list。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        texts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                texts.append(block.get("text", ""))
        return " ".join(texts)
    return str(content) if content else ""


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

    if not sid:
        try:
            sessions = SessionManager.list_sessions(data_dir=DATA_DIR)
            if sessions:
                sid = sessions[0]["session_id"]  # 用最新的会话
                st.session_state.chat_session_id = sid
                st.query_params["session"] = sid
        except Exception as e:
            logging.warning(f"自动加载最近会话失败: {e}")
    # 关闭旧 Agent 的 session
    if st.session_state.agent is not None:
        try:
            st.session_state.agent.close()
        except Exception as e:
            logging.warning(f"Agent 重建时关闭旧 Agent 失败: {e}")

    st.session_state.agent = get_agent(sid)
    st.session_state.last_agent_config = current_config
    st.session_state.last_session_id = sid

agent = st.session_state.agent

# 从 session 加载聊天历史（session_id 变化时重新加载）
_loaded_sid = st.session_state.get("_loaded_session_id")
_current_sid = st.session_state.chat_session_id
if _loaded_sid != _current_sid and _current_sid:
    # session 切换了，先清空旧消息
    st.session_state.messages = []
    try:
        # 直接用 SessionStorage 读取，不创建 SessionManager（避免 _restore_title_state 副作用）
        _storage = SessionStorage(session_id=st.session_state.chat_session_id, data_dir=DATA_DIR)
        history = _storage.get_messages()

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


# ── 主聊天界面 ────────────────────────────────────────────────

# 渲染历史消息
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

        # 显示该条消息的 Token 消耗（如果有）
        msg_usage = msg.get("usage")
        if msg_usage and (msg_usage.get("input") or msg_usage.get("output")):
            parts = [f"input={msg_usage['input']:,}", f"output={msg_usage['output']:,}"]
            if msg_usage.get("thinking"):
                parts.append(f"thinking={msg_usage['thinking']:,}")
            cached = msg_usage.get("cached", 0)
            if cached:
                hit_rate = cached / max(msg_usage["input"], 1) * 100
                parts.append(f"🔥cache={cached:,} ({hit_rate:.1f}%)")
            st.caption(f"📊 {' · '.join(parts)}")


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
        last_turn_usage = None  # 每轮 LLM 响应的 usage（用于显示单轮 token 消耗）

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
                elif "上下文已压缩" in str(content):
                    # Day 5: 压缩通知
                    st.info(f"{content}")
                elif "强制结束" in str(content) or "工具执行失败" in str(content):
                    st.warning(content)
                else:
                    st.info(content)
            elif msg_type == "usage":
                stats = st.session_state.token_stats
                stats["input"] += content.input_tokens
                stats["output"] += content.output_tokens
                stats["thinking"] += content.thinking_tokens
                stats["cached"] = stats.get("cached", 0) + (content.cached_tokens or 0)
                last_turn_usage = content  # 记录本轮最新 usage，用于显示单轮消耗

                # P3 累计缓存命中写入 sidecar（供 sidebar 会话列表显示）
                try:
                    _cache_sid = st.session_state.get("chat_session_id")
                    if _cache_sid:
                        SessionStorage.write_cache_stats(
                            Path(DATA_DIR) / f"{_cache_sid}.jsonl",
                            content,
                        )
                except Exception as _ce:
                    logging.warning(f"cache stats 写入失败: {_ce}")

                # Debug 日志：缓存命中明细
                hit_rate = (content.cached_tokens or 0) / max(content.input_tokens, 1) * 100
                logging.debug(
                    f"🔥 [Cache] in={content.input_tokens:,} · "
                    f"cached={content.cached_tokens:,} · "
                    f"hit={hit_rate:.1f}%"
                )

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
        # 本轮 LLM 响应的 token 消耗（per-turn）
        if last_turn_usage:
            turn_input = last_turn_usage.input_tokens
            turn_output = last_turn_usage.output_tokens
            turn_thinking = last_turn_usage.thinking_tokens
            turn_total = turn_input + turn_output + turn_thinking
            st.caption(
                f"📊 本轮: input={turn_input:,} · output={turn_output:,}"
                + (f" · thinking={turn_thinking:,}" if turn_thinking else "")
                + f" · 累计: input={stats['input']:,} · output={stats['output']:,} · thinking={stats['thinking']:,}"
            )
        else:
            st.caption(
                f"📊 累计: input={stats['input']:,} · "
                f"output={stats['output']:,} · "
                f"thinking={stats['thinking']:,}"
            )

    # 保存助手消息（兼容新旧格式）
    # 历史消息中 tool_logs 保持 dict 格式，渲染时按结构化显示
    st.session_state.messages.append({
        "role": "assistant",
        "content": full_text,
        "thinking": thinking_text,
        "tool_logs": tool_logs,
        "usage": {
            "input": last_turn_usage.input_tokens if last_turn_usage else 0,
            "output": last_turn_usage.output_tokens if last_turn_usage else 0,
            "thinking": last_turn_usage.thinking_tokens if last_turn_usage else 0,
            "cached": last_turn_usage.cached_tokens if last_turn_usage else 0,
        } if last_turn_usage else None,
    })

    # 更新 agent history
    # （agent.run() 内部已经更新了 agent.messages）

    # 触发 sidebar 刷新：让上下文预算面板、History 长度等 widget
    # 重新从 agent.messages 读取最新值（Streamlit 渲染顺序：sidebar 先、
    # 主区后，sidebar 在 run() 启动时取的是旧值，run() 完后需要 rerun 一次
    # 才能让 sidebar 看到新 history）。
    st.rerun()
