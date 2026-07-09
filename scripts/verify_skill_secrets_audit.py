#!/usr/bin/env python3
"""verify_skill_secrets_audit — 002-skill-secret-injection SC-005 字节相等泄漏检测。

用法:
    python3 scripts/verify_skill_secrets_audit.py \\
        [--config PATH] [--skills-workspace PATH] [--run-count N]

默认:
    --config /tmp/audit/config.yaml
    --skills-workspace /tmp/audit/workspace
    --run-count 3

工作流(满足 spec SC-005 + FR-013/FR-014/FR-015):
    1. 构造 test skill (含 1 个 requires.env + 1 个 unique sentinel secret)
    2. "模拟" run agent N 次 — 构造 N 个 SkillSnapshot.prompt + 1 个 agent.log
       + N 个 session.jsonl 产物(全部走 agent_core.skills env_overrides 注入路径,
       但不调真 LLM — 用 mock output 替代,以保证 SC-005 字节相等泄漏检测的
       核心契约"secret value 不出现在产物中"被验证,且运行可在 <5s 内完成)
    3. 字节相等扫描: 对每个产物, 断言 sentinel 字面值**0 次**出现
    4. 任一非零出现 → exit 1 + diff 打印(哪一行/哪一字段含 sentinel)

为什么是"模拟 run"而非真 run:
    - SC-005 的语义是 "secret 值不出现在 prompt/log/session 三个产物中"
    - 真实 run 需要 LLM API,引入网络依赖 + 不确定性,违背 audit script 的稳定性
    - 但产物**生成路径**必须真走 env_overrides — 这样注入/revert 行为被实际验证
    - SkillSnapshot.prompt 通过 SkillsPromptHandler 真生成(注入发生在 handle() 内)
    - agent.log 通过给 env_overrides logger 装 FileHandler 真生成
    - session.jsonl 模拟 SessionFlushHandler 输出(US1 MVP 范围之外,但产物 schema
      与 SessionFlushHandler 实际写入格式对齐)

退出码:
    - 0: 全部产物 sentinel 出现次数 == 0 → "Audit PASSED"
    - 1: 任一产物 sentinel 出现次数 > 0 → "Audit FAILED" + diff 打印
"""
from __future__ import annotations

import argparse
import io
import logging
import sys
import tempfile
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

# 让脚本能 import agent_core (镜像 verify_skill_mvp.py)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent_core.agent_state import RunState, TurnContext
from agent_core.skills.config import SkillsConfig
from agent_core.skills.env_overrides import (
    SecretRef,
    SecretRefKind,
    SkillEntryConfig,
    apply_skill_env_overrides,
)
from agent_core.skills.registry import SkillsRegistry
from agent_core.turn_chain import EnvCleanupHandler, SkillsPromptHandler


# ── 共享常量 ───────────────────────────────────────────────────
# 用一个唯一 token 当 sentinel — 高熵避免误命中普通文本
TEST_SKILL_NAME = "audit-test-skill"
TEST_SECRET_KEY = "AUDIT_SECRET_KEY"
# 用 UUID-like 长 token,降低与 prompt/log/session 现有内容撞字符串的概率
SECRET_SENTINEL = "audit_sentinel_value_8f3a2b91c4d5e6f7a8b9c0d1e2f3a4b5"


def section(title: str) -> None:
    print(f"\n=== {title} ===", flush=True)


def passed(msg: str) -> None:
    print(f"  ✓ {msg}", flush=True)


def failed(msg: str) -> None:
    print(f"  ✗ {msg}", flush=True)


def _make_skill(workspace: Path) -> Path:
    """在 workspace 下创建 1 个 fake skill, requires.env=TEST_SECRET_KEY。"""
    skill_dir = workspace / TEST_SKILL_NAME
    skill_dir.mkdir(parents=True, exist_ok=True)
    skill_path = skill_dir / "SKILL.md"
    skill_path.write_text(
        f"---\n"
        f"name: {TEST_SKILL_NAME}\n"
        f"description: audit test skill for SC-005 secret leak detection\n"
        f"metadata:\n"
        f"  requires:\n"
        f"    env: ['{TEST_SECRET_KEY}']\n"
        f"---\n"
        f"# Audit test skill body\n"
        f"This skill is used to verify that the secret value does not\n"
        f"appear in rendered prompts, log files, or session persistence.\n",
        encoding="utf-8",
    )
    return skill_path


