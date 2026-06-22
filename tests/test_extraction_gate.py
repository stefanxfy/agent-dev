from unittest.mock import MagicMock

from agent_core.memory.extraction_gate import ExtractionGate, TurnContext

def test_below_10k_no_keyword_skips():
    store = MagicMock()
    store.list_by_session.return_value = []
    router = _make_mock_router('{"should_extract": false, "confidence": 0.0, "reason": "x", "candidates": []}')
    gate = ExtractionGate(llm_router=router, memory_store=store, session_id="s1")
    ctx = TurnContext(
        session_id="s1",
        cumulative_tokens=5_000,  # < 10K
        cumulative_tool_calls=0,   # < 10
        last_messages=[{"role": "user", "content": "今天天气不错"}],
        gate1_period_start_turn=0,
    )
    decision = gate.should_extract(ctx)
    assert decision.should_extract is False
    assert "no_trigger" in decision.reason

def test_above_10k_no_keyword_enters_gate3():
    store = MagicMock()
    store.list_by_session.return_value = []
    router = _make_mock_router('{"should_extract": false, "confidence": 0.3, "reason": "技术闲聊", "candidates": []}')
    gate = ExtractionGate(llm_router=router, memory_store=store, session_id="s1")
    ctx = TurnContext(
        session_id="s1",
        cumulative_tokens=12_000,  # >= 10K
        cumulative_tool_calls=0,
        last_messages=[{"role": "user", "content": "Go 的 goroutine 调度"}],
        gate1_period_start_turn=0,
    )
    decision = gate.should_extract(ctx)
    assert decision.should_extract is False  # LLM says no / low confidence
    assert decision.via_gate1 is True  # 门1 主导

def test_below_10k_with_keyword_enters_gate3():
    store = MagicMock()
    store.list_by_session.return_value = []
    router = _make_mock_router('{"should_extract": false, "confidence": 0.5, "reason": "x", "candidates": []}')
    gate = ExtractionGate(llm_router=router, memory_store=store, session_id="s1")
    ctx = TurnContext(
        session_id="s1",
        cumulative_tokens=3_000,
        cumulative_tool_calls=0,
        last_messages=[{"role": "user", "content": "记住我喜欢用 uv"}],
        gate1_period_start_turn=0,
    )
    decision = gate.should_extract(ctx)
    assert decision.should_extract is False  # LLM 不抽 / 低置信度
    assert decision.via_gate1 is False  # 门2 主导

def test_keyword_list_has_16_items():
    store = MagicMock()
    store.list_by_session.return_value = []
    router = _make_mock_router('{"should_extract": false, "confidence": 0.0, "reason": "x", "candidates": []}')
    gate = ExtractionGate(llm_router=router, memory_store=store, session_id="s1")
    assert len(gate.KEYWORDS) == 16
    assert "记住" in gate.KEYWORDS
    assert "总是" in gate.KEYWORDS
    assert "习惯" in gate.KEYWORDS


def _make_mock_router(json_text: str) -> MagicMock:
    """构造返回固定 JSON 的 mock LLM router"""
    mock = MagicMock()

    def fake_chat(messages, **kw):
        chunk = MagicMock()
        chunk.text_delta.text = json_text
        yield chunk

    mock.chat = fake_chat
    mock.config.provider = "mock"
    return mock


def test_high_confidence_extracts():
    router = _make_mock_router('''{
      "should_extract": true,
      "confidence": 0.85,
      "reason": "用户偏好明确",
      "candidates": [
        {
          "type": "user",
          "title": "用户姓名",
          "body": "用户名叫张三",
          "source_quote": "我叫张三"
        }
      ]
    }''')
    store = MagicMock()
    store.list_by_session.return_value = []
    gate = ExtractionGate(llm_router=router, memory_store=store, session_id="s1")
    ctx = TurnContext(
        session_id="s1",
        cumulative_tokens=12_000,
        cumulative_tool_calls=0,
        last_messages=[{"role": "user", "content": "记住我叫张三"}],
        gate1_period_start_turn=0,
    )
    decision = gate.should_extract(ctx)
    assert decision.should_extract is True
    assert decision.confidence == 0.85
    assert len(decision.candidates) == 1
    assert decision.candidates[0].title == "用户姓名"


def test_low_confidence_skips():
    router = _make_mock_router('''{
      "should_extract": true,
      "confidence": 0.4,
      "reason": "信息不明确",
      "candidates": []
    }''')
    store = MagicMock()
    store.list_by_session.return_value = []
    gate = ExtractionGate(llm_router=router, memory_store=store, session_id="s1")
    ctx = TurnContext(
        session_id="s1",
        cumulative_tokens=12_000,
        cumulative_tool_calls=0,
        last_messages=[{"role": "user", "content": "Go 协程调度"}],
        gate1_period_start_turn=0,
    )
    decision = gate.should_extract(ctx)
    assert decision.should_extract is False
    assert "low_confidence" in decision.reason
    assert "0.40" in decision.reason


def test_parse_error_logs_and_skips():
    """LLM 返回非 JSON → 记 raw + skip"""
    router = _make_mock_router("not valid json {{")
    store = MagicMock()
    store.list_by_session.return_value = []
    gate = ExtractionGate(llm_router=router, memory_store=store, session_id="s1")
    ctx = TurnContext(
        session_id="s1",
        cumulative_tokens=12_000,
        cumulative_tool_calls=0,
        last_messages=[{"role": "user", "content": "Python 学习"}],
        gate1_period_start_turn=0,
    )
    decision = gate.should_extract(ctx)
    assert decision.should_extract is False
    assert "parse_error" in decision.reason or "no_candidates" in decision.reason