"""R2 regression test:resume_after_permission 路径下,LLM 必须收到完整 system_prompt + tool_schemas。

Bug 现场(2026-07-07 11:20:11):
  - Read 工具被 PermissionCheckHandler ASK
  - web/app.py 调 agent.resume_after_permission("allow")
  - 下次 streamlit rerun 走 agent.step() loop
  - step() 创建新 TurnContext(空 system_prompt / tool_schemas,因为原 turn_ctx 已销毁)
  - LLMCallHandler 读 ctx.system_prompt → 空 → messages_for_llm 只剩 agent.messages
  - 后果:LLM REQUEST 日志显示 msgs=3 tools=0(对比正常路径 13:46:39 是 msgs=6 tools=4)
  - LLM 收到空 system + 无 tools → 返回 stop 语义(无 tool_call),conversation 提前结束

R2 修复(2026-07-07):
  - system_prompt / tool_schemas 从 TurnContext(per-turn)挪到 RunState(per-run)
  - LLMCallHandler 改读 ctx.run_state.system_prompt / ctx.run_state.tool_schemas
  - resume 重新走 step() 时,新 TurnContext 是空的但 RunState 持久保留 inputs_chain 产物
  - 期望:LLM 收到 msgs=6(1 system + agent.messages)tools=4

测试方法:
  - mock agent.llm.chat 捕获调用的 messages + tools 参数
  - 模拟 inputs_chain 已填 run_state.system_prompt / tool_schemas(SETUP 阶段产物)
  - 模拟 resume 后新 turn_ctx(空)+ 同一 run_state(有值)
  - 调 LLMCallHandler.handle
  - 断言 llm.chat 收到的 messages[0] = system prompt,tools 不为 None
"""
import pytest
from unittest.mock import MagicMock
from agent_core.agent_state import RunState, TurnContext
from agent_core.turn_chain import LLMCallHandler, SystemPromptHandler, ToolsSchemaPrepareHandler


def _make_mock_agent(llm_return=MagicMock()):
    """构造一个最小 mock agent,SystemPromptHandler + LLMCallHandler 都能跑。"""
    agent = MagicMock()
    # Phase 5: base 从 agent.llm.config.system_prompt 读（不再有 agent.system_prompt 字段）
    agent.llm.config.system_prompt = "你是一个 helpful 助手。\n## Tools 段\n## Skills 段"
    # memory_index=None 让 handler._build 跳过 MEMORY 段（否则 MagicMock 会污染 f-string）
    agent.memory_index = None
    agent.tools.list_schemas.return_value = [
        {"name": "Read", "description": "Read a file", "input_schema": {}},
        {"name": "Write", "description": "Write a file", "input_schema": {}},
        {"name": "Bash", "description": "Run a command", "input_schema": {}},
        {"name": "Glob", "description": "Find files", "input_schema": {}},
    ]
    agent.messages = [
        {"role": "user", "content": "请运行 echo hello"},
        {"role": "assistant", "content": [{"type": "tool_use", "id": "call_001", "name": "Read", "input": {"path": "/x"}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "call_001", "content": "OK"}]},
    ]
    # agent.llm / agent.llm.config 由 MagicMock 自动创建子 mock；
    # 不要显式 `= MagicMock()` 覆盖前面设的 config.system_prompt（MagicMock 替换会让 base 来源失效）
    agent.llm.chat.return_value = llm_return
    # llm.config.provider.name 是 detect_provider() 读的东西
    agent.llm.config.provider.name = "zhipu"
    agent._session_manager = None
    agent._last_turn_usage = None
    return agent


