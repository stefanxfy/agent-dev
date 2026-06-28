"""
Day 7 改进：测试 usage 持久化 + F5 刷新后 baseline 恢复

验证：
1. add_assistant_message(usage=...) 把 usage 写入 jsonl entry 顶层
2. _restore_usage_baseline 从 jsonl 读最后一条带 usage 的 entry 恢复 baseline
3. 老 session（无 usage 字段）走 fallback 路径不报错
4. _estimate_used_tokens 增量路径用恢复的 baseline，不再走全量字面估算
"""

import json
import tempfile
import os
import pytest

from agent_core.context.manager import ContextManager
from agent_core.llm.router import LLMRouter, LLMConfig
from agent_core.session.storage import SessionStorage
from agent_core.session.manager import SessionManager
from agent_core.tools.base import ToolRegistry


@pytest.fixture
def tmp_data_dir():
    d = tempfile.mkdtemp()
    yield d
    # cleanup is best-effort; temp dir will be reaped by OS


def _make_router():
    return LLMRouter(LLMConfig(provider='zhipu', model='glm-4', api_key='mock'))


def test_usage_persisted_to_jsonl_entry(tmp_data_dir):
    """测试 1：add_assistant_message(usage=...) 写入 jsonl entry.usage"""
    storage = SessionStorage(session_id='test1', data_dir=tmp_data_dir)
    storage.add_message('user', '你好')
    storage.add_message('assistant', '你好！', usage={
        'input_tokens': 12345,
        'output_tokens': 67,
        'thinking_tokens': 8,
        'cached_tokens': 0,
    })
    storage.flush()

    # 读 jsonl
    with open(f'{tmp_data_dir}/test1.jsonl') as f:
        entries = [json.loads(l) for l in f if l.strip()]

    # 找 assistant entry
    asst = [e for e in entries if e['type'] == 'assistant'][0]
    assert asst['usage']['input_tokens'] == 12345
    assert asst['usage']['output_tokens'] == 67
    assert asst['usage']['thinking_tokens'] == 8
    print(f'✅ 测试 1 通过：jsonl entry.usage = {asst["usage"]}')


def test_baseline_restored_from_jsonl_usage(tmp_data_dir):
    """测试 2：F5 刷新后 baseline 从 jsonl usage 恢复"""
    sid = 'test2'
    # 第一次会话：写带 usage 的历史
    storage = SessionStorage(session_id=sid, data_dir=tmp_data_dir)
    storage.add_message('user', 'Q1')
    storage.add_message('assistant', 'A1', usage={
        'input_tokens': 33345, 'output_tokens': 24, 'thinking_tokens': 7, 'cached_tokens': 0,
    })
    storage.add_message('user', 'Q2')
    storage.add_message('assistant', 'A2', usage={
        'input_tokens': 33400, 'output_tokens': 30, 'thinking_tokens': 5, 'cached_tokens': 0,
    })
    storage.flush()

    # 模拟 F5 刷新：创建新 manager + agent
    mgr = SessionManager(session_id=sid, data_dir=tmp_data_dir)
    # 模拟 agent 重建：调 _restore_usage_baseline
    from agent_core.agent_core import ReactAgent
    agent = ReactAgent(
        llm_router=_make_router(),
        tool_registry=ToolRegistry(),
        session_id=sid,
        session_data_dir=tmp_data_dir,
    )

    # 验证 baseline 是最后一条 entry 的 input_tokens（33400），不是字面估算
    assert agent.context_manager.budget._baseline_tokens == 33400
    # 最后一条带 usage 的 entry 是 assistant A2，它的 input_tokens 不含自己
    # 刷新后 messages=4, baseline_msg_count = 4-1 = 3, 增量算 msgs[3:] = A2
    assert agent.context_manager.budget._baseline_msg_count == 3
    assert agent.context_manager.budget._baseline_valid is True

    # 验证增量估算输出 = baseline + 新增 1 条(A2) 的估算
    # 刷新后 messages=4, baseline_msg_count=3, 增量算 msgs[3:] = A2
    state = agent.context_manager.budget.compute_budget_state(agent.messages)
    # A2 msg tokens 取决于 tokenizer（tiktoken: 7, 启发式: 10）
    a2_tokens = agent.context_manager.token_counter.count_messages(
        [m for m in agent.messages if m.get("content") == "A2"]
    )
    assert state.used_tokens == 33400 + a2_tokens  # baseline + A2 估算
    print(f'✅ 测试 2 通过：F5 后 baseline=33,400 (API 真实)，used_tokens={state.used_tokens:,}')


def test_old_session_without_usage_fallback(tmp_data_dir):
    """测试 3：老 session（无 usage 字段）走 fallback 不报错"""
    sid = 'test3'
    # 写不带 usage 的老格式
    storage = SessionStorage(session_id=sid, data_dir=tmp_data_dir)
    storage.add_message('user', 'Q1')
    storage.add_message('assistant', 'A1')  # 无 usage
    storage.flush()

    # 验证 entry 没有 usage 字段
    with open(f'{tmp_data_dir}/test3.jsonl') as f:
        entries = [json.loads(l) for l in f if l.strip()]
    asst = [e for e in entries if e['type'] == 'assistant'][0]
    assert 'usage' not in asst or asst.get('usage') is None

    # F5 刷新：不应该报错
    from agent_core.agent_core import ReactAgent
    agent = ReactAgent(
        llm_router=_make_router(),
        tool_registry=ToolRegistry(),
        session_id=sid,
        session_data_dir=tmp_data_dir,
    )

    # baseline 应该是 0（fallback 状态）
    assert agent.context_manager.budget._baseline_valid is False
    assert agent.context_manager.budget._baseline_tokens == 0
    # 走全量字面估算（虽然不准但能用）
    state = agent.context_manager.budget.compute_budget_state(agent.messages)
    assert state.used_tokens > 0  # 字面估算
    print(f'✅ 测试 3 通过：老 session 无 usage，fallback 到字面估算={state.used_tokens}')


