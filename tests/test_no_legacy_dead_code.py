"""
Phase 4 / Step 4.4.2-4.4.6 bundled — 死代码清理断言

合并测试,因为这些步骤互锁:删任意一个会留 dangling 调用,
所以一次删完。

被删项:
- MetaDB 7 个旧表方法:set_cursor / get_cursor / add_pending / remove_pending /
  bump_pending_attempts / update_pending_payload / list_pending
- DualChannelWriter.persist_turn 不再调 set_cursor
- DualChannelWriter._do_persist_turn_write 整个方法删
- DualChannelWriter._do_extract_candidates 不再调 add_pending / remove_pending /
  bump_pending_attempts / set_cursor("extract", ...)
- DualChannelWriter.recover_pending 整个方法删
- DualChannelWriter._load_messages_for_retry / _on_recovery_done 删
- DualChannelWriter 不再 import make_daily_lock / make_extract_lock
  (但 IPCLock 类本身仍被 distiller 用 — 不动)
- DualChannelWriter.daily_cursor / extract_cursor 属性删
  (extract_cursor 本来就是 _do_extract_candidates 局部用)
- DualChannelWriter.extract_candidates 删 advance_cursor 参数
  (recovery 路径已无)
"""
import inspect

import pytest


class TestMetaDBLegacyMethodsGone:
    """Step 4.4.2:MetaDB 不再含 7 个旧表方法"""

    @pytest.mark.parametrize("method_name", [
        "set_cursor", "get_cursor",
        "add_pending", "remove_pending",
        "bump_pending_attempts", "update_pending_payload",
        "list_pending",
    ])
    def test_method_removed(self, method_name):
        from agent_core.memory.meta_db import MetaDB
        assert not hasattr(MetaDB, method_name), (
            f"MetaDB.{method_name} 应在 Phase 4 删,"
            f"但仍存在"
        )

    def test_meta_db_docstring_no_longer_shows_cursor_pending(self):
        """类 docstring 不再展示已删方法的示例"""
        from agent_core.memory.meta_db import MetaDB
        doc = inspect.getdoc(MetaDB) or ""
        assert "set_cursor" not in doc
        assert "add_pending" not in doc
        assert "remove_pending" not in doc


class TestDualChannelWriterNoLegacyIO:
    """Step 4.4.3 + 4.4.4 + 4.4.5:persist_turn/B 旧 IO 全清"""

    def test_persist_turn_no_set_cursor(self):
        """persist_turn 源码不再调 set_cursor"""
        from agent_core.memory.dual_channel_writer import DualChannelWriter
        src = inspect.getsource(DualChannelWriter.persist_turn)
        assert "set_cursor" not in src, (
            "persist_turn 不应再调 set_cursor"
        )

    def test_do_persist_turn_write_method_gone(self):
        """_do_persist_turn_write 已删(写 JSONL 的旧方法)"""
        from agent_core.memory.dual_channel_writer import DualChannelWriter
        assert not hasattr(DualChannelWriter, "_do_persist_turn_write"), (
            "_do_persist_turn_write 应在 Phase 4 删"
        )

    def test_do_extract_candidates_no_legacy_io(self):
        """_do_extract_candidates 源码不再调 add_pending / remove_pending /
        bump_pending_attempts / set_cursor"""
        from agent_core.memory.dual_channel_writer import DualChannelWriter
        src = inspect.getsource(DualChannelWriter._do_extract_candidates)
        for legacy in (
            "add_pending", "remove_pending",
            "bump_pending_attempts", "set_cursor",
        ):
            assert legacy not in src, (
                f"_do_extract_candidates 不应再调 {legacy}"
            )

    def test_advance_cursor_param_gone(self):
        """extract_candidates 不再有 advance_cursor 参数"""
        from agent_core.memory.dual_channel_writer import DualChannelWriter
        sig = inspect.signature(DualChannelWriter.extract_candidates)
        assert "advance_cursor" not in sig.parameters, (
            "advance_cursor 参数应随 recover_pending 一起删"
        )

    def test_recover_pending_gone(self):
        from agent_core.memory.dual_channel_writer import DualChannelWriter
        assert not hasattr(DualChannelWriter, "recover_pending"), (
            "recover_pending 应在 Phase 4 删(startup_scan 替代)"
        )

    def test_load_messages_for_retry_gone(self):
        from agent_core.memory.dual_channel_writer import DualChannelWriter
        assert not hasattr(DualChannelWriter, "_load_messages_for_retry"), (
            "_load_messages_for_retry 是 recover_pending 的辅助方法"
        )

    def test_on_recovery_done_gone(self):
        from agent_core.memory.dual_channel_writer import DualChannelWriter
        assert not hasattr(DualChannelWriter, "_on_recovery_done"), (
            "_on_recovery_done 是 recover_pending 的回调"
        )


class TestDualChannelWriterNoLegacyLocks:
    """Step 4.4.6:DualChannelWriter 不再 import daily/extract lock"""

    def test_no_make_daily_lock_import(self):
        from agent_core.memory import dual_channel_writer
        mod_src = inspect.getsource(dual_channel_writer)
        assert "make_daily_lock" not in mod_src
        assert "make_extract_lock" not in mod_src

    def test_no_ipc_lock_attributes(self):
        """writer 实例不应再有 _ipc_daily / _ipc_extract 属性"""
        from agent_core.memory.dual_channel_writer import DualChannelWriter
        src = inspect.getsource(DualChannelWriter.__init__)
        assert "_ipc_daily" not in src
        assert "_ipc_extract" not in src


class TestDualChannelWriterCursorsGone:
    """daily_cursor / extract_cursor 属性删
    (cursors 表已 DROP,persist_turn 走 max(turn_index) 派生)"""

    def test_no_daily_cursor_attribute(self):
        from agent_core.memory.dual_channel_writer import DualChannelWriter
        # __init__ 源码不含 self.daily_cursor 赋值
        src = inspect.getsource(DualChannelWriter.__init__)
        assert "self.daily_cursor" not in src
        assert "self.extract_cursor" not in src