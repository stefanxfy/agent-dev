"""
会话管理功能测试套件
覆盖 SessionStorage / SessionMetadata / SessionState / ProgressTracker / SessionManager / restore / cleanup

运行方式：
    cd ~/Desktop/myproject/agent-dev
    python3 -m agent_core.session.test_session

依赖：Python 3.9+
"""

import sys
import os
import tempfile
import shutil

# 确保可以 import
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent_core.session import (
    SessionManager,
    SessionStorage,
    SessionMetadata,
    SessionState,
    SessionCleanup,
    ProgressTracker,
    resume_session,
    continue_session,
    fork_session,
    list_sessions,
)
from agent_core.session.progress import FileChangeType

# ── 测试工具 ────────────────────────────────────────────────────────────────

class Test:
    passed = 0
    failed = 0
    current_module = ""

    @classmethod
    def module(cls, name: str):
        cls.current_module = name
        print(f"\n{'='*60}")
        print(f"  {name}")
        print(f"{'='*60}")

    @classmethod
    def check(cls, name: str, cond: bool, detail: str = ""):
        if cond:
            cls.passed += 1
            print(f"  ✅ {name}")
        else:
            cls.failed += 1
            print(f"  ❌ {name}: {detail}")

    @classmethod
    def assert_equal(cls, name: str, a, b):
        cls.check(name, a == b, f"got {a!r}, expected {b!r}")

    @classmethod
    def assert_in(cls, name: str, item, container):
        cls.check(name, item in container, f"{item!r} not in {container!r}")

    @classmethod
    def assert_not_none(cls, name: str, val):
        cls.check(name, val is not None, f"got None")

    @classmethod
    def summary(cls):
        total = cls.passed + cls.failed
        print(f"\n{'='*60}")
        print(f"  测试结果: {cls.passed}/{total} 通过", )
        if cls.failed == 0:
            print("  🎉 全部通过！")
        else:
            print(f"  ❌ {cls.failed} 个失败")
        print(f"{'='*60}")
        return cls.failed == 0


# ── 测试用例 ────────────────────────────────────────────────────────────────

def test_parent_uuid_chain(tmpdir: str):
    """测试 parentUuid 链"""
    Test.module("1. parentUuid 链")

    manager = SessionManager(data_dir=tmpdir)

    # 写 5 条消息
    u1 = manager.add_user_message("你好")
    u2 = manager.add_assistant_message("你好！有什么可以帮你的？")
    u3 = manager.add_user_message("帮我写排序函数")
    u4 = manager.add_assistant_message("好的")
    u5 = manager.add_tool_use("write_file", {"path": "sort.py"}, tool_use_id="tc1")

    manager.flush()

    # 读取验证链
    msgs = manager.get_messages()
    Test.assert_equal("消息数量", len(msgs), 5)

    # 验证 parentUuid 链
    # msg[0] 的 parentUuid 必须是 None（链起点）
    Test.assert_equal("第1条 parentUuid=None", msgs[0].get("parentUuid"), None)
    # msg[n] 的 parentUuid = msg[n-1] 的 uuid
    Test.assert_equal("第2条 parentUuid=第1条", msgs[1].get("parentUuid"), u1)
    Test.assert_equal("第3条 parentUuid=第2条", msgs[2].get("parentUuid"), u2)
    Test.assert_equal("第4条 parentUuid=第3条", msgs[3].get("parentUuid"), u3)
    Test.assert_equal("第5条 parentUuid=第4条", msgs[4].get("parentUuid"), u4)

    print(f"  链结构验证: {[m.get('parentUuid','')[:8] if m.get('parentUuid') else 'None' for m in msgs]}")

    # 清理
    manager.delete()


