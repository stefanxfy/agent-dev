"""M3 Task 1 — 权限规则管理页面(/permissions)

显示 + 编辑 allow/deny/ask 规则,实时预览解析结果。
对齐 spec §9 Phase 3 Task 1 + CC `/permissions` 命令。

通过 sidebar `🔐 权限规则` expander 的 `编辑规则 →` 链接进入。
Streamlit multipage 自动把 pages/ 下的文件加进导航。

复用 M1 资产:
- permission_loader.add_permission_rules_to_settings / delete_permission_rule_from_settings
- permission_loader.load_rules_by_source
- permission_ui_helpers(纯函数:预览/构造/格式化)
"""
from __future__ import annotations

import sys
from pathlib import Path

# 把项目根加 sys.path(让 streamlit 多页能找到 agent_core)
_PROJECT_ROOT = Path(__file__).parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import streamlit as st

from agent_core.tools.permission_loader import (
    add_permission_rules_to_settings,
    delete_permission_rule_from_settings,
    load_rules_by_source,
)
from agent_core.tools.permission_types import (
    PermissionBehavior,
    PermissionRule,
    PermissionRuleSource,
    PermissionRuleValue,
)
from agent_core.tools.permission_ui_helpers import (
    build_permission_rule,
    format_rules_by_source,
    render_rule_preview,
)

st.set_page_config(page_title="🔐 权限规则", page_icon="🔐", layout="wide")
st.title("🔐 权限规则管理")
st.caption(
    "管理 allow / deny / ask 权限规则(对齐 Claude Code `/permissions`)。"
    "规则按 source 优先级生效:policy > flag > cliArg > project > local > session > user > command。"
)


# ── 1. 当前规则列表 ──────────────────────────────────────────────

st.subheader("📋 当前规则")

try:
    rules_by_source = load_rules_by_source()
    flat_rules = format_rules_by_source(rules_by_source)
except Exception as e:
    st.error(f"读取规则失败: {e}")
    flat_rules = []

if not flat_rules:
    st.info("暂无规则。在下方添加你的第一条规则。")
else:
    st.caption(f"共 {len(flat_rules)} 条规则(deny 优先显示)")

    # 按 source 分组显示
    sources_present = []
    seen = set()
    for r in flat_rules:
        if r["source"] not in seen:
            seen.add(r["source"])
            sources_present.append(r["source"])

    for source in sources_present:
        source_rules = [r for r in flat_rules if r["source"] == source]
        with st.expander(f"📁 {source}({len(source_rules)} 条)", expanded=False):
            for r in source_rules:
                behavior_icon = {
                    "deny": "🚫", "ask": "❓", "allow": "✅",
                }.get(r["behavior"], "•")
                col1, col2, col3 = st.columns([6, 3, 1])
                with col1:
                    st.write(f"{behavior_icon} `{r['rule_str']}`")
                with col2:
                    # 解析预览
                    try:
                        preview = render_rule_preview(r["tool_name"], r["content"])
                        st.caption(preview)
                    except Exception:
                        st.caption("(解析失败)")
                with col3:
                    if st.button("🗑️", key=f"del-{source}-{r['rule_str']}",
                                 help=f"删除 {r['rule_str']}"):
                        try:
                            rule = PermissionRule(
                                source=PermissionRuleSource(source),
                                behavior=PermissionBehavior(r["behavior"]),
                                value=PermissionRuleValue(
                                    tool_name=r["tool_name"],
                                    rule_content=r["content"],
                                ),
                            )
                            deleted = delete_permission_rule_from_settings(
                                rule, PermissionRuleSource(source),
                            )
                            if deleted:
                                st.success(f"已删除 {r['rule_str']}")
                                st.rerun()
                            else:
                                st.warning(f"未找到 {r['rule_str']}(可能已被改)")
                        except Exception as e:
                            st.error(f"删除失败: {e}")


st.divider()


# ── 2. 添加规则表单 ──────────────────────────────────────────────

st.subheader("➕ 添加规则")

