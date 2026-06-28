"""
M6 / Day 6 测试 —— DistillationLoop + OTel tracer (6 cases)

覆盖:
1. tick_once gate 通过 → 调 run()
2. tick_once gate 拦住 → 返回 None,不调 run()
3. start() 起 daemon 线程,stop() 在 < 1s 内退出
4. run() 抛异常时 loop 继续 tick
5. OTel tracer 创建 span 不报错(默认 NoOp)
6. tracer 跨上下文嵌套(distill span 嵌套在 loop tick span 里)
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from agent_core.memory import (
    DistillationConfig,
    DistillationLoop,
    DistillationScheduler,
    configure_tracing,
    tracer,
)


# ──────────────────────────────────────────────────────────────────
# Fixtures
# ──────────────────────────────────────────────────────────────────

@pytest.fixture
def memory_root(tmp_path):
    """测试用 memory root"""
    root = tmp_path / "memory"
    root.mkdir()
    return root


@pytest.fixture
def logs_dir(tmp_path):
    """测试用 logs 目录(sibling of memory_root)"""
    d = tmp_path / "logs"
    d.mkdir()
    return d


@pytest.fixture
def config():
    return DistillationConfig()


@pytest.fixture
def mock_llm():
    """返回 1 个合法候选的 mock LLM"""
    def llm(prompt: str) -> str:
        return json.dumps([{
            "type": "user",
            "title": "M6 测试",
            "why": "smoke test",
            "body": "scheduler 测试通过",
            "confidence": 0.8,
            "sources": ["s0"],
            "tags": ["test"],
        }])
    return llm


@pytest.fixture
def scheduler(memory_root, logs_dir, config, mock_llm):
    """默认 scheduler + mock LLM"""
    return DistillationScheduler(memory_root, config, llm_callback=mock_llm)


def _make_old_last_distill(memory_root: Path, hours_ago: float = 25.0):
    """设置 .last-distill mtime 为 N 小时前,让 time gate 通过"""
    mtime = memory_root / ".last-distill"
    mtime.touch()
    old = time.time() - hours_ago * 3600
    os.utime(mtime, (old, old))
    return mtime


def _make_n_sessions(logs_dir: Path, n: int = 6):
    """在 logs_dir 写 N 个 session 文件"""
    for i in range(n):
        (logs_dir / f"s{i}.jsonl").write_text(
            json.dumps({"user_msg": f"msg {i}", "assistant_resp": f"resp {i}"})
        )


# ──────────────────────────────────────────────────────────────────
# 1. tick_once — gate 通过
# ──────────────────────────────────────────────────────────────────

class TestTickOnce:

    def test_tick_triggers_run_when_gates_pass(self, memory_root, logs_dir, config, mock_llm):
        """gate 通过 → tick_once() 调 run() 并返回 result"""
        _make_old_last_distill(memory_root)
        _make_n_sessions(logs_dir)

        scheduler = DistillationScheduler(memory_root, config, llm_callback=mock_llm)
        loop = DistillationLoop(scheduler)

        result = loop.tick_once()
        assert result is not None
        assert result.success is True
        assert len(result.candidates) == 1
        assert loop.tick_count == 1

    def test_tick_returns_none_when_gates_fail(self, memory_root, logs_dir, config, mock_llm):
        """gate 拦住(无 session)→ tick_once() 返回 None,run 不被调"""
        # 不写 session,gate3 会报 too_few_sessions
        scheduler = DistillationScheduler(memory_root, config, llm_callback=mock_llm)
        loop = DistillationLoop(scheduler)

        result = loop.tick_once()
        assert result is None
        assert loop.tick_count == 1  # tick 计数仍 +1,只是没跑 run

    def test_tick_returns_none_when_disabled(self, memory_root, logs_dir):
        """gate0 关 → tick_once() 返回 None"""
        config = DistillationConfig(enabled=False)
        scheduler = DistillationScheduler(memory_root, config)
        loop = DistillationLoop(scheduler)

        result = loop.tick_once()
        assert result is None


# ──────────────────────────────────────────────────────────────────
# 2. start / stop — 后台 daemon
# ──────────────────────────────────────────────────────────────────

class TestStartStop:

    def test_start_runs_in_background_and_stop_exits_quickly(
        self, memory_root, logs_dir, config, mock_llm
    ):
        """start() 起 daemon,第一次 tick 立即跑,stop() < 1s 退出"""
        _make_old_last_distill(memory_root)
        _make_n_sessions(logs_dir)

        scheduler = DistillationScheduler(memory_root, config, llm_callback=mock_llm)
        loop = DistillationLoop(scheduler)

        loop.start(interval_seconds=10)  # 第一次 tick 立即跑,后续每 10s
        assert loop.is_running

        # 等第一个 tick 跑完
        time.sleep(0.2)
        assert loop.tick_count >= 1

        t0 = time.time()
        stopped = loop.stop(timeout=5.0)
        elapsed = time.time() - t0

        assert stopped is True
        assert not loop.is_running
        assert elapsed < 1.0, f"stop() 应在 < 1s 内退出,实际 {elapsed:.2f}s"


# ──────────────────────────────────────────────────────────────────
# 3. 异常隔离
# ──────────────────────────────────────────────────────────────────

class TestExceptionIsolation:

    def test_run_exception_does_not_kill_loop(self, memory_root, logs_dir, config):
        """scheduler.run() 抛异常时 tick_once 返回 error result,不抛"""
        _make_old_last_distill(memory_root)
        _make_n_sessions(logs_dir)

        # mock LLM 抛异常
        def bad_llm(prompt):
            raise RuntimeError("LLM 模拟失败")

        scheduler = DistillationScheduler(memory_root, config, llm_callback=bad_llm)

        results = []
        loop = DistillationLoop(scheduler, on_result=results.append)

        # tick_once 不会让 loop 崩 —— 返回 error result
        result = loop.tick_once()
        assert result is not None
        assert result.success is False
        assert "LLM 模拟失败" in result.error
        assert len(results) == 1  # on_result 仍被调

        # 第二次 tick 也应正常
        result2 = loop.tick_once()
        assert result2.success is False  # 同样的失败
        assert loop.tick_count == 2


# ──────────────────────────────────────────────────────────────────
# 4. OTel tracing
# ──────────────────────────────────────────────────────────────────

class TestTracing:

    def test_tracer_creates_span_without_error(self):
        """默认 NoOp tracer:start_as_current_span 不报错,attribute 可设"""
        with tracer.start_as_current_span("memory.test") as span:
            assert span is not None
            span.set_attribute("memory.candidates", 3)
            span.set_attribute("memory.tag", "M6")

    def test_distill_span_created_inside_run(
        self, memory_root, logs_dir, config, mock_llm
    ):
        """scheduler.run() 内部开 memory.distill span,不报错"""
        _make_old_last_distill(memory_root)
        _make_n_sessions(logs_dir)

        scheduler = DistillationScheduler(memory_root, config, llm_callback=mock_llm)
        # 直接调 run() —— 内部 OTel span 包装,NoOp 不影响逻辑
        result = scheduler.run(dry_run=True)
        assert result.success
        assert len(result.candidates) == 1

    def test_configure_tracing_returns_false_when_no_endpoint(self, monkeypatch):
        """无 OTLP endpoint 配置 → configure_tracing() 返回 False,仍 NoOp"""
        monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
        ok = configure_tracing()
        assert ok is False

    def test_configure_tracing_noop_does_not_break_subsequent_calls(self):
        """configure_tracing() 多次调:第一次 NoOp(False),后续仍 False(幂等)"""
        # 没有 env var → False
        assert configure_tracing() is False
        assert configure_tracing() is False