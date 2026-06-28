"""
M10 Cluster C4 Tasks C4.1 + C4.2 — candidate_actions 5 函数测试(7 cases)

覆盖:
A. list_candidates(1): 递归列出 _candidate/ 下所有 .md(嵌套 run_id/type)
B. accept_candidate(2):
   - 接受 user 候选 → 移到 user/<hash>.md, 候选消失
   - target_type='feedback' override → 移到 feedback/
C. reject_candidate(1): 删候选文件
D. edit_candidate(1): 改 body, frontmatter 保留
E. skip_candidate(1): 不删候选
F. accept 不重复 title + 正式记忆 body 正确(弱断言,跨 C4.4 预备)

设计要点:
- _write_candidate helper 模拟 Distiller.write_candidates 输出格式
  (YAML frontmatter + # title + **Why:** + ## 内容 body)
- 用 tmp_path 隔离(不污染真实 ~/.agent_data)
- 真实校正: MemoryStore.write 要求非空 source_quote, accept_candidate
  会从候选 frontmatter 的 sources / title 派生一个
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from agent_core.memory.candidate_actions import (
    list_candidates,
    accept_candidate,
    reject_candidate,
    edit_candidate,
    skip_candidate,
)


def _write_candidate(
    cand_root: Path,
    type_: str,
    title: str,
    body: str,
    run_id: str = "r1",
    sources: list[str] | None = None,
) -> Path:
    """helper: 写一个候选 .md 到 {cand_root}/{run_id}/{type}/{ts}_{slug}.md

    模拟 Distiller.write_candidates 输出(frontmatter + # title + **Why:** + ## 内容)
    """
    ts = time.strftime("%Y-%m-%dT%H-%M-%S")
    target = cand_root / run_id / type_
    target.mkdir(parents=True, exist_ok=True)
    p = target / f"{ts}_{type_}.md"
    srcs = sources if sources is not None else ["session-1"]
    # YAML frontmatter(与 Distiller._render_frontmatter 一致格式)
    fm = (
        "---\n"
        f"type: {type_}\n"
        f"title: {title}\n"
        "confidence: 0.7\n"
        f"sources: {srcs}\n"
        "tags: []\n"
        "---\n\n"
    )
    full_body = f"# {title}\n\n**Why:** 测试理由\n\n## 内容\n{body}\n"
    p.write_text(fm + full_body, encoding="utf-8")
    return p


# ─────────────────────────────────────
# A: list (1)
# ─────────────────────────────────────

def test_list_candidates_returns_all(tmp_path):
    """list_candidates() 返回所有 _candidate/ 下的 .md(嵌套 run_id/type)"""
    cand_root = tmp_path / "_candidate"
    cand_root.mkdir()
    p1 = _write_candidate(cand_root, "user", "A", "body A")
    p2 = _write_candidate(cand_root, "feedback", "B", "body B", run_id="r2")
    p3 = _write_candidate(cand_root, "project", "C", "body C", run_id="r1")

    result = list_candidates(tmp_path)
    names = {p.name for p in result}
    assert len(result) == 3
    assert {p1.name, p2.name, p3.name}.issubset(names)


def test_list_candidates_empty_when_no_candidate_dir(tmp_path):
    """_candidate/ 不存在时返回空列表(不抛)"""
    assert list_candidates(tmp_path) == []


# ─────────────────────────────────────
# B: accept (2)
# ─────────────────────────────────────

def test_accept_moves_candidate_to_user_dir(tmp_path):
    """accept_candidate 把候选从 _candidate/ 移到 memory/<type>/<hash>.md"""
    cand_root = tmp_path / "_candidate"
    cand_root.mkdir()
    p = _write_candidate(cand_root, "user", "喜好", "user 喜欢 X")

    item_hash = accept_candidate(tmp_path, p)

    # 1. 候选消失
    assert not p.exists()
    # 2. 正式记忆出现(user/<hash>.md)
    target = tmp_path / "user" / f"{item_hash}.md"
    assert target.exists()
    # 3. item_hash 是 64 字符 hex
    assert len(item_hash) == 64
    assert all(c in "0123456789abcdef" for c in item_hash)
    # 4. 内容包含原 body(不丢内容)
    content = target.read_text(encoding="utf-8")
    assert "user 喜欢 X" in content


def test_accept_with_target_type_override(tmp_path):
    """accept_candidate(..., target_type='feedback') 把候选移到 feedback/ 而不是原 type

    注: feedback 类要求 body 含 **Why:** → candidate body 已含(见 _write_candidate)
    """
    cand_root = tmp_path / "_candidate"
    cand_root.mkdir()
    p = _write_candidate(cand_root, "user", "反馈", "user 反馈 Y")

    item_hash = accept_candidate(tmp_path, p, target_type="feedback")

    target = tmp_path / "feedback" / f"{item_hash}.md"
    assert target.exists()
    assert not (tmp_path / "user" / f"{item_hash}.md").exists()
    assert not p.exists()


# ─────────────────────────────────────
# C: reject (1)
# ─────────────────────────────────────

def test_reject_deletes_candidate(tmp_path):
    """reject_candidate 删候选文件,不创建正式记忆"""
    cand_root = tmp_path / "_candidate"
    cand_root.mkdir()
    p = _write_candidate(cand_root, "user", "X", "x")

    reject_candidate(tmp_path, p, reason="irrelevant")

    assert not p.exists()
    # 正式目录没创建(或为空)
    user_dir = tmp_path / "user"
    assert not user_dir.exists() or not list(user_dir.iterdir())


# ─────────────────────────────────────
# D: edit (1)
# ─────────────────────────────────────

def test_edit_overrides_body(tmp_path):
    """edit_candidate 改 body(frontmatter 不变)"""
    cand_root = tmp_path / "_candidate"
    cand_root.mkdir()
    p = _write_candidate(cand_root, "user", "X", "original body")

    edit_candidate(tmp_path, p, new_body="# X\n\n**Why:** 测试理由\n\n## 内容\nedited body\n")

    content = p.read_text(encoding="utf-8")
    assert "edited body" in content
    assert "original body" not in content
    # frontmatter 保留
    assert "type: user" in content
    assert "title: X" in content


# ─────────────────────────────────────
# E: skip (1)
# ─────────────────────────────────────

def test_skip_does_not_delete(tmp_path):
    """skip_candidate 不删候选(留待下次审)"""
    cand_root = tmp_path / "_candidate"
    cand_root.mkdir()
    p = _write_candidate(cand_root, "user", "X", "x")

    skip_candidate(tmp_path, p)

    assert p.exists()  # 仍在


# ─────────────────────────────────────
# F: accept 落盘后正式记忆 title 不重复(1)
# ─────────────────────────────────────

def test_accept_written_memory_has_no_duplicate_title(tmp_path):
    """accept_candidate 落盘的正式记忆不应出现两行 # title

    背景: MemoryStore.write 自己会加 `# {title}`, 候选 body 也以 `# {title}` 开头。
    accept_candidate 必须剥离 body 开头的 H1, 否则文件里 title 出现两次。
    本测试守住这个真实校正点。
    """
    cand_root = tmp_path / "_candidate"
    cand_root.mkdir()
    p = _write_candidate(cand_root, "user", "我的标题", "正文内容")

    item_hash = accept_candidate(tmp_path, p)

    target = tmp_path / "user" / f"{item_hash}.md"
    content = target.read_text(encoding="utf-8")
    # 只统计 frontmatter 之后的 # 标题行(避开 frontmatter 里的 #)
    body_part = content.split("---", 2)[-1]
    h1_lines = [ln for ln in body_part.splitlines() if ln.startswith("# ")]
    assert len(h1_lines) == 1, f"应有 1 个 H1, 实际 {len(h1_lines)}: {h1_lines}"
    assert "正文内容" in content