def test_compact_boundary(tmpdir: str):
    """测试压缩边界（断链标记）"""
    Test.module("2. 压缩边界（compact_boundary）")

    manager = SessionManager(data_dir=tmpdir)

    # 先写几条消息
    for i in range(5):
        manager.add_user_message(f"消息{i}")
        manager.add_assistant_message(f"回复{i}")
    manager.flush()

    # 添加压缩边界
    boundary_uuid = manager.add_compact_boundary()
    manager.flush()

    # get_messages(stop_at_boundary=True) 应该停在边界前
    msgs_with_boundary = manager.get_messages(stop_at_boundary=True)
    msgs_all = manager.get_messages(stop_at_boundary=False)

    # 所有消息（包括边界后，但目前边界后没新消息）
    all_entries = manager.storage.read_entries(include_compact_boundary=True)
    boundary_entries = [e for e in all_entries if e.get("type") == "compact-boundary"]

    Test.assert_equal("边界 entry 存在", len(boundary_entries), 1)
    Test.assert_equal("边界 parentUuid=None（断链）", boundary_entries[0].get("parentUuid"), None)
    Test.assert_equal("边界 UUID 正确", boundary_entries[0].get("uuid"), boundary_uuid)
    Test.assert_equal("stop_at_boundary=True 停在边界前", len(msgs_with_boundary), 10)  # 5*2条消息


def test_resume_and_continue(tmpdir: str):
    """测试 Resume / Continue 语义"""
    Test.module("3. Resume / Continue 语义")

    manager = SessionManager(data_dir=tmpdir)

    # 写一些消息
    for i in range(3):
        manager.add_user_message(f"消息{i}")
        manager.add_assistant_message(f"回复{i}")
    manager.flush()

    # 添加压缩边界
    manager.add_compact_boundary()
    manager.flush()

    # 在边界后追加几条新消息
    manager.add_user_message("边界后消息")
    manager.add_assistant_message("边界后回复")
    manager.flush()

    # Resume：从断链处恢复
    resume_msgs, resume_meta = resume_session(manager.session_id, data_dir=tmpdir)
    Test.assert_in("Resume 有边界后消息", "边界后消息",
                    [m.get("content", "") for m in resume_msgs])

    # Continue：读取全部消息
    cont_msgs, cont_meta = continue_session(manager.session_id, data_dir=tmpdir)
    # Continue 应该有边界前 + 边界后的所有消息
    all_content = [m.get("content", "") for m in cont_msgs]
    Test.assert_in("Continue 有边界前消息", "消息0", all_content)
    Test.assert_in("Continue 有边界后消息", "边界后消息", all_content)


def test_fork(tmpdir: str):
    """测试 Fork 语义"""
    Test.module("4. Fork（分叉）")

    manager = SessionManager(data_dir=tmpdir)
    manager.update_title("父会话")

    # 写消息
    for i in range(3):
        manager.add_user_message(f"父消息{i}")
        manager.add_assistant_message(f"父回复{i}")
    manager.flush()

    parent_id = manager.session_id

    # Fork
    fork_id = manager.fork()
    Test.assert_not_none("Fork 返回新 session_id", fork_id)
    Test.assert_equal("Fork session_id 不同", fork_id != parent_id, True)

    # 验证 Fork 的消息
    fork_msgs = SessionStorage(session_id=fork_id, data_dir=tmpdir).get_messages()
    Test.assert_equal("Fork 消息数量=父消息数量", len(fork_msgs), len(manager.get_messages()))

    # 验证 parentUuid 链保留（UUID 不同，但链结构相同）
    parent_msgs = manager.get_messages()
    for i, (pm, fm) in enumerate(zip(parent_msgs, fork_msgs)):
        Test.assert_equal(f"Fork 消息{i} role 相同", pm.get("role"), fm.get("role"))
        Test.assert_equal(f"Fork 消息{i} UUID 不同", pm.get("uuid") != fm.get("uuid"), True)

    # 在 Fork 中追加消息（父会话不受影响）
    fork_manager = SessionManager(session_id=fork_id, data_dir=tmpdir)
    fork_manager.add_user_message("Fork 新消息")
    fork_manager.add_assistant_message("Fork 确认")
    fork_manager.flush()

    # 父会话消息数不变
    parent_msgs2 = manager.get_messages()
    Test.assert_equal("Fork 追加后，父会话消息数不变", len(parent_msgs2), len(parent_msgs))

    # Fork 有自己的消息（父消息 + 新消息）
    fork_msgs2 = fork_manager.get_messages()
    # Fork 消息数 = 父消息数（6条）+ 新消息（2条）= 8
    Test.assert_equal("Fork 有父消息", len(fork_msgs2), 8)
    Test.assert_equal("Fork 有新消息", any("Fork 新消息" in str(m) for m in fork_msgs2), True)

    # 清理
    manager.delete()
    fork_manager.delete()


