"""tests/test_skills_prompt_handler.py — SkillsPromptHandler 注入 + C2 guard(选项 A)。

选项 A 重构 (2026-07-06):handler 用 ctx.append_system 累加 skills 段到 ctx.system_prompt
(替代原 stage_inputs merge)。范式:真实 TurnContext + mock agent → handle → 断言 ctx.system_prompt。

002-skill-secret-injection (T012/T037/T039, 2026-07-06):
- TestSecretEnvInjection:handler 触发 secret env 注入 + reverter 登记
- TestSkillsConfigExposure:agent.skills_config attribute 暴露契约
- TestEnvCleanupHandler:outputs_chain 末位 cleanup,idempotent,no-op when None
"""
from __future__ import annotations

import os
from unittest.mock import MagicMock

import pytest

from agent_core.agent_state import RunState, TurnContext
from agent_core.skills.config import SkillsConfig
from agent_core.skills.env_overrides import (
    SecretRef,
    SecretRefKind,
    SkillEntryConfig,
)
from agent_core.skills.registry import SkillsRegistry
from agent_core.turn_chain import (
    EnvCleanupHandler,
    HandlerResult,
    SkillsPromptHandler,
)


def _make_registry_with_workspace(tmp_path) -> SkillsRegistry:
    """构造一个指向 tmp_path workspace 的 registry(含 1 个 hello skill)"""
    skill_dir = tmp_path / "hello"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        '---\nname: hello\ndescription: "Greet"\n---\nbody\n',
        encoding="utf-8",
    )
    cfg = SkillsConfig.from_dict({"paths": {"workspace_dir": str(tmp_path)}})
    return SkillsRegistry(cfg)


def _make_ctx(system_prompt: str = "BASE") -> TurnContext:
    """构造真实 TurnContext,system_prompt 预设(模拟 SystemPromptHandler 已 append base)。"""
    ctx = TurnContext(run_state=RunState())
    ctx.system_prompt = system_prompt
    return ctx


def _make_agent(registry=None, tool_names=None):
    """构造 mock agent,含 skills_registry + tools.list_names()。"""
    agent = MagicMock()
    agent.skills_registry = registry
    tools = MagicMock()
    tools.list_names.return_value = tool_names if tool_names is not None else ["Read", "calc", "bash"]
    agent.tools = tools
    return agent


class TestInjection:
    def test_appends_skills_section_to_system_prompt(self, tmp_path):
        reg = _make_registry_with_workspace(tmp_path)
        agent = _make_agent(registry=reg)
        ctx = _make_ctx("BASE")
        result = SkillsPromptHandler(agent).handle(ctx)
        assert isinstance(result, HandlerResult)
        assert "## Skills (mandatory)" in ctx.system_prompt
        assert "BASE" in ctx.system_prompt  # base 保留
        assert "<name>hello</name>" in ctx.system_prompt

    def test_preserves_existing_memory_block(self, tmp_path):
        # 关键:MemoryRetrieval 已 append mem_block,skills 不能覆盖它(append_system 累加)
        reg = _make_registry_with_workspace(tmp_path)
        agent = _make_agent(registry=reg)
        ctx = _make_ctx("BASE\n\n[记忆库 / 3 hits]\n- ...")
        SkillsPromptHandler(agent).handle(ctx)
        assert "[记忆库" in ctx.system_prompt  # memory 保留
        assert "## Skills" in ctx.system_prompt  # skills 追加(非覆盖)

    def test_idempotent(self, tmp_path):
        # 重复 handle 不翻倍(幂等自检:section 已在 ctx.system_prompt 则跳过)
        reg = _make_registry_with_workspace(tmp_path)
        agent = _make_agent(registry=reg)
        ctx = _make_ctx("BASE")
        SkillsPromptHandler(agent).handle(ctx)
        SkillsPromptHandler(agent).handle(ctx)  # 再调一次
        assert ctx.system_prompt.count("## Skills (mandatory)") == 1  # 没翻倍


class TestC2Guard:
    """spec Edge Case / analyze C2:Read 不在 tool set → 跳过。"""

    def test_skip_when_read_not_in_tools(self, tmp_path):
        reg = _make_registry_with_workspace(tmp_path)
        agent = _make_agent(registry=reg, tool_names=["calc", "bash"])  # 无 Read
        ctx = _make_ctx("BASE")
        SkillsPromptHandler(agent).handle(ctx)
        assert "## Skills" not in ctx.system_prompt
        assert ctx.system_prompt == "BASE"  # 未改

    def test_skip_when_tools_none(self, tmp_path):
        reg = _make_registry_with_workspace(tmp_path)
        agent = MagicMock()
        agent.skills_registry = reg
        agent.tools = None
        ctx = _make_ctx("BASE")
        result = SkillsPromptHandler(agent).handle(ctx)
        assert isinstance(result, HandlerResult)
        assert "## Skills" not in ctx.system_prompt


