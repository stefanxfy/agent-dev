#!/usr/bin/env python3
"""verify_skill_quickstart — quickstart.md 脚本化节端到端验证（Polish T059）。

覆盖 §1/§2/§3/§4/§5/§7/§8/§9（脚本可验）；§6 slash UI 由用户手动（Constitution UI 门）；
§10 全量回归门、§11 spec acceptance 映射见 docs/agent_core-skill-system-design.md。

Constitution II 实测门：SC-002 字节稳定 / SC-004 预算 / INV 用真实断言，非假设。
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent_core.skills.config import SkillsConfig, LimitsConfig  # noqa: E402
from agent_core.skills.registry import SkillsRegistry  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    mark = "✅" if cond else "❌"
    print(f"{mark} {name}: {detail}")


def cfg(bundled, workspace, **lim):
    return SkillsConfig.from_dict({
        "paths": {"bundled_dir": str(bundled), "workspace_dir": str(workspace)},
        "limits": lim or {},
    })


def main():
    tmp = Path(tempfile.mkdtemp())
    ws = tmp / "ws"; ws.mkdir()
    bundled = tmp / "b"; bundled.mkdir()

    # §1 single skill 发现 + 注入
    hello = ws / "hello"; hello.mkdir()
    (hello / "SKILL.md").write_text(
        '---\nname: hello\ndescription: "Use when hello."\n---\n# Hello\nSkill-loaded hello!\n',
        encoding="utf-8",
    )
    reg = SkillsRegistry(cfg(bundled, ws))
    snap = reg.snapshot()
    check("§1 snapshot 含 hello", "<name>hello</name>" in snap.prompt)
    check("§1 <available_skills> 格式", "<available_skills>" in snap.prompt)

    # §2 双层覆盖
    (bundled / "skill-creator").mkdir()
    (bundled / "skill-creator" / "SKILL.md").write_text(
        '---\nname: skill-creator\ndescription: "bundled"\n---\nBUNDLED-MARKER\n', encoding="utf-8",
    )
    (ws / "skill-creator").mkdir()
    (ws / "skill-creator" / "SKILL.md").write_text(
        '---\nname: skill-creator\ndescription: "workspace"\n---\nOVERRIDDEN-MARKER\n', encoding="utf-8",
    )
    reg2 = SkillsRegistry(cfg(bundled, ws))
    snap2 = reg2.snapshot()
    check("§2 workspace 覆盖 bundled (desc)", "workspace" in snap2.prompt and '"bundled"' not in snap2.prompt,
          f"prompt has 'workspace'={'workspace' in snap2.prompt}")
    check("§2 location 指向 workspace", str(ws / "skill-creator" / "SKILL.md") in snap2.prompt)

    # §3 字节稳定（SC-002 / INV-2）
    s_a = reg2.snapshot().prompt
    s_b = reg2.snapshot().prompt
    check("§3 SC-002 字节稳定", s_a == s_b and hash(s_a) == hash(s_b))

    # §4 eligibility 即时性（INV-5）
    needkey = ws / "need-key"; needkey.mkdir()
    (needkey / "SKILL.md").write_text(
        '---\nname: need-key\ndescription: "need key"\nmetadata:\n  requires:\n    env: ["VERIFY_KEY_T59"]\n---\nb\n',
        encoding="utf-8",
    )
    os.environ.pop("VERIFY_KEY_T59", None)
    reg3 = SkillsRegistry(cfg(bundled, ws))
    before = reg3.snapshot()
    os.environ["VERIFY_KEY_T59"] = "1"
    after = reg3.snapshot()
    check("§4 INV-5 无 key 不在 prompt", "<name>need-key</name>" not in before.prompt)
    check("§4 INV-5 设 key 后出现 + version bump", "<name>need-key</name>" in after.prompt and after.version > before.version)
    os.environ.pop("VERIFY_KEY_T59", None)

    # §5 预算降级（SC-004 / INV-4）
    big_ws = tmp / "big"; big_ws.mkdir()
    big_b = tmp / "bigb"; big_b.mkdir()
    for i in range(60):
        d = big_ws / f"s{i:03d}"; d.mkdir()
        (d / "SKILL.md").write_text(
            f'---\nname: s{i:03d}\ndescription: "skill number {i} with some length here"\n---\nb\n',
            encoding="utf-8",
        )
    reg4 = SkillsRegistry(SkillsConfig.from_dict({
        "paths": {"bundled_dir": str(big_b), "workspace_dir": str(big_ws)},
        "limits": {"max_skills_prompt_chars": 2000},
    }))
    snap5 = reg4.snapshot()
    check("§5 SC-004 降级到 compact/truncate", snap5.render_mode.value in ("compact", "truncate"),
          f"mode={snap5.render_mode.value}")
    check("§5 INV-4 长度受控", len(snap5.prompt) <= 2000, f"len={len(snap5.prompt)}")
    check("§5 INV-4 不静默（⚠️ 或 truncated）", "⚠️" in snap5.prompt)

    # §7 容错（SC-005 / INV-3）
    tol_ws = tmp / "tol"; tol_ws.mkdir()
    tol_b = tmp / "tolb"; tol_b.mkdir()
    (tol_ws / "broken").mkdir()
    (tol_ws / "broken" / "SKILL.md").write_text("---\nname: broken\n---\nb\n", encoding="utf-8")  # 缺 desc
    (tol_ws / "good").mkdir()
    (tol_ws / "good" / "SKILL.md").write_text('---\nname: good\ndescription: "ok"\n---\nb\n', encoding="utf-8")
    reg5 = SkillsRegistry(cfg(tol_b, tol_ws))
    snap7 = reg5.snapshot()
    check("§7 INV-3 good 进 prompt", "<name>good</name>" in snap7.prompt)
    check("§7 INV-3 broken 不进 prompt", "<name>broken</name>" not in snap7.prompt)
    check("§7 broken 在 summary 且 errored",
          any(s.name == "broken" and s.eligibility_state.value == "errored" for s in snap7.skills))

    # §8 热重载（FR-021a / INV-6）
    hr_ws = tmp / "hr"; hr_ws.mkdir()
    hr_b = tmp / "hrb"; hr_b.mkdir()
    regr = SkillsRegistry(cfg(hr_b, hr_ws))
    v0 = regr.snapshot().version
    v1 = regr.snapshot().version
    new_d = hr_ws / "fresh"; new_d.mkdir()
    (new_d / "SKILL.md").write_text('---\nname: fresh\ndescription: "x"\n---\nb\n', encoding="utf-8")
    time.sleep(0.01)
    os.utime(new_d / "SKILL.md", None)
    v2 = regr.snapshot().version
    check("§8 INV-6 无变化 version 不变", v0 == v1)
    check("§8 INV-6 mtime 变 version 增", v2 > v1)
    check("§8 FR-021a 新 skill 被发现", "<name>fresh</name>" in regr.snapshot().prompt)

    # §9 Read 工具（FR-011/12）
    from agent_core.tools.builtin import READ_TOOL  # noqa: E402
    out = READ_TOOL.handler(path=str(hello / "SKILL.md"))
    check("§9 FR-011 Read 读 SKILL.md", "Skill-loaded hello" in out)
    check("§9 FR-012 Read name==Read", READ_TOOL.name == "Read")
    err = READ_TOOL.handler(path="/nonexistent-skill-xyz")
    check("§9 FR-012 缺文件不抛返错误串", isinstance(err, str) and len(err) > 0)

    print(f"\n{'='*50}\n✅ PASS: {len(PASS)}  ❌ FAIL: {len(FAIL)}")
    if FAIL:
        print("失败项:", FAIL)
        return 1
    print("全部脚本化节通过（§6 slash UI 需用户手动验证）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
