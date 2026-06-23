"""
M10 Cluster C4 Tasks C4.3 + C4.4 — run_id 透传 + review 决策回灌测试

覆盖:
A. compute_candidate_key (2): 稳定 + 区分 + 空 body 不崩
B. meta_db record/list (1): UPSERT + list_decided_candidates 返回 set
C. C4.3 scheduler run_id 透传 (1): dry_run=False → candidates_written 路径含 run_
D. C4.4 scheduler skip-decided (1): 已审候选下次 distill 跳过(不写盘)
E. C4.4 candidate_actions 记决策 (1): accept_candidate 后 meta_db 有 accepted row

设计要点:
- test C/D 用 fake_llm 返回固定候选 + scheduler.should_distill=lambda 强制过门
- test E 弱断言(_parse_candidate 的 body 含 markdown 结构, 精确 key 取决于实现,
  只验"有决策记录被写入")
- MetaDB(tmp_path / "meta.db") 首次构造即 executescript 建表
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_core.memory.types import compute_candidate_key
from agent_core.memory.meta_db import MetaDB
from agent_core.memory.distiller import DistillationScheduler, DistillationResult
from agent_core.memory.config import DistillationConfig
from agent_core.memory.candidate_actions import accept_candidate, reject_candidate


# ─────────────────────────────────────
# A: compute_candidate_key (2)
# ─────────────────────────────────────

def test_compute_candidate_key_stable_and_distinct():
    """同 type+body → 同 key;不同 → 不同 key;64 hex"""
    k1 = compute_candidate_key("user", "x")
    k2 = compute_candidate_key("user", "x")
    k3 = compute_candidate_key("feedback", "x")
    assert k1 == k2
    assert k1 != k3
    assert len(k1) == 64
    # 全 hex
    assert all(c in "0123456789abcdef" for c in k1)


def test_compute_candidate_key_empty_body():
    """空 body 不崩(key 仍算出)"""
    k = compute_candidate_key("user", "")
    assert len(k) == 64


# ─────────────────────────────────────
# B: meta_db record/list (1)
# ─────────────────────────────────────

def test_record_and_list_decided_candidates(tmp_path):
    """record_candidate_decision UPSERT + list_decided_candidates 返回 set"""
    db = MetaDB(tmp_path / "meta.db")
    k1 = compute_candidate_key("user", "a")
    k2 = compute_candidate_key("feedback", "b")

    db.record_candidate_decision(k1, "accepted")
    db.record_candidate_decision(k2, "rejected")

    decided = db.list_decided_candidates()
    assert isinstance(decided, set)
    assert k1 in decided
    assert k2 in decided
    assert len(decided) == 2

    # UPSERT: 同 key 覆盖(不新增)
    db.record_candidate_decision(k1, "rejected")
    assert len(db.list_decided_candidates()) == 2


# ─────────────────────────────────────
# C: C4.3 scheduler run_id 透传 (1)
# ─────────────────────────────────────

def test_scheduler_run_passes_run_id_when_writing(tmp_path):
    """scheduler.run(dry_run=False) → candidates_written 路径含 /run_/, run_id 字段非空

    目录约定: memory_root = {agent_data}/memory, meta.db 在 {agent_data}/meta.db
    (即 memory_root.parent / "meta.db")。测试用 memory_root = tmp_path/"memory" 还原。
    """
    memory_root = tmp_path / "memory"
    memory_root.mkdir()
    config = DistillationConfig(enabled=True)

    # fake LLM 返回 1 个候选
    def fake_llm(prompt):
        return json.dumps([{
            "type": "user", "title": "T", "body": "B",
            "why": "w", "sources": [], "tags": [],
        }])

    scheduler = DistillationScheduler(
        memory_root=memory_root, config=config, llm_callback=fake_llm,
    )
    # 强制过四重门
    scheduler.should_distill = lambda: (True, "forced")

    result = scheduler.run(dry_run=False)

    assert result.success
    assert result.run_id.startswith("run_")  # M10 C4.3
    assert len(result.candidates_written) == 1
    # 路径在 _candidate/run_*/user/ 下
    p = result.candidates_written[0]
    assert "/run_" in str(p)
    assert "/user/" in str(p)


# ─────────────────────────────────────
# D: C4.4 scheduler skip-decided (1)
# ─────────────────────────────────────

def test_scheduler_run_skips_decided_candidates(tmp_path):
    """已 accept 的候选,下次 distill 跳过(不写盘)

    目录约定: memory_root = tmp_path/"memory", meta.db 在 tmp_path/"meta.db"
    (scheduler 读 memory_root.parent / "meta.db")。
    """
    memory_root = tmp_path / "memory"
    memory_root.mkdir()
    # 1. 先准备 meta.db + 记一条决策
    db = MetaDB(tmp_path / "meta.db")
    decided_key = compute_candidate_key("user", "decided body")
    db.record_candidate_decision(decided_key, "accepted")

    # 2. fake LLM 返回 2 个候选,1 个已审 1 个未审
    def fake_llm(prompt):
        return json.dumps([
            {"type": "user", "title": "D", "body": "decided body",
             "why": "w", "sources": [], "tags": []},
            {"type": "user", "title": "N", "body": "new body",
             "why": "w", "sources": [], "tags": []},
        ])

    scheduler = DistillationScheduler(
        memory_root=memory_root, config=DistillationConfig(enabled=True),
        llm_callback=fake_llm,
    )
    scheduler.should_distill = lambda: (True, "forced")

    result = scheduler.run(dry_run=False)

    assert result.success
    # 只写 1 个(未审的),已审的被 skip
    assert len(result.candidates_written) == 1
    written = result.candidates_written[0].read_text(encoding="utf-8")
    assert "new body" in written
    assert "decided body" not in written


# ─────────────────────────────────────
# E: C4.4 candidate_actions 记决策 (1)
# ─────────────────────────────────────

def test_accept_candidate_records_decision_in_meta_db(tmp_path):
    """accept_candidate 后 meta_db.candidate_decisions 有 accepted row

    强断言: 记录的 key 必须与 scheduler skip-decided 对同一原始 body 算出的 key 一致
    (端到端闭环: accept 记 key → 下次 distill 同 body 被 skip)。candidate_actions
    从渲染后的候选 markdown('## 内容' 段)抽回原始 body 算 key, 与 scheduler 的
    c["body"] 口径对齐。
    """
    memory_root = tmp_path / "memory"
    memory_root.mkdir()
    # 准备候选文件(模拟 distiller 输出, body 原文 = 'body text')
    cand_root = memory_root / "_candidate" / "run_1" / "user"
    cand_root.mkdir(parents=True)
    cand = cand_root / "2026-01-01T00-00-00_x.md"
    cand.write_text(
        "---\ntype: user\ntitle: X\nconfidence: 0.7\nsources: []\ntags: []\n---\n\n"
        "# X\n\n**Why:** w\n\n## 内容\nbody text\n",
        encoding="utf-8",
    )

    # 先建 meta.db(让 candidate_actions 能记)。MetaDB 连接是懒创建,
    # 必须触发一次查询才会真正建表 + 落盘文件(否则 meta_path.exists() 为 False)
    MetaDB(tmp_path / "meta.db").list_decided_candidates()

    item_hash = accept_candidate(memory_root, cand)

    # 验: meta_db 记的 key == scheduler 对原始 body('body text')算的 key
    db = MetaDB(tmp_path / "meta.db")
    decided = db.list_decided_candidates()
    expected_key = compute_candidate_key("user", "body text")
    assert expected_key in decided, (
        f"accept 记的 key 必须与 scheduler skip 口径一致; "
        f"got {decided}, expected {expected_key}"
    )
    # 候选文件已删
    assert not cand.exists()


def test_reject_candidate_records_decision_in_meta_db(tmp_path):
    """reject_candidate 后 meta_db.candidate_decisions 有 rejected row(best-effort 弱断言)"""
    memory_root = tmp_path / "memory"
    memory_root.mkdir()
    cand_root = memory_root / "_candidate" / "run_1" / "user"
    cand_root.mkdir(parents=True)
    cand = cand_root / "2026-01-01T00-00-00_y.md"
    cand.write_text(
        "---\ntype: user\ntitle: Y\nconfidence: 0.7\nsources: []\ntags: []\n---\n\n"
        "# Y\n\n**Why:** w\n\n## 内容\nreject me\n",
        encoding="utf-8",
    )

    MetaDB(tmp_path / "meta.db").list_decided_candidates()

    reject_candidate(memory_root, cand, reason="not relevant")

    assert not cand.exists()
    db = MetaDB(tmp_path / "meta.db")
    decided = db.list_decided_candidates()
    assert len(decided) >= 1


# ─────────────────────────────────────
# F: C4.3 + C4.4 端到端闭环 (1)
# ─────────────────────────────────────

def test_accept_then_distill_skips_accepted_end_to_end(tmp_path):
    """端到端: accept 一条候选 → 下次 distill 同 body 候选被 skip(不写盘)

    守住 C4.3+C4.4 的核心闭环: accept 记的 key 必须能被 scheduler skip-decided
    命中。这依赖 candidate_actions 从 '## 内容' 段抽回原始 body 算 key,
    与 scheduler 的 c["body"] 口径一致。
    """
    memory_root = tmp_path / "memory"
    memory_root.mkdir()
    MetaDB(tmp_path / "meta.db").list_decided_candidates()

    # 1. 模拟上一轮 distill 已写出的候选文件, 然后 accept 它
    cand_root = memory_root / "_candidate" / "run_old" / "user"
    cand_root.mkdir(parents=True)
    old_cand = cand_root / "2026-01-01T00-00-00_prev.md"
    old_cand.write_text(
        "---\ntype: user\ntitle: Prev\nconfidence: 0.7\nsources: []\ntags: []\n---\n\n"
        "# Prev\n\n**Why:** w\n\n## 内容\nshared body\n",
        encoding="utf-8",
    )
    accept_candidate(memory_root, old_cand)

    # 2. 新一轮 distill: fake LLM 又吐出同一条(同 body)+ 一条新的
    def fake_llm(prompt):
        return json.dumps([
            {"type": "user", "title": "Prev", "body": "shared body",
             "why": "w", "sources": [], "tags": []},
            {"type": "user", "title": "Fresh", "body": "fresh body",
             "why": "w", "sources": [], "tags": []},
        ])

    scheduler = DistillationScheduler(
        memory_root=memory_root, config=DistillationConfig(enabled=True),
        llm_callback=fake_llm,
    )
    scheduler.should_distill = lambda: (True, "forced")

    result = scheduler.run(dry_run=False)

    assert result.success
    # 已 accept 的'shared body'被 skip, 只写'fresh body'
    assert len(result.candidates_written) == 1
    written = result.candidates_written[0].read_text(encoding="utf-8")
    assert "fresh body" in written
    assert "shared body" not in written