class TestSkipConditions:
    def test_skip_when_registry_none(self):
        agent = _make_agent(registry=None)
        ctx = _make_ctx("BASE")
        SkillsPromptHandler(agent).handle(ctx)
        assert ctx.system_prompt == "BASE"  # 未改

    def test_skip_when_empty_prompt(self, tmp_path):
        # bundled + workspace 都空 → snapshot.prompt 为空 → 不注入
        empty_ws = tmp_path / "empty_ws"
        empty_ws.mkdir()
        empty_bd = tmp_path / "empty_bd"
        empty_bd.mkdir()
        cfg = SkillsConfig.from_dict({
            "paths": {
                "bundled_dir": str(empty_bd),
                "workspace_dir": str(empty_ws),
            }
        })
        reg = SkillsRegistry(cfg)
        agent = _make_agent(registry=reg)
        ctx = _make_ctx("BASE")
        SkillsPromptHandler(agent).handle(ctx)
        assert ctx.system_prompt == "BASE"  # 无 skill → 不注入

    def test_injects_into_empty_system_prompt(self, tmp_path):
        # 边界:system_prompt 空(SystemPromptHandler 未跑或 base 为空)→ 仍注入 skills 段
        reg = _make_registry_with_workspace(tmp_path)
        agent = _make_agent(registry=reg)
        ctx = _make_ctx("")
        SkillsPromptHandler(agent).handle(ctx)
        assert "## Skills" in ctx.system_prompt  # skills 段注入


class TestSnapshotFailureIsolation:
    def test_handler_does_not_raise_on_snapshot_error(self):
        # registry.snapshot() 抛 → handler 吞掉,不改 ctx.system_prompt
        bad_reg = MagicMock()
        bad_reg.snapshot.side_effect = RuntimeError("boom")
        agent = _make_agent(registry=bad_reg)
        ctx = _make_ctx("BASE")
        result = SkillsPromptHandler(agent).handle(ctx)
        assert isinstance(result, HandlerResult)
        assert ctx.system_prompt == "BASE"  # 失败 → 不改


# ────────────────────────────────────────────────────────────────────
# 002-skill-secret-injection T012 — handler 触发 secret env 注入
# ────────────────────────────────────────────────────────────────────

def _make_agent_with_secrets(
    tmp_path, env_keys: list[str]
):
    """构造 mock agent:
    - skills_registry: 1 skill 'echo', requires.env=env_keys
    - skills_config.entries['echo'].secrets: 每 key 配 INLINE SecretRef
    - tools 含 'Read'(通过 C2 guard)
    """
    skill_dir = tmp_path / "echo"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        f'---\nname: echo\ndescription: "echo"\nmetadata:\n  requires:\n    env: {env_keys!r}\n---\nbody\n',
        encoding="utf-8",
    )
    cfg = SkillsConfig.from_dict({"paths": {"workspace_dir": str(tmp_path)}})
    reg = SkillsRegistry(cfg)

    # 给 cfg 加 entries(echo skill 对应每个 env key 一个 inline secret)
    cfg.entries = {
        "echo": SkillEntryConfig(
            secrets={
                k: SecretRef(SecretRefKind.INLINE, f"value_for_{k}")
                for k in env_keys
            },
        ),
    }
    agent = MagicMock()
    agent.skills_registry = reg
    agent.skills_config = cfg
    agent._pending_env_reverter = None
    tools = MagicMock()
    tools.list_names.return_value = ["Read"]
    agent.tools = tools
    return agent


@pytest.fixture(autouse=False)
def _clean_inject_env(monkeypatch):
    """每个 case 前后清掉可能注入的 env key(US1 MVP 隔离)。"""
    keys = ["SECRET_KEY_A", "SECRET_KEY_B", "SECRET_KEY_C"]
    for k in keys:
        monkeypatch.delenv(k, raising=False)
    yield


