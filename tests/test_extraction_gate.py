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
    """构造返回固定 JSON 的 mock LLM router(invoke() 走与 chat() 相同路径)"""
    mock = MagicMock()

    def fake_chat(messages, **kw):
        chunk = MagicMock()
        chunk.text_delta.text = json_text
        yield chunk

    def fake_invoke(messages, *, cache_namespace=None, **kwargs):
        """gate._call_llm 改走 invoke() — 聚合 fake_chat 的 chunks。"""
        chunks = list(fake_chat(messages, cache_namespace=cache_namespace, **kwargs))
        return "".join(c.text_delta.text for c in chunks if c.text_delta is not None)

    mock.chat = fake_chat
    mock.invoke = fake_invoke
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
    )
    decision = gate.should_extract(ctx)
    assert decision.should_extract is False
    assert "parse_error" in decision.reason or "no_candidates" in decision.reason


def test_markdown_code_fence_stripped():
    """LLM 把 JSON 包在 ```json ... ``` 里 → fence 剥掉后正常解析
    复现 2026-06-24 生产日志:raw='```json\\n{...}\\n```' 解析失败
    """
    # 用真实 bug 现场的格式(用户截图中的 raw)
    raw = '''```json
{
  "should_extract": true,
  "confidence": 0.95,
  "reason": "用户偏好",
  "candidates": [
    {
      "type": "user",
      "title": "饮食偏好:不吃折耳根",
      "body": "用户明确表示不喜欢折耳根",
      "source_quote": "我不吃折耳根"
    }
  ]
}
```'''
    router = _make_mock_router(raw)
    store = MagicMock()
    store.list_by_session.return_value = []
    gate = ExtractionGate(llm_router=router, memory_store=store, session_id="s1")
    ctx = TurnContext(
        session_id="s1",
        cumulative_tokens=12_000,
        cumulative_tool_calls=0,
        last_messages=[{"role": "user", "content": "记住,我不吃折耳根"}],
    )
    decision = gate.should_extract(ctx)
    # 关键断言:不返回 parse_error,正常提取
    assert "parse_error" not in decision.reason, (
        f"fence 剥除失败,decision.reason={decision.reason!r}"
    )
    assert decision.should_extract is True
    assert decision.confidence == 0.95


def test_markdown_fence_without_json_lang():
    """```\\n{...}\\n``` 无语言标签的 fence 也要支持"""
    raw = '''```
{"should_extract": false, "confidence": 0.2, "reason": "闲聊", "candidates": []}
```'''
    router = _make_mock_router(raw)
    store = MagicMock()
    store.list_by_session.return_value = []
    gate = ExtractionGate(llm_router=router, memory_store=store, session_id="s1")
    ctx = TurnContext(
        session_id="s1",
        cumulative_tokens=12_000,
        cumulative_tool_calls=0,
        last_messages=[{"role": "user", "content": "今天吃什么好"}],
    )
    decision = gate.should_extract(ctx)
    assert "parse_error" not in decision.reason
    assert decision.should_extract is False