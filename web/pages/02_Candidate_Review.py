"""M10 C4.2 — Candidate Review 页面

显示 _candidate/ 下所有候选, 每条提供 Accept/Reject/Edit/Skip 4 个 action。
通过 sidebar `📥 待审记忆` expander 的 `查看全部 →` 链接进入。
"""
from __future__ import annotations

import sys
from pathlib import Path

# 把项目根加 sys.path(让 streamlit 多页能找到 agent_core)
_PROJECT_ROOT = Path(__file__).parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import streamlit as st

from agent_core.memory.candidate_actions import (
    list_candidates,
    accept_candidate,
    reject_candidate,
    edit_candidate,
    skip_candidate,
    _parse_candidate,
)

st.set_page_config(page_title="📥 Candidate Review", page_icon="📥", layout="wide")
st.title("📥 Candidate Review")

# 拿 mem_root(与 get_agent / sidebar 同口径: config.agent_data_dir or ~/.agent_data)
try:
    from agent_core.config import config as _agent_config
    _agent_data_dir = _agent_config.agent_data_dir or str(Path.home() / ".agent_data")
except Exception:
    _agent_data_dir = str(Path.home() / ".agent_data")
_mem_root = Path(_agent_data_dir) / "memory"
st.caption(f"mem_root: `{_mem_root}`")

# 列候选
pending = list_candidates(_mem_root)
st.subheader(f"{len(pending)} 条待审")

if not pending:
    st.info("🎉 没有待审候选 — 全部都审过了(或还没生成)。")
    st.stop()

# 解析 + 渲染每条
for i, p in enumerate(pending):
    with st.container(border=True):
        parsed = _parse_candidate(p)
        col1, col2 = st.columns([3, 1])
        with col1:
            st.markdown(f"### {parsed['title']}")
            rel = p.relative_to(_mem_root / "_candidate") if (_mem_root / "_candidate").exists() else p.name
            st.caption(f"`{parsed['type']}` · {rel}")
            with st.expander("正文", expanded=False):
                st.markdown(parsed["body"])
            edit_mode = st.toggle("编辑", key=f"edit_{i}")
            new_body = None
            if edit_mode:
                new_body = st.text_area(
                    "Body", value=parsed["body"], key=f"body_{i}", height=200
                )
        with col2:
            if st.button("✅ Accept", key=f"accept_{i}", use_container_width=True):
                try:
                    if edit_mode and new_body is not None:
                        edit_candidate(_mem_root, p, new_body)
                    item_hash = accept_candidate(_mem_root, p)
                    st.success(f"已 accept → `{item_hash[:12]}...`")
                    st.rerun()
                except Exception as e:
                    st.error(f"accept 失败: {e}")
            if st.button("❌ Reject", key=f"reject_{i}", use_container_width=True):
                try:
                    reject_candidate(_mem_root, p)
                    st.rerun()
                except Exception as e:
                    st.error(f"reject 失败: {e}")
            if st.button("⏭ Skip", key=f"skip_{i}", use_container_width=True):
                skip_candidate(_mem_root, p)
                st.toast(f"跳过 {p.name}", icon="⏭")
                st.rerun()