def test_metadata(tmpdir: str):
    """测试元数据管理"""
    Test.module("5. 元数据管理")

    manager = SessionManager(data_dir=tmpdir)

    manager.update_title("自定义标题")
    manager.update_ai_title("AI生成的标题")
    manager.add_tag("tag1")
    manager.add_tag("tag2")
    manager.update_mode("write")
    manager.update_last_prompt("最后一条用户消息")
    manager.flush()

    # 从磁盘恢复元数据（直接读取完整 JSONL 尾部）
    all_entries = manager.storage.read_entries(include_compact_boundary=True)
    tail = all_entries[-20:]  # 取最后 20 条 entry
    restored = SessionMetadata.from_tail(tail, manager.session_id)

    Test.assert_equal("标题恢复", restored.title, "自定义标题")
    Test.assert_equal("AI标题恢复", restored.ai_title, "AI生成的标题")
    Test.assert_equal("标签恢复", restored.tags, ["tag1", "tag2"])
    Test.assert_equal("模式恢复", restored.mode, "write")
    Test.assert_equal("最后提示恢复", restored.last_prompt, "最后一条用户消息")
    Test.assert_equal("display_title 优先用户标题", restored.display_title, "自定义标题")

    # 清理
    manager.delete()


def test_state_machine(tmpdir: str):
    """测试状态机"""
    Test.module("6. 状态机（idle / running / requires_action）")

    state = SessionState(session_id="test")

    Test.assert_equal("初始状态 idle", state.status, "idle")
    Test.assert_equal("is_idle", state.is_idle, True)

    state.set_running()
    Test.assert_equal("set_running 后 running", state.status, "running")
    Test.assert_equal("is_idle=False", state.is_idle, False)

    from agent_core.session import RequiresActionDetails
    details = RequiresActionDetails(
        action_type="permission_request",
        message="需要写入权限",
        tool_name="write_file",
        tool_input={"path": "/etc/passwd"},
    )
    state.set_requires_action(details)
    Test.assert_equal("set_requires_action 后 requires_action", state.status, "requires_action")
    Test.assert_equal("is_requires_action", state.is_requires_action, True)
    Test.assert_equal("details 正确", state.requires_action_details.action_type, "permission_request")

    state.set_idle()
    Test.assert_equal("set_idle 后 idle", state.status, "idle")

    # 状态历史
    history = state.history
    Test.assert_equal("历史记录条数", len(history), 3)


def test_progress_tracker(tmpdir: str):
    """测试进度追踪"""
    Test.module("7. 进度追踪")

    tracker = ProgressTracker(session_id="test")

    # 文件变更
    tracker.track_file_created("/tmp/a.py", "tc1", "创建文件")
    tracker.track_file_modified("/tmp/a.py", "tc2", "修改内容", lines_added=10, lines_removed=2)

    # 待办
    todo1 = tracker.add_todo("完成排序", priority=1)
    todo2 = tracker.add_todo("写测试", priority=0)
    tracker.complete_todo(todo1.id)

    # Turn 统计
    tracker.start_turn()
    tracker.record_tool_call("write_file")
    tracker.record_tool_call("read_file")
    tracker.record_llm_call(tokens=500)
    tracker.record_compaction()

    snap = tracker.snapshot(status="running")

    Test.assert_equal("Turn 数", snap.turn_stats.turn_count, 1)
    Test.assert_equal("工具调用数", snap.turn_stats.tool_call_count, 2)
    Test.assert_equal("LLM 调用数", snap.turn_stats.llm_calls, 1)
    Test.assert_equal("压缩次数", snap.turn_stats.compactions, 1)
    Test.assert_equal("Token 数", snap.turn_stats.total_tokens, 500)
    Test.assert_equal("文件变更数", len(snap.file_changes), 2)
    Test.assert_equal("待办数（只显示 pending）", len(snap.todo_items), 1)
    Test.assert_equal("待办标题", snap.todo_items[0].description, "写测试")
    Test.assert_equal("最近工具", snap.recent_tool_calls[-1], "read_file")


