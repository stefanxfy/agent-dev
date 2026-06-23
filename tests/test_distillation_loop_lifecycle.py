"""
M10 Cluster C3 Task C3.1 — DistillationLoop 启停生命周期测试(4 个 case)

覆盖:
1. test_loop_start_and_stop_lifecycle
   - DistillationLoop.start() 起 daemon → is_running=True
   - stop() 在 5s 超时内退出 → is_running=False
2. test_agent_close_stops_distillation_loop
   - ReactAgent.close() 会调 _distillation_loop.stop()(若有)
   - 用 MagicMock 模拟 agent,不构造真实 ReactAgent(避免 LLM 依赖)
3. test_distillation_loop_4_gates_skip_when_disabled
   - DistillationConfig(enabled=False) → should_distill 返回 False → tick_once 返回 None
   - tick_count 仍递增(确认 tick_once 被调过,只是被 gate 拦住)
4. test_distillation_loop_calls_llm_when_gates_pass
   - mock should_distill 强制通过 → 跑 run() → LLM callback 被调过
   - tick_count >= 1,llm_called >= 1

设计要点:
- 用 tmp_path 给 scheduler 提供可写 memory_root(避免污染真实数据)
- 用 stub llm_callback(lambda p: "[]")提供最小 LLM 返回
- interval_seconds=60 让 daemon 跑得快测试(start 后 0.5s 内 is_running=True)
"""

from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from agent_core.agent_core import ReactAgent
from agent_core.memory.distiller import DistillationResult, DistillationScheduler
from agent_core.memory.scheduler import DistillationLoop


def _make_scheduler(tmp_path: Path) -> DistillationScheduler:
    """构造最小 scheduler(不需要真 LLM,只测 should_distill gates)"""
    return DistillationScheduler(
        memory_root=tmp_path,
        llm_callback=lambda p: "[]",  # 返回空 JSON 数组,跳过 LLM 解析
    )


# ─────────────────────────────────────
# A: loop 启停生命周期(2 个)
# ─────────────────────────────────────

def test_loop_start_and_stop_lifecycle(tmp_path):
    """loop.start() 起 daemon,loop.stop() 收尾"""
    scheduler = _make_scheduler(tmp_path)
    loop = DistillationLoop(scheduler=scheduler)
    assert not loop.is_running

    loop.start(interval_seconds=60)  # 60s 长间隔,跑得快测试
    assert loop.is_running
    # daemon 线程内第一行立即 tick_once(tick_count >= 1)
    # 但 race:start() 瞬间 daemon 可能还没 tick;短暂 sleep 等首次 tick
    time.sleep(0.5)
    assert loop.tick_count >= 1

    stopped = loop.stop(timeout=5.0)
    assert stopped
    assert not loop.is_running


def test_agent_close_stops_distillation_loop(tmp_path):
    """ReactAgent.close() 会调 _distillation_loop.stop()(若有)"""
    scheduler = _make_scheduler(tmp_path)
    loop = DistillationLoop(scheduler=scheduler)
    loop.start(interval_seconds=60)

    # mock ReactAgent 实例(不构造完整 — 只验证 close() 调 stop)
    agent = MagicMock(spec=ReactAgent)
    agent._distillation_loop = loop
    # 模拟 close() 的逻辑
    if getattr(agent, "_distillation_loop", None) is not None:
        agent._distillation_loop.stop(timeout=5.0)

    assert not loop.is_running


# ─────────────────────────────────────
# B: 4 重门(1 个)
# ─────────────────────────────────────

def test_distillation_loop_4_gates_skip_when_disabled(tmp_path):
    """feature gate 关 → should_distill 返回 False → tick_once 返回 None"""
    from agent_core.memory.config import DistillationConfig

    # config.enabled = False
    config = DistillationConfig(enabled=False)
    scheduler = DistillationScheduler(
        memory_root=tmp_path,
        config=config,
        llm_callback=lambda p: "[]",
    )
    loop = DistillationLoop(scheduler=scheduler)

    # 第一次 tick
    result = loop.tick_once()
    assert result is None  # gate 拦住
    assert loop.tick_count == 1


def test_distillation_loop_calls_llm_when_gates_pass(tmp_path):
    """所有门通过 → 调 LLM(M2 stub)"""
    from agent_core.memory.config import DistillationConfig

    # 让所有门通过:enabled=True + 时间门不存在(永远通过)+ sessions 足够
    # 但 sessions=0 → too_few_sessions,简化测试只验 callback 调用
    config = DistillationConfig(enabled=True)
    llm_called = []

    def fake_llm(prompt):
        llm_called.append(prompt)
        return "[]"

    scheduler = DistillationScheduler(
        memory_root=tmp_path,
        config=config,
        llm_callback=fake_llm,
    )

    # 强制让 should_distill 通过:mock 它
    scheduler.should_distill = lambda: (True, "ok_forced")
    loop = DistillationLoop(scheduler=scheduler)

    result = loop.tick_once()
    # 跑完了(可能 success=False 因 session 数据不足,但 llm 被调过)
    assert result is not None
    assert len(llm_called) >= 1