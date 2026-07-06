"""tests/test_skill_registry_hotreload.py — Registry mtime 失效 + version 单调（T024, INV-6, SC-001）"""
from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from agent_core.skills.config import SkillsConfig
from agent_core.skills.registry import SkillsRegistry


def _write_skill(root: Path, name: str, desc: str = "d") -> Path:
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {desc}\n---\nbody\n",
        encoding="utf-8",
    )
    return d


class TestVersionMonotonic:
    """INV-6：version 单调非递减"""

    def test_cache_hit_same_version(self, tmp_path):
        cfg = SkillsConfig.from_dict({"paths": {"workspace_dir": str(tmp_path)}})
        reg = SkillsRegistry(cfg)
        v1 = reg.snapshot().version
        v2 = reg.snapshot().version
        assert v1 == v2  # 无变化 → 同版本
        assert v1 > 0

    def test_version_strictly_increases_on_change(self, tmp_path):
        cfg = SkillsConfig.from_dict({"paths": {"workspace_dir": str(tmp_path)}})
        reg = SkillsRegistry(cfg)
        v1 = reg.snapshot().version
        _write_skill(tmp_path, "new")
        # 确保 mtime 变化（有些 FS mtime 精度低）
        time.sleep(0.01)
        os.utime(tmp_path / "new" / "SKILL.md", None)
        v2 = reg.snapshot().version
        assert v2 > v1


class TestHotReload:
    """SC-001 / FR-021a：磁盘变化无需重建 registry 即生效"""

    def test_new_skill_picked_up_without_restart(self, tmp_path):
        cfg = SkillsConfig.from_dict({"paths": {"workspace_dir": str(tmp_path)}})
        reg = SkillsRegistry(cfg)
        snap1 = reg.snapshot()
        assert "new-skill" not in snap1.prompt

        _write_skill(tmp_path, "new-skill", "fresh")
        time.sleep(0.01)
        os.utime(tmp_path / "new-skill" / "SKILL.md", None)

        snap2 = reg.snapshot()
        assert "new-skill" in snap2.prompt
        assert snap2.version > snap1.version

    def test_removed_skill_disappears(self, tmp_path):
        _write_skill(tmp_path, "temp")
        cfg = SkillsConfig.from_dict({"paths": {"workspace_dir": str(tmp_path)}})
        reg = SkillsRegistry(cfg)
        assert "temp" in reg.snapshot().prompt

        (tmp_path / "temp" / "SKILL.md").unlink()
        time.sleep(0.01)
        # 删文件后 utime 父目录确保 sig 变
        os.utime(tmp_path, None)
        snap2 = reg.snapshot()
        assert "temp" not in snap2.prompt


class TestByteStability:
    """SC-002：稳定集合 → 字节稳定"""

    def test_stable_set_byte_stable(self, tmp_path):
        _write_skill(tmp_path, "a")
        _write_skill(tmp_path, "b")
        cfg = SkillsConfig.from_dict({"paths": {"workspace_dir": str(tmp_path)}})
        reg = SkillsRegistry(cfg)
        s1 = reg.snapshot().prompt
        s2 = reg.snapshot().prompt
        assert s1 == s2  # 同版本、同输入 → 字节相同