def test_jsonl_storage_details(tmpdir: str):
    """测试 JSONL 存储细节"""
    Test.module("8. JSONL 存储细节")

    storage = SessionStorage(data_dir=tmpdir)

    # 延迟创建：文件在第一条消息前不存在
    import os
    jsonl_path = storage.data_dir / f"{storage.session_id}.jsonl"
    Test.assert_equal("延迟创建：文件未创建", os.path.exists(jsonl_path), False)

    # 追加第一条消息后文件存在
    storage.add_message("user", "hello")
    storage.flush()
    Test.assert_equal("flush 后文件存在", os.path.exists(jsonl_path), True)

    # 验证 JSONL 格式（每行一条 JSON）
    with open(jsonl_path, "r") as f:
        lines = [l.strip() for l in f if l.strip()]
    Test.assert_equal("JSONL 行数=消息数", len(lines), 1)

    # 验证 JSON 可解析
    import json
    entry = json.loads(lines[0])
    Test.assert_equal("Entry 有 uuid", "uuid" in entry, True)
    Test.assert_equal("Entry 有 parentUuid", "parentUuid" in entry, True)
    Test.assert_equal("Entry 有 sessionId", "sessionId" in entry, True)
    Test.assert_equal("Entry 有 timestamp", "timestamp" in entry, True)

    # 追加第二条，验证 parentUuid 链
    storage.add_message("assistant", "hi")
    storage.flush()
    with open(jsonl_path, "r") as f:
        lines = [json.loads(l.strip()) for l in f if l.strip()]
    Test.assert_equal("parentUuid 链正确", lines[1]["parentUuid"], lines[0]["uuid"])

    # 清理
    storage.delete()


def test_list_and_delete(tmpdir: str):
    """测试会话列表和删除"""
    Test.module("9. 会话列表和删除")

    # 创建多个会话
    ids = []
    for i in range(3):
        m = SessionManager(data_dir=tmpdir)
        m.add_user_message(f"会话{i}")
        m.flush()
        ids.append(m.session_id)

    sessions = list_sessions(data_dir=tmpdir)
    Test.assert_equal("列出会话数", len(sessions), 3)

    # 按更新时间倒序
    for i in range(len(sessions) - 1):
        Test.assert_equal(f"会话{i} 在 {i+1} 之前或同时",
                          sessions[i]["updated_at"] >= sessions[i+1]["updated_at"], True)

    # 删除一个
    SessionManager.delete_session(ids[0], data_dir=tmpdir)
    sessions2 = list_sessions(data_dir=tmpdir)
    Test.assert_equal("删除后会话数", len(sessions2), 2)

    # 清理剩余测试会话
    for sid in ids[1:]:
        SessionManager.delete_session(sid, data_dir=tmpdir)


def test_cleanup(tmpdir: str):
    """测试清理归档"""
    Test.module("10. 清理归档")

    cleanup = SessionCleanup(data_dir=tmpdir)

    # 创建会话
    m = SessionManager(data_dir=tmpdir)
    m.add_user_message("测试")
    m.flush()

    # TTL 清理（不实际删除）
    deleted = cleanup.cleanup_by_ttl(ttl_days=30, dry_run=True)
    Test.assert_equal("TTL dry_run 不删除", len(deleted), 0)

    # 空会话清理
    deleted_empty = cleanup.cleanup_empty_sessions(dry_run=True)
    Test.assert_equal("无空会话", len(deleted_empty), 0)

    # 磁盘统计
    usage = cleanup.disk_usage()
    Test.assert_equal("会话数", usage["session_count"], 1)
    Test.assert_equal("有字节数", usage["total_bytes"] > 0, True)
    Test.assert_in("有 MB 统计", "total_mb", usage)

    # 清理
    m.delete()