with st.form("add_rule_form", clear_on_submit=False):
    col_a, col_b = st.columns(2)
    with col_a:
        behavior = st.selectbox(
            "Behavior",
            options=["deny", "ask", "allow"],
            help="deny=拒绝;ask=弹窗询问;allow=直接放行",
        )
    with col_b:
        destination = st.selectbox(
            "写入位置(destination)",
            options=["projectSettings", "localSettings", "userSettings"],
            help=(
                "projectSettings=.agent_data/settings.json(团队共享);"
                "localSettings=.agent_data/settings.local.json(本地,不入 git);"
                "userSettings=~/.agent_data/settings.json(用户全局)"
            ),
        )

    tool_name = st.text_input(
        "工具名(tool_name)",
        value="Bash",
        help="如 Bash / Read / Write / Edit;留空会报错",
    )
    content = st.text_input(
        "规则内容(rule_content)",
        value="",
        placeholder="rm:* 或 npm run build(留空 = 整个 tool 命中)",
        help=(
            "前缀: `rm:*`(以 rm 开头);"
            "精确: `npm run build`(完全匹配);"
            "通配: `*echo*`(含 echo);"
            "复合: `rm:* && echo:*`(AND 语义)"
        ),
    )

    # 实时预览
    if tool_name.strip():
        try:
            preview = render_rule_preview(tool_name.strip(), content.strip() or None)
            st.caption(f"🔍 解析预览: `{preview}`")
        except Exception as e:
            st.caption(f"🔍 解析失败: {e}")

    submitted = st.form_submit_button("➕ 添加规则")

    if submitted:
        try:
            rule = build_permission_rule(
                behavior=behavior,
                tool_name=tool_name,
                content=content,
                destination=destination,
            )
            add_permission_rules_to_settings([rule], PermissionRuleSource(destination))
            st.success(f"✅ 已添加 `{rule}` 到 {destination}")
            st.caption("💡 新规则在下次工具调用时生效(或重启会话确保刷新)。")
            st.rerun()
        except ValueError as e:
            st.error(f"参数错误: {e}")
        except Exception as e:
            st.error(f"添加失败: {e}")


st.divider()


# ── 3. 沙箱 excluded commands 编辑区(M3 Task 4)───────────────────

st.subheader("🛡️ 沙箱排除命令")

from agent_core.tools.permission_loader import (
    load_excluded_commands,
    save_excluded_commands,
)

st.caption(
    "这些命令命中后将**跳过 OS 沙箱**(对齐 doc §5.3 + CC excludedCommands)。"
    "这是 **UX 而非安全** — 被排除的命令仍受应用层权限检查约束,"
    "只是不在 OS 沙箱里跑。常用于需要绕过沙箱限制的命令(如 git 提交、网络请求等)。"
)

# 4a. 当前列表 + 命中提示预览
try:
    current_excluded = load_excluded_commands()
except Exception as e:
    st.error(f"读取 excluded commands 失败: {e}")
    current_excluded = []

st.caption(f"当前 {len(current_excluded)} 条排除规则(大小写敏感,substr match):")
if current_excluded:
    for p in current_excluded:
        # 实时显示命中提示(友好消息)
        try:
            from agent_core.tools.sandbox_decision import get_excluded_command_message
            msg = get_excluded_command_message(p)
            if msg:
                st.caption(f"  • `{p}` → {msg}")
            else:
                st.caption(f"  • `{p}`")
        except Exception:
            st.caption(f"  • `{p}`")
else:
    st.caption("  (暂无)")

st.divider()

# 4b. 编辑表单
with st.form("excluded_commands_form", clear_on_submit=False):
    st.caption("编辑排除命令(每行一条,substr match,大小写敏感):")

    initial_text = "\n".join(current_excluded) if current_excluded else ""
    new_text = st.text_area(
        "excluded commands(每行一条)",
        value=initial_text,
        height=140,
        placeholder="git commit\nnpm publish\ndocker push",
        help=(
            "每行一条 pattern,保存后写入 settings.json 的 sandbox.excludedCommands 段。"
            "留空 = 清空列表。"
        ),
    )

    col_dest, col_btn = st.columns([3, 1])
    with col_dest:
        excluded_destination = st.selectbox(
            "写入位置",
            options=["projectSettings", "localSettings", "userSettings"],
            key="excluded_destination",
            help=(
                "projectSettings=.agent_data/settings.json;"
                "localSettings=.agent_data/settings.local.json(不入 git);"
                "userSettings=~/.agent_data/settings.json"
            ),
        )
    with col_btn:
        submitted_excluded = st.form_submit_button("💾 保存 excluded")

if submitted_excluded:
    try:
        new_patterns = [
            line.strip() for line in new_text.splitlines() if line.strip()
        ]
        save_excluded_commands(new_patterns, PermissionRuleSource(excluded_destination))
        st.success(
            f"✅ 已保存 {len(new_patterns)} 条 excluded commands 到 {excluded_destination}。"
            "💡 新规则在下次工具调用时生效(或重启沙箱确保 reload)。"
        )
        st.rerun()
    except Exception as e:
        st.error(f"保存失败: {e}")


st.divider()


# ── 4. 规则语法速查(M3 Task 1)────────────────────────────────────

with st.expander("📖 规则语法速查", expanded=False):
    st.markdown(
        """
| 形态 | 示例 | 语义 |
|------|------|------|
| 整个 tool | `Bash` | 所有 Bash 调用 |
| 前缀 | `Bash(rm:*)` | 以 `rm ` 开头的命令 |
| 精确 | `Bash(npm run build)` | 完全等于 |
| 通配 | `Bash(*echo*)` | 含 `echo` |
| 复合 | `Bash(rm:* && echo:*)` | AND(两部分都匹配) |

**source 优先级**(高→低):
`flag > policy > cliArg > project > local > session > user > command`

deny > ask > allow(同 source 内 deny 最强)。
"""
    )

st.caption("🔐 权限规则管理 · M3 Task 1 + Task 4 · 对齐 Claude Code `/permissions`")
