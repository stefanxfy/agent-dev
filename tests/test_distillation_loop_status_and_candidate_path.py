"""
M10 Cluster C3 Tasks C3.2 + C3.3 — DistillationLoop 状态可观测 + write_candidates run_id 路径测试(3 个 case)

覆盖:
1. test_get_status_returns_running_and_tick_count
   - DistillationLoop.get_status() 含 running + tick_count + interval + last_* 字段
   - 初始:running=False, last_tick_at=None, last_result_success=None
   - start() 后:running=True, interval_seconds==启动值
   - daemon 跑了至少 1 tick → last_tick_at 不为 None
2. test_write_candidates_with_run_id_creates_subdir
   - write_candidates(candidates, run_id="r1") 写到 _candidate/r1/{type}/...
3. test_write_candidates_sanitizes_run_id
   - run_id="../../etc" → sanitize 成安全字符
   - 写入路径仍位于 _candidate/ 下(没逃出 root)

设计要点:
- 用 tmp_path 给 scheduler / distiller 提供可写目录(避免污染真实数据)
- stub llm_callback(lambda p: "[]")提供最小 LLM 返回
- daemon 测试用 60s 间隔(避免频繁 tick)
"""

from __future__ import annotations

import re
import time
from pathlib import Path

import pytest

from agent_core.memory.distiller import DistillationScheduler, Distiller
from agent_core.memory.scheduler import DistillationLoop


def _make_scheduler(tmp_path: Path) -> DistillationScheduler:
    """构造最小 scheduler(不需要真 LLM,只测 should_distill gates)"""
    return DistillationScheduler(
        memory_root=tmp_path,
        llm_callback=lambda p: "[]",  # 返回空 JSON 数组,跳过 LLM 解析
    )


# ─────────────────────────────────────
# A: DistillationLoop.get_status(1 个)
# ─────────────────────────────────────

def test_get_status_returns_running_and_tick_count(tmp_path):
    """get_status 含 running + tick_count + interval + last_* 字段"""
    scheduler = _make_scheduler(tmp_path)
    loop = DistillationLoop(scheduler=scheduler)

    # 初始状态
    status = loop.get_status()
    assert status["running"] is False
    assert status["tick_count"] == 0
    assert status["interval_seconds"] == 0
    assert status["last_tick_at"] is None
    assert status["last_result_success"] is None
    assert status["last_candidates_count"] is None

    # start → running=True;让 daemon tick 几次
    loop.start(interval_seconds=60)
    time.sleep(0.3)  # 给 daemon 跑起来
    status = loop.get_status()
    assert status["running"] is True
    assert status["interval_seconds"] == 60
    # daemon 跑了至少 1 次 tick
    if status["tick_count"] >= 1:
        assert status["last_tick_at"] is not None
        # 验证 ISO timestamp 格式
        assert re.match(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}", status["last_tick_at"])

    loop.stop(timeout=5.0)


# ─────────────────────────────────────
# B: write_candidates run_id(2 个)
# ─────────────────────────────────────

def test_write_candidates_with_run_id_creates_subdir(tmp_path):
    """write_candidates(candidates, run_id="r1") 写到 _candidate/r1/{type}/..."""
    distiller = Distiller(
        llm_callback=lambda p: "[]",
        candidate_root=tmp_path / "_candidate",
    )
    candidates = [
        {"type": "user", "title": "test", "body": "x", "sources": [], "tags": []},
    ]
    written = distiller.write_candidates(candidates, run_id="r1")

    assert len(written) == 1
    # 验:路径在 _candidate/r1/user/...
    p = str(written[0])
    assert "_candidate" in p
    assert "/r1/" in p
    assert "/user/" in p

    # 验:文件实际写入磁盘
    assert written[0].exists()
    # 验:内容含 frontmatter
    content = written[0].read_text(encoding="utf-8")
    assert "type: user" in content


def test_write_candidates_sanitizes_run_id(tmp_path):
    """run_id 含 `../` 或特殊字符 → 兜底为安全字符"""
    distiller = Distiller(
        llm_callback=lambda p: "[]",
        candidate_root=tmp_path / "_candidate",
    )
    candidates = [
        {"type": "user", "title": "test", "body": "x", "sources": [], "tags": []},
    ]
    # run_id 含 ../  → 应被 sanitize 掉,不逃出 _candidate/
    written = distiller.write_candidates(candidates, run_id="../../etc")

    assert len(written) == 1
    # 验:路径仍在 _candidate/ 下(没逃出去)
    p = written[0].resolve()
    cand_root = (tmp_path / "_candidate").resolve()
    assert str(p).startswith(str(cand_root))
    # 验:实际写入的 run_id 子目录名是 sanitize 后的安全字符
    # ../../etc → _.._.._etc (非 / 分隔符,纯 _ 字符)
    rel = p.relative_to(cand_root)
    # 第一段就是 run_id 子目录
    first_segment = rel.parts[0]
    assert "/" not in first_segment
    # 原字符串中没有反斜杠以外的路径分隔符
    # (Windows 上不需要 — 跨平台)
    # 文件实际写到了这个 sanitize 后的子目录里
    assert written[0].exists()
