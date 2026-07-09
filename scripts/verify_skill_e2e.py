#!/usr/bin/env python3
"""
Skill 系统 E2E 验证脚本（T027 / quickstart §1）

验证 MVP 端到端：
  1. 放一个 hello/SKILL.md → SkillsRegistry.snapshot().prompt 含 hello
  2. Read 工具能按 location 读出 SKILL.md 内容
  3. SkillsPromptHandler 把 ## Skills 段注入 system message

完整 agent run（真实 LLM + Read tool_call）属 T028 手动 streamlit 验证。

用法：
    python3 scripts/verify_skill_e2e.py
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path


def main() -> int:
    # 保证 agent_core 可 import
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

    from agent_core.agent_state import RunState, TurnContext
    from agent_core.skills.config import SkillsConfig
    from agent_core.skills.registry import SkillsRegistry
    from agent_core.tools.builtin import READ_TOOL, register_builtin_tools
    from agent_core.tools.base import ToolRegistry
    from agent_core.turn_chain import SkillsPromptHandler
    from unittest.mock import MagicMock

    failures: list[str] = []

    def check(cond: bool, msg: str) -> None:
        mark = "✓" if cond else "✗"
        print(f"  {mark} {msg}")
        if not cond:
            failures.append(msg)

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        # 1. 准备 hello skill
        hello = tmp_path / "hello"
        hello.mkdir()
        (hello / "SKILL.md").write_text(
            '---\nname: hello\ndescription: "Greet the user warmly."\n---\n\nAlways say hi!\n',
            encoding="utf-8",
        )

        # 2. snapshot 含 hello
        cfg = SkillsConfig.from_dict({"paths": {"workspace_dir": str(tmp_path)}})
        reg = SkillsRegistry(cfg)
        snap = reg.snapshot()
        print(f"\n[1] snapshot: version={snap.version} mode={snap.render_mode.value}")
        check("hello" in snap.prompt, "snapshot.prompt 含 hello")
        check("<available_skills>" in snap.prompt, "含 <available_skills> 块")
        check("<location>" in snap.prompt, "含 <location> 字段")

        # 3. Read 工具按 location 读出
        reg_tools = ToolRegistry()
        register_builtin_tools(reg_tools)
        assert "Read" in reg_tools.list_names()
        loc = str(hello / "SKILL.md")
        out = READ_TOOL.handler(path=loc)
        print(f"\n[2] Read 工具读 {loc}")
        check("Always say hi" in out, "Read 读出 body 内容")
        check("hello" in out, "Read 读出 frontmatter name")

        # 4. SkillsPromptHandler 注入(选项 A:append 到 ctx.system_prompt)
        agent = MagicMock()
        agent.skills_registry = reg
        agent.tools = reg_tools
        ctx = TurnContext(run_state=RunState())
        ctx.system_prompt = "BASE"  # 模拟 SystemPromptHandler 已 append base
        SkillsPromptHandler(agent).handle(ctx)
        injected = ctx.system_prompt
        print("\n[3] SkillsPromptHandler 注入")
        check("## Skills (mandatory)" in injected, "system_prompt 含 ## Skills 段")
        check("BASE" in injected, "base system prompt 保留")
        check(injected.count("## Skills") == 1, "幂等：仅一份")

    print()
    if failures:
        print(f"FAILED: {len(failures)} 项未通过")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("PASSED: E2E 全部通过（snapshot + Read + handler 注入）")
    print("\n下一步（T028 手动）：streamlit run web/app.py，发 'say hello'，验证模型触发 Read tool_call")
    return 0


if __name__ == "__main__":
    sys.exit(main())
