# Quickstart: Verifying the ReAct Inline XML Fallback

**Branch**: `feature/react-inline-xml-fallback-parser`
**Date**: 2026-07-07

> End-to-end validation scenarios. Each step is runnable, each assertion
> is automated, each "expected outcome" matches a Success Criterion (SC)
> from [spec.md](spec.md).

## Prerequisites

- Python 3.11
- `uv` virtualenv activated
- All deps installed (`uv sync` or equivalent)
- LLM provider config in `~/.agent_data/config.yaml` (any provider)

## Setup

```bash
cd /Users/fanyunxu/Desktop/myproject/agent-dev

# 1. Verify you're on the right branch
git branch --show-current
# expected: feature/react-inline-xml-fallback-parser

# 2. Run parser unit tests (8 fixtures + edge cases)
python3 -m pytest -q tests/test_inline_xml_parser.py
# expected: all pass

# 3. Run handler-level tests + benchmark
python3 -m pytest -q tests/test_inline_xml_handler.py
# expected: all pass; benchmark asserts < 5ms for single-block

# 4. Run byte-equal log audit (mirrors 002-skill-secret-injection SC-005)
python3 -m pytest -q tests/test_inline_xml_log_audit.py
# expected: all pass — confirms no input bytes ever logged

# 5. Targeted regression gate (per "Test scope matches change scope" —
#    全量 pytest -q 在 master 上有 65 pre-existing fail / 70 error,与 003 无关)
python3 -m pytest -q \
  tests/test_inline_xml_parser.py \
  tests/test_inline_xml_handler.py \
  tests/test_inline_xml_log_audit.py \
  tests/test_builder.py \
  tests/test_builder_e2e.py
# expected: ≥ 18 new inline-xml tests + 旧 chain 长度相关 2 个测试已更新到 4 handler
```

## Scenario 1 — Single-block fallback activates (SC-001 + FR-001)

**Purpose**: Reproduce the user's 2026-07-07 UI observation: GLM-5.1
emits inline-XML for the mandatory Bash call, fallback activates, tool
runs.

```bash
# Run the parser on the canonical single-block fixture
python3 -m pytest -q tests/test_inline_xml_parser.py::TestUS1SingleBlock::test_single_block_parses_to_one_tool_call
# expected: 1 passed
```

**Expected log** (one INFO line per activation, no input bytes):

```
INFO  🧩 react.inline_xml_fallback: provider=<8-char-sha1> blocks=1 first_tool=Bash original_stop=stop
```

**Expected outcome**: `tool_calls` list contains 1 `ToolCallDelta` with
`tool_name="Bash"`, `tool_input={"command": "echo hi"}`,
`tool_use_id="inline_xml_0"`.

## Scenario 2 — Multi-block parsing (P2 user story + FR-001)

```bash
python3 -m pytest -q "tests/test_inline_xml_parser.py::TestUS2MultipleBlocks::test_multiple_blocks_parse_in_document_order"
# expected: 1 passed, tool_calls length == 3
```

**Expected**: blocks parsed in left-to-right order, each becomes a
distinct `ToolCallDelta` with sequential `inline_xml_{0,1,2}` IDs.

## Scenario 3 — Malformed block isolated (P2 + FR-005)

```bash
python3 -m pytest -q "tests/test_inline_xml_parser.py::TestUS3Malformed::test_malformed_inside_valid_skips_bad_keeps_good"
# expected: 1 passed; valid block produces tool_call; malformed logged WARN
```

**Expected log**:

```
WARN  🧩 react.inline_xml_fallback: malformed block index=1 offset=42 (json decode error: ...)
```

(no input bytes — only the offset and a generic error class name)

## Scenario 4 — No-op when structured calls present (FR-004)

```bash
python3 -m pytest -q "tests/test_inline_xml_handler.py::TestUS1NoOpWhenStructured::test_handler_no_op_when_structured_calls_present"
# expected: 1 passed; fallback_activated == False; stage_outputs.tool_calls unchanged
```

**Expected**: zero INFO log lines; fallback does NOT touch structured
tool calls.

## Scenario 5 — Configuration opt-out (FR-011 + SC-006)

```bash
# Edit config to disable:
#   inline_xml_fallback:
#     enabled: false
python3 -m pytest -q "tests/test_inline_xml_handler.py::TestUS4ActivationLog::test_disabled_by_config_no_op_with_debug_log"
# expected: 1 passed
```

**Expected log** (DEBUG, not INFO):

```
DEBUG 🧩 react.inline_xml_fallback: disabled by config (inline_xml_fallback.enabled=False)
```

## Scenario 6 — Performance benchmark (SC-005)

```bash
python3 -m pytest -q "tests/test_inline_xml_handler.py::TestUS4ActivationLog::test_single_block_under_5ms_benchmark"
# expected: 1 passed; benchmark reports < 5 ms for 1000-iter mean
```

**Expected**: parser latency budget verified.

## Scenario 7 — Provider-agnostic (FR-009)

```bash
python3 -m pytest -q "tests/test_inline_xml_parser.py::TestEdgeCases::test_all_providers"
# expected: 1 passed (drives 4 providers — Anthropic / OpenAI / GLM / Zhipu)
```

**Expected**: same parser output regardless of provider; the only
provider-specific thing in logs is the hashed provider id.

## Scenario 8 — End-to-end reproduction of user observation (SC-001)

This is the original user-reported scenario. It requires a real LLM
provider (GLM-5.1 recommended) and the `echo-skill` config from
002-skill-secret-injection:

```bash
# 1. Verify echo-skill + SECRET_DEMO is configured
grep -A 5 "echo-skill" ~/.agent_data/config.yaml
# expected:
#   echo-skill:
#     secrets:
#       SECRET_DEMO: 'inline_mvp_test_value_42'

# 2. Launch the UI
streamlit run web/app.py

# 3. In the chat: send "run echo-skill with secret"
# 4. Observe (vs. 2026-07-07 broken behavior):
#    - Bash tool is invoked (action card appears)
#    - tool result contains "inline_mvp_test_value_42"
#    - ReAct loop continues instead of FINALIZING

# 5. Check the log
tail -f logs/app/agent.log | grep "🧩 react.inline_xml_fallback"
# expected: exactly 1 INFO line per LLM turn where the model emitted inline-XML
```

**Expected outcome**: same UI as if the model had used structured
`tool_use` blocks — the user cannot tell the difference.

## Rollback

If a regression surfaces after merge:

```bash
# Disable in config (no code change needed):
cat >> ~/.agent_data/config.yaml <<'YAML'
agent:
  inline_xml_fallback:
    enabled: false
YAML

# Or, hard-revert the branch:
git checkout feature/skill-system  # last-known-good
```

The fallback is **opt-out** by design (FR-011) so rollback never
requires a code revert for users — only a config change.

## Reporting template (per 反偷懒规则 第 4 条)

After running scenarios, report three things:

1. **完成什么** — list of files created/modified, line counts
2. **测试结果** — pytest output for each scenario, before/after counts
3. **偏差说明** — any deviation from this quickstart, with reason

## Done When

- [ ] All 8 scenarios pass
- [ ] Full test suite exits 0
- [ ] One user-facing 三件套 report delivered