class TestSecretEnvInjection:
    """T012: handler 集成后, 注入 env + reverter 在 agent 上登记。
    验证 'no in-handler-revert' 语义(handler 不应在 handle 期间就 revert)。
    """

    def test_env_injected_equals_config_value(
        self, tmp_path, _clean_inject_env
    ):
        """handle() 后, agent.skills_config.entries 中的 inline 值出现在 os.environ。"""
        agent = _make_agent_with_secrets(tmp_path, ["SECRET_KEY_A", "SECRET_KEY_B"])
        ctx = _make_ctx("BASE")
        SkillsPromptHandler(agent).handle(ctx)
        # INLINE 注入完毕
        assert os.environ.get("SECRET_KEY_A") == "value_for_SECRET_KEY_A"
        assert os.environ.get("SECRET_KEY_B") == "value_for_SECRET_KEY_B"

    def test_env_still_set_after_handler_no_in_handler_revert(
        self, tmp_path, _clean_inject_env
    ):
        """handle() 不立即 revert(语义:handler 只 inject + register reverter)。
        EnvCleanupHandler 在 outputs_chain 末位才负责清理。
        """
        agent = _make_agent_with_secrets(tmp_path, ["SECRET_KEY_C"])
        ctx = _make_ctx("BASE")
        SkillsPromptHandler(agent).handle(ctx)
        # handler 跑完后 env 仍在(直到 EnvCleanupHandler 触发)
        assert os.environ.get("SECRET_KEY_C") == "value_for_SECRET_KEY_C"
        # reverter 已被挂在 agent 上供 outputs_chain 清理
        assert agent._pending_env_reverter is not None
        assert callable(agent._pending_env_reverter)


class TestSkillsConfigExposure:
    """T037: agent.skills_config attribute 暴露契约。

    用例:
    - skills_config 是 SkillsConfig 实例,可读 entries
    - skills_config is None 时(Skill 系统关)→ handler 不抛
    """

    def test_skills_config_attribute_readable(self, tmp_path):
        agent = _make_agent_with_secrets(tmp_path, ["SECRET_KEY_A"])
        assert isinstance(agent.skills_config, SkillsConfig)
        assert "echo" in agent.skills_config.entries
        assert "SECRET_KEY_A" in agent.skills_config.entries["echo"].secrets

    def test_skills_config_none_handler_no_raise(self):
        """skills_config=None(Skill 系统关)→ handler 不抛, 走原逻辑。"""
        agent = MagicMock()
        agent.skills_registry = None
        agent.skills_config = None
        agent._pending_env_reverter = None
        agent.tools = None
        ctx = _make_ctx("BASE")
        result = SkillsPromptHandler(agent).handle(ctx)
        assert isinstance(result, HandlerResult)
        assert ctx.system_prompt == "BASE"


class TestEnvCleanupHandler:
    """T039: EnvCleanupHandler 行为契约。

    - 注入 → handle → env 不再含注入值
    - 二次调用 idempotent(agent._pending_env_reverter 已 None,no-op)
    - reverter is None 时 handle 不抛
    """

    def test_handle_reverts_injected_env(
        self, tmp_path, _clean_inject_env
    ):
        """handler: 注入 + handle() 后 env 被还原。"""
        agent = _make_agent_with_secrets(tmp_path, ["SECRET_KEY_A"])
        ctx = _make_ctx("BASE")
        SkillsPromptHandler(agent).handle(ctx)
        # 注入完毕
        assert os.environ.get("SECRET_KEY_A") == "value_for_SECRET_KEY_A"
        # cleanup handler 跑
        EnvCleanupHandler(agent).handle(ctx)
        # env 还原
        assert "SECRET_KEY_A" not in os.environ
        # reverter 已 None(idempotent guard)
        assert agent._pending_env_reverter is None

    def test_handle_idempotent_when_called_twice(
        self, tmp_path, _clean_inject_env
    ):
        """二次 handle:第二次 no-op,不抛。"""
        agent = _make_agent_with_secrets(tmp_path, ["SECRET_KEY_B"])
        ctx = _make_ctx("BASE")
        SkillsPromptHandler(agent).handle(ctx)
        # 第一次 cleanup
        EnvCleanupHandler(agent).handle(ctx)
        # 第二次 cleanup(应 no-op)
        EnvCleanupHandler(agent).handle(ctx)
        # reverter 仍 None
        assert agent._pending_env_reverter is None
        assert "SECRET_KEY_B" not in os.environ

    def test_handle_noop_when_reverter_none(self):
        """agent._pending_env_reverter is None → handle 不抛, 不做任何事。"""
        agent = MagicMock()
        agent._pending_env_reverter = None
        ctx = _make_ctx("BASE")
        result = EnvCleanupHandler(agent).handle(ctx)
        assert isinstance(result, HandlerResult)
        # reverter 仍 None


# ────────────────────────────────────────────────────────────────────
# 2026-07-06 bug fix:SkillsPromptHandler.handle 顺序
# ────────────────────────────────────────────────────────────────────

