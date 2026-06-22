from agent_core.memory.extraction_gate import ExtractionGate, TurnContext

def test_below_10k_no_keyword_skips():
    gate = ExtractionGate()
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
    gate = ExtractionGate()
    ctx = TurnContext(
        session_id="s1",
        cumulative_tokens=12_000,  # >= 10K
        cumulative_tool_calls=0,
        last_messages=[{"role": "user", "content": "Go 的 goroutine 调度"}],
        gate1_period_start_turn=0,
    )
    decision = gate.should_extract(ctx)
    assert decision.should_extract is False  # 门3 没实现,默认 False
    assert "gate3" in decision.reason
    assert decision.via_gate1 is True  # 门1 主导

def test_below_10k_with_keyword_enters_gate3():
    gate = ExtractionGate()
    ctx = TurnContext(
        session_id="s1",
        cumulative_tokens=3_000,
        cumulative_tool_calls=0,
        last_messages=[{"role": "user", "content": "记住我喜欢用 uv"}],
        gate1_period_start_turn=0,
    )
    decision = gate.should_extract(ctx)
    assert decision.should_extract is False  # 门3 stub
    assert "gate3" in decision.reason
    assert decision.via_gate1 is False  # 门2 主导

def test_keyword_list_has_16_items():
    gate = ExtractionGate()
    assert len(gate.KEYWORDS) == 16
    assert "记住" in gate.KEYWORDS
    assert "总是" in gate.KEYWORDS
    assert "习惯" in gate.KEYWORDS