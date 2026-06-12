"""
会话管理测试页面 — 测试 agent_core/session/ 所有功能
"""
from __future__ import annotations

import sys
from pathlib import Path
from datetime import datetime

# 项目根目录加入 sys.path
PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import streamlit as st

st.set_page_config(
    page_title="会话管理测试",
    page_icon="💬",
    layout="wide",
)

st.title("💬 会话管理测试")
st.caption("测试 agent_core/session/ 模块的完整功能")

# 导入会话管理模块
try:
    from agent_core.session import (
        SessionManager,
        SessionStorage,
        SessionMetadata,
        SessionState,
        ProgressTracker,
        SessionCleanup,
        resume_session,
        continue_session,
    )
    SESSION_AVAILABLE = True
except ImportError as e:
    st.error(f"导入会话模块失败: {e}")
    SESSION_AVAILABLE = False

if not SESSION_AVAILABLE:
    st.stop()

# ── 初始化 ─────────────────────────────────────────────────────
if "session_manager" not in st.session_state:
    st.session_state.session_manager = None
if "current_session_id" not in st.session_state:
    st.session_state.current_session_id = None

# ── 侧边栏：会话操作 ──────────────────────────────────────────
with st.sidebar:
    st.header("🔧 会话操作")

    # 数据目录
    data_dir = st.text_input("数据目录", value=str(PROJECT_ROOT / ".agent_data"))

    # 创建新会话
    if st.button("➕ 创建新会话", use_container_width=True):
        mgr = SessionManager(data_dir=data_dir)
        st.session_state.session_manager = mgr
        st.session_state.current_session_id = mgr.session_id
        st.success(f"✅ 创建会话: {mgr.session_id[:8]}")
        st.rerun()

    # 列出所有会话
    st.divider()
    st.subheader("📋 已有会话")
    try:
        sessions = SessionManager.list_sessions(data_dir=data_dir)
        if sessions:
            for sess in sessions:
                col1, col2 = st.columns([3, 1])
                with col1:
                    label = sess.get("title") or sess["session_id"][:8]
                    if st.button(f"📄 {label}", key=f"load_{sess['session_id']}"):
                        mgr = SessionManager(data_dir=data_dir, session_id=sess["session_id"])
                        st.session_state.session_manager = mgr
                        st.session_state.current_session_id = sess["session_id"]
                        st.rerun()
                with col2:
                    if st.button("🗑️", key=f"del_{sess['session_id']}", help="删除"):
                        SessionManager.delete_session(sess["session_id"], data_dir=data_dir)
                        if st.session_state.current_session_id == sess["session_id"]:
                            st.session_state.session_manager = None
                            st.session_state.current_session_id = None
                        st.rerun()
        else:
            st.caption("暂无会话")
    except Exception as e:
        st.error(f"加载失败: {e}")

# ── 主界面 ──────────────────────────────────────────────────────
if st.session_state.session_manager is None:
    st.info("👈 请在左侧创建或选择一个会话")
    st.stop()

mgr = st.session_state.session_manager

# 会话信息
col1, col2, col3 = st.columns([2, 1, 1])
with col1:
    new_title = st.text_input("会话标题", value=mgr.metadata.title or "")
    if new_title != mgr.metadata.title:
        mgr.update_title(new_title)
        st.success("标题已更新")
with col2:
    new_tag = st.text_input("添加标签", key="new_tag")
    if st.button("添加", key="add_tag_btn"):
        if new_tag:
            mgr.add_tag(new_tag)
            st.rerun()
with col3:
    st.metric("消息数", len(mgr.get_messages()))
    # Token 统计见 Tab 2（进度追踪）

# 标签显示
if mgr.metadata.tags:
    st.caption(f"标签: {', '.join(mgr.metadata.tags)}")

st.divider()

# ── Tab 布局 ────────────────────────────────────────────────────
tab1, tab2, tab3, tab4, tab5 = st.tabs([
    "💬 消息",
    "📊 进度",
    "🔄 Resume/Continue",
    "🍴 Fork",
    "🗂️ 元数据",
])

with tab1:
    st.subheader("发送消息")

    with st.form("send_message_form", clear_on_submit=True):
        user_input = st.text_input("用户输入", placeholder="输入消息...")
        submitted = st.form_submit_button("发送")

        if submitted and user_input:
            # 添加用户消息
            mgr.add_user_message(user_input)
            mgr.flush()

            # 模拟 Assistant 回复
            assistant_reply = f"收到：{user_input}（这是模拟回复）"
            mgr.add_assistant_message(assistant_reply)
            mgr.flush()

            st.success("消息已添加")
            st.rerun()

    st.divider()

    # 显示消息历史
    st.subheader("消息历史")
    messages = mgr.get_messages()

    if messages:
        for i, msg in enumerate(messages):
            role = msg.get("role", "unknown")
            content = msg.get("content", "")
            timestamp = msg.get("timestamp", "")

            with st.chat_message(role):
                st.write(content)
                st.caption(f"#{i} | {timestamp}")
    else:
        st.caption("暂无消息")