class TestHandlerOrderingBug:
    """2026-07-06 bug fix:handler 必须在 snapshot rebuild 之前注入 secret。

    原 bug:`registry.entries` property 触发 snapshot() → rebuild 时
    SECRET_DEMO 还没 inject → echo-skill 被 filter exclude。第二次 rebuild
    是 by accident 触发(env hash 变)。

    修复:handler 用 `registry.load_entries_for_injection()`(不触发 snapshot),
    然后才 snapshot()。
    """

    def test_load_entries_for_injection_does_not_trigger_build_snapshot(self, tmp_path):
        """registry.load_entries_for_injection() 只填 _last_entries_by_name,
        不写 _cached / _cached_sig → 不触发 build_snapshot。"""
        reg = _make_registry_with_workspace(tmp_path)
        # 初始状态:_cached=None(没 snapshot 过)
        assert reg._cached is None
        assert reg._cached_sig is None

        entries = reg.load_entries_for_injection()

        # entries 已填,但 snapshot 缓存还是空的
        assert len(entries) >= 1
        assert reg._cached is None, "_cached 仍 None — load_entries_for_injection 不应触发 rebuild"
        assert reg._cached_sig is None
        # _last_entries_by_name 已填(load 复用)
        assert "hello" in reg._last_entries_by_name

        # 后续 snapshot() 调用才真正 rebuild(因为 _cached 是 None)
        reg.snapshot()
        assert reg._cached is not None
        assert reg._cached_sig is not None

    def test_handler_injects_secret_before_snapshot_rebuild(self, tmp_path, monkeypatch):
        """SkillsPromptHandler.handle 注入 secret 在 registry.snapshot() rebuild 之前。

        验证策略:spy on registry.snapshot() call count。
        - 修复前 (用 entries property):handler 调 2 次 snapshot(load + _build_section)
          → echo-skill 第 1 次 rebuild 时 SECRET_DEMO 还没注入 → 被 exclude
        - 修复后 (用 load_entries_for_injection):handler 调 1 次 snapshot(_build_section)
          → SECRET_DEMO 已注入 → rebuild 时 echo-skill ELIGIBLE
        """
        # env 注入的 key 必须在 fixture 启动前清理,避免残留
        monkeypatch.delenv("ORDERING_KEY_X", raising=False)

        reg = _make_registry_with_workspace(tmp_path)
        cfg = reg._config
        # 加一个需要 ORDERING_KEY_X 的 ordering-skill(模拟 echo-skill)
        skill_dir = tmp_path / "ordering-skill"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            '---\nname: ordering-skill\ndescription: "ordering"\n'
            'metadata:\n  requires:\n    env: ["ORDERING_KEY_X"]\n---\nbody\n',
            encoding="utf-8",
        )
        cfg.entries = {
            "ordering-skill": SkillEntryConfig(
                secrets={"ORDERING_KEY_X": SecretRef(SecretRefKind.INLINE, "ordering_value_zzz")},
            ),
        }

        # spy:把 snapshot 替换成计数器版(bind to instance)
        call_log: list[str] = []
        orig_snapshot = reg.snapshot
        def counting_snapshot():
            call_log.append("snapshot")
            return orig_snapshot()
        # monkey-patch via type bound method(避免 __get__ 解析)
        import types
        reg.snapshot = types.MethodType(lambda self: counting_snapshot(), reg)

        agent = MagicMock()
        agent.skills_registry = reg
        agent.skills_config = cfg
        agent._pending_env_reverter = None
        tools = MagicMock()
        tools.list_names.return_value = ["Read"]
        agent.tools = tools

        ctx = _make_ctx("BASE")
        SkillsPromptHandler(agent).handle(ctx)

        # ✅ 修复后:handler 只在 _build_section 调 1 次 snapshot
        # ❌ 修复前:entries property 调 1 次 + _build_section 调 1 次 = 2 次
        assert len(call_log) == 1, (
            f"BUG:handler 应只调 1 次 snapshot (_build_section),实际 {len(call_log)} 次。"
            f"多出的调用是 entries property 触发的 — 表示 secret 在 snapshot 之后注入"
        )
        # env 实际注入
        assert os.environ.get("ORDERING_KEY_X") == "ordering_value_zzz"
        # ordering-skill 应在最终 snapshot 中(eligibility 不再 missing)
        final_snap = reg.snapshot()
        assert "ordering-skill" in final_snap.prompt, (
            "ordering-skill 应在最终 prompt — SECRET_DEMO 注入后 rebuild 让 ELIGIBLE"
        )