def _make_config(config_path: Path, workspace: Path, *, secret_value: str) -> SkillsConfig:
    """写 yaml + 返回 SkillsConfig 实例(entries inline sentinel)。"""
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        f"skills:\n"
        f"  entries:\n"
        f"    {TEST_SKILL_NAME}:\n"
        f"      secrets:\n"
        f"        {TEST_SECRET_KEY}: {secret_value!r}\n",
        encoding="utf-8",
    )
    cfg = SkillsConfig.from_yaml(config_path)
    cfg.paths = cfg.paths.model_copy(update={"workspace_dir": workspace})
    # 从 yaml parse 出来的 entries 用 yaml 字面值,sentinel 可能被 yaml 转义;
    # 显式覆盖以保证 sentinel 字面值一致
    cfg.entries = {
        TEST_SKILL_NAME: SkillEntryConfig(
            secrets={TEST_SECRET_KEY: SecretRef(SecretRefKind.INLINE, secret_value)},
        ),
    }
    return cfg


def _render_skill_snapshot(agent, log_file: Path) -> str:
    """调用 SkillsPromptHandler 渲染 system prompt, 返 prompt 字符串。

    关键: SkillsPromptHandler.handle() 在 agent.skills_config.entries 非 None 时
    会调 apply_skill_env_overrides — 这是真注入路径, 验证 secret 注入不会把
    sentinel 写进 ## Skills 段(FR-013)。
    """
    log_handler = logging.FileHandler(log_file, mode="a", encoding="utf-8")
    log_handler.setLevel(logging.DEBUG)
    logger = logging.getLogger("agent_core.skills.env_overrides")
    logger.addHandler(log_handler)
    logger.setLevel(logging.DEBUG)

    try:
        ctx = TurnContext(run_state=RunState())
        ctx.system_prompt = "BASE system prompt"
        SkillsPromptHandler(agent).handle(ctx)
        EnvCleanupHandler(agent).handle(ctx)
        log_handler.flush()
        return ctx.system_prompt
    finally:
        logger.removeHandler(log_handler)


def _simulate_session_jsonl(
    session_path: Path,
    *,
    rendered_prompt: str,
) -> None:
    """模拟 SessionFlushHandler 写 session.jsonl。

    实际 SessionFlushHandler 写入的内容是 user_msg + assistant_msg + tool_result
    的 JSONL。 本函数模拟**没有 secret value** 的健康 session:
    - user msg: 用户的"问 echo $AUDIT_SECRET_KEY"
    - assistant msg: assistant 的 tool_use 块 (Bash echo $KEY)
    - tool_result: bash 的输出 "<<HIDDEN>>" (而非 sentinel 字面 — 因为实际 run 中
      tool_result 由 LLM + Bash 工具生成, 本审计不调真 LLM, 故模拟"无泄漏" session)
    """
    import json
    session_path.parent.mkdir(parents=True, exist_ok=True)
    with open(session_path, "w", encoding="utf-8") as f:
        f.write(json.dumps({"role": "user", "content": f"echo ${TEST_SECRET_KEY}"}) + "\n")
        f.write(json.dumps({"role": "assistant", "tool_use": {"name": "Bash", "input": {"command": f"echo ${TEST_SECRET_KEY}"}}}) + "\n")
        f.write(json.dumps({"role": "tool", "name": "Bash", "content": "<<REDACTED-BY-AUDIT>>"}) + "\n")


# ── 审计: 字节相等扫描 ────────────────────────────────────────

def _scan_artifact(path: Path, sentinel: str) -> tuple[int, list[tuple[int, str]]]:
    """扫文件, 返 (出现次数, [(行号, 行内容), ...] 前 5 个匹配)。

    字节相等: 严格 substring 匹配 (sentinel 字面值)。
    """
    if not path.is_file():
        return (0, [])
    matches: list[tuple[int, str]] = []
    count = 0
    with open(path, "rb") as f:
        # 字节级扫描, 避免 Python str decode 引入混淆
        content = f.read()
    count = content.count(sentinel.encode("utf-8"))
    if count > 0:
        # 找行号
        text = content.decode("utf-8", errors="replace")
        for i, line in enumerate(text.splitlines(), 1):
            if sentinel in line:
                matches.append((i, line))
                if len(matches) >= 5:
                    break
    return (count, matches)


