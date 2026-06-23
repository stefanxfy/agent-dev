"""
M10 C2.2 + C2.3 测试 —— SM 持久化 + distiller L4 输入

覆盖（v2.1 §4.4 + §7）:
1. C2.2: compact() 写 .json snapshot(含 summary + tokens + timestamp)
2. C2.3: distill() prompt 含 SM 摘要(当 sm_dir 设置)
3. C2.3: distill() prompt 不读 SM(当 sm_dir = None,向后兼容)
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_core.memory.config import MemoryConfig
from agent_core.memory.distiller import Distiller
from agent_core.memory.sm_layer import SessionMemoryLayer


# ─────────────────────────────────────
# A: C2.2 SM 持久化(1 个)
# ─────────────────────────────────────

def test_compact_writes_json_snapshot(tmp_path):
    """compact() 末尾写 .json snapshot,含 summary + tokens + timestamp"""
    sm_md = tmp_path / "sm.md"
    # 先写一个有内容的 SM(非 template)
    sm_md.write_text("""---
session_id: test_sess
schema_version: 1
last_compacted_msg_id: null
last_compacted_at: null
---

# Session Memory

## Context
user wants X
""", encoding="utf-8")

    sm = SessionMemoryLayer(
        session_id="test_sess",
        sm_path=sm_md,
        config=MemoryConfig().compact,
    )

    # 调 compact
    messages = [{"role": "user", "content": f"msg {i}"} for i in range(5)]
    # 设置 last_compacted_msg_id 让 _slice_kept_messages 有行为
    sm._last_compacted_msg_id = None  # 默认,全保留

    result = sm.compact(messages, context_window=128000)
    assert result is not None
    assert result.strategy == "sm_compact"

    # 验:json 文件已写
    json_path = sm_md.with_suffix(".json")
    assert json_path.exists()
    data = json.loads(json_path.read_text(encoding="utf-8"))
    assert data["session_id"] == "test_sess"
    assert data["strategy"] == "sm_compact"
    assert data["used_tokens_estimate"] > 0
    assert "updated_at" in data
    assert "[Session memory summary]" in data["summary_message_content"]


# ─────────────────────────────────────
# B: C2.3 distiller 读 SM(2 个)
# ─────────────────────────────────────

def test_distill_prompt_includes_sm_when_sm_dir_set(tmp_path):
    """sm_dir 不为 None 时,prompt 含 SM 摘要"""
    # 准备 SM .json 文件
    sm_dir = tmp_path / "sm"
    sm_dir.mkdir()
    (sm_dir / "s1.json").write_text(json.dumps({
        "session_id": "s1",
        "summary_message_content": "用户希望实现 X 功能,选了 Z 方案",
        "used_tokens_estimate": 500,
        "updated_at": "2026-06-23T10:00:00+00:00",
    }, ensure_ascii=False), encoding="utf-8")

    # mock LLM
    captured_prompts = []
    def fake_llm(prompt):
        captured_prompts.append(prompt)
        return "[]"  # 空结果

    distiller = Distiller(
        llm_callback=fake_llm,
        candidate_root=tmp_path / "_candidate",
        sm_dir=sm_dir,
    )
    candidates = distiller.distill(
        session_log_files=[],
        existing_memories=[],
    )

    assert len(captured_prompts) == 1
    prompt = captured_prompts[0]
    assert "s1" in prompt
    assert "用户希望实现 X 功能" in prompt
    assert "跨会话 SM 摘要" in prompt


def test_distillation_scheduler_threads_sm_dir_to_distiller(tmp_path, monkeypatch):
    """M10 C7.1 final review fix: DistillationScheduler.run()
    必须把 sm_dir 透传给 Distiller(L4 cross-session SM 输入才能在生产路径生效)
    """
    from unittest.mock import MagicMock, patch
    from agent_core.memory.distiller import DistillationScheduler

    monkeypatch.setattr("agent_core.memory.distiller.Distiller", MagicMock())

    scheduler = DistillationScheduler(
        memory_root=tmp_path,
        llm_callback=lambda p: "[]",
    )
    # 触发 run() 的最小路径:门检查/lock/scan 都 mock 掉,直接到构造 Distiller
    with patch.object(scheduler, "should_distill", return_value=(True, "ok")):
        with patch.object(scheduler, "_acquire_lock", return_value=0):
            with patch.object(scheduler, "_scan_session_logs", return_value=[]):
                with patch.object(scheduler, "_read_existing_memories", return_value=[]):
                    with patch.object(scheduler, "_release_lock", return_value=None):
                        scheduler.run(dry_run=True)

    # 验:Distiller 构造时传了 sm_dir kwarg
    from agent_core.memory.distiller import Distiller as MockDist
    call_kwargs = MockDist.call_args.kwargs
    assert "sm_dir" in call_kwargs
    expected = tmp_path / "sm"
    assert call_kwargs["sm_dir"] == expected


def test_distill_skips_sm_when_sm_dir_none(tmp_path):
    """sm_dir 为 None 时,prompt 不含 SM 段(或显式标记"(无)")"""
    captured_prompts = []
    def fake_llm(prompt):
        captured_prompts.append(prompt)
        return "[]"

    distiller = Distiller(
        llm_callback=fake_llm,
        candidate_root=tmp_path / "_candidate",
        sm_dir=None,  # 不传
    )
    distiller.distill(session_log_files=[], existing_memories=[])

    assert len(captured_prompts) == 1
    prompt = captured_prompts[0]
    # 没 SM → 显示占位符
    assert "无跨会话 SM" in prompt
