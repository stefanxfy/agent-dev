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

# ── 解析 --log-level（在加载任何模块之前）──────────────────────
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

# ── 项目根目录加入 sys.path（须在 import agent_core 之前）────────
PROJECT_ROOT = Path(__file__).parent.parent.resolve()  # 使用绝对路径
sys.path.insert(0, str(PROJECT_ROOT))

# ── 配置日志：控制台 + 按小时切分的本地文件 logs/app/agent.log ──
from agent_core.logging_setup import setup_logging
setup_logging(level=_log_level, log_dir=PROJECT_ROOT / "logs" / "app")

# ── 加载 .env 文件 ────────────────────────────────────────────
from dotenv import load_dotenv
load_dotenv()

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
from agent_core.config import config as _agent_config  # 解析 AGENT_DATA_DIR
from agent_core.memory.config import MemoryConfig  # M10 C7.1: 共享实例,line 648 + 719
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
# M7 ported: 记忆系统状态
if "memory_stats" not in st.session_state:
    st.session_state.memory_stats = {
        "total_searches": 0,
        "total_hits": 0,
        "last_zero_hit_turn": None,
        "current_turn_hits": 0,
        "stored_total": 0,
    }
if "memory_enabled" not in st.session_state:
    st.session_state.memory_enabled = True  # 默认启用记忆检索(2026-06-24 改)
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
    # M10 C6.5: 降级 banner(任一 MemoryEventKind: BUDGET/TIMEOUT/LOCK/RATE/EXTRACT_ERROR 时显示)
    ms = st.session_state.get("memory_stats", {})
    last_err = ms.get("last_extract_error")
    if last_err:
        st.error(f"⚠️ 记忆系统降级中: {last_err}")
        if st.button("🔄 重置", key="reset_degraded_state"):
            ms["last_extract_error"] = None
            st.session_state.memory_stats = ms
            st.rerun()

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

    # M7 ported: 🧠 Memory 状态(折叠面板,默认折叠)
    ms = st.session_state.memory_stats
    with st.expander("🧠 Memory 状态", expanded=False):
        st.caption(f"{'✅ 启用' if st.session_state.memory_enabled else '⚪ 未启用'}")
        new_mem_enabled = st.toggle(
            "启用记忆检索",
            value=st.session_state.memory_enabled,
            help="开启后,每条消息会检索相关记忆注入 system prompt",
            key="memory_enabled_toggle",
        )
        if new_mem_enabled != st.session_state.memory_enabled:
            st.session_state.memory_enabled = new_mem_enabled
            st.session_state.agent = None  # 强制重建 agent
            st.rerun()
        st.metric("Searches", ms["total_searches"])
        st.metric("Total Hits", ms["total_hits"])
        st.metric("Last Turn Hits", ms["current_turn_hits"])
        if ms["stored_total"]:
            st.metric("Stored (N)", f"{ms['stored_total']}")
        if ms["last_zero_hit_turn"]:
            gap = ms["total_searches"] - ms["last_zero_hit_turn"]
            st.metric("Last 0-hit", f"{gap} turns ago")

    st.divider()

    # M10 C3.2: 🌙 Auto-dream 蒸馏状态(折叠面板,默认折叠)
    with st.expander("🌙 Auto-dream", expanded=False):
        # 关键:用 st.session_state.get("agent") 而不是 get_agent()
        # sidebar rerun 频繁,不能每次重建 agent
        agent = st.session_state.get("agent")
        if agent is None:
            st.caption("(agent 未初始化)")
        else:
            loop = getattr(agent, "_distillation_loop", None)
            if loop is None:
                st.caption("(未启动)")
            else:
                status = loop.get_status()
                st.metric("状态", "🟢 跑" if status["running"] else "🔴 停")
                st.metric("Tick 数", status["tick_count"])
                st.metric("间隔(s)", status["interval_seconds"])
                if status["last_tick_at"]:
                    st.caption(f"上次 tick: {status['last_tick_at']}")
                if status["last_result_success"] is not None:
                    succ = "✓" if status["last_result_success"] else "✗"
                    st.caption(f"上次结果: {succ} ({status['last_candidates_count']} 候选)")
                else:
                    st.caption("上次 tick: gate 拦住")

    # M10 C4.1: 📥 待审候选提醒(折叠面板,默认折叠)
    # mem_root 与 get_agent() 同口径(config.agent_data_dir or ~/.agent_data)
    # session_state 不存 mem_root, 这里从 config 现算(sidebar rerun 频繁, 纯读)
    with st.expander("📥 待审记忆", expanded=False):
        try:
            from agent_core.config import config as _agent_config_c4
            _cand_data_dir = _agent_config_c4.agent_data_dir or str(Path.home() / ".agent_data")
            _mem_root_for_cand = Path(_cand_data_dir) / "memory"
        except Exception:
            _mem_root_for_cand = Path.home() / ".agent_data" / "memory"

        if not _mem_root_for_cand.exists():
            st.caption("(memory 目录不存在)")
        else:
            from agent_core.memory.candidate_actions import list_candidates as _list_cands
            pending = _list_cands(_mem_root_for_cand)
            st.caption(f"{len(pending)} 条待审")
            for _p in pending[:5]:
                st.caption(f"· {_p.name}")
            if len(pending) > 5:
                st.caption(f"... 共 {len(pending)} 条")
            try:
                st.page_link("pages/02_Candidate_Review.py", label="查看全部 →")
            except Exception:
                # 老 Streamlit 无 page_link → link_button 兜底
                st.link_button("查看全部 →", "/Candidate_Review")

    # M10 C6.4: Runtime Config(运行时切换不重建 agent)
    with st.expander("🎛 Runtime Config", expanded=False):
        _rt_agent = st.session_state.get("agent")
        if not _rt_agent:
            st.caption("⚠️ Agent 未就绪")
        elif not getattr(_rt_agent, "react_memory_bridge", None):
            st.caption("⚠️ 记忆系统未启用")
        else:
            # memory_config / _memory_config 两种属性名都试(brief Step D 验证:实际都不存在)
            _rt_config = getattr(_rt_agent, "memory_config", None) \
                or getattr(_rt_agent, "_memory_config", None)
            if _rt_config is None:
                st.caption("⚠️ 找不到 memory_config 属性")
            else:
                _current_budget = _rt_config.cost.daily_budget_usd
                _new_budget = st.number_input(
                    "Daily budget (USD)",
                    min_value=0.0,
                    max_value=100.0,
                    value=float(_current_budget),
                    step=0.1,
                    key="runtime_daily_budget",
                )
                if st.button("Apply", key="apply_runtime_budget"):
                    # 改 config(运行时,不会触发 agent 重建)
                    _rt_config.set_runtime("cost.daily_budget_usd", _new_budget)
                    # 替换 gate 的 cost_tracker 实例
                    from agent_core.memory.cost_tracker import CostTracker as _CT
                    _rt_gate = _rt_agent.react_memory_bridge.gate
                    _rt_gate.set_cost_tracker(_CT(
                        daily_budget_usd=_new_budget,
                        enabled=_rt_config.cost.enabled,
                    ))
                    st.success(f"✅ Budget 改为 ${_new_budget:.2f}")
                    st.rerun()

                # M11: 检索模式 (semantic / side_query) + sideQuery 选 N
                _mode = _rt_config.retrieval.mode
                _new_mode = st.selectbox(
                    "检索模式 (M11)",
                    options=["semantic", "side_query"],
                    index=0 if _mode == "semantic" else 1,
                    key="runtime_retrieval_mode",
                    help="semantic = 向量召回;side_query = MEMORY.md manifest 让 LLM 二次精选",
                )
                _new_max_select = st.slider(
                    "sideQuery max select",
                    min_value=1, max_value=10,
                    value=int(_rt_config.retrieval.side_query_max_select),
                    step=1, key="runtime_side_query_max_select",
                    help="sideQuery 一次 LLM 选 N 个 path",
                )
                if st.button("Apply (M11)", key="apply_runtime_m11"):
                    _rt_config.set_runtime("retrieval.mode", _new_mode)
                    _rt_config.set_runtime(
                        "retrieval.side_query_max_select", int(_new_max_select)
                    )
                    st.success(
                        f"✅ 检索模式 → {_new_mode}, max_select={_new_max_select}"
                    )
                    st.rerun()

    st.divider()
    st.header("⚙️ LLM 配置")

    # provider/model/env_key 列表由 LLMProvider enum 派生,加新厂商无需改 UI
    from web.llm_options import (
        get_default_provider_index,
        get_env_key_for_provider,
        get_models_for_provider,
        get_provider_options,
    )

    provider = st.selectbox(
        "厂商",
        options=get_provider_options(),
        index=get_default_provider_index(),
    )

    model = st.selectbox("模型", options=get_models_for_provider(provider))

    # API Key
    env_key_var = get_env_key_for_provider(provider)
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

                    # 一行：加载按钮 + 操作按钮
                    cols = st.columns([4, 1, 1])
                    with cols[0]:
                        label = f"📄 {title} ({msg_count}条)"
                        help_text = (
                            f"{sid}\n最近: {preview}"
                            if preview else f"{sid}"
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
        st.session_state.memory_stats = {
            "total_searches": 0,
            "total_hits": 0,
            "last_zero_hit_turn": None,
            "current_turn_hits": 0,
            "stored_total": 0,
        }
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
    # M10 C6.1: 启用 OTel tracer(读 env OTEL_EXPORTER_OTLP_ENDPOINT,
    # 没设则 NoOp,不破坏 dev 体验)。tracing 失败不阻断 agent。
    try:
        from agent_core.memory.tracing import configure_tracing
        if configure_tracing():
            logging.info("OTel tracer 启用")
    except Exception as e:
        logging.warning(f"configure_tracing 失败: {e}")

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

    # M7 ported: 记忆系统 hook(若启用,注入 retriever + store)
    memory_retriever = None
    memory_store = None
    memory_embed_fn = None
    # Task 8: 严格双通道(react_memory_bridge)在 memory_enabled=True 且
    # 上游 memory_store/vec_store/embed_fn 全部初始化成功后才构造。
    react_memory_bridge = None
    # M10 C7.1: 单一 MemoryConfig 实例必须 hoist 到 memory_enabled 块之前
    # 因为 ReactAgent(memory_config=...) 在 if 块外(line 719)使用
    memory_config = MemoryConfig()
    if st.session_state.get("memory_enabled", False):
        try:
            from web.memory_wiring import build_memory_system
            agent_data_dir = _agent_config.agent_data_dir or str(Path.home() / ".agent_data")
            mem_root = Path(agent_data_dir) / "memory"
            chroma_path = Path(agent_data_dir) / "chroma"
            # M11 修复(2026-06-26):build_memory_system 内部已 hoist MemoryIndex
            # 并传给 DualChannelWriter,确保写盘后 mark_dirty 触发异步 rebuild
            bundle = build_memory_system(
                memory_root=mem_root,
                chroma_path=chroma_path,
                llm_config=config,
                memory_config=memory_config,
                session_id=session_id or "default",
            )
            if bundle is None:
                raise RuntimeError("build_memory_system 返回 None,见上方 warning")
            (
                memory_store,
                vec_store,
                memory_embed_fn,
                memory_retriever,
                dual_channel,
                react_memory_bridge,
                _memory_index,
            ) = bundle
        except Exception as e:
            logging.warning(f"Memory system init failed: {e}")
            memory_retriever = None
            memory_store = None
            memory_embed_fn = None
            react_memory_bridge = None

    # Day 4: 传入 session_id 实现历史持久化
    # M10 C6.4: 传入 MemoryConfig,UI expander 才能 set_runtime in-place 改字段
    agent = ReactAgent(
        router, registry,
        max_turns=max_turns,
        session_id=session_id,
        session_data_dir=DATA_DIR,
        memory_retriever=memory_retriever,    # M7 ported: 检索 + 注入
        memory_store=memory_store,             # M7 ported: 库内计数
        react_memory_bridge=react_memory_bridge,  # Task 8: 严格双通道(同步→异步)
        memory_config=memory_config,           # M10 C6.4 + C7.1: 共享 MemoryConfig 实例
    )

    # M10 C3.1: 启动 DistillationLoop(后台 daemon,10 分钟检查 4 重门)
    # 用 st.session_state 单例化(避免 Streamlit rerun 每次 leak 一个 daemon 线程)
    try:
        from agent_core.memory.distiller import DistillationScheduler
        from agent_core.memory.scheduler import DistillationLoop

        existing_loop = st.session_state.get("distillation_loop")
        if existing_loop is None or not getattr(existing_loop, "is_running", False):
            # mem_root 来自 memory_enabled 分支或 fallback 构造
            if "mem_root" not in dir():
                from agent_core.config import config as _agent_config_fb
                _agent_data_dir = _agent_config_fb.agent_data_dir or str(Path.home() / ".agent_data")
                mem_root = Path(_agent_data_dir) / "memory"
            mem_root.mkdir(parents=True, exist_ok=True)
            distill_scheduler = DistillationScheduler(
                memory_root=mem_root,
                llm_callback=None,  # 默认 stub;真 LLM 由 scheduler 内部注入
            )
            distill_loop = DistillationLoop(
                scheduler=distill_scheduler,
                on_result=lambda r: logging.debug(f"DistillationLoop tick: {r}"),
            )
            distill_loop.start(interval_seconds=600)  # 10 min per spec §7
            st.session_state.distillation_loop = distill_loop
            logging.info("DistillationLoop 启动: interval=600s")
    except Exception as e:
        logging.warning(f"DistillationLoop 启动失败: {e}")
        # 不阻塞 agent 返回 — loop 失败不应影响 chat

    # 把 loop 挂到 agent(每次 rerun 都重挂,但 loop 实例不变)
    agent._distillation_loop = st.session_state.get("distillation_loop")

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
        # P5 修复：include_compact_summary=False 跳过压缩摘要 user message
        # 原 bug：add_summary 写入的 type="user" + message.isCompactSummary=True
        # 主聊天区 UI 加载时，etype in (..., "summary") 判断永不生效
        # （type 是 "user"），导致 1370 字符的摘要被当成普通用户消息加载
        # 解决：用 storage 内置 API 在加载阶段就过滤掉
        history = _storage.get_messages(include_compact_summary=False)

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
                # Day 7：从 entry 顶层取 usage（API 真实 token 统计）
                # 加载后 sidebar 会话信息面板能直接用这个数字，不用重算
                entry_usage = entry.get("usage")
                st.session_state.messages.append({
                    "role": "assistant",
                    "content": text_content,
                    "thinking": thinking,
                    "tool_logs": tool_logs,
                    "usage": entry_usage,  # 可能为 None（老 session）
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
        if msg_usage and (msg_usage.get("input_tokens") or msg_usage.get("output_tokens")):
            parts = [f"input={msg_usage['input_tokens']:,}", f"output={msg_usage['output_tokens']:,}"]
            if msg_usage.get("thinking_tokens"):
                parts.append(f"thinking={msg_usage['thinking_tokens']:,}")
            cached = msg_usage.get("cached_tokens", 0)
            if cached:
                hit_rate = cached / max(msg_usage["input_tokens"], 1) * 100
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
        # P4 视觉优化：思考用 st.status（自带 spinner + 自动折叠 + 绿勾）
        # - 思考中：状态 running + 展开，用户能看到流式思考
        # - 完成后：状态 complete + 自动折叠，不抢占主区
        thinking_status = st.status(
            "💭 思考中...",
            state="running",
            expanded=True,
        )
        thinking_placeholder = thinking_status.empty()
        tool_expander = st.expander("🔧 工具调用", expanded=True)
        tool_status = st.status("🔄 思考中...", expanded=True)
        text_placeholder = st.empty()
        turn_indicator = st.empty()  # P2 新增：Turn 指示器

        full_text = ""
        # P5 优化：按 turn 分段累积思考（多轮场景下方便区分 Turn 1/2/3）
        # turn_thinking[turn_num] = 该轮的 thinking 文本
        turn_thinking: dict[int, str] = {}
        current_turn: int = 1  # 默认 turn 1，收到 "Turn N" 事件时更新
        tool_logs = []
        turn_count = 0  # P2 新增：Turn 计数
        last_turn_usage = None  # 每轮 LLM 响应的 usage（用于显示单轮 token 消耗）

        def render_thinking_with_turns(cursor: str = ""):
            """渲染思考区（带 Turn 标签分隔多轮）

            Args:
                cursor: 流式光标（流式期间为 "▌"，完成后为空）
            """
            if not turn_thinking:
                return
            parts = []
            for tnum in sorted(turn_thinking.keys()):
                text = turn_thinking[tnum]
                if not text:
                    continue
                parts.append(
                    f"**🔄 Turn {tnum}**\n\n```text\n{text}{cursor}\n```"
                )
            if parts:
                thinking_placeholder.markdown("\n\n---\n\n".join(parts))

        # 逐 chunk 处理
        for msg_type, content in run_agent(prompt):
            if msg_type == "text":
                full_text += content
                text_placeholder.markdown(full_text + "▌")
            elif msg_type == "thinking":
                # P5：累积到当前 turn 的思考区
                if current_turn not in turn_thinking:
                    turn_thinking[current_turn] = ""
                turn_thinking[current_turn] += content
                # 重新渲染（带 Turn 标签）
                render_thinking_with_turns(cursor="▌")
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
                    # P5：从 "🔄 Turn N/M" 中提取 turn 号，跟踪当前 turn 用于分段 thinking
                    import re
                    m = re.search(r"Turn (\d+)/", str(content))
                    if m:
                        current_turn = int(m.group(1))
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

                # Debug 日志：缓存命中明细
                hit_rate = (content.cached_tokens or 0) / max(content.input_tokens, 1) * 100
                logging.debug(
                    f"🔥 [Cache] in={content.input_tokens:,} · "
                    f"cached={content.cached_tokens:,} · "
                    f"hit={hit_rate:.1f}%"
                )
            elif msg_type == "memory_status":
                # M7 ported: 记忆检索状态 → 累积到 session_state.memory_stats
                ms = st.session_state.memory_stats
                ms["total_searches"] += 1
                ms["total_hits"] += int(content.get("hits", 0))
                ms["current_turn_hits"] = int(content.get("hits", 0))
                ms["stored_total"] = int(content.get("stored_total", 0))
                if content.get("zero_hit"):
                    ms["last_zero_hit_turn"] = ms["total_searches"]
                logging.debug(
                    f"🧠 [Memory] hits={content.get('hits', 0)} · "
                    f"stored={ms['stored_total']} · "
                    f"zero_hit={content.get('zero_hit', False)}"
                )
            elif msg_type == "memory_event":
                # M9: 严格双通道记忆事件 → 累积到 session_state.memory_stats
                event = content
                ms = st.session_state.memory_stats
                kind = event.kind.value
                if kind == "extract_dispatched":
                    ms["candidates_written"] = ms.get("candidates_written", 0) + event.candidates_count
                elif kind == "gate_pass":
                    ms["gate_passes"] = ms.get("gate_passes", 0) + 1
                elif kind == "gate_skip":
                    ms["gate_skips"] = ms.get("gate_skips", 0) + 1
                elif kind == "extract_error":
                    ms["last_extract_error"] = str(event.reason or "unknown")
                # M10 C6.2 / C6.3: 预算超限 / 超时 → 同样写入 last_extract_error
                # banner 显示已通用(消费 last_extract_error)
                elif kind in ("budget_exceeded", "timeout"):
                    ms["last_extract_error"] = str(event.reason or "unknown")
                elif kind == "secret_detected":
                    # M10 C1.2: §14.4 extract_candidates secret sanitize 事件
                    ms["secrets_redacted"] = ms.get("secrets_redacted", 0) + 1
                st.session_state.memory_stats = ms

        # 流式结束：清理 UI
        # 文本区去掉光标
        text_placeholder.markdown(full_text)

        # P5 优化：按 turn 分段渲染思考区（带 Turn 标签）
        # 计算总 thinking 字数（跨所有 turn 求和）
        total_thinking_chars = sum(len(t) for t in turn_thinking.values())
        if turn_thinking:
            # 去掉光标重新渲染（如果某些 turn 仍在 stream 状态）
            render_thinking_with_turns(cursor="")
            # 多轮提示 label
            turn_label = (
                f"💭 思考过程 · {len(turn_thinking)} 轮 · {total_thinking_chars} 字"
                if len(turn_thinking) > 1
                else f"💭 思考过程 · {total_thinking_chars} 字"
            )
            thinking_status.update(
                label=turn_label,
                state="complete",  # 自动折叠 + 绿勾
                expanded=False,
            )
        else:
            # 本轮无 thinking（Compact 路径、enable_thinking 未触发、Provider 返回空等）
            thinking_placeholder.caption(
                f"💭 本轮未返回思考过程（{model}）"
            )
            thinking_status.update(
                label=f"💭 未返回思考过程（{model}）",
                state="complete",
                expanded=False,
            )
        
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
            turn_thinking_tokens = last_turn_usage.thinking_tokens
            turn_total = turn_input + turn_output + turn_thinking_tokens
            st.caption(
                f"📊 本轮: input={turn_input:,} · output={turn_output:,}"
                + (f" · thinking={turn_thinking_tokens:,}" if turn_thinking_tokens else "")
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
    # P5 修复：thinking_text 拼自 turn_thinking（多轮 turn 之间用 \n\n 分隔）
    thinking_text = "\n\n".join(turn_thinking.values()) if turn_thinking else ""
    st.session_state.messages.append({
        "role": "assistant",
        "content": full_text,
        "thinking": thinking_text,
        "tool_logs": tool_logs,
        # Day 7：schema 跟 jsonl entry.usage 对齐（全名 input_tokens 等），
        # 加载历史时直接 entry.get("usage") 就能用，无需转换
        "usage": {
            "input_tokens": last_turn_usage.input_tokens if last_turn_usage else 0,
            "output_tokens": last_turn_usage.output_tokens if last_turn_usage else 0,
            "thinking_tokens": last_turn_usage.thinking_tokens if last_turn_usage else 0,
            "cached_tokens": last_turn_usage.cached_tokens if last_turn_usage else 0,
        } if last_turn_usage else None,
    })

    # 更新 agent history
    # （agent.run() 内部已经更新了 agent.messages）

    # 触发 sidebar 刷新：让上下文预算面板、History 长度等 widget
    # 重新从 agent.messages 读取最新值（Streamlit 渲染顺序：sidebar 先、
    # 主区后，sidebar 在 run() 启动时取的是旧值，run() 完后需要 rerun 一次
    # 才能让 sidebar 看到新 history）。
    st.rerun()
