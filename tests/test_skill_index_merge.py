"""tests/test_skill_index_merge.py — 多源优先级覆盖 + 同名去重 + 字典序（T050, INV-1, US5）"""
from __future__ import annotations

from pathlib import Path

import pytest

from agent_core.skills.config import SkillsConfig, SourceConfig, SourcesConfig
from agent_core.skills.skill_index import load_all, merge_by_priority
from agent_core.skills.types import Skill, SkillEntry, SkillSource


def _entry(name: str, source: SkillSource, *, desc: str = "d", path: str | None = None) -> SkillEntry:
    return SkillEntry(
        skill=Skill(
            name=name, description=desc,
            file_path=path or f"/{source.value}/{name}/SKILL.md",
            base_dir=f"/{source.value}/{name}", source=source,
        )
    )


class TestMergeByPriority:
    def test_workspace_overrides_bundled(self):
        # INV-1：同名 skill workspace 覆盖 bundled
        bundled = _entry("x", SkillSource.BUNDLED, desc="bundled-v")
        workspace = _entry("x", SkillSource.WORKSPACE, desc="workspace-v")
        merged = merge_by_priority([
            (SkillSource.BUNDLED, [bundled]),
            (SkillSource.WORKSPACE, [workspace]),
        ])
        assert len(merged) == 1
        assert merged[0].skill.description == "workspace-v"

    def test_same_name_dedup_across_sources(self):
        a_b = _entry("s", SkillSource.BUNDLED)
        a_w = _entry("s", SkillSource.WORKSPACE)
        b_w = _entry("other", SkillSource.WORKSPACE)
        merged = merge_by_priority([
            (SkillSource.BUNDLED, [a_b]),
            (SkillSource.WORKSPACE, [a_w, b_w]),
        ])
        names = {e.skill.name for e in merged}
        assert names == {"s", "other"}  # s 去重
        s = next(e for e in merged if e.skill.name == "s")
        assert s.skill.source == SkillSource.WORKSPACE  # 高优先级胜

    def test_bundled_only_kept_when_no_workspace_override(self):
        bundled = _entry("only", SkillSource.BUNDLED)
        merged = merge_by_priority([
            (SkillSource.BUNDLED, [bundled]),
            (SkillSource.WORKSPACE, []),
        ])
        assert len(merged) == 1
        assert merged[0].skill.source == SkillSource.BUNDLED

    def test_same_source_same_name_first_wins(self, caplog):
        # 同源内同名 → 先到先得（不覆盖）
        e1 = _entry("dup", SkillSource.WORKSPACE, desc="first")
        e2 = _entry("dup", SkillSource.WORKSPACE, desc="second")
        merged = merge_by_priority([
            (SkillSource.WORKSPACE, [e1, e2]),
        ])
        assert len(merged) == 1
        assert merged[0].skill.description == "first"

    def test_three_source_custom_priority(self):
        # 自定义 3 源：p1 < p2 < p3，高者覆盖
        from agent_core.skills.types import SkillSource
        # 用 BUNDLED/WORKSPACE 两枚举值模拟 3 源（priority 由 config 控制，merge 只看顺序）
        e_low = _entry("k", SkillSource.BUNDLED, desc="low")
        e_mid = _entry("k", SkillSource.WORKSPACE, desc="mid")
        # 第三次"源"复用 BUNDLED 枚举但晚写 → 覆盖（模拟更高优先级）
        e_high = _entry("k", SkillSource.BUNDLED, desc="high")
        merged = merge_by_priority([
            (SkillSource.BUNDLED, [e_low]),
            (SkillSource.WORKSPACE, [e_mid]),
            (SkillSource.BUNDLED, [e_high]),
        ])
        # 最后写者胜（merge 不区分"同源"，仅看写入顺序）
        assert merged[0].skill.description == "high"


class TestLoadAll:
    def _write(self, root: Path, name: str, desc: str = "d") -> Path:
        d = root / name
        d.mkdir(parents=True, exist_ok=True)
        (d / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: {desc}\n---\nbody\n",
            encoding="utf-8",
        )
        return d

    def test_load_all_default_two_source_merge(self, tmp_path):
        bundled = tmp_path / "bundled"
        workspace = tmp_path / "workspace"
        bundled.mkdir(); workspace.mkdir()
        self._write(bundled, "shared", desc="from-bundled")
        self._write(workspace, "shared", desc="from-workspace")  # 覆盖
        self._write(workspace, "ws-only")

        cfg = SkillsConfig.from_dict({
            "paths": {"bundled_dir": str(bundled), "workspace_dir": str(workspace)}
        })
        entries = load_all(cfg)
        names = {e.skill.name for e in entries}
        assert names == {"shared", "ws-only"}
        shared = next(e for e in entries if e.skill.name == "shared")
        assert shared.skill.description == "from-workspace"  # workspace 胜

    def test_load_all_sorted_by_name(self, tmp_path):
        workspace = tmp_path / "ws"; workspace.mkdir()
        for n in ("zeta", "alpha", "mike"):
            self._write(workspace, n)
        cfg = SkillsConfig.from_dict({
            "paths": {"bundled_dir": str(tmp_path / "b"), "workspace_dir": str(workspace)}
        })
        entries = load_all(cfg)
        names = [e.skill.name for e in entries]
        assert names == sorted(names)  # 字典序

    def test_load_all_explicit_sources_three_layer(self, tmp_path):
        # 自定义 3 源优先级：low < mid < high
        low = tmp_path / "low"; mid = tmp_path / "mid"; high = tmp_path / "high"
        for d in (low, mid, high): d.mkdir()
        self._write(low, "x", desc="low-v")
        self._write(mid, "x", desc="mid-v")
        self._write(high, "x", desc="high-v")

        cfg = SkillsConfig.from_dict({
            "sources": {"items": [
                {"source": "bundled", "dir": str(low), "priority": 1},
                {"source": "workspace", "dir": str(mid), "priority": 2},
                {"source": "workspace", "dir": str(high), "priority": 3},
            ]}
        })
        entries = load_all(cfg)
        x = next(e for e in entries if e.skill.name == "x")
        assert x.skill.description == "high-v"  # 最高优先级胜


class TestSourcesList:
    def test_default_two_source_from_paths(self, tmp_path):
        cfg = SkillsConfig.from_dict({
            "paths": {"bundled_dir": str(tmp_path / "b"), "workspace_dir": str(tmp_path / "w")}
        })
        srcs = cfg.sources_list()
        assert len(srcs) == 2
        assert srcs[0].source == "bundled" and srcs[0].priority == 1
        assert srcs[1].source == "workspace" and srcs[1].priority == 2

    def test_explicit_sources_override(self, tmp_path):
        cfg = SkillsConfig.from_dict({
            "sources": {"items": [
                {"source": "bundled", "dir": str(tmp_path / "a"), "priority": 1},
                {"source": "workspace", "dir": str(tmp_path / "b"), "priority": 2},
                {"source": "workspace", "dir": str(tmp_path / "c"), "priority": 3},
            ]}
        })
        srcs = cfg.sources_list()
        assert len(srcs) == 3
        assert [s.priority for s in srcs] == [1, 2, 3]