with tab2:
    st.subheader("进度追踪")

    # 初始化 ProgressTracker
    if "tracker" not in st.session_state:
        st.session_state.tracker = ProgressTracker(session_id=mgr.session_id)

    tracker = st.session_state.tracker

    col1, col2 = st.columns(2)

    with col1:
        st.write("**文件变更**")
        with st.form("file_form"):
            path = st.text_input("文件路径", key="file_path")
            action = st.selectbox("操作", ["created", "modified", "deleted"], key="file_action")
            desc = st.text_input("描述", key="file_desc")
            if st.form_submit_button("记录"):
                if action == "created":
                    tracker.track_file_created(path, "test", desc)
                elif action == "modified":
                    tracker.track_file_modified(path, "test", desc)
                else:
                    tracker.track_file_deleted(path, "test", desc)
                st.success("已记录")
                st.rerun()

        st.write("**待办事项**")
        with st.form("todo_form"):
            todo_desc = st.text_input("待办内容", key="todo_desc")
            todo_priority = st.slider("优先级", 0, 5, 0, key="todo_priority")
            if st.form_submit_button("添加待办"):
                tracker.add_todo(todo_desc, priority=todo_priority)
                st.success("待办已添加")
                st.rerun()

    with col2:
        st.write("**快照**")
        snap = tracker.snapshot(status="running")

        st.metric("Turn 数", snap.turn_stats.turn_count)
        st.metric("工具调用", snap.turn_stats.tool_call_count)
        st.metric("LLM 调用", snap.turn_stats.llm_calls)
        st.metric("Token 使用", snap.turn_stats.total_tokens)
        st.metric("压缩次数", snap.turn_stats.compactions)

        if snap.file_changes:
            st.write("**文件变更**")
            for fc in snap.file_changes:
                st.caption(f"• {fc.path} ({fc.action})")

        if snap.todo_items:
            st.write("**待办**")
            for todo in snap.todo_items:
                status = "✅" if todo.completed else "⬜"
                st.caption(f"{status} {todo.description} (优先级 {todo.priority})")

with tab3:
    st.subheader("Resume / Continue 测试")

    st.write("**当前会话 ID:**", mgr.session_id[:8])

    # 添加压缩边界
    if st.button("添加压缩边界（模拟压缩）"):
        boundary_uuid = mgr.add_compact_boundary()
        mgr.flush()
        st.success(f"已添加压缩边界: {boundary_uuid[:8]}")
        st.rerun()

    st.divider()

    # 测试 resume
    st.write("**Resume（从断链处恢复）**")
    if st.button("测试 Resume"):
        try:
            resume_msgs, resume_meta = resume_session(mgr.session_id, data_dir=data_dir)
            st.success(f"Resume 成功，消息数: {len(resume_msgs)}")
            with st.expander("查看消息"):
                for msg in resume_msgs:
                    role = msg.get('role') or msg.get('type', 'unknown')
                    content = msg.get('content', '') or msg.get('summary', '') or str(msg)[:50]
                    st.caption(f"{role}: {content[:50]}")
        except Exception as e:
            st.error(f"Resume 失败: {e}")

    # 测试 continue
    st.write("**Continue（读取全部消息）**")
    if st.button("测试 Continue"):
        try:
            cont_msgs, cont_meta = continue_session(mgr.session_id, data_dir=data_dir)
            st.success(f"Continue 成功，消息数: {len(cont_msgs)}")
            with st.expander("查看消息"):
                for msg in cont_msgs:
                    role = msg.get('role') or msg.get('type', 'unknown')
                    content = msg.get('content', '') or msg.get('summary', '') or str(msg)[:50]
                    st.caption(f"{role}: {content[:50]}")
        except Exception as e:
            st.error(f"Continue 失败: {e}")

with tab4:
    st.subheader("Fork 会话")

    new_session_id = st.text_input("新会话 ID（留空自动生成）", key="fork_id")

    if st.button("🍴 Fork 此会话"):
        try:
            new_id = mgr.fork(new_session_id or None)
            st.success(f"✅ Fork 成功: {new_id[:8]}")
            st.info(f"新会话 ID: {new_id}")
        except Exception as e:
            st.error(f"Fork 失败: {e}")

with tab5:
    st.subheader("元数据详情")

    meta = mgr.metadata

    # 基本信息
    st.json({
        "session_id": meta.session_id,
        "title": meta.title,
        "ai_title": meta.ai_title,
        "tags": meta.tags,
        "agent_name": meta.agent_name,
        "mode": meta.mode,
        "last_prompt": meta.last_prompt,
        "project_slug": meta.project_slug,
    })

    st.divider()

    # 清理功能
    st.subheader("清理归档")
    cleanup = SessionCleanup(data_dir=data_dir)

    col1, col2, col3 = st.columns(3)

    with col1:
        if st.button("检查过期会话"):
            expired = cleanup.list_expired()
            if expired:
                st.warning(f"发现 {len(expired)} 个过期会话")
                for sess in expired:
                    st.caption(f"• {sess['session_id'][:8]}: {sess['title']}")
            else:
                st.success("无过期会话")

    with col2:
        if st.button("检查空会话"):
            empty = cleanup.list_empty_sessions()
            if empty:
                st.warning(f"发现 {len(empty)} 个空会话")
            else:
                st.success("无空会话")

    with col3:
        if st.button("磁盘统计"):
            usage = cleanup.disk_usage()
            st.json(usage)

# ── 底部：原始数据查看 ─────────────────────────────────────────
st.divider()
with st.expander("🔍 查看原始 JSONL 数据"):
    try:
        storage = SessionStorage(data_dir=data_dir)
        entries = storage.read_entries(include_compact_boundary=True)
        st.write(f"**条目数:** {len(entries)}")
        st.json(entries[-10:])  # 显示最后 10 条
    except Exception as e:
        st.error(f"读取失败: {e}")