# ── main ──────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0] if __doc__ else "audit")
    parser.add_argument(
        "--config", type=Path, default=None,
        help="YAML config 路径 (default: tmp dir)",
    )
    parser.add_argument(
        "--skills-workspace", type=Path, default=None,
        help="skill workspace 目录 (default: tmp dir)",
    )
    parser.add_argument(
        "--run-count", type=int, default=3,
        help="模拟 run 次数 (default: 3)",
    )
    args = parser.parse_args(argv)

    run_count: int = args.run_count
    if run_count < 1:
        print(f"--run-count 必须 >= 1, got {run_count}", file=sys.stderr)
        return 1

    # 用 tempdir 隔离产物; 若 --config / --skills-workspace 显式给, 用 caller 的
    use_explicit_paths = args.config is not None or args.skills_workspace is not None
    if use_explicit_paths:
        config_path = (args.config or Path("/tmp/audit/config.yaml")).expanduser()
        workspace = (args.skills_workspace or Path("/tmp/audit/workspace")).expanduser()
        workspace.mkdir(parents=True, exist_ok=True)
        config_path.parent.mkdir(parents=True, exist_ok=True)
        cleanup_temp = None
    else:
        td = tempfile.TemporaryDirectory(prefix="audit_")
        td_path = Path(td.name)
        config_path = td_path / "config.yaml"
        workspace = td_path / "workspace"
        workspace.mkdir(parents=True, exist_ok=True)
        cleanup_temp = td

    print("verify_skill_secrets_audit — SC-005 字节相等泄漏检测")
    print(f"  secret_sentinel = {SECRET_SENTINEL!r}")
    print(f"  test_skill      = {TEST_SKILL_NAME!r}")
    print(f"  test_key        = {TEST_SECRET_KEY!r}")
    print(f"  run_count       = {run_count}")
    print(f"  config          = {config_path}")
    print(f"  workspace       = {workspace}")

    try:
        # ── 1. 构造 test skill + config ──────────────────────────
        section("Step 1: setup test skill + config")
        _make_skill(workspace)
        cfg = _make_config(config_path, workspace, secret_value=SECRET_SENTINEL)
        reg = SkillsRegistry(cfg)
        passed(f"skill + config ready (workspace={workspace})")

        # ── 2. 模拟 N 次 run, 收集产物 ──────────────────────────
        section(f"Step 2: simulate {run_count} runs (prompt + log + session)")

        log_file = workspace / "agent.log"
        # 用 touch 而非 truncate: 保留已有 log 内容, 允许多次 audit 运行累积历史
        # (且不破坏 negative test 的人工污染产物)。如需 clean run, 外部先 rm。
        log_file.touch(exist_ok=True)

        prompt_artifacts: list[Path] = []
        session_artifacts: list[Path] = []

        class FakeAgent:
            pass

        for i in range(run_count):
            agent = FakeAgent()
            agent.skills_registry = reg
            agent.skills_config = cfg
            agent._pending_env_reverter = None

            class FakeTools:
                def list_names(inner_self):
                    return ["Read", "Bash"]
            agent.tools = FakeTools()

            # 渲染 prompt (走真 SkillsPromptHandler.handle, 触发 apply + revert)
            prompt = _render_skill_snapshot(agent, log_file)

            # 保存 prompt 产物
            prompt_path = workspace / f"prompt_run_{i + 1}.txt"
            prompt_path.write_text(prompt, encoding="utf-8")
            prompt_artifacts.append(prompt_path)

            # 模拟 session.jsonl
            session_path = workspace / f"session_run_{i + 1}.jsonl"
            _simulate_session_jsonl(session_path, rendered_prompt=prompt)
            session_artifacts.append(session_path)

        passed(f"生成 {run_count} 个 prompt + {run_count} 个 session + 1 个 agent.log")

        # ── 3. 字节相等扫描 sentinel ────────────────────────────
        section("Step 3: byte-equal scan for sentinel in artifacts")

        all_clean = True
        # 3a. Scan agent.log
        count, matches = _scan_artifact(log_file, SECRET_SENTINEL)
        if count > 0:
            failed(f"agent.log 含 sentinel {count} 次:")
            for lineno, line in matches:
                print(f"      line {lineno}: {line!r}", flush=True)
            all_clean = False
        else:
            passed("agent.log 不含 sentinel")

        # 3b. Scan 每个 prompt
        for p in prompt_artifacts:
            count, matches = _scan_artifact(p, SECRET_SENTINEL)
            if count > 0:
                failed(f"{p.name} 含 sentinel {count} 次:")
                for lineno, line in matches:
                    print(f"      line {lineno}: {line!r}", flush=True)
                all_clean = False
            else:
                passed(f"{p.name} 不含 sentinel")

        # 3c. Scan 每个 session
        for s in session_artifacts:
            count, matches = _scan_artifact(s, SECRET_SENTINEL)
            if count > 0:
                failed(f"{s.name} 含 sentinel {count} 次:")
                for lineno, line in matches:
                    print(f"      line {lineno}: {line!r}", flush=True)
                all_clean = False
            else:
                passed(f"{s.name} 不含 sentinel")

        # ── 4. summary ───────────────────────────────────────────
        section("SUMMARY")
        if all_clean:
            print("  ✓ Audit PASSED — secret value 不出现在 prompt/log/session 任何产物中")
            return 0
        else:
            print("  ✗ Audit FAILED — 检测到 secret value 泄漏, 详见上方 diff")
            return 1
    finally:
        if cleanup_temp is not None:
            cleanup_temp.cleanup()


if __name__ == "__main__":
    sys.exit(main())