class TestMidSessionRefresh:
    """FR-021a：mid-session 跨 turn 刷新（同一 registry，跨多次 snapshot 累进变化）"""

    def test_progressive_changes_across_turns(self, tmp_path):
        # bundled_dir 指向空目录，隔离 workspace 行为（避免 bundled skill-creator 噪声）
        bundled = tmp_path / "b"; bundled.mkdir()
        cfg = SkillsConfig.from_dict({
            "paths": {"workspace_dir": str(tmp_path / "ws"), "bundled_dir": str(bundled)}
        })
        reg = SkillsRegistry(cfg)
        ws = tmp_path / "ws"; ws.mkdir()

        def _has(prompt, name):
            return f"<name>{name}</name>" in prompt

        # turn 1：空
        snap1 = reg.snapshot()
        assert not _has(snap1.prompt, "alpha") and not _has(snap1.prompt, "beta")

        # between turn 1→2：加 skill alpha
        _write_skill(ws, "alpha")
        time.sleep(0.01)
        os.utime(ws / "alpha" / "SKILL.md", None)
        os.utime(ws, None)
        snap2 = reg.snapshot()
        assert _has(snap2.prompt, "alpha")
        assert snap2.version > snap1.version

        # between turn 2→3：加 skill beta（alpha 仍在）
        _write_skill(ws, "beta")
        time.sleep(0.01)
        os.utime(ws / "beta" / "SKILL.md", None)
        os.utime(ws, None)
        snap3 = reg.snapshot()
        assert _has(snap3.prompt, "alpha") and _has(snap3.prompt, "beta")
        assert snap3.version > snap2.version

    def test_cache_stable_across_turns_without_changes(self, tmp_path):
        # 无磁盘/env 变化 → 跨 turn 同 version（cache 命中，零 IO）
        _write_skill(tmp_path, "x")
        cfg = SkillsConfig.from_dict({"paths": {"workspace_dir": str(tmp_path)}})
        reg = SkillsRegistry(cfg)
        v1 = reg.snapshot().version
        v2 = reg.snapshot().version
        v3 = reg.snapshot().version
        assert v1 == v2 == v3


class TestBundledWorkspaceMerge:
    """US5 提前覆盖：bundled + workspace 同名 → workspace 胜"""

    def test_workspace_overrides_bundled(self, tmp_path):
        # 把 bundled 指向一个 tmp dir，workspace 指向另一个
        bundled = tmp_path / "bundled"
        workspace = tmp_path / "workspace"
        bundled.mkdir()
        workspace.mkdir()
        _write_skill(bundled, "x", desc="bundled-version")
        _write_skill(workspace, "x", desc="workspace-version")

        cfg = SkillsConfig.from_dict({
            "paths": {
                "bundled_dir": str(bundled),
                "workspace_dir": str(workspace),
            }
        })
        reg = SkillsRegistry(cfg)
        snap = reg.snapshot()
        # workspace 胜 → description 是 workspace-version
        assert "workspace-version" in snap.prompt
        assert "bundled-version" not in snap.prompt

    def test_location_points_to_workspace_version(self, tmp_path):
        # T051：经 registry.snapshot，<location> 指向 workspace 版（US5 端到端）
        bundled = tmp_path / "bundled"
        workspace = tmp_path / "workspace"
        bundled.mkdir(); workspace.mkdir()
        _write_skill(bundled, "x", desc="bundled-version")
        _write_skill(workspace, "x", desc="workspace-version")

        cfg = SkillsConfig.from_dict({
            "paths": {"bundled_dir": str(bundled), "workspace_dir": str(workspace)}
        })
        reg = SkillsRegistry(cfg)
        snap = reg.snapshot()
        ws_path = str(workspace / "x" / "SKILL.md")
        bd_path = str(bundled / "x" / "SKILL.md")
        assert ws_path in snap.prompt    # workspace location 注入
        assert bd_path not in snap.prompt  # bundled location 被覆盖

    def test_delete_workspace_restores_bundled(self, tmp_path):
        # 删 workspace 版 → bundled 恢复（SC-007 / quickstart §2）
        bundled = tmp_path / "bundled"
        workspace = tmp_path / "workspace"
        bundled.mkdir(); workspace.mkdir()
        _write_skill(bundled, "x", desc="bundled-version")
        _write_skill(workspace, "x", desc="workspace-version")

        cfg = SkillsConfig.from_dict({
            "paths": {"bundled_dir": str(bundled), "workspace_dir": str(workspace)}
        })
        reg = SkillsRegistry(cfg)
        assert "workspace-version" in reg.snapshot().prompt

        # 删 workspace 版（整个 skill 目录，对齐 quickstart §1/§2 "删除 skills/x/" 语义）
        import shutil as _sh
        _sh.rmtree(workspace / "x")
        import time as _t
        _t.sleep(0.01)
        os.utime(workspace, None)
        snap2 = reg.snapshot()
        assert "bundled-version" in snap2.prompt  # bundled 恢复
        assert "workspace-version" not in snap2.prompt