def test_baseline_with_incremental_new_messages(tmp_data_dir):
    """测试 4：F5 恢复 baseline 后，新增消息走增量估算"""
    sid = 'test4'
    storage = SessionStorage(session_id=sid, data_dir=tmp_data_dir)
    storage.add_message('user', 'Q1')
    storage.add_message('assistant', 'A1', usage={
        'input_tokens': 1000, 'output_tokens': 50, 'thinking_tokens': 0, 'cached_tokens': 0,
    })
    storage.flush()

    # F5 后创建 agent
    from agent_core.agent_core import ReactAgent
    agent = ReactAgent(
        llm_router=_make_router(),
        tool_registry=ToolRegistry(),
        session_id=sid,
        session_data_dir=tmp_data_dir,
    )
    assert agent.context_manager.budget._baseline_tokens == 1000

    # 模拟用户发新消息 + agent 调 LLM
    agent.messages.append({'role': 'user', 'content': '新问题'})  # ~12 tokens 字面

    # 增量估算：baseline(1000) + 新增(12) = ~1012
    state = agent.context_manager.budget.compute_budget_state(agent.messages)
    print(f'  增量估算: baseline=1,000 + 新增(1 条)={state.used_tokens - 1000} = {state.used_tokens}')

    # 验证：是增量不是全量
    assert state.used_tokens > 1000  # 至少是 baseline + 一点点
    assert state.used_tokens < 2000  # 不应该全量算 1 + 1 = 14
    print(f'✅ 测试 4 通过：F5 后增量估算正确，{state.used_tokens} 在 [1000, 2000]')


def test_compact_invalidates_baseline():
    """测试 5：压缩后 baseline 失效（防止数字失真）"""
    from agent_core.context.manager import ContextManager
    cm = ContextManager(llm_router=_make_router(), model='glm-4')
    cm.budget.set_baseline(1000, 5)
    assert cm.budget._baseline_valid is True
    cm.budget.invalidate_baseline()
    assert cm.budget._baseline_valid is False
    print('✅ 测试 5 通过：invalidate_baseline 正常工作')


def test_restore_baseline_o1_via_tail_window(tmp_data_dir):
    """测试 6：O(1) 快路径—tail 64KB 窗口能装下最后一条 assistant"""
    sid = 'test_o1_tail'
    storage = SessionStorage(session_id=sid, data_dir=tmp_data_dir)

    # 写 50 条短 entry（总 ≈ 50KB，在 64KB 窗口内）
    for i in range(50):
        storage.add_message('user', f'Q{i}')
        storage.add_message('assistant', f'A{i}', usage={
            'input_tokens': 1000 + i, 'output_tokens': 50, 'thinking_tokens': 0, 'cached_tokens': 0,
        })
    storage.flush()

    from agent_core.agent_core import ReactAgent
    agent = ReactAgent(
        llm_router=_make_router(),
        tool_registry=ToolRegistry(),
        session_id=sid,
        session_data_dir=tmp_data_dir,
    )

    # 最后一条 assistant usage input_tokens = 1049
    assert agent.context_manager.budget._baseline_tokens == 1049
    assert agent.context_manager.budget._baseline_valid is True
    print('✅ 测试 6 通过：O(1) tail 64KB 找到 baseline=1,049')


def test_restore_baseline_fallback_when_tail_misses(tmp_data_dir):
    """测试 7：兑底路径—灌水 entry > 64KB 时 fallback 到全量扫"""
    sid = 'test_fallback_big'
    storage = SessionStorage(session_id=sid, data_dir=tmp_data_dir)

    storage.add_message('user', '灌水测试')
    # 写 1 条 80KB content（超 64KB 窗口）的灌水 entry
    big_content = 'A' * 80_000
    storage.add_message('assistant', big_content, usage={
        'input_tokens': 50000, 'output_tokens': 100, 'thinking_tokens': 0, 'cached_tokens': 0,
    })
    storage.flush()

    from agent_core.agent_core import ReactAgent
    agent = ReactAgent(
        llm_router=_make_router(),
        tool_registry=ToolRegistry(),
        session_id=sid,
        session_data_dir=tmp_data_dir,
    )

    # 即使 tail 64KB 漏掉灌水 entry，fallback 仍能找到
    assert agent.context_manager.budget._baseline_tokens == 50000
    print('✅ 测试 7 通过：fallback 路径生效，找到 50,000 baseline')


def test_restore_baseline_no_usage_fallback(tmp_data_dir):
    """测试 8：老 jsonl 无 usage 字段，静默 fallback 不报错"""
    sid = 'test_no_usage'
    storage = SessionStorage(session_id=sid, data_dir=tmp_data_dir)
    storage.add_message('user', 'Q1')
    storage.add_message('assistant', 'A1')  # 无 usage
    storage.flush()

    from agent_core.agent_core import ReactAgent
    agent = ReactAgent(
        llm_router=_make_router(),
        tool_registry=ToolRegistry(),
        session_id=sid,
        session_data_dir=tmp_data_dir,
    )

    # baseline 无效，fallback 到字面估算
    assert agent.context_manager.budget._baseline_valid is False
    assert agent.context_manager.budget._baseline_tokens == 0
    state = agent.context_manager.budget.compute_budget_state(agent.messages)
    assert state.used_tokens > 0  # 字面估算有值
    print(f'✅ 测试 8 通过：老数据无 usage，静默 fallback 估算={state.used_tokens}')


if __name__ == '__main__':
    import sys
    sys.exit(pytest.main([__file__, '-v', '--tb=short']))
