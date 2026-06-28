"""
验证 UI 加载逻辑正确跳过压缩摘要

回归测试：web/app.py 加载历史时，type=user + isCompactSummary=True
的消息必须被跳过，不加载到主聊天区。

修复前 bug：etype in (..., "summary") 判断永不生效（type 是 "user"），
导致 1370 字符的压缩摘要被当成普通 user 消息加载，污染主聊天区。
"""
import sys
import tempfile
import json
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from agent_core.session.storage import SessionStorage


def main():
    with tempfile.TemporaryDirectory() as tmpdir:
        sid = "test-compact-load"
        storage = SessionStorage(session_id=sid, data_dir=tmpdir)

        # 写入 5 条普通对话
        for i in range(5):
            storage.append_entry(
                entry_type="user",
                message={"role": "user", "content": f"用户问题 {i}"},
            )
            storage.append_entry(
                entry_type="assistant",
                message={"role": "assistant", "content": f"回答 {i}"},
            )

        # 触发压缩
        parent_uuid = storage._get_last_uuid()
        boundary_uuid = storage.add_compact_boundary(
            parent_uuid=parent_uuid,
            trigger="auto",
            pre_tokens=1000,
            messages_summarized=10,
        )
        summary_uuid = storage.add_summary(
            summary="[压缩摘要内容] 1. 用户目标：... 2. 关键决策：... 3. 当前状态：...",
            tokens_saved=5000,
            parent_uuid=boundary_uuid,
        )
        # 压缩后保留 1 条 + 1 条新对话
        storage.append_entry(
            entry_type="user",
            message={"role": "user", "content": "压缩后第一个问题"},
        )
        storage.append_entry(
            entry_type="assistant",
            message={"role": "assistant", "content": "压缩后第一个回答"},
        )

        # 刷新到文件
        storage.flush()

        # ── 测试：模拟 UI 加载逻辑（修复后用 storage 内置 API） ──
        loaded = []
        # ✅ 修复：include_compact_summary=False 在 storage 层就过滤掉
        for entry in storage.get_messages(include_compact_summary=False):
            msg = entry.get("message", {})
            if not msg:
                continue
            role = msg.get("role", "")
            content = str(msg.get("content", ""))
            if role == "user":
                loaded.append(("user", content[:30]))
            elif role == "assistant":
                loaded.append(("assistant", content[:30]))

        # ── 验证 ──
        print("=" * 60)
        print("📋 加载到主聊天区的消息（应跳过压缩摘要）")
        print("=" * 60)
        for role, content in loaded:
            print(f"  {role}: {content}")

        # ── 断言 ──
        # 不应有"压缩摘要内容"出现
        has_summary_in_loaded = any("压缩摘要" in c for _, c in loaded)
        assert not has_summary_in_loaded, \
            f"❌ Bug 复现：压缩摘要被错误加载到主聊天区！loaded={loaded}"

        # stop_at_boundary=True 默认值：只在 boundary 之后开始读
        # 期望加载 2 条（压缩后 user + assistant）
        assert len(loaded) == 2, \
            f"❌ 加载数量不对：期望 2 条（boundary 之后），实际 {len(loaded)} 条"

        # 摘要消息确实在文件里（持久化 OK）
        all_entries = storage.read_entries(include_compact_boundary=True)
        has_summary_in_file = any(
            e.get("uuid") == summary_uuid
            and e.get("message", {}).get("isCompactSummary") is True
            for e in all_entries
        )
        assert has_summary_in_file, \
            "❌ 压缩摘要没被持久化到文件！"

        # stop_at_boundary=True 默认值：只在 boundary 之后开始读
        # 期望加载 2 条（压缩后 user + assistant）
        assert len(loaded) == 2, \
            f"❌ 加载数量不对：期望 2 条（boundary 之后），实际 {len(loaded)} 条"

        # 对比：如果 include_compact_summary=True 会返回 3 条（含摘要）
        all_loaded_count = sum(
            1 for e in storage.get_messages(include_compact_summary=True)
            if e.get("message", {}).get("role") in ("user", "assistant")
        )
        assert all_loaded_count == 3, \
            f"❌ include_compact_summary=True 时应返回 3 条（含摘要），实际 {all_loaded_count}"

        print()
        print("=" * 60)
        print("✅ 测试通过")
        print("=" * 60)
        print(f"  压缩摘要已持久化到文件（uuid={summary_uuid[:8]}）")
        print(f"  压缩摘要**未**加载到主聊天区")
        print(f"  主聊天区加载了 {len(loaded)} 条普通对话（boundary 之后）")
        print(f"  include_compact_summary=True 时会加载 3 条（含摘要）")
        print(f"  → UI 用 False 参数能正确过滤掉压缩摘要")


if __name__ == "__main__":
    try:
        main()
        sys.exit(0)
    except AssertionError as e:
        print(f"\n❌ 测试失败: {e}")
        sys.exit(1)