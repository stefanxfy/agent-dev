"""
M5 / Day 5 测试 —— DistillationScheduler (autoDream, L5)

覆盖（v2.1 §7.1 + IMPLEMENTATION_PLAN §Day 5 = 6 个核心 case）:
1. 四重门(gate / time / throttle / sessions)
2. 锁原子创建 (A1, O_EXCL)
3. 锁强占语义 (A1+A2, PID 已死 + mtime 超时)
4. JSON envelope 校验 (A11)
5. 失败回滚 mtime (A2)
6. 核心蒸馏逻辑 (dry_run / write_candidates)

总计: 12 个 case(超出 plan 6 个最低要求,含边界)
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
    DistillationResult,
    Distiller,
    DistillationScheduler,
)


# ──────────────────────────────────────────────────────────────────
# Fixtures
# ──────────────────────────────────────────────────────────────────

@pytest.fixture
def memory_root(tmp_path):
    """测试用 memory root(空,M11.6 改:门3 改 .md 数量门,空目录 → too_few_memories)"""
    root = tmp_path / "memory"
    root.mkdir()
    return root


@pytest.fixture
def populated_memory_root(tmp_path):
    """M11.6: 构造一个有 6 条 .md 记忆的 memory_root(过门3:>=5)

    写在 4 个 type 目录里各几条,模拟真实记忆库。
    """
    root = tmp_path / "memory"
    (root / "user").mkdir(parents=True)
    (root / "feedback").mkdir(parents=True)
    (root / "project").mkdir(parents=True)

    samples = [
        ("user", "h001", "姓名", "张三"),
        ("user", "h002", "职业", "工程师"),
        ("feedback", "h003", "学习风格", "先原理后 API"),
        ("feedback", "h004", "代码风格", "重视底层"),
        ("project", "h005", "技术栈", "Vite + React"),
        ("project", "h006", "项目背景", "agent-dev"),
    ]
    for type_dir, hash_id, title, body in samples:
        (root / type_dir / f"{hash_id}.md").write_text(
            f"---\ntype: {type_dir}\ntitle: {title}\n---\n\n{body}\n",
            encoding="utf-8",
        )
    return root


@pytest.fixture
def config():
    """默认 DistillationConfig"""
    return DistillationConfig()


@pytest.fixture
def scheduler(populated_memory_root, config):
    """默认 DistillationScheduler(M11.6:基于 populated root)"""
    return DistillationScheduler(populated_memory_root, config, llm_callback=lambda p: "[]")


@pytest.fixture
def mock_llm():
    """返回一个会被调的 LLM,返回合法 JSON 数组"""
    def llm(prompt: str) -> str:
        return json.dumps([
            {
                "type": "user",
                "title": "偏好 Vite",
                "why": "用户多次明确表示",
                "body": "项目用 Vite 不用 CRA",
                "confidence": 0.8,
                "sources": ["s1"],
                "tags": ["preference"],
            },
            {
                "type": "feedback",
                "title": "重视底层原理",
                "why": "学习风格",
                "body": "用户偏好先理解原理再看 API",
                "confidence": 0.7,
                "sources": ["s1"],
                "tags": ["learning"],
            },
        ])
    return llm


# ──────────────────────────────────────────────────────────────────
# 1. 四重门
# ──────────────────────────────────────────────────────────────────

class TestSchedulerGates:

    def test_gate_disabled(self, memory_root):
        """门0: gate 关 → False / gate_disabled"""
        config = DistillationConfig(enabled=False)
        s = DistillationScheduler(memory_root, config)
        ok, reason = s.should_distill()
        assert ok is False
        assert reason == "gate_disabled"

    def test_gate_too_soon_no_lock(self, memory_root):
        """门1: 无锁文件 → busy=False,但 age_hours=inf,通过(进下一门)"""
        s = DistillationScheduler(memory_root, DistillationConfig(), llm_callback=lambda p: "[]")
        # 无锁文件时 age_hours = inf,> min_interval_hours=24h
        ok, reason = s.should_distill()
        # 期待进到门3 (session 计数)
        # 空 memory_root → too_few_memories(0)
        assert ok is False
        assert "too_few_memories" in reason

    def test_gate_too_soon_with_fresh_lock(self, memory_root):
        """门1: .last-distill 存在但很新(< 24h) → too_soon"""
        # .last-distill 写入, mtime = now(< 24h)
        mtime = memory_root / ".last-distill"
        mtime.touch()
        # 注意:此处不放 .consolidate-lock,否则会触发门4 busy,优先级更高

        s = DistillationScheduler(memory_root, DistillationConfig(), llm_callback=lambda p: "[]")
        ok, reason = s.should_distill()
        assert ok is False
        assert "too_soon" in reason

    def test_gate_busy_wins_over_too_soon(self, memory_root):
        """门4 优先级 > 门1: 锁被占时,即使 .last-distill 很新也报 locked"""
        mtime = memory_root / ".last-distill"
        mtime.touch()  # 很新

        # 但 .consolidate-lock 被当前 PID 持有
        lock = memory_root / ".consolidate-lock"
        lock.touch()
        env = memory_root / ".consolidate-lock.lock.json"
        env.write_text(json.dumps({"pid": os.getpid(), "host": "test", "started_at": time.time(), "schema_version": 1}))

        s = DistillationScheduler(memory_root, DistillationConfig(), llm_callback=lambda p: "[]")
        ok, reason = s.should_distill()
        assert ok is False
        assert "locked_by_" in reason

    def test_gate_few_memories(self, memory_root, config):
        """门3: .last-distill 很久前(> 24h)但 .md 数 < 阈值 → too_few_memories"""
        # .last-distill mtime = 25h 前
        mtime = memory_root / ".last-distill"
        mtime.touch()
        old_time = time.time() - 25 * 3600
        os.utime(mtime, (old_time, old_time))

        # memory_root 写 2 个 .md(< 阈值 5)
        (memory_root / "user").mkdir(exist_ok=True)
        for i in range(2):
            (memory_root / "user" / f"h{i}.md").write_text("x", encoding="utf-8")

        s = DistillationScheduler(memory_root, config, llm_callback=lambda p: "[]")
        ok, reason = s.should_distill()
        assert ok is False
        assert "too_few_memories(2<5)" in reason

    def test_gate_ok(self, populated_memory_root, config):
        """所有门通过 → True / ok"""
        # .last-distill mtime = 25h 前
        mtime = populated_memory_root / ".last-distill"
        mtime.touch()
        old_time = time.time() - 25 * 3600
        os.utime(mtime, (old_time, old_time))

        # populated_memory_root fixture 已有 6 个 .md(> 阈值 5)
        s = DistillationScheduler(populated_memory_root, config, llm_callback=lambda p: "[]")
        ok, reason = s.should_distill()
        assert ok is True
        assert reason == "ok"


# ──────────────────────────────────────────────────────────────────
# 2. 锁原子创建 (A1, O_EXCL)
# ──────────────────────────────────────────────────────────────────

class TestLockAtomic:

    def test_concurrent_acquire_only_one_wins(self, memory_root):
        """10 线程并发 acquire → 只有 1 个赢(返回 >= 0),其它 9 个 LOCK_TAKEN"""
        s = DistillationScheduler(memory_root, DistillationConfig())
        results = []
        barrier = threading.Barrier(10)
        lock = threading.Lock()

        def attempt():
            barrier.wait()  # 对齐 10 线程同时发起
            prior = s._acquire_lock()
            with lock:
                results.append(prior)

        threads = [threading.Thread(target=attempt) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        winners = [r for r in results if r >= 0]
        losers = [r for r in results if r == s.LOCK_TAKEN]
        assert len(winners) == 1, f"应只有 1 个赢家,实际 {len(winners)}"
        assert len(losers) == 9, f"应 9 个 loser,实际 {len(losers)}"

    def test_double_acquire_same_process(self, scheduler):
        """同一进程重复 acquire → 第 2 次返回 LOCK_TAKEN"""
        first = scheduler._acquire_lock()
        assert first == 0  # 第一次:无 prior → 0

        # 第二次:O_EXCL 失败(锁文件存在)→ LOCK_TAKEN
        second = scheduler._acquire_lock()
        assert second == scheduler.LOCK_TAKEN


# ──────────────────────────────────────────────────────────────────
# 3. 锁强占语义 (A1+A2, PID 已死 + mtime 超时)
# ──────────────────────────────────────────────────────────────────

class TestLockStaleSteal:

    def test_stale_pid_recoverable(self, memory_root):
        """A1: 锁 PID 已死 → should_distill 视为空闲"""
        lock = memory_root / ".consolidate-lock"
        env = memory_root / ".consolidate-lock.lock.json"
        lock.touch()
        # 用一个明显不存在的 PID(1 通常是 init,可能存活;用 999999 更稳)
        env.write_text(json.dumps({"pid": 999999, "host": "fake", "started_at": time.time() - 100, "schema_version": 1}))

        # mtime 调新鲜(< 24h) → 但 PID 已死,仍可被强占
        s = DistillationScheduler(memory_root, DistillationConfig(), llm_callback=lambda p: "[]")
        lock_state = s._check_lock_state()
        assert lock_state["busy"] is False, f"陈旧 PID 应视为空闲,实际 busy={lock_state['busy']}"

    def test_stale_mtime_recoverable(self, memory_root):
        """A2: mtime 超时 → should_distill 视为空闲"""
        lock = memory_root / ".consolidate-lock"
        env = memory_root / ".consolidate-lock.lock.json"
        lock.touch()
        env.write_text(json.dumps({"pid": os.getpid(), "host": "alive", "started_at": time.time(), "schema_version": 1}))

        # mtime 调到 2h 前(超过 lock_stale_mtime_seconds 默认 3600s)
        old_time = time.time() - 2 * 3600
        os.utime(lock, (old_time, old_time))

        s = DistillationScheduler(memory_root, DistillationConfig(), llm_callback=lambda p: "[]")
        lock_state = s._check_lock_state()
        assert lock_state["busy"] is False

    def test_fresh_lock_busy(self, memory_root):
        """新鲜锁 + PID 存活 → busy=True"""
        lock = memory_root / ".consolidate-lock"
        env = memory_root / ".consolidate-lock.lock.json"
        lock.touch()
        env.write_text(json.dumps({"pid": os.getpid(), "host": "alive", "started_at": time.time(), "schema_version": 1}))

        s = DistillationScheduler(memory_root, DistillationConfig(), llm_callback=lambda p: "[]")
        lock_state = s._check_lock_state()
        assert lock_state["busy"] is True
        assert lock_state["holder_pid"] == os.getpid()


# ──────────────────────────────────────────────────────────────────
# 4. JSON envelope (A11)
# ──────────────────────────────────────────────────────────────────

class TestEnvelopeA11:

    def test_envelope_round_trip(self, scheduler):
        """正常 envelope → 读出 pid/host"""
        scheduler._lock_path.touch()
        scheduler._write_envelope()
        env = scheduler._read_envelope()
        assert env["pid"] == os.getpid()
        assert env["schema_version"] == 1
        assert "started_at" in env

    def test_garbage_rejected(self, scheduler):
        """garbage JSON → 读出空 dict(可被强占)"""
        scheduler._lock_path.touch()
        scheduler._envelope_path.write_text("this is not json{{{")
        env = scheduler._read_envelope()
        assert env == {}  # garbage → empty

    def test_missing_envelope(self, scheduler):
        """envelope 文件不存在 → 空 dict"""
        scheduler._lock_path.touch()
        env = scheduler._read_envelope()
        assert env == {}


# ──────────────────────────────────────────────────────────────────
# 5. 失败回滚 (A2)
# ──────────────────────────────────────────────────────────────────

class TestFailureRollback:

    def test_failure_preserves_prior_mtime(self, populated_memory_root):
        """失败:回滚 .last-distill mtime 到 prior(失败 run 不推进 24h 门)"""
        # 创建 prior .last-distill(25h 前)
        mtime_file = populated_memory_root / ".last-distill"
        mtime_file.touch()
        prior_time = time.time() - 25 * 3600
        os.utime(mtime_file, (prior_time, prior_time))
        prior_mtime_ms = int(prior_time * 1000)

        s = DistillationScheduler(populated_memory_root, DistillationConfig(), llm_callback=lambda p: "[]")

        # acquire → 拿到 prior_mtime_ms
        returned_prior = s._acquire_lock()
        assert returned_prior == prior_mtime_ms

        # 释放时 success=False → 回滚 .last-distill mtime
        s._release_lock(prior_mtime_ms, success=False)

        # 锁文件应被删除
        assert not (populated_memory_root / ".consolidate-lock").exists()
        # .last-distill 仍在,mtime 应等于 prior
        assert mtime_file.exists()
        actual_mtime = mtime_file.stat().st_mtime
        assert abs(actual_mtime - prior_time) < 1.0, f"mtime 未回滚: 实际 {actual_mtime}, 期望 {prior_time}"

    def test_success_advances_mtime(self, populated_memory_root):
        """成功:touch .last-distill(mtime = now,推进 24h 门)"""
        # prior .last-distill(25h 前)
        mtime_file = populated_memory_root / ".last-distill"
        mtime_file.touch()
        prior_time = time.time() - 25 * 3600
        os.utime(mtime_file, (prior_time, prior_time))

        s = DistillationScheduler(populated_memory_root, DistillationConfig(), llm_callback=lambda p: "[]")
        returned_prior = s._acquire_lock()

        # success=True → touch .last-distill(mtime = now)
        before_release = time.time()
        s._release_lock(returned_prior, success=True)
        after_release = time.time()

        # 锁文件被删除
        assert not (populated_memory_root / ".consolidate-lock").exists()
        # .last-distill 仍在,mtime 应是 now(在 acquire 和 release 之间)
        assert mtime_file.exists()
        actual_mtime = mtime_file.stat().st_mtime
        assert before_release - 1 <= actual_mtime <= after_release + 1, \
            f"成功路径应 touch 新 mtime,实际 = {actual_mtime},期望范围 [{before_release}, {after_release}]"


# ──────────────────────────────────────────────────────────────────
# 6. 核心蒸馏逻辑
# ──────────────────────────────────────────────────────────────────

class TestDistillCore:

    def test_distill_returns_candidates(self, populated_memory_root, mock_llm):
        """distill() 返回 LLM 给的候选列表(M11.6:全量 .md 扫描,不再需要 session logs)"""
        # populated_memory_root fixture 已有 6 个 .md(过门3:>=5)
        s = DistillationScheduler(populated_memory_root, DistillationConfig(), llm_callback=mock_llm)
        result = s.run(dry_run=True)
        assert result.success
        assert len(result.candidates) == 2
        assert result.candidates[0]["title"] == "偏好 Vite"

    def test_dry_run_skips_write(self, populated_memory_root, mock_llm):
        """dry_run=True → 不写候选文件"""
        s = DistillationScheduler(populated_memory_root, DistillationConfig(), llm_callback=mock_llm)
        result = s.run(dry_run=True)

        assert result.success
        assert result.candidates_written == []
        assert not (populated_memory_root / "_candidate").exists()

    def test_write_candidates_to_candidate_dir(self, tmp_path, mock_llm):
        """write_candidates() 写到 candidate_root/{type}/"""
        d = Distiller(mock_llm, candidate_root=tmp_path / "_candidate")
        candidates = [
            {"type": "user", "title": "偏好 Vite", "why": "原因", "body": "内容",
             "confidence": 0.8, "sources": ["s1"], "tags": ["pref"]},
            {"type": "feedback", "title": "重视原理", "why": "学习风格", "body": "正文",
             "confidence": 0.7, "sources": ["s1"], "tags": []},
        ]
        written = d.write_candidates(candidates, tmp_path / "_candidate")
        assert len(written) == 2
        user_files = list((tmp_path / "_candidate" / "user").glob("*.md"))
        feedback_files = list((tmp_path / "_candidate" / "feedback").glob("*.md"))
        assert len(user_files) == 1
        assert len(feedback_files) == 1
        # 内容校验
        text = user_files[0].read_text(encoding="utf-8")
        assert "type: user" in text
        assert "偏好 Vite" in text

    def test_run_with_no_llm_skipped(self, populated_memory_root):
        """run() 无 llm_callback → skipped / no_llm_callback"""
        # populated_memory_root fixture 已有 6 个 .md(过门3:>=5),但无 LLM 仍应 skip
        s = DistillationScheduler(populated_memory_root, DistillationConfig(), llm_callback=None)
        result = s.run(dry_run=True)
        assert not result.success
        assert result.skipped
        assert result.skip_reason == "no_llm_callback"


# ──────────────────────────────────────────────────────────────────
# 7. 数据结构
# ──────────────────────────────────────────────────────────────────

class TestDataStructures:

    def test_distillation_result_defaults(self):
        """DistillationResult 默认值"""
        r = DistillationResult(success=True)
        assert r.skipped is False
        assert r.skip_reason == ""
        assert r.candidates == []
        assert r.candidates_written == []
        assert r.sessions_processed == 0
        assert r.prior_mtime_ms == 0
        assert r.error == ""

    def test_distiller_accepts_callback(self):
        """Distiller 接受 callable"""
        d = Distiller(llm_callback=lambda p: "[]")
        assert d.llm is not None