def test_r2_resume_path_llm_receives_full_system_prompt_and_tools():
    """R2 核心场景:resume 后 LLMCallHandler 必须把完整 system_prompt + tools 发给 LLM。

    模拟 SETUP 阶段已填 run_state.system_prompt + tool_schemas,
    模拟 resume_after_permission 后创建新 TurnContext(空),
    调 LLMCallHandler.handle,断言 LLM.chat 收到 system + tools。
    """
    agent = _make_mock_agent()
    run_state = RunState(user_message="请运行 echo hello")

    # ── 模拟 SETUP 阶段:inputs_chain 已跑完,run_state 填好 system_prompt + tool_schemas ──
    # 真实链路: SystemPromptHandler → MemoryRetrievalHandler → SkillsPromptHandler → ToolsSchemaPrepareHandler
    # 这里只跑 SystemPrompt + ToolsSchema(简化)
    setup_ctx = TurnContext(run_state=run_state)
    SystemPromptHandler(agent).handle(setup_ctx)
    ToolsSchemaPrepareHandler(agent).handle(setup_ctx)
    # setup_ctx 销毁(generator semantics):setup_ctx 即将丢失,但 run_state 保留

    assert run_state.system_prompt  # SETUP 产物在 run_state
    assert run_state.tool_schemas and len(run_state.tool_schemas) == 4

    # ── 模拟 resume 路径:新 TurnContext(空 system_prompt / tool_schemas)+ 同一 run_state ──
    resume_ctx = TurnContext(run_state=run_state)
    # 关键:resume_ctx 自身不含 system_prompt / tool_schemas(已删字段)
    assert not hasattr(resume_ctx, "system_prompt")
    assert not hasattr(resume_ctx, "tool_schemas")

    # ── 调 LLMCallHandler.handle(模拟 LLM_THINKING 阶段) ──
    LLMCallHandler(agent).handle(resume_ctx)

    # ── 断言:agent.llm.chat 收到完整 system_prompt + 4 个 tools ──
    agent.llm.chat.assert_called_once()
    call_kwargs = agent.llm.chat.call_args.kwargs
    messages = call_kwargs["messages"]
    tools = call_kwargs["tools"]

    # system prompt 是 messages[0]
    assert messages[0]["role"] == "system"
    assert messages[0]["content"] == run_state.system_prompt
    assert "你是一个 helpful 助手" in messages[0]["content"]
    # agent.messages 全部追加
    assert len(messages) == 1 + len(agent.messages)
    # tools 是 4 个(不是 0,不是 None)
    assert tools is not None
    assert len(tools) == 4
    assert {t["name"] for t in tools} == {"Read", "Write", "Bash", "Glob"}


def test_r2_resume_path_llm_still_works_when_run_state_uninitialized():
    """边界:RunState 是新创建的空 run_state(没有 inputs_chain 产物)。

    这种情况对应:resume 路径走到时,RunState 是从 agent._run_state 拿的旧值,
    但 inputs_chain 没机会重新跑(因为不在 SETUP 阶段)。但如果 RunState 是 brand new,
    LLMCallHandler 应能 graceful 处理(None → messages_for_llm 只含 agent.messages)。

    这是防御性测试,确保 R2 改动没破坏"NoSystemPrompt" 兼容路径。
    """
    agent = _make_mock_agent()
    run_state = RunState()  # 默认空
    # 不跑 SystemPromptHandler / ToolsSchemaPrepare,模拟异常路径
    assert run_state.system_prompt == ""
    assert run_state.tool_schemas is None

    ctx = TurnContext(run_state=run_state)
    LLMCallHandler(agent).handle(ctx)

    agent.llm.chat.assert_called_once()
    call_kwargs = agent.llm.chat.call_args.kwargs
    messages = call_kwargs["messages"]
    tools = call_kwargs["tools"]

    # system prompt 缺失时,messages[0] = 第一条 agent.message(无 system role 头部)
    assert messages[0] != {"role": "system", "content": ""}  # 没有空 system 段
    assert messages[0]["role"] == "user"  # 第一条是 user
    # tools=None 时优雅降级(不传 tools 给 LLM)
    assert tools is None


def test_r2_run_state_persists_across_turn_ctx_lifecycle():
    """核心不变式:同一 run_state 跨 turn_ctx 生命周期读 system_prompt 必须一致。

    模拟一个 run 内:
      - Turn 1: SETUP 跑 inputs_chain → run_state.system_prompt = "T1_prompt"
      - Turn 1 销毁 turn_ctx
      - Turn 2(resume 路径):新 turn_ctx(空)+ 同一 run_state
      - LLMCallHandler 应能读到 "T1_prompt"(因为 run_state 持久)
    """
    agent = _make_mock_agent()
    run_state = RunState()

    # ── Turn 1: SETUP ──
    turn1_ctx = TurnContext(run_state=run_state)
    SystemPromptHandler(agent).handle(turn1_ctx)
    assert run_state.system_prompt  # 已填
    # 模拟 turn 1 结束,turn_ctx 销毁(del 即可,Python GC 兜底)
    del turn1_ctx

    # ── Turn 2: resume 路径,新 turn_ctx ──
    turn2_ctx = TurnContext(run_state=run_state)
    # 关键不变式:run_state.system_prompt 还在(没被 turn1_ctx 销毁带走)
    assert run_state.system_prompt  # 持久
    # 验证 LLMCallHandler 仍能正确拼 messages
    LLMCallHandler(agent).handle(turn2_ctx)
    messages = agent.llm.chat.call_args.kwargs["messages"]
    assert messages[0]["role"] == "system"
    assert messages[0]["content"] == run_state.system_prompt