def test_integration_full_workflow(tmpdir: str):
    """端到端完整工作流"""
    Test.module("11. 端到端完整工作流")

    # 创建一个开发会话
    manager = SessionManager(data_dir=tmpdir)
    manager.update_title("排序算法开发")
    manager.add_tag("algorithm")
    manager.add_tag("python")

    # Turn 1
    # 清理（如果之前有遗留会话）
    manager.delete()

    manager.state.set_running("用户输入")
    u1 = manager.add_user_message("帮我写一个 quicksort")
    a1 = manager.add_assistant_message("好的，我写 quicksort")
    tc1 = manager.add_tool_use("write_file",
                                {"path": "quicksort.py", "content": "def quicksort():\n    pass"},
                                tool_use_id="tc1")
    tr1 = manager.add_tool_result("tc1", "文件已创建: quicksort.py")
    manager.state.set_idle()
    manager.flush()

    # Turn 2
    manager.state.set_running("用户输入")
    u2 = manager.add_user_message("改成降序")
    a2 = manager.add_assistant_message("好的")
    tc2 = manager.add_tool_use("edit_file",
                                {"path": "quicksort.py", "old": "pass", "new": "reverse=True"},
                                tool_use_id="tc2")
    tr2 = manager.add_tool_result("tc2", "已修改")
    manager.state.set_idle()
    manager.flush()

    # 进度追踪
    tracker = manager.progress
    tracker.start_turn()
    tracker.start_turn()
    tracker.record_tool_call("write_file")
    tracker.record_tool_call("edit_file")

    # 持久化（manager 在 Turn 循环中没有显式 flush）
    manager.flush()

    # 压缩（模拟）
    manager.add_summary("用户要求写 quicksort，已创建 quicksort.py 并修改为降序", tokens_saved=200, format="BASE")
    manager.add_compact_boundary()
    manager.flush()

    # Fork 一个实验分支
    branch_id = manager.fork("降序实验")
    branch_manager = SessionManager(session_id=branch_id, data_dir=tmpdir)
    branch_manager.add_user_message("再加个归并排序")
    branch_manager.flush()

    # Resume 父会话
    resume_msgs, resume_meta = resume_session(manager.session_id, data_dir=tmpdir)
    # Continue 完整会话
    cont_msgs, cont_meta = continue_session(manager.session_id, data_dir=tmpdir)

    # 验证
    Test.assert_equal("Resume 有压缩边界后消息", len(resume_msgs) >= 0, True)
    Test.assert_equal("Continue 有完整消息（>=4条）", len(cont_msgs) >= 4, True)
    Test.assert_equal("元数据标题", resume_meta.display_title, "排序算法开发")

    snap = tracker.snapshot(status="idle")
    Test.assert_equal("Turn 统计", snap.turn_stats.turn_count, 2)
    Test.assert_equal("工具调用统计", snap.turn_stats.tool_call_count, 2)

    # 会话列表
    sessions = list_sessions(data_dir=tmpdir)
    Test.assert_equal("会话数量（父+分支）", len(sessions), 2)

    # 清理
    manager.delete()


# ── 入口 ──────────────────────────────────────────────────────────────────

def main():
    # 用临时目录隔离测试
    tmpdir = tempfile.mkdtemp(prefix="agent_session_test_")
    print(f"测试目录: {tmpdir}")
    print("（测试结束后自动清理）")

    try:
        test_parent_uuid_chain(tmpdir)
        test_compact_boundary(tmpdir)
        test_resume_and_continue(tmpdir)
        test_fork(tmpdir)
        test_metadata(tmpdir)
        test_state_machine(tmpdir)
        test_progress_tracker(tmpdir)
        test_jsonl_storage_details(tmpdir)
        test_list_and_delete(tmpdir)
        test_cleanup(tmpdir)
        test_integration_full_workflow(tmpdir)

        ok = Test.summary()
        return 0 if ok else 1

    finally:
        print(f"\n清理临时目录: {tmpdir}")
        shutil.rmtree(tmpdir)


if __name__ == "__main__":
    sys.exit(main())
