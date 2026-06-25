"""
真实 LLM 对话场景测试 (GLM-5.1)

数据驱动: 场景定义在 data/scenarios.yaml, 改 YAML 加新场景即可, 不需写 Python。

跑法:
    pytest tests/e2e/test_05_chat_scenarios.py -v
    pytest tests/e2e/test_05_chat_scenarios.py -v -k tool
    pytest tests/e2e/test_05_chat_scenarios.py -v -k basic
    pytest tests/e2e/test_05_chat_scenarios.py -v -k multiturn

每个场景是一个独立用例, 报告里能看到 id/name/状态/响应摘要。
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import pytest
import yaml

from .pages.chat_page import ChatPage


# ────────────────────────────────────────────────────────────
# 加载 YAML 场景
# ────────────────────────────────────────────────────────────
SCENARIOS_PATH = Path(__file__).parent / "data" / "scenarios.yaml"


def _load_scenarios() -> list[dict[str, Any]]:
    with open(SCENARIOS_PATH, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return data.get("scenarios", [])


def _scenario_id(s: dict) -> str:
    return s["id"]


# pytest 收集阶段加载一次, 缓存
_SCENARIOS = _load_scenarios()


# ────────────────────────────────────────────────────────────
# pytest fixtures
# ────────────────────────────────────────────────────────────
@pytest.fixture(scope="session")
def zhipu_api_key() -> str:
    """从 .env 或环境变量读 ZHIPU (GLM) API key。
    缺 key 时整个模块的测试 skip (CI 上可选跑)。"""
    project_root = Path(__file__).resolve().parents[2]
    env_file = project_root / ".env"
    if env_file.exists():
        from dotenv import load_dotenv
        load_dotenv(env_file)
    key = os.getenv("ZHIPU_API_KEY", "")
    if not key:
        pytest.skip("未配置 ZHIPU_API_KEY, 跳过真实对话测试")
    return key


# ────────────────────────────────────────────────────────────
# 场景运行器 (单 session, 顺序跑所有场景)
# ────────────────────────────────────────────────────────────
@pytest.fixture
def chat_session(page, app_url, zhipu_api_key, request):
    """每个场景一个 chat 会话 (page 是 function 级别, 不能跨场景共享)。"""
    chat = ChatPage(page, app_url)
    # 调试模式: -k debug 或 DEBUG_FIXTURE=1 时开
    debug = os.getenv("DEBUG_FIXTURE") == "1" or "debug" in (request.config.getoption("-k") or "")
    # 用 glm-4 (zhipu coding plan 默认模型), 避开 GLM-5.1 在某些账号下不可用的问题
    chat.setup_session(api_key=zhipu_api_key, provider="zhipu", model="glm-4", debug=debug)
    return chat


def _reset_session(chat: ChatPage) -> None:
    """(已弃用) chat_session 是 function 级别 fixture, 每个场景拿全新 page,
    Streamlit session_state 自然隔离, 不需要手动 reset。
    留着空函数以防外部脚本调用, 但不再在 test_chat_scenario 里调。"""
    pass


def _save_reply(scenario_id: str, reply: str) -> None:
    """把 AI 响应写到 reports/scenarios/<id>.md, 方便人工核查。"""
    out_dir = Path(__file__).parent / "reports" / "scenarios"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / f"{scenario_id}.md").write_text(reply, encoding="utf-8")


# ────────────────────────────────────────────────────────────
# 断言工具
# ────────────────────────────────────────────────────────────
def _eval_expect(
    reply: str,
    expect: dict[str, Any],
    chat: ChatPage,
) -> list[str]:
    """对单条 reply 跑全部 expect, 返回错误信息列表 (空=全过)。"""
    errors: list[str] = []

    # contains
    if "contains" in expect:
        mode = expect.get("contains_mode", "all")
        kws = expect["contains"]
        if mode == "all":
            missing = [k for k in kws if k not in reply]
            if missing:
                errors.append(f"缺少关键词 {missing}")
        else:  # any
            if not any(k in reply for k in kws):
                errors.append(f"关键词 {kws} 一个都不匹配")

    # contains_any (单字段, 等价于 contains_mode=any)
    if "contains_any" in expect:
        kws = expect["contains_any"]
        if not any(k in reply for k in kws):
            errors.append(f"contains_any {kws} 全部不匹配")

    # not_contains
    if "not_contains" in expect:
        bad = [k for k in expect["not_contains"] if k in reply]
        if bad:
            errors.append(f"不应包含 {bad}")

    # regex
    if "regex" in expect:
        pat = expect["regex"]
        if not re.search(pat, reply):
            errors.append(f"正则 {pat!r} 不匹配")

    # min_length / max_length
    if "min_length" in expect and len(reply) < expect["min_length"]:
        errors.append(f"长度 {len(reply)} < min {expect['min_length']}")
    if "max_length" in expect and len(reply) > expect["max_length"]:
        errors.append(f"长度 {len(reply)} > max {expect['max_length']}")

    # tool_called
    if "tool_called" in expect:
        try:
            chat.assert_tool_called(expect["tool_called"])
        except AssertionError as e:
            errors.append(str(e).split("\n")[0])

    # min_messages (整个会话至少 N 条 assistant 消息)
    if "min_messages" in expect:
        actual = len(chat.all_assistant_texts())
        if actual < expect["min_messages"]:
            errors.append(f"assistant 消息数 {actual} < {expect['min_messages']}")

    return errors


# ────────────────────────────────────────────────────────────
# 真实 pytest 用例 (parametrize over YAML)
# ────────────────────────────────────────────────────────────
@pytest.mark.parametrize("scenario", _SCENARIOS, ids=_scenario_id)
def test_chat_scenario(chat_session, scenario: dict[str, Any]) -> None:
    """跑一个 YAML 场景。失败时报告里能看到 AI 的实际响应。"""
    chat = chat_session
    messages = scenario["messages"]
    expect = scenario.get("expect", {})
    timeout = scenario.get("timeout", 60)
    save_reply = scenario.get("save_reply", False)
    tags = scenario.get("tags", [])

    # 不需要 _reset_session: chat_session 是 function 级 fixture,
    # 每个 parametrize 用例拿全新 page + Streamlit session_state 已经隔离。
    # 调用 _reset_session (再点一次 新建会话) 会触发 rerun, 让 Provider/Model
    # 的 selectbox / value= 重新计算, 把 selectbox 没真正 commit 的 zhipu
    # 状态退回 anthropic 默认, 导致 Agent 初始化失败。

    # 跑多轮对话
    replies = chat.run_scenario(messages, timeout=timeout)
    last_reply = replies[-1] if replies else ""

    # 保存响应
    if save_reply:
        _save_reply(scenario["id"], last_reply)

    # 断言最后一轮 (用 :all 模式可断言全部, 这里只断最后一轮)
    errors = _eval_expect(last_reply, expect, chat)

    # 给报告加点上下文
    info = {
        "scenario_id": scenario["id"],
        "scenario_name": scenario["name"],
        "tags": tags,
        "messages": messages,
        "replies_count": len(replies),
        "last_reply_preview": last_reply[:300],
    }
    if errors:
        pytest.fail(
            f"场景 [{scenario['id']}] 失败:\n  "
            + "\n  ".join(errors)
            + f"\n\n  AI 响应: {last_reply[:500]}"
        )
