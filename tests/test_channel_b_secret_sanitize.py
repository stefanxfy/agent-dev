"""
M10 C1.2: §14.4 Channel B secret sanitize 测试

覆盖(brief 指定 4 个):
- SecretScanner.redact() 替换单 secret
- SecretScanner.redact() 无命中返回原文本
- SecretScanner.redact() 多命中都 redact
- Channel B 集成:写盘前 redact(落盘不含原 secret)

依赖:
- chromadb / bge-m3 真嵌入(与其他 dual_channel 测试一致)
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from agent_core.memory import (
    DualChannelWriter,
    MemoryStore,
    ExtractionCandidate,
    TurnMessage,
)
from agent_core.memory.secret_scanner import SecretScanner, redact_text


# ─────────────────────────────────────
# A: SecretScanner.redact 单测(3 个)
# ─────────────────────────────────────

def test_redact_replaces_openai_sk_in_text():
    """L4: sk- 开头 token 替换为 [REDACTED:openai_sk]"""
    text = "My key is sk-1234567890abcdefghij please ignore"
    scanner = SecretScanner()
    redacted = scanner.redact(text)
    assert "sk-1234567890abcdefghij" not in redacted
    assert "[REDACTED:openai_sk]" in redacted


def test_redact_returns_unchanged_when_clean():
    """无 secret 返回原文本"""
    text = "I love python and coffee"
    scanner = SecretScanner()
    assert scanner.redact(text) == text


def test_redact_handles_multiple_hits():
    """多个 secret 在同一文本中都被 redact"""
    text = (
        "openai: sk-aaaaaaaaaaaaaaaaaaaa "
        "and github: ghp_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
    )
    scanner = SecretScanner()
    redacted = scanner.redact(text)
    assert "sk-aaaaaaaaaaaaaaaaaaaa" not in redacted
    assert "ghp_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx" not in redacted
    assert redacted.count("[REDACTED:") == 2


# ─────────────────────────────────────
# B: Channel B 集成(1 个)
# ─────────────────────────────────────

def _make_writer(tmp_path, event_callback=None):
    """构造 DualChannelWriter 集成测试 helper"""
    from agent_core.memory.meta_db import MetaDB

    store = MemoryStore(tmp_path / "memory")
    meta_db = MetaDB(tmp_path / "meta.db")
    # mock vector store + embed_fn
    vec = MagicMock()
    vec.add = MagicMock()
    vec.count = MagicMock(return_value=0)
    embed_fn = MagicMock()
    embed_fn.encode = MagicMock(return_value=[0.0] * 1024)
    return DualChannelWriter(
        session_id="test_sess",
        meta_db=meta_db,
        memory_store=store,
        vector_store=vec,
        embed_fn=embed_fn,
        event_callback=event_callback,
    )


def test_channel_b_sanitizes_secret_before_write(tmp_path):
    """Channel B 写盘前必须 redact secret"""
    writer = _make_writer(tmp_path)
    # 先写 channel A 触发 daily_cursor 推进,让 channel B 处理范围生效
    writer.channel_a_inline_write("hi", "hello", turn_index=0)
    cand = ExtractionCandidate(
        title="api key",
        body="use sk-1234567890abcdefghij please",
        type="user",
        source_quote="sk-1234567890abcdefghij",
    )

    def extractor(_):
        return [cand]

    # turn_index 必须 ≤ daily_cursor(=0 after channel_a turn 0)
    result = writer._do_channel_b_extract(
        messages=[TurnMessage(0, "test", "test")],
        extractor=extractor,
    )
    assert result["written"] == 1
    # 验:落盘 md 不含原始 secret
    md_files = list((tmp_path / "memory").rglob("*.md"))
    assert len(md_files) == 1
    content = md_files[0].read_text()
    assert "sk-1234567890abcdefghij" not in content
    assert "[REDACTED:openai_sk]" in content


# ─────────────────────────────────────
# C: PathValidator pre-flight(2 个)
# ─────────────────────────────────────

def test_channel_b_rejects_unknown_type(tmp_path):
    """Channel B 入口拒绝未知 type(不在 4 类白名单)"""
    from agent_core.memory.path_validator import PathSecurityError

    writer = _make_writer(tmp_path)
    # 先写 channel A 触发 daily_cursor 推进到 1,让 channel B 处理范围包含 turn_index=1
    writer.channel_a_inline_write("hi", "hello", turn_index=1)
    cand = ExtractionCandidate(
        title="bogus",
        body="normal content",
        type="../../etc",  # 路径穿越 type
        source_quote="normal",
    )
    def extractor(_):
        return [cand]

    result = writer._do_channel_b_extract(
        messages=[TurnMessage(1, "test", "test")],
        extractor=extractor,
    )
    # 应被 pre-flight 拦截
    assert result.get("written", 0) == 0
    assert result.get("skipped", 0) == 1
    # 验:没有任何 md 落盘
    md_files = list((tmp_path / "memory").rglob("*.md"))
    assert len(md_files) == 0


def test_channel_b_rejects_unicode_null_in_type(tmp_path):
    """Channel B 入口拒绝 type 含 \\x00(unicode trick)"""
    writer = _make_writer(tmp_path)
    # 先写 channel A 触发 daily_cursor 推进到 1,让 channel B 处理范围包含 turn_index=1
    writer.channel_a_inline_write("hi", "hello", turn_index=1)
    cand = ExtractionCandidate(
        title="obfuscated",
        body="normal content",
        type="user\x00",
        source_quote="normal",
    )
    def extractor(_):
        return [cand]

    result = writer._do_channel_b_extract(
        messages=[TurnMessage(1, "test", "test")],
        extractor=extractor,
    )
    assert result.get("written", 0) == 0
    assert result.get("skipped", 0) == 1